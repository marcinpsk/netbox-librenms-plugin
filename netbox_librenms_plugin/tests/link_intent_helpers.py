"""
Drive the Link confirmation through its real routes.

The sync page or a status list offers the token, and the lifecycle routes apply it.
"""

import re

from django.urls import reverse

from netbox_librenms_plugin.confirmation_intent import FIELD
from netbox_librenms_plugin.tests.conftest import make_interface, make_ip

_STALE = re.compile(r"^'([^']*)': the confirmed link is no longer current \(([^)]*)\)")


def model_name(obj):
    """Return the object type that the lifecycle routes name for *obj*."""
    return obj._meta.model_name


def give_primary_ip(obj, address):
    """Give *obj* a new primary IPv4 address on a new interface; return the address."""
    from virtualization.models import VMInterface

    if model_name(obj) == "device":
        interface = make_interface(obj, f"link-{address.split('/')[0]}")
    else:
        interface = VMInterface.objects.create(virtual_machine=obj, name=f"link-{address.split('/')[0]}")
    ip = make_ip(address, assigned_object=interface)
    obj.primary_ip4 = ip
    obj.save()
    return ip


def register_match(server, value, device_id, hostname="matched-host"):
    """Make the LibreNMS fake answer a lookup of *value* with device *device_id*."""
    record = {"device_id": device_id, "hostname": hostname, "sysName": hostname}
    server.register(f"/api/v0/devices/{value}", {"status": "ok", "devices": [record]})
    server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [record]})


def sync_page(client, obj, **query):
    """Render the sync page of *obj* as the logged-in user and return the response."""
    page = "device_librenms_sync" if model_name(obj) == "device" else "vm_librenms_sync"
    response = client.get(reverse(f"plugins:netbox_librenms_plugin:{page}", args=[obj.pk]), query)
    assert response.status_code == 200, response.content
    return response


def page_link(client, obj, **query):
    """Return the link proposal that the sync page of *obj* renders, or None."""
    return sync_page(client, obj, **query).context["link"]


def page_token(client, obj):
    """Return the Link token that the sync page of *obj* offers."""
    link = page_link(client, obj)
    assert link is not None and link["token"], link
    return link["token"]


def link(client, obj, tokens, *, server_key="default"):
    """POST *tokens* to the Link route of *obj*, as the sync page form does, and return the response."""
    url = reverse(
        "plugins:netbox_librenms_plugin:mapping_lifecycle", kwargs={"object_type": model_name(obj), "pk": obj.pk}
    )
    return client.post(url, {"server_key": server_key, FIELD: list(tokens)})


def status_list(client, object_type, **filters):
    """Render a status list as the logged-in user, with *filters*, and return the response."""
    name = "device_status_list" if object_type == "device" else "vm_status_list"
    response = client.get(reverse(f"plugins:netbox_librenms_plugin:{name}"), filters)
    assert response.status_code == 200, response.content
    return response


def link_selected(client, object_type, tokens, *, server_key="default", return_query=""):
    """POST *tokens* to the "Link selected" route of a status list and return the response."""
    url = reverse("plugins:netbox_librenms_plugin:link_selected", kwargs={"object_type": object_type})
    return client.post(url, {"server_key": server_key, "return_query": return_query, FIELD: list(tokens)})


def message_texts(response):
    """Return the texts of the messages that a test client response queued."""
    return [str(message) for message in response.wsgi_request._messages]


def stale_paths(response, name):
    """Return the changed paths that the stale outcome line of the object named *name* reports."""
    return {
        path
        for text in message_texts(response)
        if (match := _STALE.match(text)) and match.group(1) == name
        for path in match.group(2).split(", ")
    }
