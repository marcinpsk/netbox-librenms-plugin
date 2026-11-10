"""The coordinator composes resource acquisitions; the web transport refreshes and reads them."""

from netbox_librenms_plugin.acquisition.coordinator import plan_acquisition

import pytest

from netbox_librenms_plugin.tests.source_snapshot_helpers import ip_address_row
from netbox_librenms_plugin.tests.view_test_helpers import make_request
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

SERVER_KEY = "default"


def _serve_ip_and_vlans(server, device_id):
    server.register(f"/api/v0/devices/{device_id}/ip", {"status": "ok", "addresses": [ip_address_row("10.0.0.1", 7)]})
    server.register(f"/api/v0/devices/{device_id}/ports", {"status": "ok", "ports": [{"port_id": 7, "ifName": "e1"}]})
    server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [{"device_id": device_id, "ip": ""}]})
    server.register("/api/v0/resources/vlans", {"status": "ok", "vlans": [{"device_id": device_id, "vlan_vlan": 5}]})


def _paths(server):
    return [request["path"] for request in server.requests]


@pytest.mark.django_db
def test_one_call_acquires_several_resources_and_makes_each_request_once(live_librenms):
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device

    device = seed_mapping(make_device("coord-two"), SERVER_KEY, own=501)
    _serve_ip_and_vlans(live_librenms.server, 501)

    # An iterator is consumed once, by the validation and the acquisition alike.
    snapshot = acquire(
        plan_acquisition(device, server_key=SERVER_KEY, resources=iter([Resource.IP_ADDRESSES, Resource.VLANS]))
    )

    assert set(snapshot.resources) == {Resource.IP_ADDRESSES, Resource.VLANS}
    assert snapshot.value(Resource.VLANS, "vlans") == [{"device_id": 501, "vlan_vlan": 5}]
    assert snapshot.value(Resource.IP_ADDRESSES, "addresses") == [ip_address_row("10.0.0.1", 7)]
    paths = _paths(live_librenms.server)
    assert sorted(paths) == sorted(set(paths))


@pytest.mark.django_db
def test_a_virtual_chassis_member_acquires_its_own_ip_addresses(live_librenms):
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.snapshot import ObjectRef
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

    _vc, (master, member) = make_virtual_chassis_members("coord-vc")
    seed_mapping(master, SERVER_KEY, own=601)
    seed_mapping(member, SERVER_KEY, own=602)
    _serve_ip_and_vlans(live_librenms.server, 602)

    snapshot = acquire(plan_acquisition(member, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES]))

    # IP addresses belong to the subject, not to the VC sync owner.
    assert snapshot.resources[Resource.IP_ADDRESSES].owner == ObjectRef.of(member)
    assert "/api/v0/devices/602/ip" in _paths(live_librenms.server)
    assert "/api/v0/devices/601/ip" not in _paths(live_librenms.server)


@pytest.mark.django_db
def test_acquisition_changes_no_netbox_row(live_librenms):
    from core.models import ObjectChange
    from dcim.models import Device

    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device

    device = seed_mapping(make_device("coord-nowrite"), SERVER_KEY, own=503)
    _serve_ip_and_vlans(live_librenms.server, 503)
    before = (Device.objects.get(pk=device.pk).last_updated, ObjectChange.objects.count())

    acquire(plan_acquisition(device, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES, Resource.VLANS]))

    assert (Device.objects.get(pk=device.pk).last_updated, ObjectChange.objects.count()) == before


@pytest.mark.django_db
def test_a_payload_of_another_plugin_version_reads_as_no_snapshot(live_librenms, monkeypatch):
    from django.core.cache import cache

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device

    device = seed_mapping(make_device("coord-version"), SERVER_KEY, own=504)
    _serve_ip_and_vlans(live_librenms.server, 504)
    request = make_request("post")
    assert (
        source_cache.refresh(
            request, source_cache.authorized_plan(request, device, server_key=SERVER_KEY, resources=[Resource.VLANS])
        ).error
        is None
    )
    key = source_cache.snapshot_key(device, Resource.VLANS, SERVER_KEY)
    payload = cache.get(key)

    cache.set(key, {**payload, "plugin_version": "0.0.0"}, timeout=60)

    assert source_cache.read(device, Resource.VLANS, SERVER_KEY) is None


@pytest.mark.django_db
def test_a_refresh_of_an_unmapped_subject_fails_and_drops_the_older_snapshot(live_librenms):
    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_source_snapshot

    from netbox_librenms_plugin.tests.mapping_fixtures import seed_unmapped

    device = seed_mapping(make_device("coord-unmapped"), SERVER_KEY, own=505)
    seed_source_snapshot(device, Resource.VLANS, SERVER_KEY, vlans=[])
    seed_unmapped(device, SERVER_KEY)
    request = make_request("post")

    result = source_cache.refresh(
        request, source_cache.authorized_plan(request, device, server_key=SERVER_KEY, resources=[Resource.VLANS])
    )

    assert result == source_cache.Refresh(None, source_cache.IDENTITY_CHANGED)
    assert source_cache.read(device, Resource.VLANS, SERVER_KEY) is None
    assert live_librenms.server.requests == []


@pytest.mark.django_db
def test_a_blocked_vlan_refresh_reports_the_failed_source_and_keeps_no_snapshot(client, live_librenms):
    from django.urls import reverse

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_source_snapshot

    device = seed_mapping(make_device("coord-vlan-blocked"), SERVER_KEY, own=505)
    seed_source_snapshot(device, Resource.VLANS, SERVER_KEY, vlans=[{"device_id": 505, "vlan_vlan": 9}])
    live_librenms.server.register("/api/v0/resources/vlans", {"status": "error", "message": "boom"}, status=500)
    client.force_login(make_superuser("coord-vlan-blocked-user"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_vlan_sync", args=[device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert "Failed to fetch VLANs: resources/vlans was not obtained (boom)" in response.content.decode()
    assert source_cache.read(device, Resource.VLANS, SERVER_KEY) is None


@pytest.mark.django_db
def test_resource_tabs_share_status_fragment_mutation_and_cleanup(client, settings, django_capture_on_commit_callbacks):
    from django.urls import reverse

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.sync_cache import (
        SyncCacheConsistency,
        SyncTab,
    )
    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device, make_superuser
    from netbox_librenms_plugin.tests.source_snapshot_helpers import (
        link_row,
        port_row,
        seed_cables_snapshot,
        seed_interfaces_snapshot,
        seed_modules_snapshot,
        seed_source_snapshot,
    )

    configure_default_librenms_server(settings)
    device = seed_mapping(make_device("coord-mixed"), SERVER_KEY, own=506)
    seed_source_snapshot(device, Resource.VLANS, SERVER_KEY, vlans=[])
    interfaces = seed_interfaces_snapshot(device, [{"port_id": 1, "ifName": "eth1"}], server_key=SERVER_KEY)
    seed_cables_snapshot(
        device,
        [link_row(1, "eth2", remote_hostname="peer")],
        [port_row(1, "eth1", device_id=506)],
        server_key=SERVER_KEY,
    )
    seed_modules_snapshot(device, [], server_key=SERVER_KEY)
    coordinator = SyncCacheConsistency(device)

    status = coordinator.status(SERVER_KEY)
    assert all(status[tab]["snapshot_available"] for tab in ("vlans", "interfaces", "cables", "modules"))
    assert not status["ipaddresses"]["snapshot_available"]

    client.force_login(make_superuser("coord-mixed-user"))
    fragment = client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": device.pk, "tab": SyncTab.INTERFACES.value},
        ),
        {"server_key": SERVER_KEY},
    )
    assert fragment.status_code == 200
    assert [row.record["port_id"] for row in fragment.context["interface_sync"]["table"].rows] == [1]

    cable_fragment = client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": device.pk, "tab": SyncTab.CABLES.value},
        ),
        {"server_key": SERVER_KEY},
    )
    assert cable_fragment.status_code == 200
    assert [row.record["local_port_id"] for row in cable_fragment.context["cable_sync"]["table"].rows] == [1]

    # A sync of the interfaces tab keeps its own snapshot and clears the others.
    with django_capture_on_commit_callbacks(execute=True):
        transition = coordinator.schedule_mutation(SyncTab.INTERFACES, SERVER_KEY)
    assert transition.completed
    assert source_cache.read(device, Resource.INTERFACES, SERVER_KEY) == interfaces
    assert source_cache.read(device, Resource.VLANS, SERVER_KEY) is None
    assert source_cache.read(device, Resource.CABLES, SERVER_KEY) is None
    assert source_cache.read(device, Resource.MODULES, SERVER_KEY) is None

    coordinator.invalidate_every_tab()

    status = coordinator.status(SERVER_KEY)
    assert not any(status[tab]["snapshot_available"] for tab in status)


@pytest.mark.django_db
def test_a_mapped_interface_is_not_a_device_acquisition_subject(live_librenms):
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device, make_interface
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

    subject = make_interface(make_device("unsupported-source-subject"), "eth0")
    seed_mapping(subject, SERVER_KEY, own=42)

    with pytest.raises(ValueError):
        acquire(plan_acquisition(subject, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES]))

    assert live_librenms.server.requests == []


@pytest.mark.django_db
def test_a_resource_name_string_is_rejected_before_any_request(live_librenms):
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device

    subject = seed_mapping(make_device("unsupported-resource-type"), SERVER_KEY, own=42)

    with pytest.raises(ValueError):
        acquire(plan_acquisition(subject, server_key=SERVER_KEY, resources=[Resource.VLANS.value]))

    assert live_librenms.server.requests == []


@pytest.mark.django_db
def test_a_deleted_planned_owner_refuses_without_resource_http(live_librenms):
    from dcim.models import Device

    from netbox_librenms_plugin.acquisition.coordinator import SubjectNotMapped, acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device

    subject = make_device("deleted-planned-source", librenms_cf={SERVER_KEY: {"id": 501}})
    plan = plan_acquisition(subject, server_key=SERVER_KEY, resources=[Resource.VLANS])
    Device.objects.filter(pk=subject.pk).delete()

    with pytest.raises(SubjectNotMapped):
        acquire(plan)

    assert live_librenms.server.requests == []


@pytest.mark.django_db
@pytest.mark.parametrize("expiry", ["delete", "expire"])
def test_cached_identity_acquisition_and_snapshot_binding_are_read_only(live_librenms, expiry):
    from core.models import ObjectChange
    from dcim.models import Device
    from django.core.cache import cache

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.librenms_api import librenms_id_cache_key
    from netbox_librenms_plugin.tests.conftest import make_device

    subject = make_device("cache-only-source")
    key = librenms_id_cache_key(subject, SERVER_KEY)
    cache.set(key, "501", timeout=60)
    _serve_ip_and_vlans(live_librenms.server, 501)
    before = (Device.objects.get(pk=subject.pk).last_updated, ObjectChange.objects.count(), cache.get(key))

    snapshot = acquire(plan_acquisition(subject, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES]))

    assert snapshot.resources[Resource.IP_ADDRESSES].host.librenms_id == 501
    assert len(live_librenms.server.requests) == 3
    assert (Device.objects.get(pk=subject.pk).last_updated, ObjectChange.objects.count(), cache.get(key)) == before
    assert source_cache.store(subject, Resource.IP_ADDRESSES, snapshot)
    assert source_cache.read(subject, Resource.IP_ADDRESSES, SERVER_KEY) == snapshot
    if expiry == "expire":
        cache.expire(key, 0)
    else:
        cache.delete(key)
    assert source_cache.read(subject, Resource.IP_ADDRESSES, SERVER_KEY) is None
    assert not source_cache.store(subject, Resource.IP_ADDRESSES, snapshot)


@pytest.mark.django_db
@pytest.mark.parametrize("cached", [True, -1, "not-an-id", "7/ports", {}, None])
def test_cached_identity_must_be_a_valid_id_on_the_selected_server(live_librenms, cached):
    from django.core.cache import cache
    from netbox_librenms_plugin.acquisition.coordinator import SubjectNotMapped, acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.librenms_api import librenms_id_cache_key, stored_librenms_id
    from netbox_librenms_plugin.tests.conftest import make_device

    subject = make_device("cache-only-invalid")
    cache.set(librenms_id_cache_key(subject, "other"), 501, timeout=60)
    cache.set(librenms_id_cache_key(subject, SERVER_KEY), cached, timeout=60)
    assert stored_librenms_id(subject, SERVER_KEY) is None
    with pytest.raises(SubjectNotMapped):
        acquire(plan_acquisition(subject, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES]))
    assert live_librenms.server.requests == []


@pytest.mark.django_db
def test_stored_identity_precedes_cached_identity(live_librenms):
    from django.core.cache import cache
    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.librenms_api import librenms_id_cache_key
    from netbox_librenms_plugin.tests.conftest import make_device

    subject = make_device("cache-shadowed", librenms_cf={SERVER_KEY: {"id": 501}})
    cache.set(librenms_id_cache_key(subject, SERVER_KEY), 502, timeout=60)
    _serve_ip_and_vlans(live_librenms.server, 501)
    snapshot = acquire(plan_acquisition(subject, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES]))
    assert snapshot.resources[Resource.IP_ADDRESSES].host.librenms_id == 501


@pytest.mark.django_db
@pytest.mark.parametrize("phase", ["acquire", "store"])
def test_identity_expiry_refuses_a_ready_refresh_without_a_false_not_found(live_librenms, monkeypatch, phase):
    from django.core.cache import cache
    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.librenms_api import librenms_id_cache_key
    from netbox_librenms_plugin.tests.conftest import make_device

    subject = make_device("cache-only-expired")
    key = librenms_id_cache_key(subject, SERVER_KEY)
    cache.set(key, 501, timeout=60)
    _serve_ip_and_vlans(live_librenms.server, 501)
    request = make_request("post")
    plan = source_cache.authorized_plan(request, subject, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES])
    real_operation = getattr(source_cache, phase)

    def expire_then_run(*args, **kwargs):
        assert cache.get(key) == 501
        cache.expire(key, 0)
        return real_operation(*args, **kwargs)

    monkeypatch.setattr(source_cache, phase, expire_then_run)
    result = source_cache.refresh(request, plan)
    assert result == source_cache.Refresh(None, source_cache.IDENTITY_CHANGED)
    assert source_cache.read(subject, Resource.IP_ADDRESSES, SERVER_KEY) is None
    assert len(live_librenms.server.requests) == (0 if phase == "acquire" else 3)


@pytest.mark.django_db
def test_a_refresh_plan_requires_its_source_owner_to_be_visible(live_librenms):
    from dcim.models import Device
    from django.http import Http404
    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    subject = make_device("hidden-source", librenms_cf={SERVER_KEY: {"id": 501}})
    visible = make_device("visible-source")
    user = make_user_with_perms("hidden-source", [("view", Device)], constraints={"pk": visible.pk})
    request = make_request("post", user=user)
    with pytest.raises(Http404):
        source_cache.authorized_plan(request, subject, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES])
    assert live_librenms.server.requests == []


@pytest.mark.django_db
def test_an_optional_mapping_field_can_use_a_real_ip_refresh(client, live_librenms):
    from django.urls import reverse
    from extras.models import CustomField
    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.server_mappings import read_mapping
    from netbox_librenms_plugin.tests.conftest import make_device, make_superuser

    CustomField.objects.filter(name="librenms_id").delete()
    subject = make_device("optional-source.example.invalid")
    live_librenms.server.register(f"/api/v0/devices/{subject.name}", {"status": "ok", "devices": [{"device_id": 501}]})
    _serve_ip_and_vlans(live_librenms.server, 501)
    client.force_login(make_superuser("optional-source"))
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[subject.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert response.context["ip_sync"]["table"] is not None
    subject.refresh_from_db()
    assert read_mapping(subject).own_id(SERVER_KEY) is None
    assert source_cache.read(subject, Resource.IP_ADDRESSES, SERVER_KEY) is not None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "resource_name,route,context_name,table_name",
    [
        ("interfaces", "device_interface_sync", "interface_sync", "table"),
        ("cables", "device_cable_sync", "cable_sync", "table"),
        ("ipaddresses", "device_ipaddress_sync", "ip_sync", "table"),
        ("modules", "device_module_sync", "module_sync", "table"),
        ("vlans", "device_vlan_sync", "vlan_sync", "vlan_table"),
    ],
)
def test_a_refresh_response_refuses_an_identity_change_after_storage(
    client, live_librenms, monkeypatch, resource_name, route, context_name, table_name
):
    from django.core.cache import cache
    from django.urls import reverse

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, SyncTabState
    from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping_row

    subject = make_device("receipt-identity-change", librenms_cf={SERVER_KEY: {"id": 5501}})
    _serve_ip_and_vlans(live_librenms.server, 5501)
    if resource_name == "modules":
        from netbox_librenms_plugin.tests.test_capture_source_acquisition import serve

        serve(live_librenms.server, 5501)
    live_librenms.server.register("/api/v0/devices/5501/port_stack", {"status": "ok", "mappings": []})
    if resource_name == "cables":
        live_librenms.server.register("/api/v0/devices/5501/links", {"status": "ok", "links": []})
        live_librenms.server.register(
            "/api/v0/devices/5501/ports",
            {"status": "ok", "ports": [{"port_id": 7, "device_id": 5501, "ifName": "e1", "deleted": 0}]},
        )
    live_librenms.server.register(
        "/api/v0/devices/5501/ip", {"status": "ok", "addresses": [ip_address_row("198.18.0.1", 7)]}
    )
    live_librenms.server.register(
        "/api/v0/resources/vlans", {"status": "ok", "vlans": [{"device_id": 5501, "vlan_vlan": 5}]}
    )
    real_outcome = SyncCacheConsistency.mark_refresh_outcome

    def publish_then_replace_identity(coordinator, *args, **kwargs):
        stored = real_outcome(coordinator, *args, **kwargs)
        assert stored
        seed_stored_mapping_row(subject, {SERVER_KEY: {"id": 5502}})
        return stored

    monkeypatch.setattr(SyncCacheConsistency, "mark_refresh_outcome", publish_then_replace_identity)
    client.force_login(make_superuser("receipt-identity-change"))
    response = client.post(
        reverse(f"plugins:netbox_librenms_plugin:{route}", args=[subject.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert response.context[context_name][table_name] is None
    assert source_cache.IDENTITY_CHANGED in response.content.decode()
    tab = SyncTab(resource_name)
    state = cache.get(SyncCacheConsistency(subject).state_key(tab, SERVER_KEY))
    assert state["state"] == SyncTabState.REFRESH_FAILED.value
    assert live_librenms.server.requests
    assert all("5502" not in request["path"] for request in live_librenms.server.requests)


def _forbidden_inventory_imports(source):
    from netbox_librenms_plugin.tests.test_acquisition_imports import _imported_modules

    imported = set(_imported_modules(source))
    forbidden = {
        "netbox_librenms_plugin.import_utils.virtual_chassis",
        "netbox_librenms_plugin.librenms_api",
        "django.core.cache",
    }
    return {bad for bad in forbidden if any(name == bad or name.startswith(f"{bad}.") for name in imported)}


@pytest.mark.parametrize("module", ["acquisition/resources.py", "assessment/modules.py", "inventory_identity.py"])
def test_inventory_helpers_import_no_discovery_or_transport_module(module):
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / module
    assert not _forbidden_inventory_imports(path.read_text())


@pytest.mark.parametrize(
    "source,forbidden",
    [
        ("import django.core.cache", "django.core.cache"),
        ("from django.core import cache", "django.core.cache"),
        ("from netbox_librenms_plugin import librenms_api", "netbox_librenms_plugin.librenms_api"),
        (
            "from netbox_librenms_plugin.import_utils import virtual_chassis as discovery",
            "netbox_librenms_plugin.import_utils.virtual_chassis",
        ),
        ("from django.core.cache import cache", "django.core.cache"),
        ("from django.utils import timezone", None),
        ("import netbox_librenms_plugin.inventory_identity as identity", None),
    ],
)
def test_inventory_import_guard_checks_import_and_from_aliases(source, forbidden):
    assert _forbidden_inventory_imports(source) == ({forbidden} if forbidden else set())
