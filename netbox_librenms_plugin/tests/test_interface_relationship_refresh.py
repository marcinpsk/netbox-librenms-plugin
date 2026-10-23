"""The port_stack, the device OS and the stored name rules decide the relationships that a refresh shows."""

import pytest

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.models import PortStackLagPattern
from netbox_librenms_plugin.tests.source_snapshot_helpers import interface_rows

SERVER_KEY = "default"
DEVICE_ID = 42
DEVICE_PATH = f"/api/v0/devices/{DEVICE_ID}"
PORT_STACK_PATH = f"{DEVICE_PATH}/port_stack"


def _refresh(live_librenms, name, ports, *, device_os, port_stack):
    """Serve the ports, the device OS and the port_stack from the stub, then refresh; None makes that request fail."""
    from netbox_librenms_plugin.tests.conftest import make_device

    server = live_librenms.server
    server.register(f"{DEVICE_PATH}/ports", {"status": "ok", "ports": ports})
    if device_os is None:
        server.register(DEVICE_PATH, {"status": "error", "message": "unavailable"}, status=500)
    else:
        server.register(DEVICE_PATH, {"status": "ok", "devices": [{"device_id": DEVICE_ID, "os": device_os}]})
    if port_stack is None:
        server.register(PORT_STACK_PATH, {"status": "error", "message": "unavailable"}, status=500)
    else:
        server.register(PORT_STACK_PATH, {"status": "ok", "mappings": port_stack})
    device = make_device(name, librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
    return device, source_cache.refresh(device, Resource.INTERFACES, SERVER_KEY)


def _relationships(device):
    return interface_rows(device, server_key=SERVER_KEY)["port_stack_relationships"]


def _request_count(live_librenms, path):
    return sum(1 for request in live_librenms.server.requests if request["path"] == path)


@pytest.mark.django_db
class TestInterfaceRelationshipRefresh:
    def test_bridge_name_signal_fetches_and_resolves_port_stack(self, live_librenms):
        """A configured bridge name triggers port-stack fetch without a LAG or parent signal."""
        ports = [
            {"port_id": 100, "ifName": "vmbr0", "ifType": "ethernetCsmacd"},
            {"port_id": 103, "ifName": "nic0", "ifType": "ethernetCsmacd"},
        ]
        device, result = _refresh(
            live_librenms,
            "rel-bridge",
            ports,
            device_os="linux",
            port_stack=[{"high_port_id": 100, "low_port_id": 103}],
        )

        assert result.error is None
        assert _request_count(live_librenms, DEVICE_PATH) == 1
        assert _request_count(live_librenms, PORT_STACK_PATH) == 1
        assert _relationships(device)["bridge_members"] == {103: 100}

    def test_structural_signal_resolves_the_device_os(self, live_librenms):
        """The OS scopes the SAP colon skip, so a structural-only snapshot must resolve it too."""
        ports = [
            {"port_id": 10, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            {"port_id": 20, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
        ]
        device, result = _refresh(
            live_librenms,
            "rel-structural",
            ports,
            device_os="junos",
            port_stack=[{"high_port_id": 10, "low_port_id": 20}],
        )

        assert result.error is None
        assert _request_count(live_librenms, DEVICE_PATH) == 1
        assert _request_count(live_librenms, PORT_STACK_PATH) == 1
        assert _relationships(device)["lag_members"] == {10: 20}

    def test_junos_breakout_and_aggregate_units_resolve_through_the_refresh(self, live_librenms):
        """The live MX480 shape must survive the whole refresh path, colons and all."""
        # A structural signal alone used to leave the OS unknown, which kept the Nokia SAP skip
        # armed and dropped every relationship a channelized xe-1/1/3:1 takes part in.
        ports = [
            {"port_id": 4601, "ifName": "xe-1/1/3:1", "ifType": "ethernetCsmacd"},
            {"port_id": 4602, "ifName": "xe-1/1/3:1.0", "ifType": "propVirtual"},
            {"port_id": 4603, "ifName": "ae0", "ifType": "ieee8023adLag"},
            {"port_id": 4604, "ifName": "ae0.0", "ifType": "ieee8023adLag"},
        ]
        device, _result = _refresh(
            live_librenms,
            "rel-junos-breakout",
            ports,
            device_os="junos",
            port_stack=[
                {"high_port_id": 4601, "low_port_id": 4602},
                {"high_port_id": 4602, "low_port_id": 4604},
            ],
        )

        relationships = _relationships(device)
        assert relationships["lag_members"] == {4601: 4603}
        # ae0.0 -> ae0 exists in no ifStack row; it comes from the name fallback.
        assert relationships["sub_interfaces"] == {4602: 4601, 4604: 4603}

    def test_pairs_without_a_device_os_fail_the_refresh_and_store_nothing(self, live_librenms):
        """The OS scopes the SAP and LAG rules, so an unknown OS must not classify the pairs at all."""
        # Nokia shape: the aggregate is found structurally by ifType. With no OS, the SAP rule once
        # had no pattern, and the SAP lag1:0 became a LAG member; later the edge showed with a warning.
        ports = [
            {"port_id": 101, "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
            {"port_id": 102, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
            {"port_id": 200, "ifName": "lag1:0", "ifType": "ipForward"},
        ]
        device, result = _refresh(
            live_librenms,
            "rel-unknown-os",
            ports,
            device_os=None,
            port_stack=[
                {"high_port_id": 101, "low_port_id": 102},
                {"high_port_id": 200, "low_port_id": 102},
            ],
        )

        assert result.error.startswith(f"devices/{DEVICE_ID} was not obtained")
        assert source_cache.read(device, Resource.INTERFACES, SERVER_KEY) is None

    def test_an_unregistered_os_does_not_turn_a_sap_into_a_lag_member(self, live_librenms):
        """LibreNMS reporting an OS name nobody registered must not read as "no SAP notation"."""
        # The device is Nokia-shaped but reports an OS with no PortStackLagPattern row. Scoping
        # the rule to that name alone returned no pattern, and lag1:0 became a LAG member.
        ports = [
            {"port_id": 101, "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
            {"port_id": 102, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
            {"port_id": 200, "ifName": "lag1:0", "ifType": "ipForward"},
        ]
        device, _result = _refresh(
            live_librenms,
            "rel-unregistered-os",
            ports,
            device_os="sros-unregistered",
            port_stack=[
                {"high_port_id": 101, "low_port_id": 102},
                {"high_port_id": 200, "low_port_id": 102},
            ],
        )

        relationships = _relationships(device)
        assert relationships["lag_members"] == {101: 102}
        assert relationships["diagnostics"]["pairs_skipped_sap"] == 1

    def test_nokia_sap_rows_stay_excluded_through_the_refresh(self, live_librenms):
        """A Nokia device keeps the SAP skip that the Junos fix scoped away from everyone else."""
        ports = [
            {"port_id": 101, "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
            {"port_id": 102, "ifName": "lag-1", "ifType": "ieee8023adLag"},
            {"port_id": 200, "ifName": "lag1:0", "ifType": "ipForward"},
        ]
        device, _result = _refresh(
            live_librenms,
            "rel-nokia",
            ports,
            device_os="timos",
            port_stack=[
                {"high_port_id": 101, "low_port_id": 102},
                {"high_port_id": 200, "low_port_id": 102},
            ],
        )

        relationships = _relationships(device)
        assert relationships["lag_members"] == {101: 102}
        assert relationships["diagnostics"]["pairs_skipped_sap"] == 1

    def test_a_name_rule_applies_through_the_refresh_once_the_os_is_known(self, live_librenms):
        """A structural LAG and a name-pattern LAG resolve together, with the OS that scopes the name rule."""
        # "ios" is one of the rows migration 0013 seeds, so take the existing pattern.
        PortStackLagPattern.objects.get_or_create(librenms_os="ios", defaults={"lag_name_pattern": r"^Po\d+$"})
        ports = [
            {"port_id": 10, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            {"port_id": 20, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
            {"port_id": 30, "ifName": "Ethernet2", "ifType": "ethernetCsmacd"},
            {"port_id": 40, "ifName": "Po2", "ifType": "propVirtual"},
        ]
        device, result = _refresh(
            live_librenms,
            "rel-mixed",
            ports,
            device_os="ios",
            port_stack=[
                {"high_port_id": 10, "low_port_id": 20},
                {"high_port_id": 30, "low_port_id": 40},
            ],
        )

        assert result.error is None
        assert _relationships(device)["lag_members"] == {10: 20, 30: 40}

    def test_a_port_stack_failure_fails_the_refresh_and_stores_nothing(self, live_librenms):
        """Without the port_stack, the host rows have no relationships, so the host scope is blocked."""
        ports = [
            {"port_id": 10, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            {"port_id": 20, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
        ]
        device, result = _refresh(live_librenms, "rel-stack-fails", ports, device_os="junos", port_stack=None)

        assert _request_count(live_librenms, PORT_STACK_PATH) == 1
        assert result.error == "devices/42/port_stack was not obtained (unavailable)"
        assert source_cache.read(device, Resource.INTERFACES, SERVER_KEY) is None
