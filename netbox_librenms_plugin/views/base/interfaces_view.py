import json
import logging
from types import SimpleNamespace

from django.contrib import messages
from django.core.cache import cache
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.assessment.interfaces import assess_interfaces
from netbox_librenms_plugin.constants import (
    HOST_NAME_COLLISION_REASON,
    OOB_INVENTORY_SOURCE,
    REPORTED_NAME_PORT_COLLISION_REASON,
)
from netbox_librenms_plugin.interface_diff import interface_enabled_from_port
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
from netbox_librenms_plugin.server_mappings import read_mapping, read_mappings
from netbox_librenms_plugin.server_selection import get_librenms_sync_device
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, request_actor_id
from netbox_librenms_plugin.sync_filters import filter_sync_table, parse_table_filters, show_ignored_interfaces
from netbox_librenms_plugin.utils import (
    build_migrated_context,
    get_interface_name_field,
    get_interface_port_identity_sets,
    interface_sync_options_menu,
    reported_name_owners,
    resolve_interface_row_device,
    synced_interface_names,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    VlanAssignmentMixin,
)

logger = logging.getLogger(__name__)


def read_interface_assessment(obj, server_key, interface_name_field):
    """
    Return the stored interfaces snapshot of *obj*, assessed for *interface_name_field*.

    Every interface reader (the tab, the verify, the sync and the relationship actions) reads through
    this function, so they all show and act on one derivation.

    Returns:
        dict | None: The :func:`assess_interfaces` result, or None when no snapshot can be read.

    """
    snapshot = source_cache.read(obj, Resource.INTERFACES, server_key)
    return assess_interfaces(snapshot, interface_name_field) if snapshot is not None else None


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

    def get_ip_address(self, obj):
        """Get the primary IP address for the object."""
        if obj.primary_ip:
            return str(obj.primary_ip.address.ip)
        return None

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

        result = source_cache.refresh(obj, Resource.INTERFACES, server_key)
        if result.error is not None:
            messages.error(request, f"Failed to fetch interfaces from LibreNMS: {result.error}")
            return self._failure_response(request, obj, interface_name_field, server_key, sync_device)

        # The host rows stay usable when only the OOB controller's ports are missing.
        oob_blocked = "oob" in result.blocked
        if oob_blocked:
            notice = source_cache.blocker_notice(result.blocked["oob"])
            logger.warning("Interfaces of %s refreshed without OOB rows: %s", sync_device, notice)
            messages.warning(request, f"Interfaces refreshed without the OOB controller rows: {notice}")
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
        virtual_chassis_members,
        *,
        show_ignored,
    ):
        """Return the computed rows, reused while the snapshot, the viewer and the NetBox rows they read are unchanged."""

        def compute():
            assessment = assess_interfaces(snapshot, interface_name_field) if snapshot is not None else None
            return self._compute_interface_rows(
                request,
                obj,
                interface_name_field,
                server_key,
                cache_device,
                assessment,
                virtual_chassis_members,
                show_ignored=show_ignored,
            )

        if snapshot is None:
            return compute()
        owner_ids = [owner.pk for owner in virtual_chassis_members or [obj]]
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
            cache.get(self.get_vlan_overrides_key(cache_device, server_key)),
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
        virtual_chassis_members,
        *,
        show_ignored,
    ):
        """
        Compute the rows of the interface table and the device-wide state around them.

        This reads every NetBox interface of the device (and of each Virtual Chassis member) and compares
        it with the LibreNMS snapshot, so it is the expensive part of a render.

        Returns:
            dict: The VLAN scope, the table rows (None without a snapshot), the counts, the NetBox-only list
                and the snapshot state the tab reports.

        """
        table_rows = None
        oob_incomplete = False
        oob_blockers = ()
        relationship_diagnostics = None
        netbox_only_interfaces = []
        ignored_count = 0
        blocked_port_ids = []
        actionable_owner_ids = set()
        migrated = False
        # Include every member's scope so rows owned by another member can resolve their VLANs.
        vlan_scope_devices = virtual_chassis_members or [obj]
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

        # Load any user VLAN group overrides from cache (set by "apply to all")
        # Read overrides under the same VC-scoped key SaveVlanGroupOverridesView writes
        # (the sync device, not the viewed member) — otherwise an "apply to all" made on a VC
        # member is lost on the next render/member switch. cache_device mirrors that writer.
        vlan_group_overrides = {}
        if server_key is not None:
            vlan_group_overrides = cache.get(self.get_vlan_overrides_key(cache_device, server_key)) or {}

        if assessment is not None:
            ports_data = assessment["ports"]
            oob_incomplete = assessment["oob_incomplete"]
            oob_blockers = assessment["oob_blockers"]
            # What the device's port_stack held and what each rule made of it, for the rows below.
            relationship_diagnostics = relationship_diagnostics_report(assessment, interface_name_field)
            matched_interface_ids = set()

            relationship_maps = build_relationship_maps(assessment)
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
            actionable_owner_ids = set(
                obj.__class__.objects.restrict(request.user, "view")
                .filter(pk__in=interfaces_by_device)
                .values_list("pk", flat=True)
            )
            scoped_interfaces = interface_model.objects.filter(**{f"{owner_field}_id__in": interfaces_by_device})
            viewable_interface_ids = set(scoped_interfaces.restrict(request.user, "view").values_list("pk", flat=True))
            changeable_interface_ids = set(
                scoped_interfaces.restrict(request.user, "change").values_list("pk", flat=True)
            )
            related_interface_ids = viewable_interface_ids | changeable_interface_ids
            can_write_relationships = self.has_write_permission()
            related_interfaces_by_port_id = {}
            related_interfaces_by_name = {}
            for interface_maps in interfaces_by_device.values():
                for port_id, interfaces in interface_maps["by_librenms_id_matches"].items():
                    permitted = [
                        interface
                        for interface in interfaces
                        if interface.pk in related_interface_ids
                        and getattr(interface, f"{owner_field}_id") in actionable_owner_ids
                    ]
                    if permitted:
                        related_interfaces_by_port_id.setdefault(port_id, []).extend(permitted)
                for interface_name, interface in interface_maps["by_name"].items():
                    if (
                        interface.pk in related_interface_ids
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
                can_write=can_write_relationships,
            )

            rules = interface_rules_for_request(request)
            # A row with no resolved member is decided on every member's platform.
            member_platform_ids = sorted({member.platform_id for member in virtual_chassis_members}, key=str)
            target_device_ids = {}
            for port in ports_data:
                port_id = normalize_librenms_port_id(port.get("port_id"))
                if port_id is None:
                    continue
                if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                    target_device = resolve_interface_row_device(
                        obj,
                        port,
                        interface_name_field,
                        interfaces_by_port_id=interfaces_by_port_id,
                        members_by_position=members_by_position,
                        members_by_id=members_by_id,
                    )
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
                    rule_owner = resolve_interface_row_device(
                        obj,
                        port,
                        interface_name_field,
                        interfaces_by_port_id=interfaces_by_port_id,
                        members_by_position=members_by_position,
                        members_by_id=members_by_id,
                        return_device_on_failure=False,
                    ) or (obj if port.get("_source") == OOB_INVENTORY_SOURCE else None)
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
                port["selected_object_id"] = chassis_member.pk
                port["sync_target_resolvable"] = (
                    chassis_member.pk in actionable_owner_ids and port_id in unique_host_port_ids
                )
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
                self._add_vlan_group_selection(port, row_lookup_maps, chassis_member, vlan_group_overrides)

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
            # Every row the write check refuses, or that has no owner: the browser walk skips them.
            blocked_port_ids = [
                normalize_librenms_port_id(port.get("port_id"))
                for port in ports_data
                if port["rule_decision"] is None or decision_reason(port["rule_decision"]) is not None
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
            "actionable_owner_ids": actionable_owner_ids,
            "migrated": migrated,
            "oob_incomplete": oob_incomplete,
            "oob_blockers": oob_blockers,
            "relationship_diagnostics": relationship_diagnostics,
        }

    def get_context_data(self, request, obj, interface_name_field, server_key=None, sync_device=None):
        """
        Build the context data for the interface sync view, from the stored interfaces snapshot.

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
        table = None
        filtered_from = None
        # Every hop of the tab carries the page query, so the toggle is read from it only.
        show_ignored = show_ignored_interfaces(request)

        if interface_name_field is None:
            interface_name_field = get_interface_name_field(request, obj)
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

        virtual_chassis_members = []
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            virtual_chassis_members = list(obj.virtual_chassis.members.all())

        rows = self._interface_rows(
            request,
            obj,
            interface_name_field,
            server_key,
            cache_device,
            snapshot,
            virtual_chassis_members,
            show_ignored=show_ignored,
        )
        vlan_groups = rows["vlan_groups"]
        hidden_ipam_permissions = rows["hidden_ipam_permissions"]
        vlan_scope_incomplete = rows["vlan_scope_incomplete"]
        ignored_count = rows["ignored_count"]
        blocked_port_ids = rows["blocked_port_ids"]
        netbox_only_interfaces = rows["netbox_only_interfaces"]
        if rows["table_rows"] is not None:
            table_rows, actionable_owner_ids, migrated = (
                rows["table_rows"],
                rows["actionable_owner_ids"],
                rows["migrated"],
            )

            def build_table(rows):
                built = self.get_table(rows, obj, interface_name_field, vlan_groups=vlan_groups)
                built.allowed_vc_member_ids = actionable_owner_ids
                built.migrated_to_marker = migrated
                return built

            # The filter runs after every whole-snapshot step above, so the counts, the ignored
            # split and the blocked IDs describe the whole device.
            table, filters, filtered_from = filter_sync_table(request, table_rows, build_table)
            table.configure(request)

        cache_ttl = source_cache.remaining_ttl(obj, Resource.INTERFACES, server_key) if snapshot is not None else None
        cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl) if cache_ttl else None

        return {
            "object": obj,
            "table": table,
            "vlan_groups": vlan_groups,
            "last_fetched": snapshot.finished_at if snapshot is not None else None,
            "cache_expiry": cache_expiry,
            "virtual_chassis_members": virtual_chassis_members,
            "interface_name_field": interface_name_field,
            "netbox_only_interfaces": netbox_only_interfaces,
            "server_key": server_key,
            "oob_incomplete": rows["oob_incomplete"],
            "oob_blockers": rows["oob_blockers"],
            "relationship_diagnostics": rows["relationship_diagnostics"],
            "hidden_ipam_permissions": hidden_ipam_permissions,
            "vlan_scope_incomplete": vlan_scope_incomplete,
            "show_ignored": show_ignored,
            "ignored_count": ignored_count,
            "filters": filters,
            # The row count before the filter, shown only while a filter narrows the table.
            "filtered_from": filtered_from,
            "sync_options": interface_sync_options_menu(request),
            # The browser's related-row walk skips these, on this page and on every other.
            "blocked_port_ids_json": json.dumps([str(port_id) for port_id in blocked_port_ids if port_id is not None]),
        }
