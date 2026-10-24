"""The modules tab and its actions assess one bound source snapshot on each read."""

import pytest
from django.contrib import messages
from django.urls import reverse

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.acquisition.resources import blocked_scopes
from netbox_librenms_plugin.acquisition.snapshot import SourceSnapshot
from netbox_librenms_plugin.constants import MAIN_INVENTORY_SOURCE, OOB_INVENTORY_SOURCE
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency
from netbox_librenms_plugin.tests.conftest import make_superuser
from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping
from netbox_librenms_plugin.tests.test_acquisition_modules import HOST_ID, OOB_ID, device, serve

INVENTORY = [
    {"entPhysicalIndex": 1, "entPhysicalContainedIn": 0, "entPhysicalClass": "chassis"},
    {
        "entPhysicalIndex": 2,
        "entPhysicalContainedIn": 1,
        "entPhysicalClass": "module",
        "entPhysicalName": "Slot 1",
        "entPhysicalModelName": "CARD",
        "entPhysicalSerialNum": "CARD-SERIAL",
    },
]


def refresh(client, subject):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:device_module_sync", args=[subject.pk]),
        {"server_key": "default"},
        HTTP_HX_REQUEST="true",
    )


def fragment(client, subject):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": subject.pk, "tab": "modules"},
    )
    return client.get(url, {"server_key": "default"})


@pytest.mark.django_db
def test_oob_failure_keeps_host_modules_readable_and_names_the_failed_source(client, live_librenms):
    subject = device("module-tab-oob-failure", oob={"id": OOB_ID, "type": "idrac"})
    client.force_login(make_superuser("module-tab-oob-user"))
    serve(live_librenms.server, inventory=INVENTORY, transceivers=[])
    live_librenms.server.register(f"/api/v0/inventory/{OOB_ID}/all", {"status": "error"}, status=500)

    response = refresh(client, subject)

    assert response.status_code == 200
    assert response.context["module_sync"]["table"] is not None
    snapshot = source_cache.read(subject, Resource.MODULES, "default")
    assert snapshot is not None
    assert set(blocked_scopes(snapshot, Resource.MODULES)) == {"oob"}
    notices = [str(message) for message in response.context["messages"] if message.level == messages.WARNING]
    assert any(f"inventory/{OOB_ID}/all" in text for text in notices)
    requests = list(live_librenms.server.requests)
    assert fragment(client, subject).context["module_sync"]["table"] is not None
    assert live_librenms.server.requests == requests


@pytest.mark.django_db
def test_offsets_and_transceiver_synthesis_exist_only_in_the_assessment(client, live_librenms):
    from django.core.cache import cache
    from netbox_librenms_plugin.assessment.modules import assess

    subject = device("module-tab-assessment", oob={"id": OOB_ID, "type": "idrac"})
    client.force_login(make_superuser("module-tab-assessment-user"))
    serve(live_librenms.server, inventory=INVENTORY)
    live_librenms.server.register(f"/api/v0/inventory/{OOB_ID}/all", {"status": "ok", "inventory": INVENTORY})

    refresh(client, subject)

    payload = cache.get(source_cache.snapshot_key(subject, Resource.MODULES, "default"))
    snapshot = SourceSnapshot.from_payload(payload)
    assert snapshot.value(Resource.MODULES, "inventory") == INVENTORY
    assert "_source" not in snapshot.value(Resource.MODULES, "inventory")[0]
    assessed = assess(snapshot, subject=subject)["inventory"]
    host = [row for row in assessed if row["_source"] == MAIN_INVENTORY_SOURCE]
    oob = [row for row in assessed if row["_source"] == OOB_INVENTORY_SOURCE]
    assert {row["entPhysicalIndex"] for row in host} == {1, 2, 9001}
    assert {row["entPhysicalIndex"] for row in oob} == {10001, 10002}
    assert next(row for row in host if row["entPhysicalIndex"] == 9001)["_librenms_port_id"] == 7
    assert snapshot.to_payload() == payload


@pytest.mark.django_db
@pytest.mark.parametrize(
    "part,path",
    [
        ("inventory", f"inventory/{HOST_ID}/all"),
        ("ports", f"devices/{HOST_ID}/ports"),
        ("transceivers", f"devices/{HOST_ID}/transceivers"),
    ],
)
def test_failed_refresh_removes_old_modules_and_names_the_failed_part(client, live_librenms, part, path):
    subject = device("module-tab-failed")
    client.force_login(make_superuser("module-tab-failed-user"))
    serve(live_librenms.server, inventory=INVENTORY)
    refresh(client, subject)
    assert source_cache.read(subject, Resource.MODULES, "default") is not None
    live_librenms.server.register(f"/api/v0/{path}", {"status": "error"}, status=500)

    response = refresh(client, subject)

    assert response.context["module_sync"]["table"] is None
    assert source_cache.read(subject, Resource.MODULES, "default") is None
    assert not SyncCacheConsistency(subject).status("default")["modules"]["snapshot_available"]
    assert any(path in str(message) for message in response.context["messages"])


@pytest.mark.django_db
def test_oob_binding_changes_refuse_every_reader_without_deleting_the_snapshot(client, live_librenms):
    from django.core.cache import cache

    subject = device("module-tab-binding", oob={"id": "corrupt", "type": "idrac"})
    client.force_login(make_superuser("module-tab-binding-user"))
    serve(live_librenms.server, inventory=INVENTORY, transceivers=[])
    refresh(client, subject)
    key = source_cache.snapshot_key(subject, Resource.MODULES, "default")
    original = cache.get(key)
    assert original is not None
    assert source_cache.read(subject, Resource.MODULES, "default") is not None
    seed_stored_mapping(subject, {"default": {"id": HOST_ID, "oob": {"id": OOB_ID, "type": "idrac"}}})
    assert source_cache.read(subject, Resource.MODULES, "default") is None
    from dcim.models import Device

    Device.objects.filter(pk=subject.pk).update(custom_field_data=subject.custom_field_data)
    assert fragment(client, subject).status_code == 404
    assert cache.get(key) == original


@pytest.mark.django_db
def test_a_rule_edit_recovers_the_table_from_the_same_ready_source(client, live_librenms):
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    subject = device("module-tab-rule-recovery")
    client.force_login(make_superuser("module-tab-rule-user"))
    serve(live_librenms.server, inventory=INVENTORY, transceivers=[])
    rule = InventoryIgnoreRule.objects.create(
        name="Module source rule", match_type="regex", pattern="^skip$", require_serial_match_parent=False
    )
    InventoryIgnoreRule.objects.filter(pk=rule.pk).update(pattern="[")

    response = refresh(client, subject)

    assert response.context["module_sync"]["table"] is None
    assert response.context["module_sync"]["inventory_rule_error"]
    snapshot = source_cache.read(subject, Resource.MODULES, "default")
    assert snapshot is not None
    assert SyncCacheConsistency(subject).status("default")["modules"]["snapshot_available"]
    InventoryIgnoreRule.objects.filter(pk=rule.pk).update(pattern="^skip$")
    requests = list(live_librenms.server.requests)
    assert fragment(client, subject).context["module_sync"]["table"] is not None
    assert source_cache.read(subject, Resource.MODULES, "default").snapshot_id == snapshot.snapshot_id
    assert live_librenms.server.requests == requests


@pytest.mark.django_db
def test_another_reader_sees_the_blocked_oob_source_on_the_cached_tab(client, live_librenms):
    from django.contrib.auth import get_user_model
    from django.test import Client

    subject = device("module-tab-cached-oob", oob={"id": OOB_ID, "type": "idrac"})
    client.force_login(make_superuser("module-tab-cached-refresh-user"))
    serve(live_librenms.server, inventory=INVENTORY, transceivers=[])
    live_librenms.server.register(f"/api/v0/inventory/{OOB_ID}/all", {"status": "error"}, status=500)
    assert refresh(client, subject).context["module_sync"]["table"] is not None
    other = Client()
    other.force_login(get_user_model().objects.create_user(username="module-tab-cached-reader", is_superuser=True))
    requests = list(live_librenms.server.requests)

    response = fragment(other, subject)

    assert response.context["module_sync"]["table"] is not None
    assert any(f"inventory/{OOB_ID}/all" in str(message) for message in response.context["messages"])
    assert live_librenms.server.requests == requests


@pytest.mark.django_db
@pytest.mark.parametrize("changed_version", ["plugin", "netbox"])
def test_a_version_change_requires_a_new_source_snapshot(client, live_librenms, monkeypatch, settings, changed_version):
    from dataclasses import replace

    import netbox_librenms_plugin
    from django.core.cache import cache

    subject = device("module-tab-version")
    client.force_login(make_superuser("module-tab-version-user"))
    serve(live_librenms.server, inventory=INVENTORY, transceivers=[])
    assert refresh(client, subject).context["module_sync"]["table"] is not None
    previous_key = source_cache.snapshot_key(subject, Resource.MODULES, "default")
    previous_snapshot = cache.get(previous_key)
    requests = list(live_librenms.server.requests)
    if changed_version == "plugin":
        monkeypatch.setattr(netbox_librenms_plugin, "__version__", "0.0.0-test")
    else:
        settings.RELEASE = replace(settings.RELEASE, version="0.0.0-test")

    assert source_cache.read(subject, Resource.MODULES, "default") is None
    assert fragment(client, subject).status_code == 404
    assert cache.get(previous_key) == previous_snapshot
    assert live_librenms.server.requests == requests
