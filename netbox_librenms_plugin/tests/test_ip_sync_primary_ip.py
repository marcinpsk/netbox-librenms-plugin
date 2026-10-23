"""
Primary-IP, VRF-selection and redirect paths of ``views/sync/ip_addresses.py``.

The primary home for this module is ``test_coverage_sync_views2.py``. These cases live in their
own file because that file sits on a lower branch of the PR stack, where appending to its tail
conflicts on every restack.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    configure_no_librenms_servers,
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis_members,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import ip_address_row, seed_ip_snapshot
from netbox_librenms_plugin.tests.view_test_helpers import assert_update_logged, make_view, missing_pk
from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView


SERVER_KEY = "default"


@pytest.fixture(autouse=True)
def _server(settings):
    configure_default_librenms_server(settings)


def _login(client, username):
    """Sign a real superuser into the test client."""
    client.force_login(make_superuser(username))


def _messages(response, level=None):
    """Return the messages the request actually queued, optionally filtered by level."""
    from django.contrib import messages as django_messages
    from django.contrib.messages import get_messages

    wanted = None if level is None else getattr(django_messages, level.upper())
    return [
        str(message) for message in get_messages(response.wsgi_request) if wanted is None or message.level == wanted
    ]


def _ip_url(obj):
    """Return the IP sync endpoint for a Device or a VirtualMachine."""
    from django.urls import reverse
    from virtualization.models import VirtualMachine

    object_type = "virtualmachine" if isinstance(obj, VirtualMachine) else "device"
    return reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": object_type, "pk": obj.pk},
    )


def _row(address, port_id, interface_name, *, prefix_length=24):
    """Describe one LibreNMS IP row and the port that names its interface."""
    return {
        "address": address,
        "prefix_length": prefix_length,
        "port_id": port_id,
        "interface_name": interface_name,
        "row_id": f"{address}/{prefix_length}",
    }


def _seed(obj, rows, *, management_ip=""):
    """Store the IP snapshot of *rows*, with *management_ip* as the ``ip`` of the device record."""
    seed_ip_snapshot(
        obj,
        [ip_address_row(row["address"], row["port_id"], row["prefix_length"]) for row in rows],
        [
            {"port_id": row["port_id"], "ifName": row["interface_name"], "ifDescr": row["interface_name"]}
            for row in rows
        ],
        device={"ip": management_ip},
        server_key=SERVER_KEY,
    )


def _set_librenms_id(obj, value):
    """Store a LibreNMS id on a real object through the production writer."""
    seed_mapping(obj, SERVER_KEY, own=value)


class _PrimaryIPStealer:
    """Point a device at *address_pk* once its row is locked, as a concurrent request would."""

    def __init__(self, device_pk, address_pk):
        self.device_pk = device_pk
        self.address_pk = address_pk
        self.fired = False

    def __call__(self, execute, sql, params, many, context):
        """Run one out-of-band primary-IP write after the first device row lock."""
        from django.db import connection

        result = execute(sql, params, many, context)
        lowered = sql.lower()
        if not self.fired and "dcim_device" in lowered and "for update" in lowered:
            self.fired = True
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE dcim_device SET primary_ip4_id = %s WHERE id = %s",
                    [self.address_pk, self.device_pk],
                )
        return result


def _sync_payload(rows, *, set_primary=True):
    """Build the POST body the IP sync tab submits for *rows*."""
    row_ids = [row["row_id"] for row in rows]
    data = {"server_key": SERVER_KEY, "select": row_ids}
    data.update({f"vrf_{row_id}": "" for row_id in row_ids})
    if set_primary:
        data["set-primary-ip-toggle"] = "true"
    return data


@pytest.mark.django_db
class TestPrimaryIPFromManagementAddress:
    """The management address decides which synced row becomes the primary IP."""

    def test_management_row_becomes_the_primary_ip(self, client):
        """A synced row matching the LibreNMS management IP is written to primary_ip4."""
        from ipam.models import IPAddress

        device = make_device("ip-primary-set", librenms_cf={SERVER_KEY: {"id": 4201}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9201)
        row = _row("198.18.40.10", 9201, interface.name)
        _seed(device, [row], management_ip="198.18.40.10")
        _login(client, "ip-primary-set-user")

        response = client.post(_ip_url(device), _sync_payload([row]))

        assert response.status_code == 302
        address = IPAddress.objects.get(address=row["row_id"])
        assert address.assigned_object == interface
        device.refresh_from_db()
        assert device.primary_ip4_id == address.pk
        assert any(text.startswith("Set as Primary IP: 198.18.40.10/24") for text in _messages(response, "success"))
        assert_update_logged(device, "primary_ip4", None, address.pk)

    @pytest.mark.parametrize(
        ("record_ip", "is_primary"),
        [("  198.18.47.10  ", True), ("   ", False), (None, False), (12345, False)],
        ids=["padded", "blank", "null", "not-a-string"],
    )
    def test_the_management_address_is_the_stripped_ip_string_of_the_device_record(self, client, record_ip, is_primary):
        """A padded ``ip`` still names the management row; a blank or non-string one names no row."""
        from ipam.models import IPAddress

        device = make_device("ip-primary-record", librenms_cf={SERVER_KEY: {"id": 4211}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9212)
        row = _row("198.18.47.10", 9212, interface.name)
        _seed(device, [row], management_ip=record_ip)
        _login(client, "ip-primary-record-user")

        response = client.post(_ip_url(device), _sync_payload([row]))

        assert response.status_code == 302
        address = IPAddress.objects.get(address=row["row_id"])
        assert address.assigned_object == interface
        device.refresh_from_db()
        assert device.primary_ip4_id == (address.pk if is_primary else None)
        assert any("Set as Primary IP" in text for text in _messages(response)) is is_primary
        assert not any("Primary IP not set" in text for text in _messages(response))

    @pytest.mark.parametrize("superuser", [True, False], ids=["superuser", "writer"])
    def test_a_device_that_fails_validation_fails_the_row_and_changes_nothing(self, client, superuser):
        """The primary IP write validates the whole device, so a device NetBox refuses fails its row."""
        from dcim.models import Device, Interface
        from ipam.models import IPAddress

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        device = make_device(f"ip-primary-invalid-{superuser}", librenms_cf={SERVER_KEY: {"id": 4209}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9210)
        # A rack face without a rack: NetBox refuses the device, whatever field the write changes.
        Device.objects.filter(pk=device.pk).update(face="front")
        row = _row("198.18.41.30", 9210, interface.name)
        _seed(device, [row], management_ip="198.18.41.30")
        if superuser:
            _login(client, "ip-primary-invalid-superuser")
        else:
            client.force_login(
                make_user_with_perms(
                    "ip-primary-invalid-writer",
                    [
                        ("view", Device),
                        ("change", Device),
                        ("view", Interface),
                        ("add", IPAddress),
                        ("change", IPAddress),
                    ],
                )
            )

        response = client.post(_ip_url(device), _sync_payload([row]))

        assert response.status_code == 302
        assert not IPAddress.objects.filter(address=row["row_id"]).exists()
        device.refresh_from_db()
        assert device.primary_ip4_id is None
        [error] = _messages(response, "error")
        assert error.startswith("Failed to sync IP addresses: 198.18.41.30/24 (Primary IP not set: ")
        # The savepoint rolled the create back, so no other message may name the address.
        assert [message for message in _messages(response) if "198.18.41.30" in message] == [error]
        if superuser:
            assert "face: Cannot select a rack face without assigning a rack." in error
        else:
            assert "NetBox refuses the face field (only a superuser sees the message)" in error

    def test_primary_ip_already_pointing_at_the_row_is_left_alone(self, client):
        """A row whose address is already the primary IP reports no primary change."""
        from ipam.models import IPAddress

        device = make_device("ip-primary-idempotent", librenms_cf={SERVER_KEY: {"id": 4202}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9202)
        address = IPAddress.objects.create(address="198.18.41.10/24", assigned_object=interface, status="active")
        device.primary_ip4 = address
        device.save()
        row = _row("198.18.41.10", 9202, interface.name)
        _seed(device, [row], management_ip="198.18.41.10")
        _login(client, "ip-primary-idempotent-user")

        response = client.post(_ip_url(device), _sync_payload([row]))

        assert response.status_code == 302
        assert any("IP addresses already exist" in text for text in _messages(response, "warning"))
        assert not any("Set as Primary IP" in text for text in _messages(response))
        device.refresh_from_db()
        assert device.primary_ip4_id == address.pk

    def test_primary_ip_written_after_the_object_was_read_is_respected(self, client):
        """The locked re-read sees a primary IP written after the sync loaded the object."""
        from ipam.models import IPAddress

        from django.db import connection

        device = make_device("ip-primary-stale", librenms_cf={SERVER_KEY: {"id": 4208}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9209)
        address = IPAddress.objects.create(address="198.18.41.20/24", assigned_object=interface, status="active")
        row = _row("198.18.41.20", 9209, interface.name)
        _seed(device, [row], management_ip="198.18.41.20")
        _login(client, "ip-primary-stale-user")
        stealer = _PrimaryIPStealer(device.pk, address.pk)

        with connection.execute_wrapper(stealer):
            response = client.post(_ip_url(device), _sync_payload([row]))

        assert stealer.fired is True
        assert response.status_code == 302
        assert not any("Set as Primary IP" in text for text in _messages(response))
        device.refresh_from_db()
        assert device.primary_ip4_id == address.pk

    def test_management_row_without_an_interface_reports_primary_not_set(self, client):
        """An unmatched management row is reported apart from an ordinary unmatched row."""
        from ipam.models import IPAddress

        device = make_device("ip-primary-no-interface", librenms_cf={SERVER_KEY: {"id": 4203}})
        management_row = _row("198.18.42.10", 9203, "Ethernet1")
        other_row = _row("198.18.42.11", 9204, "Ethernet2")
        _seed(device, [management_row, other_row], management_ip="198.18.42.10")
        _login(client, "ip-primary-no-interface-user")

        response = client.post(_ip_url(device), _sync_payload([management_row, other_row]))

        assert response.status_code == 302
        assert IPAddress.objects.filter(address__in=["198.18.42.10/24", "198.18.42.11/24"]).count() == 0
        warnings = _messages(response, "warning")
        assert any(text.startswith("Primary IP not set for 198.18.42.10/24") for text in warnings)
        assert any("Skipped (no matching NetBox interface): 198.18.42.11/24" in text for text in warnings)

    def test_primary_ip_refuses_a_management_only_sibling_interface(self, client):
        """A management-only interface on a chassis sibling cannot carry the primary IP."""
        from ipam.models import IPAddress

        from dcim.models import Interface

        _chassis, members = make_virtual_chassis_members("ip-primary-vc")
        viewed, sibling = members
        _set_librenms_id(viewed, 4204)
        management_interface = Interface.objects.create(device=sibling, name="mgmt0", type="1000base-t", mgmt_only=True)
        _set_librenms_id(management_interface, 9205)
        row = _row("198.18.43.10", 9205, management_interface.name)
        _seed(viewed, [row], management_ip="198.18.43.10")
        _login(client, "ip-primary-vc-user")

        response = client.post(_ip_url(viewed), _sync_payload([row]))

        assert response.status_code == 302
        address = IPAddress.objects.get(address="198.18.43.10/24")
        assert address.assigned_object == management_interface
        viewed.refresh_from_db()
        assert viewed.primary_ip4_id is None
        assert any(
            text.startswith("Primary IP not set for 198.18.43.10/24") and "not eligible" in text
            for text in _messages(response, "warning")
        )

    def test_a_lock_conflict_in_the_owner_write_retries_the_batch_and_sets_the_primary_ip(self, client):
        """The owner save meets a 55P03 that its ValidationError handler lets pass, so the batch runs again."""
        from ipam.models import IPAddress

        from netbox_librenms_plugin.tests.lock_conflict_helpers import failing_statement

        device = make_device("ip-primary-busy", librenms_cf={SERVER_KEY: {"id": 4210}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9211)
        row = _row("198.18.44.10", 9211, interface.name)
        _seed(device, [row], management_ip="198.18.44.10")
        _login(client, "ip-primary-busy-user")

        with failing_statement(lambda sql, _params: sql.startswith('UPDATE "dcim_device"'), "55P03") as failed:
            response = client.post(_ip_url(device), _sync_payload([row]))

        assert len(failed) == 1
        assert response.status_code == 302
        address = IPAddress.objects.get(address=row["row_id"])
        device.refresh_from_db()
        assert device.primary_ip4_id == address.pk
        assert any(text.startswith("Set as Primary IP: 198.18.44.10/24") for text in _messages(response, "success"))


@pytest.mark.django_db
class TestIPRowResolution:
    """Per-row VRF resolution against current NetBox state."""

    def test_unknown_vrf_selection_fails_the_row(self, client):
        """A posted VRF the caller cannot resolve fails its row instead of writing a global address."""
        from ipam.models import IPAddress
        from ipam.models import VRF

        device = make_device("ip-vrf-missing", librenms_cf={SERVER_KEY: {"id": 4205}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_librenms_id(interface, 9206)
        row = _row("198.18.44.10", 9206, interface.name)
        _seed(device, [row])
        _login(client, "ip-vrf-missing-user")

        response = client.post(
            _ip_url(device),
            {
                "server_key": SERVER_KEY,
                "select": row["row_id"],
                f"vrf_{row['row_id']}": missing_pk(VRF),
            },
        )

        assert response.status_code == 302
        assert not IPAddress.objects.filter(address="198.18.44.10/24").exists()
        assert any(
            "Selected VRF is no longer available or you do not have permission to view it." in text
            for text in _messages(response, "error")
        )


@pytest.mark.django_db
class TestIPTabRedirect:
    """The IP tab redirect carries whichever server key the request could resolve."""

    def test_bound_client_key_is_used_when_no_post_key_was_resolved(self, live_librenms):
        """With no POST-resolved key the redirect falls back to the bound client's server key."""
        device = make_device("ip-tab-bound-key")
        view = make_view(SyncIPAddressesView, librenms_api=live_librenms.api)

        url = view.get_ip_tab_url(device)

        assert url.endswith(f"?tab=ipaddresses&server_key={live_librenms.api.server_key}")

    def test_redirect_degrades_when_no_server_is_configured(self, client, settings):
        """An unresolvable server and an unusable default still redirect instead of raising."""
        configure_no_librenms_servers(settings)
        device = make_device("ip-tab-no-server")
        _login(client, "ip-tab-no-server-user")

        response = client.post(
            _ip_url(device),
            {"server_key": "removed", "select": "198.18.48.10/24"},
        )

        assert response.status_code == 302
        assert response["Location"].endswith("?tab=ipaddresses")
        assert _messages(response, "error") == ["Selected LibreNMS server is no longer configured."]

    def test_get_object_rejects_an_unknown_object_type(self):
        """``get_object`` fails closed on an object type it cannot map to a model."""
        from django.http import Http404

        device = make_device("ip-object-type")
        view = make_view(SyncIPAddressesView, librenms_api=False)

        with pytest.raises(Http404):
            view.get_object("switch", device.pk)
