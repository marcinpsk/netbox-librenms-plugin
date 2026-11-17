"""
The master of a virtual chassis holds every LibreNMS mapping of the chassis.

Each test drives the real sync page, the real action routes or the real status list, and reads the
real database. The LibreNMS servers are loopback HTTP fakes that record each request.
"""

from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from dcim.models import Device
from django.urls import reverse

from django.contrib import messages
from django.utils.html import escape

from netbox_librenms_plugin.mapping_owner import MISPLACED_MESSAGE, NO_MASTER_MESSAGE, NOT_OWNER_MESSAGE
from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.tests.conftest import (
    configure_librenms_servers,
    ip_on,
    make_superuser,
    make_virtual_chassis_members,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.link_intent_helpers import message_texts
from netbox_librenms_plugin.tests.mapping_fixtures import mapped_on_server_constraint, mapping_summary, seed_mapping
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.test_server_mapping_edit import _editor_forms
from netbox_librenms_plugin.tests.view_test_helpers import domain_state, grant, make_user_with_perms
from netbox_librenms_plugin.transactions import outside_change_scope

PRIMARY, SECONDARY, RETIRED = "primary", "secondary", "retired"


@pytest.fixture
def servers(settings, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with ExitStack() as stack:
        primary = stack.enter_context(librenms_mock_server())
        secondary = stack.enter_context(librenms_mock_server())
        configure_librenms_servers(
            settings,
            {
                key: {"display_name": f"{key.title()} LibreNMS", "librenms_url": server.url, "api_token": "owner-token"}
                for key, server in ((PRIMARY, primary), (SECONDARY, secondary))
            },
        )
        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": PRIMARY})
        yield SimpleNamespace(primary=primary, secondary=secondary)


def _requests(servers):
    return servers.primary.requests + servers.secondary.requests


def _chassis(tag, *, count=2, master=True):
    """Return a chassis and its members in position order; the first member is the master unless *master* is False."""
    chassis, members = make_virtual_chassis_members(tag, count=count)
    if not master:
        chassis.master = None
        chassis.save()
    return chassis, members


def _sync_url(obj, **query):
    from urllib.parse import urlencode

    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[obj.pk])
    return f"{url}?{urlencode(query)}" if query else url


def _move_url(obj):
    return reverse("plugins:netbox_librenms_plugin:move_chassis_mappings", args=[obj.pk])


def _move(client, page):
    return client.post(_move_url(page), {"tab": "interfaces"})


def _fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


def _change_records(obj):
    from core.models import ObjectChange
    from django.contrib.contenttypes.models import ContentType

    return ObjectChange.objects.filter(
        changed_object_type=ContentType.objects.get_for_model(type(obj)), changed_object_id=obj.pk
    ).count()


def _all_device_permissions(username, **change):
    """Return a user who may view every device and change the devices that *change* constrains."""
    user = make_user_with_perms(username, [("view", Device)])
    return grant(user, "change", Device, **change)


def _errors(response):
    return [str(message) for message in response.wsgi_request._messages if message.level == messages.ERROR]


def _assert_no_tabs(response):
    html = response.content.decode()
    assert 'class="tab-pane' not in html
    assert response.context.get("interface_sync") is None


@pytest.mark.django_db
@pytest.mark.parametrize("viewed", [0, 1], ids=["holder", "other-member"])
def test_a_chassis_without_a_master_blocks_the_sync_page_of_every_member(client, servers, viewed):
    _vc, members = _chassis("owner-no-master-page", master=False)
    seed_mapping(members[0], PRIMARY, own=7001)
    client.force_login(make_superuser("owner-no-master-page"))
    before = domain_state()

    response = client.get(_sync_url(members[viewed]))

    assert response.status_code == 200
    assert response.context["chassis_owner_problem"]["problem"] == "no_master"
    html = response.content.decode()
    assert 'data-owner-problem="no_master"' in html
    assert 'id="librenms-move-to-master"' not in html
    _assert_no_tabs(response)
    assert domain_state() == before
    assert _requests(servers) == []


@pytest.mark.django_db
def test_an_action_on_a_chassis_without_a_master_refuses_without_a_write(client, servers):
    _vc, (first, second) = _chassis("owner-no-master-post", master=False)
    seed_mapping(first, PRIMARY, own=7002)
    client.force_login(make_superuser("owner-no-master-post"))
    before = domain_state()

    edit = client.post(
        reverse("plugins:netbox_librenms_plugin:edit_server_mapping", args=[first.pk]),
        {"object_type": "device", "server_key": SECONDARY, "own_id": "7003"},
    )
    refresh = client.post(
        reverse("plugins:netbox_librenms_plugin:device_interface_sync", args=[second.pk]),
        {"server_key": PRIMARY},
        HTTP_HX_REQUEST="true",
    )

    assert edit.status_code == 302
    assert _errors(edit) == [NO_MASTER_MESSAGE]
    assert refresh.status_code == 200
    assert refresh["HX-Reswap"] == "none"
    assert escape(NO_MASTER_MESSAGE) in refresh.content.decode()
    assert domain_state() == before
    assert _requests(servers) == []


@pytest.mark.django_db
@pytest.mark.parametrize("viewed", ["master", "holder"])
def test_misplaced_ids_show_the_move_card_on_every_member_page(client, servers, viewed):
    _vc, (master, holder) = _chassis("owner-misplaced-card")
    seed_mapping(holder, PRIMARY, own=7010)
    client.force_login(make_superuser("owner-misplaced-card"))
    before = domain_state()

    response = client.get(_sync_url(master if viewed == "master" else holder))

    assert response.status_code == 200
    problem = response.context["chassis_owner_problem"]
    assert problem["problem"] == "misplaced"
    assert problem["master"]["name"] == master.name
    assert [shown["name"] for shown in problem["holders"]] == [holder.name]
    assert problem["can_move"] is True
    html = response.content.decode()
    assert 'data-owner-problem="misplaced"' in html
    assert 'id="librenms-move-to-master"' in html
    assert f'action="{_move_url(master if viewed == "master" else holder)}"' in html
    _assert_no_tabs(response)
    assert domain_state() == before
    assert _requests(servers) == []


@pytest.mark.django_db
def test_the_move_card_names_only_the_members_that_the_user_may_view(client, servers):
    _vc, (master, holder) = _chassis("owner-misplaced-scope")
    seed_mapping(holder, PRIMARY, own=7011)
    user = make_user_with_perms("owner-misplaced-scope", [("view", Device)], constraints={"pk": master.pk})
    client.force_login(user)

    response = client.get(_sync_url(master))

    problem = response.context["chassis_owner_problem"]
    assert problem["holders"] == []
    assert problem["hidden_holders"] == 1
    assert problem["can_move"] is False
    html = response.content.decode()
    assert holder.name not in html
    assert 'id="librenms-move-to-master"' not in html


@pytest.mark.django_db
def test_misplaced_ids_block_discovery_on_the_master(client, servers):
    _vc, (master, holder) = _chassis("owner-misplaced-discovery")
    master.primary_ip4 = ip_on(master, "198.18.71.1/24", "mgmt0")
    master.save()
    seed_mapping(holder, PRIMARY, own=7020)
    servers.primary.register("/api/v0/devices/198.18.71.1", {"status": "ok", "devices": [{"device_id": 7021}]})
    client.force_login(make_superuser("owner-misplaced-discovery"))
    before = domain_state()

    page = client.get(_sync_url(master))
    refresh = client.post(
        reverse("plugins:netbox_librenms_plugin:device_interface_sync", args=[master.pk]),
        {"server_key": PRIMARY},
        HTTP_HX_REQUEST="true",
    )

    assert page.status_code == 200
    assert page.context["chassis_owner_problem"]["problem"] == "misplaced"
    assert refresh.status_code == 200
    assert escape(MISPLACED_MESSAGE) in refresh.content.decode()
    assert domain_state() == before
    assert _requests(servers) == []


@pytest.mark.django_db
def test_a_member_refresh_never_looks_up_or_stores_an_id_on_the_member(client, servers):
    _vc, (master, member) = _chassis("owner-member-discovery")
    seed_mapping(master, PRIMARY, own=7022)
    member.primary_ip4 = ip_on(member, "198.18.71.2/24", "mgmt0")
    member.save()
    servers.primary.register("/api/v0/devices/198.18.71.2", {"status": "ok", "devices": [{"device_id": 7023}]})
    client.force_login(make_superuser("owner-member-discovery"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[member.pk]),
        {"server_key": PRIMARY},
        HTTP_HX_REQUEST="true",
    )

    assert mapping_summary(_fresh(member)) == {}
    assert response.status_code == 200
    # The refresh reads the stored link of the master; it never matches the member's own address.
    assert "/api/v0/devices/198.18.71.2" not in [request["path"] for request in _requests(servers)]


@transactional_db_with_all_apps()
def test_move_puts_every_mapping_on_the_master_and_clears_each_holder(client, servers, flushed_events):
    _vc, (master, first, second) = _chassis("owner-move", count=3)
    seed_mapping(first, PRIMARY, own=7030, save=False)
    seed_mapping(first, PRIMARY, oob=7031, oob_type="idrac", save=False)
    seed_mapping(first, PRIMARY, preferred=True)
    seed_mapping(second, SECONDARY, own=7032)
    client.force_login(make_superuser("owner-move"))
    flushed_events.clear()

    response = _move(client, second)

    assert response.status_code == 302
    assert response.url == _sync_url(second, tab="interfaces")
    assert mapping_summary(_fresh(master)) == {
        PRIMARY: {"own": 7030, "oob": 7031, "oob_type": "idrac"},
        SECONDARY: {"own": 7032},
        "preferred": PRIMARY,
    }
    assert mapping_summary(_fresh(first)) == {}
    assert mapping_summary(_fresh(second)) == {}
    assert sorted(flushed_events) == sorted([("device", master.pk), ("device", first.pk), ("device", second.pk)])
    assert [_change_records(device) for device in (master, first, second)] == [1, 1, 1]
    assert client.get(_sync_url(second)).context.get("chassis_owner_problem") is None


@pytest.mark.django_db
def test_move_keeps_one_copy_of_a_value_that_the_master_already_holds(client, servers):
    _vc, (master, holder) = _chassis("owner-move-same")
    seed_mapping(master, PRIMARY, own=7033)
    seed_mapping(holder, PRIMARY, own=7033)
    client.force_login(make_superuser("owner-move-same"))

    _move(client, master)

    assert mapping_summary(_fresh(master)) == {PRIMARY: {"own": 7033}}
    assert mapping_summary(_fresh(holder)) == {}


@pytest.mark.django_db
@pytest.mark.parametrize("conflict", ["server", "preference"])
def test_move_refuses_a_value_that_differs_from_the_master_and_writes_nothing(client, servers, conflict):
    _vc, (master, holder) = _chassis(f"owner-move-conflict-{conflict}")
    if conflict == "server":
        seed_mapping(master, PRIMARY, own=7040)
        seed_mapping(holder, PRIMARY, own=7041)
    else:
        seed_mapping(master, PRIMARY, own=7042, save=False)
        seed_mapping(master, PRIMARY, preferred=True)
        seed_mapping(holder, SECONDARY, own=7043, save=False)
        seed_mapping(holder, SECONDARY, preferred=True)
    client.force_login(make_superuser(f"owner-move-conflict-{conflict}"))
    before = domain_state()

    response = _move(client, holder)

    assert response.status_code == 302
    assert domain_state() == before
    (error,) = _errors(response)
    assert ("'primary'" in error) is (conflict == "server")


@pytest.mark.django_db
@pytest.mark.parametrize("changeable", ["master", "holder"])
def test_move_refuses_without_change_permission_on_the_master_and_each_holder(client, servers, changeable):
    _vc, (master, holder) = _chassis(f"owner-move-denied-{changeable}")
    seed_mapping(holder, PRIMARY, own=7050)
    allowed = master if changeable == "master" else holder
    client.force_login(_all_device_permissions(f"owner-move-denied-{changeable}", constraints={"pk": allowed.pk}))
    before = domain_state()

    response = _move(client, allowed)

    from netbox_librenms_plugin.views.sync.chassis_mappings import MOVE_PERMISSION_MESSAGE

    assert response.status_code == 302
    assert domain_state() == before
    assert _errors(response) == [MOVE_PERMISSION_MESSAGE]


@transactional_db_with_all_apps()
def test_move_rolls_back_with_no_event_when_a_holder_leaves_the_change_scope(client, servers, flushed_events):
    _vc, (master, holder) = _chassis("owner-move-read-back")
    seed_mapping(holder, PRIMARY, own=7060)
    # The holder is in the change scope only while it stores a primary entry, so the move takes it out.
    scope = [{"pk": master.pk}, mapped_on_server_constraint(PRIMARY)]
    client.force_login(_all_device_permissions("owner-move-read-back", constraints=scope))
    flushed_events.clear()

    response = _move(client, holder)

    assert response.status_code == 302
    assert mapping_summary(_fresh(master)) == {}
    assert mapping_summary(_fresh(holder)) == {PRIMARY: {"own": 7060}}
    assert flushed_events == []
    assert _change_records(master) == _change_records(holder) == 0
    assert _errors(response) == [str(outside_change_scope(holder))]


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["no-master", "nothing-misplaced"])
def test_move_refuses_a_chassis_that_has_no_misplaced_data(client, servers, state):
    _vc, (first, second) = _chassis(f"owner-move-{state}", master=state != "no-master")
    seed_mapping(first, PRIMARY, own=7070)
    client.force_login(make_superuser(f"owner-move-{state}"))
    before = domain_state()

    response = _move(client, second)

    from netbox_librenms_plugin.views.sync.chassis_mappings import NOTHING_TO_MOVE_MESSAGE

    assert response.status_code == 302
    assert domain_state() == before
    assert message_texts(response) == [NO_MASTER_MESSAGE if state == "no-master" else NOTHING_TO_MOVE_MESSAGE]


@pytest.mark.django_db
def test_split_mappings_move_to_the_master_and_the_member_page_then_works_on_the_master(client, servers):
    _vc, (master, member) = _chassis("owner-split")
    seed_mapping(master, PRIMARY, own=7080)
    seed_mapping(member, SECONDARY, own=7081, save=False)
    seed_mapping(member, RETIRED, own=7082)
    client.force_login(make_superuser("owner-split"))

    blocked = client.get(_sync_url(member, server_key=SECONDARY))
    assert blocked.context["chassis_owner_problem"]["problem"] == "misplaced"
    assert client.post(_move_url(member)).status_code == 302

    page = client.get(_sync_url(member, server_key=SECONDARY))
    assert page.status_code == 200
    assert page.context.get("chassis_owner_problem") is None
    # The selection, the connections header and the controls all read and post to the master.
    assert page.context["server_key"] == SECONDARY
    assert page.context["lookup_device_pk"] == master.pk
    assert {mapping.server_key for mapping in page.context["all_server_mappings"]} == {PRIMARY, SECONDARY, RETIRED}
    html = page.content.decode()
    preferred_url = reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[master.pk])
    remove_url = reverse("plugins:netbox_librenms_plugin:remove_server_mapping", args=[master.pk])
    assert f'action="{preferred_url}"' in html
    assert f'action="{remove_url}"' in html
    forms = _editor_forms(client, member, server_key=SECONDARY)
    edit_url = reverse("plugins:netbox_librenms_plugin:edit_server_mapping", args=[master.pk])
    assert {action for action, _fields in forms.values()} == {edit_url}
    assert forms[SECONDARY][1]["own_id"] == "7081"
    assert forms[PRIMARY][1]["own_id"] == "7080"

    # The editor of the member page replaces the master's ID through the lifecycle confirmation.
    action, fields = forms[SECONDARY]
    confirmation = client.post(action, {**fields, "own_id": "7083"})
    assert confirmation.status_code == 200, message_texts(confirmation)
    context = confirmation.context
    from netbox_librenms_plugin.confirmation_intent import FIELD

    client.post(context["action_url"], {"server_key": context["server_key"], FIELD: [context["token"]]})
    # The Remove control unlinks the stale server from the master.
    client.post(remove_url, {"server_key": RETIRED, "object_type": "device"})
    client.post(preferred_url, {"server_key": SECONDARY, "object_type": "device"})

    assert mapping_summary(_fresh(master)) == {
        PRIMARY: {"own": 7080},
        SECONDARY: {"own": 7083},
        "preferred": SECONDARY,
    }
    assert mapping_summary(_fresh(member)) == {}


@pytest.mark.django_db
def test_the_status_list_reports_each_chassis_outcome_and_discovers_only_on_the_owner(client, servers):
    _vc, (healthy_master, healthy_member) = _chassis("owner-status-ok")
    seed_mapping(healthy_master, PRIMARY, own=7090)
    _vc, (orphan_first, orphan_second) = _chassis("owner-status-orphan", master=False)
    seed_mapping(orphan_first, PRIMARY, own=7091)
    _vc, (misplaced_master, misplaced_holder) = _chassis("owner-status-misplaced")
    seed_mapping(misplaced_holder, PRIMARY, own=7092)
    for device in (healthy_member, orphan_second, misplaced_master):
        device.primary_ip4 = ip_on(device, f"198.18.72.{device.pk % 200 + 1}/24", "mgmt0")
        device.save()
    client.force_login(make_superuser("owner-status"))
    before = domain_state()

    response = client.get(reverse("plugins:netbox_librenms_plugin:device_status_list"), {"q": "owner-status"})

    assert response.status_code == 200
    cells = {row.record.pk: str(row.get_cell("librenms_status")) for row in response.context["table"].rows}
    master_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[healthy_master.pk])
    assert "mdi-check-circle" in cells[healthy_master.pk]
    assert master_url in cells[healthy_member.pk] and healthy_master.name in cells[healthy_member.pk]
    for pk in (orphan_first.pk, orphan_second.pk):
        assert 'data-owner-problem="no_master"' in cells[pk]
    for pk in (misplaced_master.pk, misplaced_holder.pk):
        assert 'data-owner-problem="misplaced"' in cells[pk]
    assert domain_state() == before
    assert _requests(servers) == []


def _import_action(client, servers, device_id, target, action):
    from netbox_librenms_plugin.tests.import_server_helpers import librenms_device

    servers.primary.register(
        f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [librenms_device(device_id, target.name)]}
    )
    for suffix in ("", "/all"):
        servers.primary.register(f"/api/v0/inventory/{device_id}{suffix}", {"status": "ok", "inventory": []})
    return client.post(
        reverse("plugins:netbox_librenms_plugin:device_conflict_action", kwargs={"device_id": device_id}),
        {
            "action": action,
            "existing_device_id": target.pk,
            "existing_device_type": "device",
            "server_key": PRIMARY,
            "use-sysname-toggle": "on",
            "strip-domain-toggle": "off",
        },
        HTTP_HX_REQUEST="true",
    )


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["link", "update", "update_serial"])
@pytest.mark.parametrize(
    ("has_master", "refusal"), [(True, NOT_OWNER_MESSAGE), (False, NO_MASTER_MESSAGE)], ids=["member", "no-master"]
)
def test_an_import_link_never_stores_an_id_on_a_member_that_is_not_the_master(
    client, servers, action, has_master, refusal
):
    _vc, (_first, member) = _chassis(f"owner-import-{action}", master=has_master)
    client.force_login(make_superuser(f"owner-import-{action}-{has_master}"))

    response = _import_action(client, servers, 7100, member, action)

    assert response.status_code == 200
    assert escape(refusal) in response.content.decode()
    assert mapping_summary(_fresh(member)) == {}
    assert mapping_summary(_fresh(_first)) == {}


@pytest.mark.django_db
def test_an_import_link_stores_the_id_on_the_chassis_master(client, servers):
    _vc, (master, _member) = _chassis("owner-import-master")
    client.force_login(make_superuser("owner-import-master"))

    response = _import_action(client, servers, 7101, master, "link")

    assert response.status_code == 200
    assert mapping_summary(_fresh(master)) == {PRIMARY: {"own": 7101}}


@pytest.mark.django_db
def test_an_oob_link_refuses_a_master_that_lost_the_chassis_before_the_lock(client, servers, monkeypatch):
    from dcim.models import VirtualChassis

    from netbox_librenms_plugin.views.imports import actions

    chassis, (master, member, successor) = _chassis("owner-import-oob", count=3)
    seed_mapping(master, PRIMARY, own=7130)
    member.serial = "OWNER-OOB-SERIAL"
    member.save()
    servers.primary.device_info_response(
        device_id=7131, hostname="owner-oob-controller", hardware="Test chassis", serial=member.serial, ip=""
    )
    servers.primary.vc_inventory_callable(7131, [], {})
    resolve = actions.mapping_owner

    def resolve_then_move_the_master(device):
        owner = resolve(device)
        # A concurrent chassis edit commits after the owner read and before the write locks.
        VirtualChassis.objects.filter(pk=chassis.pk).update(master=successor)
        return owner

    monkeypatch.setattr(actions, "mapping_owner", resolve_then_move_the_master)
    client.force_login(make_superuser("owner-import-oob"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_add_as_oob", args=[7131]),
        {"server_key": PRIMARY, "existing_device_id": member.pk},
        HTTP_HX_REQUEST="true",
    )

    assert escape(MISPLACED_MESSAGE) in response.content.decode()
    assert mapping_summary(_fresh(master)) == {PRIMARY: {"own": 7130}}
    assert mapping_summary(_fresh(member)) == {}
    assert mapping_summary(_fresh(successor)) == {}


@pytest.mark.django_db
@pytest.mark.parametrize("selected_leaves", [False, True], ids=["member-stays", "member-leaves"])
def test_an_oob_link_refuses_a_master_that_left_the_chassis_before_the_lock(
    client, servers, monkeypatch, selected_leaves
):
    from dcim.models import VirtualChassis

    from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE
    from netbox_librenms_plugin.views.imports import actions

    chassis, (master, member, successor) = _chassis(f"owner-oob-left-{selected_leaves}", count=3)
    seed_mapping(master, PRIMARY, own=7132)
    member.serial = f"OWNER-OOB-LEFT-{selected_leaves}"
    member.save()
    servers.primary.device_info_response(
        device_id=7133, hostname="owner-oob-left-controller", hardware="Test chassis", serial=member.serial, ip=""
    )
    servers.primary.vc_inventory_callable(7133, [], {})
    resolve = actions.mapping_owner
    leavers = [master.pk, member.pk] if selected_leaves else [master.pk]

    def resolve_then_detach_the_master(device):
        owner = resolve(device)
        # A concurrent chassis edit detaches the master and makes another member the master.
        Device.objects.filter(pk__in=leavers).update(virtual_chassis=None, vc_position=None)
        VirtualChassis.objects.filter(pk=chassis.pk).update(master=successor)
        return owner

    monkeypatch.setattr(actions, "mapping_owner", resolve_then_detach_the_master)
    client.force_login(make_superuser(f"owner-oob-left-{selected_leaves}"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_add_as_oob", args=[7133]),
        {"server_key": PRIMARY, "existing_device_id": member.pk},
        HTTP_HX_REQUEST="true",
    )

    assert escape(TRY_AGAIN_MESSAGE) in response.content.decode()
    assert mapping_summary(_fresh(master)) == {PRIMARY: {"own": 7132}}
    assert mapping_summary(_fresh(member)) == {}
    assert mapping_summary(_fresh(successor)) == {}


@pytest.mark.django_db
def test_an_oob_link_refuses_a_member_that_left_the_chassis_after_the_chassis_lock(client, servers, monkeypatch):
    from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE
    from netbox_librenms_plugin.views.imports import actions

    _vc, (master, member) = _chassis("owner-oob-member-left")
    seed_mapping(master, PRIMARY, own=7134)
    member.serial = "OWNER-OOB-MEMBER-LEFT"
    member.save()
    servers.primary.device_info_response(
        device_id=7135,
        hostname="owner-oob-member-left-controller",
        hardware="Test chassis",
        serial=member.serial,
        ip="",
    )
    servers.primary.vc_inventory_callable(7135, [], {})
    lock = actions.lock_owner_chassis

    def lock_then_move_the_member(model, pk):
        chassis_id = lock(model, pk)
        # The member leaves the chassis after its chassis is read and before the owner checks.
        Device.objects.filter(pk=member.pk).update(virtual_chassis=None, vc_position=None)
        return chassis_id

    monkeypatch.setattr(actions, "lock_owner_chassis", lock_then_move_the_member)
    client.force_login(make_superuser("owner-oob-member-left"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_add_as_oob", args=[7135]),
        {"server_key": PRIMARY, "existing_device_id": member.pk},
        HTTP_HX_REQUEST="true",
    )

    assert escape(TRY_AGAIN_MESSAGE) in response.content.decode()
    assert mapping_summary(_fresh(master)) == {PRIMARY: {"own": 7134}}
    assert mapping_summary(_fresh(member)) == {}


@transactional_db_with_all_apps()
def test_a_merge_waits_for_a_master_change_of_the_donor_chassis_and_merges_nothing(servers):
    """The merge share-locks the donor's chassis, so a promotion waits, and the merge then sees a misplaced mapping."""
    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.tests.test_serial_identity_concurrency import (
        _set_master,
        _write_during_a_chassis_change,
    )

    winner = seed_mapping(make_device("owner-merge-winner"), PRIMARY, own=7201)
    chassis, (donor, member) = _chassis("owner-merge")
    donor.serial = "OWNER-MERGE-SERIAL"
    donor.save()
    seed_mapping(donor, PRIMARY, own=7200)
    servers.primary.device_info_response(
        device_id=7202, hostname=winner.name, hardware=winner.device_type.model, os="ios", serial=donor.serial, ip=""
    )
    servers.primary.vc_inventory_callable(7202, [], {})
    url = reverse("plugins:netbox_librenms_plugin:device_merge_netbox_devices", kwargs={"device_id": 7202})

    status, sqlstates = _write_during_a_chassis_change(
        make_superuser("owner-merge-user"),
        url,
        {"server_key": PRIMARY, "winner_pk": str(winner.pk)},
        [_set_master(chassis, member)],
    )

    assert "40P01" not in sqlstates
    # The owner refusal of a plain request is a redirect with the misplaced-mapping message.
    assert status == 302
    assert mapping_summary(_fresh(winner)) == {PRIMARY: {"own": 7201}}
    assert mapping_summary(_fresh(donor)) == {PRIMARY: {"own": 7200}}
    assert mapping_summary(_fresh(member)) == {}


@pytest.mark.django_db
@pytest.mark.parametrize("view_name", ["update_device_platform", "create_and_assign_platform"])
def test_a_chassis_platform_write_is_stale_when_the_master_changes_during_the_lookup(
    client, servers, monkeypatch, view_name
):
    from dcim.models import VirtualChassis

    from netbox_librenms_plugin.tests.test_platform_version_tracking_views import _platform, _tracking
    from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView, UpdateDevicePlatformView

    _tracking(False)
    if view_name == "update_device_platform":
        # The update applies an existing OS platform; the create makes it.
        _platform("junos")
    chassis, (master, successor) = _chassis(f"owner-platform-{view_name}")
    seed_mapping(master, PRIMARY, own=7300)
    servers.primary.device_info_response(device_id=7300, hostname=master.name, os="junos", version="21.2R3")
    view = UpdateDevicePlatformView if view_name == "update_device_platform" else CreateAndAssignPlatformView
    read = view.get_live_device_info

    def read_then_change_the_master(self, librenms_id):
        answer = read(self, librenms_id)
        # A concurrent chassis edit makes another member the master after the facts were read.
        VirtualChassis.objects.filter(pk=chassis.pk).update(master=successor)
        return answer

    monkeypatch.setattr(view, "get_live_device_info", read_then_change_the_master)
    client.force_login(make_superuser(f"owner-platform-{view_name}"))
    data = {"server_key": PRIMARY}
    if view_name == "create_and_assign_platform":
        data |= {
            "platform_name": "junos",
            "librenms_os": "junos",
            "manufacturer": str(master.device_type.manufacturer_id),
        }

    response = client.post(reverse(f"plugins:netbox_librenms_plugin:{view_name}", kwargs={"pk": master.pk}), data)

    assert "The virtual chassis changed after LibreNMS was read." in " ".join(message_texts(response))
    assert [_fresh(device).platform for device in (master, successor)] == [None, None]


@pytest.mark.django_db
def test_a_legacy_conversion_refuses_a_master_that_lost_the_master_role_during_the_lookup(client, servers, monkeypatch):
    from dcim.models import VirtualChassis

    from netbox_librenms_plugin.server_mappings import ContainerStatus, read_mapping
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_legacy
    from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

    chassis, (master, successor) = _chassis("owner-convert-legacy")
    master.serial = "OWNER-CONVERT-SERIAL"
    master.save()
    seed_legacy(master, 7135)
    servers.primary.device_info_response(device_id=7135, hostname=master.name, serial=master.serial)
    lookup = ConvertLegacyLibreNMSIdView.get_live_device_info

    def look_up_then_move_the_master(view, librenms_id):
        answer = lookup(view, librenms_id)
        # A concurrent chassis edit commits after the owner check and before the write locks.
        VirtualChassis.objects.filter(pk=chassis.pk).update(master=successor)
        return answer

    monkeypatch.setattr(ConvertLegacyLibreNMSIdView, "get_live_device_info", look_up_then_move_the_master)
    client.force_login(make_superuser("owner-convert-legacy"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:convert_legacy_librenms_id", args=[master.pk]),
        {"object_type": "device", "server_key": PRIMARY},
    )

    assert response.status_code == 302
    assert _errors(response) == [MISPLACED_MESSAGE]
    assert read_mapping(_fresh(master)).container is ContainerStatus.LEGACY
    assert mapping_summary(_fresh(successor)) == {}


_WAITING_WRITERS = {
    "remove": ("remove_server_mapping", {"server_key": RETIRED, "object_type": "device"}),
    "preferred": ("set_preferred_server", {"server_key": SECONDARY, "object_type": "device"}),
    "edit": (
        "edit_server_mapping",
        {
            "object_type": "device",
            "server_key": SECONDARY,
            "own_id": "7182",
            "oob_id": "",
            "oob_type": "",
            "shown_own_id": "",
            "shown_oob_id": "",
            "shown_oob_type": "",
        },
    ),
}


@transactional_db_with_all_apps()
@pytest.mark.parametrize("writer", sorted(_WAITING_WRITERS))
def test_a_mapping_writer_that_waits_for_a_master_change_writes_nothing_on_the_old_master(servers, writer):
    from netbox_librenms_plugin.tests.test_serial_identity_concurrency import (
        _set_master,
        _write_during_a_chassis_change,
    )

    chassis, (master, successor) = _chassis(f"owner-wait-{writer}")
    seed_mapping(master, PRIMARY, own=7180, save=False)
    if writer != "edit":
        seed_mapping(master, SECONDARY, own=7181, save=False)
    seed_mapping(master, RETIRED, own=7183)
    before = mapping_summary(_fresh(master))
    view_name, data = _WAITING_WRITERS[writer]
    url = reverse(f"plugins:netbox_librenms_plugin:{view_name}", args=[master.pk])

    # The holder makes the successor the master; the old master then holds misplaced data.
    status, sqlstates = _write_during_a_chassis_change(
        make_superuser(f"owner-wait-{writer}"), url, data, [_set_master(chassis, successor)]
    )

    assert "40P01" not in sqlstates
    assert status == 302
    assert mapping_summary(_fresh(master)) == before
    assert mapping_summary(_fresh(successor)) == {}


@transactional_db_with_all_apps()
def test_a_confirmed_host_change_that_waits_for_a_master_change_writes_nothing_on_the_old_master(client, servers):
    from netbox_librenms_plugin.confirmation_intent import FIELD
    from netbox_librenms_plugin.tests.test_serial_identity_concurrency import (
        _set_master,
        _write_during_a_chassis_change,
    )

    chassis, (master, successor) = _chassis("owner-wait-lifecycle")
    seed_mapping(master, PRIMARY, own=7190)
    user = make_superuser("owner-wait-lifecycle")
    client.force_login(user)
    action, fields = _editor_forms(client, master, server_key=PRIMARY)[PRIMARY]
    confirmation = client.post(action, {**fields, "own_id": "7191"})
    assert confirmation.status_code == 200, message_texts(confirmation)
    context = confirmation.context

    status, sqlstates = _write_during_a_chassis_change(
        user,
        context["action_url"],
        {"server_key": context["server_key"], FIELD: [context["token"]]},
        [_set_master(chassis, successor)],
    )

    assert "40P01" not in sqlstates
    assert status == 302
    assert mapping_summary(_fresh(master)) == {PRIMARY: {"own": 7190}}
    assert mapping_summary(_fresh(successor)) == {}
