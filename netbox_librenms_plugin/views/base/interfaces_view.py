import json
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace

from dcim.models import Device
from django.contrib import messages
from django.core.exceptions import BadRequest
from django.db.models import Q
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from ipam.models import VLAN

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.assessment.interfaces import assess_interfaces
from netbox_librenms_plugin.constants import (
    HOST_NAME_COLLISION_REASON,
    OOB_INVENTORY_SOURCE,
    REPORTED_NAME_PORT_COLLISION_REASON,
)
from netbox_librenms_plugin.interface_diff import (
    ROW_AMBIGUOUS,
    ROW_IGNORED,
    ROW_INCOMPLETE,
    blocked_row_state,
    interface_enabled_from_port,
    planned_interface_type,
)
from netbox_librenms_plugin.interface_rules import RuleDecisionKind, decision_reason, interface_rules_for_request
from netbox_librenms_plugin.interface_rows_cache import (
    cached_rows,
    netbox_fingerprint,
    permission_fingerprint,
    permission_scope,
    rows_key,
)
from netbox_librenms_plugin.interface_relationships import (
    RelationshipResolutionContext,
    build_relationship_maps,
    filter_interface_index,
    relationship_diagnostics_report,
    resolve_relationship_row,
)
from netbox_librenms_plugin.librenms_ids import normalize_librenms_port_id
from netbox_librenms_plugin.models import RuleConfigurationError
from netbox_librenms_plugin.required_rows import checked_rows, off_page_requirers, requirement_edges, requirements
from netbox_librenms_plugin.selection_draft import DraftConflict, Intent, SelectionDraft, sign_evidence
from netbox_librenms_plugin.server_mappings import read_mapping, read_mappings
from netbox_librenms_plugin.server_selection import get_librenms_sync_device
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, request_actor_id
from netbox_librenms_plugin.sync_filters import (
    filter_sync_table,
    parse_table_filters,
    selection_scope,
    show_ignored_interfaces,
)
from netbox_librenms_plugin.sync_query import SyncQuery
from netbox_librenms_plugin.tables.interfaces import interface_row_key
from netbox_librenms_plugin.sync_rows import (
    RESULTS_STALE_EVENT,
    ROW_STALE_EVENT,
    bind_draft,
    evidence_problem,
    hidden_selection,
    render_rows,
    row_url,
)
from netbox_librenms_plugin.utils import (
    build_migrated_context,
    coerce_model_pk,
    get_interface_name_field,
    get_interface_port_identity_sets,
    interface_sync_options_menu,
    reported_name_owners,
    resolve_interface_row_device,
    synced_interface_names,
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

logger = logging.getLogger(__name__)


def assess_stored_snapshot(snapshot, interface_name_field):
    """Return the :func:`assess_interfaces` result of a stored snapshot, or None without one."""
    return assess_interfaces(snapshot, interface_name_field) if snapshot is not None else None


def read_interface_assessment(obj, server_key, interface_name_field):
    """
    Return the stored interfaces snapshot of *obj*, assessed for *interface_name_field*.

    Every interface reader (the tab, the verify, the sync and the relationship actions) reads through
    this function or :func:`assess_stored_snapshot`, so they all show and act on one derivation.

    Returns:
        dict | None: The :func:`assess_interfaces` result, or None when no snapshot can be read.

    """
    return assess_stored_snapshot(source_cache.read(obj, Resource.INTERFACES, server_key), interface_name_field)


class BaseInterfaceTableView(
    VlanAssignmentMixin, LibreNMSAPIMixin, LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View
):
    """
    Base view for fetching interface data from LibreNMS and generating table data.

    Includes VLAN enrichment for interface VLAN sync functionality.
    """

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_interface_sync_content.html"
    interface_name_field = None

    def get_object(self, pk):
        """Retrieve the object (Device or VirtualMachine) the user may view."""
        # The plugin gate is model-level only, so scope the lookup or any pk is reachable.
        return self.restrict_object_or_404(self.model, pk=pk)

    def get_interfaces(self, obj):
        """
        Get interfaces related to the object.

        Should be implemented in subclasses.

        Args:
            obj (Device | VirtualMachine): Object whose interfaces to retrieve.

        Raises:
            NotImplementedError: Always, because subclasses must implement this method.

        """
        raise NotImplementedError

    def _render_tab(self, request, obj, interface_name_field, server_key, sync_device):
        """
        Render the interfaces tab partial from the current cache, which is the answer to every refresh POST.

        Args:
            request (HttpRequest): Current request.
            obj (Device | VirtualMachine): Object of the sync page.
            interface_name_field (str): Name field of the LibreNMS ports.
            server_key (str): POST-scoped server key; the tab stays on this server.
            sync_device (Device | VirtualMachine): Object that owns the cache, the VC sync device for a member.

        Returns:
            HttpResponse: The rendered partial.

        """
        context = {
            "interface_sync": self.get_context_data(
                request, obj, interface_name_field, server_key, sync_device=sync_device
            ),
            "interface_name_field": interface_name_field,
        }
        return self.render_sync_partial(request, obj, server_key, context)

    def _failure_response(self, request, obj, interface_name_field, server_key, sync_device):
        """
        Record a failed refresh and render the tab, which has no snapshot now.

        The refresh URL accepts only POST, so a redirect to it would answer 405.

        Args:
            request (HttpRequest): Current request.
            obj (Device | VirtualMachine): Object of the sync page.
            interface_name_field (str): Name field of the LibreNMS ports.
            server_key (str): POST-scoped server key; the tab stays on this server.
            sync_device (Device | VirtualMachine): Object that owns the cache, the VC sync device for a member.

        Returns:
            HttpResponse: The rendered partial.

        """
        SyncCacheConsistency(obj).mark_refresh_failure(
            SyncTab.INTERFACES,
            server_key,
            actor_id=request_actor_id(request),
        )
        return self._render_tab(request, obj, interface_name_field, server_key, sync_device)

    def get_select_related_field(self, obj):
        """Determine the appropriate select_related field based on object type."""
        if self.model.__name__.lower() == "virtualmachine":
            return "virtual_machine"
        return "device"

    def get_table(self, data, obj, interface_name_field, vlan_groups=None):
        """
        Return the table class to use for rendering interface data.

        Can be overridden by subclasses to use different tables.

        Args:
            data: List of port data dicts
            obj: Device or VirtualMachine object
            interface_name_field: Field to use for interface name ('ifName' or 'ifDescr')
            vlan_groups: List of VLANGroup objects for VLAN group dropdowns

        """
        raise NotImplementedError("Subclasses must implement get_table()")

    def _get_object_librenms_ids(self, interfaces):
        """Resolve the stored or cached LibreNMS ID of each ``(interface, mapping)`` pair, in one cache read."""
        server_key = self.librenms_api.server_key
        ids = [mapping.own_id(server_key) for _, mapping in interfaces]
        # One device can have thousands of interfaces, so the cache is read once for all of them.
        unmapped = [index for index, librenms_id in enumerate(ids) if librenms_id is None]
        cached = self.librenms_api.get_cached_librenms_ids([interfaces[index][0] for index in unmapped])
        for index, librenms_id in zip(unmapped, cached, strict=True):
            ids[index] = librenms_id
        return [
            normalize_librenms_port_id(librenms_id)
            if isinstance(librenms_id, (int, str)) and not isinstance(librenms_id, bool)
            else None
            for librenms_id in ids
        ]

    def _build_interface_lookup_maps(self, obj, *, metadata_only=False):
        """
        Build name and LibreNMS ID indexes, dropping conflicting IDs entirely.

        Also select the relationship fields used during enrichment. Device interfaces have
        ``lag``, ``parent``, and ``bridge``. VM interfaces have ``parent`` and ``bridge``.

        Args:
            obj: The NetBox device (or VM) whose interfaces are indexed.
            metadata_only: Read identity fields without constructing interface models or related objects.

        Returns:
            dict: Name and LibreNMS ID indexes plus the number of interfaces carrying each ID.
                LibreNMS IDs that map to more than one interface are dropped from the direct index.

        """
        by_name = {}
        by_librenms_id = {}
        by_librenms_id_matches = {}
        librenms_id_counts = {}
        duplicate_librenms_ids = set()

        # Prefetch the relations the row diff dereferences for every matched row (VLAN
        # assignment and MAC addresses); without this each rendered interface row issues its own
        # queries for these. Also select related relationship FKs that render_parent dereferences.
        related_field = self.get_select_related_field(obj)
        queryset = self.get_interfaces(obj)
        if metadata_only:
            records = read_mappings(queryset, fields=("pk", "name", f"{related_field}_id"))
            interfaces = (
                (SimpleNamespace(_meta=queryset.model._meta, **record.values), record.mapping) for record in records
            )
        else:
            extra_related = ["parent", "bridge"] if related_field == "virtual_machine" else ["lag", "parent", "bridge"]
            # The planned type check asks each device interface whether it has LAG members.
            extra_prefetch = [] if related_field == "virtual_machine" else ["member_interfaces"]
            interfaces = (
                (interface, read_mapping(interface))
                for interface in queryset.select_related(
                    related_field, "untagged_vlan", *extra_related
                ).prefetch_related("tagged_vlans", "tagged_vlans__group", "mac_addresses", *extra_prefetch)
            )
        interfaces = list(interfaces)
        for (interface, _), librenms_id in zip(interfaces, self._get_object_librenms_ids(interfaces), strict=True):
            by_name[interface.name] = interface
            if librenms_id is None:
                continue
            librenms_id_counts[librenms_id] = librenms_id_counts.get(librenms_id, 0) + 1
            by_librenms_id_matches.setdefault(librenms_id, []).append(interface)
            if librenms_id in by_librenms_id:
                duplicate_librenms_ids.add(librenms_id)
                continue
            by_librenms_id[librenms_id] = interface

        for librenms_id in duplicate_librenms_ids:
            by_librenms_id.pop(librenms_id, None)

        return {
            "by_name": by_name,
            "by_librenms_id": by_librenms_id,
            "by_librenms_id_matches": by_librenms_id_matches,
            "librenms_id_counts": librenms_id_counts,
        }

    def post(self, request, pk):
        """Refresh the interfaces snapshot from LibreNMS and render the tab."""
        obj = self.get_object(pk)

        interface_name_field = get_interface_name_field(request, obj)

        # Rebind the API to the POSTed server, so discovery and acquisition use the server of the tab.
        server_key = self.rebind_api_for_posted_server(request.POST)
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            # This POST is HTMX (the success path swaps in the partial), so a bare redirect would
            # swap a full page into the partial target AND drop the migrated-donor context —
            # re-enabling sync controls on a migrated donor. Render the partial with migrated
            # context resolved under the active session key (the POSTed key is now known-invalid),
            # mirroring the ip/cables/modules/vlan stale-key error paths.
            # rebind_api_for_server() returned None precisely to avoid constructing a missing/
            # misconfigured default client, so don't touch the lazy `librenms_api` property here —
            # it could raise and turn this HTMX error path back into a 500. Read the already-cached
            # client's key (else "default").
            active_server_key = self.active_server_key
            return self.render_sync_partial(
                request,
                obj,
                active_server_key,
                {
                    "interface_sync": {"object": obj, "table": None, "cache_expiry": None, "server_key": None},
                    "interface_name_field": interface_name_field,
                },
            )

        # Every Virtual Chassis member shares the snapshot of the VC sync device, so discovery runs on it.
        sync_device = get_librenms_sync_device(obj, server_key=server_key) or obj
        # Discovery may find the LibreNMS device by IP or hostname and store the mapping; acquisition
        # then reads that mapping and never writes.
        librenms_id, lookup_error = self.resolve_librenms_id(sync_device)
        if librenms_id is None:
            # A failed refresh must not leave an older snapshot to act on.
            source_cache.delete(obj, Resource.INTERFACES, server_key)
            messages.error(
                request,
                self.scoped_lookup_message(lookup_error) if lookup_error else "Device not found in LibreNMS.",
            )
            return self._failure_response(request, obj, interface_name_field, server_key, sync_device)

        result = source_cache.refresh(request, obj, Resource.INTERFACES, server_key)
        if result.error is not None:
            messages.error(request, f"Failed to fetch interfaces from LibreNMS: {result.error}")
            return self._failure_response(request, obj, interface_name_field, server_key, sync_device)

        # The host rows stay usable when only the OOB controller's ports are missing.
        oob_blocked = "oob" in result.blocked
        if oob_blocked:
            notice = source_cache.blocker_notice(result.blocked["oob"])
            logger.warning("Interfaces of %s refreshed without OOB rows: %s", sync_device, notice)
        if SyncCacheConsistency(obj).mark_refresh_outcome(
            SyncTab.INTERFACES,
            server_key,
            actor_id=request_actor_id(request),
        ):
            scope = "Host interface" if oob_blocked else "Interface"
            messages.success(request, f"{scope} data refreshed successfully.")
        else:
            messages.error(
                request,
                "Interface data could not be cached, so the tab has no snapshot to show. "
                "Refresh again; see server logs for details.",
            )

        return self._render_tab(request, obj, interface_name_field, server_key, sync_device)

    @staticmethod
    def enrich_interface_name_metadata(
        ports_data,
        interface_name_field,
        interface_model,
        interfaces_by_device,
        target_device_ids,
        viewable_interface_ids,
        changeable_interface_ids,
        *,
        snapshot_complete,
    ):
        """Set derived names and permission-scoped holder metadata; return names claimed by host rows."""
        reserved_name_port_ids = {}
        for device_id, interface_maps in interfaces_by_device.items():
            for port_id, interfaces in interface_maps["by_librenms_id_matches"].items():
                for interface in interfaces:
                    reserved_name_port_ids.setdefault(device_id, {}).setdefault(interface.name, set()).add(port_id)
        synced_names, rejected_names = synced_interface_names(
            ports_data,
            interface_name_field,
            interface_model,
            target_device_ids=target_device_ids,
            reserved_name_port_ids_by_device=reserved_name_port_ids,
        )
        name_owners = reported_name_owners(
            ports_data,
            interface_name_field,
            synced_names,
            rejected_names,
            target_device_ids=target_device_ids,
            reserved_name_port_ids_by_device=reserved_name_port_ids,
            snapshot_complete=snapshot_complete,
            model=interface_model,
        )
        claimable_names_by_device = {}
        for port in ports_data:
            if port.get("_source") == OOB_INVENTORY_SOURCE:
                continue
            port_id = normalize_librenms_port_id(port.get("port_id"))
            if port.get("_dedup_conflict") or port_id in rejected_names:
                continue
            synced_name = synced_names.get(port_id)
            device_id = target_device_ids.get(port_id)
            if synced_name is not None and device_id is not None:
                claimable_names_by_device.setdefault(device_id, set()).add(synced_name)

        for port in ports_data:
            port_id = normalize_librenms_port_id(port.get("port_id"))
            synced_name = synced_names.get(port_id)
            rejection_reason = rejected_names.get(port_id)
            port["synced_name"] = synced_name
            port["synced_name_is_derived"] = synced_name is not None and synced_name != port.get(interface_name_field)
            port["synced_name_contested"] = rejection_reason in (
                HOST_NAME_COLLISION_REASON,
                REPORTED_NAME_PORT_COLLISION_REASON,
            )
            port["synced_name_rejection_reason"] = rejection_reason
            # Name the holder only when its interface is in the caller's view scope.
            name_owner = name_owners.get(port_id)
            owner_interface = (
                interfaces_by_device.get(target_device_ids.get(port_id), {}).get("by_name", {}).get(name_owner.name)
                if name_owner is not None
                else None
            )
            owner_visible = owner_interface is not None and owner_interface.pk in viewable_interface_ids
            port["reported_name_owner"] = name_owner if owner_visible else None
            port["reported_name_owner_changeable"] = owner_visible and owner_interface.pk in changeable_interface_ids
        return claimable_names_by_device

    def _interface_rows(
        self,
        request,
        obj,
        interface_name_field,
        server_key,
        cache_device,
        snapshot,
        chassis,
        *,
        show_ignored,
        choices=None,
    ):
        """
        Return the computed rows, reused while the snapshot, the viewer, the choices and the NetBox rows they read are unchanged.

        *choices* is the :class:`InterfaceChoices` of the user's draft; None chooses nothing.
        """
        choices = choices or InterfaceChoices()

        def compute():
            return self._compute_interface_rows(
                request,
                obj,
                interface_name_field,
                server_key,
                cache_device,
                assess_stored_snapshot(snapshot, interface_name_field),
                chassis,
                show_ignored=show_ignored,
                choices=choices,
            )

        if snapshot is None:
            return compute()
        owner_ids = [owner.pk for owner in chassis.attribution_members]
        owner_field = self.get_select_related_field(obj)
        interfaces = self.get_interfaces(obj).model.objects.filter(**{f"{owner_field}_id__in": owner_ids})
        owners = type(obj).objects.filter(pk__in=owner_ids)
        key = rows_key(
            cache_device.pk,
            # A row with no resolved member belongs to the viewed member, so each member page has its own rows.
            obj.pk,
            server_key,
            snapshot.snapshot_id,
            request.user.pk,
            permission_fingerprint(request.user),
            permission_scope(request.user, owners, interfaces),
            interface_name_field,
            show_ignored,
            choices.digest(),
            netbox_fingerprint(owners, interfaces),
        )
        return cached_rows(key, compute)

    def _compute_interface_rows(  # noqa: C901
        self,
        request,
        obj,
        interface_name_field,
        server_key,
        cache_device,
        assessment,
        chassis,
        *,
        show_ignored,
        choices,
    ):
        """
        Compute the rows of the interface table and the device-wide state around them.

        This reads every NetBox interface of the device (and of each Virtual Chassis member) and compares
        it with the LibreNMS snapshot, so it is the expensive part of a render. Rows are attributed to
        every member of *chassis*; a row owned by a member the actor may not view carries
        ``member_withheld`` and no member pk. A row's member choice of *choices* replaces the inferred
        member when the user may view that member, and its VLAN group choices replace the automatic groups.

        Returns:
            dict: The VLAN scope, the table rows (None without a snapshot), the counts, the NetBox-only list,
                the requirements of each row, and the snapshot state the tab reports.

        """
        table_rows = None
        oob_incomplete = False
        oob_blockers = ()
        relationship_diagnostics = None
        netbox_only_interfaces = []
        ignored_count = 0
        blocked_port_ids = []
        migrated = False
        is_chassis = bool(getattr(obj, "virtual_chassis", None))
        virtual_chassis_members = chassis.attribution_members if is_chassis else ()
        # Include every visible member's scope so rows owned by another member can resolve their VLANs.
        vlan_scope_devices = list(chassis.visible)
        # The tab gate checks the object's own view permission only, and the table serialises VLAN
        # ids plus each group's id and name, so read IPAM as the caller.
        vlan_scope_user = self.vlan_scope_user(request)
        vlan_groups = self.get_vlan_groups_for_devices(vlan_scope_devices, user=vlan_scope_user)
        lookup_maps = self._build_vlan_lookup_maps(vlan_groups, user=vlan_scope_user)
        unscoped_groups = (
            vlan_groups
            if vlan_scope_user is None or vlan_scope_user.is_superuser
            else self.get_vlan_groups_for_devices(vlan_scope_devices)
        )
        hidden_ipam_permissions = self.hidden_vlan_permissions(
            vlan_scope_devices, vlan_scope_user, unscoped_groups=unscoped_groups
        )
        vlan_scope_incomplete = self.vlan_scope_is_incomplete(
            vlan_scope_devices,
            vlan_scope_user,
            scoped_groups=vlan_groups,
            unscoped_groups=unscoped_groups,
        )
        vlan_groups_by_device = {
            device.pk: self.filter_vlan_groups_for_device(vlan_groups, device) for device in vlan_scope_devices
        }
        # Restrict a member's maps on first use. Most rows resolve to one member, so rebuilding
        # every lookup dictionary for each member of a large chassis repeated the same indexing.
        restricted_lookup_maps = {}

        def member_lookup_maps(device_id):
            if device_id not in restricted_lookup_maps:
                device_vlan_groups = vlan_groups_by_device.get(device_id)
                restricted_lookup_maps[device_id] = (
                    self.restrict_vlan_lookup_maps(lookup_maps, device_vlan_groups)
                    if device_vlan_groups is not None
                    else {}
                )
            return restricted_lookup_maps[device_id]

        requirement_edges_by_row = {}

        if assessment is not None:
            ports_data = assessment["ports"]
            oob_incomplete = assessment["oob_incomplete"]
            oob_blockers = assessment["oob_blockers"]
            # What the device's port_stack held and what each rule made of it, for the rows below.
            relationship_diagnostics = relationship_diagnostics_report(assessment, interface_name_field)
            matched_interface_ids = set()

            relationship_maps = build_relationship_maps(assessment)
            requirement_edges_by_row = {
                row_key: sorted(required)
                for row_key, required in requirement_edges(
                    relationship_maps.lag_members, relationship_maps.sub_interfaces, relationship_maps.bridge_members
                ).items()
            }
            unique_host_port_ids, unambiguous_name_port_ids = get_interface_port_identity_sets(
                ports_data, interface_name_field
            )

            # Pre-fetch all interfaces for all potential chassis members
            # (_build_interface_lookup_maps selects related interface fields). Materialize the
            # VC members once and index them by vc_position and id so the per-port member resolution
            # and the netbox-only pass below reuse them instead of issuing a members.get(...) query
            # per port / per netbox-only interface (N+1).
            interfaces_by_device = {}
            interfaces_by_port_id = {}
            catalog_interfaces_by_port_id = {}
            catalog_interfaces_by_name = {}
            members_by_position = None
            members_by_id = None
            if virtual_chassis_members:
                members_by_position = {member.vc_position: member for member in virtual_chassis_members}
                members_by_id = {member.id: member for member in virtual_chassis_members}
                for member in virtual_chassis_members:
                    interfaces_by_device[member.id] = self._build_interface_lookup_maps(member)
            else:
                interfaces_by_device[obj.id] = self._build_interface_lookup_maps(obj)

            for interface_maps in interfaces_by_device.values():
                for port_id, interface in interface_maps["by_librenms_id"].items():
                    interfaces_by_port_id.setdefault(port_id, []).append(interface)
                for port_id, interfaces in interface_maps["by_librenms_id_matches"].items():
                    catalog_interfaces_by_port_id.setdefault(port_id, []).extend(interfaces)
                for interface_name, interface in interface_maps["by_name"].items():
                    catalog_interfaces_by_name.setdefault(interface_name, []).append(interface)

            interface_model = self.get_interfaces(obj).model
            owner_field = self.get_select_related_field(obj)
            actionable_owner_ids = set(chassis.visible_ids)
            # Only interfaces on owners the user may view are shown, named or acted on.
            scoped_interfaces = interface_model.objects.filter(**{f"{owner_field}_id__in": actionable_owner_ids})
            viewable_interface_ids = set(scoped_interfaces.restrict(request.user, "view").values_list("pk", flat=True))
            # A change grant does not show an interface: an action needs both grants.
            changeable_interface_ids = viewable_interface_ids & set(
                scoped_interfaces.restrict(request.user, "change").values_list("pk", flat=True)
            )
            can_write_relationships = self.has_write_permission()
            related_interfaces_by_port_id = {}
            related_interfaces_by_name = {}
            for interface_maps in interfaces_by_device.values():
                for port_id, interfaces in interface_maps["by_librenms_id_matches"].items():
                    permitted = [
                        interface
                        for interface in interfaces
                        if interface.pk in viewable_interface_ids
                        and getattr(interface, f"{owner_field}_id") in actionable_owner_ids
                    ]
                    if permitted:
                        related_interfaces_by_port_id.setdefault(port_id, []).extend(permitted)
                for interface_name, interface in interface_maps["by_name"].items():
                    if (
                        interface.pk in viewable_interface_ids
                        and getattr(interface, f"{owner_field}_id") in actionable_owner_ids
                    ):
                        related_interfaces_by_name.setdefault(interface_name, []).append(interface)

            catalog_index = {
                "by_lnms_id": catalog_interfaces_by_port_id,
                "by_name": catalog_interfaces_by_name,
            }
            related_index = {
                "by_lnms_id": related_interfaces_by_port_id,
                "by_name": related_interfaces_by_name,
            }
            display_index = related_index
            relationship_context = RelationshipResolutionContext(
                obj=obj,
                server_key=server_key,
                catalog_index=catalog_index,
                display_index=display_index,
                related_index=related_index,
                source_index=filter_interface_index(related_index, changeable_interface_ids),
                actionable_owner_ids=actionable_owner_ids,
                changeable_interface_ids=changeable_interface_ids,
                viewable_interface_ids=viewable_interface_ids,
                can_write=can_write_relationships,
            )

            rules = interface_rules_for_request(request)
            # A row with no resolved member is decided on the platform of every member the user can pick.
            member_platform_ids = sorted({member.platform_id for member in chassis.visible if is_chassis}, key=str)
            visible_members = {member.pk: member for member in chassis.visible} if is_chassis else {}

            def row_member(port, port_id, *, return_device_on_failure):
                """Return the member the user chose for the row when it is visible, else the inferred member."""
                chosen = visible_members.get(choices.members.get(str(port_id)))
                if chosen is not None:
                    return chosen
                return resolve_interface_row_device(
                    obj,
                    port,
                    interface_name_field,
                    interfaces_by_port_id=interfaces_by_port_id,
                    members_by_position=members_by_position,
                    members_by_id=members_by_id,
                    return_device_on_failure=return_device_on_failure,
                )

            target_device_ids = {}
            for port in ports_data:
                port_id = normalize_librenms_port_id(port.get("port_id"))
                if port_id is None:
                    continue
                if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                    target_device = row_member(port, port_id, return_device_on_failure=True)
                else:
                    target_device = obj
                target_device_ids[port_id] = target_device.pk
            claimable_names_by_device = self.enrich_interface_name_metadata(
                ports_data,
                interface_name_field,
                interface_model,
                interfaces_by_device,
                target_device_ids,
                viewable_interface_ids,
                changeable_interface_ids,
                snapshot_complete=not oob_incomplete,
            )

            for port in ports_data:
                port_id = normalize_librenms_port_id(port.get("port_id"))
                port["enabled"] = interface_enabled_from_port(port)

                if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                    # The rules read the owner's platform. An OOB row belongs to the page device, as the writer holds.
                    rule_owner = row_member(port, port_id, return_device_on_failure=False) or (
                        obj if port.get("_source") == OOB_INVENTORY_SOURCE else None
                    )
                    chassis_member = rule_owner or obj
                else:
                    rule_owner = chassis_member = obj
                # The writer's own check, so a row never offers a sync the writer refuses.
                if rule_owner is not None:
                    port["rule_decision"] = rules.check_interface_write(port, platform_id=rule_owner.platform_id)
                else:
                    # Ignored only when the writer refuses every member the user can pick; else it asks for one.
                    decisions = [rules.check_interface_write(port, platform_id=pk) for pk in member_platform_ids]
                    ignored = bool(decisions) and all(d.kind is RuleDecisionKind.IGNORE for d in decisions)
                    port["rule_decision"] = decisions[0] if ignored else None

                netbox_interface = resolve_relationship_row(
                    relationship_context,
                    port,
                    chassis_member,
                    interface_name_field,
                    unique_host_port_ids,
                    unambiguous_name_port_ids,
                    relationship_maps,
                )
                port_id = normalize_librenms_port_id(port.get("port_id"))
                # The owner stays attributed, but a member the user may not view is not named by its pk.
                port["member_withheld"] = chassis_member.pk not in actionable_owner_ids
                port["selected_object_id"] = None if port["member_withheld"] else chassis_member.pk
                port["sync_target_resolvable"] = not port["member_withheld"] and port_id in unique_host_port_ids
                # A bound interface is always resolved by the stable port_id -- for an OOB row
                # too, which can own an interface on this device. It therefore counts toward the
                # matched set used for netbox-only detection, and because no binding comes from a
                # name lookup, an OOB row can never hide a same-named host interface.
                if netbox_interface is not None:
                    matched_interface_ids.add(netbox_interface.id)

                if port.get("ifAlias") in (port.get("ifDescr"), port.get("ifName")):
                    port["ifAlias"] = ""

                # Add VLAN group auto-selection data to port, applying any user overrides
                if chassis_member.pk in actionable_owner_ids:
                    row_vlan_groups = vlan_groups_by_device.get(chassis_member.pk, [])
                    row_lookup_maps = member_lookup_maps(chassis_member.pk)
                else:
                    row_vlan_groups = []
                    row_lookup_maps = {}
                port["vlan_groups"] = row_vlan_groups
                self._add_vlan_group_selection(
                    port, row_lookup_maps, chassis_member, choices.vlan_groups.get(str(port_id))
                )

                # Add missing VLANs info for warning display
                self._add_missing_vlans_info(port, row_lookup_maps)

            # Every port above fed the matching and naming. Only now are ignored rows left out, and
            # before pagination, so the page counts only the rows it shows.
            ignored = [
                port["rule_decision"] is not None and port["rule_decision"].kind is RuleDecisionKind.IGNORE
                for port in ports_data
            ]
            ignored_count = sum(ignored)
            table_rows = (
                ports_data
                if show_ignored
                else [port for port, is_ignored in zip(ports_data, ignored, strict=True) if not is_ignored]
            )
            # Each row keeps its sync state (``_sync_state``), so the rows cache holds it and a filter reads it.
            state_table = self.get_table([], obj, interface_name_field, vlan_groups=vlan_groups)
            for port in table_rows:
                state_table.row_sync_state(port)
            # Every row the write check refuses, or that has no owner the user may pick: the browser walk skips them.
            blocked_port_ids = [
                normalize_librenms_port_id(port.get("port_id"))
                for port in ports_data
                if port["rule_decision"] is None
                or decision_reason(port["rule_decision"]) is not None
                or port["member_withheld"]
            ]
            # Propagate donor "migrated mode" so the table suppresses per-row relationship sync
            # buttons (the bulk form is already hidden by the template; the row buttons POST
            # directly, so they must be stripped here too to keep a migrated donor read-only).
            migrated = bool(build_migrated_context(obj, server_key).get("migrated_to_marker"))

            netbox_only_interfaces = []
            for device_id, device_interface_maps in interfaces_by_device.items():
                if device_id not in actionable_owner_ids:
                    continue
                for interface_name, interface in device_interface_maps["by_name"].items():
                    if interface.id not in viewable_interface_ids or interface.id in matched_interface_ids:
                        continue
                    if interface_name not in claimable_names_by_device.get(device_id, set()):
                        # Get device name for the interface (reuse the pre-indexed members — the
                        # device_id keys come from interfaces_by_device, which was built from them —
                        # instead of a members.get(id=...) query per netbox-only interface).
                        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                            device = members_by_id.get(device_id) if members_by_id else None
                            device_name = device.name if device else obj.name
                        else:
                            device_name = obj.name

                        netbox_only_interfaces.append(
                            {
                                "id": interface.id,
                                "name": interface.name,
                                "device_name": device_name,
                                "device_id": device_id,
                                "type": str(interface.type)
                                if hasattr(interface, "type") and interface.type
                                else "Virtual"
                                if hasattr(interface, "virtual_machine")
                                else "Unknown",
                                "enabled": interface.enabled,
                                "description": interface.description or "",
                                "url": interface.get_absolute_url(),
                            }
                        )

        return {
            "vlan_groups": vlan_groups,
            "hidden_ipam_permissions": hidden_ipam_permissions,
            "vlan_scope_incomplete": vlan_scope_incomplete,
            "table_rows": table_rows,
            "ignored_count": ignored_count,
            "blocked_port_ids": blocked_port_ids,
            "netbox_only_interfaces": netbox_only_interfaces,
            "migrated": migrated,
            "oob_incomplete": oob_incomplete,
            "oob_blockers": oob_blockers,
            "relationship_diagnostics": relationship_diagnostics,
            "requirement_edges": requirement_edges_by_row,
        }

    def get_context_data(self, request, obj, interface_name_field, server_key=None, sync_device=None):
        """
        Build the context data for the interface sync view, from the stored interfaces snapshot and the user's draft.

        Args:
            request: The current HTTP request.
            obj: The NetBox device (or VC member) being synced.
            interface_name_field: The interface name field preference; resolved from
                the request when None.
            server_key: The LibreNMS server key; resolved from the API client when
                None.
            sync_device: Device that owns the LibreNMS identity and the snapshot.

        Returns:
            dict: The template context (object, table, vlan_groups, server_key,
                oob_incomplete and related render state).

        """
        if interface_name_field is None:
            interface_name_field = get_interface_name_field(request, obj)
        built = self.build_table(request, obj, interface_name_field, server_key, sync_device=sync_device)
        rows, snapshot, server_key = built.rows, built.snapshot, built.server_key
        cache_ttl = source_cache.remaining_ttl(obj, Resource.INTERFACES, server_key) if snapshot is not None else None
        cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl) if cache_ttl else None
        chassis = self.sync_view_scope(obj).chassis
        draft_urls = (
            interface_command_urls(SyncQuery.from_request(request), obj, server_key) if built.draft is not None else {}
        )

        return {
            "object": obj,
            "table": built.table,
            "vlan_groups": rows["vlan_groups"],
            "last_fetched": snapshot.finished_at if snapshot is not None else None,
            "cache_expiry": cache_expiry,
            # The Bulk VC Member dialog offers only the members the user may view.
            "virtual_chassis_members": list(chassis.visible) if getattr(obj, "virtual_chassis", None) else [],
            "interface_name_field": interface_name_field,
            "netbox_only_interfaces": rows["netbox_only_interfaces"],
            "server_key": server_key,
            "oob_incomplete": rows["oob_incomplete"],
            "oob_blockers": rows["oob_blockers"],
            "relationship_diagnostics": rows["relationship_diagnostics"],
            "hidden_ipam_permissions": rows["hidden_ipam_permissions"],
            "vlan_scope_incomplete": rows["vlan_scope_incomplete"],
            "show_ignored": built.show_ignored,
            "ignored_count": rows["ignored_count"],
            "filters": built.filters,
            # The row count before the filter, shown only while a filter narrows the table.
            "filtered_from": built.filtered_from,
            "sync_options": interface_sync_options_menu(request),
            # The data refresh replaced a draft that had rows, so the page says the selection is gone.
            "selection_cleared": built.draft is not None and built.draft.cleared,
            # The Bulk VC Member dialog sends its member to this draft command.
            "member_command_url": draft_urls.get("data-member-url", ""),
            # The selected rows that the shown page does not show, which the submit takes too.
            **built.hidden_selection,
        }

    def build_table(self, request, obj, interface_name_field, server_key=None, *, sync_device=None, extra_keys=()):
        """
        Build the interface table of the whole snapshot, as the user's draft selects it and chooses its rows.

        The tab and the row read call this one builder, so a row read answers the row as the table shows it.

        Args:
            request (HttpRequest): The request; its query holds the filters, the page and the sort.
            obj (Device | VirtualMachine): The object of the sync page.
            interface_name_field (str): The interface name field.
            server_key (str | None): The LibreNMS server key; None resolves it from the request query.
            sync_device (Device | None): The device that owns the snapshot; None resolves it.
            extra_keys (Iterable[str]): Row keys off the page that get the selection state of a page row too.

        Returns:
            BuiltInterfaceTable: The table (None without a snapshot), its filters, the computed rows, the
                draft (None without a snapshot), the snapshot, the server key and the ignored toggle.

        """
        table = None
        filtered_from = None
        hidden = {}
        # Every hop of the tab carries the page query, so the toggle is read from it only.
        show_ignored = show_ignored_interfaces(request)
        # The filters show their errors also when no snapshot is cached.
        filters = parse_table_filters(request.GET, lambda rows: self.get_table(rows, obj, interface_name_field))

        unresolved = False
        if server_key is None:
            # GET render (no POST-resolved key threaded in): rebind + scope the snapshot read
            # to ?server_key (shared helper) so a non-default-server tab reads that server's
            # snapshot, not the default's. An unresolved non-blank key scopes to that key (miss).
            server_key, unresolved = self.resolve_get_render_server_key(request)

        # Every VC member shares the snapshot of the VC sync device. Reuse the device post()
        # already resolved (passed as sync_device); the GET path passes None.
        cache_device = sync_device or get_librenms_sync_device(obj, server_key=server_key) or obj

        # A key that no longer resolves left self.librenms_api bound to the DEFAULT server, so the
        # per-interface librenms_id index (_get_object_librenms_ids) would be keyed on another server
        # than the snapshot. Render empty, scoped to the requested key, instead.
        snapshot = None if unresolved or server_key is None else source_cache.read(obj, Resource.INTERFACES, server_key)
        draft = open_interface_draft(request, obj, server_key, snapshot) if snapshot is not None else None
        choices = InterfaceChoices.of(draft.entries()) if draft is not None else None

        chassis = self.sync_view_scope(obj).chassis
        rows = self._interface_rows(
            request,
            obj,
            interface_name_field,
            server_key,
            cache_device,
            snapshot,
            chassis,
            show_ignored=show_ignored,
            choices=choices,
        )
        if rows["table_rows"] is not None:
            table_rows, migrated, vlan_groups = rows["table_rows"], rows["migrated"], rows["vlan_groups"]

            def build(rows):
                built = self.get_table(rows, obj, interface_name_field, vlan_groups=vlan_groups)
                built.migrated_to_marker = migrated
                return built

            # The filter runs after every whole-snapshot step above, so the counts, the ignored
            # split and the blocked IDs describe the whole device.
            table, filters, filtered_from = filter_sync_table(request, table_rows, build)
            table.configure(request)
            if draft is not None:
                selected = show_interface_selection(
                    request,
                    obj,
                    server_key,
                    table,
                    rows,
                    draft,
                    enabled=interface_sync_options_menu(request)["auto_select_lag_members"],
                    extra_keys=extra_keys,
                )
                hidden = hidden_selection(table, selected, filters, interface_row_key)
        return BuiltInterfaceTable(
            table=table,
            filters=filters,
            filtered_from=filtered_from,
            rows=rows,
            draft=draft,
            snapshot=snapshot,
            server_key=server_key,
            show_ignored=show_ignored,
            hidden_selection=hidden,
        )

    def row_html(self, request, obj, server_key, row_key):
        """
        Return the ``<tr>`` of one interface row as the tab's table renders it, or None when no shown row has *row_key*.

        The row is built with the whole snapshot, the draft and the page query of the request, as in the tab.
        """
        built = self.build_table(
            request, obj, get_interface_name_field(request, obj), server_key, extra_keys=(row_key,)
        )
        if built.table is None:
            return None
        row = next((row for row in built.table.rows if interface_row_key(row.record) == row_key), None)
        return render_rows(request, [row]) if row is not None else None


@dataclass(frozen=True)
class BuiltInterfaceTable:
    """The interface table of one render, and the state it was built from."""

    table: object
    filters: object
    filtered_from: int | None
    rows: dict
    draft: SelectionDraft | None
    snapshot: object
    server_key: str | None
    show_ignored: bool
    # The context of hidden_selection(); empty without a draft.
    hidden_selection: dict


# =============================================================================
# The Interfaces tab on the user's selection draft
# =============================================================================

INTERFACE_TAB = SyncTab.INTERFACES
# A device, a chassis member and a virtual machine show one interface table; the draft key names the subject.
INTERFACE_TABLE_VARIANT = "interfaces"
# The draft choices of a row: its Virtual Chassis member (a Device pk), and the VLAN group pk of each VID ("" is Global).
MEMBER_CHOICE = "member"
VLAN_GROUPS_CHOICE = "vlan_groups"


@dataclass(frozen=True)
class InterfaceChoices:
    """The member and the VLAN groups that the user chose for interface rows, by row key."""

    members: dict = field(default_factory=dict)
    vlan_groups: dict = field(default_factory=dict)

    @classmethod
    def of(cls, entries):
        """Return the choices of the draft *entries*."""
        return cls(
            members={
                key: entry.choices[MEMBER_CHOICE] for key, entry in entries.items() if MEMBER_CHOICE in entry.choices
            },
            vlan_groups={
                key: dict(entry.choices[VLAN_GROUPS_CHOICE])
                for key, entry in entries.items()
                if VLAN_GROUPS_CHOICE in entry.choices
            },
        )

    def digest(self):
        """Return a stable text of the choices, for the key of the computed rows."""
        return json.dumps([self.members, self.vlan_groups], sort_keys=True)


def _interface_draft_identity(request, obj, server_key, snapshot):
    scope = selection_scope(request, obj, server_key, INTERFACE_TAB.value)
    return (request.user, obj, INTERFACE_TAB, INTERFACE_TABLE_VARIANT, server_key, scope, snapshot.snapshot_id)


def open_interface_draft(request, obj, server_key, snapshot):
    """Return the request user's interface draft for a presentation of the snapshot; only the published snapshot starts one."""

    def published():
        current = source_cache.read(obj, Resource.INTERFACES, server_key)
        return current.snapshot_id if current is not None else None

    return SelectionDraft.open(*_interface_draft_identity(request, obj, server_key, snapshot), published=published)


def find_interface_draft(request, obj, server_key, snapshot):
    """Return the request user's stored interface draft of the snapshot that a command read, or None."""
    return SelectionDraft.find(*_interface_draft_identity(request, obj, server_key, snapshot))


def interface_selectable(record):
    """Return whether the user can select the row: it has an enabled checkbox."""
    return record.get("sync_target_resolvable", True) and blocked_row_state(record["rule_decision"]) not in (
        ROW_IGNORED,
        ROW_AMBIGUOUS,
        ROW_INCOMPLETE,
    )


def interface_evidence(obj, server_key, record, table):
    """
    Return the facts that selecting one interface row approves.

    The writer writes the row to its member, onto the NetBox interface the row resolves to, with the
    type the rules plan, its relationship targets and VLAN groups; the row's sync state says what the
    page showed. A change of
    any of them asks the user to select the row again, and the writer compares the member and the
    interface again under its locks. The subject and the server bind the token to its tab.
    """
    interface = record.get("netbox_interface")
    return {
        "subject": [obj._meta.label_lower, obj.pk],
        "server": server_key,
        "row": interface_row_key(record),
        "member": record.get("selected_object_id"),
        "interface": interface.pk if interface is not None else None,
        "state": table.row_sync_state(record).state,
        "type": _proposed_type(record),
        "relations": [record.get(f"librenms_{relation}_port_id") for relation in ("lag", "parent", "bridge")],
        "vlan_groups": {
            str(vid): group.get("group_id", "") for vid, group in (record.get("vlan_group_map") or {}).items()
        },
    }


def _proposed_type(record):
    """Return the type that a sync of the row writes: the planned type of its interface, or the rule's type for a new one."""
    decision = record.get("rule_decision")
    if decision is None:
        return None
    interface = record.get("netbox_interface")
    if interface is None or not record.get("exists_in_netbox"):
        return decision.netbox_type
    return planned_interface_type(interface, decision, created=False).value


def effective_intents(obj, server_key, table, records, entries):
    """
    Return the intent of each draft row that the rule reads: a selection counts only while its evidence is current.

    Args:
        obj (Device | VirtualMachine): The subject of the sync page.
        server_key (str): The LibreNMS server of the tab.
        table (LibreNMSInterfaceTable): A table of the tab, which decides each row's sync state.
        records (dict[str, dict]): The rows of the whole snapshot by row key.
        entries (dict[str, Entry]): The draft entries.

    Returns:
        dict[str, Intent]: ``Intent.ON`` for an approved selection, ``Intent.OFF`` for an exclusion.

    """
    intents = {}
    for row_key, entry in entries.items():
        record = records.get(row_key)
        if entry.intent is Intent.OFF:
            intents[row_key] = Intent.OFF
        elif entry.intent is Intent.ON and record is not None and interface_selectable(record):
            if evidence_problem(entry, interface_evidence(obj, server_key, record, table)) is None:
                intents[row_key] = Intent.ON
    return intents


def show_interface_selection(request, obj, server_key, table, rows, draft, *, enabled, extra_keys=()):
    """
    Add to each page row its checkbox state, its intent, its requirements, its evidence token and its row read URL.

    The rule reads the draft over the whole snapshot: a row on the page shows checked when the user
    selected it, or when a selected row (on any page) requires it while *enabled*. The table is bound to
    the draft and the URLs of its commands.

    Returns:
        list[str]: The keys of the rows that the user selected, in snapshot order, which the submit takes.

    """
    entries = draft.entries()
    records = {key: record for record in rows["table_rows"] if (key := interface_row_key(record)) is not None}
    edges = rows["requirement_edges"]
    blocked = {str(port_id) for port_id in rows["blocked_port_ids"] if port_id is not None}
    intents = effective_intents(obj, server_key, table, records, entries)
    checked = checked_rows(edges, intents, blocked, enabled=enabled)
    page_records = [row.record for row in table.page.object_list] if table.page else list(table.data)
    page_keys = {interface_row_key(record) for record in page_records}
    off_page = off_page_requirers(edges, intents, blocked, page_keys)
    query = SyncQuery.from_request(request)
    shown = page_records + [records[key] for key in extra_keys if key in records and key not in page_keys]
    for record in shown:
        key = interface_row_key(record)
        if key is None:
            continue
        selectable = interface_selectable(record)
        entry = entries.get(key)
        record["selected"] = selectable and key in checked
        record["evidence_changed"] = (
            entry is not None and entry.intent is Intent.ON and intents.get(key) is not Intent.ON
        )
        record["intent"] = intents.get(key, Intent.NONE).value
        record["requires"] = json.dumps([] if key in blocked else sorted(requirements(edges, key, blocked)))
        record["offpage_requirers"] = off_page.get(key, 0)
        record["evidence_token"] = (
            sign_evidence(INTERFACE_TAB, interface_evidence(obj, server_key, record, table)) if selectable else ""
        )
        record["row_read_url"] = row_url(query, obj, server_key, "interface_row", key)
        record["vlan_modal_url"] = row_url(query, obj, server_key, "interface_vlan_modal", key)
    bind_draft(table, query, obj, server_key, draft, INTERFACE_COMMAND_URLS)
    return [key for key, record in records.items() if intents.get(key) is Intent.ON and interface_selectable(record)]


# The draft commands of the interface table, by the table attribute that names each URL.
INTERFACE_COMMAND_URLS = {
    "data-selection-url": "interface_selection",
    "data-choice-url": "interface_selection_choice",
    "data-member-url": "interface_selection_member",
    "data-vlan-groups-url": "interface_selection_vlan_groups",
}


def interface_command_urls(query, obj, server_key):
    """Return the URL of each interface draft command by its table attribute, with the page query and the server."""
    kwargs = {"object_type": obj._meta.model_name, "pk": obj.pk}
    return {
        attribute: query.url(
            reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs=kwargs), {"server_key": server_key}
        )
        for attribute, name in INTERFACE_COMMAND_URLS.items()
    }


def current_interface_rows(request, obj, server_key, snapshot, entries):
    """
    Return the tab view, a table of the tab and the computed rows of the whole *snapshot* with the draft's choices.

    Args:
        request (HttpRequest): The request; its query holds the name field and the ignored toggle.
        obj (Device | VirtualMachine): The subject of the sync page.
        server_key (str): The LibreNMS server of the snapshot.
        snapshot (SourceSnapshot): The stored interfaces snapshot.
        entries (dict[str, Entry]): The draft entries whose choices the rows take.

    Returns:
        tuple: The tab view, a table that decides each row's sync state, and the rows of ``_interface_rows``.

    """
    view = interface_tab_view(request, obj, server_key)
    name_field = get_interface_name_field(request, obj)
    rows = view._interface_rows(
        request,
        obj,
        name_field,
        server_key,
        get_librenms_sync_device(obj, server_key=server_key) or obj,
        snapshot,
        view.sync_view_scope(obj).chassis,
        show_ignored=show_ignored_interfaces(request),
        choices=InterfaceChoices.of(entries),
    )
    return view, view.get_table([], obj, name_field, vlan_groups=rows["vlan_groups"]), rows


def interface_tab_view(request, obj, server_key):
    """Return the interfaces tab view of *obj* for *request*, bound to *server_key*."""
    # Imported here: the object_sync views import this module.
    from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
    from netbox_librenms_plugin.views.object_sync.vms import VMInterfaceTableView

    view = DeviceInterfaceTableView() if isinstance(obj, Device) else VMInterfaceTableView()
    view.setup(request, pk=obj.pk)
    view.rebind_api_for_server(server_key)
    return view


def _canonical_port_key(value):
    """Return *value* when it is the row key of an interface row: a canonical LibreNMS port ID as text."""
    port_id = normalize_librenms_port_id(value) if isinstance(value, str) else None
    if port_id is None or str(port_id) != value:
        raise BadRequest("The request names no valid interface row.")
    return value


class _InterfaceDraftView(DraftView):
    """Resolve the subject, the server and the interfaces snapshot of one request on the user's interface draft."""

    resource = Resource.INTERFACES

    def current_rows(self, request, obj, server_key, entries):
        """Return the tab view and the computed rows of the whole snapshot with the choices of the draft *entries*."""
        snapshot = source_cache.read(obj, Resource.INTERFACES, server_key)
        view, _table, rows = current_interface_rows(request, obj, server_key, snapshot, entries)
        return view, rows

    @staticmethod
    def requirement_marks(request, obj, server_key, snapshot, entries):
        """
        Return, by row key, the requirements that the row renders and the selected rows that require it.

        They give each row's ``data-requires`` and ``data-offpage-requirers`` (see show_interface_selection).
        """
        _view, table, rows = current_interface_rows(request, obj, server_key, snapshot, entries)
        records = {key: record for record in rows["table_rows"] or () if (key := interface_row_key(record)) is not None}
        edges = rows["requirement_edges"]
        blocked = {str(port_id) for port_id in rows["blocked_port_ids"] if port_id is not None}
        requirers = {}
        for key, intent in effective_intents(obj, server_key, table, records, entries).items():
            if intent is Intent.ON and key not in blocked:
                for required in requirements(edges, key, blocked):
                    requirers.setdefault(required, set()).add(key)
        return {
            key: (
                frozenset() if key in blocked else frozenset(requirements(edges, key, blocked)),
                frozenset(requirers.get(key, ())),
            )
            for key in records
        }


class _InterfaceDraftCommandView(DraftCommandView, _InterfaceDraftView):
    """A command on the user's interface draft."""

    @staticmethod
    def draft(request, obj, server_key, snapshot):
        """Return the stored draft of the snapshot that the command read; None (a conflict) when a newer one replaced it."""
        return find_interface_draft(request, obj, server_key, snapshot) if snapshot is not None else None

    def row(self, request, obj, server_key, draft, row_key):
        """Return the tab view and the current row *row_key* with the draft's choices; Http404 when no shown row has it."""
        view, rows = self.current_rows(request, obj, server_key, draft.entries())
        record = next(
            (record for record in rows["table_rows"] or () if interface_row_key(record) == row_key),
            None,
        )
        if record is None:
            raise Http404("No current interface row has this key.")
        return view, rows, record


class InterfaceSelectionView(_InterfaceDraftCommandView):
    """Set the absolute selection state of interface rows in the user's draft; the page's unload beacon posts here too."""

    def post(self, request, object_type, pk):
        """Apply one batch of row states: ``draft_id``, ``expected_revision`` and ``assignments`` (JSON)."""
        obj, server_key, snapshot, error = self.subject(request, object_type, pk)
        if error:
            return error
        draft_id, expected_revision = self.draft_version(request.POST)
        assignments = parse_assignments(
            request.POST.get("assignments"), INTERFACE_TAB, obj, server_key, _canonical_port_key
        )
        if (draft := self.draft(request, obj, server_key, snapshot)) is None:
            return self.conflict()
        try:
            revision = draft.assign(draft_id, expected_revision, assignments)
        except DraftConflict:
            return self.conflict()
        return self.acknowledge(revision)


class InterfaceChoiceView(_InterfaceDraftCommandView):
    """
    Store the Virtual Chassis member that the user chose for one interface row, then name what the page reads again.

    A member can unblock or block the row, which changes the requirements that other rows render. Then the
    page reads the whole tab; otherwise it reads the chosen row only.
    """

    def post(self, request, object_type, pk):
        """Store ``value`` (a member pk; "" removes the choice) as the member of the row ``row`` (``field`` is ``member``)."""
        obj, server_key, snapshot, error = self.subject(request, object_type, pk)
        if error:
            return error
        draft_id, expected_revision = self.draft_version(request.POST)
        row_key = _canonical_port_key(request.POST.get("row"))
        if request.POST.get("field") != MEMBER_CHOICE:
            raise BadRequest("An interface row has a member choice only.")
        value = _member_value(self, obj, request.POST.get("value"))
        if (draft := self.draft(request, obj, server_key, snapshot)) is None:
            return self.conflict()
        if row_key not in _snapshot_row_keys(obj, server_key):
            raise Http404("No current interface row has this key.")
        earlier = draft.entries()
        try:
            revision = draft.choose(draft_id, expected_revision, row_key, MEMBER_CHOICE, value)
        except DraftConflict:
            return self.conflict()
        try:
            before = self.requirement_marks(request, obj, server_key, snapshot, earlier)
            after = self.requirement_marks(request, obj, server_key, snapshot, draft.entries())
        except RuleConfigurationError:
            # The choice is stored; the row read evaluates the rules again and names the broken one.
            return self.acknowledge(revision, {ROW_STALE_EVENT: {"key": row_key}})
        if any(before.get(key) != after.get(key) for key in (before.keys() | after.keys()) - {row_key}):
            return self.acknowledge(revision, {RESULTS_STALE_EVENT: None})
        return self.acknowledge(revision, {ROW_STALE_EVENT: {"key": row_key}})


class InterfaceMemberView(_InterfaceDraftCommandView):
    """Choose one Virtual Chassis member for every selected interface row of the user's draft (Bulk Edit VC Member)."""

    def post(self, request, object_type, pk):
        """Store ``value`` (a member pk) as the member of each row that the draft selects."""
        obj, server_key, snapshot, error = self.subject(request, object_type, pk)
        if error:
            return error
        draft_id, expected_revision = self.draft_version(request.POST)
        value = _member_value(self, obj, request.POST.get("value"))
        if value is None:
            raise BadRequest("Name the member for the selected rows.")
        if (draft := self.draft(request, obj, server_key, snapshot)) is None:
            return self.conflict()
        keys = _snapshot_row_keys(obj, server_key)
        values = {key: value for key, entry in draft.entries().items() if entry.intent is Intent.ON and key in keys}
        try:
            revision = draft.choose_rows(draft_id, expected_revision, MEMBER_CHOICE, values)
        except DraftConflict:
            return self.conflict()
        return self.acknowledge(revision, {RESULTS_STALE_EVENT: None})


class InterfaceVlanGroupsView(_InterfaceDraftCommandView):
    """Store the VLAN group of each VID of one interface row, or of every row with that VID ("apply to all")."""

    def post(self, request, object_type, pk):
        """Store ``group_<vid>`` (a VLAN group pk; "" is Global) for the row ``row``, and ``apply_to_all`` for every row."""
        obj, server_key, snapshot, error = self.subject(request, object_type, pk)
        if error:
            return error
        draft_id, expected_revision = self.draft_version(request.POST)
        row_key = _canonical_port_key(request.POST.get("row"))
        if (draft := self.draft(request, obj, server_key, snapshot)) is None:
            return self.conflict()
        _view, rows, record = self.row(request, obj, server_key, draft, row_key)
        chosen = _posted_vlan_groups(request.POST, record)
        targets = rows["table_rows"] if request.POST.get("apply_to_all") else [record]
        entries = draft.entries()
        values = {}
        for target in targets:
            key = interface_row_key(target)
            groups = {vid: group for vid, group in chosen.items() if _offers_vlan_group(target, vid, group)}
            if key is None or not groups:
                continue
            stored = dict(entries[key].choices.get(VLAN_GROUPS_CHOICE, {})) if key in entries else {}
            values[key] = {**stored, **groups}
        try:
            revision = draft.choose_rows(draft_id, expected_revision, VLAN_GROUPS_CHOICE, values)
        except DraftConflict:
            return self.conflict()
        trigger = (
            {RESULTS_STALE_EVENT: None} if request.POST.get("apply_to_all") else {ROW_STALE_EVENT: {"key": row_key}}
        )
        return self.acknowledge(revision, trigger)


class InterfaceRowView(_InterfaceDraftView):
    """Answer one interface row as the tab's table renders it, for the page's row read."""

    def get(self, request, object_type, pk, row_key):
        """Render the ``<tr>`` of the interface row *row_key* from the stored snapshot and the user's draft."""
        obj, server_key, snapshot, error = self.subject(request, object_type, pk)
        if error:
            return error
        if snapshot is None:
            raise Http404("The interfaces snapshot is not available.")
        html = interface_tab_view(request, obj, server_key).row_html(request, obj, server_key, row_key)
        if html is None:
            raise Http404("No current interface row has this key.")
        return row_response(html)


class InterfaceVlanModalView(_InterfaceDraftView):
    """Render the VLAN groups dialog of one interface row; a ``group_<vid>`` query previews that group's colours."""

    template_name = "netbox_librenms_plugin/htmx/interface_vlan_groups.html"

    def get(self, request, object_type, pk, row_key):
        """Render the dialog content of the row *row_key*, with the draft's groups or the previewed ones."""
        obj, server_key, snapshot, error = self.subject(request, object_type, pk)
        if error:
            return error
        if snapshot is None:
            raise Http404("The interfaces snapshot is not available.")
        draft = open_interface_draft(request, obj, server_key, snapshot)
        view, rows = self.current_rows(request, obj, server_key, draft.entries())
        record = next((r for r in rows["table_rows"] or () if interface_row_key(r) == row_key), None)
        if record is None or record.get("member_withheld") or not record.get("sync_target_resolvable", True):
            raise Http404("No current interface row with VLAN groups has this key.")
        # A preview shows the groups that the dialog shows now, without a change to the draft.
        groups = {
            str(vid): group.get("group_id", "") for vid, group in (record.get("vlan_group_map") or {}).items()
        } | _posted_vlan_groups(request.GET, record)
        table = view.get_table([record], obj, get_interface_name_field(request, obj), vlan_groups=rows["vlan_groups"])
        # The dialog's own fields ride on its preview request, never on the URLs it renders.
        page_query = request.GET.copy()
        for key in [key for key in page_query if key in ("row", "apply_to_all") or key.startswith("group_")]:
            page_query.pop(key)
        query = SyncQuery(page_query)
        context = {
            "row_key": row_key,
            "interface_name": record.get(table.interface_name_field) or "",
            "vlans": table.vlan_modal_rows(record, groups, _vids_missing_in_groups(request.user, groups)),
            "groups": record.get("vlan_groups") or [],
            "command_url": interface_command_urls(query, obj, server_key)["data-vlan-groups-url"],
            "preview_url": row_url(query, obj, server_key, "interface_vlan_modal", row_key),
            "apply_to_all": bool(request.GET.get("apply_to_all")),
        }
        response = render(request, self.template_name, context)
        response["Cache-Control"] = "no-store"
        return response


def _vids_missing_in_groups(user, groups):
    """Return the VIDs of ``{vid text: group pk text}`` whose group ("" is Global) holds no VLAN the user can view, and no global one."""
    vids = [int(vid) for vid in groups]
    group_ids = {coerce_model_pk(group) for group in groups.values() if group}
    available = set(
        VLAN.objects.restrict(user, "view")
        .filter(Q(group__isnull=True) | Q(group_id__in=group_ids), vid__in=vids)
        .values_list("vid", "group_id")
    )
    return {
        vid
        for vid, group in groups.items()
        if (int(vid), coerce_model_pk(group) if group else None) not in available and (int(vid), None) not in available
    }


def _member_value(view, obj, raw):
    """Return the member pk text that a command names, None for "", or raise when it names no visible member."""
    if raw == "":
        return None
    if not getattr(obj, "virtual_chassis", None):
        raise BadRequest("Only a Virtual Chassis member has a member choice.")
    member_id = coerce_model_pk(raw)
    if member_id is None or member_id not in set(view.sync_view_scope(obj).chassis.visible_ids):
        raise Http404("No Virtual Chassis member you can view has this ID.")
    return member_id


def _snapshot_row_keys(obj, server_key):
    """Return the row keys of the stored interfaces snapshot."""
    assessment = read_interface_assessment(obj, server_key, "ifName")
    if assessment is None:
        return set()
    return {key for port in assessment["ports"] if (key := interface_row_key(port)) is not None}


def _posted_vlan_groups(data, record):
    """Return the ``group_<vid>`` values of *data* for the VIDs of *record*, as ``{vid text: group pk text}``."""
    vids = [str(vid) for vid in ([record.get("untagged_vlan")] if record.get("untagged_vlan") else [])]
    vids += [str(vid) for vid in record.get("tagged_vlans") or ()]
    groups = {}
    for vid in vids:
        value = data.get(f"group_{vid}")
        if value is None:
            continue
        if value != "" and coerce_model_pk(value) is None:
            raise BadRequest(f"The VLAN group of VLAN {vid} is not valid.")
        groups[vid] = value
    return groups


def _offers_vlan_group(record, vid, group):
    """Return whether the row has VLAN *vid* and offers the group *group* ("" is Global) for it."""
    vids = {str(record.get("untagged_vlan"))} | {str(tagged) for tagged in record.get("tagged_vlans") or ()}
    if vid not in vids or record.get("member_withheld") or not record.get("sync_target_resolvable", True):
        return False
    return group == "" or coerce_model_pk(group) in {candidate.pk for candidate in record.get("vlan_groups") or ()}
