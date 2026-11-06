from django.contrib import messages
from django.core.exceptions import BadRequest
from django.http import Http404
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.assessment.vlans import apply_group_choices, assess, shown_group_id
from netbox_librenms_plugin.selection_draft import DraftConflict, Intent, SelectionDraft, sign_evidence
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, request_actor_id
from netbox_librenms_plugin.sync_filters import filter_sync_table, parse_table_filters, selection_scope
from netbox_librenms_plugin.sync_query import SyncQuery
from netbox_librenms_plugin.sync_rows import ROW_STALE_EVENT, bind_draft, evidence_problem, render_rows, row_url
from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
from netbox_librenms_plugin.utils import (
    build_migrated_context,
    coerce_model_pk,
    normalize_vlan_vid,
)
from netbox_librenms_plugin.views.base.draft_views import (
    DraftCommandView,
    DraftView,
    parse_assignments,
    row_response,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    VlanAssignmentMixin,
)

# Sentinel so _get_error_context can tell an *explicit* server_key=None (stale-server
# branch — must be preserved) apart from an omitted arg (fall back to session server).
_SERVER_KEY_UNSET = object()


class BaseVLANTableView(
    VlanAssignmentMixin, LibreNMSAPIMixin, LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View
):
    """
    Build the VLAN synchronization table.

    Fetches LibreNMS VLAN data and compares with NetBox.
    """

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_vlan_sync_content.html"

    def get_object(self, pk):
        """Retrieve the object (Device or VirtualMachine)."""
        return self.restrict_object_or_404(self.model, pk=pk)

    def post(self, request, pk):
        """Handle POST request to fetch and cache LibreNMS VLAN data."""
        obj = self.get_object(pk)

        # Rebind the API to the POSTed server BEFORE resolving librenms_id, so the id
        # (and the VLAN fetch/cache below) resolve against the right server in a
        # multi-server tab refresh rather than the session/default one.
        server_key = self.rebind_api_for_posted_server(request.POST)
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            # rebind_api_for_server() returned None precisely to avoid constructing a missing/
            # misconfigured default client, so don't touch the lazy `librenms_api` property here —
            # it would reconstruct LibreNMSAPI() and can raise, turning this HTMX error path into a
            # 500. Read the already-cached client's key (else "default").
            active_server_key = self.active_server_key
            # Pass server_key=None explicitly so the fragment doesn't silently fall back to
            # the session/default server (which would re-render with the wrong server selected
            # and let the next retry sync VLANs against the wrong LibreNMS instance).
            # Resolve the marker under the session/active server key — NOT the POSTed key, which
            # failed to rebind (stale/unconfigured) and would miss the marker entirely. render_sync_partial
            # always injects the migrated-context flags so the donor keeps its migration controls.
            return self.render_sync_partial(
                request,
                obj,
                active_server_key,
                {
                    "vlan_sync": self._get_error_context(
                        obj, "Selected LibreNMS server is no longer configured.", server_key=None
                    )
                },
            )

        # Discovery may find the LibreNMS device by IP or hostname and store the mapping; acquisition
        # then reads that mapping and never writes.
        self.librenms_id, lookup_error = self.resolve_librenms_id(obj)
        if self.librenms_id is None:
            # A failed refresh on a previously-synced device must not leave a stale VLAN table to act on.
            source_cache.delete(obj, Resource.VLANS, server_key)
            error_message = (
                self.scoped_lookup_message(lookup_error) if lookup_error else "Device not found in LibreNMS."
            )
        else:
            error = source_cache.refresh(request, obj, Resource.VLANS, server_key).error
            error_message = error and f"Failed to fetch VLANs: {error}"

        if error_message is not None:
            SyncCacheConsistency(obj).mark_refresh_failure(
                SyncTab.VLANS,
                server_key,
                actor_id=request_actor_id(request),
            )
            messages.error(request, error_message)
            return self.render_sync_partial(
                request,
                obj,
                server_key,
                {"vlan_sync": self._get_error_context(obj, error_message, server_key=server_key)},
            )

        if SyncCacheConsistency(obj).mark_refresh_outcome(
            SyncTab.VLANS,
            server_key,
            actor_id=request_actor_id(request),
        ):
            messages.success(request, "VLAN data refreshed successfully.")
        else:
            messages.error(
                request,
                "VLAN data could not be cached, so the tab has no snapshot to show. "
                "Refresh again; see server logs for details.",
            )

        return self.render_sync_partial(
            request, obj, server_key, {"vlan_sync": self.get_vlan_context(request, obj, server_key)}
        )

    def get_vlan_context(self, request, obj, server_key=None):
        """
        Build context for VLAN sync table.

        Args:
            request (HttpRequest): The current request.
            obj (Device | VirtualMachine): The NetBox object to synchronize.
            server_key (str | None): The selected LibreNMS server key. Resolve it from the request if None.

        Returns:
            dict: Context with:
                - ``vlan_table``: LibreNMSVLANTable instance.
                - ``vlan_groups``: QuerySet of available VLAN groups.
                - ``hidden_ipam_permissions``: IPAM view permissions the caller is missing.

        """
        vlan_table = None
        # The filters show their errors also when no snapshot is cached.
        filters = parse_table_filters(request.GET, lambda rows: LibreNMSVLANTable(rows, vlan_groups=[]))
        # The tab gate checks the object's own view permission only, and the table serialises VLAN
        # ids plus each group's id and name, so read IPAM as the caller.
        vlan_scope_user = self.vlan_scope_user(request)

        # Get cached data (scoped to the POST-resolved server when provided, else the GET-query
        # server on a page render — without the rebind a non-default-server tab reads the default
        # cache and renders empty after a successful refresh). Mirrors modules_view.get_context_data.
        unresolved = False
        if server_key is None:
            # GET render: rebind + scope the VLAN-cache read to ?server_key (shared helper) so a
            # non-default-server tab reads that server's cache, not the default's.
            server_key, unresolved = self.resolve_get_render_server_key(request)
            # No buildable client means no valid server scope, so the "default" placeholder of
            # resolve_get_render_server_key does not apply.
            if not unresolved and self._render_server_key() is None:
                server_key = None
        # Honour the POST-resolved server when provided; otherwise use the shared degrading resolver
        # (not a bare getattr on the lazy librenms_api property): on a missing/misconfigured default
        # the property raises KeyError/ValueError, which would 500 the VLAN tab on GET.
        server_key = server_key or self._render_server_key()
        if unresolved or server_key is None:
            # A server that no longer resolves, or no usable server, has no snapshot to read: its VLANs
            # may still be cached until TTL, but they are not live data.
            vlan_groups = self.get_vlan_groups_for_device(obj, user=vlan_scope_user)
            unscoped_groups = (
                vlan_groups
                if vlan_scope_user is None or vlan_scope_user.is_superuser
                else self.get_vlan_groups_for_device(obj)
            )
            return {
                "object": obj,
                "vlan_table": None,
                "filters": filters,
                "vlan_groups": vlan_groups,
                "last_fetched": None,
                "cache_expiry": None,
                "server_key": server_key,
                "hidden_ipam_permissions": self.hidden_vlan_permissions(
                    [obj], vlan_scope_user, unscoped_groups=unscoped_groups
                ),
                "vlan_scope_incomplete": self.vlan_scope_is_incomplete(
                    [obj],
                    vlan_scope_user,
                    scoped_groups=vlan_groups,
                    unscoped_groups=unscoped_groups,
                ),
            }
        snapshot = source_cache.read(obj, Resource.VLANS, server_key)
        cached_vlans = snapshot.value(Resource.VLANS, "vlans") if snapshot is not None else None
        last_fetched = snapshot.finished_at if snapshot is not None else None

        # Get available VLAN groups for this device
        vlan_groups = self.get_vlan_groups_for_device(obj, user=vlan_scope_user)
        unscoped_groups = (
            vlan_groups
            if vlan_scope_user is None or vlan_scope_user.is_superuser
            else self.get_vlan_groups_for_device(obj)
        )

        # Build lookup maps for VLAN matching
        lookup_maps = self._build_vlan_lookup_maps(vlan_groups, user=vlan_scope_user)

        filtered_from = None
        draft = None
        # `is not None` (not a bare truthiness check): an empty list is a valid successful refresh
        # (a device with no VLANs) and must still render an empty table — a truthy check would skip
        # construction and make a VLAN-less device look like it never loaded.
        if cached_vlans is not None:
            draft = open_vlan_draft(request, obj, server_key, snapshot)
            vlan_table, filters, filtered_from = self.build_vlan_table(
                request, obj, server_key, snapshot, draft, vlan_groups, lookup_maps
            )

        # Calculate cache TTL
        cache_ttl = source_cache.remaining_ttl(obj, Resource.VLANS, server_key)
        cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl) if cache_ttl else None

        return {
            "object": obj,
            "vlan_table": vlan_table,
            "filters": filters,
            # The row count before the filter, shown only while a filter narrows the table.
            "filtered_from": filtered_from,
            # The data refresh replaced a draft that had rows, so the page says the selection is gone.
            "selection_cleared": draft is not None and draft.cleared,
            "vlan_groups": vlan_groups,
            "last_fetched": last_fetched,
            "cache_expiry": cache_expiry,
            "server_key": server_key,
            "hidden_ipam_permissions": self.hidden_vlan_permissions(
                [obj], vlan_scope_user, unscoped_groups=unscoped_groups
            ),
            "vlan_scope_incomplete": self.vlan_scope_is_incomplete(
                [obj],
                vlan_scope_user,
                scoped_groups=vlan_groups,
                unscoped_groups=unscoped_groups,
            ),
        }

    def build_vlan_table(self, request, obj, server_key, snapshot, draft, vlan_groups, lookup_maps):
        """
        Return the VLAN table of the whole snapshot as the user's draft selects it and chooses its groups.

        The tab and the row read call this one builder, so a row read answers the row as the table shows it.

        Returns:
            tuple[LibreNMSVLANTable, SyncFilters, int | None]: The table, its filters, and the row count before them.

        """
        rows = vlan_rows(obj, snapshot, draft.entries(), lookup_maps)
        migrated = bool(build_migrated_context(obj, server_key).get("migrated_to_marker"))

        def build_table(rows):
            table = LibreNMSVLANTable(rows, vlan_groups=vlan_groups)
            table.migrated_to_marker = migrated
            return table

        _show_vlan_selection(request, obj, server_key, rows, draft, vlan_groups)
        # The comparison ran over every row, so duplicate and invalid VIDs describe the whole device.
        vlan_table, filters, filtered_from = filter_sync_table(request, rows, build_table)
        bind_draft(vlan_table, SyncQuery.from_request(request), obj, server_key, draft, VLAN_COMMAND_URLS)
        vlan_table.configure(request)
        return vlan_table, filters, filtered_from

    def vlan_groups_and_maps(self, request, obj):
        """Return the VLAN groups of *obj* and their lookup maps, read as the request user."""
        vlan_scope_user = self.vlan_scope_user(request)
        vlan_groups = self.get_vlan_groups_for_device(obj, user=vlan_scope_user)
        return vlan_groups, self._build_vlan_lookup_maps(vlan_groups, user=vlan_scope_user)

    def row_html(self, request, obj, server_key, snapshot, row_key):
        """Return the ``<tr>`` of one VLAN row as the tab's table renders it, or None when no current row has *row_key*."""
        vlan_groups, lookup_maps = self.vlan_groups_and_maps(request, obj)
        draft = open_vlan_draft(request, obj, server_key, snapshot)
        table, _filters, _filtered_from = self.build_vlan_table(
            request, obj, server_key, snapshot, draft, vlan_groups, lookup_maps
        )
        row = next((row for row in table.rows if _vlan_row_key(row.record) == row_key), None)
        return render_rows(request, [row]) if row is not None else None

    def _get_error_context(self, obj, error_message, server_key=_SERVER_KEY_UNSET):
        """
        Build the template context for the VLAN error fragment.

        Keep the fragment's ``server_key`` on the POST-resolved server so a retry/action after an
        error targets the same scope. An explicit ``server_key=None`` (the stale-server branch) is
        preserved as-is — using ``or`` here would treat it as falsey and silently fall back to the
        session server, re-rendering the fragment on the wrong (still-configured) server. Only an
        *omitted* server_key falls back.

        Args:
            obj (Device | VirtualMachine): The object being synced.
            error_message (str): The error message to display in the fragment.
            server_key: POST-resolved server key; the ``_SERVER_KEY_UNSET`` sentinel means "omitted"
                and falls back to the session server (an explicit None is preserved as-is).

        Returns:
            dict: The render context for the VLAN error fragment.

        """
        resolved = getattr(self.librenms_api, "server_key", None) if server_key is _SERVER_KEY_UNSET else server_key
        # The fragment renders the same group list as the table, so scope it the same way. This
        # path takes no request, so read the one bound on the view.
        vlan_scope_user = self.vlan_scope_user()
        vlan_groups = self.get_vlan_groups_for_device(obj, user=vlan_scope_user)
        unscoped_groups = (
            vlan_groups
            if vlan_scope_user is None or vlan_scope_user.is_superuser
            else self.get_vlan_groups_for_device(obj)
        )
        return {
            "object": obj,
            "error_message": error_message,
            "vlan_table": None,
            "vlan_groups": vlan_groups,
            "server_key": resolved,
            "hidden_ipam_permissions": self.hidden_vlan_permissions(
                [obj], vlan_scope_user, unscoped_groups=unscoped_groups
            ),
            "vlan_scope_incomplete": self.vlan_scope_is_incomplete(
                [obj],
                vlan_scope_user,
                scoped_groups=vlan_groups,
                unscoped_groups=unscoped_groups,
            ),
        }


# =============================================================================
# The VLANs tab on the user's selection draft
# =============================================================================

VLAN_TAB = SyncTab.VLANS
VLAN_TABLE_VARIANT = "vlans"
# The draft choice of a row: the VLAN group pk text that the row writes to; "" is Global.
GROUP_CHOICE = "group"
# The draft commands of the VLAN table, by the table attribute that names each URL.
VLAN_COMMAND_URLS = {"data-selection-url": "vlan_selection", "data-choice-url": "vlan_selection_choice"}


def _vlan_row_key(record):
    """Return the row key of a VLAN row: its canonical VID as text, or None for a row with an invalid or duplicate VID."""
    return None if record.get("vid_error") or record.get("vlan_id") is None else str(record["vlan_id"])


def _vlan_draft_identity(request, obj, server_key, snapshot):
    scope = selection_scope(request, obj, server_key, VLAN_TAB.value)
    return (request.user, obj, VLAN_TAB, VLAN_TABLE_VARIANT, server_key, scope, snapshot.snapshot_id)


def open_vlan_draft(request, obj, server_key, snapshot):
    """Return the request user's VLAN draft for a presentation of the snapshot; only the published snapshot starts one."""

    def published():
        current = source_cache.read(obj, Resource.VLANS, server_key)
        return current.snapshot_id if current is not None else None

    return SelectionDraft.open(*_vlan_draft_identity(request, obj, server_key, snapshot), published=published)


def find_vlan_draft(request, obj, server_key, snapshot):
    """Return the request user's stored VLAN draft of the snapshot that a command read, or None."""
    return SelectionDraft.find(*_vlan_draft_identity(request, obj, server_key, snapshot))


def vlan_rows(obj, snapshot, entries, lookup_maps):
    """Return the compared rows of the snapshot, each row with the group that the draft *entries* chose for it."""
    choices = {key: entry.choices[GROUP_CHOICE] for key, entry in entries.items() if GROUP_CHOICE in entry.choices}
    rows = assess(snapshot, subject=obj, lookup_maps=lookup_maps).rows
    return apply_group_choices(rows, choices, lookup_maps)


def written_group_id(row, vlan_groups):
    """Return the group pk that the row writes to: the shown group when its select offers it, else None (Global)."""
    group_id = shown_group_id(row)
    return group_id if group_id in {group.pk for group in vlan_groups} else None


def vlan_evidence(obj, server_key, row, vlan_groups):
    """
    Return the facts that selecting one VLAN row approves: the group it writes to, the proposed name and the VLAN it finds.

    A change of any of them asks the user to select the row again. The subject and the server bind the
    token to its tab.
    """
    return {
        "subject": [obj._meta.label_lower, obj.pk],
        "server": server_key,
        "row": _vlan_row_key(row),
        "group": written_group_id(row, vlan_groups),
        "name": row.get("name"),
        "vlan": row.get("netbox_vlan_id"),
    }


def approved_vlan_rows(obj, server_key, rows, vlan_groups, entries):
    """
    Return the approved evidence of each selected draft row whose evidence is still current, and the changed row keys.

    Returns:
        tuple[dict[str, dict], list[str]]: The approved evidence by row key, and the row keys that changed.

    """
    records = {key: row for row in rows if (key := _vlan_row_key(row)) is not None}
    approved, changed = {}, []
    for key, entry in entries.items():
        if entry.intent is not Intent.ON:
            continue
        record = records.get(key)
        current = vlan_evidence(obj, server_key, record, vlan_groups) if record is not None else None
        if evidence_problem(entry, current) is None:
            approved[key] = entry.evidence
        else:
            changed.append(key)
    return approved, changed


def _show_vlan_selection(request, obj, server_key, rows, draft, vlan_groups):
    """Add to each row its checkbox state, its evidence token and its row read URL."""
    entries = draft.entries()
    approved, _changed = approved_vlan_rows(obj, server_key, rows, vlan_groups, entries)
    query = SyncQuery.from_request(request)
    for row in rows:
        key = _vlan_row_key(row)
        entry = entries.get(key)
        row["selected"] = key in approved
        row["evidence_changed"] = entry is not None and entry.intent is Intent.ON and key not in approved
        row["evidence_token"] = sign_evidence(VLAN_TAB, vlan_evidence(obj, server_key, row, vlan_groups)) if key else ""
        row["row_read_url"] = row_url(query, obj, server_key, "vlan_row", key) if key else ""


def vlan_tab_view(request, obj, server_key):
    """Return the VLAN tab view of *obj* for *request*, bound to *server_key*."""
    # Imported here: the object_sync views import this module.
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    view = DeviceVLANTableView()
    view.setup(request, pk=obj.pk)
    view.rebind_api_for_server(server_key)
    return view


def _canonical_vid_key(value):
    """Return *value* when it is the row key of a VLAN row: a canonical VID as text."""
    vid = normalize_vlan_vid(value) if isinstance(value, str) else None
    if vid is None or str(vid) != value:
        raise BadRequest("The request names no valid VLAN row.")
    return value


class _VLANDraftView(DraftView):
    """Resolve the subject, the server and the VLAN snapshot of one request on the user's VLAN draft."""

    resource = Resource.VLANS

    def subject(self, request, object_type, pk):
        """Resolve the request as :meth:`DraftView.subject` does; only a device has a VLAN tab."""
        if object_type != "device":
            raise Http404("Only a device has a VLAN tab.")
        return super().subject(request, object_type, pk)


class _VLANDraftCommandView(DraftCommandView, _VLANDraftView):
    """A command on the user's VLAN draft."""

    @staticmethod
    def draft(request, obj, server_key, snapshot):
        """Return the stored draft of the snapshot that the command read; None (a conflict) when a newer one replaced it."""
        return find_vlan_draft(request, obj, server_key, snapshot) if snapshot is not None else None


class VLANSelectionView(_VLANDraftCommandView):
    """Set the absolute selection state of VLAN rows in the user's draft; the page's unload beacon posts here too."""

    def post(self, request, object_type, pk):
        """Apply one batch of row states: ``draft_id``, ``expected_revision`` and ``assignments`` (JSON)."""
        obj, server_key, snapshot, error = self.subject(request, object_type, pk)
        if error:
            return error
        draft_id, expected_revision = self.draft_version(request.POST)
        assignments = parse_assignments(request.POST.get("assignments"), VLAN_TAB, obj, server_key, _canonical_vid_key)
        if (draft := self.draft(request, obj, server_key, snapshot)) is None:
            return self.conflict()
        try:
            revision = draft.assign(draft_id, expected_revision, assignments)
        except DraftConflict:
            return self.conflict()
        return self.acknowledge(revision)


class VLANChoiceView(_VLANDraftCommandView):
    """Store the VLAN group that the user chose for one VLAN row, then name the row for the page to read again."""

    def post(self, request, object_type, pk):
        """Store ``value`` ("" is Global, else a VLAN group pk) as the group of the row ``row`` (``field`` is ``group``)."""
        obj, server_key, snapshot, error = self.subject(request, object_type, pk)
        if error:
            return error
        draft_id, expected_revision = self.draft_version(request.POST)
        row_key = _canonical_vid_key(request.POST.get("row"))
        if request.POST.get("field") != GROUP_CHOICE:
            raise BadRequest("A VLAN row has a group choice only.")
        raw = request.POST.get("value")
        group_id = coerce_model_pk(raw) if raw else None
        if raw and group_id is None:
            raise BadRequest("The VLAN group is not valid.")
        if (draft := self.draft(request, obj, server_key, snapshot)) is None:
            return self.conflict()
        view = vlan_tab_view(request, obj, server_key)
        vlan_groups, lookup_maps = view.vlan_groups_and_maps(request, obj)
        # A hidden and a missing group give the same answer.
        if group_id is not None and group_id not in {group.pk for group in vlan_groups}:
            raise Http404("No VLAN group you can view has this ID.")
        entries = {key: entry for key, entry in draft.entries().items() if key != row_key}
        row = next(
            (row for row in vlan_rows(obj, snapshot, entries, lookup_maps) if _vlan_row_key(row) == row_key), None
        )
        if row is None:
            raise Http404("No current VLAN row has this key.")
        # The group that the row shows without a choice is no choice: the row then follows a later match.
        value = None if group_id == written_group_id(row, vlan_groups) else "" if group_id is None else str(group_id)
        try:
            revision = draft.choose(draft_id, expected_revision, row_key, GROUP_CHOICE, value)
        except DraftConflict:
            return self.conflict()
        return self.acknowledge(revision, {ROW_STALE_EVENT: {"key": row_key}})


class VLANRowView(_VLANDraftView):
    """Answer one VLAN row as the tab's table renders it, for the page's row read."""

    def get(self, request, object_type, pk, row_key):
        """Render the ``<tr>`` of the VLAN row *row_key* from the stored snapshot and the user's draft."""
        obj, server_key, snapshot, error = self.subject(request, object_type, pk)
        if error:
            return error
        if snapshot is None:
            raise Http404("The VLAN snapshot is not available.")
        html = vlan_tab_view(request, obj, server_key).row_html(request, obj, server_key, snapshot, row_key)
        if html is None:
            raise Http404("No current VLAN row has this key.")
        return row_response(html)
