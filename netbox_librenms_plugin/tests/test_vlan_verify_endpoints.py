"""
The VLAN group views of a sync row: the interface VLAN groups dialog and its command, and the VLAN tab's group choice.

The tests cover their permission gate, their row-key guard, the colours that the dialog shows for the
groups it previews, and the interface view scope. Each test seeds a snapshot and drives the real views
with a signed-in client; NetBox rows and object permissions are real.
"""

import re
from urllib.parse import urlencode

import pytest
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device, make_superuser
from netbox_librenms_plugin.tests.interface_sync_post_helpers import bound_interface, sync_port
from netbox_librenms_plugin.tests.source_snapshot_helpers import port_vlans, seed_interfaces_snapshot

SERVER_KEY = "default"


def _vlan_group(name):
    """Create a real VLANGroup."""
    from ipam.models import VLANGroup

    return VLANGroup.objects.create(name=name, slug=name.lower().replace(" ", "-"))


def _vlan(vid, name, *, group=None):
    """Create a real VLAN, optionally inside *group*."""
    from ipam.models import VLAN

    return VLAN.objects.create(vid=vid, name=name, group=group)


def _device(client, settings, name, ports, *, user=None):
    """Return a device mapped to LibreNMS whose interfaces snapshot holds *ports*; sign in *user* or a superuser."""
    configure_default_librenms_server(settings)
    client.force_login(user or make_superuser(f"{name}-user"))
    device = make_device(name, librenms_cf={SERVER_KEY: {"id": 4300}})
    seed_interfaces_snapshot(device, ports, server_key=SERVER_KEY)
    return device


def _url(name, device, query=None, **kwargs):
    """Return the URL of the view *name* for *device*, with the server, the name field and *query*."""
    path = reverse(
        f"plugins:netbox_librenms_plugin:{name}", kwargs={"object_type": "device", "pk": device.pk, **kwargs}
    )
    return f"{path}?{urlencode({'server_key': SERVER_KEY, 'interface_name_field': 'ifName', **(query or {})})}"


def _dialog_vlans(client, device, row_key, **groups):
    """Return ``{vid: (css class, missing marker)}`` of the VLAN groups dialog of one row that previews *groups*."""
    response = client.get(_url("interface_vlan_modal", device, groups, row_key=row_key))
    assert response.status_code == 200, response.content
    rows = re.findall(
        r'<tr data-vid="(\d+)">\s*<td><span class="([^"]*)">\d+</span>(.*?)</td>', response.content.decode(), re.S
    )
    return {int(vid): (css, "mdi-alert" in tail) for vid, css, tail in rows}


@pytest.mark.django_db
class TestTheDraftEndpointGate:
    """Each endpoint checks the subject's view permission before it resolves the device."""

    @pytest.mark.parametrize(
        ("method", "name", "kwargs"),
        [
            ("get", "interface_vlan_modal", {"row_key": "10"}),
            ("post", "interface_selection_vlan_groups", {}),
            ("post", "vlan_selection_choice", {}),
            ("get", "vlan_row", {"row_key": "110"}),
        ],
        ids=["interface-dialog", "interface-command", "vlan-choice", "vlan-row-read"],
    )
    def test_a_missing_device_permission_is_reported_instead_of_the_device(
        self, client, settings, method, name, kwargs
    ):
        """A plugin writer without the DCIM view grant gets 403, not a device-scoped 404."""
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        user = make_user_with_perms(f"vlan-gate-{name}-user", [])
        device = _device(client, settings, f"vlan-gate-{name}", [sync_port(10, "eth0")], user=user)

        response = getattr(client, method)(_url(name, device, **kwargs))

        assert response.status_code == 403
        assert response.json() == {"error": "Missing permissions: dcim.view_device"}


@pytest.mark.django_db
class TestTheVlanGroupsCommandRowGuard:
    """The VLAN groups command names one interface row by its canonical port ID, or answers 400."""

    @pytest.mark.parametrize("row", [None, "not-a-port", "010"], ids=["missing", "text", "leading-zero"])
    def test_an_unusable_row_is_rejected(self, client, settings, row):
        from netbox_librenms_plugin.tests.view_test_helpers import draft_command, draft_tab

        device = _device(client, settings, "vlan-row-guard", [sync_port(10, "eth0", **port_vlans("access", 110))])
        shown = draft_tab(client, device, "interfaces", interface_name_field="ifName")
        fields = {"group_110": ""} | ({} if row is None else {"row": row})

        response = draft_command(client, shown, "data-vlan-groups-url", **fields)

        assert response.status_code == 400


@pytest.mark.django_db
class TestTheDialogColours:
    """The dialog colours each VLAN for the group it previews, as the row compares it with NetBox."""

    def test_a_grouped_vlan_is_missing_when_no_group_is_selected(self, client, settings):
        """Without a VLAN group only global VLANs resolve, so a grouped VID reads as missing."""
        device = _device(
            client, settings, "vlan-dialog-global", [sync_port(1, "Ethernet1", **port_vlans("tagged", 950, [940]))]
        )
        group = _vlan_group("Verify Campus")
        _vlan(940, "grouped-only", group=group)
        global_vlan = _vlan(950, "global-only")
        interface = bound_interface(device, "Ethernet1", 1)
        interface.mode = "tagged"
        interface.untagged_vlan = global_vlan
        interface.save()

        vlans = _dialog_vlans(client, device, "1", group_940="", group_950="")

        assert vlans[940] == ("text-danger", True)
        assert vlans[950] == ("text-success", False)

    def test_tagged_vlan_group_comparison_uses_the_netbox_vlan_group(self, client, settings):
        """The same tagged VID matches in its own group and reads as a group mismatch in another."""
        device = _device(
            client, settings, "vlan-dialog-tagged", [sync_port(2, "Ethernet2", **port_vlans("tagged", tagged=[930]))]
        )
        own_group = _vlan_group("Verify Tagged Own")
        other_group = _vlan_group("Verify Tagged Other")
        own_vlan = _vlan(930, "tagged-own", group=own_group)
        _vlan(930, "tagged-other", group=other_group)
        interface = bound_interface(device, "Ethernet2", 2)
        interface.mode = "tagged"
        interface.save()
        interface.tagged_vlans.set([own_vlan])

        same_group = _dialog_vlans(client, device, "2", group_930=str(own_group.pk))
        other = _dialog_vlans(client, device, "2", group_930=str(other_group.pk))

        assert same_group[930] == ("text-success", False)
        assert other[930] == ("text-warning", False)

    def test_a_tagged_vlan_outside_every_group_carries_the_missing_warning(self, client, settings):
        """A tagged VID that exists in no visible VLAN renders the missing-VLAN warning icon."""
        device = _device(
            client,
            settings,
            "vlan-dialog-tagged-missing",
            [sync_port(3, "Ethernet3", **port_vlans("tagged", tagged=[931]))],
        )
        interface = bound_interface(device, "Ethernet3", 3)
        interface.mode = "tagged"
        interface.save()

        assert _dialog_vlans(client, device, "3")[931] == ("text-danger", True)


@pytest.mark.django_db
def test_the_dialog_reads_an_interface_the_user_cannot_view_as_absent(client, settings):
    """The dialog compares only an interface in the view scope: a hidden one answers as a missing one."""
    from dcim.models import Device, Interface
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    user = make_user_with_perms("vlan-dialog-hidden-user", [("view", Device), ("view", VLANGroup), ("view", VLAN)])
    user = grant(user, "view", Interface, constraints={"name": "VisibleOnly"})
    device = _device(
        client,
        settings,
        "vlan-dialog-hidden-iface",
        [sync_port(4, "Ethernet4", **port_vlans("tagged", tagged=[932]))],
        user=user,
    )
    group = _vlan_group("Verify Hidden Iface")
    hidden = bound_interface(device, "Ethernet4", 4)
    hidden.mode = "tagged"
    hidden.save()
    hidden.tagged_vlans.set([_vlan(932, "hidden-iface", group=group)])

    hidden_answer = _dialog_vlans(client, device, "4", group_932=str(group.pk))
    hidden.delete()
    absent_answer = _dialog_vlans(client, device, "4", group_932=str(group.pk))

    assert hidden_answer == absent_answer
