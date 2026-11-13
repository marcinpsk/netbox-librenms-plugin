"""Seed the interface snapshot and post the interface sync as the browser does, for end-to-end sync tests."""

import json
from types import SimpleNamespace

from django.urls import reverse

from netbox_librenms_plugin.middleware import REQUEST_FAILED_EVENT
from netbox_librenms_plugin.tests.conftest import make_interface
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_interfaces_snapshot
from netbox_librenms_plugin.tests.view_test_helpers import messages_on, select_rows
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

SERVER_KEY = "default"
SYNCED = "Selected interfaces synced successfully."


def sync_port(port_id, name, *, alias="", mac="", if_type="ethernetCsmacd", **extra):
    """Return one cached LibreNMS port row; *extra* adds keys such as the VLAN fields."""
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifType": if_type,
        "ifAdminStatus": "up",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifPhysAddress": mac,
        "ifAlias": alias,
        **extra,
    }


def seed_ports(owner, ports, port_stack=(), **kwargs):
    """Store an interfaces snapshot of *owner* with *ports* and the *port_stack* pairs, as a refresh of the tab does."""
    return seed_interfaces_snapshot(owner, ports, port_stack, server_key=SERVER_KEY, **kwargs)


def bound_interface(device, name, port_id, *, iface_type="other"):
    """Create an interface on *device* that is bound to LibreNMS port *port_id*."""
    interface = make_interface(device, name, iface_type=iface_type)
    seed_mapping(interface, SERVER_KEY, own=port_id, save=False)
    interface.save()
    return interface


def synced_interface(device, name, port_id, **fields):
    """Return an interface that a sync of ``sync_port(port_id, name)`` leaves unchanged, with *fields* set."""
    from dcim.models import Interface

    interface = bound_interface(device, name, port_id)
    Interface.objects.filter(pk=interface.pk).update(speed=1_000_000, mtu=1500, enabled=True, **fields)
    interface.refresh_from_db()
    return interface


def _object_type(owner):
    """Return the URL object type of *owner*, a Device or a VirtualMachine."""
    from virtualization.models import VirtualMachine

    return "virtualmachine" if isinstance(owner, VirtualMachine) else "device"


def sync_page(owner):
    """Return the absolute URL of the interfaces tab of *owner*."""
    url_name = (
        "dcim:device_librenms_sync"
        if _object_type(owner) == "device"
        else "plugins:netbox_librenms_plugin:vm_librenms_sync"
    )
    return "http://testserver" + reverse(url_name, kwargs={"pk": owner.pk}) + "?tab=interfaces"


def interface_choices(*, members=None, vlan_groups=None):
    """
    Return the draft commands that choose rows' members and VLAN groups, for :func:`select_interface_rows`.

    Args:
        members (dict | None): The member pk to choose for each port ID.
        vlan_groups (dict | None): The VLAN group pk ("" is Global) of each VID to choose for each port ID.

    Returns:
        list[tuple[str, dict]]: The table attribute of each command URL and its fields.

    """
    commands = [
        ("data-choice-url", {"row": str(port_id), "field": "member", "value": str(member)})
        for port_id, member in (members or {}).items()
    ]
    commands += [
        ("data-vlan-groups-url", {"row": str(port_id), **{f"group_{vid}": str(group) for vid, group in groups.items()}})
        for port_id, groups in (vlan_groups or {}).items()
    ]
    return commands


def select_interface_rows(
    client, owner, port_ids, *, interface_name_field="ifName", members=None, vlan_groups=None, states=None
):
    """
    Choose and select *port_ids* in the user's interface draft of *owner* as the page does.

    Returns:
        dict[str, str]: The ``draft_id`` and ``expected_revision`` that the sync form posts.

    """
    return select_rows(
        client,
        owner,
        "interfaces",
        port_ids,
        server_key=SERVER_KEY,
        choices=interface_choices(members=members, vlan_groups=vlan_groups),
        states=states,
        interface_name_field=interface_name_field,
    )


def post_interface_sync(
    client,
    owner,
    port_ids,
    *,
    htmx,
    exclude_columns=("vlans", "mac_address"),
    interface_name_field="ifName",
    members=None,
    vlan_groups=None,
    states=None,
    draft=None,
    extra=None,
):
    """
    Select *port_ids* through the tab's draft, then post the interface sync form of *owner*, as the page does.

    Args:
        members (dict | None): The member pk to choose for each port ID first.
        vlan_groups (dict | None): The VLAN groups to choose for each port ID first, ``{vid: group pk}``.
        states (dict | None): Other intents by port ID, for example ``{12: "off"}``.
        draft (dict | None): The ``draft_id`` and ``expected_revision`` of an earlier :func:`select_interface_rows`;
            then the helper selects nothing.
        extra (dict | None): More form fields, for example ``auto_select_lag_members``.

    """
    url = (
        reverse(
            "plugins:netbox_librenms_plugin:sync_selected_interfaces",
            kwargs={"object_type": _object_type(owner), "object_id": owner.pk},
        )
        + f"?interface_name_field={interface_name_field}"
    )
    headers = {"HTTP_REFERER": sync_page(owner)}
    if htmx:
        headers["HTTP_HX_REQUEST"] = "true"
    if draft is None:
        draft = select_interface_rows(
            client,
            owner,
            port_ids,
            interface_name_field=interface_name_field,
            members=members,
            vlan_groups=vlan_groups,
            states=states,
        )
    data = {"server_key": SERVER_KEY, **draft, "exclude_columns": list(exclude_columns), **(extra or {})}
    return client.post(url, data, **headers)


def count_sync_attempts(monkeypatch):
    """
    Count sync attempts at the owner lock, which each attempt takes once.

    Returns:
        SimpleNamespace: ``count`` is the number of attempts. Set ``before_retry`` to a callable
            that runs when the second attempt starts.

    """
    real_lock = SyncInterfacesView._lock_selected_device_targets
    state = SimpleNamespace(count=0, before_retry=None)

    def counting_lock(self, obj):
        state.count += 1
        if state.count == 2 and state.before_retry is not None:
            state.before_retry()
        return real_lock(self, obj)

    monkeypatch.setattr(SyncInterfacesView, "_lock_selected_device_targets", counting_lock)
    return state


def assert_try_again_answer(response, device, htmx, message):
    """Assert the one visible answer the middleware gives for a lock conflict."""
    if htmx:
        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert json.loads(response["HX-Trigger"]) == {REQUEST_FAILED_EVENT: None}
        assert message in response.content.decode()
        assert messages_on(response.wsgi_request) == []
    else:
        assert response.status_code == 302
        assert response["Location"] == sync_page(device)
        assert messages_on(response.wsgi_request) == [("error", message)]
