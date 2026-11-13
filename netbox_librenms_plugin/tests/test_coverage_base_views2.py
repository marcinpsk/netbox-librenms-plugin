"""
Database-backed coverage for cable and IP-address base views.

These tests use real NetBox objects, permission checks, cache entries, and rendered JSON.
LibreNMS is not contacted because every exercised view path starts from a warm cache or resolves
only against NetBox state.
"""

import json

import pytest

from netbox_librenms_plugin.tests.source_snapshot_helpers import (
    ip_address_row,
    link_row,
    port_row,
    seed_cables_snapshot,
    seed_ip_snapshot,
    sensor_row,
)
from django.core.cache import cache
from django.urls import reverse

from netbox_librenms_plugin.server_mappings import MappingRole, identity_q
from netbox_librenms_plugin.tests.conftest import (
    cable_together,
    make_device,
    make_interface,
    make_ip,
    make_superuser,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.view_test_helpers import make_request, post as view_post, read_ip_row

pytestmark = pytest.mark.django_db


def _request(payload, *, path="/verify/"):
    """Build an authenticated JSON request for a direct view call."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return make_request(
        "post",
        body,
        user=make_superuser(),
        path=path,
        content_type="application/json",
    )


def _json(response):
    """Decode a direct JsonResponse call."""
    return json.loads(response.content)


def _post_cable(
    device, links=(), *, ports=(), neighbour_ports=None, sensors=None, oob_links=None, oob_ports=None, row_id, **extra
):
    """Warm a real cable snapshot and call the real cable verification endpoint."""
    from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

    view = SingleCableVerifyView()
    server_key = view._render_server_key()
    assert server_key is not None
    seed_cables_snapshot(
        device,
        links,
        ports,
        neighbour_ports=neighbour_ports,
        sensors=sensors,
        oob_links=oob_links,
        oob_ports=oob_ports,
        server_key=server_key,
    )
    payload = {"device_id": device.pk, "row_id": row_id, "server_key": server_key, **extra}
    return view_post(view, _request(payload))


class TestLibreNMSIdentityQueries:
    """Resolve only valid LibreNMS identities and keep host and OOB identities distinct."""

    @pytest.mark.parametrize("invalid", [True, False, 4.2, 0, -1, "", "   ", None, [], {}])
    def test_invalid_identity_matches_no_device(self, invalid):
        from dcim.models import Device

        make_device("id-invalid", librenms_cf={"default": {"id": 42, "oob": {"id": 99}}})

        assert not Device.objects.filter(
            identity_q(Device, server="default", identities=(invalid,), roles=(MappingRole.OWN, MappingRole.OOB))
        ).exists()

    @pytest.mark.parametrize(
        ("mapping", "server_key", "value"),
        [
            ({"default": 42}, "default", 42),
            ({"default": "42"}, "default", 42),
            ({"default": {"id": 42}}, "default", "42"),
            ({"default": {"oob": {"id": 42}}}, "default", 42),
            (42, "secondary", "42"),
        ],
    )
    def test_supported_storage_shapes_resolve_the_real_device(self, mapping, server_key, value):
        from dcim.models import Device

        expected = make_device("id-shape", librenms_cf=mapping)
        predicate = identity_q(Device, server=server_key, identities=(value,), roles=(MappingRole.OWN, MappingRole.OOB))

        assert Device.objects.get(predicate) == expected

    def test_remote_identity_excludes_an_oob_reference(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        expected = make_device("remote-owner", librenms_cf={"default": {"id": 71}})
        make_device("remote-oob-reference", librenms_cf={"default": {"oob": {"id": 71}}})

        device, found, error = BaseCableTableView().get_device_by_id_or_name(
            71,
            "no-name-fallback",
            "default",
        )

        assert (device, found, error) == (expected, True, None)

    def test_exact_and_short_hostname_fallbacks_use_real_querysets(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        expected = make_device("edge-switch")
        view = BaseCableTableView()

        exact = view.get_device_by_id_or_name(None, "edge-switch", "default")
        short = view.get_device_by_id_or_name(None, "edge-switch.example.test", "default")
        missing = view.get_device_by_id_or_name(None, "missing.example.test", "default")

        assert exact == (expected, True, None)
        assert short == (expected, True, None)
        assert missing == (None, False, None)


class TestIPAddressRowRead:
    """The row read renders one IP row of the warm snapshot, decided in the VRF the user chose for it."""

    def test_an_unknown_device_answers_404(self, client):
        client.force_login(make_superuser())

        response = client.get(
            reverse(
                "plugins:netbox_librenms_plugin:ip_row",
                kwargs={"object_type": "device", "pk": 2_147_483_647, "row_key": "198.18.6.1/24"},
            ),
            {"server_key": "default"},
        )

        assert response.status_code == 404

    def test_existing_address_in_the_shown_vrf_renders_synced(self, client):
        client.force_login(make_superuser())
        device = make_device("ip-existing", librenms_cf={"default": {"id": 48}})
        interface = make_interface(device, "Ethernet8")
        make_ip("198.18.8.1/24", assigned_object=interface)
        seed_ip_snapshot(device, [ip_address_row("198.18.8.1", 8)], [{"port_id": 8, "ifName": "Ethernet8"}])

        row = read_ip_row(client, device, "198.18.8.1/24", vrf="")

        assert "Synced" in row.status

    def test_existing_address_in_a_different_vrf_renders_update(self, client):
        from ipam.models import VRF

        client.force_login(make_superuser())
        device = make_device("ip-update", librenms_cf={"default": {"id": 49}})
        interface = make_interface(device, "Ethernet9")
        make_ip("198.18.9.1/24", assigned_object=interface)
        seed_ip_snapshot(device, [ip_address_row("198.18.9.1", 9)], [{"port_id": 9, "ifName": "Ethernet9"}])
        vrf = VRF.objects.create(name="new-vrf", rd="64512:10")

        row = read_ip_row(client, device, "198.18.9.1/24", vrf=str(vrf.pk))

        assert "Update" in row.status
        assert row.shown_vrf == str(vrf.pk)

    def test_an_address_netbox_lacks_renders_create_in_the_chosen_vrf(self, client):
        from ipam.models import VRF

        client.force_login(make_superuser())
        device = make_device("ip-create", librenms_cf={"default": {"id": 50}})
        make_interface(device, "Ethernet10")
        seed_ip_snapshot(device, [ip_address_row("198.18.10.1", 10)], [{"port_id": 10, "ifName": "Ethernet10"}])
        vrf = VRF.objects.create(name="create-vrf", rd="64512:11")

        row = read_ip_row(client, device, "198.18.10.1/24", vrf=str(vrf.pk))

        assert "Create</button>" in row.status
        assert row.shown_vrf == str(vrf.pk)


class TestCableVerifyEndpoint:
    """Recompute cached cable rows from current NetBox interfaces and cables."""

    def test_missing_device_returns_the_empty_row(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        response = view_post(SingleCableVerifyView(), _request({"server_key": "default"}))

        assert response.status_code == 200
        assert _json(response)["formatted_row"]["cable_status"] == "Missing Ports"

    def test_an_unsupported_source_snapshot_returns_an_empty_row_without_deletion(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        device = make_device("cable-malformed")
        view = SingleCableVerifyView()
        server_key = view._render_server_key()
        assert server_key is not None
        key = view.get_cache_key(device, "links", server_key)
        cache.set(key, {"schema": -1}, timeout=300)

        response = view_post(
            view,
            _request({"device_id": device.pk, "row_id": "7", "server_key": server_key}),
        )

        assert response.status_code == 200
        assert _json(response)["formatted_row"]["cable_status"] == "Missing Ports"
        assert cache.get(key) == {"schema": -1}

    def test_origin_must_match_the_selected_device(self):
        selected = make_device("cable-selected")
        origin = make_device("cable-other-origin")
        seed_mapping(selected, "default", own=406)

        response = _post_cable(
            selected,
            [],
            row_id="1",
            origin_device_id=origin.pk,
        )

        assert response.status_code == 400
        assert _json(response)["message"] == "The cable page and selected device do not match."

    def test_serial_row_rejects_member_verification(self):
        from dcim.models import ConsoleServerPort

        device = make_device("cable-serial")
        ConsoleServerPort.objects.create(device=device, name="ttyS0")
        seed_mapping(device, "default", own=406)
        response = _post_cable(
            device, sensors=[sensor_row(7, 0, device_id=406, sensor_descr="console-server Status")], row_id="serial:7"
        )

        assert response.status_code == 400
        assert _json(response)["message"] == "Serial cable rows have a fixed device owner."

    def test_real_interfaces_and_cable_render_current_links(self):
        local = make_device("cable-local")
        remote = make_device("cable-remote")
        local_interface = make_interface(local, "Ethernet10")
        remote_interface = make_interface(remote, "Ethernet11")
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        server_key = SingleCableVerifyView()._render_server_key()
        assert server_key is not None
        seed_mapping(local_interface, server_key, own=1010)
        seed_mapping(remote_interface, server_key, own=1111)
        cable = cable_together(local_interface, remote_interface)
        seed_mapping(local, server_key, own=406)
        seed_mapping(remote, server_key, own=408)
        response = _post_cable(
            local,
            [link_row(1010, "stale-remote-name", remote_hostname=remote.name, remote_port_id=1111)],
            ports=[port_row(1010, "stale-local-name", device_id=406)],
            neighbour_ports={408: [port_row(1111, "stale-remote-name", device_id=408)]},
            row_id="1010",
        )
        rendered = _json(response)["formatted_row"]

        assert response.status_code == 200
        assert f"/dcim/interfaces/{local_interface.pk}/" in rendered["local_port"]
        assert f"/dcim/interfaces/{remote_interface.pk}/" in rendered["remote_port"]
        assert f"/dcim/devices/{remote.pk}/" in rendered["remote_device"]
        assert f"/dcim/cables/{cable.pk}/" in rendered["cable_status"]

    def test_unresolved_oob_port_keeps_badge_and_never_renders_none(self):
        device = make_device("cable-oob")
        seed_mapping(device, "default", own=406, oob=407)
        response = _post_cable(device, oob_links=[link_row(1212, "")], oob_ports=[], row_id="1212")
        rendered = _json(response)["formatted_row"]

        assert response.status_code == 200
        assert rendered["can_create_cable"] is False
        assert "OOB" in rendered["local_port"]
        assert "None" not in rendered["local_port"]
