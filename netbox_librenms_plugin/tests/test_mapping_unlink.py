"""
Relink and change_oob through the real routes, with the interface cascade.

The mapping editor offers each token, the lifecycle route applies it, and
each test reads the real database. The scope is a Device, every member of a virtual chassis, or a VM.
"""

import pytest
from dcim.models import Device, Interface
from django.db import connection
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin.tests.conftest import (
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
    lifecycle_url,
    message_texts,
    offered_token,
    stale_paths,
)
from netbox_librenms_plugin.tests.lock_conflict_helpers import commit_when_waited, second_connection
from netbox_librenms_plugin.tests.mapping_fixtures import (
    mapped_on_server_constraint,
    mapping_summary,
    seed_legacy,
    seed_malformed_entry,
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
    client.force_login(make_user_with_perms("host_change-writer", PERMISSIONS))
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
        token = offered_token(edit_mapping(writer, owner, own_id="4110"))
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
        token = offered_token(edit_mapping(writer, owner, own_id="4110"))
        seed_mapping(_fresh(interfaces["both"]), "other", own=5299)
        Device.objects.filter(pk=owner.pk).update(description="changed")

        _confirm(writer, owner, token)

        assert mapping_summary(_fresh(owner)) == {
            "default": {"own": 4110, "oob": 4101, "oob_type": "idrac"},
            "other": {"own": 4102},
            "preferred": "default",
        }
        assert mapping_summary(_fresh(interfaces["both"])) == {"other": {"own": 5299}}

    def test_a_malformed_change_of_the_object_entry_is_stale(self, writer, server):
        """The decoded IDs stay the same, so only the digest of the exact stored entry sees the change."""
        owner = seed_mapping(make_device("stale-entry-malformed"), "default", own=4120)
        _interface(owner, "eth0", default=5120)
        token = offered_token(edit_mapping(writer, owner, own_id="4110"))
        seed_malformed_entry(_fresh(owner), "default", {"id": 4120, "oob": {"id": "abc"}})
        before = domain_state()

        response = _confirm(writer, owner, token)

        assert domain_state() == before
        assert "assumptions.entry.digest" in stale_paths(response, owner.name)


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

        response = edit_mapping(writer, owner, own_id="4110")

        assert response.status_code == 302
        assert domain_state() == before
        [text] = message_texts(response)
        assert f"{owner.name} eth2" in text

    @pytest.mark.parametrize(
        ("entry", "tag"),
        [
            (True, "bool"),
            ("not-a-port", "text"),
            ({"id": 0}, "zero-id"),
            ({"id": 5110, "oob": {"id": "abc"}}, "port-with-unreadable-oob"),
        ],
        ids=["bool", "text", "zero-id", "port-with-unreadable-oob"],
    )
    def test_a_malformed_interface_entry_in_the_scope_refuses_and_names_it(
        self, writer, server, flushed_events, entry, tag
    ):
        owner, interfaces = _linked("device", f"refuse-entry-{tag}")
        seed_malformed_entry(interfaces["none"], "default", entry)
        before = domain_state()

        response = edit_mapping(writer, owner, own_id="4110")

        assert response.status_code == 302
        assert domain_state() == before
        assert flushed_events == []
        [text] = message_texts(response)
        assert f"{owner.name} eth2" in text

    def test_a_refused_interface_on_a_hidden_member_does_not_name_the_member(self, client, server):
        owner, interfaces = _linked("chassis", "refuse-hidden-member")
        hidden = Device.objects.filter(virtual_chassis=owner.virtual_chassis).exclude(pk=owner.pk).get()
        seed_legacy(interfaces["default"], 79)
        user = make_user_with_perms("refuse-hidden-member", [p for p in PERMISSIONS if p[1] is not Device])
        for action in ("view", "change"):
            user = grant(user, action, Device, constraints={"pk": owner.pk})
        client.force_login(user)
        before = domain_state()

        response = edit_mapping(client, owner, own_id="4110")

        assert response.status_code == 302
        assert domain_state() == before
        [text] = message_texts(response)
        assert hidden.name not in text

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

        response = edit_mapping(client, owner, own_id="4110")

        assert response.status_code == 302
        assert domain_state() == before
        assert "permissions do not cover" in message_texts(response)[0]

    def test_a_permission_lost_after_the_offer_refuses_the_whole_change(self, client, server):
        from users.models import ObjectPermission

        owner, interfaces = _linked("chassis", "refuse-scope-late")
        user = make_user_with_perms("refuse-scope-late", PERMISSIONS)
        client.force_login(user)
        token = offered_token(edit_mapping(client, owner, own_id="4110"))
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
        token = offered_token(edit_mapping(client, owner, own_id="4110"))
        before = domain_state()

        response = _confirm(client, owner, token)

        assert domain_state() == before
        assert any("outside your change permission" in text for text in message_texts(response))


@transactional_db_with_all_apps()
def test_a_stale_host_change_sends_no_event(client, server, flushed_events):
    owner, interfaces = _linked("device", "host_change-stale-event")
    client.force_login(make_user_with_perms("host_change-stale-event", PERMISSIONS))
    token = offered_token(edit_mapping(client, owner, own_id="4110"))
    seed_mapping(_fresh(interfaces["default"]), "default", own=5198)
    flushed_events.clear()

    _confirm(client, owner, token)

    assert mapping_summary(_fresh(owner))["default"]["own"] == 4100
    assert flushed_events == []


@transactional_db_with_all_apps()
def test_a_refused_host_change_write_sends_no_event(client, server, flushed_events):
    """The cleared interfaces leave the change scope, so the whole savepoint rolls back with its events."""
    owner, interfaces = _linked("device", "host_change-refused-event")
    user = make_user_with_perms("host_change-refused-event", [p for p in PERMISSIONS if p != ("change", Interface)])
    client.force_login(grant(user, "change", Interface, constraints=mapped_on_server_constraint("default")))
    token = offered_token(edit_mapping(client, owner, own_id="4110"))
    flushed_events.clear()

    _confirm(client, owner, token)

    assert mapping_summary(_fresh(owner))["default"]["own"] == 4100
    assert flushed_events == []


@transactional_db_with_all_apps()
def test_an_applied_host_change_sends_the_events_of_the_object_and_the_cleared_interfaces(
    client, server, flushed_events
):
    owner, interfaces = _linked("device", "host_change-event")
    client.force_login(make_user_with_perms("host_change-event", PERMISSIONS))
    token = offered_token(edit_mapping(client, owner, own_id="4110"))
    flushed_events.clear()

    _confirm(client, owner, token)

    assert sorted(flushed_events) == sorted(
        [("device", owner.pk), ("interface", interfaces["both"].pk), ("interface", interfaces["default"].pk)]
    )


@transactional_db_with_all_apps()
def test_a_port_written_on_a_scope_interface_after_the_offer_makes_the_host_change_stale(client, server):
    """The port writer holds its row while the host_change waits for the interface locks, then commits first."""
    owner, interfaces = _linked("device", "host_change-port-race")
    client.force_login(make_user_with_perms("host_change-port-race", PERMISSIONS))
    token = offered_token(edit_mapping(client, owner, own_id="4110"))
    # The same builder that the interface sync writes a port with.
    port = seed_mapping(Interface(), "default", own=5150, save=False)

    response = commit_when_waited(
        lambda other: seed_mapping_row(interfaces["none"], port, connection=other),
        lambda: _confirm(client, owner, token),
    )

    assert {"effects.interfaces.count", "effects.interfaces.digest"} <= stale_paths(response, owner.name)
    assert mapping_summary(_fresh(owner))["default"]["own"] == 4100
    assert mapping_summary(_fresh(interfaces["none"])) == {"default": {"own": 5150}}
    assert mapping_summary(_fresh(interfaces["default"])) == {"default": {"own": 5101}}


@transactional_db_with_all_apps()
def test_a_host_change_whose_device_moved_before_the_lock_retries_and_is_stale(client, server):
    """The first attempt locks the old chassis and refuses; the new attempt locks the new one and sees the move."""
    from netbox_librenms_plugin.tests.lock_conflict_helpers import commit_row_change

    owner, _interfaces = _linked("chassis", "host_change-moved")
    other_chassis, _members = make_virtual_chassis_members("host_change-moved-to", count=1)
    client.force_login(make_user_with_perms("host_change-moved", PERMISSIONS))
    token = offered_token(edit_mapping(client, owner, own_id="4110"))
    chassis_locks = []

    def move_before_the_first_chassis_lock(execute, sql, params, many, context):
        if 'FROM "dcim_virtualchassis"' in sql and "FOR UPDATE" in sql:
            if not chassis_locks:
                with second_connection() as other:
                    commit_row_change(
                        other, Device, owner.pk, {"virtual_chassis_id": other_chassis.pk, "vc_position": 5}
                    )
            chassis_locks.append(params[0])
        return execute(sql, params, many, context)

    with connection.execute_wrapper(move_before_the_first_chassis_lock):
        response = _confirm(client, owner, token)

    # The refused attempt locked the old chassis; the new transaction locked the new one.
    assert chassis_locks == [owner.virtual_chassis_id, other_chassis.pk]
    assert {"assumptions.membership.count", "assumptions.membership.digest"} & stale_paths(response, owner.name)
    assert mapping_summary(_fresh(owner))["default"]["own"] == 4100


@transactional_db_with_all_apps()
@pytest.mark.parametrize("check_at_save", [True, False], ids=["in-save", "at-commit"])
def test_a_lock_conflict_of_the_host_change_write_gives_the_try_again_answer(client, server, caplog, check_at_save):
    """The change record's user foreign key meets a real 55P03 while another session holds the user row."""
    from contextlib import ExitStack

    from django.contrib.auth import get_user_model

    from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_row, lock_timeout
    from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE

    owner, interfaces = _linked("device", f"host_change-locked-{check_at_save}")
    user = make_user_with_perms(f"host_change-locked-{check_at_save}", PERMISSIONS)
    client.force_login(user)
    token = offered_token(edit_mapping(client, owner, own_id="4110"))

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


@pytest.mark.django_db
class TestTheHostChangeRoute:
    """The route answers that the removed RemoveServerMappingView answered, through the host_change confirmation."""

    @pytest.mark.parametrize("failure", ["validation", "unexpected"])
    def test_a_save_failure_rolls_back_the_whole_host_change(self, writer, server, failure):
        """A validation error is reported; any other error propagates; both leave every row as it was."""
        from django.core.exceptions import ValidationError
        from django.db.models.signals import pre_save

        owner, interfaces = _linked("device", f"host_change-failure-{failure}")
        token = offered_token(edit_mapping(writer, owner, own_id="4110"))
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
        owner, _interfaces = _linked("device", "host_change-two-tokens")
        token = offered_token(edit_mapping(writer, owner, own_id="4110"))
        other = offered_token(edit_mapping(writer, owner, own_id="4799"))
        before = domain_state()

        response = writer.post(lifecycle_url(owner), {"server_key": "default", intent.FIELD: [token, other]})

        assert domain_state() == before
        assert len(message_texts(response)) == 1
