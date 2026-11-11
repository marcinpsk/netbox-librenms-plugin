"""
Unlink, relink and change_oob through the real routes, with the interface cascade.

The unlink confirmation or the mapping editor offers each token, the lifecycle route applies it, and
each test reads the real database. The scope is a Device, every member of a virtual chassis, or a VM.
"""

from threading import Event

import pytest
from dcim.models import Device, Interface
from django.core import signing
from django.db import connection
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin.tests.conftest import (
    bounded_executor,
    configure_librenms_servers,
    make_cluster,
    make_device,
    make_interface,
    make_virtual_chassis_members,
    make_vm,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.link_intent_helpers import (
    edit_mapping,
    give_primary_ip,
    lifecycle_url,
    message_texts,
    offered_token,
    page_link,
    register_match,
    stale_paths,
    sync_page,
    unlink_page,
)
from netbox_librenms_plugin.tests.lock_conflict_helpers import backend_pid, second_connection, wait_for_lock_wait
from netbox_librenms_plugin.tests.mapping_fixtures import (
    mapped_on_server_constraint,
    mapping_summary,
    seed_legacy,
    seed_mapping,
    seed_mapping_row,
    seed_quarantined,
)
from netbox_librenms_plugin.tests.view_test_helpers import domain_state, grant, make_user_with_perms

PERMISSIONS = [
    (action, model) for model in (Device, VirtualMachine, Interface, VMInterface) for action in ("view", "change")
]
SCOPES = pytest.mark.parametrize("kind", ["device", "chassis", "vm"])


@pytest.fixture
def server(settings, librenms_server):
    configure_librenms_servers(
        settings,
        {
            key: {"display_name": f"{key.title()} LibreNMS", "librenms_url": librenms_server.url, "api_token": "t"}
            for key in ("default", "other")
        },
    )
    return librenms_server


@pytest.fixture
def writer(client):
    client.force_login(make_user_with_perms("unlink-writer", PERMISSIONS))
    return client


def _scope(kind, tag):
    """Return the object that holds the mapping, and the objects whose interfaces are its scope."""
    if kind == "device":
        device = make_device(tag)
        return device, [device]
    if kind == "chassis":
        _chassis, members = make_virtual_chassis_members(tag)
        return members[0], members
    vm = make_vm(tag, make_cluster(f"{tag}-cluster"))
    return vm, [vm]


def _interface(parent, name, **ports):
    """Create an interface on *parent* with the port ID of each server in *ports*."""
    if isinstance(parent, VirtualMachine):
        interface = VMInterface.objects.create(virtual_machine=parent, name=name)
    else:
        interface = make_interface(parent, name)
    for server_key, port_id in ports.items():
        seed_mapping(interface, server_key, own=port_id)
    return interface


def _linked(kind, tag):
    """Return a linked object with ports on two servers in its scope, and its interfaces by name."""
    owner, parents = _scope(kind, tag)
    seed_mapping(owner, "default", own=4100, save=False)
    seed_mapping(owner, "default", oob=4101, oob_type="idrac", save=False)
    seed_mapping(owner, "other", own=4102, save=False)
    seed_mapping(owner, "default", preferred=True)
    interfaces = {
        "both": _interface(parents[0], "eth0", default=5100, other=5200),
        "default": _interface(parents[-1], "eth1", default=5101),
        "none": _interface(parents[0], "eth2"),
    }
    return owner, interfaces


def _fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


def _change_records(obj):
    from core.models import ObjectChange
    from django.contrib.contenttypes.models import ContentType

    return ObjectChange.objects.filter(
        changed_object_type=ContentType.objects.get_for_model(type(obj)), changed_object_id=obj.pk
    ).count()


def _confirm(client, obj, token, server_key="default"):
    return client.post(lifecycle_url(obj), {"server_key": server_key, intent.FIELD: [token]})


def _ports(interfaces):
    return {name: mapping_summary(_fresh(interface)) for name, interface in interfaces.items()}


@pytest.mark.django_db
@SCOPES
class TestHostChanges:
    """Each change writes the object and its scope's interfaces together; other servers' entries stay."""

    def test_unlink_removes_the_entry_its_preference_and_the_scope_ports(self, writer, server, kind):
        owner, interfaces = _linked(kind, f"unlink-{kind}")
        records = {name: _change_records(interface) for name, interface in interfaces.items()}
        owner_records = _change_records(owner)

        response = _confirm(writer, owner, offered_token(unlink_page(writer, owner)))

        assert response.status_code == 302
        assert mapping_summary(_fresh(owner)) == {"other": {"own": 4102}}
        assert _ports(interfaces) == {"both": {"other": {"own": 5200}}, "default": {}, "none": {}}
        assert _change_records(owner) == owner_records + 1
        assert {name: _change_records(interface) - records[name] for name, interface in interfaces.items()} == {
            "both": 1,
            "default": 1,
            "none": 0,
        }

    def test_relink_replaces_the_own_id_and_clears_the_scope_ports(self, writer, server, kind):
        owner, interfaces = _linked(kind, f"relink-{kind}")

        confirmation = edit_mapping(writer, owner, own_id="4110")
        assert mapping_summary(_fresh(owner))["default"]["own"] == 4100
        _confirm(writer, owner, offered_token(confirmation))

        assert mapping_summary(_fresh(owner)) == {
            "default": {"own": 4110, "oob": 4101, "oob_type": "idrac"},
            "other": {"own": 4102},
            "preferred": "default",
        }
        assert _ports(interfaces) == {"both": {"other": {"own": 5200}}, "default": {}, "none": {}}

    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            ({"oob_id": "4111", "oob_type": "ilo"}, {"own": 4100, "oob": 4111, "oob_type": "ilo"}),
            ({"oob_id": "", "oob_type": ""}, {"own": 4100}),
        ],
        ids=["replace", "clear"],
    )
    def test_change_oob_replaces_or_clears_the_oob_and_clears_the_scope_ports(
        self, writer, server, kind, typed, expected
    ):
        owner, interfaces = _linked(kind, f"oob-{kind}-{typed['oob_id'] or 'clear'}")

        _confirm(writer, owner, offered_token(edit_mapping(writer, owner, **typed)))

        assert mapping_summary(_fresh(owner)) == {"default": expected, "other": {"own": 4102}, "preferred": "default"}
        assert _ports(interfaces) == {"both": {"other": {"own": 5200}}, "default": {}, "none": {}}


@pytest.mark.django_db
class TestTheConfirmationNamesItsEffects:
    def test_the_unlink_page_lists_each_cleared_interface_and_writes_nothing(self, writer, server):
        owner, _interfaces = _linked("chassis", "unlink-page")
        before = domain_state()

        response = unlink_page(writer, owner)

        assert domain_state() == before
        listed = {(row["parent"], row["name"]) for row in response.context["interfaces"]}
        members = list(Device.objects.filter(virtual_chassis=owner.virtual_chassis).order_by("pk"))
        assert listed == {(members[0].name, "eth0"), (members[1].name, "eth1")}
        assert (response.context["before"].own_id, response.context["before"].oob_id) == (4100, 4101)

    def test_an_unconfigured_server_unlinks_too(self, writer, server):
        owner = seed_mapping(make_device("unlink-retired"), "retired", own=4200)
        seed_mapping(owner, "default", own=4201)
        interface = _interface(owner, "eth0", retired=5300, default=5301)

        _confirm(writer, owner, offered_token(unlink_page(writer, owner, "retired")), "retired")

        assert mapping_summary(_fresh(owner)) == {"default": {"own": 4201}}
        assert mapping_summary(_fresh(interface)) == {"default": {"own": 5301}}

    def test_the_sync_page_offers_unlink_for_each_mapped_server(self, writer, server):
        owner = seed_mapping(make_device("unlink-actions"), "retired", own=4210)
        seed_mapping(owner, "default", own=4211)

        body = sync_page(writer, owner).content.decode()

        assert 'data-unlink-server="default"' in body
        assert 'data-unlink-server="retired"' in body

    def test_a_viewer_without_change_permission_gets_no_unlink(self, client, server):
        owner = seed_mapping(make_device("unlink-viewer"), "default", own=4220)
        client.force_login(make_user_with_perms("unlink-viewer", [("view", Device)]))
        before = domain_state()

        body = sync_page(client, owner).content.decode()
        response = unlink_page(client, owner)

        assert "data-unlink-server" not in body
        assert response.status_code == 302
        assert message_texts(response) == ["Missing permissions: dcim.change_device"]
        assert domain_state() == before


@pytest.mark.django_db
class TestStale:
    """A change of the object entry, the membership or the cascade after the offer is stale and writes nothing."""

    @pytest.mark.parametrize(
        ("fact", "path"),
        [
            ("interface_added", "effects.interfaces.count"),
            ("interface_removed", "effects.interfaces.count"),
            ("port_changed", "effects.interfaces.digest"),
            ("entry_changed", "assumptions.entry.own_id"),
            ("member_joined", "assumptions.membership.count"),
            ("member_left", "assumptions.membership.count"),
            ("legacy_interface", "assumptions.refused_interfaces"),
            ("migration_marker", "assumptions.migrated"),
        ],
    )
    def test_a_relevant_change_is_stale(self, writer, server, fact, path):
        owner, interfaces = _linked("chassis", f"stale-{fact}")
        second = Device.objects.filter(virtual_chassis=owner.virtual_chassis).exclude(pk=owner.pk).get()
        token = offered_token(unlink_page(writer, owner))
        if fact == "interface_added":
            _interface(second, "eth9", default=5109)
        elif fact == "interface_removed":
            interfaces["default"].delete()
        elif fact == "port_changed":
            seed_mapping(_fresh(interfaces["default"]), "default", own=5199)
        elif fact == "entry_changed":
            seed_mapping(_fresh(owner), "default", own=4199)
        elif fact == "member_joined":
            joined = make_device("stale-joined")
            joined.virtual_chassis, joined.vc_position = owner.virtual_chassis, 3
            joined.save()
        elif fact == "member_left":
            Device.objects.filter(pk=second.pk).update(virtual_chassis=None, vc_position=None)
        elif fact == "legacy_interface":
            seed_legacy(interfaces["none"], 77)
        else:
            seed_mapping(_fresh(owner), "default", migrated_to=make_device("stale-winner").pk)
        before = domain_state()

        response = _confirm(writer, owner, token)

        assert domain_state() == before
        assert path in stale_paths(response, owner.name)

    def test_an_unrelated_change_still_applies(self, writer, server):
        owner, interfaces = _linked("chassis", "stale-unrelated")
        token = offered_token(unlink_page(writer, owner))
        seed_mapping(_fresh(interfaces["both"]), "other", own=5299)
        Device.objects.filter(pk=owner.pk).update(description="changed")

        _confirm(writer, owner, token)

        assert mapping_summary(_fresh(owner)) == {"other": {"own": 4102}}
        assert mapping_summary(_fresh(interfaces["both"])) == {"other": {"own": 5299}}


@pytest.mark.django_db
class TestRefusals:
    @pytest.mark.parametrize("value", ["legacy", "invalid"])
    def test_a_legacy_or_invalid_interface_value_in_the_scope_refuses_and_names_it(self, writer, server, value):
        owner, interfaces = _linked("chassis", f"refuse-interface-{value}")
        if value == "legacy":
            seed_legacy(interfaces["none"], 78)
        else:
            seed_quarantined(interfaces["none"])
        before = domain_state()

        response = unlink_page(writer, owner)

        assert response.status_code == 302
        assert domain_state() == before
        [text] = message_texts(response)
        assert f"{owner.name} eth2" in text

    def test_a_legacy_object_value_refuses(self, writer, server):
        owner = seed_legacy(make_device("refuse-legacy-owner"), 4300)
        before = domain_state()

        response = unlink_page(writer, owner)

        assert response.status_code == 302
        assert domain_state() == before
        assert len(message_texts(response)) == 1

    def test_a_migration_marker_refuses(self, writer, server):
        winner = make_device("refuse-winner")
        owner = seed_mapping(make_device("refuse-marker"), "default", migrated_to=winner.pk)
        before = domain_state()

        response = unlink_page(writer, owner)

        assert response.status_code == 302
        assert domain_state() == before
        assert "merged" in message_texts(response)[0]

    @pytest.mark.parametrize("owner_visible", [True, False], ids=["owner-visible", "owner-hidden"])
    def test_a_relink_to_an_id_that_another_object_holds_names_the_owner(self, client, server, owner_visible):
        owner, interfaces = _linked("device", f"refuse-owned-{owner_visible}")
        holder = seed_mapping(make_device(f"refuse-holder-{owner_visible}"), "default", own=4400)
        user = make_user_with_perms(f"refuse-owned-{owner_visible}", PERMISSIONS)
        if not owner_visible:
            from users.models import ObjectPermission

            # A change grant also discloses an object, so both stop at the edited device.
            ObjectPermission.objects.filter(users=user, object_types__model="device").delete()
            for action in ("view", "change"):
                user = grant(user, action, Device, constraints={"pk": owner.pk})
        client.force_login(user)
        before = domain_state()

        response = edit_mapping(client, owner, own_id="4400")

        assert response.status_code == 302
        assert domain_state() == before
        [text] = message_texts(response)
        assert "4400" in text
        assert (holder.name in text) is owner_visible

    def test_an_actor_who_cannot_change_one_scope_interface_is_refused_the_whole_change(self, client, server):
        owner, interfaces = _linked("chassis", "refuse-scope")
        user = make_user_with_perms("refuse-scope", [p for p in PERMISSIONS if p != ("change", Interface)])
        client.force_login(grant(user, "change", Interface, constraints={"pk": interfaces["both"].pk}))
        before = domain_state()

        response = unlink_page(client, owner)

        assert response.status_code == 302
        assert domain_state() == before
        assert "permissions do not cover" in message_texts(response)[0]

    def test_a_permission_lost_after_the_offer_refuses_the_whole_change(self, client, server):
        from users.models import ObjectPermission

        owner, interfaces = _linked("chassis", "refuse-scope-late")
        user = make_user_with_perms("refuse-scope-late", PERMISSIONS)
        client.force_login(user)
        token = offered_token(unlink_page(client, owner))
        ObjectPermission.objects.filter(users=user, actions=["change"], object_types__model="interface").delete()
        client.force_login(grant(user, "change", Interface, constraints={"pk": interfaces["both"].pk}))
        before = domain_state()

        response = _confirm(client, owner, token)

        assert domain_state() == before
        assert "scope.permission" in stale_paths(response, owner.name)

    def test_a_cleared_interface_that_leaves_the_change_scope_rolls_back_the_whole_change(self, client, server):
        owner, interfaces = _linked("device", "refuse-saved-scope")
        user = make_user_with_perms("refuse-saved-scope", [p for p in PERMISSIONS if p != ("change", Interface)])
        client.force_login(grant(user, "change", Interface, constraints=mapped_on_server_constraint("default")))
        token = offered_token(unlink_page(client, owner))
        before = domain_state()

        response = _confirm(client, owner, token)

        assert domain_state() == before
        assert any("outside your change permission" in text for text in message_texts(response))


@pytest.mark.django_db
def test_after_an_unlink_the_next_page_load_proposes_the_match_and_writes_nothing(writer, server):
    owner = make_device("unlink-proposes-again")
    give_primary_ip(owner, "203.0.113.200/24")
    register_match(server, "203.0.113.200", 4500, hostname=owner.name)
    seed_mapping(_fresh(owner), "default", own=4500)
    _confirm(writer, owner, offered_token(unlink_page(writer, owner)))
    assert mapping_summary(_fresh(owner)) == {}
    before = domain_state()

    proposal = page_link(writer, owner)

    assert domain_state() == before
    assert (proposal["state"], proposal["librenms_id"]) == ("match", 4500)
    assert proposal["token"]


@pytest.mark.django_db
@pytest.mark.parametrize("count", [1, 1000, 5000])
def test_the_token_of_a_large_cascade_fits_the_token_limit(writer, server, count):
    owner = seed_mapping(make_device(f"unlink-large-{count}"), "default", own=4600)
    rows = []
    for index in range(count):
        interface = Interface(device=owner, name=f"ge-0/0/{index}", type="other")
        rows.append(seed_mapping(interface, "default", own=100000 + index, save=False))
    Interface.objects.bulk_create(rows, batch_size=1000)

    token = offered_token(unlink_page(writer, owner))

    assert len(token) <= intent.MAX_TOKEN_LENGTH
    payload = signing.loads(token.partition(":")[2], salt=f"{intent.SALT_NAMESPACE}:mapping_lifecycle")
    assert payload["operation"]["effects"]["interfaces"]["count"] == count


@pytest.mark.django_db
def test_the_token_holds_only_the_subject_and_bound_values(client, server):
    """A signed payload is readable, so it names no other object; a hidden scope member offers no token."""
    owner, interfaces = _linked("chassis", "unlink-disclosure")
    user = make_user_with_perms("unlink-disclosure", PERMISSIONS)
    client.force_login(user)

    payload = signing.loads(
        offered_token(unlink_page(client, owner)).partition(":")[2],
        salt=f"{intent.SALT_NAMESPACE}:mapping_lifecycle",
    )
    operation = payload["operation"]
    assumptions, effects = operation["assumptions"], operation["effects"]

    assert payload["subject"] == ["dcim.device", owner.pk]
    assert operation["identity"] == {"model": "dcim.device", "pk": owner.pk, "server": "default"}
    assert assumptions["entry"] == {"own_id": 4100, "oob_id": 4101, "oob_type": "idrac"}
    assert assumptions["membership"]["count"] == 2 and len(assumptions["membership"]["digest"]) == 64
    assert effects["interfaces"]["count"] == 2 and len(effects["interfaces"]["digest"]) == 64
    assert effects["clears_preference"] is True
    assert {key for key in assumptions} == {"scoped", "migrated", "entry", "membership", "refused_interfaces"}
    from users.models import ObjectPermission

    ObjectPermission.objects.filter(users=user, actions=["view"], object_types__model="device").delete()
    # The second chassis member is outside the view scope now.
    client.force_login(grant(user, "view", Device, constraints={"pk": owner.pk}))

    hidden = unlink_page(client, owner)

    assert hidden.status_code == 302
    assert "permissions do not cover" in message_texts(hidden)[0]


@transactional_db_with_all_apps()
def test_a_stale_unlink_sends_no_event(client, server, flushed_events):
    owner, interfaces = _linked("device", "unlink-stale-event")
    client.force_login(make_user_with_perms("unlink-stale-event", PERMISSIONS))
    token = offered_token(unlink_page(client, owner))
    seed_mapping(_fresh(interfaces["default"]), "default", own=5198)
    flushed_events.clear()

    _confirm(client, owner, token)

    assert mapping_summary(_fresh(owner))["default"]["own"] == 4100
    assert flushed_events == []


@transactional_db_with_all_apps()
def test_a_refused_unlink_write_sends_no_event(client, server, flushed_events):
    """The cleared interfaces leave the change scope, so the whole savepoint rolls back with its events."""
    owner, interfaces = _linked("device", "unlink-refused-event")
    user = make_user_with_perms("unlink-refused-event", [p for p in PERMISSIONS if p != ("change", Interface)])
    client.force_login(grant(user, "change", Interface, constraints=mapped_on_server_constraint("default")))
    token = offered_token(unlink_page(client, owner))
    flushed_events.clear()

    _confirm(client, owner, token)

    assert mapping_summary(_fresh(owner))["default"]["own"] == 4100
    assert flushed_events == []


@transactional_db_with_all_apps()
def test_an_applied_unlink_sends_the_events_of_the_object_and_the_cleared_interfaces(client, server, flushed_events):
    owner, interfaces = _linked("device", "unlink-event")
    client.force_login(make_user_with_perms("unlink-event", PERMISSIONS))
    token = offered_token(unlink_page(client, owner))
    flushed_events.clear()

    _confirm(client, owner, token)

    assert sorted(flushed_events) == sorted(
        [("device", owner.pk), ("interface", interfaces["both"].pk), ("interface", interfaces["default"].pk)]
    )


@transactional_db_with_all_apps()
def test_a_port_written_on_a_scope_interface_after_the_offer_makes_the_unlink_stale(client, server):
    """The port writer holds its row while the unlink waits for the interface locks, then commits first."""
    owner, interfaces = _linked("device", "unlink-port-race")
    client.force_login(make_user_with_perms("unlink-port-race", PERMISSIONS))
    token = offered_token(unlink_page(client, owner))
    unlink_pid = backend_pid(connection)
    written = Event()
    # The same builder that the interface sync writes a port with.
    port = seed_mapping(Interface(), "default", own=5150, save=False)

    def write_a_port_and_commit_when_the_unlink_waits():
        with second_connection() as other:
            seed_mapping_row(interfaces["none"], port, connection=other)
            written.set()
            wait_for_lock_wait(other, unlink_pid)
            other.commit()

    with bounded_executor(max_workers=1) as executor:
        writer_done = executor.submit(write_a_port_and_commit_when_the_unlink_waits)
        assert written.wait(5), "the port writer did not write"
        response = _confirm(client, owner, token)
        writer_done.result(timeout=10)

    assert {"effects.interfaces.count", "effects.interfaces.digest"} <= stale_paths(response, owner.name)
    assert mapping_summary(_fresh(owner))["default"]["own"] == 4100
    assert mapping_summary(_fresh(interfaces["none"])) == {"default": {"own": 5150}}
    assert mapping_summary(_fresh(interfaces["default"])) == {"default": {"own": 5101}}


@pytest.mark.django_db
class TestTheUnlinkRoute:
    """The route answers that the removed RemoveServerMappingView answered, through the unlink confirmation."""

    def test_the_change_record_holds_the_before_state_and_the_route_returns_to_the_tab(self, writer, server):
        from core.models import ObjectChange
        from django.contrib.contenttypes.models import ContentType

        from netbox_librenms_plugin.tests.mapping_fixtures import mapping_from_change_record

        owner = seed_mapping(make_device("unlink-record"), "default", own=4700, save=False)
        seed_mapping(owner, "retired", own=4701)
        last_updated = Device.objects.values_list("last_updated", flat=True).get(pk=owner.pk)
        token = offered_token(unlink_page(writer, owner, "retired", tab="interfaces"))

        response = writer.post(
            lifecycle_url(owner), {"server_key": "retired", "tab": "interfaces", intent.FIELD: token}
        )

        assert response.url.endswith("?tab=interfaces")
        assert Device.objects.values_list("last_updated", flat=True).get(pk=owner.pk) > last_updated
        change = ObjectChange.objects.get(
            changed_object_type=ContentType.objects.get_for_model(Device), changed_object_id=owner.pk, action="update"
        )
        before = mapping_from_change_record(change, before=True)
        after = mapping_from_change_record(change, before=False)
        assert set(before) == {"default", "retired"} and set(after) == {"default"}

    def test_an_unsupported_object_type_is_not_found(self, writer, server):
        from django.urls import reverse

        owner = seed_mapping(make_device("unlink-rack"), "default", own=4710)
        url = reverse(
            "plugins:netbox_librenms_plugin:mapping_lifecycle", kwargs={"object_type": "rack", "pk": owner.pk}
        )

        assert writer.get(url, {"action": "unlink", "server_key": "default"}).status_code == 404
        assert writer.post(url, {"server_key": "default"}).status_code == 404

    @pytest.mark.parametrize("server_key", ["", "_preferred_server"])
    def test_a_missing_or_reserved_server_key_is_refused(self, writer, server, server_key):
        owner = seed_mapping(make_device(f"unlink-key-{server_key}"), "default", own=4720)

        response = unlink_page(writer, owner, server_key)

        assert response.status_code == 302
        assert len(message_texts(response)) == 1
        assert mapping_summary(_fresh(owner)) == {"default": {"own": 4720}}

    def test_a_server_without_an_entry_has_nothing_to_unlink(self, writer, server):
        owner = seed_mapping(make_device("unlink-absent"), "default", own=4730)

        response = unlink_page(writer, owner, "retired")

        assert response.status_code == 302
        assert "removes or replaces no LibreNMS ID" in message_texts(response)[0]

    @pytest.mark.parametrize("failure", ["validation", "unexpected"])
    def test_a_save_failure_rolls_back_the_whole_unlink(self, writer, server, failure):
        """A validation error is reported; any other error propagates; both leave every row as it was."""
        from django.core.exceptions import ValidationError
        from django.db.models.signals import pre_save

        owner, interfaces = _linked("device", f"unlink-failure-{failure}")
        token = offered_token(unlink_page(writer, owner))
        before = domain_state()

        def reject_save(sender, instance, **kwargs):
            if instance.pk == owner.pk:
                if failure == "validation":
                    raise ValidationError({"custom_field_data": ["mapping rejected"]})
                raise RuntimeError("database write failed")

        pre_save.connect(reject_save, sender=Device, weak=False)
        try:
            if failure == "validation":
                response = _confirm(writer, owner, token)
                assert any("was not saved" in text for text in message_texts(response))
            else:
                with pytest.raises(RuntimeError, match="database write failed"):
                    _confirm(writer, owner, token)
        finally:
            pre_save.disconnect(reject_save, sender=Device)

        assert domain_state() == before

    def test_two_tokens_in_one_request_refuse(self, writer, server):
        owner, _interfaces = _linked("device", "unlink-two-tokens")
        token = offered_token(unlink_page(writer, owner))
        other = offered_token(edit_mapping(writer, owner, own_id="4799"))
        before = domain_state()

        response = writer.post(lifecycle_url(owner), {"server_key": "default", intent.FIELD: [token, other]})

        assert domain_state() == before
        assert len(message_texts(response)) == 1


@transactional_db_with_all_apps()
@pytest.mark.parametrize("check_at_save", [True, False], ids=["in-save", "at-commit"])
def test_a_lock_conflict_of_the_unlink_write_gives_the_try_again_answer(client, server, caplog, check_at_save):
    """The change record's user foreign key meets a real 55P03 while another session holds the user row."""
    from contextlib import ExitStack

    from django.contrib.auth import get_user_model

    from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_row, lock_timeout
    from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE

    owner, interfaces = _linked("device", f"unlink-locked-{check_at_save}")
    user = make_user_with_perms(f"unlink-locked-{check_at_save}", PERMISSIONS)
    client.force_login(user)
    token = offered_token(unlink_page(client, owner))

    def immediate_checks(execute, sql, params, many, context):
        if sql.startswith('UPDATE "dcim_device"'):
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        return execute(sql, params, many, context)

    with caplog.at_level("ERROR"), second_connection() as other, ExitStack() as stack:
        lock_row(other, get_user_model(), user.pk)
        stack.enter_context(lock_timeout(200))
        if check_at_save:
            stack.enter_context(connection.execute_wrapper(immediate_checks))
        response = _confirm(client, owner, token)

    assert response.status_code == 302
    assert message_texts(response) == [TRY_AGAIN_MESSAGE]
    assert mapping_summary(_fresh(owner))["default"]["own"] == 4100
    assert _ports(interfaces)["default"] == {"default": {"own": 5101}}
    assert [record.getMessage() for record in caplog.records if record.levelname == "ERROR"] == []
