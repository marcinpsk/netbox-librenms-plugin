"""A VC member uses one cable snapshot for selection, verification and sync."""

import re

import pytest
from dcim.models import Cable
from django.urls import reverse

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.tests.conftest import (
    configured_server_key,
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis_members,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import link_row, port_row, seed_cables_snapshot


@pytest.mark.django_db
@pytest.mark.parametrize("local_name", ["eth1", "xe-2/0/0"], ids=["stable-port-owner", "member-name-owner"])
def test_a_vc_member_keeps_its_manual_pick_through_verify_and_sync(client, live_librenms, local_name):
    server_key = configured_server_key()
    chassis, (master, member) = make_virtual_chassis_members("cable-vc-pick")
    chassis.master = master
    chassis.save()
    seed_mapping(master, server_key, own=6401)
    local = make_interface(member, local_name)
    seed_mapping(local, server_key, own=100)
    neighbour = make_device("cable-vc-pick-neighbour")
    seed_mapping(neighbour, server_key, own=6402)
    chosen = make_interface(neighbour, "chosen")
    seed_mapping(chosen, server_key, own=300)
    snapshot = seed_cables_snapshot(
        member,
        [link_row(100, "eth2", remote_device_id=6402, remote_hostname=neighbour.name, remote_port_id=200)],
        [port_row(100, local.name, device_id=6401)],
        neighbour_ports={6402: [port_row(200, "eth2", device_id=6402), port_row(300, chosen.name, device_id=6402)]},
        server_key=server_key,
    )
    client.force_login(make_superuser("cable-vc-pick-user"))

    picked = client.post(
        reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[member.pk]),
        {"row_id": "100", "remote_interface_id": chosen.pk, "server_key": server_key},
        HTTP_HX_REQUEST="true",
    )

    assert picked.status_code == 200
    picked_rows = [row.record for row in picked.context["cable_sync"]["table"].rows]
    assert [row["row_id"] for row in picked_rows] == ["100"]
    assert picked_rows[0]["netbox_remote_interface_id"] == chosen.pk
    selector = re.search(r'<select[^>]*name="device_selection_100"[^>]*>(.*?)</select>', picked.content.decode(), re.S)
    selected_member = re.search(r'<option value="(\d+)" selected', selector.group(1)).group(1)
    assert int(selected_member) == member.pk
    verified = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_cable"),
        {"device_id": member.pk, "origin_device_id": member.pk, "row_id": "100", "server_key": server_key},
        content_type="application/json",
    )

    assert verified.status_code == 200
    verified_row = verified.json()["formatted_row"]
    assert reverse("dcim:interface", args=[chosen.pk]) in verified_row["remote_port"]
    assert verified_row["expected_local_id"] == local.pk
    assert verified_row["expected_remote_id"] == chosen.pk
    synced = client.post(
        reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[member.pk]),
        {
            "sync_one": "100",
            "device_selection_100": selected_member,
            "expected_local_id_100": local.pk,
            "expected_local_device_id_100": member.pk,
            "expected_remote_id_100": chosen.pk,
            "expected_remote_device_id_100": neighbour.pk,
            "server_key": server_key,
        },
        HTTP_HX_REQUEST="true",
    )

    assert synced.status_code == 200
    local.refresh_from_db()
    chosen.refresh_from_db()
    assert local.cable_id is not None
    assert local.cable_id == chosen.cable_id
    assert Cable.objects.filter(pk=local.cable_id).exists()
    synced_rows = [row.record for row in synced.context["cable_sync"]["table"].rows]
    assert [row["row_id"] for row in synced_rows] == ["100"]
    assert source_cache.read(member, Resource.CABLES, server_key).snapshot_id == snapshot.snapshot_id
    assert live_librenms.server.requests == []
