"""Real multi-server cache-scoping tests for sync-tab GET renders."""

from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

import pytest

from netbox_librenms_plugin.tests.source_snapshot_helpers import link_row, port_row, seed_cables_snapshot

from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_modules_snapshot
from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip, make_superuser

pytestmark = pytest.mark.django_db


@pytest.fixture
def configured_servers(configure_librenms):
    """Configure two real LibreNMS clients without requiring a network request."""
    configure_librenms(
        {
            "default": {
                "display_name": "Default test server",
                "librenms_url": "https://default.example.test",
                "api_token": "test-token",
                "verify_ssl": True,
            },
            "production": {
                "display_name": "Production test server",
                "librenms_url": "https://production.example.test",
                "api_token": "test-token",
                "verify_ssl": True,
            },
        }
    )


def _request(server_key):
    from django.test import RequestFactory

    request = RequestFactory().get("/", {"server_key": server_key})
    request.user = make_superuser()
    return request


def _bind(view, request):
    view.setup(request)
    return view


class TestConfiguredServerCacheScoping:
    """A configured requested server reads only that server's snapshot."""

    def test_interfaces_render_the_requested_servers_snapshot(self, configured_servers):
        from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_interfaces_snapshot
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = make_device("multiserver-interface")
        make_interface(device, "Ethernet1")
        view = DeviceInterfaceTableView()
        request = _request("production")
        seed_interfaces_snapshot(
            device, [{"port_id": 11, "ifName": "Ethernet1", "ifAdminStatus": "up"}], server_key="production"
        )

        context = _bind(view, request).get_context_data(request, device, "ifName")

        assert context["server_key"] == "production"
        assert context["table"] is not None
        assert list(context["table"].data)[0]["port_id"] == 11

    def test_cables_render_the_requested_servers_snapshot(self, configured_servers):

        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        device = make_device("multiserver-cable")
        view = DeviceCableTableView()
        request = _request("production")
        seed_mapping(device, "production", own=402)
        seed_cables_snapshot(
            device,
            [link_row(12, "", local_port="Ethernet2")],
            [port_row(12, "Ethernet2", device_id=402)],
            server_key="production",
        )

        context = _bind(view, request).get_context_data(request, device)

        assert context["server_key"] == "production"
        assert context["table"] is not None
        assert list(context["table"].data)[0]["local_port_id"] == 12

    def test_vlans_render_the_requested_servers_snapshot(self, configured_servers):
        from netbox_librenms_plugin.acquisition.registry import Resource
        from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_source_snapshot
        from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

        device = make_device("multiserver-vlan")
        view = DeviceVLANTableView()
        request = _request("production")
        seed_source_snapshot(device, Resource.VLANS, "production", vlans=[{"vlan_vlan": 120, "vlan_name": "DATA"}])

        context = _bind(view, request).get_vlan_context(request, device)

        assert context["server_key"] == "production"
        assert context["vlan_table"] is not None
        assert list(context["vlan_table"].data)[0]["vlan_id"] == 120

    def test_ip_addresses_render_the_requested_servers_snapshot(self, configured_servers):
        from netbox_librenms_plugin.tests.source_snapshot_helpers import ip_address_row, seed_ip_snapshot
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        device = make_device("multiserver-ip", librenms_cf={"default": 31, "production": 13})
        interface = make_interface(device, "Ethernet3")
        make_ip("198.18.30.1/24", assigned_object=interface)
        port = {"port_id": 13, "ifName": interface.name, "ifDescr": interface.name}
        seed_ip_snapshot(device, [ip_address_row("198.18.29.1", 13)], [port], server_key="default")
        seed_ip_snapshot(device, [ip_address_row("198.18.30.1", 13)], [port], server_key="production")
        view = DeviceIPAddressTableView()
        request = _request("production")

        context = _bind(view, request).get_context_data(request, device)

        assert context["server_key"] == "production"
        assert context["table"] is not None
        assert [row["ip_with_mask"] for row in context["table"].data] == ["198.18.30.1/24"]

    def test_modules_use_the_requested_server_mapping_and_snapshot(self, configured_servers):

        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        device = make_device("multiserver-module", librenms_cf={"production": 14})
        view = DeviceModuleTableView()
        request = _request("production")
        seed_modules_snapshot(device, [], server_key="production")

        context = _bind(view, request).get_context_data(request, device)

        assert context["server_key"] == "production"
        assert context["object"] == device
        assert context["table"] is not None


class TestRemovedServerFailsClosed:
    """An unconfigured requested key never renders default or stale per-key data."""

    @pytest.fixture(autouse=True)
    def _default_only(self, configure_librenms):
        configure_librenms(
            {
                "default": {
                    "librenms_url": "https://default.example.test",
                    "api_token": "test-token",
                    "verify_ssl": True,
                }
            }
        )

    @pytest.mark.parametrize(
        ("view_path", "data_type", "payload", "context_method", "table_key", "extra_args"),
        [
            (
                "netbox_librenms_plugin.views.object_sync.devices.DeviceInterfaceTableView",
                "ports",
                {"ports": [{"port_id": 21, "ifName": "Ethernet4", "ifAdminStatus": "up"}]},
                "get_context_data",
                "table",
                ("ifName",),
            ),
            (
                "netbox_librenms_plugin.views.object_sync.devices.DeviceCableTableView",
                "links",
                {"links": [{"local_port": "Ethernet4", "local_port_id": 21}]},
                "get_context_data",
                "table",
                (),
            ),
            (
                "netbox_librenms_plugin.views.object_sync.devices.DeviceVLANTableView",
                "vlans",
                [{"vlan_vlan": 210, "vlan_name": "STALE"}],
                "get_vlan_context",
                "vlan_table",
                (),
            ),
            (
                "netbox_librenms_plugin.views.object_sync.devices.DeviceIPAddressTableView",
                "ip_addresses",
                [{"ip_address": "198.18.32.1", "prefix_length": 24, "port_id": 22}],
                "get_context_data",
                "table",
                (),
            ),
        ],
    )
    def test_stale_removed_server_snapshot_is_ignored(
        self,
        view_path,
        data_type,
        payload,
        context_method,
        table_key,
        extra_args,
    ):
        from django.core.cache import cache
        from django.utils.module_loading import import_string

        from netbox_librenms_plugin.acquisition.registry import Resource
        from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_ip_snapshot, seed_source_snapshot

        view_class = import_string(view_path)
        device = make_device(f"removed-{data_type}", librenms_cf={"removed": 22})
        if data_type == "ports":
            make_interface(device, "Ethernet4")
        view = view_class()
        request = _request("removed")
        if data_type == "vlans":
            seed_source_snapshot(device, Resource.VLANS, "removed", vlans=payload)
        elif data_type == "ip_addresses":
            seed_ip_snapshot(device, payload, server_key="removed")
        elif data_type == "links":
            seed_cables_snapshot(device, server_key="removed")
        else:
            cache.set(view.get_cache_key(device, data_type, "removed"), payload)

        context = getattr(_bind(view, request), context_method)(request, device, *extra_args)

        assert context["server_key"] == "removed"
        assert context[table_key] is None

    def test_removed_server_ip_context_keeps_real_move_candidates(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        winner = make_device("removed-ip-winner")
        donor = make_device(
            "removed-ip-donor",
            librenms_cf={"removed": {"_migrated_to": {"device_id": winner.pk, "server_key": "removed"}}},
        )
        interface = make_interface(donor, "Ethernet5")
        address = make_ip("198.18.31.1/24", assigned_object=interface)
        view = DeviceIPAddressTableView()
        request = _request("removed")

        context = _bind(view, request).get_context_data(request, donor)

        assert context["movable_ips"] == [
            {"id": address.pk, "address": "198.18.31.1/24", "interface_name": "Ethernet5"}
        ]
        assert context["set_primary_ip"] is False
