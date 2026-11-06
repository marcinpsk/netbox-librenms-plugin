"""Integration tests for device synchronization tables, the row reads and the draft commands."""

import re

import pytest
from django.core.cache import cache
from django.urls import reverse
from ipam.models import VLAN, VLANGroup

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.tables.cables import LibreNMSCableTable, VCCableTable
from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable, VCInterfaceTable
from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable, VCModuleTable
from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis_members,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping, seed_stored_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import (
    port_vlans,
    seed_interfaces_snapshot,
    seed_source_snapshot,
)
from netbox_librenms_plugin.tests.view_test_helpers import (
    draft_command,
    draft_tab,
    make_request,
    make_user_with_perms,
)
from netbox_librenms_plugin.views.object_sync.devices import (
    DeviceCableTableView,
    DeviceInterfaceTableView,
    DeviceModuleTableView,
)


SERVER_KEY = "default"


def _login(client, username):
    client.force_login(make_superuser(username))


def _bind_api(view, request, live_librenms):
    view.request = request
    view._librenms_api = live_librenms.api
    return view


def _port(port_id=4001, name="Ethernet1"):
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifAlias": "Server uplink",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifPhysAddress": "",
        "ifMtu": 1500,
        "ifAdminStatus": "up",
        "ifOperStatus": "up",
    }


def _set_id(obj, value):
    seed_mapping(obj, SERVER_KEY, own=value)


@pytest.mark.django_db
class TestDeviceTables:
    def test_interface_view_reads_real_device_interfaces(self, live_librenms):
        device = make_device("device-interface-table")
        first = make_interface(device, "Ethernet1")
        second = make_interface(device, "Ethernet2")
        request = make_request("get", user=make_superuser("device-interface-table-user"), path="/sync/")
        view = _bind_api(DeviceInterfaceTableView(), request, live_librenms)

        assert list(view.get_interfaces(device)) == [first, second]

    def test_non_chassis_device_uses_standard_interface_table(self, live_librenms):
        device = make_device("device-interface-standard")
        request = make_request("get", user=make_superuser("device-interface-standard-user"), path="/sync/")
        view = _bind_api(DeviceInterfaceTableView(), request, live_librenms)

        table = view.get_table([_port()], device, "ifName")

        assert isinstance(table, LibreNMSInterfaceTable)

    def test_chassis_member_uses_virtual_chassis_interface_table(self, live_librenms):
        _chassis, (device, _sibling) = make_virtual_chassis_members("device-interface-vc")
        request = make_request("get", user=make_superuser("device-interface-vc-user"), path="/sync/")
        view = _bind_api(DeviceInterfaceTableView(), request, live_librenms)

        table = view.get_table([_port()], device, "ifName")

        assert isinstance(table, VCInterfaceTable)

    def test_cable_table_type_follows_virtual_chassis_membership(self, live_librenms):
        plain = make_device("device-cable-standard")
        _chassis, (member, _sibling) = make_virtual_chassis_members("device-cable-vc")
        user = make_superuser("device-cable-table-user")
        request = make_request("get", user=user, path="/sync/")
        view = _bind_api(DeviceCableTableView(), request, live_librenms)

        assert isinstance(view.get_table([], plain), LibreNMSCableTable)
        assert isinstance(view.get_table([], member), VCCableTable)

    def test_module_table_type_and_write_flags_follow_real_user_permissions(self, live_librenms):
        plain = make_device("device-module-standard")
        _chassis, (member, _sibling) = make_virtual_chassis_members("device-module-vc")
        request = make_request("get", user=make_superuser("device-module-table-user"), path="/sync/")
        view = _bind_api(DeviceModuleTableView(), request, live_librenms)

        plain_table = view.get_table([], plain)
        chassis_table = view.get_table([], member)

        assert isinstance(plain_table, LibreNMSModuleTable)
        assert isinstance(chassis_table, VCModuleTable)
        assert plain_table.can_add_module is True
        assert plain_table.can_change_interface is True

    def test_module_table_uses_the_active_render_server(self, live_librenms):
        device = make_device("device-module-active-server")
        request = make_request("get", user=make_superuser("device-module-active-server-user"), path="/sync/")
        view = _bind_api(DeviceModuleTableView(), request, live_librenms)
        view._active_server_key = "secondary"

        table = view.get_table([], device)

        assert table.server_key == "secondary"

    def test_read_only_user_gets_no_module_mutation_flags(self, live_librenms):
        device = make_device("device-module-read-only")
        user = make_user_with_perms("device-module-read-only-user", [], plugin_write=False)
        request = make_request("get", user=user, path="/sync/")
        view = _bind_api(DeviceModuleTableView(), request, live_librenms)

        table = view.get_table([], device)

        assert table.has_write_permission is False
        assert table.can_add_module is False
        assert table.can_change_module is False
        assert table.can_delete_module is False


def _row_url(name, device, row_key):
    """Return the URL of the *name* row read of *device* on the default server."""
    path = reverse(
        f"plugins:netbox_librenms_plugin:{name}",
        kwargs={"object_type": "device", "pk": device.pk, "row_key": row_key},
    )
    return f"{path}?server_key={SERVER_KEY}&interface_name_field=ifName"


def _cell(row, column):
    return dict(re.findall(r'<td[^>]*data-col="([^"]+)"[^>]*>(.*?)</td>', row, re.S))[column]


@pytest.mark.django_db
class TestTheInterfaceRowRead:
    def test_a_malformed_snapshot_payload_returns_a_controlled_404(self, client, live_librenms):
        device = make_device("verify-interface-malformed", librenms_cf={SERVER_KEY: {"id": 141}})
        key = source_cache.snapshot_key(device, Resource.INTERFACES, SERVER_KEY)
        cache.set(key, {"ports": "not-a-list"}, timeout=300)
        _login(client, "verify-interface-malformed-user")

        response = client.get(_row_url("interface_row", device, "4001"))

        assert response.status_code == 404
        # A read never deletes: a refresh can store a new snapshot under the same key.
        assert cache.get(key) == {"ports": "not-a-list"}

    def test_cached_port_returns_a_rendered_real_row(self, client, live_librenms):
        device = make_device("verify-interface-success", librenms_cf={SERVER_KEY: {"id": 142}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_id(interface, 4001)
        seed_interfaces_snapshot(device, [_port()], server_key=SERVER_KEY)
        _login(client, "verify-interface-success-user")

        response = client.get(_row_url("interface_row", device, "4001"))

        assert response.status_code == 200
        assert "Ethernet1" in _cell(response.content.decode(), "name")

    def test_a_duplicate_cached_port_id_offers_no_selection(self, client, live_librenms):
        device = make_device("verify-interface-duplicate", librenms_cf={SERVER_KEY: {"id": 143}})
        seed_interfaces_snapshot(device, [_port(name="Ethernet1"), _port(name="Ethernet2")], server_key=SERVER_KEY)
        _login(client, "verify-interface-duplicate-user")

        shown = draft_tab(client, device, "interfaces", interface_name_field="ifName")

        assert not shown.rows.get("4001", {}).get("data-evidence")

    def test_user_without_device_view_permission_is_rejected_before_lookup(self, client, live_librenms):
        client.force_login(make_user_with_perms("verify-interface-denied-user", []))
        url = reverse(
            "plugins:netbox_librenms_plugin:interface_row",
            kwargs={"object_type": "device", "pk": 999_999_999, "row_key": "4001"},
        )

        response = client.get(url, {"server_key": SERVER_KEY})

        assert response.status_code == 403
        assert "view_device" in response.json()["error"]

    def test_the_vlans_cell_compares_the_real_untagged_assignment(self, client, live_librenms):
        device = make_device("verify-interface-vlan", librenms_cf={SERVER_KEY: {"id": 144}})
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        _set_id(interface, 4001)
        group = VLANGroup.objects.create(name="Interface Group", slug="interface-group", scope=device.site)
        vlan = VLAN.objects.create(vid=3203, name="Users", group=group, status="active")
        interface.mode = "access"
        interface.untagged_vlan = vlan
        interface.save(update_fields=["mode", "untagged_vlan"])
        seed_interfaces_snapshot(device, [{**_port(), **port_vlans("access", 3203)}], server_key=SERVER_KEY)
        _login(client, "verify-interface-vlan-user")

        response = client.get(_row_url("interface_row", device, "4001"))

        assert response.status_code == 200
        assert '<span class="text-success">3203(U)' in _cell(response.content.decode(), "vlans")


@pytest.mark.django_db
class TestTheVlanRowRead:
    @staticmethod
    def _device(tag, librenms_id, vlans):
        device = make_device(tag, librenms_cf={SERVER_KEY: {"id": librenms_id}})
        seed_source_snapshot(device, Resource.VLANS, SERVER_KEY, vlans=vlans)
        return device

    def test_a_global_vlan_with_the_same_name_reads_synced(self, client, live_librenms):
        VLAN.objects.create(vid=3201, name="Application", status="active")
        device = self._device("verify-vlan-global", 145, [{"vlan_vlan": 3201, "vlan_name": "Application"}])
        _login(client, "verify-vlan-global-user")

        response = client.get(_row_url("vlan_row", device, "3201"))

        assert response.status_code == 200
        status = _cell(response.content.decode(), "status")
        assert "text-success" in status and "sync_one" not in status

    def test_a_group_choice_scopes_the_lookup_to_that_group(self, client, live_librenms):
        first = VLANGroup.objects.create(name="First Group", slug="first-group")
        second = VLANGroup.objects.create(name="Second Group", slug="second-group")
        VLAN.objects.create(vid=3202, name="Application", group=first, status="active")
        device = self._device("verify-vlan-group", 146, [{"vlan_vlan": 3202, "vlan_name": "Application"}])
        _login(client, "verify-vlan-group-user")
        shown = draft_tab(client, device, "vlans")

        chosen = draft_command(client, shown, "data-choice-url", row="3202", field="group", value=str(second.pk))
        response = client.get(_row_url("vlan_row", device, "3202"))

        assert chosen.status_code == 204
        assert 'name="sync_one" value="3202"' in _cell(response.content.decode(), "status")

    @pytest.mark.parametrize("fields", [{"field": "group", "value": ""}, {"row": "not-a-vid", "field": "group"}])
    def test_an_invalid_vlan_choice_returns_400(self, client, live_librenms, fields):
        device = self._device(f"verify-vlan-invalid-{len(fields)}", 147, [{"vlan_vlan": 3204, "vlan_name": "v"}])
        _login(client, f"verify-vlan-invalid-{fields.get('row', 'none')}-user")
        shown = draft_tab(client, device, "vlans")

        response = draft_command(client, shown, "data-choice-url", **fields)

        assert response.status_code == 400
        assert draft_tab(client, device, "vlans").table["data-draft-revision"] == "0"

    def test_an_invalid_interface_vlan_group_id_returns_400(self, client, live_librenms):
        device = make_device("verify-interface-vlan-invalid-group", librenms_cf={SERVER_KEY: {"id": 148}})
        seed_interfaces_snapshot(device, [{**_port(), **port_vlans("access", 3204)}], server_key=SERVER_KEY)
        _login(client, "verify-interface-vlan-invalid-group-user")
        shown = draft_tab(client, device, "interfaces", interface_name_field="ifName")

        response = draft_command(client, shown, "data-vlan-groups-url", row="4001", group_3204="not-an-id")

        assert response.status_code == 400


@pytest.mark.django_db
class TestTheInterfaceVlanGroupsCommand:
    @staticmethod
    def _command_url(device):
        path = reverse(
            "plugins:netbox_librenms_plugin:interface_selection_vlan_groups",
            kwargs={"object_type": "device", "pk": device.pk},
        )
        return f"{path}?server_key={SERVER_KEY}&interface_name_field=ifName"

    def test_missing_ports_snapshot_conflicts_without_saving(self, client, live_librenms):
        device = make_device("save-vlan-overrides-miss", librenms_cf={SERVER_KEY: {"id": 152}})
        _login(client, "save-vlan-overrides-miss-user")

        response = client.post(
            self._command_url(device), {"draft_id": "any", "expected_revision": "0", "row": "4001", "group_3206": ""}
        )

        assert response.status_code == 409

    def test_a_snapshot_whose_binding_no_longer_holds_takes_no_groups(self, client, live_librenms):
        device = make_device("save-vlan-overrides-rebound", librenms_cf={SERVER_KEY: {"id": 153}})
        seed_interfaces_snapshot(device, [{**_port(), **port_vlans("access", 3207)}], server_key=SERVER_KEY)
        _login(client, "save-vlan-overrides-rebound-user")
        shown = draft_tab(client, device, "interfaces", interface_name_field="ifName")
        # The write signals run on commit, which this test never reaches, so only the binding refuses the snapshot.
        seed_stored_mapping(device, {SERVER_KEY: {"id": 153, "oob": {"id": 154, "type": "idrac"}}}, save=True)
        assert cache.ttl(source_cache.snapshot_key(device, Resource.INTERFACES, SERVER_KEY)) > 0

        response = draft_command(client, shown, "data-vlan-groups-url", row="4001", group_3207="")

        assert response.status_code == 409
        assert client.get(_row_url("interface_row", device, "4001")).status_code == 404

    def test_a_user_without_plugin_view_is_denied_before_the_view_runs(self, client, live_librenms):
        """LibreNMSPermissionMixin denies in dispatch(), so the view body never runs."""
        device = make_device("save-vlan-overrides-denied", librenms_cf={SERVER_KEY: {"id": 153}})
        seed_interfaces_snapshot(device, [{**_port(), **port_vlans("access", 3207)}], server_key=SERVER_KEY)
        client.force_login(
            make_user_with_perms(
                "save-vlan-overrides-denied-user",
                [("view", type(device))],
                plugin_write=False,
            )
        )

        response = client.post(
            self._command_url(device), {"draft_id": "any", "expected_revision": "0", "row": "4001", "group_3207": ""}
        )

        assert response.status_code == 403


@pytest.mark.django_db
def test_superuser_helper_reactivates_an_existing_user():
    from django.contrib.auth import get_user_model

    get_user_model().objects.filter(is_superuser=True, is_active=True).delete()
    user = get_user_model().objects.create(
        username="reactivated-superuser",
        is_active=False,
        is_superuser=False,
    )

    restored = make_superuser("reactivated-superuser")

    assert restored.pk == user.pk
    assert restored.is_active is True
    assert restored.is_superuser is True
