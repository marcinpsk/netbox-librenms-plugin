"""Matching asks LibreNMS for the device of an object, in lookup order, and tells "no device" from "no answer"."""

import json
import time

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


def _take_all_probe_slots(timeout):
    """Take every probe slot within *timeout* seconds; return the number taken. The caller gives them back."""
    from netbox_librenms_plugin import librenms_api

    end = time.monotonic() + timeout
    taken = 0
    while taken < librenms_api.MATCH_PROBE_WORKERS and librenms_api._match_probe_slots.acquire(
        timeout=max(0, end - time.monotonic())
    ):
        taken += 1
    return taken


def _give_back_probe_slots(count):
    from netbox_librenms_plugin import librenms_api

    for _ in range(count):
        librenms_api._match_probe_slots.release()


def _wait_for_free_probe_slots(timeout=10):
    """Return whether every probe slot is free within *timeout* seconds: every probe worker has ended."""
    from netbox_librenms_plugin import librenms_api

    taken = _take_all_probe_slots(timeout)
    _give_back_probe_slots(taken)
    return taken == librenms_api.MATCH_PROBE_WORKERS


@pytest.fixture(autouse=True)
def probe_workers_end(librenms_server):
    """After each test, end the answers still on the way and require every probe worker to have ended."""
    yield
    librenms_server.release_streams()
    assert _wait_for_free_probe_slots(), "a probe worker did not end after its answer stopped"


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

    def test_a_match_that_arrives_after_the_deadline_is_not_accepted(self, api, librenms_server, monkeypatch):
        now = [500.0]
        monkeypatch.setattr(matching, "_clock", lambda: now[0])
        deadline = matching.Deadline(15)

        def late_match(**request):
            now[0] += 16
            return 200, _found(55)

        librenms_server.register(IP_PATH, late_match)

        assert isinstance(matching.find_librenms_match(_device(), api, deadline=deadline), NotChecked)

    def test_a_body_that_dribbles_in_ends_near_the_deadline_on_the_real_clock(self, api, librenms_server):
        """Each part arrives inside a socket timeout, so only the deadline stops the read."""
        body = json.dumps(_found(55)).encode()
        # A kept-alive connection, as a LibreNMS behind an HTTP/1.1 web server answers.
        librenms_server.register_streamed(
            IP_PATH, [body[:5], body[5:10], body[10:15], body[15:]], pause=0.8, headers={"Connection": "keep-alive"}
        )
        device = _device()
        started = time.perf_counter()

        result = matching.find_librenms_match(device, api, deadline=matching.Deadline(1))

        elapsed = time.perf_counter() - started
        assert isinstance(result, NotChecked)
        assert elapsed < 1.5
        assert _paths(librenms_server) == [IP_PATH]


def _dribbled(head, body=b""):
    """Return the parts of a raw response whose status line, headers and body arrive a few bytes at a time."""
    raw = head + body
    return [raw[index : index + 8] for index in range(0, len(raw), 8)]


_BODY = json.dumps(_found(55)).encode()


def _head(status_line, *headers):
    return ("\r\n".join([status_line, *headers]) + "\r\n\r\n").encode()


_SLOW_ANSWERS = {
    # The status line and the headers dribble in, so the request itself does not return.
    "slow-headers": _dribbled(
        _head("HTTP/1.1 200 OK", "Content-Type: application/json", f"Content-Length: {len(_BODY)}"), _BODY
    ),
    # A redirect whose body requests reads before it returns, although it follows no redirect.
    "redirect-with-slow-body": [
        _head("HTTP/1.1 302 Found", "Location: /api/v0/devices/elsewhere", f"Content-Length: {len(_BODY)}"),
        *_dribbled(b"", _BODY),
    ],
    # A body that only the end of the connection delimits.
    "connection-close-body": [
        _head("HTTP/1.1 200 OK", "Content-Type: application/json", "Connection: close"),
        *_dribbled(b"", _BODY),
    ],
}


@pytest.mark.django_db
class TestTheDeadlineBoundsTheWholeRequest:
    """Each part arrives well inside a socket timeout; only the deadline may end the wait (1 s here)."""

    @pytest.mark.parametrize("answer", list(_SLOW_ANSWERS))
    def test_a_slow_answer_is_not_checked_within_the_deadline(self, api, librenms_server, answer):
        librenms_server.register_raw_stream(IP_PATH, _SLOW_ANSWERS[answer], pause=0.3)
        device = _device()
        started = time.perf_counter()

        result = matching.find_librenms_match(device, api, deadline=matching.Deadline(1))

        elapsed = time.perf_counter() - started
        assert isinstance(result, NotChecked)
        assert elapsed < 1.5
        # The probe reached the server, so the answer path named by the case ran.
        assert _paths(librenms_server) == [IP_PATH]

    def test_a_connection_that_is_never_accepted_is_not_checked_within_the_deadline(self, api):
        """A full accept queue drops the connection request, so the connect itself waits."""
        import socket

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(0)
            host, port = listener.getsockname()
            # One waiting connection fills a queue of length zero.
            fillers = [socket.create_connection((host, port), timeout=1)]
            try:
                api.librenms_url = f"http://{host}:{port}"
                device = _device()
                started = time.perf_counter()

                result = matching.find_librenms_match(device, api, deadline=matching.Deadline(1))

                assert isinstance(result, NotChecked)
                assert time.perf_counter() - started < 1.5
            finally:
                for filler in fillers:
                    filler.close()

    def test_a_probe_finds_every_slot_taken_and_is_not_checked_at_once_then_runs_when_they_free(
        self, api, librenms_server
    ):
        """Slow answers hold every probe slot; a further probe waits for none, and runs again once they end."""
        import threading

        from netbox_librenms_plugin.librenms_api import MATCH_PROBE_WORKERS
        from netbox_librenms_plugin.matching import LookupInputs

        librenms_server.register_raw_stream(IP_PATH, _SLOW_ANSWERS["slow-headers"], pause=0.3)
        slow = _device()
        LookupInputs.of(slow)
        fast = make_device("match-fast")
        give_primary_ip(fast, "192.0.2.9/24")
        fast = type(fast).objects.get(pk=fast.pk)
        LookupInputs.of(fast)
        librenms_server.register("/api/v0/devices/192.0.2.9", _found(56))
        busy = [
            threading.Thread(
                target=matching.find_librenms_match, args=(slow, api), kwargs={"deadline": matching.Deadline(3)}
            )
            for _ in range(MATCH_PROBE_WORKERS)
        ]
        for thread in busy:
            thread.start()
        waited = time.perf_counter()
        while len(_paths(librenms_server)) < MATCH_PROBE_WORKERS and time.perf_counter() - waited < 2:
            time.sleep(0.01)
        assert len(_paths(librenms_server)) == MATCH_PROBE_WORKERS
        assert _take_all_probe_slots(0) == 0
        started = time.perf_counter()

        result = matching.find_librenms_match(fast, api, deadline=matching.Deadline(1))

        elapsed = time.perf_counter() - started
        assert isinstance(result, NotChecked)
        assert elapsed < 0.3
        assert "/api/v0/devices/192.0.2.9" not in _paths(librenms_server)
        librenms_server.release_streams()
        for thread in busy:
            thread.join(timeout=10)
        assert _wait_for_free_probe_slots()
        assert isinstance(matching.find_librenms_match(fast, api, deadline=matching.Deadline(1)), Match)

    def test_a_missing_ca_bundle_is_unreachable(self, api):
        """Requests refuses a missing CA bundle with a plain OSError before it connects; that is no server answer."""
        api.librenms_url = "https://127.0.0.1:9"
        api.verify_ssl = "/nonexistent/librenms-ca.pem"

        assert isinstance(matching.find_librenms_match(_device(), api, deadline=matching.Deadline(1)), Unreachable)


_EXIT_CHILD = """
import socket
import sys
import threading
import time
from types import SimpleNamespace

import django

django.setup()
from netbox_librenms_plugin.librenms_api import LibreNMSAPI

listener = socket.create_server(("127.0.0.1", 0))


def never_finish():
    connection, _address = listener.accept()
    connection.recv(4096)
    while True:
        connection.sendall(b"H")
        time.sleep(0.2)


threading.Thread(target=never_finish, daemon=True).start()
client = SimpleNamespace(
    librenms_url=f"http://127.0.0.1:{listener.getsockname()[1]}", headers={}, verify_ssl=False
)
end = time.monotonic() + 1
answer = LibreNMSAPI.get_response_before(client, "devices/stalled", lambda: end - time.monotonic())
print("caller returned", answer, flush=True)
"""


def test_a_probe_that_never_ends_does_not_hold_the_process_open():
    """The caller gave up on the probe; the interpreter must still exit while the server keeps sending."""
    import os
    import subprocess
    import sys

    child = subprocess.Popen(
        [sys.executable, "-c", _EXIT_CHILD],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    try:
        # Django setup takes its own time; the exit is measured from the moment the caller gave up.
        returned = child.stdout.readline()
        child.wait(timeout=5)
    finally:
        child.kill()
        child.communicate()

    assert returned.strip() == "caller returned None"
    assert child.returncode == 0


@pytest.mark.django_db
def test_a_redirect_is_unreachable_and_is_not_followed(api, librenms_server):
    device = _device()
    librenms_server.register_redirect(IP_PATH, "/api/v0/devices/redirected")
    librenms_server.register("/api/v0/devices/redirected", _found(55))

    result = matching.find_librenms_match(device, api)

    assert isinstance(result, Unreachable)
    assert _paths(librenms_server) == [IP_PATH]
