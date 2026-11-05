"""
A sync tab, a sync dialog or a banner never shows the pk or name of a device the actor may not view.

Rows are still attributed to every chassis member; only the disclosure goes through the view scope.
Each test renders the real page or endpoint as a constrained user and reads the HTML or JSON.
"""

import json
import re
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace

import pytest
from django.urls import reverse

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis
from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

pytestmark = pytest.mark.django_db


def _configure(settings, primary, secondary):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        key: {
            "display_name": f"{key.title()} LibreNMS",
            "librenms_url": server.url,
            "api_token": "scope-members-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        }
        for key, server in (("primary", primary), ("secondary", secondary))
    }
    plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def servers(settings, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with ExitStack() as stack:
        primary = stack.enter_context(librenms_mock_server())
        secondary = stack.enter_context(librenms_mock_server())
        _configure(settings, primary, secondary)
        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
        yield SimpleNamespace(primary=primary, secondary=secondary)


def _register(server, device_id, *, inventory=()):
    # A LibreNMS name is a LibreNMS fact: keep it apart from the NetBox names the tests look for.
    name = f"lnms-{device_id}"
    server.register(
        f"/api/v0/devices/{device_id}",
        {"status": "ok", "devices": [{"device_id": device_id, "sysName": name, "hostname": name, "serial": "-"}]},
    )
    for path in (f"/api/v0/inventory/{device_id}/all", f"/api/v0/inventory/{device_id}"):
        server.register(path, {"status": "ok", "inventory": list(inventory)})


def _sync_url(obj):
    name = "vm_librenms_sync" if obj._meta.model_name == "virtualmachine" else "device_librenms_sync"
    return reverse(f"plugins:netbox_librenms_plugin:{name}", args=[obj.pk])


def _chassis(tag):
    """Return a chassis of a visible page member and a hidden member."""
    page = make_device(f"{tag}-page", serial=f"{tag.upper()}-PAGE")
    hidden = make_device(f"{tag}-hidden", serial=f"{tag.upper()}-HIDDEN")
    make_virtual_chassis(f"{tag}-vc", page, hidden)
    return page, hidden


def _page_only_user(username, page, *, extra=()):
    """Return a plugin writer who may view (and change) only *page* among the devices."""
    from dcim.models import Device

    user = make_user_with_perms(username, [("view", Device), ("change", Device)], constraints={"pk": page.pk})
    for action, model, constraints in extra:
        user = grant(user, action, model, constraints=constraints)
    return user


def _stack(*serials):
    root = {"entPhysicalIndex": 1, "entPhysicalClass": "stack", "entPhysicalContainedIn": 0}
    rows = [
        {
            "entPhysicalIndex": 100 + offset,
            "entPhysicalClass": "chassis",
            "entPhysicalContainedIn": 1,
            "entPhysicalSerialNum": serial,
            "entPhysicalModelName": "Stack member",
        }
        for offset, serial in enumerate(serials)
    ]
    return [root, *rows]


def _between(html, start_marker, end_marker):
    start = html.index(start_marker)
    return html[start : html.index(end_marker, start)]


def _assert_hidden(html, device):
    assert device.name not in html
    assert device.get_absolute_url() not in html
    assert _sync_url(device) not in html


# =============================================================================
# Sync page: the VC serial dialog and its POST
# =============================================================================


def test_the_vc_serial_dialog_lists_only_visible_members_and_withholds_a_hidden_match(client, servers):
    page, hidden = _chassis("serial-dialog")
    seed_stored_mapping(page, {"primary": {"id": 14101}}, save=True)
    _register(servers.primary, 14101, inventory=_stack(page.serial, hidden.serial, "SERIAL-DIALOG-FREE"))
    client.force_login(_page_only_user("serial-dialog-user", page))

    response = client.get(_sync_url(page), {"server_key": "primary"})

    html = response.content.decode()
    assert response.status_code == 200
    _assert_hidden(html, hidden)
    form = _between(html, 'id="vc-serials-form"', "</form>")
    assert f'value="{hidden.pk}"' not in form
    assert set(re.findall(r'<option value="(\d+)"', form)) == {str(page.pk)}
    shown = {item["serial"]: item for item in response.context["vc_inventory_serials"]}
    assert shown[hidden.serial]["assigned_member"] is None
    assert shown[hidden.serial]["assigned_withheld"] is True
    assert shown["SERIAL-DIALOG-FREE"]["assigned_withheld"] is False
    assert shown[page.serial]["assigned_member"] == page
    assert response.context["vc_hidden_member_count"] == 1


@pytest.mark.parametrize(
    ("grants", "written"), [("view_and_change", True), ("change_only", False), ("outside_chassis", False)]
)
def test_a_serial_post_needs_view_change_and_current_membership(client, servers, grants, written):
    from dcim.models import Device

    page, member = _chassis(f"serial-post-{grants}")
    outsider = make_device(f"serial-post-{grants}-outsider", serial="OUTSIDER-OLD")
    target = outsider if grants == "outside_chassis" else member
    view_pks = {"view_and_change": [page.pk, member.pk], "change_only": [page.pk]}.get(grants, [page.pk, outsider.pk])
    user = make_user_with_perms(f"serial-post-{grants}-user", [])
    user = grant(user, "view", Device, constraints={"pk__in": view_pks})
    user = grant(user, "change", Device, constraints={"pk__in": [page.pk, member.pk, outsider.pk]})
    client.force_login(user)
    before = target.serial

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:assign_vc_serial", args=[page.pk]),
        {"librenms_serial_1": "SERIAL-POST-NEW", "member_id_1": str(target.pk), "server_key": "primary"},
        follow=True,
    )

    target.refresh_from_db()
    assert target.serial == ("SERIAL-POST-NEW" if written else before)
    if grants == "change_only":
        assert member.name not in response.content.decode()


# =============================================================================
# Sync page: the "managed by" banner and the mapping owner
# =============================================================================


def test_the_managed_by_banner_does_not_name_a_hidden_sync_member(client, servers):
    page, hidden = _chassis("managed-by")
    seed_stored_mapping(hidden, {"primary": {"id": 14111}}, save=True)
    _register(servers.primary, 14111)
    client.force_login(_page_only_user("managed-by-user", page))

    response = client.get(_sync_url(page), {"server_key": "primary"})

    html = response.content.decode()
    assert response.status_code == 200
    assert response.context["is_vc_member"] is True
    assert response.context["librenms_sync_device"] is None
    _assert_hidden(html, hidden)
    assert f"/{hidden.pk}/" not in html


def test_the_preference_forms_exist_only_for_a_visible_mapping_owner(client, servers):
    """The hidden owner holds two usable mappings: a change grant alone must not render its forms."""
    page, hidden = _chassis("preference-owner")
    seed_stored_mapping(hidden, {"primary": {"id": 14121}, "secondary": {"id": 14122}}, save=True)
    _register(servers.primary, 14121)
    client.force_login(_page_only_user("preference-owner-user", page))

    response = client.get(_sync_url(page), {"server_key": "primary"})

    html = response.content.decode()
    assert response.status_code == 200
    assert response.context["lookup_device_pk"] is None
    assert response.context["can_manage_server_preference"] is False
    preference_url = reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[hidden.pk])
    assert preference_url not in html
    _assert_hidden(html, hidden)


def test_a_vm_with_the_hidden_owner_pk_does_not_disclose_the_owner(client, servers):
    """A Device pk never resolves a VirtualMachine: a visible VM with the same pk does not make the owner visible."""
    from virtualization.models import Cluster, ClusterType, VirtualMachine

    page, hidden = _chassis("same-pk-owner")
    seed_stored_mapping(hidden, {"primary": {"id": 14131}, "secondary": {"id": 14132}}, save=True)
    _register(servers.primary, 14131)
    cluster_type, _ = ClusterType.objects.get_or_create(name="Scope CType", slug="scope-ctype")
    cluster, _ = Cluster.objects.get_or_create(name="Scope Cluster", defaults={"type": cluster_type})
    VirtualMachine.objects.create(pk=hidden.pk, name="same-pk-owner-vm", cluster=cluster, status="active")
    client.force_login(
        _page_only_user(
            "same-pk-owner-user",
            page,
            extra=[("view", VirtualMachine, None), ("change", VirtualMachine, None)],
        )
    )

    response = client.get(_sync_url(page), {"server_key": "primary"})

    html = response.content.decode()
    assert response.status_code == 200
    assert response.context["lookup_device_pk"] is None
    assert reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[hidden.pk]) not in html


def test_a_selection_required_page_does_not_link_a_hidden_mapping_owner(client, settings, servers):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"]["incomplete"] = {
        "display_name": "Incomplete LibreNMS",
        "librenms_url": servers.primary.url,
        "api_token": "",
        "verify_ssl": False,
    }
    settings.PLUGINS_CONFIG = plugin_config
    page, hidden = _chassis("blocked-owner")
    seed_stored_mapping(hidden, {"incomplete": {"id": 14141}}, save=True)
    client.force_login(_page_only_user("blocked-owner-user", page))

    response = client.get(_sync_url(page))

    html = response.content.decode()
    assert response.status_code == 200
    assert response.context["server_selection_state"] == "selection_required"
    assert response.context["lookup_device_pk"] is None
    assert 'id="librenms-server-selection-recovery"' in html
    _assert_hidden(html, hidden)


def test_an_invalid_server_page_offers_no_form_of_a_hidden_mapping_owner(client, servers):
    page, hidden = _chassis("invalid-owner")
    seed_stored_mapping(
        hidden, {"primary": {"id": 14151}, "secondary": {"id": 14152}, "retired": {"id": 14153}}, save=True
    )
    client.force_login(_page_only_user("invalid-owner-user", page))

    response = client.get(_sync_url(page), {"server_key": "missing"})

    html = response.content.decode()
    assert response.status_code == 200
    assert response.context["server_selection_state"] == "invalid"
    assert response.context["lookup_device_pk"] is None
    assert response.context["can_manage_server_preference"] is False
    assert reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[hidden.pk]) not in html
    assert reverse("plugins:netbox_librenms_plugin:remove_server_mapping", args=[hidden.pk]) not in html
    _assert_hidden(html, hidden)


def test_preference_management_needs_view_and_change_on_the_owner(client, servers):
    """A view grant alone, or a change grant alone, does not render the star forms."""
    from dcim.models import Device

    owner = make_device("preference-view-change")
    seed_stored_mapping(owner, {"primary": {"id": 14161}, "secondary": {"id": 14162}}, save=True)
    _register(servers.primary, 14161)
    user = make_user_with_perms("preference-view-only", [("view", Device)], constraints={"pk": owner.pk})
    client.force_login(user)

    view_only = client.get(_sync_url(owner), {"server_key": "primary"})

    assert view_only.context["can_manage_server_preference"] is False
    user = grant(user, "change", Device, constraints={"pk": owner.pk})
    client.force_login(user)
    view_and_change = client.get(_sync_url(owner), {"server_key": "primary"})
    assert view_and_change.context["can_manage_server_preference"] is True


@pytest.mark.parametrize("object_type", ["device", "virtualmachine"])
def test_a_preference_post_needs_view_and_change_on_the_owner(client, servers, object_type):
    """A forged preference POST for an owner that the user may change but not view writes nothing."""
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    from netbox_librenms_plugin.server_mappings import PREFERRED_SERVER_FIELD
    from netbox_librenms_plugin.tests.conftest import make_vm
    from netbox_librenms_plugin.tests.mapping_fixtures import stored_mapping_for_test

    model = VirtualMachine if object_type == "virtualmachine" else Device
    owner = make_vm("preference-hidden-vm") if model is VirtualMachine else make_device("preference-hidden-device")
    seed_stored_mapping(owner, {"primary": {"id": 14191}, "secondary": {"id": 14192}}, save=True)
    user = make_user_with_perms(f"preference-hidden-{object_type}", [("change", model)])
    client.force_login(grant(user, "view", model, constraints={"pk": owner.pk + 100000}))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[owner.pk]),
        {"object_type": object_type, "server_key": "secondary", "active_server_key": "primary"},
    )

    owner.refresh_from_db()
    assert PREFERRED_SERVER_FIELD not in stored_mapping_for_test(owner)
    assert response.status_code == 404


@pytest.mark.parametrize("object_type", ["device", "virtualmachine"])
def test_a_remove_mapping_post_needs_view_and_change_on_the_owner(client, servers, object_type):
    """A forged remove-mapping POST for an owner that the user may change but not view removes nothing."""
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    from netbox_librenms_plugin.tests.conftest import make_vm
    from netbox_librenms_plugin.tests.mapping_fixtures import stored_mapping_for_test

    model = VirtualMachine if object_type == "virtualmachine" else Device
    owner = make_vm("remove-hidden-vm") if model is VirtualMachine else make_device("remove-hidden-device")
    seed_stored_mapping(owner, {"primary": {"id": 14195}, "retired": {"id": 14196}}, save=True)
    user = make_user_with_perms(f"remove-hidden-{object_type}", [("change", model)])
    client.force_login(grant(user, "view", model, constraints={"pk": owner.pk + 100000}))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:remove_server_mapping", args=[owner.pk]),
        {"object_type": object_type, "server_key": "retired", "active_server_key": "primary"},
    )

    owner.refresh_from_db()
    assert "retired" in stored_mapping_for_test(owner)
    assert response.status_code == 404


# =============================================================================
# Sync page: the migration winner
# =============================================================================


def _mark(donor, winner_pk, server_key="primary"):
    from netbox_librenms_plugin.server_mappings import mark_migrated
    from netbox_librenms_plugin.tests.mapping_fixtures import apply_mapping_change

    apply_mapping_change(donor, mark_migrated(donor, winner_pk, server_key, at="2026-11-01T00:00:00+00:00"))
    donor.save(update_fields=["custom_field_data"])


def _migrated_banner(html):
    return _between(html, "This device was merged into another NetBox device.", "</div>\n</div>")


def test_a_hidden_and_a_missing_winner_render_the_same_banner(client, servers):
    from dcim.models import Device

    hidden_winner = make_device("banner-hidwinner")
    hidden_donor = make_device("banner-donor-a", librenms_cf={"primary": {"id": 14171}})
    missing_donor = make_device("banner-donor-b", librenms_cf={"primary": {"id": 14172}})
    _mark(hidden_donor, hidden_winner.pk)
    _mark(missing_donor, hidden_winner.pk + 100000)
    user = make_user_with_perms(
        "winner-banner-user", [("view", Device)], constraints={"pk__in": [hidden_donor.pk, missing_donor.pk]}
    )
    client.force_login(user)

    hidden_html = client.get(_sync_url(hidden_donor), {"server_key": "primary"}).content.decode()
    missing_html = client.get(_sync_url(missing_donor), {"server_key": "primary"}).content.decode()

    _assert_hidden(hidden_html, hidden_winner)
    assert f"id={hidden_winner.pk}" not in hidden_html
    assert f"id={hidden_winner.pk + 100000}" not in missing_html
    assert _migrated_banner(hidden_html) == _migrated_banner(missing_html)


def test_the_htmx_ip_card_does_not_name_a_hidden_winner(client, servers):
    from dcim.models import Device, Interface
    from ipam.models import IPAddress

    from netbox_librenms_plugin.tests.conftest import make_interface, make_ip

    winner = make_device("ip-card-hidwinner")
    donor = make_device("ip-card-donor", librenms_cf={"default": {"id": 14181}})
    make_ip("198.18.141.10/24", assigned_object=make_interface(donor, "Ethernet1", iface_type="1000base-t"))
    # The refresh names a removed server, so the card reads the marker of the unbound default key.
    _mark(donor, winner.pk, "default")
    user = make_user_with_perms("ip-card-user", [("view", Device)], constraints={"pk": donor.pk})
    for model in (Interface, IPAddress):
        user = grant(user, "view", model)
    client.force_login(user)

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[donor.pk]),
        {"server_key": "removed"},
        HTTP_HX_REQUEST="true",
    )

    html = response.content.decode()
    assert response.status_code == 200
    assert 'id="librenms-ipaddress-migrate-table"' in html
    _assert_hidden(html, winner)


# =============================================================================
# Interfaces tab: rows owned by a hidden member, the member selects and the Bulk VC Member dialog
# =============================================================================


def _interface_port(port_id, name, *, if_type="ethernetCsmacd"):
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifType": if_type,
        "ifAdminStatus": "up",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifPhysAddress": "",
        "ifAlias": "",
        "untagged_vlan": 10,
        "tagged_vlans": [],
    }


def _interface_tab(client, device):
    from html import unescape

    response = client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": device.pk, "tab": "interfaces"},
        ),
        {"server_key": "default", "interface_name_field": "ifName"},
    )
    assert response.status_code == 200
    return unescape(response.content.decode())


def _table_rows(html, attribute):
    return {
        int(match.group(1)): match.group(0)
        for match in re.finditer(rf'<tr[^>]*{attribute}="([^"]+)"[^>]*>.*?</tr>', html, flags=re.S)
        if match.group(1).isdigit()
    }


@pytest.fixture
def default_server(settings):
    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server

    return configure_default_librenms_server(settings)


def test_an_interface_row_on_a_hidden_member_names_no_member_and_offers_no_action(client, default_server):
    from dcim.models import Interface

    from netbox_librenms_plugin.tests.conftest import make_interface
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_interfaces_snapshot

    page, hidden = _chassis("iface-scope")
    seed_stored_mapping(page, {"default": {"id": 14201}}, save=True)
    bound = make_interface(hidden, "Ethernet9", iface_type="1000base-t")
    seed_mapping(bound, "default", own=702, save=True)
    # This interface holds the reported name of row 703 through a port that LibreNMS no longer reports.
    holder = make_interface(hidden, "Gi2/0/3", iface_type="1000base-t")
    seed_mapping(holder, "default", own=799, save=True)
    seed_interfaces_snapshot(
        page,
        [
            _interface_port(700, "Gi1/0/1"),
            # The name puts this row on member 2, the binding puts the next one there.
            _interface_port(701, "Gi2/0/1"),
            _interface_port(702, "Ethernet9"),
            _interface_port(703, "Gi2/0/3"),
        ],
        server_key="default",
    )
    client.force_login(_page_only_user("iface-scope-user", page, extra=[("view", Interface, None)]))

    html = _interface_tab(client, page)

    _assert_hidden(html, hidden)
    for interface in (bound, holder):
        assert interface.get_absolute_url() not in html
    assert "799" not in html
    assert f'data-device-id="{hidden.pk}"' not in html
    for select in re.findall(
        r"<select[^>]*vc-member-select.*?</select>|<select[^>]*bulk-vc-member.*?</select>", html, re.S
    ):
        assert f'value="{hidden.pk}"' not in select
    (bulk,) = re.findall(r'<select id="bulk-vc-member-select".*?</select>', html, re.S)
    assert re.findall(r'<option value="(\d+)"', bulk) == [str(page.pk)]
    rows = _table_rows(html, "data-port-id")
    assert f'value="{page.pk}" selected' in rows[700]
    for port_id in (701, 702, 703):
        row = rows[port_id]
        assert '<option value="" selected>' in row
        assert f'value="{page.pk}" selected' not in row
        assert 'name="sync_one"' not in row
        assert "data-device-id" not in row
        (checkbox,) = re.findall(r'<input[^>]*name="select"[^>]*>', row)
        assert "disabled" in checkbox
    blocked = json.loads(re.search(r'data-blocked-port-ids="(\[[^\]]*\])"', html).group(1))
    assert {"701", "702", "703"} <= set(blocked)
    assert "700" not in blocked


def test_the_interface_verify_of_a_hidden_member_row_returns_no_hidden_identity(client, default_server):
    from dcim.models import Interface

    from netbox_librenms_plugin.tests.conftest import make_interface
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_interfaces_snapshot

    page, hidden = _chassis("iface-verify-scope")
    seed_stored_mapping(page, {"default": {"id": 14211}}, save=True)
    bound = make_interface(hidden, "Ethernet9", iface_type="1000base-t")
    seed_mapping(bound, "default", own=712, save=True)
    seed_interfaces_snapshot(page, [_interface_port(712, "Ethernet9")], server_key="default")
    client.force_login(_page_only_user("iface-verify-scope-user", page, extra=[("view", Interface, None)]))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_interface"),
        data=json.dumps({"device_id": page.pk, "origin_device_id": page.pk, "port_id": 712, "server_key": "default"}),
        content_type="application/json",
    )

    body = json.dumps(response.json())
    assert response.status_code == 200
    assert hidden.name not in body
    assert hidden.get_absolute_url() not in body
    assert bound.get_absolute_url() not in body
    assert f'\\"{hidden.pk}\\"' not in body


def _verify_port(client, device, port_id):
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_interface"),
        data=json.dumps(
            {"device_id": device.pk, "origin_device_id": device.pk, "port_id": port_id, "server_key": "default"}
        ),
        content_type="application/json",
    )
    assert response.status_code == 200
    return json.dumps(response.json(), sort_keys=True)


def test_an_interface_the_user_may_change_but_not_view_is_absent_from_the_tab_and_the_verify(client, default_server):
    """A change grant does not show an interface: the row and the verify read as if it did not exist."""
    from dcim.models import Device, Interface

    from netbox_librenms_plugin.tests.conftest import make_interface
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_interfaces_snapshot

    owner = make_device("iface-changeonly-owner")
    seed_stored_mapping(owner, {"default": {"id": 14221}}, save=True)
    hidden = make_interface(owner, "HiddenEth5", iface_type="100gbase-x-qsfp28")
    seed_mapping(hidden, "default", own=905, save=True)
    seed_interfaces_snapshot(owner, [_interface_port(905, "Ethernet5")], server_key="default")
    user = make_user_with_perms("iface-changeonly-user", [("view", Device), ("change", Device), ("change", Interface)])
    client.force_login(grant(user, "view", Interface, constraints={"name": "VisibleOnly"}))
    hidden_marks = (hidden.name, hidden.get_absolute_url(), hidden.type, f'"{hidden.pk}"')

    hidden_html = _interface_tab(client, owner)
    hidden_verify = _verify_port(client, owner, 905)
    hidden.delete()
    absent_html = _interface_tab(client, owner)
    absent_verify = _verify_port(client, owner, 905)

    for mark in hidden_marks:
        assert mark not in hidden_html
        assert mark not in hidden_verify
    assert hidden_verify == absent_verify
    assert _table_rows(hidden_html, "data-port-id")[905] == _table_rows(absent_html, "data-port-id")[905]


def test_a_visible_cross_member_lag_reads_the_same_in_the_table_and_the_verify(client, default_server):
    """Interface A on member 1 is in LAG B on member 2, and LibreNMS reports no LAG: both show the NetBox-only LAG."""
    from netbox_librenms_plugin.tests.conftest import make_interface, make_superuser
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_interfaces_snapshot

    page, other = _chassis("rel-cross-member")
    seed_stored_mapping(page, {"default": {"id": 14271}}, save=True)
    member = make_interface(page, "Gi1/0/1", iface_type="1000base-t")
    seed_mapping(member, "default", own=901, save=True)
    member.lag = make_interface(other, "Po2", iface_type="lag")
    member.save()
    seed_interfaces_snapshot(page, [_interface_port(901, "Gi1/0/1")], server_key="default")
    client.force_login(make_superuser("rel-cross-member-user"))

    row = _table_rows(_interface_tab(client, page), "data-port-id")[901]
    table_cell = re.search(r'<td data-col="parent">(.*?)</td>', row, re.S).group(1)
    verify_cell = json.loads(_verify_port(client, page, 901))["formatted_row"]["parent"]

    assert table_cell
    assert verify_cell == table_cell


@pytest.mark.parametrize("relation", ["lag", "parent", "bridge"])
def test_a_hidden_relationship_target_renders_the_same_as_no_target(client, default_server, relation):
    """A visible interface whose LAG, parent or bridge the user cannot view: table and verify read as no target."""
    from dcim.models import Device, Interface

    from netbox_librenms_plugin.tests.conftest import make_interface
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_interfaces_snapshot

    owner = make_device(f"rel-hidden-{relation}")
    seed_stored_mapping(owner, {"default": {"id": 14251}}, save=True)
    visible = make_interface(owner, "eth1", iface_type="1000base-t")
    seed_mapping(visible, "default", own=801, save=True)
    target = make_interface(owner, f"private-{relation}", iface_type="lag" if relation == "lag" else "1000base-t")
    seed_interfaces_snapshot(owner, [_interface_port(801, "eth1")], server_key="default")
    user = make_user_with_perms(f"rel-hidden-{relation}-user", [("view", Device)])
    client.force_login(grant(user, "view", Interface, constraints={"name__startswith": "eth"}))

    setattr(visible, relation, target)
    visible.save()
    hidden_row = _table_rows(_interface_tab(client, owner), "data-port-id")[801]
    hidden_verify = _verify_port(client, owner, 801)
    setattr(visible, relation, None)
    visible.save()
    absent_row = _table_rows(_interface_tab(client, owner), "data-port-id")[801]
    absent_verify = _verify_port(client, owner, 801)

    assert hidden_row == absent_row
    assert hidden_verify == absent_verify


# =============================================================================
# Cables tab: local and remote ends on hidden members
# =============================================================================


def _cable_tab(client, device):
    from html import unescape

    response = client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": device.pk, "tab": "cables"},
        ),
        {"server_key": "default"},
    )
    assert response.status_code == 200
    return unescape(response.content.decode())


def test_a_cable_row_on_a_hidden_local_or_remote_member_names_no_member(client, default_server):
    from dcim.models import Device, Interface

    from netbox_librenms_plugin.tests.conftest import make_interface
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
    from netbox_librenms_plugin.tests.source_snapshot_helpers import link_row, port_row, seed_cables_snapshot

    page, hidden = _chassis("cable-scope")
    seed_stored_mapping(page, {"default": {"id": 14221}}, save=True)
    page_port = make_interface(page, "Ethernet1", iface_type="1000base-t")
    hidden_port = make_interface(hidden, "Ethernet2", iface_type="1000base-t")
    seed_mapping(hidden_port, "default", own=722, save=True)
    remote_visible = make_device("cable-scope-remote-a")
    remote_hidden = make_device("cable-scope-remote-b")
    make_virtual_chassis("cable-scope-remote-vc", remote_visible, remote_hidden)
    remote_port = make_interface(remote_hidden, "ge-2/0/5", iface_type="1000base-t")
    seed_cables_snapshot(
        page,
        [
            link_row(721, "ge-2/0/5", local_port=page_port.name, remote_hostname=remote_visible.name, id=None),
            link_row(722, "Ethernet7", local_port=hidden_port.name, remote_hostname="unknown-neighbour", id=None),
        ],
        [
            port_row(721, page_port.name, device_id=14221, if_descr=None),
            port_row(722, hidden_port.name, device_id=14221, if_descr=None),
        ],
        server_key="default",
    )
    user = _page_only_user(
        "cable-scope-user",
        page,
        extra=[("view", Interface, None), ("view", Device, {"pk": remote_visible.pk})],
    )
    client.force_login(user)

    html = _cable_tab(client, page)

    for device in (hidden, remote_hidden):
        _assert_hidden(html, device)
    for port in (hidden_port, remote_port):
        assert port.get_absolute_url() not in html
    selects = re.findall(r'<select name="device_selection_[^"]*".*?</select>', html, re.S)
    assert len(selects) == 2
    assert all(f'value="{hidden.pk}"' not in select for select in selects)
    (withheld,) = [select for select in selects if "disabled" in select]
    assert '<option value="" selected>' in withheld
    assert 'name="sync_one"' not in html


def _remote_bound_on_a_hidden_member(tag):
    """Return a row whose remote port is bound on hidden member B while its name points at visible member A."""
    from dcim.models import Cable, Device, Interface

    from netbox_librenms_plugin.tests.conftest import make_interface
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
    from netbox_librenms_plugin.tests.source_snapshot_helpers import link_row, port_row, seed_cables_snapshot

    page = make_device(f"{tag}-page")
    seed_stored_mapping(page, {"default": {"id": 14231}}, save=True)
    local = make_interface(page, "Ethernet1", iface_type="1000base-t")
    remote_a = make_device(f"{tag}-remote-a")
    remote_b = make_device(f"{tag}-remote-b")
    make_virtual_chassis(f"{tag}-remote-vc", remote_a, remote_b)
    # The name ge-1/0/5 selects member 1 (A); the stable binding of port 9731 is on B.
    namesake = make_interface(remote_a, "ge-1/0/5", iface_type="1000base-t")
    bound = make_interface(remote_b, "Bound-B", iface_type="1000base-t")
    seed_mapping(bound, "default", own=9731, save=True)
    seed_cables_snapshot(
        page,
        [link_row(731, "ge-1/0/5", local_port=local.name, remote_hostname=remote_a.name, remote_port_id=9731)],
        [port_row(731, local.name, device_id=14231, if_descr=None)],
        server_key="default",
    )
    visible = {"pk__in": [page.pk, remote_a.pk]}
    user = make_user_with_perms(f"{tag}-user", [("view", Device), ("change", Device)], constraints=visible)
    for action, model in (
        ("view", Interface),
        ("change", Interface),
        ("view", Cable),
        ("add", Cable),
        ("change", Cable),
    ):
        user = grant(user, action, model)
    return page, local, remote_a, namesake, user


def test_a_remote_port_bound_on_a_hidden_member_never_resolves_to_a_visible_namesake(client, default_server):
    page, _local, _remote_a, namesake, user = _remote_bound_on_a_hidden_member("cable-remote-bound")
    client.force_login(user)

    html = _cable_tab(client, page)

    assert namesake.get_absolute_url() not in html
    assert 'name="sync_one"' not in html


def test_the_cable_verify_never_resolves_a_hidden_bound_port_to_a_visible_namesake(client, default_server):
    page, _local, remote_a, namesake, user = _remote_bound_on_a_hidden_member("cable-verify-bound")
    client.force_login(user)
    _cable_tab(client, page)

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_cable"),
        data=json.dumps({"device_id": page.pk, "row_id": "731", "server_key": "default"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    row = response.json()["formatted_row"]
    assert row["expected_remote_id"] is None
    assert row["can_create_cable"] is False
    assert namesake.get_absolute_url() not in json.dumps(row)


def test_a_forged_cable_post_cannot_cable_the_visible_namesake_of_a_hidden_bound_port(client, default_server):
    from dcim.models import Cable

    page, local, remote_a, namesake, user = _remote_bound_on_a_hidden_member("cable-remote-forged")
    client.force_login(user)
    _cable_tab(client, page)

    client.post(
        reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[page.pk]),
        {
            "sync_one": "731",
            "server_key": "default",
            "expected_local_id_731": local.pk,
            "expected_local_device_id_731": page.pk,
            "expected_remote_id_731": namesake.pk,
            "expected_remote_device_id_731": remote_a.pk,
        },
        HTTP_HX_REQUEST="true",
    )

    namesake.refresh_from_db()
    assert namesake.cable_id is None
    assert not Cable.objects.exists()


def _local_bound_on_a_hidden_member(tag):
    """Return a row whose local port is bound on hidden member B while its name points at the visible page member."""
    from dcim.models import Cable, Device, Interface

    from netbox_librenms_plugin.tests.conftest import make_interface
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
    from netbox_librenms_plugin.tests.source_snapshot_helpers import link_row, port_row, seed_cables_snapshot

    page, hidden = _chassis(tag)
    seed_stored_mapping(page, {"default": {"id": 14261}}, save=True)
    # The name Gi1/0/5 selects member 1 (the page); the stable binding of port 751 is on B.
    namesake = make_interface(page, "Gi1/0/5", iface_type="1000base-t")
    bound = make_interface(hidden, "Gi2/0/9", iface_type="1000base-t")
    seed_mapping(bound, "default", own=751, save=True)
    remote = make_device(f"{tag}-remote")
    remote_port = make_interface(remote, "Ethernet9", iface_type="1000base-t")
    seed_cables_snapshot(
        page,
        [link_row(751, remote_port.name, local_port="Gi1/0/5", remote_hostname=remote.name)],
        [port_row(751, "Gi1/0/5", device_id=14261, if_descr=None)],
        server_key="default",
    )
    user = make_user_with_perms(
        f"{tag}-user", [("view", Device), ("change", Device)], constraints={"pk__in": [page.pk, remote.pk]}
    )
    for action, model in (
        ("view", Interface),
        ("change", Interface),
        ("view", Cable),
        ("add", Cable),
        ("change", Cable),
    ):
        user = grant(user, action, model)
    return page, namesake, remote, remote_port, user


def test_the_cable_verify_never_moves_a_hidden_bound_local_port_to_a_visible_namesake(client, default_server):
    page, namesake, _remote, _remote_port, user = _local_bound_on_a_hidden_member("cable-local-verify")
    client.force_login(user)
    _cable_tab(client, page)

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_cable"),
        data=json.dumps({"device_id": page.pk, "row_id": "751", "server_key": "default"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    row = response.json()["formatted_row"]
    assert row["expected_local_id"] is None
    assert row["can_create_cable"] is False
    assert namesake.get_absolute_url() not in json.dumps(row)


def test_a_forged_member_selection_cannot_cable_the_namesake_of_a_hidden_bound_local_port(client, default_server):
    from dcim.models import Cable

    page, namesake, remote, remote_port, user = _local_bound_on_a_hidden_member("cable-local-forged")
    client.force_login(user)
    _cable_tab(client, page)

    client.post(
        reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[page.pk]),
        {
            "sync_one": "751",
            "server_key": "default",
            "device_selection_751": page.pk,
            "expected_local_id_751": namesake.pk,
            "expected_local_device_id_751": page.pk,
            "expected_remote_id_751": remote_port.pk,
            "expected_remote_device_id_751": remote.pk,
        },
        HTTP_HX_REQUEST="true",
    )

    namesake.refresh_from_db()
    assert namesake.cable_id is None
    assert not Cable.objects.exists()


def test_a_forged_cable_post_cannot_cable_a_local_interface_that_the_user_cannot_view(client, default_server):
    """The user may change the bound local interface but not view it: no cable reaches it."""
    from dcim.models import Cable, Device, Interface

    from netbox_librenms_plugin.tests.conftest import make_interface
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
    from netbox_librenms_plugin.tests.source_snapshot_helpers import link_row, port_row, seed_cables_snapshot

    page = make_device("cable-hidden-local-page")
    seed_stored_mapping(page, {"default": {"id": 14241}}, save=True)
    hidden = make_interface(page, "private-uplink", iface_type="1000base-t")
    seed_mapping(hidden, "default", own=741, save=True)
    remote = make_device("cable-hidden-local-remote")
    remote_port = make_interface(remote, "Ethernet9", iface_type="1000base-t")
    seed_cables_snapshot(
        page,
        [link_row(741, remote_port.name, local_port="uplink", remote_hostname=remote.name)],
        [port_row(741, "uplink", device_id=14241, if_descr=None)],
        server_key="default",
    )
    user = make_user_with_perms("cable-hidden-local-user", [("view", Device), ("change", Device)])
    for action, model, constraints in (
        ("view", Interface, {"name__startswith": "Eth"}),
        ("change", Interface, None),
        ("view", Cable, None),
        ("add", Cable, None),
        ("change", Cable, None),
    ):
        user = grant(user, action, model, constraints=constraints)
    client.force_login(user)

    html = _cable_tab(client, page)
    client.post(
        reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[page.pk]),
        {
            "sync_one": "741",
            "server_key": "default",
            "expected_local_id_741": hidden.pk,
            "expected_local_device_id_741": page.pk,
            "expected_remote_id_741": remote_port.pk,
            "expected_remote_device_id_741": remote.pk,
        },
        HTTP_HX_REQUEST="true",
    )

    assert hidden.get_absolute_url() not in html
    hidden.refresh_from_db()
    assert hidden.cable_id is None
    assert not Cable.objects.exists()


@pytest.mark.parametrize("view_interfaces", [True, False], ids=["hidden_member", "hidden_interface"])
def test_the_interface_delete_refuses_an_interface_the_user_cannot_view(client, view_interfaces):
    from dcim.models import Interface

    from netbox_librenms_plugin.tests.conftest import make_interface

    page, hidden = _chassis(f"delete-scope-{view_interfaces}")
    target = make_interface(hidden if view_interfaces else page, "Gi9/0/1", iface_type="1000base-t")
    # The delete grant covers every interface; only the view scope hides the target.
    extra = [("delete", Interface, None)]
    if view_interfaces:
        extra.append(("view", Interface, None))
    else:
        extra.append(("view", Interface, {"pk__in": [0]}))
    client.force_login(_page_only_user(f"delete-scope-user-{view_interfaces}", page, extra=extra))

    response = client.post(
        reverse(
            "plugins:netbox_librenms_plugin:delete_netbox_interfaces",
            kwargs={"object_type": "device", "object_id": page.pk},
        ),
        {"interface_ids": [str(target.pk)]},
    )

    assert Interface.objects.filter(pk=target.pk).exists()
    body = response.content.decode()
    assert target.name not in body
    assert hidden.name not in body


def test_a_cable_row_cannot_be_moved_onto_a_member_the_user_cannot_view():
    from types import SimpleNamespace

    from netbox_librenms_plugin.views.sync.cables import SyncCablesView

    page, hidden = _chassis("cable-target-scope")
    sync = object.__new__(SyncCablesView)
    sync.request = SimpleNamespace(user=_page_only_user("cable-target-scope-user", page))
    sync._initial_device = page

    assert sync._selected_device_is_in_page_context(page.pk)
    assert not sync._selected_device_is_in_page_context(hidden.pk)
