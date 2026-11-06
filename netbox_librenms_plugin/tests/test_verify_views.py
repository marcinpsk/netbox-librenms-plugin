"""
Tests for the cable and module verify endpoints, and for the interface and IP row reads that replaced row verification.

The cable verify view delegates VC device resolution to get_librenms_sync_device() and handles the
None return gracefully (e.g. empty VC members or vc_position type errors). The interface row read
renders one row as the tab does, in the caller's object scope.
"""

import json
import re
from copy import deepcopy

from netbox_librenms_plugin.tests.source_snapshot_helpers import link_row, port_row, seed_cables_snapshot

import pytest
from django.conf import settings
from django.test import RequestFactory, override_settings
from django.urls import reverse

from netbox_librenms_plugin.tests._html_helpers import open_tags
from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    configured_server_key,
    make_superuser,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import (
    interface_rows,
    port_pair,
    port_vlans,
    seed_interfaces_snapshot,
)

from netbox_librenms_plugin.tests.view_test_helpers import bind_relationship, draft_command, draft_tab
from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView


pytestmark = pytest.mark.django_db


def _make_request(body: dict):
    """Create a real JSON POST request with a permitted user."""
    request = RequestFactory().post("/verify/", data=json.dumps(body), content_type="application/json")
    request.user = make_superuser()
    return request


def _verify_superuser(tag):
    """A real superuser so the object-perm gate passes and restrict() resolves the real device."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_superuser(username=f"vv-{tag}", email="", password="x")


def _real_vc_device(tag, name=None):
    """A real Device that belongs to a VirtualChassis (so post() takes the VC sync-resolution branch)."""
    from dcim.models import VirtualChassis

    device = _make_gate_device(name=name or f"vc-{tag}")
    vc = VirtualChassis.objects.create(name=f"VV-VC-{tag}")
    device.virtual_chassis = vc
    device.vc_position = 1
    device.save()
    return device


def _real_verify_view(view_cls, body, user, *, server_key=None):
    """Build a real verify view, request, user, and API client."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from django.test import RequestFactory

    server_key = server_key or configured_server_key()
    body = {**body, "server_key": server_key}
    view = view_cls()
    view._librenms_api = LibreNMSAPI(server_key=server_key)
    request = RequestFactory().post("/verify/", data=json.dumps(body), content_type="application/json")
    request.user = user
    view.setup(request)
    return view, request


# ---------------------------------------------------------------------------
# SingleCableVerifyView
# ---------------------------------------------------------------------------
class TestSingleCableVerifyView:
    """SingleCableVerifyView.post() VC resolution and None guard (real device, real gate/restrict)."""

    def _make_view(self):
        view = SingleCableVerifyView()
        view.setup(_make_request({}))
        return view

    @pytest.mark.django_db
    def test_vc_no_resolvable_sync_device_returns_an_empty_row(self):
        """Verify a virtual chassis without a resolvable sync member returns the safe empty row."""
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
        from netbox_librenms_plugin.server_selection import get_librenms_sync_device

        _virtual_chassis, (device, _sibling) = make_virtual_chassis_members("cbl-nosync")
        device.vc_position = None
        device.save(update_fields=["vc_position"])
        assert get_librenms_sync_device(device, server_key=configured_server_key()) is None

        view, request = _real_verify_view(
            SingleCableVerifyView, {"device_id": device.pk, "row_id": "42"}, _verify_superuser("cbl-nosync")
        )
        response = view.post(request)

        data = json.loads(response.content)
        assert data["status"] == "success"
        assert data["formatted_row"]["cable_status"] == "Missing Ports"
        assert data["formatted_row"]["local_port"] == ""

    def test_no_device_id_returns_empty_row(self):
        """Missing device_id: returns default empty formatted_row."""
        view = self._make_view()
        request = _make_request({"local_port_id": "42"})
        response = view.post(request)

        data = json.loads(response.content)
        assert data["status"] == "success"
        assert data["formatted_row"]["cable_status"] == "Missing Ports"


def _snapshot_port(port_id, name, **fields):
    """Return one raw LibreNMS port row, as an interfaces snapshot holds it."""
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifAlias": "",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifPhysAddress": "",
        "ifMtu": 1500,
        "ifAdminStatus": "up",
        **fields,
    }


def _seed_parent_and_child(subject, child, parent, *, server_key="default", child_type="l2vlan", pair=(10, 20)):
    """Store the raw ports 10 (*child*) and 20 (*parent*) and one port_stack pair; assert the derived parent edge."""
    seed_interfaces_snapshot(
        subject,
        [_snapshot_port(10, child.name, ifType=child_type), _snapshot_port(20, parent.name)],
        [port_pair(*pair)],
        server_key=server_key,
    )
    assert interface_rows(subject, server_key=server_key)["port_stack_relationships"]["sub_interfaces"] == {10: 20}


def _signed_in_client(settings, user):
    """Return a client signed in as *user*, with the default LibreNMS server configured."""
    from django.test import Client

    configure_default_librenms_server(settings)
    client = Client()
    client.force_login(user)
    return client


def _mapped(device, librenms_id=4500):
    """Map *device* to the default server, so its sync tab and its row reads answer."""
    seed_mapping(device, "default", own=librenms_id)
    return device


def _shown_row(client, owner, port_id, server_key="default", **query):
    """Return the ``<tr>`` of LibreNMS port *port_id* as the interfaces tab renders it."""
    shown = draft_tab(client, owner, "interfaces", server_key=server_key, **{"interface_name_field": "ifName", **query})
    for match in re.finditer(r"<tr\b[^>]*>.*?</tr>", shown.html, re.S):
        if f'data-row-key="{port_id}"' in match.group(0):
            return match.group(0)
    raise AssertionError(f"the tab renders no row {port_id}")


def _row_read(client, owner, port_id, server_key="default", **query):
    """Return the ``<tr>`` that the row read of LibreNMS port *port_id* answers, from the URL the tab renders on it."""
    row = _shown_row(client, owner, port_id, server_key, **query)
    (tr,) = open_tags(row, "tr")
    response = client.get(tr["hx-get"])
    assert response.status_code == 200, response.content
    return response.content.decode()


def _cell(row, column):
    """Return the markup of the *column* cell of a rendered row."""
    return dict(re.findall(r'<td[^>]*data-col="([^"]+)"[^>]*>(.*?)</td>', row, re.S))[column]


def _row_attrs(row):
    (tr,) = open_tags(row, "tr")
    return tr


# ---------------------------------------------------------------------------
# The interface row read (the verify endpoint's successor): scope, resolution and write offers
# ---------------------------------------------------------------------------
class TestTheInterfaceRowRead:
    """The row read renders one interface row as the tab does, with the caller's scope and the row's own resolution."""

    @pytest.mark.django_db
    @pytest.mark.parametrize("source", ["host", "oob"])
    def test_the_row_hides_vlan_metadata_from_a_user_without_ipam_view_rights(self, settings, source):
        """The gate only checks view_device, so the IPAM reads must be scoped to the caller."""
        from dcim.models import Device, Site
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.models import LibreNMSSettings
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        device = _mapped(make_device("verify-ipam-scope"))
        group = VLANGroup.objects.create(
            name="Verify Hidden VLAN Group",
            slug="verify-hidden-vlan-group",
            scope_type=ContentType.objects.get_for_model(Site),
            scope_id=device.site.pk,
        )
        VLAN.objects.create(vid=100, name="Verify Hidden VLAN", group=group, status="active")
        port = _snapshot_port(41, "Ethernet1", **port_vlans("access", 100))
        if source == "oob":
            seed_mapping(device, "default", oob=4141)
            seed_interfaces_snapshot(device, [], oob_ports=[port])
        else:
            seed_interfaces_snapshot(device, [port])
        restricted_user = make_user_with_perms("verify-ipam-scope-user", [("view", Device)], plugin_write=False)
        restricted_user = grant(restricted_user, "view", LibreNMSSettings)

        visible = _cell(
            _row_read(_signed_in_client(settings, _verify_superuser("ipam-scope-admin")), device, 41), "vlans"
        )
        restricted = _cell(_row_read(_signed_in_client(settings, restricted_user), device, 41), "vlans")

        # The group name reaches the tooltip only for a caller who may view the group.
        assert group.name in visible
        assert group.name not in restricted
        assert "Not in NetBox" in restricted

    @pytest.mark.django_db
    def test_the_row_does_not_name_match_an_interface_bound_to_another_port(self, settings):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = _mapped(make_device("verify-conflicting-port-id"))
        wrong_interface = make_interface(device, "Ethernet1")
        seed_mapping(wrong_interface, own=30, save=False)
        wrong_interface.save()
        seed_interfaces_snapshot(device, [_snapshot_port(20, "Ethernet1")])

        row = _row_read(_signed_in_client(settings, _verify_superuser("conflicting-port-id")), device, 20)

        assert "text-danger" in _cell(row, "name")

    @pytest.mark.django_db
    def test_a_member_choice_gives_the_row_the_chosen_members_vlan_group(self, settings):
        from django.contrib.contenttypes.models import ContentType
        from dcim.models import Rack
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.selection_draft import read_evidence
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
        from netbox_librenms_plugin.views.base.interfaces_view import INTERFACE_TAB

        _virtual_chassis, (member1, member2) = make_virtual_chassis_members("verify-vlan-owner")
        _mapped(member1)
        rack1 = Rack.objects.create(name="Verify VLAN Rack 1", site=member1.site, status="active")
        rack2 = Rack.objects.create(name="Verify VLAN Rack 2", site=member1.site, status="active")
        member1.rack = rack1
        member2.rack = rack2
        member1.save()
        member2.save()
        rack_type = ContentType.objects.get_for_model(Rack)
        group1 = VLANGroup.objects.create(
            name="Verify VLAN Group 1",
            slug="verify-vlan-group-1",
            scope_type=rack_type,
            scope_id=rack1.pk,
        )
        group2 = VLANGroup.objects.create(
            name="Verify VLAN Group 2",
            slug="verify-vlan-group-2",
            scope_type=rack_type,
            scope_id=rack2.pk,
        )
        VLAN.objects.create(vid=100, name="Verify Rack 1 VLAN", group=group1, status="active")
        VLAN.objects.create(vid=100, name="Verify Rack 2 VLAN", group=group2, status="active")
        seed_interfaces_snapshot(member1, [_snapshot_port(10, "Ethernet2", **port_vlans("access", 100))])
        client = _signed_in_client(settings, _verify_superuser("vlan-owner"))
        shown = draft_tab(client, member1, "interfaces", interface_name_field="ifName")

        chosen = draft_command(client, shown, "data-choice-url", row="10", field="member", value=str(member2.pk))
        row = _row_read(client, member1, 10)

        assert chosen.status_code == 204
        assert read_evidence(INTERFACE_TAB, _row_attrs(row)["data-evidence"])["vlan_groups"] == {"100": str(group2.pk)}

    @pytest.mark.django_db
    def test_the_row_normalizes_relationship_port_ids(self, settings):
        """A port_stack pair that names a port id as a string still gives the parent edge."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = _mapped(make_device("verify-string-relationship-key"))
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        seed_mapping(child, own=10, save=False)
        seed_mapping(parent, own=20, save=False)
        child.save()
        parent.save()
        _seed_parent_and_child(device, child, parent, pair=("10", 20))

        row = _row_read(_signed_in_client(settings, _verify_superuser("string-relationship-key")), device, 10)

        assert parent.name in _cell(row, "parent")
        attrs = _row_attrs(row)
        assert (attrs["data-parent-port-id"], attrs["data-parent-name"]) == ("20", parent.name)
        assert (attrs["data-member-of-lag"], attrs["data-lag-name"]) == ("", "")
        assert (attrs["data-bridge-port-id"], attrs["data-bridge-name"]) == ("", "")

    @pytest.mark.django_db
    def test_the_table_and_the_row_read_resolve_the_same_stable_id_row(self, settings):
        """The table and the row read must prefer the same stable-ID interface."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        server_key = configure_default_librenms_server(settings)
        device = _mapped(make_device("verify-table-resolution"))
        stable_match = make_interface(device, "NetBoxStable")
        name_candidate = make_interface(device, "Ethernet1")
        seed_mapping(stable_match, server_key, own=10, save=False)
        stable_match.mtu = 9000
        stable_match.save()
        name_candidate.mtu = 1500
        name_candidate.save()
        seed_interfaces_snapshot(device, [_snapshot_port(10, "Ethernet1", ifMtu=9000)], server_key=server_key)
        client = _signed_in_client(settings, _verify_superuser("table-resolution"))

        table_row = _shown_row(client, device, 10)
        read_row = _row_read(client, device, 10)

        assert _cell(read_row, "name") == _cell(table_row, "name")
        assert _cell(read_row, "mtu") == _cell(table_row, "mtu")
        assert "text-success" in _cell(read_row, "mtu")

    @pytest.mark.django_db
    def test_the_row_keeps_a_view_only_interface_match_without_offering_a_relationship_write(self, settings):
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        device = _mapped(make_device("verify-view-only-match"))
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        seed_mapping(child, own=10, save=False)
        seed_mapping(parent, own=20, save=False)
        child.save()
        parent.save()
        user = make_user_with_perms(
            "verify-view-only-match",
            [("view", Device), ("view", Interface)],
        )
        _seed_parent_and_child(device, child, parent)

        row = _row_read(_signed_in_client(settings, user), device, 10)

        assert "text-success" in _cell(row, "name")
        assert "parent-sync-btn" not in _cell(row, "parent")

    @pytest.mark.django_db
    def test_plugin_read_only_user_never_receives_relationship_write_button(self, settings):
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.models import LibreNMSSettings
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        device = _mapped(make_device("verify-plugin-read-only"))
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        seed_mapping(child, own=10, save=False)
        seed_mapping(parent, own=20, save=False)
        child.save()
        parent.save()
        user = make_user_with_perms(
            "verify-plugin-read-only",
            [("view", Device), ("view", Interface), ("change", Interface)],
            plugin_write=False,
        )
        user = grant(user, "view", LibreNMSSettings)
        _seed_parent_and_child(device, child, parent)
        client = _signed_in_client(settings, user)

        assert "parent-sync-btn" not in _cell(_shown_row(client, device, 10), "parent")
        assert "parent-sync-btn" not in _cell(_row_read(client, device, 10), "parent")

    @pytest.mark.django_db
    @pytest.mark.parametrize("migrated", [False, True], ids=["active-page", "migrated-page"])
    def test_a_virtual_chassis_member_row_uses_the_page_mode(self, client, settings, migrated):
        """A row bound on another member keeps the page mode and targets that member."""
        from netbox_librenms_plugin.server_mappings import mark_migrated
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_virtual_chassis_members
        from netbox_librenms_plugin.tests.mapping_fixtures import apply_mapping_change

        _virtual_chassis, (page_device, selected_device) = make_virtual_chassis_members("verify-migrated-page")
        _mapped(page_device)
        winner = make_device("verify-migrated-winner")
        child = make_interface(selected_device, "Ethernet2.100", iface_type="virtual")
        parent = make_interface(selected_device, "Ethernet2")
        seed_mapping(child, own=10, save=False)
        seed_mapping(parent, own=20, save=False)
        child.save()
        parent.save()
        if migrated:
            apply_mapping_change(page_device, mark_migrated(page_device, winner.pk, "default"))
            page_device.save()
        _seed_parent_and_child(page_device, child, parent)
        configure_default_librenms_server(settings)
        client.force_login(_verify_superuser("migrated-page"))
        fragment = reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": page_device.pk, "tab": "interfaces"},
        )

        response = client.get(fragment, {"server_key": "default", "interface_name_field": "ifName"})

        assert response.status_code == 200, response.content
        row = re.search(r'<tr\b[^>]*data-port-id="10".*?</tr>', response.content.decode(), re.S).group(0)
        parent_html = _cell(row, "parent")
        if migrated:
            assert "parent-sync-btn" not in parent_html
        else:
            assert "parent-sync-btn" in parent_html
            assert f"/device/{selected_device.pk}/sync-interface-parent/" in parent_html

    @pytest.mark.django_db
    def test_the_tab_and_the_row_read_do_not_expose_inaccessible_vc_members(self, settings):
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        _virtual_chassis, (page_device, selected_device, hidden_device) = make_virtual_chassis_members(
            "verify-member-scope",
            count=3,
        )
        _mapped(page_device)
        user = make_user_with_perms("verify-member-scope", [])
        user = grant(user, "view", Device, constraints={"pk": page_device.pk})
        user = grant(user, "view", Device, constraints={"pk": selected_device.pk})
        seed_interfaces_snapshot(page_device, [_snapshot_port(10, "Ethernet2")])
        client = _signed_in_client(settings, user)

        tab = draft_tab(client, page_device, "interfaces", interface_name_field="ifName").html
        row = _row_read(client, page_device, 10)

        for body in (tab, row):
            assert hidden_device.name not in body
            # Match the pk as a device reference: a bare number can match any rendered value.
            assert f"/dcim/devices/{hidden_device.pk}/" not in body
            assert f'value="{hidden_device.pk}"' not in body
        assert f'value="{selected_device.pk}"' in tab

    @pytest.mark.django_db
    def test_hidden_related_owner_stays_unavailable_through_the_row_and_the_inline_post(self, settings):
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.tests.conftest import make_interface, make_virtual_chassis_members
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms, make_view
        from netbox_librenms_plugin.tests.view_test_helpers import post as call_post
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        _virtual_chassis, (page_device, hidden_parent_device) = make_virtual_chassis_members(
            "verify-hidden-parent-owner"
        )
        child = make_interface(page_device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(hidden_parent_device, "Ethernet2")
        server_key = configure_default_librenms_server(settings)
        _mapped(page_device)
        seed_mapping(child, server_key, own=10, save=False)
        seed_mapping(parent, server_key, own=20, save=False)
        child.save()
        parent.save()
        user = make_user_with_perms("verify-hidden-parent-owner", [])
        user = grant(user, "view", Device, constraints={"pk": page_device.pk})
        user = grant(user, "change", Interface, constraints={"pk": child.pk})
        user = grant(user, "view", Interface, constraints={"pk": parent.pk})
        # The names share no base, so only the L3 ifType of the child makes the pair a parent edge.
        _seed_parent_and_child(page_device, child, parent, server_key=server_key, child_type="ipForward")
        client = _signed_in_client(settings, user)

        assert "parent-sync-btn" not in _cell(_shown_row(client, page_device, 10), "parent")
        assert "parent-sync-btn" not in _cell(_row_read(client, page_device, 10), "parent")

        relationship_request = make_request(
            "post", {"port_id": "10", "parent_port_id": "20", "server_key": server_key}, user=user
        )
        relationship_response = call_post(
            make_view(SyncInterfaceParentView, relationship_request),
            bind_relationship(relationship_request, page_device, server_key=server_key),
            object_type="device",
            object_id=page_device.pk,
        )

        assert relationship_response.status_code == 404, relationship_response.content
        child.refresh_from_db()
        assert child.parent_id is None

    @pytest.mark.django_db
    def test_view_only_non_lag_target_never_receives_lag_promotion_button(self, settings):
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms, make_view
        from netbox_librenms_plugin.tests.view_test_helpers import post as call_post
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        server_key = configure_default_librenms_server(settings)
        device = _mapped(make_device("verify-view-only-lag-target"))
        member = make_interface(device, "Ethernet1")
        aggregate = make_interface(device, "Port-Channel1", iface_type="other")
        seed_mapping(member, server_key, own=10, save=False)
        seed_mapping(aggregate, server_key, own=20, save=False)
        member.save()
        aggregate.save()
        user = make_user_with_perms("verify-view-only-lag-target", [("view", Device)])
        user = grant(user, "change", Interface, constraints={"pk": member.pk})
        user = grant(user, "view", Interface, constraints={"pk": aggregate.pk})
        seed_interfaces_snapshot(
            device,
            [_snapshot_port(10, member.name), _snapshot_port(20, aggregate.name, ifType="ieee8023adLag")],
            [port_pair(10, 20)],
            server_key=server_key,
        )
        client = _signed_in_client(settings, user)

        table_html = _cell(_shown_row(client, device, 10), "parent")
        read_html = _cell(_row_read(client, device, 10), "parent")
        inline_request = make_request(
            "post", {"port_id": "10", "lag_port_id": "20", "server_key": server_key}, user=user
        )
        inline_response = call_post(
            make_view(SyncInterfaceLagView, inline_request),
            bind_relationship(inline_request, device, server_key=server_key),
            object_type="device",
            object_id=device.pk,
        )

        # Pin the refusal status: "!= 200" also passes for a 500 from an unrelated crash, and the
        # state assertions below hold after any failed request because nothing was written.
        assert inline_response.status_code == 403, inline_response.content
        assert "lag-sync-btn" not in table_html
        assert "lag-sync-btn" not in read_html
        member.refresh_from_db()
        aggregate.refresh_from_db()
        assert member.lag_id is None
        assert aggregate.type != "lag"

    @pytest.mark.django_db
    def test_the_row_read_falls_back_when_the_interface_name_field_is_unknown(self, settings):
        from netbox_librenms_plugin.tests.conftest import make_device

        device = _mapped(make_device("verify-malformed-name-field"))
        seed_interfaces_snapshot(device, [_snapshot_port(10, "Ethernet1", ifDescr="GigabitEthernet1")])

        row = _row_read(
            _signed_in_client(settings, _verify_superuser("malformed-name-field")),
            device,
            10,
            interface_name_field="bogus",
        )

        # A bogus field name falls back to the default name column, never to another column.
        assert "Ethernet1" in _cell(row, "name")
        assert "GigabitEthernet1" not in _cell(row, "name")


# ---------------------------------------------------------------------------
# The IP row read: object-permission gate and server namespace (real DB, real has_perm)
# ---------------------------------------------------------------------------
def _make_gate_device(name="ipgate-dev"):
    """Create a real Device for the IP row read object-permission gate tests."""
    from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

    mfr, _ = Manufacturer.objects.get_or_create(name="IPGate-Mfr", slug="ipgate-mfr")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="IPGate-DT", slug="ipgate-dt")
    role, _ = DeviceRole.objects.get_or_create(name="IPGate-Role", slug="ipgate-role")
    site, _ = Site.objects.get_or_create(name="IPGate-Site", slug="ipgate-site")
    return Device.objects.create(name=name, device_type=dt, role=role, site=site, status="active")


def _make_gate_vm(name="ipgate-vm"):
    """Create a real VirtualMachine for the IP row read object-permission gate tests."""
    from virtualization.models import Cluster, ClusterType, VirtualMachine

    ct, _ = ClusterType.objects.get_or_create(name="IPGate-CT", slug="ipgate-ct")
    cluster, _ = Cluster.objects.get_or_create(name="IPGate-Cluster", type=ct)
    return VirtualMachine.objects.create(name=name, cluster=cluster, status="active")


def _ip_row_url(owner, row_key="1.2.3.4/24", server_key="default"):
    """Return the URL of the IP row read of *owner*, as the tab renders it on each row."""
    path = reverse(
        "plugins:netbox_librenms_plugin:ip_row",
        kwargs={"object_type": owner._meta.model_name, "pk": owner.pk, "row_key": row_key},
    )
    return f"{path}?server_key={server_key}"


@pytest.mark.django_db
class TestTheIPRowReadObjectPermissionGate:
    """The IP row read requires the view permission of the URL's subject model before it reveals the subject."""

    @staticmethod
    def _user(username, *models):
        """Return a user with the plugin's permissions, so only the object gate can refuse, and *models* to view."""
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        return make_user_with_perms(username, [("view", model) for model in models])

    @staticmethod
    def _get(user, owner):
        from django.test import Client

        client = Client()
        client.force_login(user)
        return client.get(_ip_row_url(owner))

    def test_user_without_view_device_is_denied(self):
        """A user lacking dcim.view_device is refused (403) before any object data is read."""
        device = _make_gate_device()

        response = self._get(self._user("ipgate-noperm"), device)

        assert response.status_code == 403
        # The denied response must NOT leak the hidden device's name.
        assert device.name.encode() not in response.content

    def test_user_with_view_device_passes_gate(self):
        """A user granted dcim.view_device clears the gate (non-403)."""
        from dcim.models import Device

        device = _make_gate_device(name="ipgate-dev2")

        assert self._get(self._user("ipgate-perm", Device), device).status_code != 403

    def test_device_view_only_user_denied_vm_data(self):
        """A user with only dcim.view_device must NOT read VirtualMachine data through the row read."""
        from dcim.models import Device

        vm = _make_gate_vm()

        response = self._get(self._user("ipgate-devonly", Device), vm)

        assert response.status_code == 403
        assert vm.name.encode() not in response.content

    def test_vm_view_user_passes_gate_for_vm(self):
        """A user granted virtualization.view_virtualmachine clears the gate for a VM target."""
        from virtualization.models import VirtualMachine

        vm = _make_gate_vm(name="ipgate-vm2")

        assert self._get(self._user("ipgate-vmperm", VirtualMachine), vm).status_code != 403


@pytest.mark.django_db
class TestTheIPRowReadServerKeyCacheNamespace:
    """An unconfigured server key cannot select an arbitrary IP cache namespace for the row read."""

    def test_a_forged_server_key_reads_no_snapshot(self):
        from django.contrib.auth import get_user_model
        from django.test import Client

        from netbox_librenms_plugin.tests.source_snapshot_helpers import ip_address_row, seed_ip_snapshot

        device = _make_gate_device(name="ipgate-srvkey")
        for own, namespace in enumerate(("default", "alpha", "evil-namespace"), start=4301):
            seed_mapping(device, namespace, own=own, save=False)
        device.save()
        # The address is on two ports in each snapshot, so only the snapshot that holds the row key answers.
        for first_port, namespace in ((1, "default"), (3, "alpha"), (5, "evil-namespace")):
            port_ids = (first_port, first_port + 1)
            seed_ip_snapshot(
                device,
                [ip_address_row("1.2.3.4", port_id) for port_id in port_ids],
                [{"port_id": port_id, "ifName": f"eth{port_id}", "ifDescr": f"eth{port_id}"} for port_id in port_ids],
                server_key=namespace,
            )
        client = Client()
        client.force_login(get_user_model().objects.create_user(username="ipgate-srvkey-su", is_superuser=True))

        servers = {
            "alpha": {"librenms_url": "https://a.example.com", "api_token": "t"},
            "beta": {"librenms_url": "https://b.example.com", "api_token": "t"},
        }
        with _servers_settings(servers):
            forged_own_row = client.get(_ip_row_url(device, "1.2.3.4/24@5", "evil-namespace"))
            configured_row = client.get(_ip_row_url(device, "1.2.3.4/24@3", "alpha"))

        # The forged key never reads its own namespace.
        assert forged_own_row.status_code == 404
        # Positive control: a configured key reads its own snapshot.
        assert configured_row.status_code == 200, configured_row.content


# ---------------------------------------------------------------------------
# server_key guards on the module verify and the interface row read
# (real DB, only the plugin-config boundary patched)
# ---------------------------------------------------------------------------
_GUARD_SERVERS = {
    "alpha": {"librenms_url": "https://a.example.com", "api_token": "t"},
    "beta": {"librenms_url": "https://b.example.com", "api_token": "t"},
}


def _servers_settings(servers):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = servers
    return override_settings(PLUGINS_CONFIG=plugin_config)


def _make_vc_member_device(name="srvkey-vc-dev"):
    """A real VC-member Device: the unguarded views route its server_key into cf_dict.get()."""
    from dcim.models import VirtualChassis

    device = _make_gate_device(name=name)
    vc = VirtualChassis.objects.create(name=f"vc-{name}")
    device.virtual_chassis = vc
    device.vc_position = 1
    # A dict-form librenms_id mapping: get_librenms_sync_device reads this dict with the
    # caller's server_key, so an unhashable key actually reaches cf_dict.get(["a"]).
    seed_mapping(device, "alpha", own=4242, save=False)
    device.save()
    return device


def _superuser(username):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username=username, password="x", is_superuser=True)
    return get_user_model().objects.get(pk=user.pk)


def _json_post(url, body, user):
    request = RequestFactory().post(url, data=json.dumps(body), content_type="application/json")
    request.user = user
    return request


def _interface_row_url(owner, row_key, server_key):
    """Return the URL of the interface row read of *owner* on *server_key*."""
    path = reverse(
        "plugins:netbox_librenms_plugin:interface_row",
        kwargs={"object_type": owner._meta.model_name, "pk": owner.pk, "row_key": row_key},
    )
    return f"{path}?server_key={server_key}"


@pytest.mark.django_db
class TestVerifyEndpointsServerKeyGuard:
    """The module verify and the interface row read reject forged or non-string server keys without a 500 or arbitrary cache access."""

    def test_module_verify_unhashable_server_key_degrades(self):
        from netbox_librenms_plugin.views.object_sync.devices import SingleModuleVerifyView

        device = _make_vc_member_device(name="srvkey-mod-dev")
        request = _json_post(
            "/plugins/librenms_plugin/verify-module/",
            {"device_id": device.pk, "page_device_id": device.pk, "ent_physical_index": 7, "server_key": ["a"]},
            _superuser("srvkey-mod-su"),
        )
        view = SingleModuleVerifyView()
        view.request = request

        with _servers_settings(_GUARD_SERVERS):
            response = view.post(request)

        assert response.status_code in (200, 404)

    def test_interface_row_read_forged_string_key_never_scopes_cache(self, monkeypatch):
        from django.test import Client

        from netbox_librenms_plugin import source_cache

        device = _make_gate_device(name="srvkey-forged-dev")
        for own, namespace in enumerate(("alpha", "evil-namespace"), start=4401):
            seed_mapping(device, namespace, own=own, save=False)
        device.save()
        for namespace in ("alpha", "evil-namespace"):
            seed_interfaces_snapshot(device, [_snapshot_port(10, "eth0")], server_key=namespace)
        client = Client()
        client.force_login(_superuser("srvkey-forged-su"))
        captured = []
        original_read = source_cache.read

        def spy(subject, resource, server_key):
            captured.append(server_key)
            return original_read(subject, resource, server_key)

        monkeypatch.setattr(source_cache, "read", spy)

        with _servers_settings(_GUARD_SERVERS):
            forged = client.get(_interface_row_url(device, "10", "evil-namespace"))
            configured = client.get(_interface_row_url(device, "10", "alpha"))

        # The forged key never reads its own namespace; a configured key reads its own snapshot.
        assert forged.status_code == 404
        assert configured.status_code == 200, configured.content
        assert "evil-namespace" not in captured
        assert "alpha" in captured


@pytest.mark.django_db
class TestParseRequestJsonRejectsNonObject:
    """A valid-JSON non-object body must 400 centrally, not AttributeError-500 at data.get()."""

    def test_helper_rejects_list_str_and_number(self):
        from netbox_librenms_plugin.views.mixins import parse_request_json

        for body in (b"[1]", b'"x"', b"7"):
            request = RequestFactory().generic("POST", "/", data=body, content_type="application/json")
            data, err = parse_request_json(request)
            assert data is None
            assert err is not None and err.status_code == 400

    def test_cable_verify_returns_400_for_array_body(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        request = RequestFactory().post(
            "/plugins/librenms_plugin/verify-cable/", data="[1]", content_type="application/json"
        )
        request.user = _superuser("nonobj-cable-su")
        view = SingleCableVerifyView()
        view.request = request

        with _servers_settings(_GUARD_SERVERS):
            response = view.post(request)

        assert response.status_code == 400
        assert json.loads(response.content)["message"] == "JSON payload must be an object"


@pytest.mark.django_db
class TestInterfaceRowReadMalformedPortsCache:
    """A malformed payload under the snapshot key must degrade to the 404 miss path."""

    def test_corrupt_snapshot_degrades_to_the_miss(self):
        from django.core.cache import cache as real_cache
        from django.test import Client

        from netbox_librenms_plugin import source_cache
        from netbox_librenms_plugin.acquisition.registry import Resource

        device = _make_gate_device(name="srvkey-corrupt-dev")
        seed_mapping(device, "alpha", own=4410)
        client = Client()
        client.force_login(_superuser("srvkey-corrupt-su"))

        with _servers_settings(_GUARD_SERVERS):
            cache_key = source_cache.snapshot_key(device, Resource.INTERFACES, "alpha")
            real_cache.set(cache_key, ["corrupt", "legacy", "shape"], timeout=60)
            response = client.get(_interface_row_url(device, "10", "alpha"))

        # Degrades to the not-found miss path instead of AttributeError-500.
        assert response.status_code == 404
        # A reader never deletes: a refresh replaces the entry.
        assert real_cache.get(cache_key) == ["corrupt", "legacy", "shape"]


@pytest.mark.django_db
class TestInterfaceRowReadObjectScope:
    """The row read restricts the device lookup so constrained grants cannot reveal cached data outside their object scope."""

    SERVER_KEY = "default"  # the only configured server in the test env

    @staticmethod
    def _constrained_user(in_scope_device):
        from dcim.models import Device

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        user = make_user_with_perms("scoped-verify", [])
        return grant(user, "view", Device, constraints={"pk": in_scope_device.pk}, name="scoped-view-device")

    def test_out_of_scope_device_row_read_answers_404(self):
        """The row read 404s an out-of-scope device even with its snapshot stored (no data leak)."""
        from django.test import Client

        in_scope = _make_gate_device(name="scope-in")
        out_of_scope = _make_gate_device(name="scope-out")
        for own, device in enumerate((in_scope, out_of_scope), start=4420):
            seed_mapping(device, self.SERVER_KEY, own=own)
            seed_interfaces_snapshot(device, [_snapshot_port(1, "Gi0/0")], server_key=self.SERVER_KEY)
        client = Client()
        client.force_login(self._constrained_user(in_scope))

        hidden = client.get(_interface_row_url(out_of_scope, "1", self.SERVER_KEY))
        visible = client.get(_interface_row_url(in_scope, "1", self.SERVER_KEY))

        # An out-of-scope pk 404s at the lookup like a nonexistent one, so the snapshot is never read.
        assert hidden.status_code == 404
        assert b"Gi0/0" not in hidden.content
        assert visible.status_code == 200, visible.content

    def test_grant_scopes_lookup_to_covered_device(self):
        """The restricted queryset the endpoint uses covers the in-scope device and excludes others."""
        from dcim.models import Device

        in_scope = _make_gate_device(name="scope-in-2")
        out_of_scope = _make_gate_device(name="scope-out-2")
        user = self._constrained_user(in_scope)

        scoped = Device.objects.restrict(user, "view")
        assert scoped.filter(pk=in_scope.pk).exists() is True
        assert scoped.filter(pk=out_of_scope.pk).exists() is False


@pytest.mark.django_db
class TestInterfaceVlanGroupsCommandObjectScope:
    """The VLAN groups command blocks plugin writers from changing the draft of a device outside their view scope."""

    SERVER_KEY = "default"

    @staticmethod
    def _plugin_writer_scoped_to(in_scope_device):
        """A real user with plugin write access and a pk-constrained view_device grant (no superuser)."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        user = make_user_with_perms("scoped-writer", [])
        return grant(user, "view", Device, constraints={"pk": in_scope_device.pk}, name="scoped-view-dev")

    def test_out_of_scope_device_is_blocked_and_an_in_scope_device_is_not(self):
        """A plugin writer scoped to another device cannot choose VLAN groups for an out-of-scope device."""
        from django.test import Client

        in_scope = _make_gate_device(name="ovr-in")
        out_of_scope = _make_gate_device(name="ovr-out")
        for own, device in enumerate((in_scope, out_of_scope), start=4430):
            seed_mapping(device, self.SERVER_KEY, own=own)
            seed_interfaces_snapshot(
                device, [_snapshot_port(10, "Gi0/0", **port_vlans("access", 100))], server_key=self.SERVER_KEY
            )
        client = Client()
        client.force_login(self._plugin_writer_scoped_to(in_scope))
        shown = draft_tab(client, in_scope, "interfaces", interface_name_field="ifName")
        fields = {"row": "10", "group_100": ""}
        hidden_url = shown.table["data-vlan-groups-url"].replace(
            f"/device/{in_scope.pk}/", f"/device/{out_of_scope.pk}/"
        )

        blocked = client.post(
            hidden_url, {"draft_id": shown.table["data-draft-id"], "expected_revision": "0", **fields}
        )
        applied = draft_command(client, shown, "data-vlan-groups-url", **fields)

        # The out-of-scope device 404s at the restricted lookup before any draft is read.
        assert blocked.status_code == 404
        assert applied.status_code == 204


# ---------------------------------------------------------------------------
# SingleModuleVerifyView — object-permission ordering (device enumeration guard)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSingleModuleVerifyPermissionOrder:
    """SingleModuleVerifyView.post() must reject a caller lacking dcim.view_device before resolving the device (no 404-vs-403 enumeration)."""

    def _view_for(self, user):
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.object_sync.devices import SingleModuleVerifyView

        request = RequestFactory().post(
            "/verify-module/",
            data=json.dumps({"device_id": 999_999_999, "ent_physical_index": 1, "server_key": "default"}),
            content_type="application/json",
        )
        request.user = user
        view = SingleModuleVerifyView()
        view.request = request
        return view, request

    def test_missing_view_device_perm_returns_403_not_404_for_absent_device(self):
        """A real user without dcim.view_device POSTing a non-existent device_id gets 403, not 404 (real ObjectPermissionBackend decides has_perm)."""
        from django.contrib.auth import get_user_model
        from django.http import Http404

        user = get_user_model().objects.create_user("no-view-device")  # non-superuser, no object perms
        view, request = self._view_for(user)

        try:
            response = view.post(request)
        except Http404:
            pytest.fail(
                "get_object_or_404(Device) ran before the permission check: a caller without "
                "dcim.view_device can enumerate devices by observing 404-vs-403."
            )
        assert response.status_code == 403
        assert "view_device" in json.loads(response.content)["error"]


@pytest.mark.django_db
def test_module_verify_rejects_an_out_of_range_device_id_like_its_siblings():
    """
    The module endpoint let a raw JSON value reach the pk lookup, so a bad request became a
    database error and a 500 where the interface and VLAN-group endpoints answer 400."""
    import json as json_module

    from netbox_librenms_plugin.tests.view_test_helpers import make_request
    from netbox_librenms_plugin.utils import _POSTGRES_BIGINT_MAX
    from netbox_librenms_plugin.views.object_sync.devices import SingleModuleVerifyView

    view = SingleModuleVerifyView()
    request = make_request(
        "post",
        json_module.dumps({"device_id": _POSTGRES_BIGINT_MAX + 1, "ent_physical_index": 7}),
        user=_verify_superuser("bigint-module-device-id"),
        path="/verify/",
        content_type="application/json",
    )

    response = view.post(request)

    assert response.status_code == 400
    assert json_module.loads(response.content)["message"] == "No device ID provided"


@pytest.mark.django_db
def test_the_interface_row_read_rejects_a_device_id_beyond_the_bigint_range(client, settings):
    """An oversized primary key must fail as a missing device, not in the database driver."""
    from netbox_librenms_plugin.utils import _POSTGRES_BIGINT_MAX

    configure_default_librenms_server(settings)
    client.force_login(_verify_superuser("bigint-device-id"))
    url = reverse(
        "plugins:netbox_librenms_plugin:interface_row",
        kwargs={"object_type": "device", "pk": _POSTGRES_BIGINT_MAX + 1, "row_key": "10"},
    )

    response = client.get(url, {"server_key": "default"})

    assert response.status_code == 404


@pytest.mark.django_db
def test_cable_verify_selects_the_cached_row_named_by_row_id():
    """Verify a populated cable cache selects the row named by row_id instead of returning the default row."""
    from django.core.cache import cache

    from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

    device = _make_gate_device(name="cbl-rowid-device")
    server_key = configured_server_key()
    seed_mapping(device, server_key, own=405)
    links = [link_row(10, "console-A", remote_hostname="peer-a"), link_row(11, "console-B", remote_hostname="peer-b")]
    seed_cables_snapshot(
        device,
        links,
        [port_row(10, "ttyS0", device_id=405), port_row(11, "ttyS1", device_id=405)],
        server_key=server_key,
    )
    second_row_id = "11"
    view_probe = SingleCableVerifyView()
    cache_key = view_probe.get_cache_key(device, "links", server_key)

    def _render(row_id, tag):
        view, request = _real_verify_view(
            SingleCableVerifyView,
            {"device_id": device.pk, "row_id": row_id},
            _verify_superuser(f"cbl-rowid-{tag}"),
        )
        response = view.post(request)
        data = json.loads(response.content)
        assert data["status"] == "success"
        return json.dumps(data["formatted_row"])

    try:
        second = _render(second_row_id, "second")
        first = _render("10", "first")
    finally:
        cache.delete(cache_key)

    # Each row_id must select its own row: asserting only one direction would pass just as well
    # if the view always returned the same row.
    assert "console-B" in second and "console-A" not in second
    assert "console-A" in first and "console-B" not in first
