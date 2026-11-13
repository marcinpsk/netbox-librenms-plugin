import pytest
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory

from netbox_librenms_plugin.models import PortStackLagPattern
from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView


DEVICE_ID = 42
DEVICE_PATH = f"/api/v0/devices/{DEVICE_ID}"
PORT_STACK_PATH = f"{DEVICE_PATH}/port_stack"


def _refresh_view(live_librenms, *, device_os, port_stack):
    """Serve the device OS and port_stack from the stub; None makes that request fail."""
    server = live_librenms.server
    if device_os is None:
        server.register(DEVICE_PATH, {"status": "error", "message": "unavailable"}, status=500)
    else:
        server.register(DEVICE_PATH, {"status": "ok", "devices": [{"device_id": DEVICE_ID, "os": device_os}]})
    if port_stack is None:
        server.register(PORT_STACK_PATH, {"status": "error", "message": "unavailable"}, status=500)
    else:
        server.register(PORT_STACK_PATH, {"status": "ok", "mappings": port_stack})
    view = object.__new__(BaseInterfaceTableView)
    view._librenms_api = live_librenms.api
    view.librenms_id = DEVICE_ID
    return view


def _request_count(live_librenms, path):
    return sum(1 for request in live_librenms.server.requests if request["path"] == path)


def _message_request():
    request = RequestFactory().post("/")
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


@pytest.mark.django_db
class TestInterfaceRelationshipRefresh:
    def test_bridge_name_signal_fetches_and_resolves_port_stack(self, live_librenms):
        """A configured bridge name triggers port-stack fetch without a LAG or parent signal."""
        ports = [
            {"port_id": 100, "ifName": "vmbr0", "ifType": "ethernetCsmacd"},
            {"port_id": 103, "ifName": "nic0", "ifType": "ethernetCsmacd"},
        ]
        view = _refresh_view(live_librenms, device_os="linux", port_stack=[{"high_port_id": 100, "low_port_id": 103}])
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(_message_request(), snapshot, ports, "ifName")

        assert _request_count(live_librenms, DEVICE_PATH) == 1
        assert _request_count(live_librenms, PORT_STACK_PATH) == 1
        assert snapshot["port_stack_relationships"]["bridge_members"] == {103: 100}

    def test_structural_signal_resolves_the_device_os(self, live_librenms):
        """The OS scopes the SAP colon skip, so a structural-only snapshot must resolve it too."""
        ports = [
            {"port_id": 10, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            {"port_id": 20, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
        ]
        view = _refresh_view(live_librenms, device_os="junos", port_stack=[{"high_port_id": 10, "low_port_id": 20}])
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(_message_request(), snapshot, ports, "ifName")

        assert _request_count(live_librenms, DEVICE_PATH) == 1
        assert _request_count(live_librenms, PORT_STACK_PATH) == 1
        assert snapshot["port_stack_relationships"]["lag_members"] == {10: 20}
        assert "relationship_data_incomplete" not in snapshot

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
        view = _refresh_view(
            live_librenms,
            device_os="junos",
            port_stack=[
                {"high_port_id": 4601, "low_port_id": 4602},
                {"high_port_id": 4602, "low_port_id": 4604},
            ],
        )
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(_message_request(), snapshot, ports, "ifName")

        relationships = snapshot["port_stack_relationships"]
        assert relationships["lag_members"] == {4601: 4603}
        # ae0.0 -> ae0 exists in no ifStack row; it comes from the name fallback.
        assert relationships["sub_interfaces"] == {4602: 4601, 4604: 4603}

    def test_unknown_os_keeps_sap_rows_out_and_says_the_snapshot_is_incomplete(self, live_librenms):
        """A structural-only snapshot whose OS lookup fails must not turn a SAP into a LAG member."""
        # Nokia shape: the aggregate is found structurally by ifType, so no LAG name pattern fires
        # and nothing else would have fetched the OS. Scoping the SAP rule by an OS we could not
        # read once left this device with no rule at all, and the SAP became a LAG member.
        ports = [
            {"port_id": 101, "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
            {"port_id": 102, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
            {"port_id": 200, "ifName": "lag1:0", "ifType": "ipForward"},
        ]
        view = _refresh_view(
            live_librenms,
            device_os=None,
            port_stack=[
                {"high_port_id": 101, "low_port_id": 102},
                {"high_port_id": 200, "low_port_id": 102},
            ],
        )
        request = _message_request()
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(request, snapshot, ports, "ifName")

        assert snapshot["port_stack_relationships"]["lag_members"] == {101: 102}
        # lag1:0 is ipForward, which never joins a LAG, so only the skip count proves the SAP rule fired.
        assert snapshot["port_stack_relationships"]["diagnostics"]["pairs_skipped_sap"] == 1
        # The user is told the column may be incomplete rather than being shown a wrong edge.
        assert snapshot["relationship_data_incomplete"] is True
        assert any("device OS could not be determined" in str(message) for message in get_messages(request))

    def test_an_unregistered_os_does_not_turn_a_sap_into_a_lag_member(self, live_librenms):
        """LibreNMS reporting an OS name nobody registered must not read as "no SAP notation"."""
        # The device is Nokia-shaped but reports an OS with no PortStackLagPattern row. Scoping
        # the rule to that name alone returned no pattern, and lag1:0 became a LAG member.
        ports = [
            {"port_id": 101, "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
            {"port_id": 102, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
            {"port_id": 200, "ifName": "lag1:0", "ifType": "ipForward"},
        ]
        view = _refresh_view(
            live_librenms,
            device_os="sros-unregistered",
            port_stack=[
                {"high_port_id": 101, "low_port_id": 102},
                {"high_port_id": 200, "low_port_id": 102},
            ],
        )
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(_message_request(), snapshot, ports, "ifName")

        assert snapshot["port_stack_relationships"]["lag_members"] == {101: 102}
        assert snapshot["port_stack_relationships"]["diagnostics"]["pairs_skipped_sap"] == 1

    def test_nokia_sap_rows_stay_excluded_through_the_refresh(self, live_librenms):
        """A Nokia device keeps the SAP skip that the Junos fix scoped away from everyone else."""
        ports = [
            {"port_id": 101, "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
            {"port_id": 102, "ifName": "lag-1", "ifType": "ieee8023adLag"},
            {"port_id": 200, "ifName": "lag1:0", "ifType": "ipForward"},
        ]
        view = _refresh_view(
            live_librenms,
            device_os="timos",
            port_stack=[
                {"high_port_id": 101, "low_port_id": 102},
                {"high_port_id": 200, "low_port_id": 102},
            ],
        )
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(_message_request(), snapshot, ports, "ifName")

        assert snapshot["port_stack_relationships"]["lag_members"] == {101: 102}
        assert snapshot["port_stack_relationships"]["diagnostics"]["pairs_skipped_sap"] == 1

    def test_mixed_structural_and_name_signals_warn_when_os_is_unknown(self, live_librenms):
        # "ios" is one of the rows migration 0013 seeds, so take the existing pattern.
        PortStackLagPattern.objects.get_or_create(librenms_os="ios", defaults={"lag_name_pattern": r"^Po\d+$"})
        ports = [
            {"port_id": 10, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            {"port_id": 20, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
            {"port_id": 30, "ifName": "Ethernet2", "ifType": "ethernetCsmacd"},
            {"port_id": 40, "ifName": "Po2", "ifType": "propVirtual"},
        ]
        view = _refresh_view(
            live_librenms,
            device_os=None,
            port_stack=[
                {"high_port_id": 10, "low_port_id": 20},
                {"high_port_id": 30, "low_port_id": 40},
            ],
        )
        request = _message_request()
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(request, snapshot, ports, "ifName")

        assert _request_count(live_librenms, DEVICE_PATH) == 1
        assert _request_count(live_librenms, PORT_STACK_PATH) == 1
        assert snapshot["port_stack_relationships"]["lag_members"] == {10: 20}
        assert snapshot["relationship_data_incomplete"] is True
        assert any("device OS could not be determined" in str(message) for message in get_messages(request))

    def test_structural_signal_marks_snapshot_incomplete_when_port_stack_fetch_fails(self, live_librenms):
        """A failed structural relationship fetch marks the rendered snapshot incomplete."""
        ports = [
            {"port_id": 10, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            {"port_id": 20, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
        ]
        view = _refresh_view(live_librenms, device_os="junos", port_stack=None)
        request = _message_request()
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(request, snapshot, ports, "ifName")

        assert _request_count(live_librenms, DEVICE_PATH) == 1
        assert _request_count(live_librenms, PORT_STACK_PATH) == 1
        assert snapshot["relationship_data_incomplete"] is True
        assert any("relationship data could not be fetched" in str(message) for message in get_messages(request))
