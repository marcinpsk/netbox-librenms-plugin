"""
Drive the VLAN tab and the VLAN writer through their real routes with confirmation tokens.

The tokens come from the rendered tab, the reassessment endpoint or the rename disclosure, so a test
approves exactly what the assessment offered.
"""

import json
import re
from urllib.parse import urlencode

from django.urls import reverse

from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.confirmation_intent import FIELD
from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_source_snapshot

_ROW = re.compile(r'<tr\b[^>]*\bdata-vlan-id="(\d+)"[^>]*>(.*?)</tr>', re.S)
_TOKEN = re.compile(r'name="confirmation_intent" value="([^"]*)"')


def seed_vlans(device, rows, server_key="default"):
    """Store a VLAN source snapshot holding *rows*."""
    seed_source_snapshot(device, Resource.VLANS, server_key, vlans=rows)


def row_tokens(html):
    """Return the token that each rendered VLAN row offers, by VID; a row without an offer maps to ''."""
    tokens = {}
    for vid, row in _ROW.findall(html):
        (token,) = _TOKEN.findall(row)
        tokens[int(vid)] = token
    return tokens


def tab_tokens(client, device, server_key="default", **params):
    """Render the VLAN tab fragment as the logged-in user and return its row tokens by VID."""
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": "vlans"},
    )
    query = {"server_key": server_key, "vlans_per_page": "100", **params}
    response = client.get(f"{url}?{urlencode(query)}")
    assert response.status_code == 200, response.content
    return row_tokens(response.content.decode())


def sync_url(device):
    """Return the VLAN synchronization route of *device*."""
    return reverse(
        "plugins:netbox_librenms_plugin:sync_selected_vlans",
        kwargs={"object_type": "device", "object_id": device.pk},
    )


def sync(client, device, tokens, *, confirm=False, server_key="default", htmx=True, **fields):
    """POST *tokens* to the VLAN writer and return the response."""
    data = {"server_key": server_key, "action": "create_vlans", FIELD: list(tokens), **fields}
    if confirm:
        data["confirm_conflicts"] = "1"
    return client.post(sync_url(device), data, **({"HTTP_HX_REQUEST": "true"} if htmx else {}))


def reassess(client, device, vid, group, *, server_key="default", **fields):
    """Ask the reassessment endpoint for one row with the chosen group, and return the response."""
    body = {
        "device_id": device.pk,
        "server_key": server_key,
        "vid": vid,
        "vlan_group_id": group.pk if group is not None else None,
        **fields,
    }
    return client.post(
        reverse("plugins:netbox_librenms_plugin:verify_vlan_sync_group"),
        data=json.dumps(body),
        content_type="application/json",
    )


def message_texts(response):
    """Return the texts of the messages that a test client response queued."""
    return [str(message) for message in response.wsgi_request._messages]


def assessed_tokens(device, user, server_key="default", *, choices=None):
    """Assess the device's VLAN snapshot as *user*, the way the tab does, and return the row tokens by VID."""
    from netbox_librenms_plugin import confirmation_intent, source_cache
    from netbox_librenms_plugin.assessment.vlans import assess
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    view = DeviceVLANTableView()
    groups = view.get_vlan_groups_for_device(device, user=user)
    if choices:
        from ipam.models import VLANGroup

        chosen = [pk for pk in choices.values() if pk is not None]
        groups = list({*groups, *VLANGroup.objects.restrict(user, "view").filter(pk__in=chosen)})
    rows = assess(
        source_cache.read(device, Resource.VLANS, server_key),
        subject=device,
        lookup_maps=view._build_vlan_lookup_maps(groups, user=user),
        context=confirmation_intent.context_for(user, device, server_key),
        choices=choices,
    ).rows
    return {row["vlan_id"]: row["confirmation_intent"] for row in rows if row["vlan_id"] is not None}
