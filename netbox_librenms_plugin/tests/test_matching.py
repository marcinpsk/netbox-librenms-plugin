"""Matching asks LibreNMS for the device of an object, in lookup order, and tells "no device" from "no answer"."""

import pytest

from netbox_librenms_plugin import matching
from netbox_librenms_plugin.matching import Candidate, Match, NoMatch, NotChecked, ProbeResult, Unreachable
from netbox_librenms_plugin.tests.conftest import bind_librenms_server, make_device
from netbox_librenms_plugin.tests.link_intent_helpers import give_primary_ip
from netbox_librenms_plugin.tests.view_test_helpers import domain_state

IP_PATH = "/api/v0/devices/192.0.2.1"
DNS_PATH = "/api/v0/devices/match-dns.example.net"


@pytest.fixture
def api(settings, librenms_server):
    return bind_librenms_server(settings, librenms_server, server_key="default")


def _device(name="match-device", *, dns_name=""):
    device = make_device(name)
    ip = give_primary_ip(device, "192.0.2.1/24")
    if dns_name:
        ip.dns_name = dns_name
        ip.save()
    return type(device).objects.get(pk=device.pk)


def _found(device_id, hostname="matched"):
    return {"status": "ok", "devices": [{"device_id": device_id, "hostname": hostname, "sysName": "sys"}]}


def _paths(server):
    return [request["path"] for request in server.requests]


@pytest.mark.django_db
class TestLookupOrder:
    def test_the_primary_ip_answers_first_and_ends_the_lookup(self, api, librenms_server):
        device = _device(dns_name="match-dns.example.net")
        librenms_server.register(IP_PATH, _found(55))
        before = domain_state()

        result = matching.find_librenms_match(device, api)

        assert isinstance(result, Match)
        assert (result.librenms_id, result.candidate, result.hostname, result.sysname) == (
            55,
            Candidate.PRIMARY_IP,
            "matched",
            "sys",
        )
        assert result.probes == (matching.Probe(Candidate.PRIMARY_IP, "192.0.2.1", ProbeResult.MATCH),)
        assert _paths(librenms_server) == [IP_PATH]
        assert domain_state() == before

    @pytest.mark.parametrize("no_match", [(200, {"status": "ok", "devices": []}), (404, {"status": "error"})])
    def test_a_404_or_an_empty_list_moves_on_to_the_next_candidate(self, api, librenms_server, no_match):
        device = _device(dns_name="match-dns.example.net")
        status, body = no_match
        librenms_server.register(IP_PATH, body, status=status)
        librenms_server.register(DNS_PATH, {"status": "error"}, status=404)
        librenms_server.register(f"/api/v0/devices/{device.name}", _found("77"))

        result = matching.find_librenms_match(device, api)

        assert isinstance(result, Match)
        assert (result.librenms_id, result.candidate) == (77, Candidate.NAME)
        assert [probe.result for probe in result.probes] == [
            ProbeResult.NO_MATCH,
            ProbeResult.NO_MATCH,
            ProbeResult.MATCH,
        ]
        assert _paths(librenms_server) == [IP_PATH, DNS_PATH, f"/api/v0/devices/{device.name}"]

    def test_an_object_without_a_primary_ip_is_looked_up_by_name_only(self, api, librenms_server):
        device = make_device("match-name-only")

        result = matching.find_librenms_match(device, api)

        assert isinstance(result, NoMatch)
        assert result.probes == (matching.Probe(Candidate.NAME, "match-name-only", ProbeResult.NO_MATCH),)


@pytest.mark.django_db
class TestNoAnswer:
    @pytest.mark.parametrize(
        "answer",
        [
            (500, {"message": "Server Error"}),
            (401, {"status": "error"}),
            (200, {"status": "ok", "devices": [{"device_id": "not-an-id"}]}),
            (200, {"status": "ok"}),
        ],
        ids=["server-error", "unauthorized", "malformed-id", "no-devices-key"],
    )
    def test_an_unusable_answer_is_unreachable_and_stops_the_lookup(self, api, librenms_server, answer):
        device = _device()
        status, body = answer
        librenms_server.register(IP_PATH, body, status=status)

        result = matching.find_librenms_match(device, api)

        assert isinstance(result, Unreachable)
        assert _paths(librenms_server) == [IP_PATH]

    def test_a_dropped_connection_is_unreachable(self, api, librenms_server):
        device = _device()
        librenms_server.register_disconnect(IP_PATH)

        assert isinstance(matching.find_librenms_match(device, api), Unreachable)


@pytest.mark.django_db
class TestDeadline:
    def test_no_probe_is_sent_after_the_deadline(self, api, librenms_server, monkeypatch):
        now = [500.0]
        monkeypatch.setattr(matching, "_clock", lambda: now[0])
        deadline = matching.Deadline(15)
        now[0] += 15

        result = matching.find_librenms_match(_device(), api, deadline=deadline)

        assert isinstance(result, NotChecked)
        assert librenms_server.requests == []

    def test_a_probe_that_outlasts_the_deadline_is_not_checked_rather_than_unreachable(
        self, api, librenms_server, monkeypatch
    ):
        now = [500.0]
        monkeypatch.setattr(matching, "_clock", lambda: now[0])
        deadline = matching.Deadline(15)

        def outlast(**request):
            now[0] += 16
            return 500, {"message": "late"}

        librenms_server.register(IP_PATH, outlast)

        assert isinstance(matching.find_librenms_match(_device(), api, deadline=deadline), NotChecked)
