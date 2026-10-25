"""
A cable write goes ahead only when no LibreNMS port it touches is ignored.

The touched ports are the row's local and remote ports, the port bound to each endpoint the write
attaches (a manual pick too), and the port bound to each far end of a cable it removes. Each is
decided with the binding and the owner platform read under the lock. Only Ignore blocks a cable,
and a port with no record blocks it only when an Ignore rule could apply. The tests drive real
requests against real NetBox objects and rules; LibreNMS is a loopback server.
"""

import json
import re
from html import unescape

import pytest
from dcim.models import Cable
from django.contrib.messages import get_messages
from django.urls import reverse

from netbox_librenms_plugin.interface_rules import HIDDEN_PORT_REASON
from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import cable_rows, seed_cables_snapshot, link_row, port_row
from netbox_librenms_plugin.assessment.cables import assess as assess_cables
from netbox_librenms_plugin.tests.conftest import (
    bind_librenms_server,
    cable_together,
    make_device,
    make_interface,
    make_superuser,
    map_device_to_librenms,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.test_cable_overwrite import _confirmed_intent
from netbox_librenms_plugin.tests.test_interface_rule_writes import _platform, _port
from netbox_librenms_plugin.assessment.cables import port_record
from netbox_librenms_plugin.views.sync.cables import SyncCablesView

SERVER_KEY = "default"
IGNORE = InterfaceTypeMapping.ACTION_IGNORE


def _bound(device, name, port_id):
    interface = make_interface(device, name, iface_type="10gbase-x-sfpp")
    seed_mapping(interface, SERVER_KEY, own=port_id, save=False)
    interface.save()
    return interface


def _row(local, remote_device, **overrides):
    """Return the raw LLDP report between the two mapped source devices."""
    return {
        **link_row(
            100,
            "Gi0/1",
            local_port=local.name,
            remote_hostname=remote_device.name,
            remote_device_id=9,
            remote_port_id=500,
            id=1,
        ),
        **overrides,
    }


def _seed(device, source_link, *, ports=None, neighbour_ports=None, extra_links=()):
    """Store explicit raw port facts for the two-device cable fixture."""
    snapshot = seed_cables_snapshot(
        device,
        [source_link, *extra_links],
        [port_row(100, "Te1/1", device_id=8)] if ports is None else ports,
        neighbour_ports={9: [port_row(500, "Gi0/1", device_id=9)]} if neighbour_ports is None else neighbour_ports,
        server_key=SERVER_KEY,
    )
    return assess_cables(snapshot, subject=device)["links"][0]["row_id"]


def _render(client, device):
    """Render the cable tab: the one row's record and its HTML."""
    response = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"tab": "cables", "server_key": SERVER_KEY},
    )
    assert response.status_code == 200
    table = response.context["cable_sync"]["table"]
    row = next(row for row in table.rows if row.record["row_id"] == "100")
    return row.record, unescape(table.as_html(response.wsgi_request))


def _sync_data(record, **extra):
    row_id = record["row_id"]
    return {
        "select": row_id,
        "server_key": SERVER_KEY,
        f"expected_local_id_{row_id}": record["netbox_local_interface_id"],
        f"expected_local_device_id_{row_id}": record["netbox_local_device_id"],
        f"expected_remote_id_{row_id}": record["netbox_remote_interface_id"],
        f"expected_remote_device_id_{row_id}": record["netbox_remote_device_id"],
        **extra,
    }


def _sync(client, device, data):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[device.pk]), data, HTTP_HX_REQUEST="true"
    )


def _messages(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def _pick(client, device, row_id, interface):
    """Pick the row's remote end by hand, through the real picker."""
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[device.pk]),
        {"row_id": row_id, "server_key": SERVER_KEY, "remote_interface_id": interface.pk},
    )
    assert response.status_code == 200


def _refused(response, port_label, reason):
    text = f"Skipped (the interface rules refuse a port): Te1/1 (LibreNMS port {port_label}: {reason}"
    return any(text in message for message in _messages(response))


class _Link:
    """Two mapped devices on their own platforms, Te1/1 bound to port 100 and Gi0/1 bound to port 500."""

    def __init__(self, tag, settings, server):
        bind_librenms_server(settings, server, server_key=SERVER_KEY)
        self.server = server
        self.local_platform = _platform(f"{tag}-local")
        self.remote_platform = _platform(f"{tag}-remote")
        self.local_device = self._device(f"{tag}-local", self.local_platform, 8)
        self.remote_device = self._device(f"{tag}-remote", self.remote_platform, 9)
        self.local = _bound(self.local_device, "Te1/1", 100)
        self.remote = _bound(self.remote_device, "Gi0/1", 500)

    @staticmethod
    def _device(name, platform, librenms_id):
        device = make_device(name)
        device.platform = platform
        device.save()
        return map_device_to_librenms(device, librenms_id, server_key=SERVER_KEY)

    def ignore(self, platform, pattern):
        return InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern=pattern)

    def cables(self):
        """Return the cables on the two endpoints, read from the database."""
        self.local.refresh_from_db()
        self.remote.refresh_from_db()
        return {interface.cable_id for interface in (self.local, self.remote)} - {None}


@pytest.fixture
def link(settings, librenms_server, client, request):
    client.force_login(make_superuser("cable-rule-user"))
    return _Link(request.node.name[:24].replace("[", "-").rstrip("]"), settings, librenms_server)


@pytest.mark.django_db
def test_the_refresh_keeps_the_rule_inputs_and_an_ignored_remote_port_gets_no_cable(client, link):
    """The whole path: refresh, table, sync. The remote platform ignores Gi0/1."""
    rule = link.ignore(link.remote_platform, "^Gi0/1$")
    link.server.register(
        "/api/v0/devices/8/links",
        {
            "status": "ok",
            "links": [
                {
                    "id": 1,
                    "protocol": "lldp",
                    "local_port_id": 100,
                    "remote_port": "Gi0/1",
                    "remote_hostname": link.remote_device.name,
                    "remote_port_id": 500,
                    "remote_device_id": 9,
                }
            ],
        },
    )
    link.server.register("/api/v0/devices/8/ports", {"status": "ok", "ports": [_port(100, "Te1/1")]})
    link.server.register("/api/v0/devices/9/ports", {"status": "ok", "ports": [_port(500, "Gi0/1")]})

    refreshed = client.post(
        reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[link.local_device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )
    assert refreshed.status_code == 200
    (raw,) = cable_rows(refreshed.wsgi_request, link.local_device, SERVER_KEY)["links"]
    assert raw["local_port_record"] == port_record(_port(100, "Te1/1"))
    assert raw["remote_port_record"] == port_record(_port(500, "Gi0/1"))

    record, html = _render(client, link.local_device)
    response = _sync(client, link.local_device, _sync_data(record))

    reason = f"ignored by interface rule {rule.pk}"
    assert f"Not synced: LibreNMS port 500 (Gi0/1): {reason}" in html
    assert "Sync Cable" not in html
    assert re.search(r'<input[^>]*name="select"[^>]*disabled', html)
    assert link.cables() == set()
    assert _refused(response, "500 (Gi0/1)", reason)


@pytest.mark.django_db
def test_an_ignored_local_port_gets_no_cable_and_the_verified_row_says_why(client, link):
    rule = link.ignore(link.local_platform, "^Te")
    row_id = _seed(link.local_device, _row(link.local, link.remote_device))

    record, html = _render(client, link.local_device)
    verified = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_cable"),
        data=json.dumps({"device_id": link.local_device.pk, "row_id": row_id, "server_key": SERVER_KEY}),
        content_type="application/json",
    ).json()["formatted_row"]
    response = _sync(client, link.local_device, _sync_data(record))

    reason = f"ignored by interface rule {rule.pk}"
    assert f"Not synced: LibreNMS port 100 (Te1/1): {reason}" in html
    assert f"Not synced: LibreNMS port 100 (Te1/1): {reason}" in unescape(verified["actions"])
    assert verified["can_create_cable"] is False
    assert link.cables() == set()
    assert _refused(response, "100 (Te1/1)", reason)


@pytest.mark.django_db
def test_a_manual_pick_bound_to_an_ignored_port_gets_no_cable(client, link):
    """The advertised port is allowed; the picked interface is bound to a port the rules ignore."""
    rule = link.ignore(link.remote_platform, "^Gi0/9$")
    picked = _bound(link.remote_device, "Gi0/9", 509)
    row_id = _seed(
        link.local_device,
        _row(link.local, link.remote_device),
        neighbour_ports={9: [port_row(500, "Gi0/1", device_id=9), port_row(509, "Gi0/9", device_id=9)]},
    )
    _pick(client, link.local_device, row_id, picked)

    record, _html = _render(client, link.local_device)
    assert record["netbox_remote_interface_id"] == picked.pk
    response = _sync(client, link.local_device, _sync_data(record))

    assert link.cables() == set()
    assert _refused(response, "509 (Gi0/9)", f"ignored by interface rule {rule.pk}")
    assert not any(request["path"].startswith("/api/v0/ports/") for request in link.server.requests)


@pytest.mark.django_db
@pytest.mark.parametrize("ignored", [True, False], ids=["far-end-ignored", "control"])
@pytest.mark.parametrize("displaced_end", ["local", "remote"])
@pytest.mark.parametrize("obtained", [True, False])
@pytest.mark.parametrize("source_mapping", ["own", "vc-owner", "oob"])
@pytest.mark.parametrize("neighbour_identity", ["id", "hostname", "unmapped-id"])
def test_a_replacement_that_removes_a_cable_to_an_ignored_port_is_refused(
    client, link, ignored, displaced_end, obtained, source_mapping, neighbour_identity
):
    """Te1/1 is cabled to Xe0/1 on a third device; replacing that cable detaches Xe0/1."""
    far_platform = _platform("cable-rule-far")
    if source_mapping == "vc-owner":
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members

        _vc, (master, far_device) = make_virtual_chassis_members("cable-rule-far")
    else:
        far_device = make_device("cable-rule-far")
    far_device.platform = far_platform
    far_device.save()
    far = _bound(far_device, "Xe0/1", 777)
    old = cable_together(link.local if displaced_end == "local" else link.remote, far)
    if source_mapping == "vc-owner":
        map_device_to_librenms(master, 10, server_key=SERVER_KEY)
    elif source_mapping == "oob":
        map_device_to_librenms(far_device, None, server_key=SERVER_KEY, oob={"id": 10, "type": "idrac"})
    else:
        map_device_to_librenms(far_device, 10, server_key=SERVER_KEY)
    link.ignore(far_platform, "^Xe" if ignored else "^no-such-port$")
    row = _row(link.local, link.remote_device)
    if neighbour_identity == "hostname":
        row["remote_device_id"] = None
    elif neighbour_identity == "unmapped-id":
        row["remote_device_id"] = 19
        link.server.ports_response(19, [port_row(500, link.remote.name, device_id=19)])
    link.server.register("/api/v0/devices/8/links", {"status": "ok", "links": [row]})
    link.server.ports_response(8, [port_row(100, link.local.name, device_id=8)])
    link.server.ports_response(9, [port_row(500, link.remote.name, device_id=9)])
    link.server.register(
        "/api/v0/devices/10/ports",
        {"status": "ok", "ports": [port_row(777, far.name, device_id=10)]},
        status=200 if obtained else 503,
    )
    refreshed = client.post(
        reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[link.local_device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )
    assert refreshed.status_code == 200
    record, _html = _render(client, link.local_device)
    assert record["cable_status"] == "Cable Mismatch"

    first = _sync(client, link.local_device, _sync_data(record))
    link.local.refresh_from_db()
    link.remote.refresh_from_db()
    intent = _confirmed_intent(link.local, link.remote, old)
    forced = _sync(
        client,
        link.local_device,
        _sync_data(record, force="on", **{f"expected_cable_intent_{record['row_id']}": intent}),
    )

    link.local.refresh_from_db()
    if ignored or not obtained:
        assert Cable.objects.filter(pk=old.pk).exists()
    else:
        assert first.status_code == 200
        assert forced.status_code == 200
        assert not Cable.objects.filter(pk=old.pk).exists()
        assert link.local.cable.b_terminations == [link.remote]
    paths = [entry["path"] for entry in link.server.requests]
    assert paths.count("/api/v0/devices/10/ports") == 1
    assert all(paths.count(path) == 1 for path in paths if path.endswith("/ports"))


def _restricted_client(username, device_ids, interface_ids):
    """A real client whose user may view and change only these devices and interfaces."""
    from dcim.models import Device, Interface
    from django.test import Client

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    user = make_user_with_perms(username, [("view", Cable), ("add", Cable), ("change", Cable)])
    for action in ("view", "change"):
        user = grant(user, action, Device, constraints={"pk__in": device_ids})
        user = grant(user, action, Interface, constraints={"pk__in": interface_ids})
    client = Client()
    client.force_login(user)
    return client


def _actions_cell(html):
    match = re.search(r'<td[^>]*data-col="actions"[^>]*>(.*?)</td>', html, flags=re.S)
    assert match is not None
    return match.group(1)


@pytest.mark.django_db
@pytest.mark.parametrize("neighbour", ["hidden", "not-in-netbox"])
def test_the_advertised_port_of_a_neighbour_the_caller_cannot_see_is_not_named(link, neighbour):
    """
    The row's advertised port 500 (secret-if) is on neighbour B, which the caller cannot view or
    which is not in NetBox. A global rule ignores it; the caller picks a remote end on device C.
    The table and the sync still refuse, but neither names the port, its name or the rule.
    """
    rule = link.ignore(None, "^secret-if$")
    hidden_neighbour = make_device("cable-rule-secret-neighbour")
    picked_device = make_device("cable-rule-picked-c")
    picked = _bound(picked_device, "Eth9", 509)
    map_device_to_librenms(picked_device, 10, server_key=SERVER_KEY)
    row = _row(link.local, link.remote_device, remote_port="secret-if")
    row["remote_hostname"] = hidden_neighbour.name if neighbour == "hidden" else "not-in-netbox"
    row_id = _seed(
        link.local_device,
        row,
        ports=[port_row(100, "Te1/1", device_id=8), port_row(101, "source-extra", device_id=8)],
        extra_links=[
            link_row(101, picked.name, remote_device_id=10, remote_hostname=picked_device.name, remote_port_id=509)
        ],
        neighbour_ports={9: [port_row(500, "secret-if", device_id=9)], 10: [port_row(509, picked.name, device_id=10)]},
    )
    client = _restricted_client(
        f"cable-rule-neighbour-{neighbour}", [link.local_device.pk, picked_device.pk], [link.local.pk, picked.pk]
    )
    _pick(client, link.local_device, row_id, picked)

    record, html = _render(client, link.local_device)
    response = _sync(client, link.local_device, _sync_data(record))

    picked.refresh_from_db()
    pill = _actions_cell(html)
    texts = " ".join(_messages(response))
    assert record["netbox_remote_interface_id"] == picked.pk
    assert "Not synced: an interface rule refuses a port on an interface or device you cannot view" in pill
    assert picked.cable_id is None
    assert "Skipped (the interface rules refuse a port): Te1/1 (an interface rule refuses a port" in texts
    for secret in ("secret-if", "port 500", str(rule)):
        assert secret not in pill
        assert secret not in texts
    for secret in (reverse("dcim:device", args=[hidden_neighbour.pk]),):
        assert secret not in html
        assert secret not in response.content.decode()


def _cable_render_queries(client, tag, count, first_port, *, blocked=True):
    """Seed *count* rows whose local port a global rule refuses, render the cable tab, and count queries."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    local_device = map_device_to_librenms(make_device(f"{tag}-local"), first_port, server_key=SERVER_KEY)
    remote_device = map_device_to_librenms(make_device(f"{tag}-remote"), first_port + 70000, server_key=SERVER_KEY)
    links = []
    host_ports = []
    remote_ports = []
    for index in range(count):
        local = _bound(local_device, f"Te1/{index}", first_port + index)
        _bound(remote_device, f"Gi0/{index}", first_port + 500 + index)
        links.append(
            link_row(
                first_port + index,
                f"Gi0/{index}",
                remote_hostname=remote_device.name,
                remote_device_id=first_port + 70000,
                remote_port_id=None,
                id=index,
            )
        )
        host_ports.append(port_row(first_port + index, local.name, device_id=first_port))
        remote_ports.append(port_row(first_port + 500 + index, f"Gi0/{index}", device_id=first_port + 70000))
    seed_cables_snapshot(local_device, links, host_ports, neighbour_ports={first_port + 70000: remote_ports})
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": local_device.pk, "tab": "cables"},
    )
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url, {"server_key": SERVER_KEY})
    assert response.status_code == 200
    assert unescape(response.content.decode()).count("Not synced: LibreNMS port") == (count if blocked else 0)
    # NetBox reloads its config revision on its own schedule; that is not part of the table.
    return sum("core_configrevision" not in query["sql"] for query in queries.captured_queries)


@pytest.mark.django_db
@pytest.mark.parametrize(("row_count", "extra_batches"), [(16, 0), (20, 1)])
def test_the_cable_table_batches_identity_queries_for_blocking_rows(
    settings, librenms_server, row_count, extra_batches
):
    """Rule checks stay constant; another 32-ID catalog batch adds one query."""
    from dcim.models import Device, Interface
    from django.test import Client

    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
    InterfaceTypeMapping.objects.create(action=IGNORE, name_pattern="^Te")
    user = make_user_with_perms(
        "cable-rule-queries-user",
        [
            ("view", Device),
            ("view", Interface),
            ("change", Interface),
            ("view", Cable),
            ("add", Cable),
            ("change", Cable),
        ],
    )
    client = Client()
    client.force_login(user)
    _cable_render_queries(client, "cable-rule-queries-warm", 2, 1000)

    two = _cable_render_queries(client, "cable-rule-queries-2", 2, 2000)
    many = _cable_render_queries(client, f"cable-rule-queries-{row_count}", row_count, 3000)

    assert many == two + extra_batches


@pytest.mark.django_db
def test_a_refusal_does_not_name_a_port_the_caller_cannot_view(link):
    """
    Te1/1 is cabled to C, whose device and interface the caller cannot view. C is bound to port 777,
    which a rule ignores. The replacement is refused, and the refusal names nothing about C.
    """
    from dcim.models import Device, Interface
    from django.test import Client

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    hidden_device = make_device("cable-rule-hidden-device")
    hidden = _bound(hidden_device, "hidden-if", 777)
    old = cable_together(link.local, hidden)
    link.server.register("/api/v0/ports/777", {"status": "ok", "port": [_port(777, "hidden-port-name")]})
    rule = link.ignore(None, "^hidden-port")
    row_id = _seed(link.local_device, _row(link.local, link.remote_device))
    user = make_user_with_perms("cable-rule-restricted", [("view", Cable), ("add", Cable), ("change", Cable)])
    for action in ("view", "change"):
        user = grant(user, action, Device, constraints={"pk__in": [link.local_device.pk, link.remote_device.pk]})
        user = grant(user, action, Interface, constraints={"pk__in": [link.local.pk, link.remote.pk]})
    client = Client()
    client.force_login(user)
    data = {
        "select": row_id,
        "server_key": SERVER_KEY,
        f"expected_local_id_{row_id}": link.local.pk,
        f"expected_local_device_id_{row_id}": link.local_device.pk,
        f"expected_remote_id_{row_id}": link.remote.pk,
        f"expected_remote_device_id_{row_id}": link.remote_device.pk,
    }

    response = _sync(client, link.local_device, data)

    link.local.refresh_from_db()
    texts = _messages(response)
    body = response.content.decode() + " ".join(texts)
    assert link.local.cable_id == old.pk
    assert any("Skipped (the interface rules refuse a port): Te1/1 (" in text for text in texts)
    for secret in (
        "hidden-port-name",
        "port 777",
        str(rule),
        hidden_device.name,
        reverse("dcim:interface", args=[hidden.pk]),
        reverse("dcim:device", args=[hidden_device.pk]),
    ):
        assert secret not in body


class _ChangesBeforeTheLock(SyncCablesView):
    """The real sync view, with a concurrent change after the pre-lock reads and before the lock."""

    def __init__(self, *, change, **kwargs):
        super().__init__(**kwargs)
        self.change = change
        self.steps = []

    def _snapshot_cable_port_records(self, link_data, terminations, prefetched=None):
        self.steps.append("snapshot evidence")
        return super()._snapshot_cable_port_records(link_data, terminations, prefetched)

    def _lock_cable_terminations(self, local_term, remote_term, **kwargs):
        # The endpoints and the far ends are already read; the lock re-reads them.
        bindings = [read_mapping(term).own_id(SERVER_KEY) for term in (local_term, remote_term)]
        self.steps.append(("pre-lock bindings", *bindings))
        self.change()
        return super()._lock_cable_terminations(local_term, remote_term, **kwargs)


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["platform-to-ignored", "binding-to-unknown", "platform-to-allowed"])
def test_the_gate_decides_with_the_binding_and_platform_read_under_the_lock(client, link, change):
    """The binding and the platform change between the pre-lock read and the lock; the locked value decides."""
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, post

    ignoring, plain = _platform("cable-rule-ignoring"), _platform("cable-rule-plain")
    moved_rule = link.ignore(ignoring, "^Gi0/1$")
    # An Ignore rule can apply on the remote platform; before the change it ignores Gi0/1 only in
    # the "platform-to-allowed" case.
    link.ignore(link.remote_platform, "^Gi0/1$" if change == "platform-to-allowed" else "^no-such-port$")
    _seed(link.local_device, _row(link.local, link.remote_device))
    record, _html = _render(client, link.local_device)

    def change_state():
        if change == "binding-to-unknown":
            seed_mapping(link.remote, SERVER_KEY, own=888, save=False)
            link.remote.save()
        else:
            moved_to = ignoring if change == "platform-to-ignored" else plain
            type(link.remote_device).objects.filter(pk=link.remote_device.pk).update(platform=moved_to)

    view = _ChangesBeforeTheLock(change=change_state)
    request = make_request("post", _sync_data(record))
    post(view, request, pk=link.local_device.pk)

    texts = [str(message) for message in get_messages(request)]
    # The pre-lock read saw the old binding: port 500 on the remote end.
    assert view.steps == ["snapshot evidence", ("pre-lock bindings", 100, 500)]
    if change == "platform-to-allowed":
        assert link.cables() != set()
        assert not any("refuse a port" in text for text in texts)
    elif change == "platform-to-ignored":
        assert link.cables() == set()
        assert any(f"LibreNMS port 500 (Gi0/1): ignored by interface rule {moved_rule.pk}" in text for text in texts)
    else:
        # Gi0/1 is now bound to port 888, so it is no longer the row's port 500.
        assert link.cables() == set()
        assert any("The cable state or target changed after confirmation" in text for text in texts)
        assert not any("refuse a port" in text for text in texts)


@pytest.mark.django_db
def test_a_manual_pick_onto_another_device_keeps_the_advertised_ports_platform(client, link):
    """The advertised port 500 belongs to the neighbour on its platform; picking a device on another platform does not change that."""
    rule = link.ignore(link.remote_platform, "^Gi0/1$")
    other = make_device("cable-rule-picked-device")
    other.platform = _platform("cable-rule-picked")
    other.save()
    picked = make_interface(other, "Eth9", iface_type="10gbase-x-sfpp")
    row_id = _seed(link.local_device, _row(link.local, link.remote_device))
    _pick(client, link.local_device, row_id, picked)

    record, html = _render(client, link.local_device)
    response = _sync(client, link.local_device, _sync_data(record))

    reason = f"LibreNMS port 500 (Gi0/1): ignored by interface rule {rule.pk}"
    assert record["netbox_remote_interface_id"] == picked.pk
    assert f"Not synced: {reason}" in html
    assert "Sync Cable" not in html
    link.local.refresh_from_db()
    picked.refresh_from_db()
    assert link.local.cable_id is None and picked.cable_id is None
    assert _refused(response, "500 (Gi0/1)", f"ignored by interface rule {rule.pk}")


@pytest.mark.django_db
@pytest.mark.parametrize("rule", ["global-advertised", "picked-platform-advertised", "picked-platform-bound"])
def test_a_manual_pick_for_a_neighbour_not_in_netbox(client, link, rule):
    """
    No NetBox device owns the advertised port 500, so it is decided with no platform: only a global
    rule matches it. The picked interface's own bound port 509 is decided with B's platform.
    """
    # The neighbour is not in NetBox, so nothing is bound to its port 500 either.
    link.remote.delete()
    picked_platform = _platform("cable-rule-picked-q")
    other = make_device("cable-rule-picked-b")
    other.platform = picked_platform
    other.save()
    picked = _bound(other, "Eth9", 509)
    map_device_to_librenms(other, 10, server_key=SERVER_KEY)
    ignore = {
        "global-advertised": (None, "^Gi0/1$"),
        "picked-platform-advertised": (picked_platform, "^Gi0/1$"),
        "picked-platform-bound": (picked_platform, "^Eth9$"),
    }[rule]
    ignore_rule = link.ignore(*ignore)
    row = _row(link.local, link.remote_device)
    row["remote_hostname"] = "not-in-netbox"
    row["remote_device_id"] = 70009
    row_id = _seed(
        link.local_device,
        row,
        ports=[port_row(100, "Te1/1", device_id=8), port_row(101, "source-extra", device_id=8)],
        extra_links=[link_row(101, picked.name, remote_device_id=10, remote_hostname=other.name, remote_port_id=509)],
        neighbour_ports={
            70009: [port_row(500, "Gi0/1", device_id=70009)],
            10: [port_row(509, picked.name, device_id=10)],
        },
    )
    _pick(client, link.local_device, row_id, picked)

    record, html = _render(client, link.local_device)
    response = _sync(client, link.local_device, _sync_data(record))

    picked.refresh_from_db()
    assert record["netbox_remote_interface_id"] == picked.pk
    reason = f"ignored by interface rule {ignore_rule.pk}"
    if rule == "global-advertised":
        # The port's owner did not resolve, so the refusal names nothing about it.
        assert f"Not synced: {HIDDEN_PORT_REASON}" in html
        assert picked.cable_id is None
        assert any(f"Te1/1 ({HIDDEN_PORT_REASON})" in text for text in _messages(response))
    elif rule == "picked-platform-advertised":
        # A platform-scoped rule cannot match a port whose owner is unknown.
        assert "Sync Cable" in html
        assert picked.cable_id is not None
    else:
        # The bound port's obtained record uses the picked device's platform.
        assert picked.cable_id is None
        assert _refused(response, "509 (Eth9)", reason)


@pytest.mark.django_db
@pytest.mark.parametrize("may_ignore", [True, False], ids=["ignore-rule-on-platform", "ignore-rule-elsewhere"])
def test_a_missing_port_record_refuses_only_when_an_ignore_rule_could_apply(client, link, may_ignore):
    """A missing snapshot record blocks only when an Ignore rule can apply."""
    link.ignore(link.local_platform if may_ignore else _platform("cable-rule-elsewhere"), "^no-such-port$")
    row = _row(link.local, link.remote_device)
    _seed(link.local_device, row, ports=[])
    record, _html = _render(client, link.local_device)

    response = _sync(client, link.local_device, _sync_data(record))

    assert not any(request["path"].startswith("/api/v0/ports/") for request in link.server.requests)
    if may_ignore:
        assert link.cables() == set()
        assert _refused(response, "100", "no LibreNMS port record is cached for it; refresh the data")
    else:
        link.local.refresh_from_db()
        assert link.local.cable is not None and link.local.cable.b_terminations == [link.remote]


@pytest.mark.django_db
def test_an_ambiguous_port_is_cabled(client, link):
    """Two equal-rank Set type rules block an interface write, but a cable writes no type."""
    for netbox_type in ("10gbase-x-sfpp", "other"):
        InterfaceTypeMapping.objects.create(
            platform=link.local_platform, name_pattern=f"^Te(?#{netbox_type})", netbox_type=netbox_type
        )
    _seed(link.local_device, _row(link.local, link.remote_device))
    record, _html = _render(client, link.local_device)

    _sync(client, link.local_device, _sync_data(record))

    link.local.refresh_from_db()
    assert link.local.cable is not None and link.local.cable.b_terminations == [link.remote]


@pytest.mark.django_db
def test_a_stale_advertised_port_is_not_a_port_the_write_touches(client, link):
    """The matched record 500 names the far end; the advertised 777 is not on the neighbour now."""
    link.ignore(None, "^Xe0/7$")
    link.server.register("/api/v0/ports/777", {"status": "ok", "port": [_port(777, "Xe0/7")]})
    _seed(link.local_device, _row(link.local, link.remote_device, remote_port_id=777))

    record, html = _render(client, link.local_device)
    _sync(client, link.local_device, _sync_data(record))

    assert record["netbox_remote_interface_id"] == link.remote.pk
    assert "Not synced" not in html
    assert "Sync Cable" in html
    link.local.refresh_from_db()
    assert link.local.cable is not None and link.local.cable.b_terminations == [link.remote]
    assert "/api/v0/ports/777" not in [request["path"] for request in link.server.requests]


@pytest.mark.django_db
def test_the_far_end_create_is_not_offered_when_the_local_port_is_ignored(client, link):
    rule = link.ignore(link.local_platform, "^Te")
    link.remote.delete()
    row_id = _seed(link.local_device, _row(link.local, link.remote_device))
    record, html = _render(client, link.local_device)

    offer = client.get(
        reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[link.local_device.pk]),
        {"expected_local_id": link.local.pk, "row_id": row_id, "server_key": SERVER_KEY},
    )

    assert "remote_create_url" not in record
    assert "Create the remote interface" not in html
    assert offer.status_code == 409
    assert f"LibreNMS port 100 (Te1/1): ignored by interface rule {rule.pk}" in offer.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("set_type_rule", [False, True])
def test_cable_render_skips_disclosure_queries_without_ignore_rules(client, settings, librenms_server, set_type_rule):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from netbox_librenms_plugin.tests.conftest import bind_librenms_server

    bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
    InterfaceTypeMapping.objects.all().delete()
    if set_type_rule:
        InterfaceTypeMapping.objects.create(librenms_type="ethernetCsmacd", netbox_type="1000base-t")
    client.force_login(make_superuser("cable-no-ignore-user"))

    with CaptureQueriesContext(connection) as queries:
        _cable_render_queries(client, "cable-no-ignore", 2, 8100, blocked=False)

    scans = [
        query["sql"]
        for query in queries.captured_queries
        if 'FROM "virtualization_vminterface"' in query["sql"] and " ~ " in query["sql"]
    ]
    assert scans == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("rule_state", ["allowed", "ignored", "no-rules"])
@pytest.mark.parametrize("obtained", [True, False], ids=["obtained", "failed"])
@pytest.mark.parametrize("source_mapping", ["own", "vc-owner", "oob"])
def test_a_manual_destination_prefetches_displaced_rule_evidence_before_the_write(
    client, link, monkeypatch, rule_state, obtained, source_mapping
):
    from django.db import connection
    from requests.sessions import Session

    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource

    picked_device = make_device("manual-cable-destination")
    map_device_to_librenms(picked_device, 11, server_key=SERVER_KEY)
    picked = _bound(picked_device, "Eth1", 601)
    far_platform = _platform("manual-cable-displaced")
    if source_mapping == "vc-owner":
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members

        _vc, (master, far_device) = make_virtual_chassis_members("manual-cable-displaced")
        map_device_to_librenms(master, 10, server_key=SERVER_KEY)
    else:
        far_device = make_device("manual-cable-displaced")
        map_device_to_librenms(
            far_device,
            10 if source_mapping == "own" else None,
            server_key=SERVER_KEY,
            oob={"id": 10, "type": "idrac"} if source_mapping == "oob" else None,
        )
    far_device.platform = far_platform
    far_device.save()
    far = _bound(far_device, "Xe0/1", 777)
    old = cable_together(picked, far)
    if rule_state != "no-rules":
        link.ignore(far_platform, "^Xe" if rule_state == "ignored" else "^no-such-port$")
    row_id = _seed(link.local_device, _row(link.local, link.remote_device))
    before = source_cache.read(link.local_device, Resource.CABLES, SERVER_KEY).to_payload()
    _pick(client, link.local_device, row_id, picked)
    record, _html = _render(client, link.local_device)
    assert record["netbox_remote_interface_id"] == picked.pk
    link.server.ports_response(11, [port_row(601, picked.name, device_id=11)])
    link.server.register(
        "/api/v0/devices/10/ports",
        {"status": "ok", "ports": [port_row(777, far.name, device_id=10)]},
        status=200 if obtained else 503,
    )
    real_send = Session.send

    def offline_write(session, request, **kwargs):
        assert not connection.in_atomic_block
        return real_send(session, request, **kwargs)

    monkeypatch.setattr(Session, "send", offline_write)
    intent = _confirmed_intent(link.local, picked, old)
    response = _sync(
        client,
        link.local_device,
        _sync_data(record, force="on", **{f"expected_cable_intent_{row_id}": intent}),
    )
    assert response.status_code == 200
    link.local.refresh_from_db()
    if rule_state == "ignored" or not obtained:
        assert Cable.objects.filter(pk=old.pk).exists()
        assert link.local.cable_id is None
    else:
        assert not Cable.objects.filter(pk=old.pk).exists()
        assert link.local.cable.b_terminations == [picked]
    paths = [entry["path"] for entry in link.server.requests]
    assert paths.count("/api/v0/devices/10/ports") == 1
    assert all(paths.count(path) == 1 for path in paths if path.endswith("/ports"))
    assert source_cache.read(link.local_device, Resource.CABLES, SERVER_KEY).to_payload() == before


@pytest.mark.django_db
def test_manual_rows_share_one_ports_request_per_displaced_owner(client, link):
    local_b = _bound(link.local_device, "Te1/2", 101)
    picked_device = _Link._device("manual-cable-batch-picked", None, 11)
    far_device = _Link._device("manual-cable-batch-displaced", _platform("manual-cable-batch"), 10)
    link.ignore(far_device.platform, "^no-such-port$")
    chosen = [_bound(picked_device, "Eth1", 601), _bound(picked_device, "Eth2", 602)]
    far = [_bound(far_device, "Xe0/1", 777), _bound(far_device, "Xe0/2", 778)]
    old = [cable_together(pick, displaced) for pick, displaced in zip(chosen, far, strict=True)]
    _seed(
        link.local_device,
        _row(link.local, link.remote_device),
        ports=[port_row(100, link.local.name, device_id=8), port_row(101, local_b.name, device_id=8)],
        extra_links=[_row(local_b, link.remote_device, local_port_id=101, remote_port_id=501, id=2)],
    )
    for row_id, pick in zip(("100", "101"), chosen, strict=True):
        _pick(client, link.local_device, row_id, pick)
    page = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[link.local_device.pk]),
        {"tab": "cables", "server_key": SERVER_KEY},
    )
    rows = {row.record["row_id"]: row.record for row in page.context["cable_sync"]["table"].rows}
    link.server.ports_response(10, [port_row(777, "Xe0/1", device_id=10), port_row(778, "Xe0/2", device_id=10)])
    link.server.ports_response(11, [port_row(601, "Eth1", device_id=11), port_row(602, "Eth2", device_id=11)])
    data = {"select": ["100", "101"], "server_key": SERVER_KEY, "force": "on"}
    for row_id, local, pick, cable in zip(("100", "101"), (link.local, local_b), chosen, old, strict=True):
        data.update({key: value for key, value in _sync_data(rows[row_id]).items() if key != "select"})
        data[f"expected_cable_intent_{row_id}"] = _confirmed_intent(local, pick, cable)

    response = _sync(client, link.local_device, data)

    assert response.status_code == 200
    assert not Cable.objects.filter(pk__in=[cable.pk for cable in old]).exists()
    for local, pick in zip((link.local, local_b), chosen, strict=True):
        local.refresh_from_db()
        pick.refresh_from_db()
        assert local.cable_id == pick.cable_id is not None
    paths = [entry["path"] for entry in link.server.requests]
    assert paths.count("/api/v0/devices/10/ports") == 1
    assert paths.count("/api/v0/devices/11/ports") == 1


@pytest.mark.django_db
@pytest.mark.parametrize("source_mapping", ["own", "vc-owner", "oob"])
@pytest.mark.parametrize("host_obtained", [True, False])
@pytest.mark.parametrize("hidden_part", ["device", "interface"])
def test_displaced_source_failures_are_redacted_for_each_snapshot_reader(
    client, link, source_mapping, host_obtained, hidden_part
):
    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource

    if source_mapping == "vc-owner":
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members

        _vc, (master, hidden_owner) = make_virtual_chassis_members("hidden-source-owner")
        map_device_to_librenms(master, 10, server_key=SERVER_KEY)
    else:
        hidden_owner = make_device("hidden-source-owner")
        map_device_to_librenms(
            hidden_owner,
            10 if source_mapping == "own" else None,
            server_key=SERVER_KEY,
            oob={"id": 10, "type": "idrac"} if source_mapping == "oob" else None,
        )
    hidden_port = _bound(hidden_owner, "hidden-source-port", 777)
    cable_together(link.remote, hidden_port)
    link.server.register(
        "/api/v0/devices/8/links",
        {"status": "ok", "links": [_row(link.local, link.remote_device, remote_device_id=None)]},
    )
    link.server.ports_response(8, [port_row(100, link.local.name, device_id=8)])
    link.server.ports_response(9, [port_row(500, link.remote.name, device_id=9)])
    secret = "private-displaced-source-error"
    link.server.register("/api/v0/devices/10/ports", {"status": "error", "message": secret}, status=503)
    refresh_url = reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[link.local_device.pk])
    privileged = client.post(refresh_url, {"server_key": SERVER_KEY}, HTTP_HX_REQUEST="true")
    assert privileged.status_code == 200
    before = source_cache.read(link.local_device, Resource.CABLES, SERVER_KEY).to_payload()
    assert any(secret in blocker.message for blocker in privileged.context["cable_sync"]["blockers"])
    assert secret in json.dumps(before, default=str)
    visible_devices = [link.local_device.pk, link.remote_device.pk]
    if source_mapping == "vc-owner":
        visible_devices.append(master.pk)
    if hidden_part == "interface":
        visible_devices.append(hidden_owner.pk)
    restricted = _restricted_client("hidden-source-reader", visible_devices, [link.local.pk, link.remote.pk])
    cached = restricted.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[link.local_device.pk]),
        {"tab": "cables", "server_key": SERVER_KEY},
    )
    assert cached.status_code == 200
    assert cached.context["cable_sync"]["blockers"]
    assert source_cache.read(link.local_device, Resource.CABLES, SERVER_KEY).to_payload() == before
    if not host_obtained:
        link.server.register("/api/v0/devices/8/ports", {"status": "error"}, status=503)
    refreshed = restricted.post(refresh_url, {"server_key": SERVER_KEY}, HTTP_HX_REQUEST="true")
    assert refreshed.status_code == 200
    for response in (cached, refreshed):
        body = response.content.decode() + " ".join(_messages(response))
        for value in (secret, "devices/10/ports", hidden_owner.name, hidden_port.name):
            assert value not in body


@pytest.mark.django_db
@pytest.mark.parametrize("hidden_source", ["chassis-master", "other-interface"])
def test_manual_prefetch_failures_use_the_source_owner_disclosure_scope(client, link, hidden_source):
    if hidden_source == "chassis-master":
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members

        _vc, (hidden_master, far_owner) = make_virtual_chassis_members("manual-hidden-source")
        map_device_to_librenms(hidden_master, 10, server_key=SERVER_KEY)
    else:
        far_owner = _Link._device("manual-hidden-source", None, 10)
        _bound(far_owner, "private-source-interface", 778)
    far_platform = _platform("manual-hidden-source")
    far_owner.platform = far_platform
    far_owner.save()
    far = _bound(far_owner, "Xe0/1", 777)
    picked_owner = _Link._device("manual-visible-destination", None, 11)
    picked = _bound(picked_owner, "Eth1", 601)
    old = cable_together(picked, far)
    link.ignore(far_platform, "^no-such-port$")
    row_id = _seed(link.local_device, _row(link.local, link.remote_device))
    restricted = _restricted_client(
        "manual-hidden-source-reader",
        [link.local_device.pk, link.remote_device.pk, picked_owner.pk, far_owner.pk],
        [link.local.pk, link.remote.pk, picked.pk, far.pk],
    )
    _pick(restricted, link.local_device, row_id, picked)
    record, _html = _render(restricted, link.local_device)
    link.server.ports_response(11, [port_row(601, picked.name, device_id=11)])
    secret = "private-manual-source-error"
    link.server.register("/api/v0/devices/10/ports", {"status": "error", "message": secret}, status=503)
    response = _sync(
        restricted,
        link.local_device,
        _sync_data(
            record,
            force="on",
            **{f"expected_cable_intent_{row_id}": _confirmed_intent(link.local, picked, old)},
        ),
    )

    assert response.status_code == 200
    assert Cable.objects.filter(pk=old.pk).exists()
    link.local.refresh_from_db()
    assert link.local.cable_id is None
    body = response.content.decode() + " ".join(_messages(response))
    assert secret not in body
    assert "devices/10/ports" not in body
