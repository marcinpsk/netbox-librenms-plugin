import json

from dcim.models import Device
from django.contrib import messages
from django.core import signing
from django.core.exceptions import BadRequest
from django.http import Http404, HttpResponse
from django.utils import timezone
from django.views import View
from virtualization.models import VirtualMachine

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.librenms_api import LibreNMSAPI
from netbox_librenms_plugin.selection_draft import Assignment, DraftConflict, Intent, read_evidence
from netbox_librenms_plugin.server_mappings import mapped_device_servers, read_mapping
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, request_actor_id
from netbox_librenms_plugin.sync_filters import parse_table_filters
from netbox_librenms_plugin.sync_rows import (
    IP_TAB,
    RESULTS_STALE_EVENT,
    REVISION_HEADER,
    ROW_STALE_EVENT,
    VRF_CHOICE,
    find_ip_draft,
    ip_row_html,
    ip_rows,
    ip_table,
    management_assignments,
    offpage_selection,
    open_ip_draft,
    read_ip_assessment,
    seed_management_rows,
)
from netbox_librenms_plugin.sync_view_scope import VRF_UNAVAILABLE, SyncViewScope, parse_vrf_id
from netbox_librenms_plugin.tables.ipaddresses import IPAddressTable
from netbox_librenms_plugin.utils import (
    normalize_ip_sync_row_id,
    resolve_create_missing_interfaces,
    resolve_set_primary_ip,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)

from netbox_librenms_plugin.assessment.ip_addresses import shown_vrf_id


class BaseIPAddressTableView(LibreNMSPermissionMixin, LibreNMSAPIMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """Base view for synchronizing IP address information from LibreNMS."""

    partial_template_name = "netbox_librenms_plugin/_ipaddress_sync_content.html"

    def get_object(self, pk):
        return self.restrict_object_or_404(self.model, pk=pk)

    def _prepare_context(self, request, obj, server_key):
        """Return the tab context of the stored snapshot, or None when there is no snapshot to show."""
        snapshot = source_cache.read(obj, Resource.IP_ADDRESSES, server_key)
        if snapshot is None:
            return None
        return self._snapshot_context(request, obj, snapshot, server_key)

    def _snapshot_context(self, request, obj, snapshot, server_key):
        """Return the tab context of one snapshot, assessed for the request user and selected by the user's draft."""
        draft = open_ip_draft(request, obj, server_key, snapshot)
        cleared = draft.cleared
        assessment = ip_rows(request, obj, server_key, snapshot, draft.entries())
        if not seed_management_rows(request, assessment, draft):
            # Another request changed the new draft first, so the tab shows the draft as it is now.
            draft = open_ip_draft(request, obj, server_key, snapshot)
            assessment = ip_rows(request, obj, server_key, snapshot, draft.entries())
        cache_ttl = source_cache.remaining_ttl(obj, Resource.IP_ADDRESSES, server_key)
        cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl) if cache_ttl else None
        table, filters, filtered_from = ip_table(request, obj, server_key, assessment, draft)
        offpage = offpage_selection(table, assessment.rows)

        return {
            "table": table,
            "filters": filters,
            # The row count before the filter, shown only while a filter narrows the table.
            "filtered_from": filtered_from,
            # The data refresh replaced a draft that had rows, so the page says the selection is gone.
            "selection_cleared": cleared,
            # The selected rows that the shown page does not show, which the submit takes too.
            "offpage_selected": offpage,
            "offpage_selected_json": json.dumps(offpage),
            "object": obj,
            "cache_expiry": cache_expiry,
            "server_key": server_key,
            "set_primary_ip": resolve_set_primary_ip(request),
            "create_missing_interfaces": resolve_create_missing_interfaces(request),
            # Donor "Move IP to winner" candidates (empty unless this device carries a
            # _migrated_to marker for server_key); drives the migrated-mode action card.
            "movable_ips": self._movable_ips_for_migration(request, obj, server_key),
        }

    def _empty_context(self, request, obj, server_key, migration_server_key):
        """
        Return the tab context without a table.

        The set-primary-IP and create-missing switches keep their state, and the donor's move card
        stays: its moves are NetBox operations that never read LibreNMS.
        """
        return {
            "table": None,
            "object": obj,
            "cache_expiry": None,
            "server_key": server_key,
            "set_primary_ip": resolve_set_primary_ip(request),
            "create_missing_interfaces": resolve_create_missing_interfaces(request),
            "movable_ips": self._movable_ips_for_migration(request, obj, migration_server_key),
        }

    @staticmethod
    def _movable_ips_for_migration(request, obj, server_key):
        """
        List the donor's IPs that the user may move to the winner (empty unless migrated).

        The scope lists viewable addresses on the donor's own viewable interfaces. Gated on the
        marker so a non-migrated device pays no extra query.

        Args:
            request (HttpRequest): The request of the user who reads the tab.
            obj (Device | VirtualMachine): The candidate donor object.
            server_key (str | None): The LibreNMS server key for the migration marker.

        Returns:
            list[dict[str, int | str]]: The movable IP candidates, or an empty list.
        """
        if not isinstance(obj, Device) or not read_mapping(obj).migrated_to(server_key or "default"):
            return []
        return SyncViewScope.for_request(request.user, obj).movable_ips()

    def get_context_data(self, request, obj):
        """Get the context data for the IP address sync view."""
        # The filters show their errors also when no snapshot is cached.
        filters = parse_table_filters(request.GET, IPAddressTable)
        # Read the snapshot of ?server_key; a key that no longer resolves renders an empty table, never the default's.
        scoped, unresolved = self.resolve_get_render_server_key(request)
        server_key = None if unresolved else scoped or self._render_server_key()
        context = self._prepare_context(request, obj, server_key) if server_key is not None else None
        if context is None:
            context = self._empty_context(request, obj, scoped, scoped)
        context.setdefault("filters", filters)
        return context

    def post(self, request, pk):
        """Refresh the IP snapshot from LibreNMS and render the tab."""
        obj = self.get_object(pk)
        # Rebind the API to the POSTed server, so discovery and acquisition use the server of the tab.
        server_key = self.rebind_api_for_posted_server(request.POST)
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            # rebind_api_for_server() returned None to avoid building a missing/misconfigured
            # default client; reading the lazy `librenms_api` property here would reconstruct it
            # and can raise (a 500 on this HTMX error path). Use the already-cached client's key.
            active_server_key = self.active_server_key
            # render_sync_partial injects the migrated-donor context (resolved from the active
            # session key, since the POSTed key is now known-invalid) so a stale server_key can't
            # silently re-enable IP sync on a migrated donor.
            return self.render_sync_partial(
                request,
                obj,
                active_server_key,
                {"ip_sync": self._empty_context(request, obj, None, active_server_key)},
            )

        # Discovery may find the LibreNMS device by IP or hostname and store the mapping; acquisition
        # then reads that mapping and never writes.
        librenms_id, lookup_error = self.resolve_librenms_id(obj)
        if librenms_id is None:
            # A failed refresh must not leave an older snapshot to act on.
            source_cache.delete(obj, Resource.IP_ADDRESSES, server_key)
            error_message = self.scoped_lookup_message(lookup_error) if lookup_error else "Device not found in LibreNMS"
        else:
            result = source_cache.refresh(request, obj, Resource.IP_ADDRESSES, server_key)
            snapshot = result.snapshot
            error_message = result.error and f"Failed to fetch IP addresses from LibreNMS: {result.error}"

        if error_message is not None:
            messages.error(request, error_message)
            SyncCacheConsistency(obj).mark_refresh_failure(
                SyncTab.IP_ADDRESSES,
                server_key,
                actor_id=request_actor_id(request),
            )
            return self.render_sync_partial(
                request, obj, server_key, {"ip_sync": self._empty_context(request, obj, server_key, server_key)}
            )

        if SyncCacheConsistency(obj).mark_refresh_outcome(
            SyncTab.IP_ADDRESSES,
            server_key,
            actor_id=request_actor_id(request),
        ):
            messages.success(request, "IP address data refreshed successfully.")
        else:
            # The rows below come from this refresh and are rendered, so the message must not
            # claim there is nothing to show. Only the snapshot write failed, and a sync posts
            # against that snapshot, so say what the user has to do before syncing.
            messages.error(
                request,
                "IP address data could not be cached. The rows shown come from this refresh only "
                "and will not survive a reload. Refresh again before syncing; see server logs for details.",
            )
        return self.render_sync_partial(
            request, obj, server_key, {"ip_sync": self._snapshot_context(request, obj, snapshot, server_key)}
        )


def _owner_model(object_type):
    """Return the model of a sync subject URL's ``object_type``."""
    if object_type == "device":
        return Device
    if object_type == "virtualmachine":
        return VirtualMachine
    raise Http404("Unsupported sync object type.")


def _canonical_row_key(value):
    """Return *value* when it is the canonical ID of an IP row."""
    try:
        if normalize_ip_sync_row_id(value) == value:
            return value
    except ValueError:
        pass
    raise BadRequest("The request names no valid IP row.")


def _parse_assignments(raw, obj, server_key):
    """
    Return the assignments that a selection request posts: ``{row_key: {"state", "token"}}`` as JSON.

    A selection carries the row's evidence token, and the token must be one that the IP tab of this
    subject and server signed for this row.

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
        _canonical_row_key(row_key)
        try:
            intent = Intent(value["state"])
            token = value["token"] if intent is Intent.ON else None
        except (KeyError, TypeError, ValueError):
            raise BadRequest(f"The assignment of IP row {row_key} is not valid.") from None
        assignments[row_key] = (
            Assignment(intent, _row_evidence(row_key, token, obj, server_key))
            if intent is Intent.ON
            else Assignment(intent)
        )
    return assignments


def _parse_shown_evidence(raw, obj, server_key):
    """
    Return the evidence payload of each management row that the page showed: ``{row_key: token}`` as JSON.

    Raises:
        BadRequest: The body is not such a JSON object, or a token is not one of its row.

    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise BadRequest("The shown evidence is not JSON.") from None
    if not isinstance(data, dict):
        raise BadRequest("The shown evidence is not a JSON object.")
    return {_canonical_row_key(key): _row_evidence(key, token, obj, server_key) for key, token in data.items()}


def _row_evidence(row_key, token, obj, server_key):
    """
    Return the payload of *token* when the IP tab of this subject and server signed it for the row *row_key*.

    Raises:
        BadRequest: The token is not such a token.

    """
    try:
        payload = read_evidence(IP_TAB, token) if isinstance(token, str) else None
    except signing.BadSignature:
        payload = None
    if isinstance(payload, dict) and (payload.get("row"), payload.get("subject"), payload.get("server")) == (
        row_key,
        [obj._meta.label_lower, obj.pk],
        server_key,
    ):
        return payload
    raise BadRequest(f"The request carries no evidence of IP row {row_key}.")


class _IPDraftView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """
    Resolve the subject, the server and the IP snapshot of one request on the user's IP selection draft.

    The draft holds only the user's own selection, so the tab's view permissions are enough; the
    submit checks the write permissions. The URL query names the server and the page query.
    """

    def subject(self, request, object_type, pk):
        """
        Return the subject, its server key and its IP snapshot (None when there is none), or an error response.

        Raises:
            Http404: The subject is outside the view scope, or the server is not mapped to it.

        """
        model = _owner_model(object_type)
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
        return obj, server_key, source_cache.read(obj, Resource.IP_ADDRESSES, server_key), None


class _IPDraftCommandView(_IPDraftView):
    """
    A command on the user's IP draft: a compare-and-set on the draft ID and revision that the client acknowledged.

    It answers 204 with the new revision in ``X-Draft-Revision``, or 409 when the draft is not the one
    the client names (another window changed it, or a data refresh replaced it); then nothing changed.
    """

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

    @staticmethod
    def draft(request, obj, server_key, snapshot):
        """Return the stored draft of the snapshot that the command read; None (a conflict) when a newer one replaced it."""
        return find_ip_draft(request, obj, server_key, snapshot) if snapshot is not None else None


class IPSelectionView(_IPDraftCommandView):
    """Set the absolute selection state of IP rows in the user's draft; the page's unload beacon posts here too."""

    def post(self, request, object_type, pk):
        """Apply one batch of row states: ``draft_id``, ``expected_revision`` and ``assignments`` (JSON)."""
        obj, server_key, snapshot, error = self.subject(request, object_type, pk)
        if error:
            return error
        draft_id, expected_revision = self.draft_version(request.POST)
        assignments = _parse_assignments(request.POST.get("assignments"), obj, server_key)
        if (draft := self.draft(request, obj, server_key, snapshot)) is None:
            return self.conflict()
        try:
            revision = draft.assign(draft_id, expected_revision, assignments)
        except DraftConflict:
            return self.conflict()
        return self.acknowledge(revision)


class IPChoiceView(_IPDraftCommandView):
    """Store the VRF that the user chose for one IP row, then name the row for the page to read again."""

    def post(self, request, object_type, pk):
        """Store ``value`` ("" is Global, else a VRF pk) as the VRF of the IP row ``row``."""
        obj, server_key, snapshot, error = self.subject(request, object_type, pk)
        if error:
            return error
        draft_id, expected_revision = self.draft_version(request.POST)
        row_key = _canonical_row_key(request.POST.get("row"))
        vrf_id = parse_vrf_id(request.POST.get("value"))
        if (draft := self.draft(request, obj, server_key, snapshot)) is None:
            return self.conflict()
        rows = read_ip_assessment(request, obj, snapshot, server_key).rows
        row = next((row for row in rows if row["row_id"] == row_key), None)
        if row is None or row.get("view_block") is not None:
            raise Http404("No current IP row with a VRF choice has this key.")
        # A hidden and a missing VRF give the same answer.
        if vrf_id is not None and vrf_id not in {vrf.pk for vrf in row["vrfs"]}:
            raise Http404(VRF_UNAVAILABLE)
        # The VRF that the row shows without a choice is no choice: the row then follows a later suggestion.
        value = None if vrf_id == shown_vrf_id(row) else "" if vrf_id is None else str(vrf_id)
        try:
            revision = draft.choose(draft_id, expected_revision, row_key, VRF_CHOICE, value)
        except DraftConflict:
            return self.conflict()
        return self.acknowledge(revision, {ROW_STALE_EVENT: {"key": row_key}})


class IPManagementView(_IPDraftCommandView):
    """Select the management rows that Set Primary IP takes in the user's draft, or clear them."""

    def post(self, request, object_type, pk):
        """Select the management rows with the shown ``evidence`` when ``selected`` is ``on``; else clear them."""
        obj, server_key, snapshot, error = self.subject(request, object_type, pk)
        if error:
            return error
        draft_id, expected_revision = self.draft_version(request.POST)
        selected = request.POST.get("selected")
        if selected not in ("on", "off"):
            raise BadRequest("Set Primary IP is on or off.")
        # The page sends the evidence it showed for the management rows; only that evidence is approved.
        shown = _parse_shown_evidence(request.POST.get("evidence"), obj, server_key) if selected == "on" else {}
        if (draft := self.draft(request, obj, server_key, snapshot)) is None:
            return self.conflict()
        rows = ip_rows(request, obj, server_key, snapshot, draft.entries()).rows
        try:
            revision = draft.assign(draft_id, expected_revision, management_assignments(rows, selected == "on", shown))
        except DraftConflict:
            return self.conflict()
        return self.acknowledge(revision, {RESULTS_STALE_EVENT: None})


class IPRowView(_IPDraftView):
    """Answer one IP row as the tab's table renders it, for the page's row read."""

    def get(self, request, object_type, pk, row_key):
        """Render the ``<tr>`` of the IP row *row_key* from the stored snapshot and the user's draft."""
        obj, server_key, snapshot, error = self.subject(request, object_type, pk)
        if error:
            return error
        if snapshot is None:
            raise Http404("The IP snapshot is not available.")
        # A row read is a presentation: like the tab render, it opens the draft of the published snapshot.
        html = ip_row_html(
            request, obj, server_key, snapshot, open_ip_draft(request, obj, server_key, snapshot), row_key
        )
        if html is None:
            raise Http404("No current IP row has this key.")
        response = HttpResponse(html)
        # Per-viewer selection state.
        response["Cache-Control"] = "no-store"
        return response
