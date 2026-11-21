"""Drive mapping editor confirmations through their real lifecycle route."""

import re

from django.urls import reverse

_STALE = re.compile(r"^'([^']*)': the confirmed [^(]+ is no longer current \(([^)]*)\)")


def model_name(obj):
    """Return the object type that the lifecycle routes name for *obj*."""
    return obj._meta.model_name


def lifecycle_url(obj):
    """Return the lifecycle route of *obj*: its POST applies the host change token."""
    return reverse(
        "plugins:netbox_librenms_plugin:mapping_lifecycle", kwargs={"object_type": model_name(obj), "pk": obj.pk}
    )


def edit_mapping(client, obj, server_key="default", **typed):
    """Post the mapping editor of *obj* with the shown entry of *server_key* and the *typed* values."""
    from netbox_librenms_plugin.server_mappings import host_entry

    entry = host_entry(type(obj).objects.get(pk=obj.pk), server_key)
    shown = {
        "own_id": "" if entry.own_id is None else str(entry.own_id),
        "oob_id": "" if entry.oob_id is None else str(entry.oob_id),
        "oob_type": entry.oob_type or "",
    }
    data = {"object_type": model_name(obj), "server_key": server_key, **shown}
    data.update({f"shown_{field}": value for field, value in shown.items()})
    data.update(typed)
    return client.post(reverse("plugins:netbox_librenms_plugin:edit_server_mapping", args=[obj.pk]), data)


def offered_token(response):
    """Return the token of a rendered host change confirmation."""
    assert response.status_code == 200, (response.status_code, message_texts(response))
    return response.context["token"]


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
