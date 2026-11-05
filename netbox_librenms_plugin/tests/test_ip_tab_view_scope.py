"""
The IP tab uses only the NetBox objects that the actor may view: the table, the VRF verify and the sync.

Each test stores an IP snapshot, then renders, verifies or syncs it through real requests with real
object permissions. A hidden object must never reach the HTML, the JSON or a signed payload, and a
hidden object must block a row instead of letting it fall back to a visible one.
"""

import json
import re
from ipaddress import ip_interface

import pytest
from dcim.models import Device, Interface
from django.core import signing
from django.urls import reverse
from ipam.models import VRF, IPAddress
from users.models import ObjectPermission
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.server_mappings import mark_migrated
from netbox_librenms_plugin.tests.conftest import (
    configure_librenms_servers,
    transactional_db_with_all_apps,
    make_device,
    make_interface,
    make_ip,
    make_virtual_chassis_members,
    make_vm,
)
from netbox_librenms_plugin.tests.mapping_fixtures import apply_mapping_change, seed_mapping, seed_stored_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import ip_address_row, seed_ip_snapshot
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
from netbox_librenms_plugin.views.sync.ip_addresses import IP_CONFLICT_SIGNING_SALT

pytestmark = pytest.mark.django_db

SERVER_KEY = "default"
# The IP view grant of a limited reader: an address with another status is hidden.
VISIBLE_STATUS = {"status": "active"}
HIDDEN_STATUS = "reserved"
ROW_RE = re.compile(r"<tr[^>]*data-interface=\"([^\"]+)\"[^>]*>(.*?)</tr>", re.S)


def _servers(settings, *keys):
    configure_librenms_servers(
        settings, {key: {"librenms_url": f"https://{key}.librenms.example.com", "api_token": "t"} for key in keys}
    )


def _seed(subject, rows, *, server_key=SERVER_KEY, management_ip=""):
    """Store an IP snapshot of *rows*: ``(address, prefix_length, port_id, port_name)``."""
    ports = {port_id: {"port_id": port_id, "ifName": name, "ifDescr": name} for _a, _p, port_id, name in rows}
    seed_ip_snapshot(
        subject,
        [ip_address_row(address, port_id, prefix_length) for address, prefix_length, port_id, _name in rows],
        list(ports.values()),
        device={"ip": management_ip},
        server_key=server_key,
    )


def _device(name, *interface_names, servers=(SERVER_KEY,)):
    device = make_device(name, librenms_cf={key: {"id": 42} for key in servers})
    return device, [make_interface(device, interface, iface_type="1000base-t") for interface in interface_names]


def _reader(name, *, ip_view=VISIBLE_STATUS, owner=Device, interface=Interface, owner_view=None, interface_view=None):
    """A real user who may sync IPs, with the IP view grant constrained by *ip_view*."""
    user = make_user_with_perms(f"{name}-user", [("view", VRF), ("add", IPAddress), ("change", IPAddress)])
    user = grant(user, "view", owner, constraints=owner_view)
    user = grant(user, "view", interface, constraints=interface_view)
    return grant(user, "view", IPAddress, constraints=ip_view)


def _object_type(subject):
    return "virtualmachine" if isinstance(subject, VirtualMachine) else "device"


def _fragment(client, subject, server_key=SERVER_KEY):
    response = client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": _object_type(subject), "pk": subject.pk, "tab": "ipaddresses"},
        ),
        {"server_key": server_key},
    )
    assert response.status_code == 200
    return response.content.decode()


def _rows(html):
    return dict(ROW_RE.findall(html))


def _selectable(row_html):
    """Return whether the row offers an enabled selection checkbox."""
    boxes = re.findall(r'<input[^>]*name="select"[^>]*>', row_html)
    return any("disabled" not in box for box in boxes)


def _actions(row_html):
    """Return whether the row offers a write action: a row button, a VRF create or a VRF suggestion."""
    return any(marker in row_html for marker in ('name="sync_one"', 'name="create_vrf"', "mdi-lightbulb-on-outline"))


def _verify(client, subject, address, *, row_id=None, vrf_id=None, server_key=SERVER_KEY):
    body = {
        "device_id": subject.pk,
        "object_type": _object_type(subject),
        "ip_address": address,
        "server_key": server_key,
        "vrf_id": vrf_id,
    }
    if row_id is not None:
        body["row_id"] = row_id
    return client.post(
        reverse("plugins:netbox_librenms_plugin:verify_ipaddress"), json.dumps(body), content_type="application/json"
    )


def _sync(client, subject, row_ids, *, server_key=SERVER_KEY, **extra):
    data = {"server_key": server_key, "select": list(row_ids), "set-primary-ip-toggle": "off", **extra}
    return client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
            kwargs={"object_type": _object_type(subject), "pk": subject.pk},
        ),
        data,
        HTTP_HX_REQUEST="true",
    )


def _intents(html):
    """Return each signed conflict intent the response carries, decoded as the writer decodes it."""
    tokens = re.findall(r'name="conflict_intent" value="([^"]*)"', html)
    return [signing.loads(token, salt=IP_CONFLICT_SIGNING_SALT) for token in tokens]


def _hide_ip_again(user, constraints):
    """Narrow the user's IP view grant after the fact, as an administrator would."""
    (permission,) = ObjectPermission.objects.filter(users=user, actions=["view"], object_types__model="ipaddress")
    permission.constraints = constraints
    permission.save()


# =============================================================================
# The table
# =============================================================================


class TestABlockedAddressRendersNoIdentity:
    """Any NetBox address with the row's exact address that the actor cannot view blocks the row."""

    @pytest.mark.parametrize(
        ("address", "prefix_length", "where"),
        [
            pytest.param("198.18.101.10", 24, "target", id="on-the-target-interface"),
            pytest.param("198.18.101.11", 24, "other", id="on-another-interface"),
            pytest.param("198.18.101.12", 24, "vrf", id="in-another-vrf"),
            pytest.param("2001:db8:101::12", 64, "vrf", id="ipv6"),
            pytest.param("::192.0.2.1", 128, "unassigned", id="ipv4-compatible-ipv6"),
        ],
    )
    def test_a_hidden_exact_match_blocks_the_row(self, client, settings, address, prefix_length, where):
        _servers(settings, SERVER_KEY)
        device, (eth1, eth2) = _device(f"ip-scope-hidden-{where}-{prefix_length}", "eth1", "eth2")
        row_id = str(ip_interface(f"{address}/{prefix_length}"))
        assigned = {"target": eth1, "other": eth2}.get(where)
        hidden = make_ip(row_id, assigned_object=assigned, status=HIDDEN_STATUS)
        if where == "vrf":
            hidden.vrf = VRF.objects.create(name=f"VRF {address}")
            hidden.save()
        _seed(device, [(address, prefix_length, 7101, "eth1")])
        client.force_login(_reader(f"ip-scope-hidden-{where}-{prefix_length}"))

        html = _fragment(client, device)

        row = _rows(html)[row_id]
        assert hidden.get_absolute_url() not in html
        assert not _selectable(row)
        assert not _actions(row)

    def test_a_visible_duplicate_of_a_hidden_address_is_blocked_too(self, client, settings):
        _servers(settings, SERVER_KEY)
        device, (eth1,) = _device("ip-scope-mixed", "eth1")
        visible = make_ip("198.18.102.10/24", assigned_object=eth1)
        hidden = make_ip("198.18.102.10/24", status=HIDDEN_STATUS)
        hidden.vrf = VRF.objects.create(name="Mixed VRF")
        hidden.save()
        _seed(device, [("198.18.102.10", 24, 7102, "eth1")])
        client.force_login(_reader("ip-scope-mixed"))

        html = _fragment(client, device)

        row = _rows(html)["198.18.102.10/24"]
        assert visible.get_absolute_url() not in html
        assert hidden.get_absolute_url() not in html
        assert not _selectable(row)

    def test_a_visible_address_in_a_hidden_vrf_is_blocked(self, client, settings):
        _servers(settings, SERVER_KEY)
        device, (eth1,) = _device("ip-scope-hidden-vrf", "eth1")
        secret = VRF.objects.create(name="Secret VRF")
        visible_vrf = VRF.objects.create(name="Visible VRF")
        ip = make_ip("198.18.103.10/24", assigned_object=eth1)
        ip.vrf = secret
        ip.save()
        _seed(device, [("198.18.103.10", 24, 7103, "eth1")])
        user = _reader("ip-scope-hidden-vrf")
        (vrf_grant,) = ObjectPermission.objects.filter(users=user, actions=["view"], object_types__model="vrf")
        vrf_grant.constraints = {"name": visible_vrf.name}
        vrf_grant.save()
        client.force_login(user)

        html = _fragment(client, device)

        row = _rows(html)["198.18.103.10/24"]
        assert ip.get_absolute_url() not in html
        assert f'value="{secret.pk}"' not in row and secret.name not in html
        assert not _selectable(row)

    @pytest.mark.parametrize("hidden_by", ["interface", "owner"])
    def test_a_visible_address_on_a_hidden_interface_is_blocked_and_never_signed(self, client, settings, hidden_by):
        _servers(settings, SERVER_KEY)
        device, (eth1,) = _device(f"ip-scope-held-{hidden_by}", "eth1")
        holder_device = device if hidden_by == "interface" else make_device(f"ip-scope-holder-{hidden_by}")
        holder = make_interface(holder_device, "eth9", iface_type="1000base-t")
        ip = make_ip("198.18.104.10/24", assigned_object=holder)
        _seed(device, [("198.18.104.10", 24, 7104, "eth1")])
        if hidden_by == "interface":
            user = _reader("ip-scope-held-interface", ip_view=None, interface_view={"name": "eth1"})
        else:
            user = _reader("ip-scope-held-owner", ip_view=None, owner_view={"pk": device.pk})
        client.force_login(user)

        html = _fragment(client, device)
        response = _sync(client, device, ["198.18.104.10/24"])

        row = _rows(html)["198.18.104.10/24"]
        assert ip.get_absolute_url() not in html
        assert not _selectable(row)
        assert not _actions(row)
        for payload in _intents(response.content.decode()):
            assert payload["ip_state"]["assigned_object_id"] != str(holder.pk)
        ip.refresh_from_db()
        assert ip.assigned_object_id == holder.pk

    def test_two_readers_with_different_grants_read_one_snapshot_in_both_orders(self, client, settings):
        _servers(settings, SERVER_KEY)
        device, (eth1,) = _device("ip-scope-two-readers", "eth1")
        hidden = make_ip("198.18.105.10/24", assigned_object=eth1, status=HIDDEN_STATUS)
        _seed(device, [("198.18.105.10", 24, 7105, "eth1")])
        limited = _reader("ip-scope-two-readers-limited")
        full = _reader("ip-scope-two-readers-full", ip_view=None)

        seen = []
        for order in ((limited, full), (full, limited)):
            for user in order:
                client.force_login(user)
                seen.append((user, hidden.get_absolute_url() in _fragment(client, device)))

        assert seen == [(limited, False), (full, True), (full, True), (limited, False)]


# =============================================================================
# Port bindings on interfaces or owners the actor cannot view
# =============================================================================


class TestAWithheldBindingNeverFallsBackByName:
    """A port bound on a hidden interface resolves to it and is withheld; a visible namesake never takes the IP."""

    @staticmethod
    def _chassis(tag):
        """Return the page member, its visible eth1, and a hidden member's eth1 bound to port 7201."""
        _chassis, (page, hidden_member) = make_virtual_chassis_members(tag)
        seed_stored_mapping(page, {SERVER_KEY: {"id": 42}}, save=True)
        visible_eth1 = make_interface(page, "eth1", iface_type="1000base-t")
        bound = make_interface(hidden_member, "eth1", iface_type="1000base-t")
        seed_mapping(bound, SERVER_KEY, own=7201)
        _seed(page, [("198.18.106.10", 24, 7201, "eth1")])
        return page, visible_eth1

    def _client(self, client, settings, tag, page, extra=()):
        _servers(settings, SERVER_KEY)
        user = _reader(tag, owner_view={"pk": page.pk})
        for action, model in extra:
            user = grant(user, action, model)
        client.force_login(user)
        return client

    def test_the_table_withholds_the_row(self, client, settings):
        page, visible_eth1 = self._chassis("ip-scope-withheld-table")
        client = self._client(client, settings, "ip-scope-withheld-table", page)

        html = _fragment(client, page)

        row = _rows(html)["198.18.106.10/24"]
        assert visible_eth1.get_absolute_url() not in row
        assert not _selectable(row)
        assert not _actions(row)

    def test_the_verify_withholds_the_row(self, client, settings):
        page, visible_eth1 = self._chassis("ip-scope-withheld-verify")
        client = self._client(client, settings, "ip-scope-withheld-verify", page)

        response = _verify(client, page, "198.18.106.10/24", row_id="198.18.106.10/24")

        assert response.status_code == 200
        row = response.json()["formatted_row"]
        assert row["selectable"] is False
        assert 'name="sync_one"' not in row["status"]
        assert visible_eth1.get_absolute_url() not in response.content.decode()

    @pytest.mark.parametrize("create_missing", ["off", "on"])
    def test_the_sync_refuses_the_row_and_creates_nothing(self, client, settings, create_missing):
        page, visible_eth1 = self._chassis(f"ip-scope-withheld-sync-{create_missing}")
        client = self._client(
            client,
            settings,
            f"ip-scope-withheld-sync-{create_missing}",
            page,
            extra=(("add", Interface), ("change", Interface)),
        )
        interfaces_before = Interface.objects.count()

        response = _sync(client, page, ["198.18.106.10/24"], **{"create-missing-interfaces-toggle": create_missing})

        assert response.status_code in (200, 302)
        assert not IPAddress.objects.filter(address="198.18.106.10/24").exists()
        assert Interface.objects.count() == interfaces_before

    def test_a_binding_counts_only_on_the_server_of_the_snapshot(self, client, settings):
        _servers(settings, "alpha", "beta")
        device, (visible_if, hidden_if) = _device("ip-scope-two-servers", "eth1", "eth9", servers=("alpha", "beta"))
        seed_mapping(hidden_if, "alpha", own=7202)
        seed_mapping(visible_if, "beta", own=7202)
        for server_key in ("alpha", "beta"):
            _seed(device, [("198.18.107.10", 24, 7202, "eth1")], server_key=server_key)
        client.force_login(_reader("ip-scope-two-servers", interface_view={"name": "eth1"}))

        on_beta = _rows(_fragment(client, device, "beta"))["198.18.107.10/24"]
        on_alpha = _rows(_fragment(client, device, "alpha"))["198.18.107.10/24"]
        response = _sync(client, device, ["198.18.107.10/24"], server_key="alpha")

        assert visible_if.get_absolute_url() in on_beta
        assert visible_if.get_absolute_url() not in on_alpha
        assert not _selectable(on_alpha)
        assert response.status_code in (200, 302)
        assert not IPAddress.objects.filter(address="198.18.107.10/24").exists()


# =============================================================================
# The verify
# =============================================================================


class TestTheVerifyAnswersAsTheTable:
    def test_a_hidden_exact_match_is_not_selectable(self, client, settings):
        _servers(settings, SERVER_KEY)
        device, (eth1,) = _device("ip-scope-verify-hidden", "eth1")
        hidden = make_ip("198.18.108.10/24", assigned_object=eth1, status=HIDDEN_STATUS)
        _seed(device, [("198.18.108.10", 24, 7108, "eth1")])
        client.force_login(_reader("ip-scope-verify-hidden"))

        response = _verify(client, device, "198.18.108.10/24")

        assert response.status_code == 200
        assert response.json()["formatted_row"]["selectable"] is False
        assert hidden.get_absolute_url() not in response.content.decode()

    @pytest.mark.parametrize("which", ["hidden", "missing"])
    def test_a_hidden_or_missing_vrf_answers_404(self, client, settings, which):
        _servers(settings, SERVER_KEY)
        device, _interfaces = _device(f"ip-scope-verify-vrf-{which}", "eth1")
        secret = VRF.objects.create(name=f"Secret {which}")
        _seed(device, [("198.18.109.10", 24, 7109, "eth1")])
        user = _reader(f"ip-scope-verify-vrf-{which}")
        (vrf_grant,) = ObjectPermission.objects.filter(users=user, actions=["view"], object_types__model="vrf")
        vrf_grant.constraints = {"name": "Visible VRF"}
        vrf_grant.save()
        client.force_login(user)
        vrf_id = secret.pk if which == "hidden" else secret.pk + 1000

        response = _verify(client, device, "198.18.109.10/24", vrf_id=vrf_id)

        assert response.status_code == 404
        assert secret.name not in response.content.decode()

    def test_a_malformed_vrf_answers_400(self, client, settings):
        _servers(settings, SERVER_KEY)
        device, _interfaces = _device("ip-scope-verify-vrf-malformed", "eth1")
        _seed(device, [("198.18.110.10", 24, 7110, "eth1")])
        client.force_login(_reader("ip-scope-verify-vrf-malformed"))

        assert _verify(client, device, "198.18.110.10/24", vrf_id="abc").status_code == 400

    def test_an_address_without_a_cached_row_is_refused(self, client, settings):
        _servers(settings, SERVER_KEY)
        device, (eth1,) = _device("ip-scope-verify-no-row", "eth1")
        _seed(device, [("198.18.111.10", 24, 7111, "eth1")])
        client.force_login(_reader("ip-scope-verify-no-row"))

        response = _verify(client, device, "198.18.111.99/24")

        assert response.status_code == 400
        assert eth1.get_absolute_url() not in response.content.decode()


# =============================================================================
# The writer
# =============================================================================


class TestTheWriterRefusesWhatTheActorCannotView:
    def test_a_hidden_holder_is_never_unchanged_or_the_primary_ip(self, client, settings):
        _servers(settings, SERVER_KEY)
        device, (eth1,) = _device("ip-scope-writer-unchanged", "eth1")
        hidden = make_ip("198.18.112.10/24", assigned_object=eth1, status=HIDDEN_STATUS)
        _seed(device, [("198.18.112.10", 24, 7112, "eth1")], management_ip="198.18.112.10")
        client.force_login(grant(_reader("ip-scope-writer-unchanged"), "change", Device))

        _sync(client, device, ["198.18.112.10/24"], **{"set-primary-ip-toggle": "on"})

        device.refresh_from_db()
        assert device.primary_ip4_id is None
        hidden.refresh_from_db()
        assert hidden.assigned_object_id == eth1.pk

    def test_a_hidden_same_host_row_is_neither_named_nor_signed(self, client, settings):
        _servers(settings, SERVER_KEY)
        device, (eth1,) = _device("ip-scope-writer-host", "eth1")
        hidden = make_ip("198.18.113.10/16", status=HIDDEN_STATUS)
        _seed(device, [("198.18.113.10", 24, 7113, "eth1")])
        client.force_login(_reader("ip-scope-writer-host"))

        response = _sync(client, device, ["198.18.113.10/24"])

        html = response.content.decode()
        assert "198.18.113.10/16" not in html
        assert all(payload["ip_pk"] != str(hidden.pk) for payload in _intents(html))
        assert not IPAddress.objects.filter(address="198.18.113.10/24").exists()
        hidden.refresh_from_db()
        assert str(hidden.address) == "198.18.113.10/16"

    @transactional_db_with_all_apps()
    def test_an_address_hidden_while_the_sync_waits_for_its_locks_is_never_named_or_signed(
        self, client, settings, monkeypatch
    ):
        """Another transaction hides the other-prefix address after the first read and before the row locks."""
        from netbox_librenms_plugin.tests.lock_conflict_helpers import commit_row_change, second_connection
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        _servers(settings, SERVER_KEY)
        device, (eth1,) = _device("ip-scope-writer-race", "eth1")
        seed_mapping(eth1, SERVER_KEY, own=7120)
        existing = make_ip("198.18.120.10/25")
        _seed(device, [("198.18.120.10", 24, 7120, "eth1")])
        client.force_login(_reader("ip-scope-writer-race"))
        real_lock = SyncIPAddressesView._lock_target_interface
        commits = []

        with second_connection() as other:

            def lock_after_a_concurrent_commit(self, *args, **kwargs):
                if not commits:
                    commit_row_change(other, IPAddress, existing.pk, {"status": HIDDEN_STATUS})
                    commits.append(existing.pk)
                return real_lock(self, *args, **kwargs)

            monkeypatch.setattr(SyncIPAddressesView, "_lock_target_interface", lock_after_a_concurrent_commit)
            response = _sync(client, device, ["198.18.120.10/24"])

        html = response.content.decode()
        assert commits == [existing.pk]
        assert "198.18.120.10/25" not in html
        assert all(payload.get("ip_pk") != str(existing.pk) for payload in _intents(html))
        assert not IPAddress.objects.filter(address="198.18.120.10/24").exists()

    @transactional_db_with_all_apps()
    def test_a_port_bound_on_a_hidden_interface_while_the_sync_waits_for_its_locks_is_refused(
        self, client, settings, monkeypatch
    ):
        """The port first resolves by name to a visible interface; another transaction then binds it to a hidden one."""
        from netbox_librenms_plugin.tests.lock_conflict_helpers import second_connection
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        _servers(settings, SERVER_KEY)
        device, (eth5, hidden) = _device("ip-scope-writer-rebind", "eth5", "private-b")
        _seed(device, [("198.18.130.10", 24, 7130, "eth5")])
        client.force_login(_reader("ip-scope-writer-rebind", interface_view={"name__startswith": "eth"}))
        seed_mapping(hidden, SERVER_KEY, own=7130, save=False)
        bound_value = json.dumps(hidden.custom_field_data)
        real_lock = SyncIPAddressesView._lock_target_interface
        commits = []

        with second_connection() as other:

            def lock_after_a_concurrent_binding(self, *args, **kwargs):
                if not commits:
                    with other.cursor() as cursor:
                        cursor.execute(
                            'UPDATE "dcim_interface" SET "custom_field_data" = %s::jsonb WHERE id = %s',
                            [bound_value, hidden.pk],
                        )
                    other.commit()
                    commits.append(hidden.pk)
                return real_lock(self, *args, **kwargs)

            monkeypatch.setattr(SyncIPAddressesView, "_lock_target_interface", lock_after_a_concurrent_binding)
            response = _sync(client, device, ["198.18.130.10/24"])

        assert commits == [hidden.pk]
        assert "private-b" not in response.content.decode()
        assert not IPAddress.objects.filter(address="198.18.130.10/24").exists()

    @transactional_db_with_all_apps()
    def test_a_hidden_member_that_joins_the_chassis_while_the_sync_waits_for_its_locks_refuses_the_row(
        self, client, settings, monkeypatch
    ):
        """The port first resolves by name on the page; then a hidden device that holds its binding joins the chassis."""
        from dcim.models import VirtualChassis

        from netbox_librenms_plugin.tests.lock_conflict_helpers import commit_row_change, second_connection
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        _servers(settings, SERVER_KEY)
        device, (eth6,) = _device("ip-scope-writer-join", "eth6")
        chassis = VirtualChassis.objects.create(name="ip-scope-writer-join-vc", master=device)
        device.virtual_chassis, device.vc_position = chassis, 1
        device.save()
        joining = make_device("ip-scope-writer-join-hidden")
        seed_mapping(make_interface(joining, "Gi2/0/6", iface_type="1000base-t"), SERVER_KEY, own=7140)
        _seed(device, [("198.18.140.10", 24, 7140, "eth6")])
        client.force_login(_reader("ip-scope-writer-join", owner_view={"pk": device.pk}))
        real_lock = SyncIPAddressesView._lock_interface_owner_scope
        commits = []

        with second_connection() as other:

            def lock_after_a_concurrent_join(self, *args, **kwargs):
                if not commits:
                    commit_row_change(other, Device, joining.pk, {"virtual_chassis_id": chassis.pk, "vc_position": 2})
                    commits.append(joining.pk)
                return real_lock(self, *args, **kwargs)

            monkeypatch.setattr(SyncIPAddressesView, "_lock_interface_owner_scope", lock_after_a_concurrent_join)
            response = _sync(client, device, ["198.18.140.10/24"])

        assert commits == [joining.pk]
        assert joining.name not in response.content.decode()
        assert not IPAddress.objects.filter(address="198.18.140.10/24").exists()

    def test_a_confirmation_signed_before_the_address_was_hidden_is_refused(self, client, settings):
        _servers(settings, SERVER_KEY)
        device, (eth1, eth2) = _device("ip-scope-writer-force", "eth1", "eth2")
        seed_mapping(eth1, SERVER_KEY, own=7114)
        ip = make_ip("198.18.114.10/24", assigned_object=eth2)
        _seed(device, [("198.18.114.10", 24, 7114, "eth1")])
        user = _reader("ip-scope-writer-force", ip_view=None)
        client.force_login(user)
        (intent,) = re.findall(
            r'name="conflict_intent" value="([^"]*)"', _sync(client, device, ["198.18.114.10/24"]).content.decode()
        )
        _hide_ip_again(user, {"status": HIDDEN_STATUS})

        _sync(client, device, [], conflict_intent=intent, force_conflict="198.18.114.10/24")

        ip.refresh_from_db()
        assert ip.assigned_object_id == eth2.pk

    def test_a_forged_hidden_vrf_never_writes_to_global(self, client, settings):
        _servers(settings, SERVER_KEY)
        device, (eth1,) = _device("ip-scope-writer-forged-vrf", "eth1")
        seed_mapping(eth1, SERVER_KEY, own=7119)
        secret = VRF.objects.create(name="Forged VRF")
        _seed(device, [("198.18.119.10", 24, 7119, "eth1")])
        user = _reader("ip-scope-writer-forged-vrf", ip_view=None)
        (vrf_grant,) = ObjectPermission.objects.filter(users=user, actions=["view"], object_types__model="vrf")
        vrf_grant.constraints = {"name": "Visible VRF"}
        vrf_grant.save()
        client.force_login(user)

        response = _sync(client, device, ["198.18.119.10/24"], **{"vrf_198.18.119.10/24": str(secret.pk)})

        assert response.status_code in (200, 302)
        assert not IPAddress.objects.filter(address="198.18.119.10/24").exists()
        assert secret.name not in response.content.decode()

    @pytest.mark.parametrize("constrained", ["view", "add"])
    def test_a_create_outside_the_scope_rolls_back_with_the_primary_ip(self, client, settings, constrained):
        _servers(settings, SERVER_KEY)
        device, (eth1,) = _device(f"ip-scope-writer-create-{constrained}", "eth1")
        _seed(device, [("198.18.115.10", 24, 7115, "eth1")], management_ip="198.18.115.10")
        user = make_user_with_perms(
            f"ip-scope-writer-create-{constrained}-user",
            [("view", Device), ("change", Device), ("view", Interface), ("change", IPAddress)],
        )
        user = grant(user, "view", IPAddress, constraints={"status": HIDDEN_STATUS} if constrained == "view" else None)
        user = grant(user, "add", IPAddress, constraints={"status": HIDDEN_STATUS} if constrained == "add" else None)
        client.force_login(user)

        _sync(client, device, ["198.18.115.10/24"], **{"set-primary-ip-toggle": "on"})

        assert not IPAddress.objects.filter(address="198.18.115.10/24").exists()
        device.refresh_from_db()
        assert device.primary_ip4_id is None

    def test_a_confirmed_vrf_move_out_of_the_view_scope_rolls_back(self, client, settings):
        _servers(settings, SERVER_KEY)
        device, (eth1,) = _device("ip-scope-writer-move", "eth1")
        seen_vrf = VRF.objects.create(name="Seen VRF")
        other_vrf = VRF.objects.create(name="Other VRF")
        ip = make_ip("198.18.116.10/24", assigned_object=eth1)
        ip.vrf = seen_vrf
        ip.save()
        _seed(device, [("198.18.116.10", 24, 7116, "eth1")])
        client.force_login(_reader("ip-scope-writer-move", ip_view={"vrf": seen_vrf.pk}))
        row_vrf = {"vrf_198.18.116.10/24": str(other_vrf.pk)}
        (intent,) = re.findall(
            r'name="conflict_intent" value="([^"]*)"',
            _sync(client, device, ["198.18.116.10/24"], **row_vrf).content.decode(),
        )

        _sync(client, device, [], conflict_intent=intent, force_conflict="198.18.116.10/24")

        ip.refresh_from_db()
        assert ip.vrf_id == seen_vrf.pk


# =============================================================================
# A virtual machine subject
# =============================================================================


class TestAVirtualMachineSubject:
    @staticmethod
    def _vm(tag):
        vm = make_vm(tag)
        seed_stored_mapping(vm, {SERVER_KEY: {"id": 42}}, save=True)
        eth0 = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        hidden = make_ip("198.18.117.10/24", assigned_object=eth0, status=HIDDEN_STATUS)
        _seed(vm, [("198.18.117.10", 24, 7117, "eth0")], management_ip="198.18.117.10")
        return vm, hidden

    def _client(self, client, settings, tag):
        _servers(settings, SERVER_KEY)
        user = _reader(tag, owner=VirtualMachine, interface=VMInterface)
        client.force_login(grant(user, "change", VirtualMachine))
        return client

    def test_the_table_and_the_verify_block_a_hidden_address(self, client, settings):
        vm, hidden = self._vm("ip-scope-vm-render")
        client = self._client(client, settings, "ip-scope-vm-render")

        html = _fragment(client, vm)
        response = _verify(client, vm, "198.18.117.10/24")

        assert hidden.get_absolute_url() not in html
        assert not _selectable(_rows(html)["198.18.117.10/24"])
        assert response.status_code == 200
        assert response.json()["formatted_row"]["selectable"] is False

    def test_the_sync_never_sets_a_hidden_primary_ip(self, client, settings):
        vm, _hidden = self._vm("ip-scope-vm-sync")
        client = self._client(client, settings, "ip-scope-vm-sync")

        _sync(client, vm, ["198.18.117.10/24"], **{"set-primary-ip-toggle": "on"})

        vm.refresh_from_db()
        assert vm.primary_ip4_id is None


# =============================================================================
# The migration card
# =============================================================================


@pytest.mark.parametrize("hidden_by", ["address", "interface"])
def test_the_migration_card_lists_only_visible_addresses_on_visible_interfaces(client, settings, hidden_by):
    _servers(settings, SERVER_KEY)
    donor, (eth1, eth2) = _device(f"ip-scope-move-{hidden_by}", "eth1", "eth2")
    winner = make_device(f"ip-scope-move-winner-{hidden_by}")
    apply_mapping_change(donor, mark_migrated(donor, winner.pk, SERVER_KEY))
    donor.save(update_fields=["custom_field_data"])
    shown = make_ip("198.18.118.10/24", assigned_object=eth1)
    hidden = make_ip(
        "198.18.118.20/24", assigned_object=eth2, status=HIDDEN_STATUS if hidden_by == "address" else "active"
    )
    _seed(donor, [("198.18.118.10", 24, 7118, "eth1")])
    interface_view = {"name": "eth1"} if hidden_by == "interface" else None
    client.force_login(_reader(f"ip-scope-move-{hidden_by}", interface_view=interface_view))

    html = _fragment(client, donor)

    card = html.partition('id="librenms-ipaddress-migrate-table"')[2]
    assert str(shown.address) in card
    assert "198.18.118.20" not in html
    assert reverse("plugins:netbox_librenms_plugin:ipaddress_move_to_winner", args=[hidden.pk]) not in html
