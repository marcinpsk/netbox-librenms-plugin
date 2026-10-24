"""
How the IP tab resolves each row's LibreNMS interface name and VRF suggestion from the source snapshot.

Its own file rather than an append to ``test_ip_address_render_boundaries.py``: that file sits on a
lower branch of the PR stack, so adding to it conflicts on every restack.
"""

from copy import deepcopy

import pytest
from django.urls import reverse
from ipam.models import VRF, IPAddress
from tenancy.models import Tenant

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.coordinator import acquire
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.data_shapes.recordings_store import load_recording
from netbox_librenms_plugin.tables.ipaddresses import IPAddressTable
from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_superuser
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping, seed_stored_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_ip_snapshot
from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_view

SERVER_KEY = "default"
DEVICE_ID = 6100

_IOSXE_RECORDING = load_recording("iosxe-subinterfaces")
_IOSXE_DEVICE_ID = _IOSXE_RECORDING["device_id"]
_IOSXE_PORTS_PATH = f"/api/v0/devices/{_IOSXE_DEVICE_ID}/ports"
_IOSXE_ADDRESSES_PATH = f"/api/v0/devices/{_IOSXE_DEVICE_ID}/ip"
_IOSXE_PORTS = _IOSXE_RECORDING["responses"][f"GET {_IOSXE_PORTS_PATH}"]["ports"]
_IOSXE_ADDRESSES = _IOSXE_RECORDING["responses"][f"GET {_IOSXE_ADDRESSES_PATH}"]["addresses"]
_RD_PORT = next(port for port in _IOSXE_PORTS if port["ifName"] == "Po1.801")
_NAME_PORT = next(port for port in _IOSXE_PORTS if port["ifName"] == "Gi0")
_UNTAGGED_PORT_ID = next(port["port_id"] for port in _IOSXE_PORTS if port["ifName"] == "Lo10")
_RD_PORT_ID = _RD_PORT["port_id"]
_NAME_PORT_ID = _NAME_PORT["port_id"]
_IOSXE_VRFS = _IOSXE_RECORDING["responses"]["GET /api/v0/routing/vrf"]["vrfs"]
_RD_VRF = next(vrf for vrf in _IOSXE_VRFS if vrf["vrf_id"] == _RD_PORT["ifVrf"])
_NAME_VRF = next(vrf for vrf in _IOSXE_VRFS if vrf["vrf_id"] == _NAME_PORT["ifVrf"])
_RD_VRF_NAME = _RD_VRF["vrf_name"]
_RD_VRF_RD = _RD_VRF["mplsVpnVrfRouteDistinguisher"]
_NAME_VRF_NAME = _NAME_VRF["vrf_name"]
_RD_ADDRESS = next(row for row in _IOSXE_ADDRESSES if row["port_id"] == _RD_PORT_ID)
_RD_CIDR = f"{_RD_ADDRESS['ipv4_address']}/{_RD_ADDRESS['ipv4_prefixlen']}"


def _set_librenms_id(obj, value):
    seed_mapping(obj, SERVER_KEY, own=value)


def _request(**query):
    return make_request("get", query)


def _ip_view(request):
    from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

    return make_view(DeviceIPAddressTableView, request, librenms_api=False)


def _assess(device, request=None):
    """Assess the stored snapshot of *device* as a superuser, through the IP table's own entry point."""
    from netbox_librenms_plugin.views.base.ip_addresses_view import read_ip_assessment

    snapshot = source_cache.read(device, Resource.IP_ADDRESSES, SERVER_KEY)
    assert snapshot is not None
    return read_ip_assessment(request or _request(), device, snapshot, SERVER_KEY)


def _render(device, request=None):
    """Render the IP tab context from the stored snapshot and return its table rows."""
    request = request or _request()
    context = _ip_view(request)._prepare_context(request, device, SERVER_KEY)
    assert context is not None
    return list(context["table"].data)


def _ports(port_count):
    return [
        {"port_id": 9000 + index, "ifName": f"Ethernet{index}", "ifDescr": f"Ethernet{index}"}
        for index in range(port_count)
    ]


def _addresses(ports):
    return [
        {"ipv4_address": f"198.18.{index}.10", "ipv4_prefixlen": 24, "port_id": port["port_id"]}
        for index, port in enumerate(ports)
    ]


def _seed(device, port_count):
    """Store a snapshot whose ports each carry one IP address."""
    ports = _ports(port_count)
    seed_ip_snapshot(device, _addresses(ports), ports)
    return ports


def _serve(live_librenms, port_count):
    """Serve a device whose ports each carry one IP address, for a real refresh."""
    ports = _ports(port_count)
    server = live_librenms.server
    server.register(f"/api/v0/devices/{DEVICE_ID}/ports", {"status": "ok", "ports": ports})
    server.register(f"/api/v0/devices/{DEVICE_ID}/ip", {"status": "ok", "addresses": _addresses(ports)})
    server.register(f"/api/v0/devices/{DEVICE_ID}", {"status": "ok", "devices": [{"device_id": DEVICE_ID, "ip": ""}]})
    # The per-port route still answers, so a regression re-introducing the fan-out is caught by the request count only.
    for port in ports:
        server.register(f"/api/v0/ports/{port['port_id']}", {"status": "ok", "port": [port]})
    return ports


def _acquired(live_librenms, recording_name, *, ports=None, addresses=None, vrfs=None):
    """
    Acquire one recorded device's IP snapshot from the loopback LibreNMS and store it.

    A given part replaces the recorded response, as LibreNMS would serve it.
    """
    recording = load_recording(recording_name)
    device_id = recording["device_id"]
    server = live_librenms.server
    server.load_recording(recording)
    replacements = {
        f"/api/v0/devices/{device_id}/ports": ("ports", ports),
        f"/api/v0/devices/{device_id}/ip": ("addresses", addresses),
        "/api/v0/routing/vrf": ("vrfs", vrfs),
    }
    for path, (field, rows) in replacements.items():
        if rows is not None:
            server.register(path, {"status": "ok", field: rows}, method="GET")
    device = make_device(f"ip-vrf-{recording_name}", librenms_cf={SERVER_KEY: {"id": device_id}})
    server.requests.clear()
    snapshot = acquire(device, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES])
    assert source_cache.store(device, Resource.IP_ADDRESSES, snapshot)
    return device


def _vrf_requests(live_librenms):
    return [request for request in live_librenms.server.requests if request["path"] == "/api/v0/routing/vrf"]


def _row_on(rows, port_id):
    return next(row for row in rows if row["port_id"] == port_id)


@pytest.mark.django_db
class TestIpRowInterfaceNames:
    """The snapshot's device ports already carry every name the IP rows need."""

    def test_render_port_key_work_grows_with_ports_and_rows(self, monkeypatch):
        """A render must not normalize all port keys again for each IP row."""
        from netbox_librenms_plugin.assessment import ip_addresses as ip_addresses_view

        device = make_device("ip-linear-cache", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
        ports = _seed(device, 20)
        original = ip_addresses_view._port_key
        conversions = []

        def record_conversion(value):
            conversions.append(value)
            return original(value)

        monkeypatch.setattr(ip_addresses_view, "_port_key", record_conversion)
        rows = _render(device)

        assert len(rows) == len(ports)
        assert len(conversions) <= 4 * (len(ports) + len(rows))

    @pytest.mark.parametrize("port_count", [4, 16])
    def test_assignment_port_lookup_work_grows_with_ports_and_rows(self, monkeypatch, port_count):
        from netbox_librenms_plugin import utils
        from netbox_librenms_plugin.assessment import ip_addresses as ip_addresses_view

        device = make_device("ip-linear-assignments", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
        ports = _seed(device, port_count)
        for port in ports:
            interface = make_interface(device, port["ifName"])
            _set_librenms_id(interface, port["port_id"])
        conversions = []
        original = utils.normalize_librenms_port_id

        def record_conversion(value):
            conversions.append(value)
            return original(value)

        monkeypatch.setattr(utils, "normalize_librenms_port_id", record_conversion)
        # ip_assignment_ports() calls the view module's own imported binding.
        monkeypatch.setattr(ip_addresses_view, "normalize_librenms_port_id", record_conversion)
        rows = _assess(device).rows

        assert len(rows) == port_count
        assert all(row.get("interface_url") for row in rows)
        assert len(conversions) <= 4 * (len(ports) + len(rows)), len(conversions)

    def test_a_refresh_reads_the_ports_once_and_a_render_reads_nothing(self, client, live_librenms):
        """One /devices/{id}/ports read replaces one /ports/{port_id} call per row; the render calls no LibreNMS."""
        device = make_device("ip-fanout", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
        ports = _serve(live_librenms, 6)
        client.force_login(make_superuser("ip-fanout-user"))

        refreshed = client.post(
            reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk]),
            {"server_key": SERVER_KEY},
            HTTP_HX_REQUEST="true",
        )
        refresh_paths = [request["path"] for request in live_librenms.server.requests]
        live_librenms.server.requests.clear()
        rendered = client.get(
            reverse(
                "plugins:netbox_librenms_plugin:sync_cache_fragment",
                kwargs={"object_type": "device", "pk": device.pk, "tab": "ipaddresses"},
            ),
            {"server_key": SERVER_KEY},
        )

        assert refreshed.status_code == 200
        assert rendered.status_code == 200
        per_port = [path for path in refresh_paths if path.startswith("/api/v0/ports/")]
        assert per_port == [], f"{len(per_port)} per-port fetches for {len(ports)} rows"
        assert refresh_paths.count(f"/api/v0/devices/{DEVICE_ID}/ports") == 1
        assert all(f'data-name="{row["ipv4_address"]}/24"' in rendered.content.decode() for row in _addresses(ports))
        assert live_librenms.server.requests == []

    def test_rows_still_carry_the_librenms_interface_name(self):
        """Dropping the fan-out must not drop the name it was fetched for."""
        device = make_device("ip-fanout-names", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
        interface = make_interface(device, "Ethernet0", iface_type="1000base-t")
        _set_librenms_id(interface, 9000)
        _seed(device, 3)

        rows = _assess(device).rows

        assert [row["interface_name"] for row in rows] == ["Ethernet0", "Ethernet1", "Ethernet2"]

    @pytest.mark.parametrize(
        ("field", "expected"), [("ifName", ["Ethernet0", "Ethernet1"]), ("ifDescr", ["uplink-0", "uplink-1"])]
    )
    def test_the_requested_name_field_is_honoured(self, field, expected):
        """ifDescr-mode devices read their name from the same one ports snapshot."""
        device = make_device("ip-fanout-ifdescr", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
        ports = [{**port, "ifDescr": f"uplink-{index}"} for index, port in enumerate(_ports(2))]
        seed_ip_snapshot(device, _addresses(ports), ports)

        assessment = _assess(device, _request(interface_name_field=field))

        assert assessment.interface_name_field == field
        assert [row["interface_name"] for row in assessment.rows] == expected

    def test_a_row_naming_an_unknown_port_keeps_its_address(self):
        """An IP row pointing at a port the device does not report still renders, without a name."""
        device = make_device("ip-fanout-unknown", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
        seed_ip_snapshot(device, [{"ipv4_address": "198.18.9.9", "ipv4_prefixlen": 24, "port_id": 4242}], _ports(1))

        assessment = _assess(device)

        assert len(assessment.rows) == 1
        assert assessment.rows[0]["ip_address"] == "198.18.9.9"
        assert assessment.rows[0].get("interface_name") is None
        # Only the ports the rows name are in the row map; a named port the snapshot lacks maps to None.
        assert assessment.ports_by_id == {"4242": None}

    def test_a_port_bound_in_netbox_is_rule_evidence_and_not_a_row_port(self):
        """A port no row names stays out of the row map; a bound one is kept as evidence for the rules."""
        device = make_device("ip-bound-evidence", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
        ports = _ports(3)
        _set_librenms_id(make_interface(device, "renamed"), ports[1]["port_id"])
        seed_ip_snapshot(device, _addresses(ports[:1]), ports)

        assessment = _assess(device)

        assert assessment.ports_by_id == {"9000": ports[0]}
        assert assessment.bound_ports_by_id == {"9001": ports[1]}

    def test_a_failed_port_read_fails_the_refresh_and_a_render_does_not_retry_it(self, client, live_librenms):
        """Without the ports the rows cannot be named, so no snapshot is stored and the render stays off LibreNMS."""
        device = make_device("ip-ports-fail", librenms_cf={SERVER_KEY: {"id": DEVICE_ID}})
        _serve(live_librenms, 2)
        ports_path = f"/api/v0/devices/{DEVICE_ID}/ports"
        live_librenms.server.register(ports_path, {"status": "error", "message": "ports unavailable"}, status=500)
        client.force_login(make_superuser("ip-ports-fail-user"))

        refreshed = client.post(
            reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk]),
            {"server_key": SERVER_KEY},
            HTTP_HX_REQUEST="true",
        )
        live_librenms.server.requests.clear()
        context = _ip_view(_request())._prepare_context(_request(), device, SERVER_KEY)

        assert refreshed.status_code == 200
        assert f"devices/{DEVICE_ID}/ports was not obtained" in refreshed.content.decode()
        assert source_cache.read(device, Resource.IP_ADDRESSES, SERVER_KEY) is None
        assert context is None
        assert live_librenms.server.requests == []


@pytest.mark.django_db
class TestIpRowVrfSuggestions:
    """LibreNMS VRF evidence in the snapshot can preselect an unset NetBox VRF choice."""

    def test_a_render_keeps_the_suggestion_without_reading_librenms(self, live_librenms):
        """The snapshot's VRF rows supply the suggestion without another LibreNMS read."""
        target = VRF.objects.create(name="NetBox RD target", rd=_RD_VRF_RD)
        device = _acquired(live_librenms, "iosxe-subinterfaces")
        live_librenms.server.requests.clear()

        tagged = _row_on(_render(device), _RD_PORT_ID)

        assert live_librenms.server.requests == []
        assert tagged["vrf_id"] is None
        assert tagged["suggested_vrf_id"] == target.pk
        assert tagged["vrf_suggested_from"] == {
            "name": _RD_VRF_NAME,
            "matched_by": "route distinguisher",
        }

    def test_a_render_removes_the_suggestion_after_the_netbox_vrf_is_deleted(self, live_librenms):
        """The render assesses the snapshot against current NetBox, so a deleted VRF is no longer suggested."""
        target = VRF.objects.create(name="NetBox RD target", rd=_RD_VRF_RD)
        device = _acquired(live_librenms, "iosxe-subinterfaces")
        assert _row_on(_render(device), _RD_PORT_ID)["suggested_vrf_id"] == target.pk
        target.delete()
        live_librenms.server.requests.clear()

        tagged = _row_on(_render(device), _RD_PORT_ID)

        assert live_librenms.server.requests == []
        assert tagged.get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in tagged

    def test_a_render_adds_the_suggestion_after_the_netbox_vrf_is_created(self, live_librenms):
        """The snapshot's source identity binds to a NetBox VRF created after the refresh."""
        device = _acquired(live_librenms, "iosxe-subinterfaces")
        assert "suggested_vrf_id" not in _row_on(_render(device), _RD_PORT_ID)
        target = VRF.objects.create(name="NetBox RD target", rd=_RD_VRF_RD)
        live_librenms.server.requests.clear()

        tagged = _row_on(_render(device), _RD_PORT_ID)

        assert live_librenms.server.requests == []
        assert tagged["vrf_id"] is None
        assert tagged["suggested_vrf_id"] == target.pk
        assert tagged["vrf_suggested_from"] == {
            "name": _RD_VRF_NAME,
            "matched_by": "route distinguisher",
        }

    def test_a_malformed_source_vrf_row_does_not_suggest(self, live_librenms):
        """A LibreNMS VRF row without a usable name gives no identity, so it never suggests."""
        VRF.objects.create(name=_NAME_VRF_NAME)
        vrfs = deepcopy(_IOSXE_VRFS)
        next(vrf for vrf in vrfs if vrf["vrf_id"] == _NAME_PORT["ifVrf"])["vrf_name"] = []
        device = _acquired(live_librenms, "iosxe-subinterfaces", vrfs=vrfs)

        tagged = _row_on(_assess(device).rows, _NAME_PORT_ID)

        assert tagged.get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in tagged

    def test_a_suggestion_never_reaches_the_verify_path_as_a_netbox_vrf(self, live_librenms):
        """A suggestion preselects a dropdown. Read as the row's VRF it would report "Synced"."""
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        target = VRF.objects.create(name="Suggested only", rd=_RD_VRF_RD)
        device = _acquired(live_librenms, "iosxe-subinterfaces")
        rows = _assess(device).rows

        assert _row_on(rows, _RD_PORT_ID)["suggested_vrf_id"] == target.pk
        # NetBox holds no IPAddress for this row, so the verify path must still offer to create it.
        verify = SingleIPAddressVerifyView()
        _row, original_vrf_id, _port_id = verify._find_row(
            rows, _RD_ADDRESS["ipv4_address"], _RD_ADDRESS["ipv4_prefixlen"]
        )
        assert original_vrf_id is None
        assert verify._determine_status(False, False, original_vrf_id, target.pk) == "sync"

    def test_unique_route_distinguisher_suggests_a_vrf(self, live_librenms):
        """A unique RD match takes precedence over the different VRF names."""
        target = VRF.objects.create(name="NetBox RD target", rd=_RD_VRF_RD)
        device = _acquired(live_librenms, "iosxe-subinterfaces")
        assert len(_vrf_requests(live_librenms)) == 1

        tagged = _row_on(_assess(device).rows, _RD_PORT_ID)

        assert tagged["vrf_id"] is None
        assert tagged["suggested_vrf_id"] == target.pk
        assert tagged["vrf_suggested_from"] == {
            "name": _RD_VRF_NAME,
            "matched_by": "route distinguisher",
        }

    def test_suggested_vrf_renders_an_accessible_source_mark(self, live_librenms):
        """The preselected dropdown identifies why LibreNMS suggested the VRF."""
        VRF.objects.create(name="NetBox RD target", rd=_RD_VRF_RD)
        device = _acquired(live_librenms, "iosxe-subinterfaces")
        tagged = _row_on(_assess(device).rows, _RD_PORT_ID)

        html = IPAddressTable([tagged], mgmt_row_ids=[]).as_html(_request())

        label = f"Suggested from LibreNMS VRF {_RD_VRF_NAME} by route distinguisher match"
        assert f'title="{label}"' in html
        assert f'aria-label="{label}"' in html
        assert "mdi-lightbulb-on-outline text-muted" in html

    def test_unique_name_suggests_a_vrf_when_no_rd_matches(self, live_librenms):
        """A unique name supplies the suggestion when the source has no RD."""
        target = VRF.objects.create(name=_NAME_VRF_NAME)
        device = _acquired(live_librenms, "iosxe-subinterfaces")

        tagged = _row_on(_assess(device).rows, _NAME_PORT_ID)

        assert tagged["vrf_id"] is None
        assert tagged["suggested_vrf_id"] == target.pk
        assert tagged["vrf_suggested_from"] == {
            "name": _NAME_VRF_NAME,
            "matched_by": "name",
        }

    @pytest.mark.parametrize("netbox_rd, suggests", [("203.0.113.82:1", False), (None, True)])
    def test_a_name_match_never_crosses_a_conflicting_rd(self, live_librenms, netbox_rd, suggests):
        """A source RD allows a name match only when the NetBox namesake has no RD."""
        namesake = VRF.objects.create(name=_RD_VRF_NAME, rd=netbox_rd)
        device = _acquired(live_librenms, "iosxe-subinterfaces")

        tagged = _row_on(_assess(device).rows, _RD_PORT_ID)

        assert tagged.get("suggested_vrf_id") == (namesake.pk if suggests else None)

    def test_an_untagged_port_does_not_suggest(self, live_librenms):
        """The common ifVrf=0 row gets no suggestion, even when other ports of the device name a VRF."""
        VRF.objects.create(name="Unrelated VRF", rd=_RD_VRF_RD)
        device = _acquired(live_librenms, "iosxe-subinterfaces")

        untagged = _row_on(_assess(device).rows, _UNTAGGED_PORT_ID)

        assert untagged.get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in untagged
        assert "librenms_vrf" not in untagged

    def test_nokia_base_instance_is_not_a_vrf_suggestion(self, live_librenms):
        """Nokia Base identifies the global table, not a VRF named Base."""
        VRF.objects.create(name="Base")
        device = _acquired(live_librenms, "nokia-timos-transceivers")

        rows = _assess(device).rows

        recording = load_recording("nokia-timos-transceivers")
        base_vrf = next(
            vrf for vrf in recording["responses"]["GET /api/v0/routing/vrf"]["vrfs"] if vrf["vrf_name"] == "Base"
        )
        ports = recording["responses"][f"GET /api/v0/devices/{recording['device_id']}/ports"]["ports"]
        base_ports = {port["port_id"] for port in ports if port["ifVrf"] == base_vrf["vrf_id"]}
        base_row = next(row for row in rows if row["port_id"] in base_ports)
        assert base_row.get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in base_row

    def test_ambiguous_route_distinguisher_does_not_suggest_a_vrf(self, live_librenms):
        """An RD claimed by two NetBox VRFs binds neither one."""
        from netbox_librenms_plugin.assessment.ip_addresses import (
            _Assessment,
            _index_snapshot_ports,
            _vrf_identities,
        )

        first = VRF.objects.create(name="First RD claimant", rd=_RD_VRF_RD)
        # NetBox enforces RD uniqueness, so keep the second candidate unsaved.
        second = VRF(name="Second RD claimant", rd=_RD_VRF_RD)
        device = _acquired(live_librenms, "iosxe-subinterfaces")
        snapshot = source_cache.read(device, Resource.IP_ADDRESSES, SERVER_KEY)
        rows = snapshot.value(Resource.IP_ADDRESSES, "addresses")
        ports_by_id, _bound = _index_snapshot_ports(snapshot.value(Resource.IP_ADDRESSES, "ports"), rows, set())
        vrf_identities = _vrf_identities(ports_by_id, rows, snapshot.value(Resource.IP_ADDRESSES, "vrfs"))

        assert str(_RD_PORT_ID) in vrf_identities
        suggestions = _Assessment(None, "ifName")._load_vrf_suggestions(vrf_identities, [first, second])

        assert str(_RD_PORT_ID) not in suggestions

    def test_ambiguous_name_does_not_suggest_a_vrf(self, live_librenms):
        """A name claimed by two NetBox VRFs binds neither one."""
        first_tenant = Tenant.objects.create(name="First VRF tenant", slug="first-vrf-tenant")
        second_tenant = Tenant.objects.create(name="Second VRF tenant", slug="second-vrf-tenant")
        VRF.objects.create(name=_NAME_VRF_NAME, tenant=first_tenant)
        VRF.objects.create(name=_NAME_VRF_NAME, tenant=second_tenant)
        device = _acquired(live_librenms, "iosxe-subinterfaces")

        tagged = _row_on(_assess(device).rows, _NAME_PORT_ID)

        assert tagged.get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in tagged

    def test_stale_port_id_does_not_suggest_a_vrf(self, live_librenms):
        """An IP row cannot inherit VRF evidence from an unrelated port."""
        VRF.objects.create(name="NetBox RD target", rd=_RD_VRF_RD)
        device = _acquired(live_librenms, "iosxe-subinterfaces", addresses=[{**_RD_ADDRESS, "port_id": 999999}])

        (row,) = _assess(device).rows

        assert row.get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in row

    @pytest.mark.parametrize("if_vrf", [None, 0, "0", "not-a-number", True, "missing"])
    def test_unusable_if_vrf_does_not_suggest(self, live_librenms, if_vrf):
        """Missing, null, zero, boolean and non-numeric port values are not VRF tags."""
        VRF.objects.create(name="NetBox RD target", rd=_RD_VRF_RD)
        port = deepcopy(_RD_PORT)
        if if_vrf == "missing":
            port.pop("ifVrf")
        else:
            port["ifVrf"] = if_vrf
        device = _acquired(live_librenms, "iosxe-subinterfaces", ports=[port], addresses=[_RD_ADDRESS])

        rows = _assess(device).rows

        assert all(row.get("suggested_vrf_id") is None for row in rows)
        assert all("vrf_suggested_from" not in row for row in rows)
        # Acquisition reads the VRFs by the same port rule, so an unusable tag reads no VRFs.
        assert _vrf_requests(live_librenms) == []

    def test_existing_ip_vrf_is_kept_without_a_suggestion_mark(self, live_librenms):
        """Source evidence never replaces an existing NetBox IP VRF."""
        VRF.objects.create(name="Suggested target", rd=_RD_VRF_RD)
        existing_vrf = VRF.objects.create(name="Existing assignment")
        IPAddress.objects.create(address=_RD_CIDR, vrf=existing_vrf, status="active")
        device = _acquired(live_librenms, "iosxe-subinterfaces")

        existing = next(row for row in _assess(device).rows if row["ip_address"] == _RD_ADDRESS["ipv4_address"])

        assert existing["vrf_id"] == existing_vrf.pk
        assert "vrf_suggested_from" not in existing

    def test_an_existing_global_address_gets_no_suggestion(self, live_librenms):
        """An address NetBox already holds globally is not a candidate: the row reads "Synced"."""
        VRF.objects.create(name="Suggested target", rd=_RD_VRF_RD)
        # No VRF: the row's vrf_id stays None, which must not be read as "free to suggest into".
        IPAddress.objects.create(address=_RD_CIDR, status="active")
        device = _acquired(live_librenms, "iosxe-subinterfaces")

        existing = next(row for row in _assess(device).rows if row["ip_address"] == _RD_ADDRESS["ipv4_address"])

        assert existing["exists"] is True
        assert existing.get("suggested_vrf_id") is None
        assert "vrf_suggested_from" not in existing

    def test_string_typed_librenms_ids_still_suggest(self, live_librenms):
        """LibreNMS documents string ids for /routing/vrf; the join must not silently stop."""
        target = VRF.objects.create(name="NetBox RD target", rd=_RD_VRF_RD)
        vrfs = deepcopy(_IOSXE_VRFS)
        for vrf in vrfs:
            vrf["vrf_id"] = str(vrf["vrf_id"])
            vrf["device_id"] = str(vrf["device_id"])
        device = _acquired(live_librenms, "iosxe-subinterfaces", vrfs=vrfs)

        tagged = _row_on(_assess(device).rows, _RD_PORT_ID)

        assert tagged["suggested_vrf_id"] == target.pk


@pytest.mark.django_db
def test_port_disclosure_preload_binds_each_stored_form_with_one_regex_per_path():
    """Many ports cost one regex per JSON path, and each stored text form still binds only its own port."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from netbox_librenms_plugin.utils import PortDisclosure

    device = make_device("disclosure-preload-device")
    stored = {
        "scalar": {SERVER_KEY: 7101},
        "dict": {SERVER_KEY: {"id": "007102"}},
        "legacy": " +7103 ",
        "longer": {SERVER_KEY: 71010},
    }
    for name, value in stored.items():
        seed_stored_mapping(make_interface(device, name), value, save=True)
    bound = [7101, 7102, 7103]
    disclose = PortDisclosure(make_superuser("disclosure-preload-user"), SERVER_KEY)

    with CaptureQueriesContext(connection) as queries:
        disclose.preload([(port_id, None) for port_id in [*bound, 710, *range(9000, 9040)]])

    binding_sql = [query["sql"] for query in queries if 'FROM "dcim_interface"' in query["sql"]]
    assert [sql.count(" ~ ") for sql in binding_sql] == [4]
    assert [port_id for port_id in [*bound, 710, 9000] if disclose(port_id, None)] == bound


@pytest.mark.parametrize(
    "records, expected",
    [
        ({"0042": {"port_id": 42}}, {42: {"port_id": 42}}),
        ({42: {"port_id": 42}, "0042": {"port_id": 42}}, {42: None}),
        ({42: None, "0042": {"port_id": 42}}, {42: {"port_id": 42}}),
        ({"bad": {"port_id": 42}, "0": {"port_id": 0}}, {}),
    ],
)
def test_ip_port_index_preserves_the_canonical_ambiguity_rule(records, expected):
    from netbox_librenms_plugin.utils import index_ip_port_records, ip_row_port_record

    index = index_ip_port_records(records)

    assert index == expected
    assert ip_row_port_record(index, "+42") == expected.get(42)


@pytest.mark.django_db
def test_ip_tab_get_keeps_an_unbound_interface_and_the_source_unchanged(client, live_librenms):
    from core.models import ObjectChange
    from dcim.models import Interface
    from django.core.cache import cache

    device = seed_mapping(make_device("ip-get-no-write"), SERVER_KEY, own=DEVICE_ID)
    interface = make_interface(device, "Ethernet0")
    _serve(live_librenms, 1)
    snapshot = acquire(device, server_key=SERVER_KEY, resources=[Resource.IP_ADDRESSES])
    assert source_cache.store(device, Resource.IP_ADDRESSES, snapshot)
    before = (
        Interface.objects.get(pk=interface.pk).custom_field_data,
        Interface.objects.get(pk=interface.pk).last_updated,
        ObjectChange.objects.count(),
        cache.get(source_cache.snapshot_key(device, Resource.IP_ADDRESSES, SERVER_KEY)),
    )
    client.force_login(make_superuser("ip-get-no-write-user"))

    requests = list(live_librenms.server.requests)
    response = client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": device.pk, "tab": "ipaddresses"},
        ),
        {"server_key": SERVER_KEY},
    )

    assert response.status_code == 200
    assert len(list(response.context["ip_sync"]["table"].data)) == 1
    assert live_librenms.server.requests == requests
    interface.refresh_from_db()
    assert (
        interface.custom_field_data,
        interface.last_updated,
        ObjectChange.objects.count(),
        cache.get(source_cache.snapshot_key(device, Resource.IP_ADDRESSES, SERVER_KEY)),
    ) == before
    assert live_librenms.server.requests[-1]["path"] != f"/api/v0/ports/{9000}"
