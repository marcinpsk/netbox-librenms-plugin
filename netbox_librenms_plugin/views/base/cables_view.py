import logging
from collections import defaultdict
from urllib.parse import quote_plus

from dcim.constants import VIRTUAL_IFACE_TYPES
from dcim.models import Cable, ConsolePort, ConsoleServerPort, Device, Interface
from django.contrib import messages
from django.core.cache import cache
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.views import View

from netbox_librenms_plugin.cable_scope import remote_name_candidates as _remote_name_candidates
from netbox_librenms_plugin.cable_scope import resolve_remote_device
from netbox_librenms_plugin.breakout_cables import (
    HIDDEN_REASONS,
    breakout_far_end,
    decide_breakout_cable,
    decide_channel_far_end,
    hidden_breakout_refusal,
    holds_a_cable,
    is_channel,
)
from netbox_librenms_plugin.constants import (
    MATCHING,
    MISMATCHED,
    NOT_IN_NETBOX,
    OOB_INVENTORY_SOURCE,
    SERIAL_INVENTORY_SOURCE,
    UNCLASSIFIED,
)
from netbox_librenms_plugin.interface_rules import interface_rules_for_request, row_rule_block
from netbox_librenms_plugin.librenms_ids import coerce_librenms_id
from netbox_librenms_plugin.server_mappings import (
    AmbiguousLibreNMSIdError,
    MappingRole,
    find_port_owner,
    port_holders,
    identity_q,
    name_match_may_be_port,
    read_mapping,
    resolve_device_port,
)
from netbox_librenms_plugin.server_selection import get_librenms_sync_device
from netbox_librenms_plugin.sync_cache import (
    SyncCacheConsistency,
    SyncTab,
    request_actor_id,
    apply_cable_manual_picks,
    cable_manual_pick_cache_key,
)
from netbox_librenms_plugin.sync_filters import filter_sync_table, parse_table_filters
from netbox_librenms_plugin.sync_query import SyncQuery
from netbox_librenms_plugin.tables.cables import SYNC_CABLE_LABEL
from netbox_librenms_plugin.utils import (
    cable_far_terminations,
    cable_has_librenms_tag,
    cable_is_point_to_point,
    cable_path_reaches,
    get_interface_name_field,
    get_librenms_cable_tag,
    get_virtual_chassis_member,
    oob_badge_html,
    PortDisclosure,
    remote_port_html,
    rule_block_html,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    parse_request_json,
    server_unavailable_json,
)

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.coordinator import resource_owner
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.assessment.cables import (
    assess,
    remote_port_ref,
    dedupe_protocol_duplicates,
    also_reported_by,
)

logger = logging.getLogger(__name__)


def read_cable_assessment(request, subject, server_key, interface_name_field="ifName"):
    """Read the bound source snapshot and assess its cable rows for the current name option."""
    snapshot = source_cache.read(subject, Resource.CABLES, server_key)
    if snapshot is None:
        return None
    assessed = assess(
        snapshot,
        subject=subject,
        interface_name_field=interface_name_field,
        include_serial=source_cache.can_view_serial_ports(
            request.user, resource_owner(subject, Resource.CABLES, server_key)
        ),
    )
    disclosed = source_cache.cable_blockers_for_user(
        snapshot, request.user, {"display": assessed["blockers"], **assessed["blocked_scopes"]}
    )
    return {
        **assessed,
        "blockers": disclosed.pop("display"),
        "blocked_scopes": disclosed,
        "snapshot_id": snapshot.snapshot_id,
        "last_fetched": snapshot.finished_at,
    }


def _serial_remote_labels(links):
    """Keep usable serial labels for the page's name catalog."""
    return {
        label
        for link in links
        if link.get("_source") == SERIAL_INVENTORY_SOURCE
        and isinstance((label := link.get("remote_device")), str)
        and label.strip()
    }


def _remote_name_queries(names):
    """Build bounded case-insensitive name queries for the direct resolver."""
    sorted_names = sorted(names)
    for offset in range(0, len(sorted_names), 32):
        chunk = sorted_names[offset : offset + 32]
        name_q = Q(name__iexact=chunk[0])
        for name in chunk[1:]:
            name_q |= Q(name__iexact=name)
        yield name_q


def _remote_name_device_pks(names):
    """Load names with the same case-insensitive lookup as the direct resolver."""
    device_pks = set()
    for name_q in _remote_name_queries(names):
        device_pks.update(Device.objects.filter(name_q).values_list("pk", flat=True))
    return device_pks


def _remote_name_devices(names):
    """Load each case-insensitive name match once in primary-key order."""
    by_pk = {}
    for name_q in _remote_name_queries(names):
        by_pk.update((device.pk, device) for device in Device.objects.filter(name_q))
    return [by_pk[pk] for pk in sorted(by_pk)]


def _remote_port_name_candidates(row):
    """Every LibreNMS name the far end of *row* is known by: advertised first, then the aliases."""
    names = [row.get("remote_port"), *(row.get("remote_port_aliases") or [])]
    return [name for name in names if isinstance(name, str) and name]


def port_owner_id(row, side):
    """
    Return the pk of the NetBox device that owns the row's *side* LibreNMS port, or None.

    The remote owner is the resolved neighbour (or its chassis member); a manual pick never
    changes it. None means no owner is known, and the port is decided with no platform.
    """
    key = "netbox_local_device_id" if side == "local" else "remote_port_owner_id"
    return coerce_librenms_id(row.get(key))


def cable_row_ports(row):
    """
    Return the LibreNMS ports a cable row names, with the records its snapshot cached for them.

    A serial row names no LibreNMS port. The remote end is the one port the write touches
    (:func:`remote_port_ref`); only the matched port record has a cached record.

    Args:
        row (dict): A cable row.

    Returns:
        list[tuple[str, int, dict | None]]: ``(side, port_id, record)``; side is ``local`` or ``remote``.

    """
    if row.get("_source") == SERIAL_INVENTORY_SOURCE:
        return []

    def cached(key):
        record = row.get(key)
        return record if isinstance(record, dict) else None

    ports = []
    if (local_id := coerce_librenms_id(row.get("local_port_id"))) is not None:
        ports.append(("local", local_id, cached("local_port_record")))
    if (remote_id := remote_port_ref(row)) is not None:
        matched = remote_id == coerce_librenms_id(row.get("remote_port_key"))
        ports.append(("remote", remote_id, cached("remote_port_record") if matched else None))
    return ports


CABLE_MODAL_QUERY_KEYS = ("row_id", "expected_local_id", "action", "source", "q", "device_id")


def _set_cable_state(link, status, comparison):
    """
    Set the cable status text of a row and its comparison with the desired connection.

    The comparison is the row's sync-state filter value: ``MATCHING`` (the desired connection is
    present, also through a patch path), ``MISMATCHED`` (it reaches another endpoint, or the cable
    still needs adoption), ``NOT_IN_NETBOX`` (the cable is confirmed absent) or ``UNCLASSIFIED``
    (the evidence is hidden, unresolved or refused). Every status write sets both, so they cannot drift.

    Args:
        link (dict): The cable row, updated in place.
        status (str): The text of the Cable Status cell.
        comparison (str): One of the four comparison values.

    """
    link["cable_status"] = status
    link["cable_comparison"] = comparison


class BaseCableTableView(
    NetBoxObjectPermissionMixin,
    LibreNMSPermissionMixin,
    LibreNMSAPIMixin,
    CacheMixin,
    View,
):
    """Base view for synchronizing cable information from LibreNMS."""

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_cable_sync_content.html"
    required_object_permissions = {"GET": [("view", Device)], "POST": [("view", Device)]}

    def get_object(self, pk):
        """Retrieve the object (Device or VirtualMachine)."""
        return self.restrict_object_or_404(self.model, pk=pk)

    def _viewable_queryset(self, model):
        """Return a read-scoped queryset during a real authenticated request."""
        user = getattr(getattr(self, "request", None), "user", None)
        if getattr(user, "is_authenticated", False) is True:
            return model.objects.restrict(user, "view")
        return model.objects.all()

    def _changeable_queryset(self, model):
        """Return a change-scoped queryset during a real authenticated request."""
        user = getattr(getattr(self, "request", None), "user", None)
        if getattr(user, "is_authenticated", False) is True:
            return model.objects.restrict(user, "change")
        return model.objects.all()

    def _object_is_viewable(self, obj):
        """Return whether the request user may view one current NetBox object."""
        return self._viewable_queryset(type(obj)).filter(pk=obj.pk).exists()

    def _viewable_sync_device(self, obj, server_key):
        """
        Return the chassis sync owner only when the request may view it.

        A chassis with no resolvable sync member falls back to the page object, which the caller
        already resolved through a restricted queryset. A member that resolves but is out of the
        request's scope returns None instead, so the page cannot act under a hidden owner.

        Args:
            obj: The page object (Device or VirtualMachine) the tab was opened on.
            server_key (str): LibreNMS server key scoping the sync-member lookup.

        Returns:
            Device | None: The viewable sync owner, or None when it is out of scope.

        """
        sync_device = get_librenms_sync_device(obj, server_key=server_key) or obj
        if not self._object_is_viewable(sync_device):
            return None
        return sync_device

    def _apply_termination_change_scope(self, links, preloaded_changeable_ids=None):
        """Disable cable writes unless both resolved terminations are changeable."""
        required_ids = defaultdict(set)
        row_requirements = []
        for link in links:
            requirements = []
            if link.get("can_create_cable"):
                if link.get("_source") == SERIAL_INVENTORY_SOURCE:
                    requirements = [
                        (ConsoleServerPort, coerce_librenms_id(link.get("netbox_local_interface_id"))),
                        (ConsolePort, coerce_librenms_id(link.get("netbox_remote_interface_id"))),
                    ]
                else:
                    requirements = [
                        (Interface, coerce_librenms_id(link.get("netbox_local_interface_id"))),
                        (Interface, coerce_librenms_id(link.get("netbox_remote_interface_id"))),
                    ]
                for model, object_id in requirements:
                    if object_id is not None:
                        required_ids[model].add(object_id)
            row_requirements.append((link, requirements))

        changeable_ids = {model: set(object_ids) for model, object_ids in (preloaded_changeable_ids or {}).items()}
        for model, object_ids in required_ids.items():
            if model not in changeable_ids:
                changeable_ids[model] = set(
                    self._changeable_queryset(model).filter(pk__in=object_ids).values_list("pk", flat=True)
                )
        for link, requirements in row_requirements:
            if requirements and any(
                object_id is None or object_id not in changeable_ids.get(model, set())
                for model, object_id in requirements
            ):
                link["can_create_cable"] = False

    def _provenance_tag(self):
        """Return the request's provenance tag without repeating settings/tag queries per row."""
        if not hasattr(self, "_cable_provenance_tag"):
            self._cable_provenance_tag = get_librenms_cable_tag(create=False)
        return self._cable_provenance_tag

    def _cable_has_provenance(self, cable):
        """Check the request's provenance tag on *cable*."""
        return cable_has_librenms_tag(cable, self._provenance_tag())

    def _build_trace_visibility(self, paths):
        """Resolve every object permission used by a page of cable traces in bounded queries."""
        object_ids = defaultdict(set)
        for path in paths:
            for near, segment_cables, far in path or []:
                for termination in (*list(near or []), *list(far or [])):
                    object_ids[type(termination)].add(termination.pk)
                    device = getattr(termination, "device", None)
                    if device is not None:
                        object_ids[type(device)].add(device.pk)
                cables = segment_cables if isinstance(segment_cables, (list, tuple)) else [segment_cables]
                for cable in cables:
                    if cable is not None:
                        object_ids[type(cable)].add(cable.pk)
        return {
            model: set(self._viewable_queryset(model).filter(pk__in=ids).values_list("pk", flat=True))
            for model, ids in object_ids.items()
        }

    def _trace_is_viewable(self, path, visible_ids=None):
        """Return whether every cable, termination, and owning device in a trace is viewable."""

        def is_viewable(obj):
            if visible_ids is None:
                return self._object_is_viewable(obj)
            return obj.pk in visible_ids.get(type(obj), set())

        for near, segment_cables, far in path or []:
            for termination in (*list(near or []), *list(far or [])):
                if not is_viewable(termination):
                    return False
                device = getattr(termination, "device", None)
                if device is not None and not is_viewable(device):
                    return False
            cables = segment_cables if isinstance(segment_cables, (list, tuple)) else [segment_cables]
            if any(cable is not None and not is_viewable(cable) for cable in cables):
                return False
        return True

    @staticmethod
    def _first_catalog_match(matches, visible_ids):
        """Return one visible catalog match, while hidden or duplicate evidence blocks fallback."""
        if len(matches) != 1:
            return None, bool(matches)
        match = matches[0]
        return (match if match.pk in visible_ids else None), True

    def _build_normal_link_context(self, links, obj, server_key):
        """Load normal LLDP/CDP resolution and permission candidates once per snapshot."""
        normal_links = [link for link in links if link.get("_source") != SERIAL_INVENTORY_SOURCE]
        if not normal_links:
            return None

        devices_by_name, devices_by_librenms_id, visible_device_ids = self._load_remote_device_catalog(
            normal_links, server_key
        )
        remote_device_by_link = self._resolve_remote_devices(
            normal_links, devices_by_name, devices_by_librenms_id, visible_device_ids
        )
        remote_owner_by_link = self._resolve_remote_owners(normal_links, remote_device_by_link)
        chassis_members, local_owner_by_link = self._resolve_local_owners(normal_links, obj, server_key)
        manual_ids, candidate_specs = self._collect_interface_candidates(
            normal_links, local_owner_by_link, remote_owner_by_link
        )
        owner_ids = {device.pk for device in chassis_members}
        owner_ids.update(device.pk for device in remote_owner_by_link.values() if device is not None)
        context = self._load_interface_catalog(candidate_specs, manual_ids, owner_ids, server_key)
        context["local_owner_by_link"] = local_owner_by_link
        context["remote_device_by_link"] = remote_device_by_link
        context["remote_owner_by_link"] = remote_owner_by_link
        trace_paths = self._load_link_traces(normal_links, context, local_owner_by_link, remote_owner_by_link)
        context["trace_paths"] = trace_paths
        context["trace_visibility"] = self._build_trace_visibility(trace_paths.values())
        return context

    def _load_remote_device_catalog(self, normal_links, server_key):
        """Load every device a normal row could name, indexed by name and by LibreNMS id."""
        remote_names = {
            candidate for link in normal_links for candidate in _remote_name_candidates(link.get("remote_device"))
        }
        remote_ids = {
            remote_id
            for link in normal_links
            if (remote_id := coerce_librenms_id(link.get("remote_device_id"))) is not None
        }
        device_pks = _remote_name_device_pks(remote_names)
        sorted_remote_ids = sorted(remote_ids)
        # Chunked so a wide page cannot build one unbounded OR chain.
        for offset in range(0, len(sorted_remote_ids), 32):
            # The remote device_id is the device's own identity; an OOB match names a different device.
            id_q = identity_q(
                Device,
                server=server_key,
                identities=sorted_remote_ids[offset : offset + 32],
                roles=(MappingRole.OWN,),
            )
            device_pks.update(Device.objects.filter(id_q).values_list("pk", flat=True))
        catalog_devices = list(Device.objects.filter(pk__in=device_pks).select_related("virtual_chassis"))
        visible_device_ids = set(
            self._viewable_queryset(Device)
            .filter(pk__in=[device.pk for device in catalog_devices])
            .values_list("pk", flat=True)
        )
        devices_by_name = defaultdict(list)
        devices_by_librenms_id = defaultdict(list)
        for device in catalog_devices:
            if device.name:
                devices_by_name[device.name.lower()].append(device)
            device_librenms_id = read_mapping(device).own_id(server_key)
            if device_librenms_id is not None:
                devices_by_librenms_id[device_librenms_id].append(device)
        return devices_by_name, devices_by_librenms_id, visible_device_ids

    def _resolve_remote_devices(self, normal_links, devices_by_name, devices_by_librenms_id, visible_device_ids):
        """Resolve each row's remote device, LibreNMS id first, then exact name, then stripped name."""
        catalog = devices_by_name, devices_by_librenms_id, visible_device_ids
        return {
            id(link): resolve_remote_device(
                link.get("remote_device_id"),
                link.get("remote_device"),
                server_key="",
                catalog=catalog,
            )[0]
            for link in normal_links
        }

    def _resolve_remote_owners(self, normal_links, remote_device_by_link):
        """Resolve which chassis member actually owns each row's remote port."""
        remote_chassis_ids = {
            device.virtual_chassis_id
            for device in remote_device_by_link.values()
            if device is not None and device.virtual_chassis_id is not None
        }
        remote_members_by_chassis = defaultdict(dict)
        if remote_chassis_ids:
            for member in Device.objects.filter(virtual_chassis_id__in=remote_chassis_ids):
                if member.vc_position is not None:
                    remote_members_by_chassis[member.virtual_chassis_id][member.vc_position] = member
        remote_owner_by_link = {}
        for link in normal_links:
            remote_device = remote_device_by_link.get(id(link))
            if remote_device is None:
                remote_owner = None
            elif remote_device.virtual_chassis_id is None:
                remote_owner = remote_device
            else:
                remote_owner = get_virtual_chassis_member(
                    remote_device,
                    link.get("remote_port"),
                    members_by_position=remote_members_by_chassis[remote_device.virtual_chassis_id],
                    return_device_on_failure=False,
                )
            remote_owner_by_link[id(link)] = remote_owner
        return remote_owner_by_link

    def _resolve_local_owners(self, normal_links, obj, server_key):
        """Resolve local owners by stable port binding before a chassis member name."""
        if obj.virtual_chassis_id is None:
            return [obj], {id(link): obj for link in normal_links}
        chassis_members = list(obj.virtual_chassis.members.all())
        members_by_id = {member.pk: member for member in chassis_members}
        members_by_position = {
            member.vc_position: member for member in chassis_members if member.vc_position is not None
        }
        holders = port_holders((link.get("local_port_id") for link in normal_links), server=server_key)
        owner_ids = dict(
            Interface.objects.filter(
                pk__in=[
                    holder[1] for holder in holders.values() if holder is not None and holder[0] == "dcim.interface"
                ]
            ).values_list("pk", "device_id")
        )
        owners = {}
        for link in normal_links:
            port_id = coerce_librenms_id(link.get("local_port_id"))
            if port_id in holders:
                holder = holders[port_id]
                owner_id = owner_ids.get(holder[1]) if holder is not None and holder[0] == "dcim.interface" else None
                owners[id(link)] = members_by_id.get(owner_id)
            else:
                owners[id(link)] = get_virtual_chassis_member(
                    obj, link.get("local_port"), members_by_position=members_by_position
                )
        return chassis_members, owners

    @staticmethod
    def _collect_interface_candidates(normal_links, local_owner_by_link, remote_owner_by_link):
        """Group the port names and LibreNMS ids each owning device must be searched for."""
        manual_ids = {
            manual_id
            for link in normal_links
            if (manual_id := coerce_librenms_id(link.get("manual_remote_id"))) is not None
        }
        candidate_specs = defaultdict(lambda: {"names": set(), "ids": set()})
        for link in normal_links:
            local_owner = local_owner_by_link.get(id(link))
            if local_owner is not None:
                candidate_specs[local_owner.pk]["names"].update(
                    name
                    for name in (link.get("local_port"), link.get("local_port_alt"))
                    if isinstance(name, str) and name
                )
                if (local_id := coerce_librenms_id(link.get("local_port_id"))) is not None:
                    candidate_specs[local_owner.pk]["ids"].add(local_id)
            remote_owner = remote_owner_by_link.get(id(link))
            if remote_owner is not None:
                candidate_specs[remote_owner.pk]["names"].update(_remote_port_name_candidates(link))
                if (remote_id := remote_port_ref(link)) is not None:
                    candidate_specs[remote_owner.pk]["ids"].add(remote_id)
        return manual_ids, candidate_specs

    def _load_interface_catalog(self, candidate_specs, manual_ids, owner_ids, server_key):
        """Prefetch every candidate interface once and build the row lookup and permission sets."""
        candidate_pks = set(manual_ids)
        named_candidate_items = [
            (owner_id, evidence["names"]) for owner_id, evidence in sorted(candidate_specs.items()) if evidence["names"]
        ]
        # Chunked so a wide page cannot build one unbounded OR chain.
        for offset in range(0, len(named_candidate_items), 64):
            candidate_q = Q(pk__isnull=True) & Q(pk__isnull=False)
            for owner_id, names in named_candidate_items[offset : offset + 64]:
                candidate_q |= Q(device_id=owner_id, name__in=names)
            candidate_pks.update(Interface.objects.filter(candidate_q).values_list("pk", flat=True))
        id_candidate_items = [
            (owner_id, port_id)
            for owner_id, evidence in sorted(candidate_specs.items())
            for port_id in sorted(evidence["ids"])
        ]
        for offset in range(0, len(id_candidate_items), 32):
            chunk = id_candidate_items[offset : offset + 32]
            # One regex per JSON path. The catalog keys each match by its own (device, ID) and name, so
            # a match that pairs one owner with the ID of another owner is never read.
            candidate_q = Q(device_id__in={owner_id for owner_id, _port_id in chunk}) & identity_q(
                Interface,
                server=server_key,
                identities={port_id for _owner_id, port_id in chunk},
                roles=(MappingRole.OWN, MappingRole.OOB),
            )
            candidate_pks.update(Interface.objects.filter(candidate_q).values_list("pk", flat=True))
        catalog_interfaces = list(
            Interface.objects.filter(pk__in=candidate_pks)
            .select_related("device", "cable", "parent__cable")
            .prefetch_related(
                "cable__tags",
                "cable__terminations__termination",
                "parent__cable__tags",
                "parent__cable__terminations__termination",
            )
        )
        # A manual pick can name an interface on a device no row resolved an owner for.
        owner_ids.update(interface.device_id for interface in catalog_interfaces if interface.pk in manual_ids)
        interface_ids_by_device = defaultdict(list)
        interfaces_by_name = defaultdict(list)
        interfaces_by_pk = {}
        for interface in catalog_interfaces:
            interfaces_by_pk[interface.pk] = interface
            interfaces_by_name[(interface.device_id, interface.name)].append(interface)
            interface_librenms_id = read_mapping(interface).own_id(server_key)
            if interface_librenms_id is not None:
                interface_ids_by_device[(interface.device_id, interface_librenms_id)].append(interface)
        cable_ids = {interface.cable_id for interface in catalog_interfaces if interface.cable_id is not None}
        return {
            "server_key": server_key,
            "interfaces_by_pk": interfaces_by_pk,
            "interfaces_by_name": interfaces_by_name,
            "interface_ids_by_device": interface_ids_by_device,
            "visible_interface_ids": set(
                self._viewable_queryset(Interface).filter(pk__in=interfaces_by_pk).values_list("pk", flat=True)
            ),
            "visible_owner_ids": set(
                self._viewable_queryset(Device).filter(pk__in=owner_ids).values_list("pk", flat=True)
            ),
            "visible_cable_ids": set(
                self._viewable_queryset(Cable).filter(pk__in=cable_ids).values_list("pk", flat=True)
            ),
        }

    def _load_link_traces(self, normal_links, context, local_owner_by_link, remote_owner_by_link):
        """Trace only the rows whose two ends sit on different cables the request may see."""
        trace_paths = {}
        for link in normal_links:
            local_interface = self._resolve_context_interface(
                context,
                local_owner_by_link.get(id(link)),
                link.get("local_port_id"),
                [link.get("local_port"), link.get("local_port_alt")],
            )
            manual_id = coerce_librenms_id(link.get("manual_remote_id"))
            if manual_id is not None:
                remote_interface = context["interfaces_by_pk"].get(manual_id)
            else:
                remote_interface = self._resolve_context_interface(
                    context,
                    remote_owner_by_link.get(id(link)),
                    remote_port_ref(link),
                    _remote_port_name_candidates(link),
                )
            if self._link_ends_conflict(local_interface, remote_interface, context["visible_cable_ids"]):
                if local_interface.pk not in trace_paths:
                    trace_paths[local_interface.pk] = local_interface.trace()
        return trace_paths

    @staticmethod
    def _link_ends_conflict(local_interface, remote_interface, visible_cable_ids):
        """Return whether both ends are cabled, to different cables, and both are viewable."""
        return (
            local_interface is not None
            and remote_interface is not None
            and not is_channel(local_interface)
            and local_interface.cable_id is not None
            and remote_interface.cable_id is not None
            and local_interface.cable_id != remote_interface.cable_id
            and local_interface.cable_id in visible_cable_ids
            and remote_interface.cable_id in visible_cable_ids
        )

    @staticmethod
    def _resolve_context_interface(context, device, port_id, name_candidates):
        """Resolve one prefetched Interface with the existing stable-ID-first precedence."""
        if context is None or device is None or device.pk not in context["visible_owner_ids"]:
            return None
        normalized_id = coerce_librenms_id(port_id)
        if normalized_id is not None:
            matches = context["interface_ids_by_device"].get((device.pk, normalized_id), [])
            if len(matches) == 1:
                interface = matches[0]
                return interface if interface.pk in context["visible_interface_ids"] else None
            if matches:
                return None
        candidate_names = {name for name in name_candidates if isinstance(name, str) and name}
        matches = {
            interface.pk: interface
            for name in candidate_names
            for interface in context["interfaces_by_name"].get((device.pk, name), ())
        }
        if len(matches) != 1:
            return None
        interface = next(iter(matches.values()))
        if not name_match_may_be_port(interface, server=context["server_key"], port_id=port_id):
            return None
        return interface if interface.pk in context["visible_interface_ids"] else None

    def get_ip_address(self, obj):
        """Get the primary IP address for the object."""
        if obj.primary_ip:
            return str(obj.primary_ip.address.ip)
        return None

    def get_device_by_id_or_name(self, remote_device_id, hostname, server_key=None, queryset=None):
        """Try to find device in NetBox first by librenms_id custom field, then by name."""
        return resolve_remote_device(
            remote_device_id,
            hostname,
            server_key=server_key if server_key is not None else self._render_server_key(),
            queryset=queryset,
        )

    def enrich_local_port(
        self,
        link,
        obj,
        server_key=None,
        sync_device=None,
        serial_ports_by_name=None,
        normal_context=None,
    ):
        """
        Add the local-port URL if the interface/CSP exists in NetBox.

        Returns the resolved ConsoleServerPort for a serial row (so the caller can reuse it for
        the cable-status check without a second fetch by pk), else None.

        Args:
            link (dict): The cable row to enrich.
            obj (Device): The device shown on the cable page.
            server_key (str | None): The LibreNMS server key, or None to resolve it.
            sync_device (Device | None): The pre-resolved LibreNMS sync device.
            serial_ports_by_name (dict[str, ConsoleServerPort] | None): Preloaded serial ports
                keyed by name.
            normal_context (dict | None): Preloaded interface and chassis lookup data.

        Returns:
            ConsoleServerPort | None: The resolved serial port for a serial row, or None.

        """
        # Merged OOB-controller rows are context-only: their local port lives on the
        # CONTROLLER, not the host, so a shared name (or colliding stored librenms_id)
        # must not bind a host interface — that would render a wrong local_port_url and
        # cable state. Sync and the actions column already refuse OOB rows; leave the
        # local end unresolved here too.
        if link.get("_source") == OOB_INVENTORY_SOURCE:
            return None
        if local_port := link.get("local_port"):
            # Serial rows map to ConsoleServerPort, not Interface. Resolve the CSP on the
            # Serial rows belong to the sync owner on a Virtual Chassis page.
            if link.get("_source") == SERIAL_INVENTORY_SOURCE:
                if server_key is None:
                    server_key = self.librenms_api.server_key
                if sync_device is None:
                    sync_device = self._viewable_sync_device(obj, server_key)
                if sync_device is None:
                    return None
                csp = (
                    serial_ports_by_name.get(local_port)
                    if serial_ports_by_name is not None
                    else self._viewable_queryset(ConsoleServerPort).filter(device=sync_device, name=local_port).first()
                )
                if csp:
                    link["local_port_url"] = reverse("dcim:consoleserverport", args=[csp.pk])
                    link["netbox_local_interface_id"] = csp.pk
                    link["netbox_local_device_id"] = csp.device_id
                return csp

            interface = None
            local_port_id = link.get("local_port_id")
            if server_key is None:
                server_key = self._render_server_key()

            # Name fallback is field-agnostic: a NetBox interface may be named from the
            # LibreNMS field the user is not currently displaying. For example, the
            # interface carries the ifDescr value while interface_name_field selects ifName).
            # Match the displayed name plus the alternate field captured at fetch time,
            # mirroring the dual ifName/ifDescr fallback in
            # interface relationship enrichment. The stable librenms_id match below
            # still wins when present; this only widens the fragile name fallback.
            name_candidates = [n for n in (local_port, link.get("local_port_alt")) if n]

            if normal_context is not None:
                chassis_member = normal_context["local_owner_by_link"].get(id(link))
                interface = self._resolve_context_interface(
                    normal_context,
                    chassis_member,
                    local_port_id,
                    name_candidates,
                )
            elif hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                chassis_member = get_virtual_chassis_member(obj, local_port)

                if chassis_member:
                    interface = resolve_device_port(
                        chassis_member, server=server_key, port_id=local_port_id, name_candidates=name_candidates
                    )
            else:
                interface = resolve_device_port(
                    obj, server=server_key, port_id=local_port_id, name_candidates=name_candidates
                )

            if interface:
                if normal_context is None and not self._object_is_viewable(interface):
                    return None
                link["local_port_url"] = reverse("dcim:interface", args=[interface.pk])
                link["netbox_local_interface_id"] = interface.pk
                link["netbox_local_device_id"] = interface.device_id

    def enrich_remote_port(self, link, device, server_key=None, normal_context=None):
        """Add remote port URL if device and interface exist in NetBox."""
        remote_port = link.get("remote_port")
        if isinstance(remote_port, str) and remote_port:
            remote_name_candidates = _remote_port_name_candidates(link)
            netbox_remote_interface = None
            librenms_remote_port_id = remote_port_ref(link)
            if server_key is None:
                server_key = self._render_server_key()

            # Same id-beats-name resolution as the local end — reuse the shared resolver so a fix
            # in one path can't miss the other again (the drift risk _resolve_local_interface was
            # extracted to close). VC-member selection stays here because the remote side leaves the
            # interface unresolved when the member lookup fails.
            if normal_context is not None:
                netbox_remote_interface = self._resolve_context_interface(
                    normal_context,
                    normal_context["remote_owner_by_link"].get(id(link)),
                    librenms_remote_port_id,
                    remote_name_candidates,
                )
            elif hasattr(device, "virtual_chassis") and device.virtual_chassis:
                chassis_member = get_virtual_chassis_member(
                    device,
                    remote_port,
                    return_device_on_failure=False,
                )
                if chassis_member:
                    netbox_remote_interface = resolve_device_port(
                        chassis_member,
                        server=server_key,
                        port_id=librenms_remote_port_id,
                        name_candidates=remote_name_candidates,
                    )
            else:
                netbox_remote_interface = resolve_device_port(
                    device, server=server_key, port_id=librenms_remote_port_id, name_candidates=remote_name_candidates
                )

            if netbox_remote_interface:
                if normal_context is None and (
                    not self._object_is_viewable(netbox_remote_interface)
                    or not self._viewable_queryset(Device).filter(pk=netbox_remote_interface.device_id).exists()
                ):
                    return link
                link["remote_port_url"] = reverse("dcim:interface", args=[netbox_remote_interface.pk])
                link["netbox_remote_interface_id"] = netbox_remote_interface.pk
                link["netbox_remote_device_id"] = netbox_remote_interface.device_id
                link["remote_device_display"] = netbox_remote_interface.device.name
                link["remote_device_url"] = reverse("dcim:device", args=[netbox_remote_interface.device_id])
                link["remote_port_name"] = netbox_remote_interface.name

        # The Remote Port column reads remote_port_name, which only the resolved branch above and
        # the device-not-found branch of process_remote_device set. A row whose DEVICE resolved and
        # whose PORT did not therefore rendered an empty cell, hiding the very port the row is
        # about. Fall back to what LibreNMS advertised, which is what the verify path already does.
        link.setdefault("remote_port_name", remote_port if isinstance(remote_port, str) else "")

        # Return the link even when remote_port is empty (or unresolved): callers assign the
        # result back (link = process_remote_device(...)) and then dereference it, so returning
        # None here would crash enrich_links_data with an AttributeError and take down the whole
        # Cables tab for LLDP/CDP neighbors that advertise a remote device but no remote port.
        return link

    def check_cable_status(self, link, normal_context=None):
        """
        Check cable status against the LibreNMS-desired connection and set the sync affordance.

        States (``cable_status`` / ``can_create_cable``):

        - ``"No Cable"`` / True: neither end cabled; sync creates the cable.
        - ``"Cable Found"`` / untagged-only: the desired connection is already cabled directly.
          When the cable lacks the librenms tag a sync merely adopts it (``tag_only`` in
          :func:`~netbox_librenms_plugin.utils.classify_cable_action`); when already tagged
          there is nothing to do.
        - ``"Connected via Patch Path"`` / False: both ends cabled and the traced path reaches
          the LibreNMS target through patch panels: a remodel is a better model of the same
          link, so no re-sync is offered.
        - ``"Cable Mismatch"`` / True: cabled somewhere that does NOT reach the target; a
          re-sync is offered and the exact current cable must be confirmed before replacement.

        Args:
            link (dict): The cable row to update.
            normal_context (dict | None): Preloaded interface, cable, and trace data.

        Returns:
            dict: The cable row with its status and sync affordance.

        """
        link.pop("_local_end_cabled", None)
        local_interface_id = link.get("netbox_local_interface_id")
        remote_interface_id = link.get("netbox_remote_interface_id")

        # Default state. OOB-controller rows are context-only (shared-LOM detection) and are
        # skipped by SyncCablesView.process_single_interface, so they must never offer a Sync
        # Cable action in ANY state: an OOB row whose shared-name local port resolves to a host
        # interface would otherwise present a dead button (in both the table render and the
        # verify response, which both gate the action on can_create_cable).
        link["can_create_cable"] = False
        actionable = link.get("_source") != OOB_INVENTORY_SOURCE
        if link.get("manual_remote") and not remote_interface_id:
            return link

        if local_interface_id and remote_interface_id:
            if normal_context is not None:
                local_interface = normal_context["interfaces_by_pk"].get(local_interface_id)
                remote_interface = normal_context["interfaces_by_pk"].get(remote_interface_id)
                visible_interface_ids = normal_context["visible_interface_ids"]
                if (
                    local_interface is None
                    or remote_interface is None
                    or local_interface.pk not in visible_interface_ids
                    or remote_interface.pk not in visible_interface_ids
                ):
                    _set_cable_state(link, "Interface Not Available", UNCLASSIFIED)
                    return link
            else:
                try:
                    local_interface = self._viewable_queryset(Interface).get(pk=local_interface_id)
                    remote_interface = self._viewable_queryset(Interface).get(pk=remote_interface_id)
                except Interface.DoesNotExist:
                    _set_cable_state(link, "Interface Not Available", UNCLASSIFIED)
                    return link
            if self._is_breakout_row(link, local_interface, remote_interface, actionable):
                return link
            local_cable = local_interface.cable
            remote_cable = remote_interface.cable
            if normal_context is not None:
                cable_state_hidden = any(
                    cable is not None and cable.pk not in normal_context["visible_cable_ids"]
                    for cable in {local_cable, remote_cable}
                )
            else:
                cable_state_hidden = any(
                    cable is not None and not self._object_is_viewable(cable) for cable in {local_cable, remote_cable}
                )
            if cable_state_hidden:
                _set_cable_state(link, "Cable State Not Available", UNCLASSIFIED)
                return link

            unsupported_cable = next(
                (
                    cable
                    for cable in (local_cable, remote_cable)
                    if cable is not None and not cable_is_point_to_point(cable)
                ),
                None,
            )
            if unsupported_cable is not None:
                _set_cable_state(link, "Multi-termination Cable Not Supported", UNCLASSIFIED)
                link["cable_url"] = reverse("dcim:cable", args=[unsupported_cable.pk])
                link["_multi_termination_unsupported"] = True
                return link

            if local_cable is None and remote_cable is None:
                _set_cable_state(link, "No Cable", NOT_IN_NETBOX)
                link["can_create_cable"] = actionable
            elif local_cable is not None and local_cable == remote_cable:
                # The desired connection is already cabled directly. Offer a sync only while
                # the cable is untagged — it then just adopts (tags) the cable, never recreates.
                adopted = self._cable_has_provenance(local_cable)
                # An untagged cable still needs the sync to adopt it, so it does not match yet.
                _set_cable_state(link, "Cable Found", MATCHING if adopted else MISMATCHED)
                link["cable_url"] = reverse("dcim:cable", args=[local_cable.pk])
                link["can_create_cable"] = actionable and not adopted
            elif local_cable is not None and remote_cable is not None:
                path = (
                    normal_context["trace_paths"].get(local_interface.pk)
                    if normal_context is not None
                    else local_interface.trace()
                )
                if not self._trace_is_viewable(
                    path,
                    (normal_context or {}).get("trace_visibility"),
                ):
                    _set_cable_state(link, "Cable State Not Available", UNCLASSIFIED)
                    return link
                if cable_path_reaches(local_interface, remote_termination=remote_interface, path=path):
                    # A remodeled multi-segment path (through patch panels) that still reaches
                    # the LibreNMS target: the link is correct, just modeled in more detail.
                    _set_cable_state(link, "Connected via Patch Path", MATCHING)
                else:
                    _set_cable_state(link, "Cable Mismatch", MISMATCHED)
                    link["can_create_cable"] = actionable
                link["cable_url"] = reverse("dcim:cable", args=[local_cable.pk])
            else:
                # Cabled somewhere that does not reach the LibreNMS target: offer a re-sync,
                # gated by classify_cable_action and explicit confirmation of the current cable.
                occupying_cable = local_cable or remote_cable
                _set_cable_state(link, "Cable Mismatch", MISMATCHED)
                link["cable_url"] = reverse("dcim:cable", args=[occupying_cable.pk])
                link["can_create_cable"] = actionable
        else:
            _set_cable_state(
                link,
                "Both Interfaces Not Found in Netbox"
                if not (local_interface_id or remote_interface_id)
                else "Local Interface Not Found in Netbox"
                if not local_interface_id
                else "Remote Interface Not Found in Netbox",
                UNCLASSIFIED,
            )
            # LibreNMS gave only one end, so the row can never be synced. Reporting the cable
            # the resolved end already has keeps a cabled port from reading as unconnected.
            self._report_one_sided_cable(link, local_interface_id or remote_interface_id, normal_context)

        return link

    def _report_one_sided_cable(self, link, interface_id, normal_context):
        """
        Report the NetBox cable on the end of a one-sided row that did resolve.

        Scoped throughout: a cable the request cannot view is never linked, and the far end is
        named only when its device is viewable. ``can_create_cable`` is left off, because there
        is still no NetBox remote to cable to.

        Args:
            link (dict): The cable row to update in place.
            interface_id (int | None): The one end that resolved, if any.
            normal_context (dict | None): Preloaded interface, cable, and trace data.

        Returns:
            None

        """
        if link.get("cable_status") not in (
            None,
            "Device Not Found in NetBox",
            "Both Interfaces Not Found in Netbox",
            "Local Interface Not Found in Netbox",
            "Remote Interface Not Found in Netbox",
        ):
            return
        if not interface_id:
            return
        if normal_context is not None:
            interface = normal_context["interfaces_by_pk"].get(interface_id)
            if interface is None or interface.pk not in normal_context["visible_interface_ids"]:
                return
        else:
            interface = self._viewable_queryset(Interface).filter(pk=interface_id).first()
            if interface is None:
                return

        # The gate's view step comes first: a channel's connector state belongs to its parent's cable.
        if is_channel(interface) and (refusal := hidden_breakout_refusal(interface, self._may)) is not None:
            _set_cable_state(link, f"Breakout Not Synced: {refusal.reason}", UNCLASSIFIED)
            return
        cable = interface.cable
        if cable is None or not holds_a_cable(interface):
            return
        if interface.pk == link.get("netbox_local_interface_id"):
            link["_local_end_cabled"] = True
        if normal_context is not None:
            cable_visible = cable.pk in normal_context["visible_cable_ids"]
        else:
            cable_visible = self._object_is_viewable(cable)
        if not cable_visible:
            _set_cable_state(link, "Cable State Not Available", UNCLASSIFIED)
            return

        link["cable_url"] = reverse("dcim:cable", args=[cable.pk])
        peer_device = self._viewable_cable_peer_device(interface, cable)
        _set_cable_state(
            link, f"Cabled to {peer_device.name}" if peer_device is not None else "Cabled in Netbox", UNCLASSIFIED
        )

    def _is_breakout_row(self, link, local_interface, remote_interface, actionable):
        """Return whether an end of the row is a channel; the row then shows the breakout gate's decision."""
        if is_channel(local_interface):
            decision = self._breakout_decision(local_interface, remote_interface)
        elif (decision := decide_channel_far_end(local_interface, remote_interface, self._may)) is None:
            return False
        channel = local_interface if is_channel(local_interface) else remote_interface
        cable = None if channel.parent is None else channel.parent.cable
        if cable is not None and decision.reason not in HIDDEN_REASONS:
            link["cable_url"] = reverse("dcim:cable", args=[cable.pk])
        if decision.action == "matching":
            _set_cable_state(link, "Cable Found", MATCHING)
        elif decision.action == "refuse":
            _set_cable_state(link, f"Breakout Not Synced: {decision.reason}", UNCLASSIFIED)
        else:
            _set_cable_state(link, "No Cable", NOT_IN_NETBOX)
            link["can_create_cable"] = actionable
        return True

    def _breakout_decision(self, channel, remote):
        """Return the breakout gate's decision for a channel row of this request."""
        return decide_breakout_cable(channel, remote, self._provenance_tag(), self._may)

    def _may(self, action, obj):
        """Return whether the request user may do *action* (``view`` or ``change``) on *obj*."""
        # The channels of one parent ask the same questions; the writer keeps its own uncached check.
        memo = self.__dict__.setdefault("_may_memo", {})
        key = (action, type(obj), obj.pk)
        if key not in memo:
            queryset = self._viewable_queryset if action == "view" else self._changeable_queryset
            memo[key] = queryset(type(obj)).filter(pk=obj.pk).exists()
        return memo[key]

    def _viewable_cable_peer_device(self, interface, cable):
        """Return the device at the far end of *cable*, only when the request may view it."""
        if is_channel(interface):
            far = breakout_far_end(interface, interface.parent, cable)
        else:
            if not cable_is_point_to_point(cable):
                return None
            a_terminations = list(cable.a_terminations)
            b_terminations = list(cable.b_terminations)
            if len(a_terminations) != 1 or len(b_terminations) != 1:
                return None
            near, far = a_terminations[0], b_terminations[0]
            if near != interface:
                near, far = far, near
            if near != interface:
                return None
        peer_device_id = getattr(far, "device_id", None)
        if peer_device_id is None:
            return None
        # One management switch is the peer of many ports: read its device once per request, by id.
        memo = self.__dict__.setdefault("_peer_device_visibility", {})
        if peer_device_id not in memo:
            memo[peer_device_id] = self._viewable_queryset(Device).filter(pk=peer_device_id).first()
        return memo[peer_device_id]

    def check_serial_cable_status(self, link, csp=None, remote_context=None):
        """
        Check cable status for a serial ConsoleServerPort row.

        ``csp`` may be the ConsoleServerPort already loaded by ``enrich_local_port`` for this
        row; when it matches the resolved id it's reused instead of re-fetching by pk.

        Args:
            link (dict): The serial cable row to update.
            csp (ConsoleServerPort | None): The preloaded local console server port.
            remote_context (dict | None): Preloaded cable visibility data.

        Returns:
            dict: The serial cable row with its status and sync affordance.

        """
        csp_id = link.get("netbox_local_interface_id")
        link["can_create_cable"] = False
        if not csp_id:
            _set_cable_state(link, "Console Server Port Not Found in NetBox", UNCLASSIFIED)
            return link
        if csp is None or csp.pk != csp_id:
            try:
                csp = self._viewable_queryset(ConsoleServerPort).get(pk=csp_id)
            except ConsoleServerPort.DoesNotExist:
                _set_cable_state(link, "Console Server Port Not Found in NetBox", UNCLASSIFIED)
                return link
        if csp.cable:
            visible_cable_ids = (remote_context or {}).get("visible_cable_ids")
            cable_is_visible = (
                csp.cable_id in visible_cable_ids
                if visible_cable_ids is not None
                else self._object_is_viewable(csp.cable)
            )
            if not cable_is_visible:
                _set_cable_state(link, "Cable State Not Available", UNCLASSIFIED)
                link["_serial_cable_hidden"] = True
                return link
            # The local end alone does not show the desired connection: enrich_serial_remote decides it.
            _set_cable_state(link, "Cable Found", UNCLASSIFIED)
            link["cable_url"] = reverse("dcim:cable", args=[csp.cable.pk])
            if not cable_is_point_to_point(csp.cable):
                _set_cable_state(link, "Multi-termination Cable Not Supported", UNCLASSIFIED)
                link["_multi_termination_unsupported"] = True
        else:
            _set_cable_state(link, "No Cable", UNCLASSIFIED)
        return link

    def enrich_serial_remote(self, link, claimed_cp_ids=None, csp=None, remote_context=None):
        """
        Resolve the remote ConsolePort for a serial row using the Avocent label.

        Called whenever the local ConsoleServerPort is found. Matches the label to a NetBox
        device by name, then resolves against the CSP's cable state:

        - CSP un-cabled: pick the first uncabled ConsolePort on the label device and set
          ``can_create_cable = True`` so the sync action can create the cable.
        - CSP cabled directly to a ConsolePort ON the label device: the desired connection
          already exists — resolve the remote to THAT port (never a fresh free one) and offer
          a sync only while the cable is untagged (it then just adopts/tags the cable).
        - CSP cabled and the traced path reaches the label device through patch panels: a
          remodel of the same link — mark ``"Connected via Patch Path"``, no action.
        - CSP cabled somewhere that does not reach the label device: if the cable carries the
          librenms tag it is TRUSTED over the label (the label is only a hint; a tagged cable
          was placed deliberately, possibly via a manual pick — flipping it to a mismatch
          would offer a re-point of a correct cable). Otherwise ``"Cable Mismatch"``: pick a free
          ConsolePort as the re-point target. The sync requires exact cable confirmation before
          replacement.

        A manually picked remote (``manual_remote_id``, set by CableRemotePickerView) wins over
        the label hint entirely — the label mismatching is exactly why the user picked by hand.
        The manual states mirror the label states but match at PORT level, and the tag-trust
        rule does not apply (the pick carries the user's explicit intent to re-point).

        Args:
            link (dict): The serial cable-sync row, mutated in place with the resolved
                remote device/port (or a ``cable_status`` note on failure).
            claimed_cp_ids (set | None): ConsolePort pks already picked by an earlier serial
                row in this response. The pick excludes them (and adds its own), so two serial
                rows resolving to the same remote device don't both target the same uncabled
                ConsolePort — which would create one cable and then misreport the second as a
                "duplicate" while a genuinely free port stays uncabled.
            csp: The ConsoleServerPort already loaded for this row (reused when it matches the
                resolved id, saving a re-fetch by pk).
            remote_context (dict | None): Preloaded visibility and trace data for remote objects.

        Returns:
            None

        """
        csp = self._resolve_serial_local_csp(link, csp)
        if csp is None:
            return

        if "manual_remote_id" in link:
            self._resolve_manual_serial_remote(link, csp, claimed_cp_ids, remote_context)
            return

        # The default label on an unconfigured sensor identifies only the local appliance
        # port. It is not evidence for a remote NetBox Device with the same name.
        if link.get("is_configured") is False:
            self._display_serial_far_end_if_cabled(link, csp, remote_context)
            return

        label = link.get("remote_device")
        if not label:
            self._display_serial_far_end_if_cabled(link, csp, remote_context)
            return

        device = self._lookup_serial_label_device(label, remote_context)
        if device is None:
            # A dead label must not leave a cabled row's remote columns empty.
            self._display_serial_far_end_if_cabled(link, csp, remote_context)
            return

        link["remote_device_url"] = reverse("dcim:device", args=[device.pk])
        link["netbox_remote_device_id"] = device.pk

        # A cabled row resolves against its cable state; only a mismatch falls through to the
        # free-port pick below (the re-sync re-points at the label device).
        if csp.cable is not None and self._resolve_cabled_serial_row(
            link,
            csp,
            device,
            remote_context=remote_context,
        ):
            return

        self._pick_free_console_port(link, csp, device, claimed_cp_ids, remote_context)

    def _resolve_serial_local_csp(self, link, csp):
        """Return the row's local ConsoleServerPort, or None when the row cannot be resolved."""
        csp_id = link.get("netbox_local_interface_id")
        # No resolved local end -> nothing to resolve a remote against (call sites gate on the
        # id, so this is a defensive guard, not a reachable state on the render path).
        if not csp_id:
            return None
        if csp is None or csp.pk != csp_id:
            try:
                csp = self._viewable_queryset(ConsoleServerPort).get(pk=csp_id)
            except ConsoleServerPort.DoesNotExist:
                return None
        if link.get("_serial_cable_hidden") or link.get("_multi_termination_unsupported"):
            return None
        return csp

    def _display_serial_far_end_if_cabled(self, link, csp, remote_context):
        """Show where an already-cabled row really lands, with no sync target attached."""
        if csp.cable is None:
            return
        self._display_serial_cable_far_end(
            link,
            csp,
            path=(remote_context or {}).get("trace_paths", {}).get(csp.pk),
            visible_ids=(remote_context or {}).get("trace_visibility"),
        )

    def _lookup_serial_label_device(self, label, remote_context):
        """Resolve the Avocent label to one viewable Device, or None when it names nothing."""
        if remote_context is not None:
            return remote_context["devices_by_label"].get(label)
        device, found, _error = self.get_device_by_id_or_name(
            None,
            label,
            queryset=self._viewable_queryset(Device),
        )
        return device if found else None

    def _resolve_manual_serial_remote(self, link, csp, claimed_cp_ids, remote_context):
        """
        Resolve a hand-picked remote at port level, or fail the row closed.

        A manual pick carries the user's explicit intent, so a pick that is no longer available
        must NOT fall back to the label: that would sync a different endpoint than was chosen.
        """
        manual_pk = coerce_librenms_id(link.get("manual_remote_id"))
        manual_cp = (remote_context or {}).get("manual_ports", {}).get(manual_pk)
        if remote_context is None:
            manual_cp = (
                self._viewable_queryset(ConsolePort)
                .filter(pk=manual_pk, device__in=self._viewable_queryset(Device))
                .select_related("device")
                .first()
            )
        if manual_cp is None:
            _set_cable_state(link, "Selected remote port is no longer available", UNCLASSIFIED)
            link.update({"manual_remote": True, "can_create_cable": False})
            return
        manual_is_actionable = remote_context is None or (
            csp.pk in remote_context["changeable_console_server_port_ids"]
            and manual_cp.pk in remote_context["changeable_console_port_ids"]
        )
        # Reserve the pick in the shared dedup set so a sibling auto-matched row cannot
        # target the manually picked endpoint in the same batch.
        if claimed_cp_ids is not None and manual_is_actionable:
            claimed_cp_ids.add(manual_cp.pk)
        self._apply_serial_remote_target(
            link,
            csp,
            manual_cp,
            manual=True,
            remote_context=remote_context,
        )
        if not manual_is_actionable:
            link["can_create_cable"] = False

    def _pick_free_console_port(self, link, csp, device, claimed_cp_ids, remote_context):
        """Target the first free ConsolePort on the label device, skipping sibling rows' claims."""
        if claimed_cp_ids is None:
            claimed_cp_ids = set()
        # Order explicitly so the picked port is deterministic (the label is only a hint and
        # the user confirms before sync) and stable regardless of the model's default ordering;
        # exclude ports already claimed by a sibling serial row this response.
        if remote_context is not None:
            local_is_actionable = csp.pk in remote_context["changeable_console_server_port_ids"]
            uncabled_cp = next(
                (
                    port
                    for port in remote_context["ports_by_device"].get(device.pk, ())
                    if port.cable_id is None
                    and port.pk not in claimed_cp_ids
                    and (not local_is_actionable or port.pk in remote_context["changeable_console_port_ids"])
                ),
                None,
            )
        else:
            uncabled_cp = (
                self._viewable_queryset(ConsolePort)
                .filter(device=device, cable__isnull=True)
                .exclude(pk__in=claimed_cp_ids)
                .order_by("name")
                .first()
            )
        if not uncabled_cp:
            _set_cable_state(link, "Console Port Not Found in NetBox", UNCLASSIFIED)
            return
        can_create = remote_context is None or (
            csp.pk in remote_context["changeable_console_server_port_ids"]
            and uncabled_cp.pk in remote_context["changeable_console_port_ids"]
        )
        if can_create:
            claimed_cp_ids.add(uncabled_cp.pk)
        link["netbox_remote_interface_id"] = uncabled_cp.pk
        link["remote_port_name"] = uncabled_cp.name
        link["remote_port_url"] = reverse("dcim:consoleport", args=[uncabled_cp.pk])
        link["can_create_cable"] = can_create
        # A cabled local end reaches here only after _resolve_cabled_serial_row found a mismatch.
        if csp.cable is not None:
            _set_cable_state(link, "Cable Mismatch", MISMATCHED)
        else:
            _set_cable_state(link, "No Cable", NOT_IN_NETBOX)

    def _resolve_cabled_serial_row(self, link, csp, device, remote_context=None) -> bool:
        """
        Resolve a cabled serial row against the label-matched *device*.

        Adopted match (cable lands on a ConsolePort of the label device), remodeled path
        (traced path reaches the device through patch panels), and trusted tag (a plugin-tagged
        cable is deliberate, so a wrong-name label must not offer a re-point) all fully
        resolve the row. A mismatch does not: the caller falls through to the free-port pick
        that re-points at the label device.

        Args:
            link (dict): The serial cable-sync row, mutated in place.
            csp: The row's resolved (and cabled) local ConsoleServerPort.
            device: The label-matched NetBox device.
            remote_context (dict | None): Preloaded visibility and trace data for remote objects.

        Returns:
            bool: True when the row is fully resolved; False to fall through (mismatch).

        """
        visible_console_port_ids = (remote_context or {}).get("visible_console_port_ids")
        far_cp = next(
            (
                termination
                for termination in cable_far_terminations(csp.cable, csp)
                if isinstance(termination, ConsolePort)
                and termination.device_id == device.pk
                and (
                    termination.pk in visible_console_port_ids
                    if visible_console_port_ids is not None
                    else self._object_is_viewable(termination)
                )
            ),
            None,
        )
        if far_cp is not None:
            link["netbox_remote_interface_id"] = far_cp.pk
            link["remote_port_name"] = far_cp.name
            link["remote_port_url"] = reverse("dcim:consoleport", args=[far_cp.pk])
            adopted = self._cable_has_provenance(csp.cable)
            # An untagged cable still needs the sync to adopt it, so it does not match yet.
            _set_cable_state(link, "Cable Found", MATCHING if adopted else MISMATCHED)
            link["can_create_cable"] = not adopted
            return True
        # Trace once and reuse it for both the reach check and the far-end display — trace()
        # walks the full cable path in the DB, so the reach+display branches would otherwise
        # pay for it twice per cabled row.
        if remote_context is not None and csp.pk in remote_context["trace_paths"]:
            path = remote_context["trace_paths"][csp.pk]
        else:
            path = csp.trace()
        trace_visibility = (remote_context or {}).get("trace_visibility")
        if not self._trace_is_viewable(path, trace_visibility):
            _set_cable_state(link, "Cable State Not Available", UNCLASSIFIED)
            link["can_create_cable"] = False
            link["_serial_cable_hidden"] = True
            return True
        if cable_path_reaches(
            csp,
            remote_device=device,
            path=path,
            remote_termination_type=ConsolePort,
        ):
            # Show the END of the traced path (the real console), not the panel port the
            # first segment lands on.
            _set_cable_state(link, "Connected via Patch Path", MATCHING)
            self._display_serial_cable_far_end(
                link,
                csp,
                path=path,
                visible_ids=trace_visibility,
            )
            return True
        if self._cable_has_provenance(csp.cable):
            # Trust rule: a plugin-tagged cable was placed deliberately; the wrong-name label
            # must not flip it to a mismatch offering a re-point. Display where it
            # really goes (overriding the label-device link the caller set).
            # The trace does not reach the label device, and the tag does not prove another endpoint.
            _set_cable_state(link, "Cable Found", UNCLASSIFIED)
            self._display_serial_cable_far_end(
                link,
                csp,
                path=path,
                visible_ids=trace_visibility,
            )
            return True
        _set_cable_state(link, "Cable Mismatch", MISMATCHED)
        return False

    def _display_serial_cable_far_end(self, link, csp, path=None, visible_ids=None):
        """
        Fill *link*'s remote display fields from where the CSP's cable path actually ends.

        Used when a cabled serial row has no resolvable sync target (dead label, trusted tag,
        remodeled path): the row must still show — and link — the real far-end device and
        port instead of dead columns. Follows patch-panel pass-throughs, so a remodeled path
        displays the end console rather than the panel port the first segment lands on.
        Display only: ``netbox_remote_interface_id`` is NOT set (there is nothing to sync at).

        Args:
            link (dict): The serial cable-sync row, mutated in place.
            csp: The row's resolved (and cabled) local ConsoleServerPort.
            path: An already-computed ``csp.trace()`` result to reuse (avoids re-tracing).
            visible_ids (dict | None): Visible object IDs grouped by model.

        """
        if path is None:
            path = csp.trace()
        if path and not self._trace_is_viewable(path, visible_ids):
            _set_cable_state(link, "Cable State Not Available", UNCLASSIFIED)
            link["can_create_cable"] = False
            link["_serial_cable_hidden"] = True
            return
        far = (path[-1][2] if path else None) or cable_far_terminations(csp.cable, csp)
        termination = far[0] if far else None
        termination_is_viewable = termination is not None and (
            termination.pk in visible_ids.get(type(termination), set())
            if visible_ids is not None
            else self._object_is_viewable(termination)
        )
        if not termination_is_viewable:
            return
        device = getattr(termination, "device", None)
        device_is_viewable = device is not None and (
            device.pk in visible_ids.get(type(device), set())
            if visible_ids is not None
            else self._object_is_viewable(device)
        )
        if not device_is_viewable:
            return
        # DISPLAY-ONLY key: never overwrite the raw ``remote_device`` label — it survives
        # the cache strip, so a leaked far-end name would resolve as a "label" on the next
        # cached re-render and flip the row's status (fresh vs cached renders disagreeing).
        link["remote_device_display"] = device.name
        link["remote_device_url"] = reverse("dcim:device", args=[device.pk])
        link["netbox_remote_device_id"] = device.pk
        link["remote_port_name"] = getattr(termination, "name", str(termination))
        if hasattr(termination, "get_absolute_url"):
            link["remote_port_url"] = termination.get_absolute_url()

    def _apply_serial_remote_target(self, link, csp, target_cp, manual=False, remote_context=None):
        """
        Resolve *link*'s remote to *target_cp* and derive status/affordance from the cable state.

        Port-level counterpart of the label path in :meth:`enrich_serial_remote`, used for a
        manually picked remote: un-cabled → sync creates; cabled directly to the target →
        "Cable Found" (adopt offered while untagged); traced path reaches the target →
        "Connected via Patch Path"; cabled elsewhere → "Cable Mismatch" re-pointing at the
        target (the classify gate still protects non-owned cables at sync time).

        Args:
            link (dict): The serial cable-sync row, mutated in place.
            csp: The row's resolved local ConsoleServerPort.
            target_cp: The ConsolePort the remote should resolve to.
            manual (bool): Mark the row as manually picked (rendered as a hint in the table).
            remote_context (dict | None): Preloaded visibility and trace data for remote objects.

        """
        link["netbox_remote_device_id"] = target_cp.device_id
        # Show the picked device's real name (display-only key: the raw ``remote_device`` label
        # survives the cache strip and must stay pristine for re-enrichment) — the LibreNMS
        # label mismatching is exactly why the user picked by hand.
        link["remote_device_display"] = target_cp.device.name
        link["remote_device_url"] = reverse("dcim:device", args=[target_cp.device_id])
        link["netbox_remote_interface_id"] = target_cp.pk
        link["remote_port_name"] = target_cp.name
        link["remote_port_url"] = reverse("dcim:consoleport", args=[target_cp.pk])
        if manual:
            link["manual_remote"] = True

        cable = csp.cable
        target_cable = target_cp.cable
        visible_cable_ids = (remote_context or {}).get("visible_cable_ids")
        cable_state_hidden = any(
            current is not None
            and (
                current.pk not in visible_cable_ids
                if visible_cable_ids is not None
                else not self._object_is_viewable(current)
            )
            for current in (cable, target_cable)
        )
        if cable_state_hidden:
            _set_cable_state(link, "Cable State Not Available", UNCLASSIFIED)
            link["can_create_cable"] = False
            return
        unsupported_cable = next(
            (
                current
                for current in (cable, target_cable)
                if current is not None and not cable_is_point_to_point(current)
            ),
            None,
        )
        if unsupported_cable is not None:
            _set_cable_state(link, "Multi-termination Cable Not Supported", UNCLASSIFIED)
            link["cable_url"] = reverse("dcim:cable", args=[unsupported_cable.pk])
            link["can_create_cable"] = False
            link["_multi_termination_unsupported"] = True
            link.pop("picker_url", None)
            return
        if cable is None and target_cable is None:
            _set_cable_state(link, "No Cable", NOT_IN_NETBOX)
            link["can_create_cable"] = True
            return
        if cable is not None and any(termination == target_cp for termination in cable_far_terminations(cable, csp)):
            # The desired connection already exists — offer adopt while untagged.
            adopted = self._cable_has_provenance(cable)
            _set_cable_state(link, "Cable Found", MATCHING if adopted else MISMATCHED)
            link["can_create_cable"] = not adopted
            return
        if cable is not None:
            if remote_context is not None and csp.pk in remote_context["trace_paths"]:
                path = remote_context["trace_paths"][csp.pk]
            else:
                path = csp.trace()
            if not self._trace_is_viewable(path, (remote_context or {}).get("trace_visibility")):
                _set_cable_state(link, "Cable State Not Available", UNCLASSIFIED)
                link["can_create_cable"] = False
                return
            if cable_path_reaches(csp, remote_termination=target_cp, path=path):
                _set_cable_state(link, "Connected via Patch Path", MATCHING)
                return
        _set_cable_state(link, "Cable Mismatch", MISMATCHED)
        link["can_create_cable"] = True

    def _set_remote_picker_affordance(self, link, obj, server_key):
        """
        Attach a ``picker_url`` to every row where manually picking the remote end is possible.

        Remote matching is name-based (serial label / LLDP port name) and often fails, so any
        row with a resolved local end gets the pick-remote action — including satisfied rows
        (tagged "Cable Found", "Connected via Patch Path"), where the pick RE-POINTS the
        existing cable: a manual re-point always goes through the force-confirm modal at sync
        time, so offering it here is safe. Only context-only OOB rows (never syncable) and
        rows without a local end are excluded.

        Args:
            link (dict): The enriched cable row, mutated in place.
            obj: The page device (URL scope for the picker endpoint).
            server_key: The active LibreNMS server key, carried in the picker URL.

        """
        if (
            not self.has_write_permission()
            or link.get("_source") == OOB_INVENTORY_SOURCE
            or link.get("_multi_termination_unsupported")
            or not link.get("netbox_local_interface_id")
        ):
            return
        url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[obj.pk])
        query = f"row_id={quote_plus(str(link.get('row_id', '')))}"
        if server_key:
            query += f"&server_key={quote_plus(server_key)}"
        link["picker_url"] = f"{url}?{query}"

    def _set_remote_create_affordance(self, link, obj, server_key):
        """
        Attach a ``remote_create_url`` to a row whose neighbour is modelled but its port is not.

        Such a row can never be synced: ``check_cable_status`` reports "Remote Interface Not Found
        in Netbox" and ``SyncCablesView`` refuses it. The action offers to create the far end and
        the cable together. It is the ONE rule the button and the view both read, so a row that
        renders the button is exactly a row the endpoint will act on.

        A row is offered the action only when the local end resolved, the remote DEVICE resolved,
        the remote INTERFACE did not, and LibreNMS holds a port record to name and type the new
        interface from. Without a modelled neighbour there is nothing to attach an interface to,
        so the action is absent rather than failing.

        Args:
            link (dict): The enriched cable row, mutated in place.
            obj: The page device (URL scope for the endpoint).
            server_key: The active LibreNMS server key, carried in the URL.

        """
        link.pop("remote_create_url", None)
        if (
            not self.has_write_permission()
            or link.get("rule_block")
            or link.get("_source") == OOB_INVENTORY_SOURCE
            or link.get("manual_remote")
            or link.get("_local_end_cabled")
            or link.get("_multi_termination_unsupported")
            or not link.get("netbox_local_interface_id")
            or not link.get("netbox_remote_device_id")
            or not link.get("remote_port_owner_id")
            or link.get("netbox_remote_interface_id")
        ):
            return
        # The port record is what names and types the interface; a row without one would create
        # a bare "other" interface from a neighbour-advertised string, which is a guess.
        port_key = coerce_librenms_id(link.get("remote_port_key"))
        if port_key is None:
            return
        # A port another interface holds (another device, or a VM) would get a second owner.
        try:
            if find_port_owner(port_key, server=server_key) is not None:
                return
        except AmbiguousLibreNMSIdError:
            return
        local = self._viewable_queryset(Interface).filter(pk=link["netbox_local_interface_id"]).first()
        if is_channel(local):
            decision = self._breakout_decision(local, None)
            if decision.action not in ("create", "extend"):
                _set_cable_state(link, f"Breakout Not Synced: {decision.reason}", UNCLASSIFIED)
                return
        url = reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[obj.pk])
        query = f"row_id={quote_plus(str(link.get('row_id', '')))}"
        query += f"&expected_local_id={link['netbox_local_interface_id']}"
        if server_key:
            query += f"&server_key={quote_plus(server_key)}"
        link["remote_create_url"] = f"{url}?{query}"

    def _remote_picker_action_html(self, link, obj, server_key):
        """Render the picker action shared by table-verify response paths."""
        self._set_remote_picker_affordance(link, obj, server_key)
        if not (picker_url := link.get("picker_url")):
            return ""
        return f"""
            <button type="button" class="btn btn-sm btn-outline-secondary" title="Pick remote end"
                    aria-label="Pick remote end"
                    data-cable-picker-url="{escape(picker_url)}">
                <i class="mdi mdi-connection"></i>
            </button>
        """

    def _remote_create_action_html(self, link, obj, server_key):
        """Render the create action when the verified row still has no remote interface."""
        self._set_remote_create_affordance(link, obj, server_key)
        if not (create_url := link.get("remote_create_url")):
            return ""
        return f"""
            <button type="button" class="btn btn-sm btn-outline-primary"
                    title="Create the remote interface and the cable"
                    aria-label="Create the remote interface and the cable"
                    data-cable-picker-url="{escape(create_url)}">
                <i class="mdi mdi-plus-network"></i>
            </button>
        """

    def _build_serial_remote_context(self, links, serial_ports):
        """Bulk-load serial label targets and free ports for one table render."""
        labels = _serial_remote_labels(links)
        candidate_names = {candidate for label in labels for candidate in _remote_name_candidates(label)}
        catalog_devices = _remote_name_devices(candidate_names)
        visible_device_ids = set(
            self._viewable_queryset(Device)
            .filter(pk__in=[device.pk for device in catalog_devices])
            .values_list("pk", flat=True)
        )
        devices_by_name = defaultdict(list)
        for device in catalog_devices:
            devices_by_name[device.name.lower()].append(device)

        devices_by_label = {}
        for label in labels:
            full_name, short_name = _remote_name_candidates(label)
            exact = devices_by_name[full_name.lower()]
            simple = devices_by_name[short_name.lower()]
            matches = exact if exact else simple
            devices_by_label[label] = matches[0] if len(matches) == 1 and matches[0].pk in visible_device_ids else None

        manual_ids = {
            normalized
            for link in links
            if link.get("_source") == SERIAL_INVENTORY_SOURCE
            if (normalized := coerce_librenms_id(link.get("manual_remote_id"))) is not None
        }
        target_device_ids = {device.pk for device in devices_by_label.values() if device is not None}
        console_ports = list(
            self._viewable_queryset(ConsolePort)
            .filter(Q(device_id__in=target_device_ids) | Q(pk__in=manual_ids))
            .filter(device__in=self._viewable_queryset(Device))
            .select_related("device", "cable")
            .order_by("device_id", "name", "pk")
        )
        ports_by_device = defaultdict(list)
        manual_ports = {}
        for port in console_ports:
            ports_by_device[port.device_id].append(port)
            if port.pk in manual_ids:
                manual_ports[port.pk] = port

        # Load each cable ONCE with its tags and terminations, then hand the same instance to both
        # ends. A serial row's local ConsoleServerPort and its remote ConsolePort share a cable, so
        # prefetching per queryset walked the same tag/termination sets twice per render.
        cable_ids = {port.cable_id for port in [*serial_ports, *console_ports] if port.cable_id is not None}
        visible_cables = {
            cable.pk: cable
            for cable in self._viewable_queryset(Cable)
            .filter(pk__in=cable_ids)
            .prefetch_related("tags", "terminations__termination")
        }
        visible_cable_ids = set(visible_cables)
        for port in (*serial_ports, *console_ports):
            if (shared_cable := visible_cables.get(port.cable_id)) is not None:
                port.cable = shared_cable
        changeable_console_port_ids = set(
            self._changeable_queryset(ConsolePort)
            .filter(pk__in=[port.pk for port in console_ports])
            .values_list("pk", flat=True)
        )
        changeable_console_server_port_ids = set(
            self._changeable_queryset(ConsoleServerPort)
            .filter(pk__in=[port.pk for port in serial_ports])
            .values_list("pk", flat=True)
        )
        links_by_local_port = defaultdict(list)
        for link in links:
            if link.get("_source") == SERIAL_INVENTORY_SOURCE:
                links_by_local_port[link.get("local_port")].append(link)
        trace_paths = {}
        for port in serial_ports:
            if port.cable_id is None or port.cable_id not in visible_cable_ids:
                continue
            direct_far = cable_far_terminations(port.cable, port)
            needs_path = False
            for link in links_by_local_port.get(port.name, ()):
                manual_id = coerce_librenms_id(link.get("manual_remote_id"))
                if manual_id is not None:
                    manual_port = manual_ports.get(manual_id)
                    needs_path = manual_port is not None and manual_port not in direct_far
                else:
                    label_device = devices_by_label.get(link.get("remote_device"))
                    needs_path = (
                        link.get("is_configured") is False
                        or label_device is None
                        or not any(
                            isinstance(far, ConsolePort) and far.device_id == label_device.pk for far in direct_far
                        )
                    )
                if needs_path:
                    break
            if not needs_path:
                continue
            trace_paths[port.pk] = (
                [([port], [port.cable], direct_far)]
                if any(isinstance(far, ConsolePort) for far in direct_far)
                else port.trace()
            )
        return {
            "devices_by_label": devices_by_label,
            "ports_by_device": ports_by_device,
            "manual_ports": manual_ports,
            "visible_console_port_ids": {port.pk for port in console_ports},
            "visible_cable_ids": visible_cable_ids,
            "changeable_console_port_ids": changeable_console_port_ids,
            "changeable_console_server_port_ids": changeable_console_server_port_ids,
            "trace_paths": trace_paths,
            "trace_visibility": self._build_trace_visibility(trace_paths.values()),
        }

    def process_remote_device(self, link, remote_hostname, remote_device_id, server_key=None, normal_context=None):
        """Process remote device data and add remote device URL if device exists in NetBox."""
        if normal_context is not None:
            device = normal_context["remote_device_by_link"].get(id(link))
            found = device is not None
            error_message = None
        else:
            device, found, error_message = self.get_device_by_id_or_name(
                remote_device_id,
                remote_hostname,
                server_key=server_key,
                queryset=self._viewable_queryset(Device),
            )
        if found:
            link.update(
                {
                    "remote_device_url": reverse("dcim:device", args=[device.pk]),
                    "netbox_remote_device_id": device.pk,
                }
            )
            return self.enrich_remote_port(
                link,
                device,
                server_key=server_key,
                normal_context=normal_context,
            )

        _set_cable_state(link, error_message if error_message else "Device Not Found in NetBox", UNCLASSIFIED)
        link.update({"remote_port_name": link["remote_port"], "can_create_cable": False})
        return link

    def _apply_manual_interface_target(self, link, normal_context=None):
        """Resolve one request-scoped manual Interface pick into the shared row shape."""
        if "manual_remote_id" not in link:
            return False
        manual_pk = coerce_librenms_id(link.get("manual_remote_id"))
        if normal_context is not None:
            manual_iface = normal_context["interfaces_by_pk"].get(manual_pk)
            if manual_iface is not None and (
                manual_iface.pk not in normal_context["visible_interface_ids"]
                or manual_iface.device_id not in normal_context["visible_owner_ids"]
            ):
                manual_iface = None
        else:
            manual_iface = (
                self._viewable_queryset(Interface)
                .filter(pk=manual_pk, device__in=self._viewable_queryset(Device))
                .select_related("device")
                .first()
            )
        if manual_iface is None:
            _set_cable_state(link, "Selected remote port is no longer available", UNCLASSIFIED)
            link.update({"manual_remote": True, "can_create_cable": False})
            return True
        link.update(
            {
                "netbox_remote_device_id": manual_iface.device_id,
                "remote_device_display": manual_iface.device.name,
                "remote_device_url": reverse("dcim:device", args=[manual_iface.device_id]),
                "netbox_remote_interface_id": manual_iface.pk,
                "remote_port_name": manual_iface.name,
                "remote_port_url": reverse("dcim:interface", args=[manual_iface.pk]),
                "manual_remote": True,
            }
        )
        return True

    def enrich_links_data(self, links_data, obj, server_key=None, sync_device=None):
        """Enrich links data with local and remote port URLs and cable status."""
        if server_key is None:
            # Use the degrading resolver (mirrors the other render sites) so a broken/missing
            # default server yields None instead of raising ValueError and 500ing a cached render.
            server_key = self._render_server_key()
        # Resolve the serial sync device once for the whole loop (loop-invariant) instead of
        # per serial row. Reuse the device the caller (_prepare_context) already resolved to
        # avoid a second get_librenms_sync_device() VC-members query per request; falls back to
        # resolving here when called without one.
        serial_sync_device = sync_device or self._viewable_sync_device(obj, server_key)
        serial_links_present = any(link.get("_source") == SERIAL_INVENTORY_SOURCE for link in links_data)
        serial_ports = (
            list(self._viewable_queryset(ConsoleServerPort).filter(device=serial_sync_device).select_related("cable"))
            if serial_links_present and serial_sync_device is not None
            else []
        )
        serial_ports_by_name = {port.name: port for port in serial_ports}
        serial_remote_context = (
            self._build_serial_remote_context(links_data, serial_ports) if serial_links_present else None
        )
        normal_context = self._build_normal_link_context(links_data, obj, server_key)
        # ConsolePorts already auto-picked by an earlier serial row this response, so two rows
        # resolving to the same remote device don't collide on one port (see enrich_serial_remote).
        # Reserve later manual picks before row-order auto matching, but only when that row still
        # has a current, viewable local CSP. A stale pick for a deleted/hidden CSP must not consume
        # a valid row's only free remote port.
        manual_ports = (serial_remote_context or {}).get("manual_ports", {})
        changeable_console_port_ids = (serial_remote_context or {}).get("changeable_console_port_ids", set())
        changeable_console_server_port_ids = (serial_remote_context or {}).get(
            "changeable_console_server_port_ids", set()
        )
        claimed_remote_cp_ids = {
            manual_id
            for link in links_data
            if link.get("_source") == SERIAL_INVENTORY_SOURCE
            and (local_csp := serial_ports_by_name.get(link.get("local_port"))) is not None
            and local_csp.pk in changeable_console_server_port_ids
            if (manual_id := coerce_librenms_id(link.get("manual_remote_id"))) in manual_ports
            and manual_id in changeable_console_port_ids
        }
        for link in links_data:
            csp = self.enrich_local_port(
                link,
                obj,
                server_key=server_key,
                sync_device=serial_sync_device,
                serial_ports_by_name=serial_ports_by_name,
                normal_context=normal_context,
            )

            # Serial rows: check CSP cable status, then try to resolve remote ConsolePort.
            if link.get("_source") == SERIAL_INVENTORY_SOURCE:
                # Serial rows already carry the CSP-owning sync device_id from
                # map_sensors_to_serial_links; don't overwrite it with the viewed obj.id —
                # on a VC-member page that would default the per-row member dropdown (and the
                # sync target) to the wrong member. Reuse the CSP enrich_local_port just loaded.
                self.check_serial_cable_status(link, csp=csp, remote_context=serial_remote_context)
                # If CSP is found, resolve the remote against the label — for cabled rows too,
                # so the row can offer adopt (matched untagged) or re-sync (mismatch) actions.
                if link.get("netbox_local_interface_id"):
                    self.enrich_serial_remote(
                        link,
                        claimed_cp_ids=claimed_remote_cp_ids,
                        csp=csp,
                        remote_context=serial_remote_context,
                    )
                self._set_remote_picker_affordance(link, obj, server_key)
                continue

            link["device_id"] = obj.id

            # A manually picked remote Interface wins over LLDP name matching entirely (the
            # names mismatching is why the user picked by hand). check_cable_status then derives
            # the matched / patch-path / mismatch state at termination level as usual. A vanished
            # picked port falls back to the name-matching path below.
            if self._apply_manual_interface_target(link, normal_context=normal_context):
                link = self.check_cable_status(link, normal_context=normal_context)
            elif remote_hostname := link.get("remote_device"):
                link = self.process_remote_device(
                    link,
                    remote_hostname,
                    link.get("remote_device_id"),
                    server_key=server_key,
                    normal_context=normal_context,
                )
                if link.get("netbox_remote_device_id"):
                    link = self.check_cable_status(link, normal_context=normal_context)
            if not link.get("netbox_remote_device_id"):
                self._report_one_sided_cable(link, link.get("netbox_local_interface_id"), normal_context)
            link["remote_port_owner_id"] = getattr(
                self._remote_port_owner(link, server_key, normal_context), "pk", None
            )
            self._set_remote_picker_affordance(link, obj, server_key)
            self._set_remote_create_affordance(link, obj, server_key)

        # Drop a serial row only when its ConsoleServerPort exists but is out of view scope. A
        # sensor with no NetBox port at all is LibreNMS data, not NetBox data: it renders as
        # "Console Server Port Not Found in NetBox" and tells the operator which port to create,
        # so hiding it would give a granted user a shorter table than an administrator.
        if serial_links_present and serial_sync_device is not None:
            # Only a row that did NOT resolve can be hiding a port: everything else already
            # matched a viewable one. When every row resolved, this costs no query at all.
            unresolved_names = {
                name
                for link in links_data
                if link.get("_source") == SERIAL_INVENTORY_SOURCE
                and (name := link.get("local_port")) not in serial_ports_by_name
            }
            hidden_serial_port_names = (
                set(
                    ConsoleServerPort.objects.filter(device=serial_sync_device, name__in=unresolved_names).values_list(
                        "name", flat=True
                    )
                )
                if unresolved_names
                else set()
            )
            if hidden_serial_port_names:
                links_data[:] = [
                    link
                    for link in links_data
                    if link.get("_source") != SERIAL_INVENTORY_SOURCE
                    or link.get("local_port") not in hidden_serial_port_names
                ]
        # After resolution, so one adjacency reported over two protocols collapses on what the
        # rows resolved to rather than on the two protocols' differing name strings.
        links_data[:] = dedupe_protocol_duplicates(links_data)
        self._apply_termination_change_scope(
            links_data,
            preloaded_changeable_ids={
                ConsolePort: changeable_console_port_ids,
                ConsoleServerPort: changeable_console_server_port_ids,
            }
            if serial_remote_context is not None
            else None,
        )
        self._apply_cable_rule_blocks(links_data, server_key)
        return links_data

    def _remote_port_owner(self, link, server_key, normal_context=None):
        """
        Return the NetBox device that owns the row's advertised remote port, or None.

        This is the neighbour the row names, or its chassis member for a VC, resolved the way the
        remote end is. A manual pick does not change it. An owner the request may not view is None.
        """
        if normal_context is not None:
            owner = normal_context["remote_owner_by_link"].get(id(link))
            return owner if owner is not None and owner.pk in normal_context["visible_owner_ids"] else None
        device, found, _error = self.get_device_by_id_or_name(
            link.get("remote_device_id"),
            link.get("remote_device"),
            server_key=server_key,
            queryset=self._viewable_queryset(Device),
        )
        if not found:
            return None
        if device.virtual_chassis_id is None:
            return device
        member = get_virtual_chassis_member(device, link.get("remote_port"), return_device_on_failure=False)
        return member if member is not None and self._object_is_viewable(member) else None

    def _apply_cable_rule_blocks(self, links, server_key):
        """
        Refuse, and name the rule, on a row whose write the interface rules block.

        This is the cable sync gate's decision for the ports the snapshot has records for, each
        with the platform of the device that owns it (:func:`port_owner_id`), or no platform. A
        port without a record is decided by the sync itself, after it fetches the record. The
        reason names a port only when ``utils.PortDisclosure`` allows it.

        Args:
            links (list[dict]): The enriched cable rows, updated in place.
            server_key (str): The LibreNMS server whose bindings count.

        Returns:
            None

        """
        rows = [
            (link, ports)
            for link in links
            if link.get("can_create_cable") or link.get("remote_create_url")
            if (ports := [port for port in cable_row_ports(link) if port[2] is not None])
        ]
        if not rows:
            return
        rules = interface_rules_for_request(self.request)
        if not rules.ignores_any_port:
            return
        owner_ids = {port_owner_id(link, side) for link, ports in rows for side, *_ in ports}
        owners = Device.objects.in_bulk(owner_ids - {None})
        decided = [
            (link, [(port_id, record, owners.get(port_owner_id(link, side))) for side, port_id, record in ports])
            for link, ports in rows
        ]
        disclose = PortDisclosure(self.request.user, server_key)
        disclose.preload((port_id, owner) for _link, ports in decided for port_id, _record, owner in ports)
        for link, ports in decided:
            blocked = rules.first_blocked_port(
                (
                    (port_id, record, None if owner is None else owner.platform_id, owner)
                    for port_id, record, owner in ports
                ),
                disclose,
            )
            if blocked is not None:
                link["can_create_cable"] = False
                link.pop("remote_create_url", None)
                link["rule_block"] = row_rule_block(*blocked)
                # A row the rules refuse is in no sync state, as on the other tabs.
                link["cable_comparison"] = UNCLASSIFIED

    def get_table(self, data, obj):
        """Return the cable table for *data*; concrete subclasses choose the table class."""
        raise NotImplementedError

    def _prepare_context(self, request, obj, fetch_fresh=False, server_key=None):
        """Assess the bound source snapshot, then compare its rows with visible NetBox objects."""
        server_key = server_key or self._render_server_key()
        if server_key is None:
            return None
        owner = self._viewable_sync_device(obj, server_key)
        if owner is None:
            return None
        interface_name_field = get_interface_name_field(request, obj)
        result = None
        if fetch_fresh:
            self._cable_refresh_error = None
            librenms_id, lookup_error = self.resolve_librenms_id(owner)
            if lookup_error is not None or (librenms_id is None and not read_mapping(owner).has_oob(server_key)):
                source_cache.delete(obj, Resource.CABLES, server_key)
                self._cable_refresh_error = (
                    self.scoped_lookup_message(lookup_error) if lookup_error else "Device not found in LibreNMS."
                )
                return None
            result = source_cache.refresh(request, obj, Resource.CABLES, server_key)
            self._cable_refresh_error = result.error
            if result.error is not None:
                return None
        assessed = read_cable_assessment(request, obj, server_key, interface_name_field)
        if assessed is None and result is not None:
            self._cable_refresh_error = "Cable data could not be cached. Refresh before syncing."
        if assessed is None:
            return None
        cache_key = source_cache.snapshot_key(obj, Resource.CABLES, server_key)
        raw_links = assessed["links"]
        if request.user.pk is not None:
            raw_links, _has_manual_picks = apply_cable_manual_picks(
                cache, cache_key, assessed["snapshot_id"], request.user.pk, raw_links
            )
        links_data = self.enrich_links_data(raw_links, obj, server_key=server_key, sync_device=owner)
        if isinstance(obj, Device):
            self._disable_actions_for_migrated_owners(links_data, obj, owner, server_key)
        table, filters, filtered_from = self._build_cable_table(request, links_data, obj, server_key)
        return {
            "table": None if assessed["identity_error"] else table,
            "filters": filters,
            "filtered_from": filtered_from,
            "object": obj,
            "cache_expiry": self._cable_cache_expiry(cache_key),
            "server_key": server_key,
            "last_fetched": assessed["last_fetched"],
            "blockers": assessed["blockers"],
        }

    def _disable_actions_for_migrated_owners(self, links_data, obj, cache_device, server_key):
        """Strip every sync affordance from rows owned by a device already merged into another."""
        local_owner_ids = {
            owner_id
            for link in links_data
            if (owner_id := coerce_librenms_id(link.get("netbox_local_device_id"))) is not None
        }
        owner_devices = {device.pk: device for device in (obj, cache_device)}
        owner_devices.update(
            {
                device.pk: device
                for device in self._viewable_queryset(Device).filter(pk__in=local_owner_ids - owner_devices.keys())
            }
        )
        # A render without a resolved server reads the marker under "default", as the marker reader always did.
        marker_key = server_key or "default"
        read_only_owner_ids = {
            device_id for device_id, device in owner_devices.items() if read_mapping(device).migrated_to(marker_key)
        }
        page_is_read_only = obj.pk in read_only_owner_ids or cache_device.pk in read_only_owner_ids
        for link in links_data:
            if (
                not page_is_read_only
                and coerce_librenms_id(link.get("netbox_local_device_id")) not in read_only_owner_ids
            ):
                continue
            link["can_create_cable"] = False
            link.pop("picker_url", None)
            link.pop("remote_create_url", None)

    @staticmethod
    def _cable_cache_expiry(cache_key):
        """Return when the cached snapshot expires, or None when it carries no TTL."""
        cache_ttl = cache.ttl(cache_key)
        if cache_ttl is None or cache_ttl <= 0:
            return None
        return timezone.now() + timezone.timedelta(seconds=cache_ttl)

    def _build_cable_table(self, request, links_data, obj, server_key):
        """
        Build the table of the rows that match the tab's filters, ordered and paginated for the request.

        The rows carry their row IDs and comparisons from the whole snapshot, so the filter runs after them.

        Returns:
            tuple[Table, SyncFilters, int | None]: The table, its bound filters, and the row count before them.

        """
        table, filters, filtered_from = filter_sync_table(request, links_data, lambda rows: self.get_table(rows, obj))
        table.configure(request)
        return table, filters, filtered_from

    def get_context_data(self, request, obj):
        """Get the context data for the cable sync view."""
        # The filters show their errors also when no snapshot is cached.
        filters = parse_table_filters(request.GET, lambda rows: self.get_table(rows, obj))
        # GET render: rebind + scope the cache read to ?server_key (shared helper) so a
        # non-default-server tab reads that server's cache, not the default's.
        scoped, unresolved = self.resolve_get_render_server_key(request)
        if unresolved:
            # The query named a server that no longer resolves (deleted/misconfigured). Its
            # links snapshot may still be cached until TTL — render empty scoped to the
            # requested key instead of serving a removed server's cable rows as a live table
            # (mirrors the interfaces/modules/IP tabs' unresolved guards).
            return {"table": None, "filters": filters, "object": obj, "cache_expiry": None, "server_key": scoped}
        context = self._prepare_context(request, obj, fetch_fresh=False, server_key=scoped)
        if context is None:
            # No data found; return context with empty table
            context = {"table": None, "object": obj, "cache_expiry": None, "server_key": scoped}
        context.setdefault("filters", filters)
        return context

    def post(self, request, pk):
        """Handle POST request for cable sync view."""
        if error := self.require_object_permissions("POST"):
            return error
        obj = self.get_object(pk)
        # Rebind the API to the POSTed server so live link/port fetches hit the same
        # Active server whose source snapshot supplies this multi-server tab. The strict helper,
        # not request.POST.get(): QueryDict.get() keeps the LAST of repeated values, so an
        # ambiguous payload would silently refresh one server's cache namespace from another's.
        server_key = self.rebind_api_for_posted_server(request.POST)
        if server_key is None:
            getlist = getattr(request.POST, "getlist", None)
            posted_values = getlist("server_key") if callable(getlist) else request.POST.get("server_key")
            if isinstance(posted_values, (list, tuple)) and len(posted_values) > 1:
                messages.error(request, "Select exactly one configured LibreNMS server.")
            else:
                messages.error(request, "Selected LibreNMS server is no longer configured.")
            # rebind_api_for_server() returned None to avoid building a missing/misconfigured
            # default client; reading the lazy `librenms_api` property here would reconstruct it
            # and can raise (a 500 on this HTMX error path). Use the already-cached client's key.
            active_server_key = self.active_server_key
            # render_sync_partial injects the migrated-donor context (resolved from the active
            # session key, since the POSTed key is now known-invalid) so a stale server_key can't
            # silently re-enable cable sync on a migrated donor.
            return self.render_sync_partial(
                request,
                obj,
                active_server_key,
                {"cable_sync": {"object": obj, "table": None, "cache_expiry": None, "server_key": None}},
            )
        context = self._prepare_context(request, obj, fetch_fresh=True, server_key=server_key)

        coordinator = SyncCacheConsistency(obj)
        if context is None:
            messages.error(request, getattr(self, "_cable_refresh_error", None) or "No cable source was obtained.")
            coordinator.mark_refresh_failure(SyncTab.CABLES, server_key, actor_id=request_actor_id(request))
            context = {"object": obj, "table": None, "cache_expiry": None, "server_key": server_key}
        else:
            if coordinator.mark_refresh_outcome(SyncTab.CABLES, server_key, actor_id=request_actor_id(request)):
                messages.success(request, "Cable data refreshed successfully.")
            else:
                messages.error(request, "Cable data could not be cached. Refresh again before syncing.")
        return self.render_sync_partial(request, obj, server_key, {"cable_sync": context})


class SingleCableVerifyView(BaseCableTableView):
    """View to verify a single cable link between two devices."""

    # A fetch() caller reads the answer, so the middleware answers with a JSON error.
    answers_json = True

    # Read-only verify endpoint: require object-view permission (mirrors the interface/module
    # verify views). Without it any user with mere plugin-view rights could POST an arbitrary
    # device id and read back that device's rendered cable/topology rows.
    required_object_permissions = {"POST": [("view", Device)]}

    def post(self, request):  # noqa: C901
        data, err = parse_request_json(request)
        if err:
            return err
        # Gate BEFORE resolving the device / touching the LibreNMS client: an unauthorized caller
        # must not be able to probe device IDs or trigger work through this endpoint.
        if error := self.require_object_permissions_json("POST"):
            return error
        selected_device_id = data.get("device_id")
        row_id = data.get("row_id")
        # Honour only a posted key that names a configured server (mirrors the interfaces POST path,
        # issues #108/#109); anything else falls back to the active server.
        server_key = self.resolve_requested_server_key(data)
        if server_key is None:
            return server_unavailable_json()

        formatted_row = {
            "local_port": "",
            "remote_port": "",
            "remote_device": "",
            "cable_status": "Missing Ports",
            "actions": "",
            "can_create_cable": False,
            "expected_local_id": None,
            "expected_local_device_id": None,
            "expected_remote_id": None,
            "expected_remote_device_id": None,
        }

        if selected_device_id:
            # Object-scope the lookup: the gate only checked model-level view_device, so an
            # out-of-scope pk must 404 rather than expose that device's cached cable row.
            selected_device = self.restrict_object_or_404(Device, pk=selected_device_id)
            origin_device = selected_device
            raw_origin_device_id = data.get("origin_device_id")
            if raw_origin_device_id is not None:
                origin_device_id = coerce_librenms_id(raw_origin_device_id)
                if origin_device_id is None:
                    return JsonResponse(
                        {"status": "error", "message": "A valid origin device ID is required."},
                        status=400,
                    )
                origin_device = self.restrict_object_or_404(Device, pk=origin_device_id)
                same_device = origin_device.pk == selected_device.pk
                same_chassis = (
                    origin_device.virtual_chassis_id is not None
                    and origin_device.virtual_chassis_id == selected_device.virtual_chassis_id
                )
                if not same_device and not same_chassis:
                    return JsonResponse(
                        {"status": "error", "message": "The cable page and selected device do not match."},
                        status=400,
                    )
            # Use the same sync-device resolution as the GET path so the cache
            # key matches what _prepare_context wrote. When the VC has no
            # resolvable sync device, return an empty row rather than crashing.
            if selected_device.virtual_chassis:
                primary_device = self._viewable_sync_device(selected_device, server_key)
                if primary_device is None:
                    return JsonResponse({"status": "success", "formatted_row": formatted_row})
            else:
                primary_device = selected_device

            read_only_origin = any(
                read_mapping(device).migrated_to(server_key or "default")
                for device in {origin_device, primary_device, selected_device}
            )

            links_cache_key = source_cache.snapshot_key(origin_device, Resource.CABLES, server_key)
            assessed = read_cable_assessment(
                request, origin_device, server_key, get_interface_name_field(request, origin_device)
            )
            valid_links = assessed["links"] if assessed is not None else None
            if valid_links and request.user.pk is not None:
                valid_links, _has_manual_picks = apply_cable_manual_picks(
                    cache,
                    links_cache_key,
                    assessed["snapshot_id"],
                    request.user.pk,
                    valid_links,
                )
            if valid_links:
                link_data = next(
                    (link for link in valid_links if link.get("row_id") == row_id),
                    None,
                )
                if link_data:
                    manual_remote_id = link_data.get("manual_remote_id")
                    other_protocols = also_reported_by(valid_links, row_id)
                    # Keep the assessment local to this verify request.
                    link_data = dict(link_data)
                    if manual_remote_id is not None:
                        link_data["manual_remote_id"] = manual_remote_id
                    if other_protocols:
                        link_data["also_reported_by"] = other_protocols

                    # Serial rows have a fixed ConsoleServerPort owner. Their owner selector is
                    # disabled, so they never need the member-change verify path.
                    if link_data.get("_source") == SERIAL_INVENTORY_SOURCE:
                        return JsonResponse(
                            {"status": "error", "message": "Serial cable rows have a fixed device owner."},
                            status=400,
                        )

                    # The verify response returns formatted_row HTML directly (it does not pass
                    # through LibreNMSCableTable.render_local_port), so re-apply the OOB badge
                    # here to match the initial render — otherwise a verified OOB cable row loses
                    # the badge and looks like a plain host-port row. Same helper as the table
                    # render, so the two can't drift.
                    oob_badge = oob_badge_html(link_data, leading_space=True)

                    # Re-enrich remote side from current NetBox state
                    remote_hostname = link_data.get("remote_device", "")
                    if not self._apply_manual_interface_target(link_data) and remote_hostname:
                        link_data = self.process_remote_device(
                            link_data, remote_hostname, link_data.get("remote_device_id"), server_key=server_key
                        )
                    link_data["remote_port_owner_id"] = getattr(
                        self._remote_port_owner(link_data, server_key), "pk", None
                    )

                    # `or ""` (not a .get default): the OOB-merge path stores local_port=None when
                    # the port name can't be resolved, and a present-but-None value would otherwise
                    # render the literal string "None" via escape() below.
                    local_port = link_data.get("local_port") or ""
                    formatted_row["local_port"] = local_port

                    # Resolve the local interface on the member selected by the user. The caller
                    # already proved that this Device is visible. Do not infer a different member
                    # from the port name after the dropdown selection.
                    interface = None
                    lookup_device = selected_device
                    # Merged OOB-controller rows are context-only: their local port lives on the
                    # CONTROLLER, so a shared name (or colliding stored librenms_id) must not bind
                    # a HOST interface here — mirrors enrich_local_port's guard on the initial
                    # render. Left unresolved, the row takes the labelled, badge-carrying
                    # unresolved branch below instead of linking the wrong interface.
                    if link_data.get("_source") != OOB_INVENTORY_SOURCE:
                        # Shared id→dual-name resolution core (issue #88 fallback included), so
                        # this path can't drift from enrich_local_port's again.
                        name_candidates = [n for n in (local_port, link_data.get("local_port_alt")) if n]
                        interface = resolve_device_port(
                            lookup_device,
                            server=server_key,
                            port_id=link_data.get("local_port_id"),
                            name_candidates=name_candidates,
                        )
                        if interface is not None and not self._object_is_viewable(interface):
                            interface = None

                    if interface:
                        link_data["netbox_local_interface_id"] = interface.pk
                        link_data["netbox_local_device_id"] = interface.device_id

                        # Check cable status if remote side was resolved
                        if link_data.get("netbox_remote_device_id"):
                            link_data = self.check_cable_status(link_data)
                        else:
                            self._report_one_sided_cable(link_data, interface.pk, None)

                        self._apply_termination_change_scope([link_data])
                        if not read_only_origin:
                            self._set_remote_create_affordance(link_data, selected_device, server_key)
                        self._apply_cable_rule_blocks([link_data], server_key)
                        if read_only_origin:
                            link_data["can_create_cable"] = False
                        formatted_row["can_create_cable"] = bool(link_data.get("can_create_cable"))
                        formatted_row["expected_local_id"] = coerce_librenms_id(
                            link_data.get("netbox_local_interface_id")
                        )
                        formatted_row["expected_local_device_id"] = coerce_librenms_id(
                            link_data.get("netbox_local_device_id")
                        )
                        formatted_row["expected_remote_id"] = coerce_librenms_id(
                            link_data.get("netbox_remote_interface_id")
                        )
                        formatted_row["expected_remote_device_id"] = coerce_librenms_id(
                            link_data.get("netbox_remote_device_id")
                        )

                        # Escape LibreNMS-sourced labels to prevent XSS
                        safe_local_port = escape(local_port)
                        remote_port_name = link_data.get("remote_port_name") or link_data.get("remote_port") or ""
                        remote_device_name = (
                            link_data.get("remote_device_display") or link_data.get("remote_device") or ""
                        )
                        safe_remote_device = escape(remote_device_name)
                        safe_cable_status = escape(link_data.get("cable_status", "Missing Ports"))

                        formatted_row["cable_status"] = safe_cable_status
                        formatted_row["local_port"] = (
                            f'<a href="{reverse("dcim:interface", args=[interface.pk])}">{safe_local_port}</a>{oob_badge}'
                        )
                        formatted_row["remote_port"] = remote_port_html(remote_port_name, link_data)
                        formatted_row["remote_device"] = (
                            f'<a href="{link_data["remote_device_url"]}">{safe_remote_device}</a>'
                            if link_data.get("remote_device_url")
                            else safe_remote_device
                        )
                        if link_data.get("cable_url"):
                            formatted_row["cable_status"] = (
                                f'<a href="{link_data["cable_url"]}">{safe_cable_status}</a>'
                            )

                        if link_data.get("rule_block"):
                            formatted_row["actions"] = rule_block_html(link_data["rule_block"])
                        if link_data.get("can_create_cable"):
                            formatted_row["actions"] = f"""
                                <button type="submit"
                                        class="btn btn-sm btn-primary"
                                        name="sync_one"
                                        value="{escape(str(row_id))}">
                                    {SYNC_CABLE_LABEL}
                                </button>
                            """
                    else:
                        formatted_row["local_port"] = f"{escape(local_port)}{oob_badge}"
                        # Keep remote port name visible, add URL and badges if available
                        remote_port_name = link_data.get("remote_port_name") or link_data.get("remote_port") or ""
                        formatted_row["remote_port"] = remote_port_html(remote_port_name, link_data)
                        # Keep remote device name visible, add URL if available
                        remote_device_name = (
                            link_data.get("remote_device_display") or link_data.get("remote_device") or ""
                        )
                        safe_remote_device = escape(remote_device_name)
                        formatted_row["remote_device"] = (
                            f'<a href="{link_data["remote_device_url"]}">{safe_remote_device}</a>'
                            if link_data.get("remote_device_url")
                            else safe_remote_device
                        )

                        # First check if remote device exists in NetBox
                        if remote_device_name and not link_data.get("remote_device_url"):
                            formatted_row["cable_status"] = "Device Not Found in NetBox"
                        # Then check interface status
                        elif link_data.get("remote_device_url") and link_data.get("remote_port_url"):
                            formatted_row["cable_status"] = "Local Interface Not Found in NetBox"
                        else:
                            formatted_row["cable_status"] = "Missing Interface"

                        formatted_row["actions"] = ""

                    if not read_only_origin:
                        formatted_row["actions"] += self._remote_create_action_html(
                            link_data,
                            selected_device,
                            server_key,
                        )
                        formatted_row["actions"] += self._remote_picker_action_html(
                            link_data,
                            selected_device,
                            server_key,
                        )

        return JsonResponse({"status": "success", "formatted_row": formatted_row})


class CableRemotePickerView(BaseCableTableView):
    """
    Modal picker for a cable row's remote endpoint.

    Remote matching is name-based (the serial label / LLDP port name) and often fails — names
    simply don't always match. This view lets the user pick the remote end by hand:

    - ``GET`` serves the picker modal, plus its HTMX fragments: ``action=search`` (device
      search results) and ``action=ports`` (the chosen device's port list — ConsolePorts for
      serial rows, Interfaces otherwise).
    - ``POST`` stores the pick in a user- and row-scoped cache entry outside the shared LibreNMS
      snapshot. It then re-renders the cable partial and closes the modal.

    The pick only needs to live until the sync runs. Its key includes the snapshot generation, so
    a full "Refresh Cables" invalidates unsynced picks without sharing them across users.
    """

    # Both methods enumerate devices and their ports, so both gate on dcim.view_device (mirroring
    # SingleCableVerifyView). Every device/port lookup below additionally resolves through a
    # restricted queryset: the gate only asks has_perm without an instance, which a CONSTRAINED
    # view_device grant clears.
    required_object_permissions = {"GET": [("view", Device)], "POST": [("view", Device)]}

    def _viewable_ports(self, request, model):
        """Return the permission-filtered port queryset, or None without model access."""
        permission = f"{model._meta.app_label}.view_{model._meta.model_name}"
        if not request.user.has_perm(permission):
            return None
        queryset = self.restricted_queryset(model, "view")
        if model is Interface:
            queryset = queryset.exclude(type__in=VIRTUAL_IFACE_TYPES)
        return queryset

    def _cache_state(self, obj, server_key):
        """Return the cache key and assessed rows of the current bound snapshot."""
        owner = self._viewable_sync_device(obj, server_key)
        cache_key = source_cache.snapshot_key(obj, Resource.CABLES, server_key)
        if owner is None:
            return cache_key, None
        return cache_key, read_cable_assessment(
            self.request, obj, server_key, get_interface_name_field(self.request, obj)
        )

    def _row_is_viewable(self, request, row, obj, server_key):
        """Return whether the picker requester may view a cached row's local endpoint."""
        source = row.get("_source")
        if source == OOB_INVENTORY_SOURCE:
            return False
        local_name = row.get("local_port")
        if not isinstance(local_name, str) or not local_name:
            return False
        if source != SERIAL_INVENTORY_SOURCE:
            _members, owners = self._resolve_local_owners([row], obj, server_key)
            owner = owners[id(row)]
            if owner is None or not self.restricted_queryset(Device).filter(pk=owner.pk).exists():
                return False
            interface = resolve_device_port(
                owner,
                server=server_key,
                port_id=row.get("local_port_id"),
                name_candidates=[local_name, row.get("local_port_alt")],
            )
            return bool(interface is not None and self.restricted_queryset(Interface).filter(pk=interface.pk).exists())

        owner_id = coerce_librenms_id(row.get("device_id"))
        if owner_id is None:
            return False
        if not self.restricted_queryset(Device).filter(pk=owner_id).exists():
            return False
        viewable_ports = self._viewable_ports(request, ConsoleServerPort)
        return bool(viewable_ports is not None and viewable_ports.filter(device_id=owner_id, name=local_name).exists())

    def get(self, request, pk):
        """Serve the picker modal or one of its search/ports HTMX fragments."""
        if denied := self.require_object_permissions("GET"):
            return denied
        obj = self.restrict_object_or_404(Device, pk=pk)
        server_key = self.rebind_api_for_posted_server(request.GET)
        if server_key is None:
            return HttpResponse("Selected LibreNMS server is no longer configured.", status=400)
        row_id = request.GET.get("row_id", "")
        action = request.GET.get("action")
        base_url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[obj.pk])
        if action:
            source = request.GET.get("source") or ""
            base_query = f"row_id={quote_plus(str(row_id))}&server_key={quote_plus(server_key)}"
            if source:
                base_query += f"&source={quote_plus(source)}"
            if action == "search":
                q = (request.GET.get("q") or "").strip()
                devices = (
                    self.restricted_queryset(Device)
                    .filter(name__icontains=q)
                    .select_related("site")
                    .order_by("name")[:20]
                    if q
                    else []
                )
                return render(
                    request,
                    "netbox_librenms_plugin/htmx/_remote_picker_device_results.html",
                    {"devices": devices, "q": q, "ports_url": f"{base_url}?{base_query}&action=ports"},
                )
            if action == "ports":
                try:
                    target_pk = int(request.GET.get("device_id", ""))
                except (TypeError, ValueError):
                    return HttpResponse("Select a device.", status=400)
                target = self.restrict_object_or_404(Device, pk=target_pk)
                serial = source == "serial"
                port_model = ConsolePort if serial else Interface
                viewable_ports = self._viewable_ports(request, port_model)
                if viewable_ports is None:
                    return HttpResponse("You do not have permission to view these ports.", status=403)
                ports = viewable_ports.filter(device=target)
                cabled_ports = ports.none()
                if request.user.has_perm("dcim.view_cable"):
                    visible_cables = self.restricted_queryset(Cable, "view")
                    cabled_ports = ports.filter(cable__in=visible_cables).order_by("name")
                return render(
                    request,
                    "netbox_librenms_plugin/htmx/_remote_picker_ports.html",
                    {
                        "device": target,
                        "free_ports": ports.filter(cable__isnull=True).order_by("name"),
                        "cabled_ports": cabled_ports,
                        "port_noun": "console ports" if serial else "interfaces",
                    },
                )
            return HttpResponse("Unknown picker action.", status=400)

        # Only the modal render needs the row: the fragments above carry `source` in their URLs
        # and run per keystroke, so they never read the snapshot.
        cache_key, cached = self._cache_state(obj, server_key)
        rows = cached["links"] if cached is not None else None
        row = next((candidate for candidate in rows or [] if candidate.get("row_id") == row_id), None)
        if rows is None:
            return HttpResponse("Cable data expired. Refresh Cables before selecting a remote port.", status=409)

        if row is None or not self._row_is_viewable(request, row, obj, server_key):
            return HttpResponse("Cable row not found.", status=404)

        # Carry the row's _source in the fragment URLs so port-type selection does not depend on
        # the cache: with the snapshot expired mid-pick, a serial picker would otherwise silently
        # fall back to listing Interfaces instead of ConsolePorts. Row wins when the cache is
        # alive; the URL param is the fallback.
        source = (row.get("_source") if row else request.GET.get("source")) or ""
        base_query = f"row_id={quote_plus(str(row_id))}" + (
            f"&server_key={quote_plus(server_key)}" if server_key else ""
        )
        if source:
            base_query += f"&source={quote_plus(source)}"

        # Initial modal: prefill the device results with the label match when there is one, so
        # the common "right device, wrong port name" case is one click away from the port list.
        initial_devices = []
        label = (row or {}).get("remote_device")
        if row and label:
            device, found, _ = self.get_device_by_id_or_name(None, label)
            # The label match runs against the plain manager (it is shared with the render path),
            # so keep the prefill inside the user's own scope.
            if found and self.restricted_queryset(Device).filter(pk=device.pk).exists():
                initial_devices = [device]
        return render(
            request,
            "netbox_librenms_plugin/htmx/cable_remote_picker_modal.html",
            {
                "object": obj,
                "row": row,
                "server_key": server_key,
                # The modal posts with the page query, so the re-rendered tab keeps every tab's filters and page.
                "post_url": SyncQuery.from_request(request).url(
                    base_url, {"server_key": server_key}, reset=CABLE_MODAL_QUERY_KEYS
                ),
                "search_url": f"{base_url}?{base_query}&action=search",
                "ports_url": f"{base_url}?{base_query}&action=ports",
                "initial_devices": initial_devices,
            },
        )

    def post(self, request, pk):
        """Validate the picked port and store it on the cached row, then re-render the partial."""
        denied = self.require_all_permissions("POST")
        if denied is not None:
            return denied
        obj = self.restrict_object_or_404(Device, pk=pk)
        server_key = self.rebind_api_for_posted_server(request.POST)
        if server_key is None:
            return HttpResponse("Selected LibreNMS server is no longer configured.", status=400)
        row_id = request.POST.get("row_id", "")
        cache_key, cached = self._cache_state(obj, server_key)
        rows = cached["links"] if cached is not None else None
        row = next((candidate for candidate in rows or [] if candidate.get("row_id") == row_id), None)
        if rows is None:
            return HttpResponse("Cable data expired. Refresh Cables before selecting a remote port.", status=409)

        if row is None or not self._row_is_viewable(request, row, obj, server_key):
            return HttpResponse("Cable row not found.", status=404)

        serial = row.get("_source") == SERIAL_INVENTORY_SOURCE
        port_model = ConsolePort if serial else Interface
        try:
            remote_pk = int(request.POST.get("remote_interface_id", ""))
        except (TypeError, ValueError):
            return HttpResponse("Select a remote port.", status=400)
        # Scope the pick to a device the user may see — the same rule the ports fragment lists by,
        # so a forged remote_interface_id can't bind a termination on an out-of-scope device.
        viewable_ports = self._viewable_ports(request, port_model)
        port = (
            viewable_ports.filter(pk=remote_pk, device__in=self.restricted_queryset(Device))
            .select_related("device")
            .first()
            if viewable_ports is not None
            else None
        )
        if port is None:
            noun = "console port" if serial else "interface"
            return HttpResponse(f"Selected {noun} does not exist (or is the wrong kind of port).", status=400)

        # Keep unsaved UI state outside the shared LibreNMS snapshot. The key includes the
        # snapshot generation, user, and row, so users cannot see or overwrite each other's picks.
        remaining_ttl = cache.ttl(cache_key)
        pick_key = cable_manual_pick_cache_key(
            cache_key,
            cached["snapshot_id"],
            request.user.pk,
            row["row_id"],
        )
        cache.set(
            pick_key,
            {"manual_remote_id": port.pk},
            timeout=remaining_ttl if remaining_ttl and remaining_ttl > 0 else 300,
        )

        # Re-render the cable partial (rows re-enrich against the updated cache) and close the
        # modal via the partial's close_modal OOB block. Mirrors SyncCablesView._sync_response.
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        view = DeviceCableTableView()
        view.setup(request, pk=obj.pk)
        # Re-render from the same server and snapshot scope.
        resolved_key = server_key
        context = view._prepare_context(request, obj, fetch_fresh=False, server_key=resolved_key)
        if context is None:
            context = {"table": None, "object": obj, "cache_expiry": None, "server_key": resolved_key}
        context["close_modal"] = True
        return view.render_sync_partial(request, obj, resolved_key, {"cable_sync": context})
