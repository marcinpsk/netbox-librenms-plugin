"""
The draft endpoints that every sync tab on the server selection draft shares.

A draft view resolves the subject, the server and the stored snapshot of one request. A draft command
is a compare-and-set on the draft ID and the revision that the client acknowledged: it answers 204 with
the new revision in ``X-Draft-Revision``, or 409 when the draft is not the one the client names (another
window changed it, or a data refresh replaced it); then nothing changed.
"""

import json

from dcim.models import Device
from django.core import signing
from django.core.exceptions import BadRequest
from django.http import Http404, HttpResponse
from django.views import View
from virtualization.models import VirtualMachine

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.librenms_api import LibreNMSAPI
from netbox_librenms_plugin.selection_draft import Assignment, Intent, read_evidence
from netbox_librenms_plugin.server_mappings import mapped_device_servers
from netbox_librenms_plugin.sync_rows import REVISION_HEADER
from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin, NetBoxObjectPermissionMixin


def owner_model(object_type):
    """Return the model of a sync subject URL's ``object_type``."""
    if object_type == "device":
        return Device
    if object_type == "virtualmachine":
        return VirtualMachine
    raise Http404("Unsupported sync object type.")


def parse_assignments(raw, tab, obj, server_key, canonical_row_key):
    """
    Return the assignments that a selection request posts: ``{row_key: {"state", "token"}}`` as JSON.

    A selection carries the row's evidence token, and the token must be one that the tab of this subject
    and server signed for this row.

    Args:
        raw (str | None): The posted JSON.
        tab (SyncTab): The tab whose evidence salt signs the tokens.
        obj (Device | VirtualMachine): The subject of the sync page.
        server_key (str): The LibreNMS server of the tab.
        canonical_row_key (Callable[[object], str]): Returns a valid row key unchanged; raises BadRequest otherwise.

    Raises:
        BadRequest: The body is not such a JSON object.

    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise BadRequest("The assignments are not JSON.") from None
    if not isinstance(data, dict) or not data:
        raise BadRequest("The assignments name no row.")
    assignments = {}
    for row_key, value in data.items():
        canonical_row_key(row_key)
        try:
            intent = Intent(value["state"])
            token = value["token"] if intent is Intent.ON else None
        except (KeyError, TypeError, ValueError):
            raise BadRequest(f"The assignment of row {row_key} is not valid.") from None
        assignments[row_key] = (
            Assignment(intent, row_evidence(tab, row_key, token, obj, server_key))
            if intent is Intent.ON
            else Assignment(intent)
        )
    return assignments


def row_evidence(tab, row_key, token, obj, server_key):
    """
    Return the payload of *token* when the tab *tab* of this subject and server signed it for the row *row_key*.

    Raises:
        BadRequest: The token is not such a token.

    """
    try:
        payload = read_evidence(tab, token) if isinstance(token, str) else None
    except signing.BadSignature:
        payload = None
    if isinstance(payload, dict) and (payload.get("row"), payload.get("subject"), payload.get("server")) == (
        row_key,
        [obj._meta.label_lower, obj.pk],
        server_key,
    ):
        return payload
    raise BadRequest(f"The request carries no evidence of row {row_key}.")


class DraftView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """
    Resolve the subject, the server and the snapshot of one request on the user's selection draft of a tab.

    The draft holds only the user's own selection, so the tab's view permissions are enough; the
    submit checks the write permissions. The URL query names the server and the page query.
    """

    # The snapshot resource of the tab.
    resource = None

    def subject(self, request, object_type, pk):
        """
        Return the subject, its server key and its snapshot (None when there is none), or an error response.

        Raises:
            Http404: The subject is outside the view scope, or the server is not mapped to it.

        """
        model = owner_model(object_type)
        self.required_object_permissions = {request.method: [("view", model)]}
        if error := self.require_object_permissions_json(request.method):
            return None, None, None, error
        obj = self.restrict_object_or_404(model, pk=pk)
        server_key = request.GET.get("server_key")
        # A key that names no configured server reads no cache namespace, also when a mapping names it.
        if (
            not isinstance(server_key, str)
            or server_key not in LibreNMSAPI.get_available_servers()
            or server_key not in mapped_device_servers(obj, active_server=server_key)
        ):
            raise Http404("LibreNMS server is not mapped to this object.")
        return obj, server_key, source_cache.read(obj, self.resource, server_key), None


class DraftCommandView(DraftView):
    """A command on the user's draft of a tab: a compare-and-set on the draft ID and revision that the client acknowledged."""

    # The draft writer of the page reads only the status and the headers.
    answers_json = True

    @staticmethod
    def draft_version(data):
        """Return the draft ID and the expected revision that a command names."""
        try:
            return str(data["draft_id"]), int(data["expected_revision"])
        except (KeyError, TypeError, ValueError):
            raise BadRequest("A draft command names its draft and revision.") from None

    @staticmethod
    def acknowledge(revision, trigger=None):
        """Return the answer of an applied command."""
        response = HttpResponse(status=204)
        response[REVISION_HEADER] = str(revision)
        if trigger is not None:
            response["HX-Trigger"] = json.dumps(trigger)
        return response

    @staticmethod
    def conflict():
        """Return the answer of a command that named another draft or revision."""
        return HttpResponse(status=409)


def row_response(html):
    """Return the answer of a row read: one ``<tr>``, never kept by a cache because it shows a viewer's selection."""
    response = HttpResponse(html)
    response["Cache-Control"] = "no-store"
    return response
