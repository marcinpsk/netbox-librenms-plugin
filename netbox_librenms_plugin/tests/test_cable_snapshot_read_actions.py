"""Cable readers and actions use the bound source facts for the current actor."""

import pytest
from dcim.models import Cable, ConsoleServerPort, Device, Interface
from django.contrib import messages
from django.contrib.messages import get_messages
from django.core.cache import cache, caches
from django.urls import reverse

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab
from netbox_librenms_plugin.tests.conftest import (
    bounded_executor,
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis,
    make_virtual_chassis_members,
    map_device_to_librenms,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import (
    link_row,
    port_row,
    seed_cables_snapshot,
    sensor_row,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

SERVER_KEY = "default"
HOST_ID = 6401
NEIGHBOUR_ID = 6402
LOCAL_PORT_ID = 100
REMOTE_PORT_ID = 200


def _fragment(client, device):
    return client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": device.pk, "tab": "cables"},
        ),
        {"server_key": SERVER_KEY},
    )


def _rows(response):
    table = response.context["cable_sync"]["table"]
    return [] if table is None else [row.record for row in table.rows]


def _refresh(client, device):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )


def _pair(*, remote_interface=False, host_ports=True, neighbour_ports=True):
    owner = map_device_to_librenms(make_device("source-read-owner"), HOST_ID, server_key=SERVER_KEY)
    subject = owner
    neighbour = map_device_to_librenms(make_device("source-read-neighbour"), NEIGHBOUR_ID, server_key=SERVER_KEY)
    local = make_interface(subject, "eth1")
    seed_mapping(local, SERVER_KEY, own=LOCAL_PORT_ID)
    remote = make_interface(neighbour, "eth2") if remote_interface else None
    if remote is not None:
        seed_mapping(remote, SERVER_KEY, own=REMOTE_PORT_ID)
    local_port = port_row(LOCAL_PORT_ID, "eth1", device_id=HOST_ID)
    remote_port = port_row(REMOTE_PORT_ID, "eth2", device_id=NEIGHBOUR_ID)
    snapshot = seed_cables_snapshot(
        subject,
        [
            link_row(
                LOCAL_PORT_ID,
                "eth2",
                local_port="eth1",
                remote_device_id=NEIGHBOUR_ID,
                remote_port_id=REMOTE_PORT_ID,
                remote_hostname=neighbour.name,
            )
        ],
        [local_port] if host_ports else [],
        neighbour_ports={NEIGHBOUR_ID: [remote_port] if neighbour_ports else []},
        server_key=SERVER_KEY,
    )
    return subject, neighbour, local, remote, snapshot, local_port, remote_port


def _sync(client, subject, local, remote):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[subject.pk]),
        {
            "server_key": SERVER_KEY,
            "select": str(LOCAL_PORT_ID),
            f"device_selection_{LOCAL_PORT_ID}": local.device_id,
            f"expected_local_id_{LOCAL_PORT_ID}": local.pk,
            f"expected_local_device_id_{LOCAL_PORT_ID}": local.device_id,
            f"expected_remote_id_{LOCAL_PORT_ID}": remote.pk,
            f"expected_remote_device_id_{LOCAL_PORT_ID}": remote.device_id,
        },
    )


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("has_evidence", [True, False], ids=["obtained-port", "missing-port"])
def test_far_end_actions_use_only_the_snapshot_port_evidence(client, live_librenms, method, has_evidence):
    subject, neighbour, local, _remote, _snapshot, _local_port, remote_port = _pair(neighbour_ports=has_evidence)
    client.force_login(make_superuser("source-far-end-reader"))
    live_librenms.server.register(f"/api/v0/ports/{REMOTE_PORT_ID}", {"status": "ok", "port": [remote_port]})

    response = getattr(client, method)(
        reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[subject.pk]),
        {"server_key": SERVER_KEY, "row_id": str(LOCAL_PORT_ID), "expected_local_id": local.pk},
    )

    assert live_librenms.server.requests == []
    if not has_evidence:
        assert response.status_code == 404
        assert not Interface.objects.filter(device=neighbour).exists()
        assert not Cable.objects.exists()
    elif method == "get":
        assert response.status_code == 200
        assert not Interface.objects.filter(device=neighbour).exists()
    else:
        assert response.status_code == 302
        assert Interface.objects.filter(device=neighbour, name="eth2").exists()
        assert Cable.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("has_evidence", [True, False], ids=["obtained-port", "missing-port"])
def test_sync_reads_snapshot_port_records_and_refuses_missing_evidence(client, live_librenms, has_evidence):
    subject, _neighbour, local, remote, _snapshot, local_port, _remote_port = _pair(
        remote_interface=True, host_ports=has_evidence
    )
    InterfaceTypeMapping.objects.create(action="ignore", name_pattern="^not-this-port$")
    client.force_login(make_superuser("source-sync-reader"))
    live_librenms.server.register(f"/api/v0/ports/{LOCAL_PORT_ID}", {"status": "ok", "port": [local_port]})

    response = _sync(client, subject, local, remote)

    assert response.status_code == 302
    assert live_librenms.server.requests == []
    assert Cable.objects.count() == int(has_evidence)


@pytest.mark.django_db
@pytest.mark.parametrize("reader", ["fragment", "verify", "sync"])
def test_cached_unmodelled_serial_rows_are_hidden_without_owner_visibility(client, live_librenms, reader):
    subject = map_device_to_librenms(make_device("source-serial-owner"), HOST_ID, server_key=SERVER_KEY)
    ConsoleServerPort.objects.create(device=subject, name="ttyS1")
    admin = make_superuser("source-serial-admin")
    restricted = make_user_with_perms("source-serial-restricted", [("view", Device), ("add", Cable), ("change", Cable)])
    sensors = [
        sensor_row(701, 1, device_id=HOST_ID),
        sensor_row(709, 9, device_id=HOST_ID),
    ]
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/links", {"status": "ok", "links": []})
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/ports", {"status": "ok", "ports": []})
    live_librenms.server.register("/api/v0/resources/sensors", {"status": "ok", "sensors": sensors})
    client.force_login(admin)
    assert {row["local_port"] for row in _rows(_refresh(client, subject))} == {"ttyS1", "ttyS9"}
    snapshot = source_cache.read(subject, Resource.CABLES, SERVER_KEY)
    before = list(live_librenms.server.requests)

    client.force_login(restricted)
    if reader == "fragment":
        response = _fragment(client, subject)
    elif reader == "verify":
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:verify_cable"),
            {"device_id": subject.pk, "row_id": "serial:709", "server_key": SERVER_KEY},
            content_type="application/json",
        )
    else:
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[subject.pk]),
            {"server_key": SERVER_KEY, "select": "serial:709", "device_selection_serial:709": subject.pk},
            HTTP_HX_REQUEST="true",
        )

    assert response.status_code == 200
    if reader == "verify":
        assert response.json()["formatted_row"]["expected_local_id"] is None
    else:
        assert _rows(response) == []
    assert source_cache.read(subject, Resource.CABLES, SERVER_KEY).snapshot_id == snapshot.snapshot_id
    assert live_librenms.server.requests == before
    client.force_login(admin)
    assert {row["local_port"] for row in _rows(_fragment(client, subject))} == {"ttyS1", "ttyS9"}


@pytest.mark.django_db
def test_serial_off_is_unknown_without_a_notice(client, live_librenms):
    subject, *_rest = _pair()
    client.force_login(make_superuser("source-serial-off"))

    response = _fragment(client, subject)

    assert response.status_code == 200
    assert response.context["cable_sync"]["blockers"] == ()
    assert not [notice for notice in get_messages(response.wsgi_request) if notice.level == messages.WARNING]
    assert source_cache.read(subject, Resource.CABLES, SERVER_KEY) is not None
    assert live_librenms.server.requests == []


@pytest.mark.django_db
def test_refresh_requires_an_actor(live_librenms):
    subject = map_device_to_librenms(make_device("source-required-actor"), HOST_ID, server_key=SERVER_KEY)
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/links", {"status": "ok", "links": []})
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/ports", {"status": "ok", "ports": []})

    with pytest.raises(TypeError):
        source_cache.refresh(None, subject, Resource.CABLES, SERVER_KEY)

    assert live_librenms.server.requests == []


@pytest.mark.django_db
def test_assessment_always_reports_its_identity_state(live_librenms):
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.assessment.cables import assess

    subject = map_device_to_librenms(make_device("source-identity-state"), HOST_ID, server_key=SERVER_KEY)
    live_librenms.server.register(
        f"/api/v0/devices/{HOST_ID}/links",
        {"status": "ok", "links": [link_row(LOCAL_PORT_ID, "eth2", local_port="eth1")]},
    )
    live_librenms.server.register(
        f"/api/v0/devices/{HOST_ID}/ports",
        {"status": "ok", "ports": [port_row(LOCAL_PORT_ID, "eth1", device_id=HOST_ID)]},
    )

    snapshot = acquire(subject, server_key=SERVER_KEY, resources=[Resource.CABLES])
    result = assess(snapshot, subject=subject)

    assert result["identity_error"] is False
    assert result["links"][0]["row_id"] == str(LOCAL_PORT_ID)


@pytest.mark.django_db
def test_an_uncached_refresh_shows_no_rows(client, live_librenms, monkeypatch):
    subject = map_device_to_librenms(make_device("source-uncached-refresh"), HOST_ID, server_key=SERVER_KEY)
    client.force_login(make_superuser("source-uncached-refresh-user"))
    live_librenms.server.register(
        f"/api/v0/devices/{HOST_ID}/links",
        {"status": "ok", "links": [link_row(LOCAL_PORT_ID, "eth2", local_port="eth1")]},
    )
    live_librenms.server.register(
        f"/api/v0/devices/{HOST_ID}/ports",
        {"status": "ok", "ports": [port_row(LOCAL_PORT_ID, "eth1", device_id=HOST_ID)]},
    )
    key = source_cache.snapshot_key(subject, Resource.CABLES, SERVER_KEY)
    real_set = cache.set

    def drop_snapshot_write(cache_key, *args, **kwargs):
        if cache_key != key:
            return real_set(cache_key, *args, **kwargs)

    monkeypatch.setattr(caches["default"], "set", drop_snapshot_write)

    response = _refresh(client, subject)

    assert response.context["cable_sync"]["table"] is None
    assert source_cache.read(subject, Resource.CABLES, SERVER_KEY) is None
    assert SyncCacheConsistency(subject).status(SERVER_KEY)[SyncTab.CABLES.value]["state"] == "refresh_failed"


@pytest.mark.django_db
def test_a_failed_host_scope_cannot_publish_obtained_neighbour_ports(client, live_librenms):
    subject = map_device_to_librenms(make_device("source-readiness-owner"), HOST_ID, server_key=SERVER_KEY)
    client.force_login(make_superuser("source-readiness-user"))
    live_librenms.server.register(
        f"/api/v0/devices/{HOST_ID}/links",
        {"status": "ok", "links": [link_row(LOCAL_PORT_ID, "eth2", remote_device_id=NEIGHBOUR_ID)]},
    )
    live_librenms.server.register(f"/api/v0/devices/{HOST_ID}/ports", {"status": "error"}, status=500)
    live_librenms.server.register(
        f"/api/v0/devices/{NEIGHBOUR_ID}/ports",
        {"status": "ok", "ports": [port_row(REMOTE_PORT_ID, "eth2", device_id=NEIGHBOUR_ID)]},
    )

    response = _refresh(client, subject)

    assert response.context["cable_sync"]["table"] is None
    assert source_cache.read(subject, Resource.CABLES, SERVER_KEY) is None
    assert f"devices/{HOST_ID}/ports" in "; ".join(
        str(notice) for notice in get_messages(response.wsgi_request) if notice.level == messages.ERROR
    )
    paths = [entry["path"] for entry in live_librenms.server.requests]
    assert f"/api/v0/devices/{NEIGHBOUR_ID}/ports" in paths
    assert "/api/v0/resources/sensors" not in paths


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("missing_field", ["device_id", "deleted"])
def test_far_end_actions_refuse_incomplete_snapshot_evidence(client, live_librenms, method, missing_field):
    subject, neighbour, local, _remote, _snapshot, local_port, remote_port = _pair()
    incomplete_port = {key: value for key, value in remote_port.items() if key != missing_field}
    seed_cables_snapshot(
        subject,
        [
            link_row(
                LOCAL_PORT_ID,
                "eth2",
                local_port="eth1",
                remote_device_id=NEIGHBOUR_ID,
                remote_port_id=REMOTE_PORT_ID,
                remote_hostname=neighbour.name,
            )
        ],
        [local_port],
        neighbour_ports={NEIGHBOUR_ID: [incomplete_port]},
        server_key=SERVER_KEY,
    )
    live_librenms.server.register(f"/api/v0/ports/{REMOTE_PORT_ID}", {"status": "ok", "port": [remote_port]})
    client.force_login(make_superuser("source-incomplete-evidence"))

    response = getattr(client, method)(
        reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[subject.pk]),
        {"server_key": SERVER_KEY, "row_id": str(LOCAL_PORT_ID), "expected_local_id": local.pk},
    )

    assert live_librenms.server.requests == []
    assert response.status_code == 409
    assert not Interface.objects.filter(device=neighbour).exists()
    assert not Cable.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("port_owner", [NEIGHBOUR_ID, NEIGHBOUR_ID + 1], ids=["neighbour-port", "other-device-port"])
def test_create_far_end_of_a_hostname_only_link_checks_the_resolved_neighbour(client, live_librenms, port_owner):
    """The create endpoint checks the port against the neighbour that acquisition resolved from the hostname."""
    subject, neighbour, local, _remote, _snapshot, local_port, _remote_port = _pair()
    seed_cables_snapshot(
        subject,
        [
            link_row(
                LOCAL_PORT_ID, "eth2", local_port="eth1", remote_port_id=REMOTE_PORT_ID, remote_hostname=neighbour.name
            )
        ],
        [local_port],
        neighbour_ports={NEIGHBOUR_ID: [port_row(REMOTE_PORT_ID, "eth2", device_id=port_owner)]},
        server_key=SERVER_KEY,
    )
    client.force_login(make_superuser("source-hostname-only-create"))
    (row,) = _rows(_fragment(client, subject))
    assert row["remote_device_id"] is None
    assert row.get("remote_create_url")

    response = client.get(
        reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[subject.pk]),
        {"server_key": SERVER_KEY, "row_id": str(LOCAL_PORT_ID), "expected_local_id": local.pk},
    )

    assert live_librenms.server.requests == []
    assert response.status_code == (200 if port_owner == NEIGHBOUR_ID else 409)
    assert not Interface.objects.filter(device=neighbour).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("mapping_moved", [False, True], ids=["mapping-kept", "mapping-moved"])
def test_create_far_end_of_a_hostname_only_link_refuses_a_moved_neighbour_mapping(client, live_librenms, mapping_moved):
    """A hostname-only row binds the port of the neighbour resolved at refresh, so a moved mapping needs a refresh."""
    subject = map_device_to_librenms(make_device("source-moved-owner"), HOST_ID, server_key=SERVER_KEY)
    neighbour = map_device_to_librenms(make_device("source-moved-neighbour"), NEIGHBOUR_ID, server_key=SERVER_KEY)
    other = make_device("source-moved-other")
    local = seed_mapping(make_interface(subject, "eth1"), SERVER_KEY, own=LOCAL_PORT_ID)
    live_librenms.server.register(
        f"/api/v0/devices/{HOST_ID}/links",
        {
            "status": "ok",
            "links": [
                link_row(
                    LOCAL_PORT_ID,
                    "eth2",
                    local_port="eth1",
                    remote_port_id=REMOTE_PORT_ID,
                    remote_hostname=neighbour.name,
                )
            ],
        },
    )
    live_librenms.server.register(
        f"/api/v0/devices/{HOST_ID}/ports",
        {"status": "ok", "ports": [port_row(LOCAL_PORT_ID, "eth1", device_id=HOST_ID)]},
    )
    live_librenms.server.register(
        f"/api/v0/devices/{NEIGHBOUR_ID}/ports",
        {"status": "ok", "ports": [port_row(REMOTE_PORT_ID, "eth2", device_id=NEIGHBOUR_ID)]},
    )
    client.force_login(make_superuser("source-moved-mapping"))
    (row,) = _rows(_refresh(client, subject))
    assert row["remote_device_id"] is None
    assert row.get("remote_create_url")
    if mapping_moved:
        map_device_to_librenms(neighbour, NEIGHBOUR_ID + 10, server_key=SERVER_KEY)
        map_device_to_librenms(other, NEIGHBOUR_ID, server_key=SERVER_KEY)

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[subject.pk]),
        {"server_key": SERVER_KEY, "row_id": str(LOCAL_PORT_ID), "expected_local_id": local.pk},
    )

    if mapping_moved:
        assert not Interface.objects.filter(device__in=(neighbour, other)).exists()
        assert not Cable.objects.exists()
        assert response.status_code == 409
        assert "Refresh" in response.content.decode()
    else:
        assert response.status_code == 302
        assert Interface.objects.filter(device=neighbour, name="eth2").exists()
        assert Cable.objects.count() == 1


@pytest.mark.django_db
def test_create_far_end_rechecks_a_hostname_neighbour_mapping_under_the_lock(client, live_librenms, monkeypatch):
    """A mapping that moves after the proposal check and before the device lock still refuses the create."""
    from netbox_librenms_plugin.views.sync.cables import CableRemoteCreateView

    subject = map_device_to_librenms(make_device("source-race-owner"), HOST_ID, server_key=SERVER_KEY)
    neighbour = map_device_to_librenms(make_device("source-race-neighbour"), NEIGHBOUR_ID, server_key=SERVER_KEY)
    other = make_device("source-race-other")
    local = seed_mapping(make_interface(subject, "eth1"), SERVER_KEY, own=LOCAL_PORT_ID)
    live_librenms.server.register(
        f"/api/v0/devices/{HOST_ID}/links",
        {
            "status": "ok",
            "links": [
                link_row(
                    LOCAL_PORT_ID,
                    "eth2",
                    local_port="eth1",
                    remote_port_id=REMOTE_PORT_ID,
                    remote_hostname=neighbour.name,
                )
            ],
        },
    )
    live_librenms.server.register(
        f"/api/v0/devices/{HOST_ID}/ports",
        {"status": "ok", "ports": [port_row(LOCAL_PORT_ID, "eth1", device_id=HOST_ID)]},
    )
    live_librenms.server.register(
        f"/api/v0/devices/{NEIGHBOUR_ID}/ports",
        {"status": "ok", "ports": [port_row(REMOTE_PORT_ID, "eth2", device_id=NEIGHBOUR_ID)]},
    )
    client.force_login(make_superuser("source-race-mapping"))
    (row,) = _rows(_refresh(client, subject))
    assert row["remote_device_id"] is None
    resolve_proposal = CableRemoteCreateView._resolve_proposal

    def resolve_then_move_the_mapping(self, *args, **kwargs):
        result = resolve_proposal(self, *args, **kwargs)
        # Another request moves the mapping after the check and before the lock.
        map_device_to_librenms(Device.objects.get(pk=neighbour.pk), NEIGHBOUR_ID + 10, server_key=SERVER_KEY)
        map_device_to_librenms(other, NEIGHBOUR_ID, server_key=SERVER_KEY)
        return result

    monkeypatch.setattr(CableRemoteCreateView, "_resolve_proposal", resolve_then_move_the_mapping)

    client.post(
        reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[subject.pk]),
        {"server_key": SERVER_KEY, "row_id": str(LOCAL_PORT_ID), "expected_local_id": local.pk},
    )

    assert not Interface.objects.filter(device__in=(neighbour, other)).exists()
    assert not Cable.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change", [None, "member-moved", "positions-swapped"], ids=["member-kept", "member-moved", "positions-swapped"]
)
def test_create_far_end_rechecks_the_chassis_of_a_hostname_neighbour_member_under_the_lock(
    client, live_librenms, monkeypatch, change
):
    """A far-end member that no longer owns the port in the neighbour's chassis refuses the create."""
    from netbox_librenms_plugin.views.sync.cables import CableRemoteCreateView

    subject = map_device_to_librenms(make_device("source-vc-race-owner"), HOST_ID, server_key=SERVER_KEY)
    _chassis, (mapping_owner, far_member, third_member) = make_virtual_chassis_members("source-vc-race", count=3)
    map_device_to_librenms(mapping_owner, NEIGHBOUR_ID, server_key=SERVER_KEY)
    other_chassis, (other_owner,) = make_virtual_chassis_members("source-vc-race-other", count=1)
    map_device_to_librenms(other_owner, NEIGHBOUR_ID + 10, server_key=SERVER_KEY)
    local = seed_mapping(make_interface(subject, "eth1"), SERVER_KEY, own=LOCAL_PORT_ID)
    live_librenms.server.register(
        f"/api/v0/devices/{HOST_ID}/links",
        {
            "status": "ok",
            "links": [
                link_row(
                    LOCAL_PORT_ID,
                    "xe-2/0/0",
                    local_port="eth1",
                    remote_port_id=REMOTE_PORT_ID,
                    remote_hostname=mapping_owner.name,
                )
            ],
        },
    )
    live_librenms.server.register(
        f"/api/v0/devices/{HOST_ID}/ports",
        {"status": "ok", "ports": [port_row(LOCAL_PORT_ID, "eth1", device_id=HOST_ID)]},
    )
    live_librenms.server.register(
        f"/api/v0/devices/{NEIGHBOUR_ID}/ports",
        {"status": "ok", "ports": [port_row(REMOTE_PORT_ID, "xe-2/0/0", device_id=NEIGHBOUR_ID)]},
    )
    client.force_login(make_superuser("source-vc-race-member"))
    (row,) = _rows(_refresh(client, subject))
    assert row["remote_device_id"] is None
    assert row["remote_port_owner_id"] == far_member.pk
    resolve_proposal = CableRemoteCreateView._resolve_proposal

    def resolve_then_move_the_member(self, *args, **kwargs):
        result = resolve_proposal(self, *args, **kwargs)
        if change == "member-moved":
            # Another request moves the far-end member to another chassis before the lock.
            moved = Device.objects.get(pk=far_member.pk)
            moved.virtual_chassis = other_chassis
            moved.vc_position = 2
            moved.save()
        elif change == "positions-swapped":
            # Another request swaps positions, so the port name now points to the third member.
            for pk, position in ((far_member.pk, 9), (third_member.pk, 2), (far_member.pk, 3)):
                Device.objects.filter(pk=pk).update(vc_position=position)
        return result

    monkeypatch.setattr(CableRemoteCreateView, "_resolve_proposal", resolve_then_move_the_member)

    client.post(
        reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[subject.pk]),
        {"server_key": SERVER_KEY, "row_id": str(LOCAL_PORT_ID), "expected_local_id": local.pk},
    )

    if change:
        assert not Interface.objects.filter(device__in=(far_member, third_member)).exists()
        assert not Cable.objects.exists()
    else:
        assert Interface.objects.filter(device=far_member, name="xe-2/0/0").exists()
        assert Cable.objects.count() == 1


@transactional_db_with_all_apps()
def test_create_far_end_locks_every_member_of_a_hostname_neighbour_chassis(client, live_librenms, monkeypatch):
    """No chassis member can take a LibreNMS identity while the create holds its locks."""
    from django.db import OperationalError, close_old_connections, connection, transaction

    from netbox_librenms_plugin.views.sync.cables import CableRemoteCreateView

    subject = map_device_to_librenms(make_device("source-vc-lock-owner"), HOST_ID, server_key=SERVER_KEY)
    # Members order by name, so the unmapped member "a" comes before the mapping owner "b".
    mapping_owner = map_device_to_librenms(make_device("source-vc-lock-b"), NEIGHBOUR_ID, server_key=SERVER_KEY)
    far_member = make_device("source-vc-lock-c")
    earlier_member = make_device("source-vc-lock-a")
    make_virtual_chassis("source-vc-lock", mapping_owner, far_member, earlier_member)
    local = seed_mapping(make_interface(subject, "eth1"), SERVER_KEY, own=LOCAL_PORT_ID)
    live_librenms.server.register(
        f"/api/v0/devices/{HOST_ID}/links",
        {
            "status": "ok",
            "links": [
                link_row(
                    LOCAL_PORT_ID,
                    "xe-2/0/0",
                    local_port="eth1",
                    remote_port_id=REMOTE_PORT_ID,
                    remote_hostname=mapping_owner.name,
                )
            ],
        },
    )
    live_librenms.server.register(
        f"/api/v0/devices/{HOST_ID}/ports",
        {"status": "ok", "ports": [port_row(LOCAL_PORT_ID, "eth1", device_id=HOST_ID)]},
    )
    live_librenms.server.register(
        f"/api/v0/devices/{NEIGHBOUR_ID}/ports",
        {"status": "ok", "ports": [port_row(REMOTE_PORT_ID, "xe-2/0/0", device_id=NEIGHBOUR_ID)]},
    )
    client.force_login(make_superuser("source-vc-lock-member"))
    (row,) = _rows(_refresh(client, subject))
    assert row["remote_device_id"] is None
    assert row["remote_port_owner_id"] == far_member.pk
    outcomes = []

    def map_the_earlier_member():
        close_old_connections()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '300ms'")
                map_device_to_librenms(
                    Device.objects.get(pk=earlier_member.pk), NEIGHBOUR_ID + 10, server_key=SERVER_KEY
                )
        finally:
            close_old_connections()

    create_remote_interface = CableRemoteCreateView._create_remote_interface

    def create_while_another_session_maps_a_member(self, *args, **kwargs):
        # The create holds its locks here, so another session tries to give a member an identity.
        with bounded_executor(max_workers=1) as executor:
            try:
                executor.submit(map_the_earlier_member).result(timeout=5)
            except OperationalError as error:
                outcomes.append(error)
            else:
                outcomes.append(None)
        return create_remote_interface(self, *args, **kwargs)

    monkeypatch.setattr(CableRemoteCreateView, "_create_remote_interface", create_while_another_session_maps_a_member)

    client.post(
        reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[subject.pk]),
        {"server_key": SERVER_KEY, "row_id": str(LOCAL_PORT_ID), "expected_local_id": local.pk},
    )

    (outcome,) = outcomes
    assert isinstance(outcome, OperationalError)
    assert "lock timeout" in str(outcome)
    assert Interface.objects.filter(device=far_member, name="xe-2/0/0").exists()
    assert Cable.objects.count() == 1
