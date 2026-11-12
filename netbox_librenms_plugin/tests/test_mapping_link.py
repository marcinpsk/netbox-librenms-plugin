"""
Link through the real routes: the sync page and the status lists propose a match, and only a confirmed Link stores it.

Each test mints its token through the page that offers it, posts it over HTTP, and reads the real database.
"""

import netaddr
import pytest
from dcim.models import Device
from django.test import Client
from django.urls import reverse
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.tests.claim_race_helpers import run_librenms_id_claim_race
from netbox_librenms_plugin.tests.conftest import (
    configure_librenms_servers,
    make_cluster,
    make_device,
    make_virtual_chassis,
    make_vm,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.link_intent_helpers import (
    give_primary_ip,
    link,
    link_selected,
    message_texts,
    page_link,
    page_token,
    register_match,
    stale_paths,
    status_list,
    sync_page,
)
from netbox_librenms_plugin.tests.mapping_fixtures import (
    mapping_summary,
    seed_mapping,
    seed_mapping_row,
    unmapped_only_constraint,
)
from netbox_librenms_plugin.tests.view_test_helpers import domain_state, grant, make_user_with_perms
from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE

PERMISSIONS = [
    ("view", Device),
    ("change", Device),
    ("view", VirtualMachine),
    ("change", VirtualMachine),
]
KINDS = pytest.mark.parametrize("kind", ["device", "virtualmachine"])


@pytest.fixture
def server(settings, librenms_server):
    configure_librenms_servers(
        settings,
        {
            key: {"display_name": f"{key.title()} LibreNMS", "librenms_url": librenms_server.url, "api_token": "t"}
            for key in ("default", "other")
        },
    )
    return librenms_server


@pytest.fixture
def linker(client):
    client.force_login(make_user_with_perms("link-writer", PERMISSIONS))
    return client


def _object(kind, name, *, cluster=None):
    if kind == "device":
        return make_device(name)
    return make_vm(name, cluster or make_cluster(f"{name}-cluster"))


def _matched(kind, name, server, address, librenms_id, **extra):
    """Return an unlinked object whose primary IP *address* the LibreNMS fake matches as *librenms_id*."""
    obj = _object(kind, name, **extra)
    give_primary_ip(obj, f"{address}/24")
    register_match(server, address, librenms_id, hostname=name)
    return obj


def _change_records(obj):
    from core.models import ObjectChange
    from django.contrib.contenttypes.models import ContentType

    return ObjectChange.objects.filter(
        changed_object_type=ContentType.objects.get_for_model(type(obj)), changed_object_id=obj.pk
    ).count()


def _fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


@pytest.mark.django_db
def test_the_primary_ip_helper_keeps_the_address_type_that_netbox_loads():
    # NetBox 4.4 IPAddress.family (read on delete and in Device.clean) raises on a str address.
    ip = give_primary_ip(make_device("typed-address"), "198.51.100.40/24")

    assert isinstance(ip.address, netaddr.IPNetwork)


@pytest.mark.django_db
@KINDS
class TestLinkOnTheSyncPage:
    def test_an_unchanged_match_links_and_records_the_change(self, linker, server, kind):
        obj = _matched(kind, f"link-applies-{kind}", server, "203.0.113.10", 8100)
        changes = _change_records(obj)

        response = link(linker, obj, [page_token(linker, obj)])

        assert response.status_code == 302
        assert mapping_summary(_fresh(obj)) == {"default": {"own": 8100}}
        assert _change_records(obj) == changes + 1
        assert page_link(linker, obj) is None

    @pytest.mark.parametrize(
        ("fact", "path"),
        [
            ("primary_ip_changed", "assumptions.inputs.primary_ip4.address"),
            ("primary_ip_replaced", "assumptions.inputs.primary_ip4.pk"),
            ("another_object_took_the_id", "assumptions.owned"),
            ("linked_meanwhile", "assumptions.own_id"),
        ],
    )
    def test_a_relevant_change_is_stale_and_writes_nothing(self, linker, server, kind, fact, path):
        from ipam.models import IPAddress

        obj = _matched(kind, f"link-stale-{kind}-{fact}", server, "203.0.113.20", 8200)
        token = page_token(linker, obj)
        if fact == "primary_ip_changed":
            IPAddress.objects.filter(pk=obj.primary_ip4_id).update(address="203.0.113.21/24")
        elif fact == "primary_ip_replaced":
            give_primary_ip(_fresh(obj), "203.0.113.22/24")
        elif fact == "another_object_took_the_id":
            seed_mapping(_object(kind, f"link-owner-{kind}"), "default", own=8200)
        else:
            seed_mapping(_fresh(obj), "default", own=8201)
        before = domain_state()

        response = link(linker, obj, [token])

        assert domain_state() == before
        assert path in stale_paths(response, obj.name)

    @pytest.mark.parametrize("tokens", [["mapping_lifecycle:tampered"], "stale"])
    def test_the_page_after_a_refused_link_proposes_the_match_again(self, linker, server, kind, tokens):
        obj = _matched(kind, f"link-refused-{kind}-{len(tokens)}", server, "203.0.113.40", 8400)
        if tokens == "stale":
            from ipam.models import IPAddress

            tokens = [page_token(linker, obj)]
            IPAddress.objects.filter(pk=obj.primary_ip4_id).update(address="203.0.113.41/24")
            register_match(server, "203.0.113.41", 8401, hostname=obj.name)

        response = link(linker, obj, tokens)
        page = linker.get(response.url)

        assert mapping_summary(_fresh(obj)) == {}
        assert page.status_code == 200
        assert page.context["link"]["token"]

    def test_a_new_primary_ip_after_a_name_match_is_stale(self, linker, server, kind):
        """The primary IP probe comes first, so an address that appears after a name match can match elsewhere."""
        obj = _object(kind, f"link-by-name-{kind}")
        register_match(server, obj.name, 8300, hostname=obj.name)
        link_proposal = page_link(linker, obj)
        assert link_proposal["matched_by"] == "name"
        give_primary_ip(_fresh(obj), "203.0.113.30/24")
        before = domain_state()

        response = link(linker, obj, [link_proposal["token"]])

        assert domain_state() == before
        assert "assumptions.inputs.primary_ip4" in stale_paths(response, obj.name)

    def test_a_dual_stack_object_links_through_its_ipv4_primary(self, linker, server, kind):
        obj = _matched(kind, f"link-dual-v4-{kind}", server, "203.0.113.70", 8500)
        give_primary_ip(_fresh(obj), "2001:db8::70/64")

        response = link(linker, obj, [page_token(linker, obj)])

        assert response.status_code == 302
        assert mapping_summary(_fresh(obj)) == {"default": {"own": 8500}}

    def test_a_changed_ipv4_primary_after_an_ipv6_match_is_stale(self, linker, server, kind):
        """The IPv4 probe comes first, so its new address can match another LibreNMS device."""
        from ipam.models import IPAddress

        obj = _object(kind, f"link-dual-v6-{kind}")
        ip4 = give_primary_ip(obj, "203.0.113.80/24")
        give_primary_ip(_fresh(obj), "2001:db8::80/64")
        register_match(server, "2001%3Adb8%3A%3A80", 8600, hostname=obj.name)
        token = page_token(linker, obj)
        IPAddress.objects.filter(pk=ip4.pk).update(address="203.0.113.81/24")
        before = domain_state()

        response = link(linker, obj, [token])

        assert domain_state() == before
        assert "assumptions.inputs.primary_ip4.address" in stale_paths(response, obj.name)

    def test_primaries_that_name_two_devices_offer_no_link_and_write_nothing(self, linker, server, kind):
        obj = _matched(kind, f"link-dual-conflict-{kind}", server, "203.0.113.90", 8700)
        give_primary_ip(_fresh(obj), "2001:db8::90/64")
        register_match(server, "2001%3Adb8%3A%3A90", 8701, hostname=obj.name)
        before = domain_state()

        link_proposal = page_link(linker, obj)

        assert (link_proposal["state"], link_proposal["token"]) == ("no_match", None)
        assert domain_state() == before

    def test_primaries_that_name_one_device_offer_its_link(self, linker, server, kind):
        obj = _matched(kind, f"link-dual-agree-{kind}", server, "203.0.113.91", 8710)
        give_primary_ip(_fresh(obj), "2001:db8::91/64")
        register_match(server, "2001%3Adb8%3A%3A91", 8710, hostname=obj.name)

        response = link(linker, obj, [page_token(linker, obj)])

        assert response.status_code == 302
        assert mapping_summary(_fresh(obj)) == {"default": {"own": 8710}}
        assert "/api/v0/devices/2001%3Adb8%3A%3A91" in [request["path"] for request in server.requests]

    def test_an_ipv6_dns_name_change_after_an_ipv4_dns_name_match_is_stale(self, linker, server, kind):
        """A DNS name match asks both DNS names, so a token binds both."""
        from ipam.models import IPAddress

        obj = _object(kind, f"link-dns6-{kind}")
        ip4 = give_primary_ip(obj, "203.0.113.110/24")
        ip6 = give_primary_ip(_fresh(obj), "2001:db8::110/64")
        IPAddress.objects.filter(pk=ip4.pk).update(dns_name=f"v4-{kind}.example.net")
        IPAddress.objects.filter(pk=ip6.pk).update(dns_name=f"v6-{kind}.example.net")
        register_match(server, f"v4-{kind}.example.net", 8730, hostname=obj.name)
        link_proposal = page_link(linker, obj)
        assert link_proposal["matched_by"] == "DNS name"
        IPAddress.objects.filter(pk=ip6.pk).update(dns_name=f"v6-other-{kind}.example.net")
        before = domain_state()

        response = link(linker, obj, [link_proposal["token"]])

        assert domain_state() == before
        assert "assumptions.inputs.dns_name6" in stale_paths(response, obj.name)

    @pytest.mark.parametrize("change", ["added_other_device", "added_same_device", "changed", "removed"])
    def test_an_ipv6_change_after_an_ipv4_match_is_stale(self, linker, server, kind, change):
        """Every lookup asks both primary addresses, so a token binds both rows."""
        from ipam.models import IPAddress

        obj = _matched(kind, f"link-v6-{change}-{kind}", server, "203.0.113.100", 8720)
        if change in ("changed", "removed"):
            ip6 = give_primary_ip(_fresh(obj), "2001:db8::100/64")
            register_match(server, "2001%3Adb8%3A%3A100", 8720, hostname=obj.name)
        token = page_token(linker, obj)
        if change == "added_other_device":
            give_primary_ip(_fresh(obj), "2001:db8::101/64")
            register_match(server, "2001%3Adb8%3A%3A101", 8721, hostname=obj.name)
        elif change == "added_same_device":
            give_primary_ip(_fresh(obj), "2001:db8::102/64")
            register_match(server, "2001%3Adb8%3A%3A102", 8720, hostname=obj.name)
        elif change == "changed":
            IPAddress.objects.filter(pk=ip6.pk).update(address="2001:db8::103/64")
        else:
            type(obj).objects.filter(pk=obj.pk).update(primary_ip6=None)
        before = domain_state()

        response = link(linker, obj, [token])

        assert domain_state() == before
        assert any(path.startswith("assumptions.inputs.primary_ip6") for path in stale_paths(response, obj.name))

    def test_a_match_that_another_object_owns_offers_no_link(self, linker, server, kind):
        obj = _matched(kind, f"link-owned-{kind}", server, "203.0.113.40", 8400)
        owner = seed_mapping(_object(kind, f"link-owned-by-{kind}"), "default", own=8400)

        proposal = page_link(linker, obj)

        assert proposal["state"] == "owned"
        assert proposal["token"] is None
        assert owner.name in proposal["owner_label"]


@pytest.mark.django_db
class TestLinkWritesOnTheStoredMapping:
    def test_the_link_keeps_the_oob_entry_and_the_other_servers(self, linker, server):
        device = _matched("device", "link-keeps-entries", server, "203.0.113.120", 9800)
        seed_mapping(_fresh(device), "default", oob=9801, oob_type="idrac")
        seed_mapping(_fresh(device), "other", own=9802)

        link(linker, device, [page_token(linker, device)])

        assert mapping_summary(_fresh(device)) == {
            "default": {"own": 9800, "oob": 9801, "oob_type": "idrac"},
            "other": {"own": 9802},
        }


# A change grant that admits only objects without a stored mapping: a Link moves its object out of it.
_UNMAPPED_ONLY = unmapped_only_constraint()


def _unmapped_only_writer(client, tag):
    user = make_user_with_perms(f"scope-after-{tag}", [("view", Device)])
    client.force_login(grant(user, "change", Device, constraints=_UNMAPPED_ONLY))
    return client


@pytest.mark.django_db
class TestLinkStaysInTheChangeScope:
    """The saved object must still be in the actor's change scope; else its link rolls back."""

    def test_a_link_that_moves_the_object_out_of_the_scope_is_refused(self, client, server):
        device = _matched("device", "scope-after-single", server, "203.0.113.130", 9900)
        writer = _unmapped_only_writer(client, "single")
        token = page_token(writer, device)
        before = domain_state()

        response = link(writer, device, [token])

        assert domain_state() == before
        assert not any(text.startswith("Linked ") for text in message_texts(response))

    def test_a_refused_row_rolls_back_alone_and_the_other_rows_link(self, client, server):
        first = _matched("device", "scope-after-first", server, "203.0.113.131", 9901)
        second = _matched("device", "scope-after-second", server, "203.0.113.132", 9902)
        writer = _unmapped_only_writer(client, "bulk")
        tokens = [page_token(writer, first), page_token(writer, second)]
        # Only the first row leaves the scope: the grant admits the second by name too.
        from users.models import ObjectPermission

        permission = ObjectPermission.objects.get(
            users__username="scope-after-bulk", actions=["change"], object_types__model="device"
        )
        permission.constraints = [*_UNMAPPED_ONLY, {"name": second.name}]
        permission.save()

        response = link_selected(writer, "device", tokens)

        assert mapping_summary(_fresh(first)) == {}
        assert mapping_summary(_fresh(second)) == {"default": {"own": 9902}}
        texts = message_texts(response)
        assert any(text.startswith(f"'{first.name}': ") for text in texts)
        assert any(text.startswith(f"Linked '{second.name}'") for text in texts)


@transactional_db_with_all_apps()
def test_a_refused_link_sends_no_event(client, server, flushed_events):
    """The savepoint rolls the row back, so the event that its save queued must not be sent either."""
    device = _matched("device", "event-after-single", server, "203.0.113.150", 9980)
    writer = _unmapped_only_writer(client, "event-single")
    token = page_token(writer, device)

    link(writer, device, [token])

    assert mapping_summary(_fresh(device)) == {}
    assert flushed_events == []


@transactional_db_with_all_apps()
def test_a_bulk_link_sends_the_event_of_the_applied_row_only(client, server, flushed_events):
    from users.models import ObjectPermission

    first = _matched("device", "event-after-first", server, "203.0.113.151", 9981)
    second = _matched("device", "event-after-second", server, "203.0.113.152", 9982)
    writer = _unmapped_only_writer(client, "event-bulk")
    tokens = [page_token(writer, first), page_token(writer, second)]
    permission = ObjectPermission.objects.get(
        users__username="scope-after-event-bulk", actions=["change"], object_types__model="device"
    )
    permission.constraints = [*_UNMAPPED_ONLY, {"name": second.name}]
    permission.save()

    link_selected(writer, "device", tokens)

    assert mapping_summary(_fresh(second)) == {"default": {"own": 9982}}
    assert flushed_events == [("device", second.pk)]


@pytest.mark.django_db
class TestSyncPageAnswers:
    def test_a_device_without_a_match_is_not_found(self, linker, server):
        device = make_device("link-no-match")
        give_primary_ip(device, "203.0.113.50/24")

        proposal = page_link(linker, device)

        assert proposal["state"] == "no_match"
        assert proposal["token"] is None

    @pytest.mark.parametrize("answer", ["server_error", "disconnect"])
    def test_an_unreachable_server_is_not_reported_as_not_found(self, linker, server, answer):
        device = make_device("link-unreachable")
        give_primary_ip(device, "203.0.113.60/24")
        if answer == "server_error":
            server.register("/api/v0/devices/203.0.113.60", {"status": "error"}, status=500)
        else:
            server.register_disconnect("/api/v0/devices/203.0.113.60")

        proposal = page_link(linker, device)

        assert proposal["state"] == "unreachable"
        # An unanswered primary IP probe makes a later name match untrustworthy, so the name is not asked.
        assert [request["path"] for request in server.requests] == ["/api/v0/devices/203.0.113.60"]

    def test_a_refused_stored_value_asks_for_repair_and_makes_no_lookup(self, linker, server):
        from netbox_librenms_plugin.tests.mapping_fixtures import seed_quarantined

        device = _matched("device", "link-refused", server, "203.0.113.140", 9950)
        seed_quarantined(_fresh(device))

        page = sync_page(linker, device)
        listed = status_list(linker, "device", q=device.name)

        body = page.content.decode()
        assert page.context["link"]["state"] == "refused"
        assert page.context["show_add_device"] is False
        # The card and the guidance each take the repair branch; neither takes the branch of a lookup without a match.
        assert body.count('data-link-branch="refused"') == 2
        assert 'data-link-branch="no_match"' not in body
        assert [record.librenms_status for record in listed.context["table"].data.data] == ["refused"]
        assert server.requests == []

    def test_a_viewer_without_change_permission_sees_the_match_without_a_token(self, client, server):
        device = _matched("device", "link-read-only", server, "203.0.113.70", 8700)
        client.force_login(make_user_with_perms("link-reader", [("view", Device)]))

        proposal = page_link(client, device)

        assert proposal["state"] == "match"
        assert proposal["token"] is None


@pytest.mark.django_db
class TestReadersNeedAStoredLink:
    """A refresh or a device-field sync reads the stored link only; it never matches, writes or fetches."""

    @pytest.mark.parametrize(
        "route",
        ["device_interface_sync", "device_ipaddress_sync", "device_vlan_sync", "device_module_sync"],
    )
    def test_a_refresh_of_an_unlinked_device_fetches_nothing(self, linker, server, route):
        device = _matched("device", f"link-refresh-{route}", server, "203.0.113.80", 8800)
        before = domain_state()

        response = linker.post(
            reverse(f"plugins:netbox_librenms_plugin:{route}", args=[device.pk]),
            {"server_key": "default"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert domain_state() == before
        assert server.requests == []
        assert "Not linked to Default LibreNMS." in message_texts(response)

    @pytest.mark.parametrize("route", ["update_device_name", "update_device_serial", "update_device_type"])
    def test_a_device_field_sync_of_an_unlinked_device_refuses(self, linker, server, route):
        device = _matched("device", f"link-field-{route}", server, "203.0.113.90", 8900)
        before = domain_state()

        response = linker.post(
            reverse(f"plugins:netbox_librenms_plugin:{route}", args=[device.pk]), {"server_key": "default"}
        )

        assert response.status_code == 302
        assert domain_state() == before
        assert server.requests == []
        assert message_texts(response) == ["Not linked to Default LibreNMS."]


def _status_rows(response):
    return response.context["table"].link_rows


@pytest.mark.django_db
class TestStatusLists:
    def test_each_row_shows_its_stored_link_or_its_match(self, linker, server):
        from dcim.models import DeviceRole

        role = DeviceRole.objects.create(name="link-status", slug="link-status", color="00ff00")
        devices = {}
        for name, address, librenms_id in [
            ("status-a-linked", None, None),
            ("status-b-match", "198.18.1.2", 9002),
            ("status-c-owned", "198.18.1.3", 9003),
            ("status-d-twin", "198.18.1.4", 9004),
            ("status-e-twin", "198.18.1.5", 9004),
            ("status-f-none", "198.18.1.6", None),
        ]:
            device = make_device(name)
            device.role = role
            device.save()
            if address:
                give_primary_ip(device, f"{address}/24")
            if librenms_id:
                register_match(server, address, librenms_id, hostname=name)
            devices[name] = device
        seed_mapping(devices["status-a-linked"], "default", own=9001)
        seed_mapping(make_device("status-owner"), "default", own=9003)
        before = domain_state()

        response = status_list(linker, "device", role=role.pk)

        assert domain_state() == before
        rows = _status_rows(response)
        states = {device.name: rows[device.pk].state for device in devices.values() if device.pk in rows}
        assert states == {
            "status-b-match": "match",
            "status-c-owned": "owned",
            "status-d-twin": "ambiguous",
            "status-e-twin": "ambiguous",
            "status-f-none": "no_match",
        }
        assert [pk for pk, row in rows.items() if row.token] == [devices["status-b-match"].pk]
        listed = {record.pk: record.librenms_status for record in response.context["table"].data.data}
        assert listed[devices["status-a-linked"].pk] == "linked"
        # The linked row is never matched.
        assert not any("198.18.1.1" in request["path"] for request in server.requests)

    def test_matching_stops_at_the_deadline_and_reports_the_rows_not_checked(self, linker, server, monkeypatch):
        import json
        import time

        from netbox_librenms_plugin.views import status_check

        monkeypatch.setattr(status_check, "STATUS_MATCH_DEADLINE_SECONDS", 1)
        cluster = make_cluster("link-deadline")
        vms = [_object("virtualmachine", f"deadline-{index}", cluster=cluster) for index in range(3)]
        for index, vm in enumerate(vms):
            give_primary_ip(vm, f"198.18.2.{index + 1}/24")
        body = json.dumps({"status": "ok", "devices": [{"device_id": 9100, "hostname": "deadline-0"}]}).encode()
        # The first answer arrives in parts, each inside a socket timeout, over almost five seconds.
        parts = [body[index : index + 10] for index in range(0, len(body), 10)]
        server.register_streamed("/api/v0/devices/198.18.2.1", parts, pause=0.8, headers={"Connection": "keep-alive"})
        before = domain_state()
        started = time.perf_counter()

        response = status_list(linker, "virtualmachine", cluster=cluster.pk)

        assert time.perf_counter() - started < 3
        assert domain_state() == before
        rows = _status_rows(response)
        assert [rows[vm.pk].state for vm in vms] == ["not_checked", "not_checked", "not_checked"]
        assert [request["path"] for request in server.requests] == ["/api/v0/devices/198.18.2.1"]
        assert any(text.startswith("3 of 3 unlinked objects") for text in message_texts(response))

    def test_a_row_that_htmx_pages_in_links_through_the_forms_of_a_first_page_without_offers(self, linker, server):
        """NetBox swaps a later page in as a table fragment, so its Link controls use the forms of the first page."""
        import re

        from dcim.models import DeviceRole

        from netbox_librenms_plugin.confirmation_intent import FIELD

        role = DeviceRole.objects.create(name="htmx-pages", slug="htmx-pages", color="00ff00")
        # NetBox folds up to five orphans into the last page, so page one holds one linked row of seven.
        linked = [make_device(f"htmx-pages-a{index}") for index in range(6)]
        unlinked = make_device("htmx-pages-b")
        for device in (*linked, unlinked):
            device.role = role
            device.save()
        for index, device in enumerate(linked):
            seed_mapping(device, "default", own=9970 + index)
        give_primary_ip(unlinked, "198.18.9.2/24")
        register_match(server, "198.18.9.2", 9962)
        list_url = reverse("plugins:netbox_librenms_plugin:device_status_list")

        first = status_list(linker, "device", role=role.pk, per_page=1)
        fragment = linker.get(list_url, {"role": role.pk, "per_page": 1, "page": 2}, HTTP_HX_REQUEST="true")
        tokens = re.findall(
            r'form="librenms-link-row" name="confirmation_intent" value="([^"]+)"', fragment.content.decode()
        )
        second_page = f"{list_url}?role={role.pk}&per_page=1&page=2"
        response = linker.post(
            reverse("plugins:netbox_librenms_plugin:link_selected", kwargs={"object_type": "device"}),
            {"server_key": "default", FIELD: tokens},
            HTTP_REFERER=f"http://testserver{second_page}",
        )

        assert not any(row.token for row in _status_rows(first).values())
        for form in ("librenms-link-row", "librenms-link-selected"):
            assert f'id="{form}"' in first.content.decode()
        assert len(tokens) == 1
        assert response.url == second_page
        assert mapping_summary(_fresh(unlinked)) == {"default": {"own": 9962}}

    def test_a_quick_search_row_links_through_the_forms_of_the_unfiltered_first_page(self, linker, server):
        """NetBox quick search swaps its result in as a table fragment, so its Link uses the forms of the empty list."""
        import re

        from netbox_librenms_plugin.confirmation_intent import FIELD

        unlinked = _matched("device", "quick-search-b", server, "198.18.9.3", 9963)
        list_url = reverse("plugins:netbox_librenms_plugin:device_status_list")

        first = linker.get(list_url)
        fragment = linker.get(list_url, {"q": "quick-search-b"}, HTTP_HX_REQUEST="true")
        tokens = re.findall(
            r'form="librenms-link-row" name="confirmation_intent" value="([^"]+)"', fragment.content.decode()
        )
        response = linker.post(
            reverse("plugins:netbox_librenms_plugin:link_selected", kwargs={"object_type": "device"}),
            {"server_key": "default", FIELD: tokens},
            HTTP_REFERER=f"http://testserver{list_url}?q=quick-search-b",
        )

        for form in ("librenms-link-row", "librenms-link-selected"):
            assert f'id="{form}"' in first.content.decode()
        assert len(tokens) == 1
        assert response.status_code == 302
        assert mapping_summary(_fresh(unlinked)) == {"default": {"own": 9963}}

    def test_the_unfiltered_list_renders_without_link_forms_when_no_server_is_usable(self, linker, settings):
        configure_librenms_servers(settings, {})

        response = linker.get(reverse("plugins:netbox_librenms_plugin:device_status_list"))

        assert response.status_code == 200
        assert 'id="librenms-link-selected"' not in response.content.decode()

    def test_an_unreachable_server_is_not_checked_rather_than_not_found(self, linker, server):
        cluster = make_cluster("link-unreachable-list")
        vm = _object("virtualmachine", "unreachable-vm", cluster=cluster)
        give_primary_ip(vm, "198.18.3.1/24")
        server.register("/api/v0/devices/198.18.3.1", {"status": "error"}, status=503)

        response = status_list(linker, "virtualmachine", cluster=cluster.pk)

        assert _status_rows(response)[vm.pk].state == "unreachable"
        assert any(text.startswith("1 of 1 unlinked objects") for text in message_texts(response))


@pytest.mark.django_db
@KINDS
class TestLinkSelected:
    def test_two_tokens_for_one_id_both_refuse_and_an_unrelated_row_applies(self, linker, server, kind):
        cluster = make_cluster(f"bulk-{kind}")
        first = _matched(kind, f"bulk-first-{kind}", server, "198.18.4.1", 9200, cluster=cluster)
        second = _matched(kind, f"bulk-second-{kind}", server, "198.18.4.2", 9200, cluster=cluster)
        third = _matched(kind, f"bulk-third-{kind}", server, "198.18.4.3", 9201, cluster=cluster)
        # Each sync page sees only its own match, so it offers each one a token for the same ID.
        tokens = [page_token(linker, obj) for obj in (first, second, third)]

        response = link_selected(linker, kind, tokens)

        assert response.status_code == 302
        assert mapping_summary(_fresh(first)) == {}
        assert mapping_summary(_fresh(second)) == {}
        assert mapping_summary(_fresh(third)) == {"default": {"own": 9201}}
        assert "claims.shared" in stale_paths(response, first.name)
        assert "claims.shared" in stale_paths(response, second.name)
        assert f"Linked '{third.name}' to LibreNMS ID 9201 on Default LibreNMS." in message_texts(response)

    def test_a_token_whose_object_left_the_change_scope_refuses(self, client, server, kind):
        from users.models import ObjectPermission

        first = _matched(kind, f"scope-first-{kind}", server, "198.18.5.1", 9300)
        second = _matched(kind, f"scope-second-{kind}", server, "198.18.5.2", 9301)
        user = make_user_with_perms(f"scope-writer-{kind}", PERMISSIONS)
        client.force_login(user)
        tokens = [page_token(client, first), page_token(client, second)]
        ObjectPermission.objects.filter(users=user, actions=["change"], object_types__model=kind).delete()
        model = type(first)
        client.force_login(grant(user, "change", model, constraints={"pk": second.pk}))

        response = link_selected(client, kind, tokens)

        assert mapping_summary(_fresh(first)) == {}
        assert mapping_summary(_fresh(second)) == {"default": {"own": 9301}}
        assert "context.subject" in stale_paths(response, first.name)

    def test_the_status_list_offers_the_tokens_that_link_selected_applies(self, linker, server, kind):
        cluster = make_cluster(f"offered-{kind}")
        objects = []
        for index in range(2):
            obj = _object(kind, f"offered-{kind}-{index}", cluster=cluster)
            give_primary_ip(obj, f"198.18.6.{index + 1}/24")
            register_match(server, f"198.18.6.{index + 1}", 9400 + index)
            objects.append(obj)
        filters = {"cluster": cluster.pk} if kind == "virtualmachine" else {"q": f"offered-{kind}"}

        rows = _status_rows(status_list(linker, kind, **filters))
        response = link_selected(linker, kind, [rows[obj.pk].token for obj in objects])

        assert response.status_code == 302
        assert [mapping_summary(_fresh(obj)) for obj in objects] == [
            {"default": {"own": 9400}},
            {"default": {"own": 9401}},
        ]


@pytest.mark.django_db
def test_a_token_of_another_model_refuses_on_the_route_of_a_list(linker, server):
    vm = _matched("virtualmachine", "substituted-vm", server, "198.18.7.1", 9500)
    device = _matched("device", "substituted-device", server, "198.18.7.2", 9501)
    before = domain_state()

    response = link_selected(linker, "device", [page_token(linker, vm)])

    assert domain_state() == before
    assert "context.subject" in stale_paths(response, vm.name)
    assert device.name not in " ".join(message_texts(response))


@transactional_db_with_all_apps()
def test_two_links_of_one_id_never_make_two_owners(server):
    first = _matched("device", "race-first", server, "198.18.8.1", 9600)
    second = _matched("device", "race-second", server, "198.18.8.2", 9600)
    user = make_user_with_perms("race-writer", PERMISSIONS)
    clients = []
    tokens = []
    for obj in (first, second):
        client = Client()
        client.force_login(user)
        clients.append(client)
        tokens.append(page_token(client, obj))

    responses, claim_keys = run_librenms_id_claim_race(
        *(
            (lambda client=client, obj=obj, token=token: link(client, obj, [token]))
            for client, obj, token in zip(clients, (first, second), tokens, strict=True)
        )
    )

    assert len(set(claim_keys)) == 1
    owners = [obj for obj in (first, second) if mapping_summary(_fresh(obj)) == {"default": {"own": 9600}}]
    assert len(owners) == 1
    loser = (first, second)[1 - (first, second).index(owners[0])]
    assert mapping_summary(_fresh(loser)) == {}
    loser_texts = message_texts(responses[(first, second).index(loser)])
    assert "assumptions.owned" in stale_paths(responses[(first, second).index(loser)], loser.name) or (
        TRY_AGAIN_MESSAGE in loser_texts
    )


def _chassis_master(tag, server, address, librenms_id):
    """Return the master of a new two-member chassis, which matches *librenms_id*, and the other member."""
    master, member = make_device(f"{tag}-master"), make_device(f"{tag}-member")
    chassis = make_virtual_chassis(f"{tag}-vc", master, member)
    chassis.master = master
    chassis.save()
    master = _fresh(master)
    give_primary_ip(master, f"{address}/24")
    register_match(server, address, librenms_id, hostname=master.name)
    return master, _fresh(member)


@pytest.mark.django_db
class TestLinkOfAChassisMember:
    def test_the_chassis_sync_device_links(self, linker, server):
        master, _member = _chassis_master("vc-link", server, "198.18.10.1", 9700)

        link(linker, master, [page_token(linker, master)])

        assert mapping_summary(_fresh(master)) == {"default": {"own": 9700}}

    def test_a_member_linked_after_the_offer_makes_the_link_stale(self, linker, server):
        master, member = _chassis_master("vc-other-linked", server, "198.18.10.2", 9710)
        token = page_token(linker, master)
        seed_mapping(member, "default", own=9711)
        before = domain_state()

        response = link(linker, master, [token])

        assert domain_state() == before
        assert "assumptions.chassis.sync_device" in stale_paths(response, master.name)

    def test_a_device_that_joins_a_linked_chassis_after_the_offer_is_stale(self, linker, server):
        device = _matched("device", "vc-joiner", server, "198.18.10.3", 9720)
        token = page_token(linker, device)
        _master, member = _chassis_master("vc-joined", server, "198.18.10.4", 9721)
        seed_mapping(member, "default", own=9722)
        device.virtual_chassis_id, device.vc_position = member.virtual_chassis_id, 3
        device.save()
        before = domain_state()

        response = link(linker, device, [token])

        assert domain_state() == before
        assert "assumptions.chassis" in stale_paths(response, device.name)


@transactional_db_with_all_apps()
def test_a_member_link_that_commits_while_the_link_waits_makes_it_stale(server):
    """The Link locks every chassis member, so a member's link that is open meanwhile commits first."""
    import queue

    from django.db import close_old_connections, connection

    from netbox_librenms_plugin.tests.conftest import bounded_executor
    from netbox_librenms_plugin.tests.lock_conflict_helpers import backend_pid, second_connection, wait_for_lock_wait

    master, member = _chassis_master("vc-race", server, "198.18.10.5", 9730)
    client = Client()
    client.force_login(make_user_with_perms("vc-race-writer", PERMISSIONS))
    token = page_token(client, master)
    pids = queue.Queue()

    def link_master():
        close_old_connections()
        try:
            pids.put(backend_pid(connection))
            return link(client, master, [token])
        finally:
            connection.close()

    with second_connection() as other, bounded_executor(max_workers=1) as executor:
        seed_mapping_row(member, seed_mapping(Device(), "default", own=9731, save=False), connection=other)
        future = executor.submit(link_master)
        try:
            wait_for_lock_wait(other, pids.get(timeout=10))
        finally:
            other.commit()
        response = future.result(timeout=30)

    assert mapping_summary(_fresh(master)) == {}
    assert mapping_summary(_fresh(member)) == {"default": {"own": 9731}}
    assert "assumptions.chassis.sync_device" in stale_paths(response, master.name)


@transactional_db_with_all_apps()
def test_a_member_linked_while_the_page_probes_gets_the_match_without_a_link_offer(server):
    """The page chose the master before its probe; the probe answers after another member got a link."""
    from django.db import connection as server_connection

    master, member = _chassis_master("vc-probe-race", server, "198.18.10.7", 9740)
    record = {"device_id": 9740, "hostname": master.name, "sysName": master.name}

    def link_the_member_then_answer(**_request):
        try:
            seed_mapping(Device.objects.get(pk=member.pk), "default", own=9741)
        finally:
            server_connection.close()
        return 200, {"status": "ok", "devices": [record]}

    server.register("/api/v0/devices/198.18.10.7", link_the_member_then_answer)
    client = Client()
    client.force_login(make_user_with_perms("vc-probe-race-writer", PERMISSIONS))

    link_proposal = page_link(client, master)

    assert (link_proposal["state"], link_proposal["librenms_id"]) == ("match", 9740)
    assert link_proposal["token"] is None
    assert mapping_summary(_fresh(member)) == {"default": {"own": 9741}}
