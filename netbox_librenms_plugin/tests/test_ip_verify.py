"""
The IP row read and the VRF choice command, which replaced the single IP verify.

Covers:
- the row read answers the row of the snapshot of its URL's server, so a non-default server reads
  its own source rows.
- the choice command validates the posted VRF before it reads NetBox.
- the row key names one row and never guesses among equal addresses.
"""

import pytest
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_superuser
from netbox_librenms_plugin.tests.source_snapshot_helpers import ip_address_row, seed_ip_snapshot
from netbox_librenms_plugin.tests.view_test_helpers import ip_tab, read_ip_row

pytestmark = pytest.mark.django_db


def _row_url(device, row_key, server_key):
    path = reverse(
        "plugins:netbox_librenms_plugin:ip_row",
        kwargs={"object_type": "device", "pk": device.pk, "row_key": row_key},
    )
    return f"{path}?server_key={server_key}"


class TestTheRowReadReadsTheSnapshotOfItsServer:
    """The row read reads the snapshot of the URL's server; a row of another server's snapshot is not found."""

    def test_each_server_answers_only_its_own_rows(self, client, configure_librenms):
        configure_librenms(
            {
                "default": {"librenms_url": "https://default.example.test", "api_token": "test-token"},
                "production": {"librenms_url": "https://production.example.test", "api_token": "test-token"},
            }
        )
        client.force_login(make_superuser())
        device = make_device("ip-verify-namespace", librenms_cf={"default": 40, "production": 41})
        address = "198.18.40.1"
        # The address is on two ports in each snapshot, so only the snapshot that holds the row key answers.
        for namespace, first_port in (("default", 10), ("production", 20)):
            port_ids = (first_port, first_port + 1)
            seed_ip_snapshot(
                device,
                [ip_address_row(address, port_id, prefix_length=32) for port_id in port_ids],
                [{"port_id": port_id, "ifName": f"eth{port_id}", "ifDescr": f"eth{port_id}"} for port_id in port_ids],
                server_key=namespace,
            )

        for server_key, own, other in (("default", 10, 20), ("production", 20, 10)):
            own_response = client.get(_row_url(device, f"{address}/32@{own}", server_key))
            assert own_response.status_code == 200, own_response.content
            assert f'data-row-key="{address}/32@{own}"' in own_response.content.decode()
            assert client.get(_row_url(device, f"{address}/32@{other}", server_key)).status_code == 404


class TestTheChoiceRejectsAMalformedVrf:
    """A non-numeric VRF must answer 400 before the choice reads NetBox, also when NetBox holds the address."""

    @pytest.fixture
    def device(self, client):
        from ipam.models import IPAddress

        client.force_login(make_superuser())
        device = make_device("vrfsk-dev", librenms_cf={"default": 50})
        # NetBox holds the address, so a VRF decision of the row would read it.
        IPAddress.objects.get_or_create(address="10.0.0.1/24")
        seed_ip_snapshot(device, [ip_address_row("10.0.0.1", 1)], [{"port_id": 1, "ifName": "eth1"}])
        return device

    @staticmethod
    def _choose(client, device, value):
        table = ip_tab(client, device).table
        return client.post(
            table["data-choice-url"],
            {
                "draft_id": table["data-draft-id"],
                "expected_revision": table["data-draft-revision"],
                "row": "10.0.0.1/24",
                "value": value,
            },
        )

    @pytest.mark.parametrize("value", ["abc", "2.5", "True"])
    def test_a_malformed_vrf_answers_400(self, client, device, value):
        assert self._choose(client, device, value).status_code == 400

    def test_a_numeric_string_vrf_is_accepted(self, client, device):
        from ipam.models import VRF

        assert self._choose(client, device, str(VRF.objects.create(name="vrfsk-numeric").pk)).status_code == 204


class TestTheRowKeyNamesOneRow:
    """A row key names one assessed row: an address on two ports needs its port, and a key of another address is a miss."""

    @pytest.fixture
    def setup(self, client):
        from ipam.models import VRF, IPAddress

        client.force_login(make_superuser())
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
        return device, vrf

    def test_a_unique_address_shows_its_netbox_vrf_and_source_port(self, client, setup):
        device, vrf = setup

        row = read_ip_row(client, device, "10.0.0.1/32")

        assert row.shown_vrf == str(vrf.pk)
        assert ">eth5<" in row.html

    def test_an_address_on_two_ports_needs_the_port(self, client, setup):
        device, _vrf = setup

        assert client.get(_row_url(device, "10.0.0.2/32", "default")).status_code == 404
        row = client.get(_row_url(device, "10.0.0.2/32@7", "default"))
        assert row.status_code == 200
        assert 'data-row-key="10.0.0.2/32@7"' in row.content.decode()
        assert ">eth7<" in row.content.decode()

    def test_a_row_key_of_another_address_is_a_miss(self, client, setup):
        device, _vrf = setup

        assert client.get(_row_url(device, "10.0.0.1/32@7", "default")).status_code == 404
