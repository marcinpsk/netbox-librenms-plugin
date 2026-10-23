"""
Regression tests for SingleIPAddressVerifyView.post().

Covers:
- server_key from POST body selects the stored IP snapshot of that server, so non-default
  servers verify against their own source rows.
- request validation and the row lookup over the assessed rows.
"""

import json

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
from netbox_librenms_plugin.tests.source_snapshot_helpers import ip_address_row, seed_ip_snapshot


pytestmark = pytest.mark.django_db


def _make_view(request=None):
    """Create a SingleIPAddressVerifyView bound to a real request and user."""
    from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

    view = SingleIPAddressVerifyView()
    view.setup(request if request is not None else _make_request({}))
    return view


def _make_request(body_dict):
    """Create a JSON POST request with a real permitted user."""
    from django.test import RequestFactory

    request = RequestFactory().post("/", data=json.dumps(body_dict), content_type="application/json")
    request.user = make_superuser()
    return request


class TestVerifyPostRejectsNonObjectBody:
    """A non-object JSON body must 400, not 500 on .get()."""

    def test_non_dict_json_returns_400(self):
        """A JSON array body returns 400 before any .get(), instead of raising AttributeError."""
        view = _make_view()
        request = _make_request([1, 2, 3])  # valid JSON, but an array — not an object

        response = view.post(request)

        assert response.status_code == 400
        assert json.loads(response.content)["message"] == "JSON payload must be an object"


class TestVerifyPostServerCacheNamespace:
    """The endpoint reads the selected configured namespace or the default namespace."""

    @pytest.mark.parametrize(
        ("server_key", "include_server_key", "expected_port"),
        [("production", True, 20), (None, True, 10), (None, False, 10)],
    )
    def test_configured_missing_and_null_server_keys_select_the_expected_cache(
        self,
        configure_librenms,
        server_key,
        include_server_key,
        expected_port,
    ):
        configure_librenms(
            {
                "default": {
                    "librenms_url": "https://default.example.test",
                    "api_token": "test-token",
                },
                "production": {
                    "librenms_url": "https://production.example.test",
                    "api_token": "test-token",
                },
            }
        )
        device = make_device(
            f"ip-verify-namespace-{expected_port}-{include_server_key}",
            librenms_cf={"default": 40, "production": 41},
        )
        address = "198.18.40.1"
        # The address is on two ports in each snapshot, so only the snapshot that holds the posted row id answers.
        for namespace, first_port in (("default", 10), ("production", 20)):
            port_ids = (first_port, first_port + 1)
            seed_ip_snapshot(
                device,
                [ip_address_row(address, port_id, prefix_length=32) for port_id in port_ids],
                [{"port_id": port_id, "ifName": f"eth{port_id}", "ifDescr": f"eth{port_id}"} for port_id in port_ids],
                server_key=namespace,
            )

        body = {
            "device_id": device.pk,
            "object_type": "device",
            "ip_address": f"{address}/32",
            "row_id": f"{address}/32@{expected_port}",
        }
        if include_server_key:
            body["server_key"] = server_key
        request = _make_request(body)

        response = _make_view(request).post(request)

        assert response.status_code == 200, response.content
        assert json.loads(response.content)["status"] == "success"


@pytest.mark.django_db
class TestVerifyPostRejectsMalformedVrfId:
    """
    A non-numeric vrf_id must 400 before the VRF filter, not 500 via the broad handler.

    The `vrf__id` filter in `_find_existing_ip` is only reached when an IPAddress at the posted address
    already exists, so the real IP is created first — otherwise the guard is never exercised and the
    pre-fix code wouldn't 500 either. The snapshot is empty; `_find_existing_ip` runs for real.
    """

    def _post(self, vrf_id):
        from ipam.models import IPAddress

        device = make_device("vrfsk-dev", librenms_cf={"default": 50})
        IPAddress.objects.get_or_create(address="10.0.0.1/24")  # make the vrf__id filter reachable
        seed_ip_snapshot(device, [])

        request = _make_request(
            {"ip_address": "10.0.0.1/24", "vrf_id": vrf_id, "device_id": device.pk, "object_type": "device"}
        )
        return _make_view(request).post(request)

    def test_non_numeric_vrf_id_returns_400(self):
        response = self._post("abc")
        assert response.status_code == 400
        assert json.loads(response.content)["message"] == "Invalid VRF ID"

    def test_list_vrf_id_returns_400(self):
        assert self._post([1, 2]).status_code == 400

    def test_boolean_vrf_id_returns_400(self):
        # bool is an int subclass; a JSON `true` must not silently coerce to vrf__id=1.
        assert self._post(True).status_code == 400

    def test_numeric_string_vrf_id_is_accepted(self):
        # A digit string coerces to int and flows through the real filter without 400/500.
        assert self._post("999999").status_code == 200


class TestFindRowResolvesAssessedRows:
    """SingleIPAddressVerifyView._find_row resolves one row of a real assessment and never guesses among equal addresses."""

    @staticmethod
    def _rows():
        from ipam.models import VRF, IPAddress

        from netbox_librenms_plugin import source_cache
        from netbox_librenms_plugin.acquisition.registry import Resource
        from netbox_librenms_plugin.tests.conftest import make_interface
        from netbox_librenms_plugin.views.base.ip_addresses_view import assess_ip_snapshot

        device = make_device("ip-verify-find-row", librenms_cf={"default": 60})
        vrf = VRF.objects.create(name="find-row-vrf")
        IPAddress.objects.create(address="10.0.0.1/32", vrf=vrf, assigned_object=make_interface(device, "eth5"))
        seed_ip_snapshot(
            device,
            [
                ip_address_row("10.0.0.1", 5, prefix_length=32),
                ip_address_row("10.0.0.2", 6, prefix_length=32),
                ip_address_row("10.0.0.2", 7, prefix_length=32),
            ],
            [{"port_id": port_id, "ifName": f"eth{port_id}", "ifDescr": f"eth{port_id}"} for port_id in (5, 6, 7)],
        )
        request = _make_request({})
        snapshot = source_cache.read(device, Resource.IP_ADDRESSES, "default")
        return assess_ip_snapshot(request, device, snapshot, "default").rows, vrf

    def test_a_unique_address_returns_its_netbox_vrf_and_source_port(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        rows, vrf = self._rows()

        row, vrf_id, port_id = SingleIPAddressVerifyView._find_row(rows, "10.0.0.1", 32)

        assert (row["ip_with_mask"], vrf_id, port_id) == ("10.0.0.1/32", vrf.pk, 5)

    def test_an_address_on_two_ports_needs_the_row_id(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        rows, _vrf = self._rows()

        assert SingleIPAddressVerifyView._find_row(rows, "10.0.0.2", 32) == (None, None, None)
        row, _vrf_id, port_id = SingleIPAddressVerifyView._find_row(rows, "10.0.0.2", 32, "10.0.0.2/32@7")
        assert (row["row_id"], port_id) == ("10.0.0.2/32@7", 7)

    def test_a_row_id_of_another_address_is_a_miss(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        rows, _vrf = self._rows()

        assert SingleIPAddressVerifyView._find_row(rows, "10.0.0.1", 32, "10.0.0.2/32@7") == (None, None, None)


class TestNumericIDValidation:
    """post() must reject a non-numeric device_id/vrf_id with a clean 400 rather than let the value reach the ORM and surface as a generic 500."""

    def test_non_numeric_object_id_returns_400(self):
        view = _make_view()
        request = _make_request({"device_id": "abc", "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["status"] == "error"
        # Assert the specific message so the test can't pass on an unrelated 400 branch.
        assert payload["message"] == "Invalid object ID"

    def test_non_numeric_vrf_id_returns_400(self):
        view = _make_view()
        request = _make_request({"device_id": 5, "vrf_id": "xyz", "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["status"] == "error"
        assert payload["message"] == "Invalid VRF ID"

    def test_boolean_false_object_id_rejected_as_invalid(self):
        # bool is an int subclass; object_id=False must hit the explicit boolean guard
        # ("Invalid object ID"), not the falsy "No object ID provided" branch. The guard
        # therefore has to run before `if not object_id`.
        view = _make_view()
        request = _make_request({"device_id": False, "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["message"] == "Invalid object ID"

    def test_boolean_true_object_id_rejected_as_invalid(self):
        # object_id=True would otherwise int() to 1 and validate as device #1.
        view = _make_view()
        request = _make_request({"device_id": True, "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["message"] == "Invalid object ID"

    def test_boolean_vrf_id_rejected_as_invalid(self):
        # bool is an int subclass; vrf_id=True would otherwise int() to 1 and validate as VRF #1.
        # The boolean guard must reject it ("Invalid VRF ID"), mirroring the object_id guards —
        # so true/false can't silently regress to 1/0.
        view = _make_view()
        request = _make_request({"device_id": 5, "vrf_id": True, "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["status"] == "error"
        assert payload["message"] == "Invalid VRF ID"

    def test_float_object_id_rejected_as_invalid(self):
        # A JSON float device_id=1.9 would otherwise int()-truncate to 1 and bind device #1.
        # The explicit float guard must reject it with a clean 400 instead.
        view = _make_view()
        request = _make_request({"device_id": 1.9, "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["message"] == "Invalid object ID"

    def test_float_vrf_id_rejected_as_invalid(self):
        # vrf_id=2.5 would otherwise int()-truncate to 2 and validate as VRF #2.
        view = _make_view()
        request = _make_request({"device_id": 5, "vrf_id": 2.5, "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["status"] == "error"
        assert payload["message"] == "Invalid VRF ID"
