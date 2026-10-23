"""Shared interface relationship discovery and row resolution."""

import logging
import re
from dataclasses import dataclass, field

from dcim.models import Device, Interface
from django.db.models import Q
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.constants import (
    DEFAULT_INTERFACE_NAME_FIELD,
    INTERFACE_NAME_FIELDS,
    OOB_INVENTORY_SOURCE,
    RELATIONSHIP_KINDS,
)
from netbox_librenms_plugin.librenms_ids import normalize_librenms_port_id
from netbox_librenms_plugin.models import PortStackLagPattern
from netbox_librenms_plugin.server_mappings import MappingRole, identity_q, name_match_may_be_port, read_mapping
from netbox_librenms_plugin.utils import (
    REGEX_COMPILE_ERRORS,
    invert_relationship_edges,
    is_list_of_dicts,
    normalize_relationship_maps,
    normalize_stacked_ports,
)

RELATIONSHIP_CANDIDATE_BATCH_SIZE = 64

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelationshipMaps:
    """Normalized cached relationship data for one LibreNMS snapshot."""

    lag_members: dict
    sub_interfaces: dict
    ports_by_id: dict
    bridge_members: dict = field(default_factory=dict)
    # Ports LibreNMS reports as stacked that no rule classified. Symmetric and directionless, so
    # it needs no inversion and the relationship writer never reads it.
    stacked_ports: dict = field(default_factory=dict)
    # The downward view of each edge map, derived here so every construction site gets it and a
    # per-row read stays a dict lookup rather than a scan of the device's whole port_stack.
    lag_members_by_aggregate: dict = field(init=False)
    sub_interfaces_by_parent: dict = field(init=False)
    bridge_members_by_bridge: dict = field(init=False)

    def __post_init__(self):
        """Derive the three downward views once per snapshot."""
        for source, derived in (
            ("lag_members", "lag_members_by_aggregate"),
            ("sub_interfaces", "sub_interfaces_by_parent"),
            ("bridge_members", "bridge_members_by_bridge"),
        ):
            object.__setattr__(self, derived, invert_relationship_edges(getattr(self, source)))


@dataclass(frozen=True)
class RelationshipResolutionContext:
    """Indexes and permission state shared by every row resolver."""

    obj: object
    server_key: str
    catalog_index: dict
    display_index: dict
    related_index: dict
    source_index: dict
    actionable_owner_ids: set
    changeable_interface_ids: set
    can_write: bool


def interface_owner(interface):
    """Return the ``(device_id, virtual_machine_id)`` owner tuple for an interface."""
    return (getattr(interface, "device_id", None), getattr(interface, "virtual_machine_id", None))


def interface_owner_for_object(obj):
    """Return the owner tuple for a Device or VirtualMachine."""
    if isinstance(obj, Device):
        return (obj.pk, None)
    return (None, obj.pk)


def interface_queryset_for_object(obj):
    """Return the interfaces in a Device chassis or VirtualMachine scope."""
    if isinstance(obj, Device):
        if obj.virtual_chassis_id is not None:
            member_ids = obj.virtual_chassis.members.values_list("id", flat=True)
            return Interface.objects.filter(device__in=member_ids)
        return Interface.objects.filter(device=obj)
    if isinstance(obj, VirtualMachine):
        return VMInterface.objects.filter(virtual_machine=obj)
    return None


def relationship_candidate_q(model, server_key, port_ids, names):
    """Build one query on the interface *model* for stable IDs and safe name hints."""
    unique_port_ids = {
        (type(port_id).__name__, str(port_id)): port_id
        for port_id in port_ids
        if normalize_librenms_port_id(port_id) is not None
    }
    candidate_q = identity_q(
        model,
        server=server_key,
        identities=[unique_port_ids[marker] for marker in sorted(unique_port_ids)],
        roles=(MappingRole.OWN, MappingRole.OOB),
    )
    unique_names = sorted({name for name in names if isinstance(name, str) and name})
    if unique_names:
        candidate_q |= Q(name__in=unique_names)
    return candidate_q


def relationship_candidate_ids(obj, server_key, port_ids, names):
    """Find relationship candidates in bounded batches."""
    interface_queryset = interface_queryset_for_object(obj)
    candidate_ids = set()
    unique_port_ids = sorted(
        {port_id for raw_port_id in port_ids if (port_id := normalize_librenms_port_id(raw_port_id)) is not None}
    )
    unique_names = sorted({name for name in names if isinstance(name, str) and name})

    for start in range(0, len(unique_port_ids), RELATIONSHIP_CANDIDATE_BATCH_SIZE):
        batch = unique_port_ids[start : start + RELATIONSHIP_CANDIDATE_BATCH_SIZE]
        candidate_ids.update(
            interface_queryset.filter(
                relationship_candidate_q(interface_queryset.model, server_key, batch, ())
            ).values_list("pk", flat=True)
        )

    for start in range(0, len(unique_names), RELATIONSHIP_CANDIDATE_BATCH_SIZE):
        batch = unique_names[start : start + RELATIONSHIP_CANDIDATE_BATCH_SIZE]
        candidate_ids.update(interface_queryset.filter(name__in=batch).values_list("pk", flat=True))

    return candidate_ids


def build_interface_index(obj, server_key, user=None, action="change", *, lock=False, allowed_ids=None):
    """Build an ambiguity-preserving interface index for repeated resolution."""
    interface_queryset = interface_queryset_for_object(obj)
    if interface_queryset is None:
        return None

    if allowed_ids is not None:
        interface_queryset = interface_queryset.filter(pk__in=allowed_ids)
    elif user is not None:
        interface_queryset = interface_queryset.restrict(user, action)
    if isinstance(obj, Device):
        interface_queryset = interface_queryset.select_related(
            "bridge__device__virtual_chassis",
            "device__virtual_chassis",
            "device__location",
            "device__rack",
            "device__site",
            "lag__device__virtual_chassis",
            "parent__device__virtual_chassis",
            "untagged_vlan__site",
        )
    else:
        interface_queryset = interface_queryset.select_related(
            "bridge__virtual_machine",
            "virtual_machine",
            "virtual_machine__site",
            "parent__virtual_machine",
            "untagged_vlan__site",
        )
    if lock:
        interface_queryset = interface_queryset.select_for_update(of=("self",)).order_by("pk")

    by_librenms_id = {}
    by_name = {}
    for interface in interface_queryset:
        stored_id = normalize_librenms_port_id(read_mapping(interface).own_id(server_key))
        if stored_id is not None:
            by_librenms_id.setdefault(stored_id, []).append(interface)
        by_name.setdefault(interface.name, []).append(interface)
    return {"by_lnms_id": by_librenms_id, "by_name": by_name}


def filter_interface_index(index, allowed_ids):
    """Return an interface index limited to the supplied primary keys."""
    return {
        key: {
            value: [interface for interface in interfaces if interface.pk in allowed_ids]
            for value, interfaces in mapping.items()
            if any(interface.pk in allowed_ids for interface in interfaces)
        }
        for key, mapping in index.items()
    }


def resolve_interface_by_port_id(
    obj, port_id: str, server_key: str, name_hint: str = "", expected_owner=None, index=None
):
    """Resolve one stable LibreNMS port ID, with a safe exact-name fallback."""
    if not port_id:
        return None, "port_id is required"

    if index is None:
        index = build_interface_index(obj, server_key)
        if index is None:
            return None, f"Unsupported object type: {type(obj).__name__}"

    target_id = normalize_librenms_port_id(port_id)
    matches = list(index["by_lnms_id"].get(target_id, [])) if target_id is not None else []
    if len(matches) > 1:
        return None, f"LibreNMS port_id {port_id} is ambiguous on {obj} (matches multiple interfaces)"
    if expected_owner is not None:
        owned = [match for match in matches if interface_owner(match) == expected_owner]
        if len(owned) == 1:
            return owned[0], None
    elif len(matches) == 1:
        return matches[0], None

    if name_hint:
        interface, error = _resolve_interface_by_name_hint(
            obj,
            name_hint,
            index=index,
            expected_owner=expected_owner,
        )
        if error:
            return None, error
        if interface is not None:
            if expected_owner is not None and interface_owner(interface) != expected_owner:
                return None, f"Interface name '{name_hint}' resolves to a different owner than the selected row"
            if not name_match_may_be_port(interface, server=server_key, port_id=target_id):
                stored_id = normalize_librenms_port_id(read_mapping(interface).own_id(server_key))
                return None, f"Interface name '{name_hint}' is already bound to LibreNMS port_id {stored_id}"
            return interface, None

    if expected_owner is not None and matches:
        return None, f"LibreNMS port_id {port_id} resolves to a different owner than the selected row"
    return None, f"Interface with LibreNMS port_id {port_id} not found on {obj}"


def _resolve_interface_by_name_hint(obj, name_hint, index=None, expected_owner=None):
    """Resolve one exact interface name while preserving ambiguity."""
    if index is not None:
        matches = index["by_name"].get(name_hint, [])
        if expected_owner is not None:
            owned = [match for match in matches if interface_owner(match) == expected_owner]
            if owned:
                matches = owned
        if not matches:
            return None, None
        if len(matches) > 1:
            return None, f"Interface name '{name_hint}' is ambiguous on {obj}"
        return matches[0], None
    try:
        if isinstance(obj, Device):
            if expected_owner is not None and expected_owner[0] is not None:
                interface = Interface.objects.get(device_id=expected_owner[0], name=name_hint)
            elif obj.virtual_chassis_id is not None:
                member_ids = obj.virtual_chassis.members.values_list("id", flat=True)
                interface = Interface.objects.get(device__in=member_ids, name=name_hint)
            else:
                interface = Interface.objects.get(device=obj, name=name_hint)
        else:
            interface = VMInterface.objects.get(virtual_machine=obj, name=name_hint)
        return interface, None
    except (Interface.DoesNotExist, VMInterface.DoesNotExist):
        return None, None
    except (Interface.MultipleObjectsReturned, VMInterface.MultipleObjectsReturned):
        return None, f"Interface name '{name_hint}' is ambiguous on {obj}"


def normalized_pair_key(first_port, second_port):
    """Return one order-independent key for a port pair, or None when it names a single port."""
    first_id = normalize_librenms_port_id(first_port.get("port_id"))
    second_id = normalize_librenms_port_id(second_port.get("port_id"))
    if first_id is None or second_id is None or first_id == second_id:
        return None
    return frozenset((first_id, second_id))


def undirected_adjacency(pair_keys) -> dict[int, list[int]]:
    """
    Expand order-independent port pairs into a symmetric ``port -> [partner, ...]`` map.

    An unclassified stack pair has no direction: LibreNMS reports that two ports are stacked and
    nothing that says which one is the composite. ifStack position does not answer it either, since
    a sub-unit sits on the low side on Junos and a LAG aggregate on either side on SR OS. So both
    ports name the other and neither claims to be the parent.

    Args:
        pair_keys (Iterable[frozenset[int]]): Two-element port-id pairs.

    Returns:
        dict[int, list[int]]: Each port's partners, sorted so two renders cannot disagree.

    """
    adjacency: dict[int, list[int]] = {}
    for pair_key in pair_keys:
        first_id, second_id = sorted(pair_key)
        adjacency.setdefault(first_id, []).append(second_id)
        adjacency.setdefault(second_id, []).append(first_id)
    for partners in adjacency.values():
        partners.sort()
    return adjacency


def resolve_port_relationships(  # noqa: C901
    ports: list,
    port_stack: list,
    *,
    lag_patterns: dict | None = None,
    device_os: str | None = None,
    interface_name_field: str = "ifName",
    compiled_lag_patterns: list | None = None,
    compiled_sap_patterns: list | None = None,
    bridge_patterns: dict | None = None,
    compiled_bridge_patterns: list | None = None,
) -> dict:
    """
    Resolve LAG, parent, and bridge relationships from LibreNMS port-stack data.

    Universal rules (vendor-agnostic, hardcoded):
      1. The LAG aggregate is normally the 'low' entry in a port_stack pair, but the
         aggregate side is determined authoritatively by _is_lag_aggregate() (ifType
         ieee8023adLag or a configured name pattern), not by position, so a pair whose
         aggregate is on the 'high' side is still mapped member->aggregate correctly.
      2. Skip a pair when a SAP pattern matches either port's ifName or ifDescr.
      3. Strip a '.N' suffix in the active name field to resolve a physical-level port.
      4. A pair in the active name field where one name is the other plus a numeric '.N'
         suffix is a parent/child pair. The child may be on either side.
      5. A configured bridge-name pattern identifies the bridge side from ifName or ifDescr.
         This classification is stable across name-field fallback, so one pair cannot be
         reinterpreted as a different relationship kind.
      6. A port with ifType 'ipForward' is an L3 interface: never a LAG member or aggregate.
         In a pair that rules 4 and 5 leave, an ipForward port with a partner of another
         ifType is a sub-interface of that partner as reported (no '.N' stripping). A pair
         of two ipForward ports stays unclassified.

    Configurable via PortStackLagPattern model:
      - Per-OS regex patterns identify LAG aggregates when ifType is not 'ieee8023adLag'.

    Args:
        ports: Port dicts from get_ports(), each with port_id, ifName, ifType keys.
        port_stack: Port stack dicts from get_port_stack(), each with
                    high_port_id and low_port_id keys.
        lag_patterns: Optional dict of {librenms_os: pattern_str} overriding DB lookup.
                      Pass an empty dict to disable name-pattern matching entirely.
                      When None (default), patterns are fetched from PortStackLagPattern,
                      scoped to device_os when that is provided.
        device_os: LibreNMS OS of the device being resolved. When set (and lag_patterns
                   is None), only that OS's pattern is loaded so a vendor-specific regex
                   can't misclassify an interface on another platform. When None, all
                   stored patterns are loaded (legacy, unscoped behaviour).
        interface_name_field: Interface-name field selected for this device
                              ('ifName' or 'ifDescr'). Each relationship map is built from
                              this field alone. A map this field leaves empty is taken from
                              the other field alone.
        compiled_lag_patterns: Optional list of pre-compiled name-pattern regexes. When
                               provided, the PortStackLagPattern DB read and per-call
                               compile are skipped and this list is used directly, taking
                               priority over lag_patterns/device_os.
        compiled_sap_patterns: Optional list of pre-compiled SAP-name regexes. A port_stack
                               row whose ifName or ifDescr names a SAP describes a service,
                               not an interface relationship, so it is skipped. Read from the
                               rows stored for device_os only when the LAG patterns are read
                               from there too, so a caller that supplies its own patterns
                               stays DB-free; an empty list disables the skip.
        bridge_patterns: Optional dict of ``{librenms_os: pattern_str}`` that overrides the
                         bridge-pattern database lookup. Pass an empty dict to disable bridge
                         matching.
        compiled_bridge_patterns: Optional pre-compiled bridge-name regexes. These take
                                  priority over bridge_patterns and the database lookup.

    Returns:
        dict with keys (port_ids are canonical normalized positive ints, so every
        consumer can look up by ``normalize_librenms_port_id(...)`` without re-deriving
        str/int fallbacks):
            'lag_members':    {member_port_id: aggregate_port_id}
            'sub_interfaces': {child_port_id: parent_port_id}
            'bridge_members': {member_port_id: bridge_port_id}
            'stacked_ports':  {port_id: [partner_port_id, ...]} for each pair no rule
                              classified (symmetric, no direction, partners sorted)
            'diagnostics':    pair counts (pairs_seen, _malformed, _unresolved_end,
                              _skipped_sap, _usable, _unclassified), per-kind claimed,
                              edges, conflicted and from_fallback, and the patterns in use

    """
    # A 0-port device (e.g. an iosxr management node with an empty ifTable) can surface
    # `ports`/`port_stack` as None when a caller threads a missing or `null` payload field
    # straight in — `ports_data["ports"]` is None when the LibreNMS body carries
    # `"ports": null`, and likewise a null `mappings`. Coerce any non-list input to [] so
    # resolution is a clean no-op instead of crashing on iteration ("'NoneType' object is
    # not iterable"). A dict/str input already yields no usable entries below; normalizing
    # here keeps that behaviour while closing the None gap.
    if not isinstance(ports, list):
        ports = []
    if not isinstance(port_stack, list):
        port_stack = []
    # Ignore malformed port items without losing valid relationships from the same payload.
    safe_ports = [port for port in ports if isinstance(port, dict)]

    # Keep nameless ports because ifType can identify a LAG aggregate without a name.
    by_id = {}
    ambiguous_port_ids = set()
    for port in safe_ports:
        port_id = normalize_librenms_port_id(port.get("port_id"))
        if port_id is None or port_id in ambiguous_port_ids:
            continue
        if port_id in by_id:
            by_id.pop(port_id)
            ambiguous_port_ids.add(port_id)
            continue
        by_id[port_id] = port
    ports_with_id = list(by_id.values())

    if compiled_lag_patterns is not None:
        compiled_patterns = compiled_lag_patterns
    elif lag_patterns is None:
        compiled_patterns = PortStackLagPattern.compiled_patterns_for_os(device_os)
    else:
        compiled_patterns = []
        for pattern_str in lag_patterns.values():
            # A caller-supplied recording pattern, not a stored rule; the stored rules come in compiled.
            # nosemgrep: regex-error-must-raise
            try:
                compiled_patterns.append(re.compile(pattern_str))
            except (*REGEX_COMPILE_ERRORS, TypeError) as exc:
                logger.warning("Skipping invalid LAG name pattern %r: %s", pattern_str, exc)

    if compiled_sap_patterns is None:
        if compiled_lag_patterns is None and lag_patterns is None:
            compiled_sap_patterns = PortStackLagPattern.compiled_sap_patterns_for_os(device_os)
        else:
            # A caller that supplies LAG patterns also supplies the SAP policy.
            compiled_sap_patterns = []

    if compiled_bridge_patterns is None:
        if bridge_patterns is None and compiled_lag_patterns is None and lag_patterns is None:
            compiled_bridge_patterns = PortStackLagPattern.compiled_bridge_patterns_for_os(device_os)
        elif bridge_patterns is None:
            # A caller that supplies another pattern policy stays database-free.
            compiled_bridge_patterns = []
        else:
            compiled_bridge_patterns = []
            for pattern_str in bridge_patterns.values():
                # A caller-supplied recording pattern, not a stored rule; the stored rules come in compiled.
                # nosemgrep: regex-error-must-raise
                try:
                    compiled_bridge_patterns.append(re.compile(pattern_str))
                except (*REGEX_COMPILE_ERRORS, TypeError) as exc:
                    logger.warning("Skipping invalid bridge name pattern %r: %s", pattern_str, exc)

    def _matches_name_patterns(name, patterns) -> bool:
        if not isinstance(name, str):
            return False
        try:
            return any(pattern.search(name) for pattern in patterns)
        except UnicodeEncodeError:
            # Recording patterns use RE2, which requires a UTF-8 subject.
            return False

    def _has_sap_name(*ports) -> bool:
        """Return whether any name on these ports matches an excluded SAP pattern."""
        names = tuple(
            name
            for port in ports
            for field in INTERFACE_NAME_FIELDS
            if isinstance(name := port.get(field), str) and name
        )
        return any(_matches_name_patterns(name, compiled_sap_patterns) for name in names)

    # Validate and remove SAP rows once so fallback cannot reconsider them. Each drop reason is
    # counted, because this loop is the only place a reported pair disappears before any rule
    # sees it, and "LibreNMS reported nothing" and "we dropped it" looked identical (#179/10).
    pair_counts = dict.fromkeys(("seen", "malformed", "unresolved_end", "sap"), 0)
    filtered_port_pairs = []
    for entry in port_stack:
        pair_counts["seen"] += 1
        if not isinstance(entry, dict):
            pair_counts["malformed"] += 1
            continue
        # Rows carry the ports_stack columns high_port_id/low_port_id; the documented port_id_high spelling is never sent.
        if "high_port_id" not in entry and "low_port_id" not in entry:
            logger.warning("Unrecognized port_stack entry shape, keys: %s", sorted(entry))
            pair_counts["malformed"] += 1
            continue
        high_id = normalize_librenms_port_id(entry.get("high_port_id"))
        low_id = normalize_librenms_port_id(entry.get("low_port_id"))
        if high_id is None or low_id is None:
            pair_counts["unresolved_end"] += 1
            continue
        high_port = by_id.get(high_id)
        low_port = by_id.get(low_id)
        if not high_port or not low_port:
            pair_counts["unresolved_end"] += 1
            continue
        if _has_sap_name(high_port, low_port):
            pair_counts["sap"] += 1
            continue
        filtered_port_pairs.append((high_port, low_port))

    def _resolve_with(field: str, excluded_pairs=None) -> dict:  # noqa: C901
        excluded_pairs = excluded_pairs or set()
        by_name: dict[str, dict] = {}
        ambiguous_names: set[str] = set()
        for port in ports_with_id:
            name = port.get(field)
            if not isinstance(name, str) or not name or name in ambiguous_names:
                continue
            existing = by_name.get(name)
            if existing is not None and normalize_librenms_port_id(
                existing.get("port_id")
            ) != normalize_librenms_port_id(port.get("port_id")):
                by_name.pop(name)
                ambiguous_names.add(name)
                continue
            by_name[name] = port

        lag_members: dict = {}
        sub_interfaces: dict = {}
        bridge_members: dict = {}
        conflicted_lag_members: set = set()
        conflicted_sub_interfaces: set = set()
        conflicted_bridge_members: set = set()
        claimed_pairs: set[frozenset[int]] = set()
        # Which rule claimed each pair, so a map taken from the other name field cannot let a
        # pair count as claimed by a pass whose answer was thrown away.
        claimed_by_kind: dict[str, set] = {kind: set() for kind in RELATIONSHIP_KINDS}

        def _claim(kind: str, pair_key: frozenset) -> None:
            """Record that one rule accounted for this pair."""
            claimed_pairs.add(pair_key)
            claimed_by_kind[kind].add(pair_key)

        def _is_lag_aggregate(port: dict) -> bool:
            if port.get("ifType") == "ieee8023adLag":
                return True
            name = port.get(field)
            return _matches_name_patterns(name, compiled_patterns)

        def _is_l3_interface(port: dict) -> bool:
            return port.get("ifType") == "ipForward"

        def _is_bridge(port: dict) -> bool:
            return any(
                _matches_name_patterns(port.get(name_field), compiled_bridge_patterns)
                for name_field in INTERFACE_NAME_FIELDS
            )

        def _relate(mapping: dict, conflicted_keys: set, key_port: dict, value_port: dict) -> None:
            """Store a normalized edge and drop keys that have conflicting targets."""
            key_id = normalize_librenms_port_id(key_port.get("port_id"))
            value_id = normalize_librenms_port_id(value_port.get("port_id"))
            if key_id is None or value_id is None or key_id in conflicted_keys:
                return
            existing = mapping.get(key_id)
            if existing is None:
                mapping[key_id] = value_id
            elif existing != value_id:
                mapping.pop(key_id, None)
                conflicted_keys.add(key_id)

        def _resolve_physical_port(port: dict) -> dict:
            """Resolve the physical-level port named by the active field."""
            name = port.get(field)
            if not isinstance(name, str):
                return port
            base, separator, suffix = name.rpartition(".")
            if separator and suffix.isdigit():
                return by_name.get(base, port)
            return port

        def _name_derived_parent(port: dict) -> dict | None:
            """Resolve the parent named by a numeric suffix in the active field."""
            name = port.get(field)
            if not isinstance(name, str):
                return None
            base, separator, suffix = name.rpartition(".")
            if not separator or not suffix.isdigit():
                return None
            candidate = by_name.get(base)
            return candidate if candidate is not port else None

        def _is_sub_unit_of(child_port: dict, parent_port: dict) -> bool:
            """Return whether the active field names child_port as a sub-unit."""
            child_name = child_port.get(field)
            parent_name = parent_port.get(field)
            if not isinstance(child_name, str) or not isinstance(parent_name, str) or not parent_name:
                return False
            return child_name.startswith(parent_name + ".") and child_name[len(parent_name) + 1 :].isdigit()

        l3_child_ids: set = set()
        for high_port, low_port in filtered_port_pairs:
            pair_key = normalized_pair_key(high_port, low_port)
            if pair_key is None or pair_key in excluded_pairs:
                continue
            # ifStack ordering does not determine which side is the child.
            if _is_sub_unit_of(low_port, high_port):
                _relate(sub_interfaces, conflicted_sub_interfaces, low_port, high_port)
                _claim("sub_interfaces", pair_key)
                continue
            if _is_sub_unit_of(high_port, low_port):
                _relate(sub_interfaces, conflicted_sub_interfaces, high_port, low_port)
                _claim("sub_interfaces", pair_key)
                continue

            low_is_bridge = _is_bridge(low_port)
            high_is_bridge = _is_bridge(high_port)
            if low_is_bridge != high_is_bridge:
                if low_is_bridge:
                    _relate(bridge_members, conflicted_bridge_members, high_port, low_port)
                else:
                    _relate(bridge_members, conflicted_bridge_members, low_port, high_port)
                _claim("bridge_members", pair_key)
                continue

            low_is_l3 = _is_l3_interface(low_port)
            high_is_l3 = _is_l3_interface(high_port)
            if low_is_l3 != high_is_l3:
                child, parent = (low_port, high_port) if low_is_l3 else (high_port, low_port)
                _relate(sub_interfaces, conflicted_sub_interfaces, child, parent)
                l3_child_ids.add(normalize_librenms_port_id(child.get("port_id")))
                _claim("sub_interfaces", pair_key)
                continue
            if low_is_l3:
                continue

            high_phys = _resolve_physical_port(high_port)
            low_phys = _resolve_physical_port(low_port)
            if normalize_librenms_port_id(high_phys.get("port_id")) == normalize_librenms_port_id(
                low_phys.get("port_id")
            ):
                continue
            if _is_l3_interface(high_phys) or _is_l3_interface(low_phys):
                continue

            low_is_agg = _is_lag_aggregate(low_phys)
            high_is_agg = _is_lag_aggregate(high_phys)
            if low_is_agg and high_is_agg:
                # Structural ifType disambiguates a pair where both names match a LAG pattern.
                low_struct = low_phys.get("ifType") == "ieee8023adLag"
                high_struct = high_phys.get("ifType") == "ieee8023adLag"
                if low_struct and not high_struct:
                    _relate(lag_members, conflicted_lag_members, high_phys, low_phys)
                    _claim("lag_members", pair_key)
                elif high_struct and not low_struct:
                    _relate(lag_members, conflicted_lag_members, low_phys, high_phys)
                    _claim("lag_members", pair_key)
            elif low_is_agg:
                _relate(lag_members, conflicted_lag_members, high_phys, low_phys)
                _claim("lag_members", pair_key)
            elif high_is_agg:
                _relate(lag_members, conflicted_lag_members, low_phys, high_phys)
                _claim("lag_members", pair_key)

        # A name-derived edge shortens the active-field name, so these edges alone cannot form a cycle.
        for port in ports_with_id:
            child_id = normalize_librenms_port_id(port.get("port_id"))
            if child_id is None or child_id in sub_interfaces or child_id in conflicted_sub_interfaces:
                continue
            parent_port = _name_derived_parent(port)
            if parent_port is None or normalize_librenms_port_id(parent_port.get("port_id")) is None:
                continue
            pair_key = normalized_pair_key(port, parent_port)
            if pair_key is None or pair_key in excluded_pairs or pair_key in claimed_pairs:
                continue
            # This loop walks ports_with_id, not filtered_port_pairs, so rule 2 is reapplied here.
            if _has_sap_name(port, parent_port):
                continue
            _relate(sub_interfaces, conflicted_sub_interfaces, port, parent_port)
            _claim("sub_interfaces", pair_key)

        # An ipForward edge can close a parent cycle with name edges; the names are the stronger signal.
        for child_id in sorted(l3_child_ids):
            parent_id, seen = sub_interfaces.get(child_id), set()
            while parent_id is not None and parent_id != child_id and parent_id not in seen:
                seen.add(parent_id)
                parent_id = sub_interfaces.get(parent_id)
            if parent_id == child_id:
                sub_interfaces.pop(child_id)
                conflicted_sub_interfaces.add(child_id)

        return {
            "lag_members": lag_members,
            "sub_interfaces": sub_interfaces,
            "bridge_members": bridge_members,
            "claimed_pairs": claimed_pairs,
            "claimed_by_kind": claimed_by_kind,
            "conflicted": {
                "lag_members": len(conflicted_lag_members),
                "sub_interfaces": len(conflicted_sub_interfaces),
                "bridge_members": len(conflicted_bridge_members),
            },
        }

    if not isinstance(interface_name_field, str) or interface_name_field not in INTERFACE_NAME_FIELDS:
        interface_name_field = DEFAULT_INTERFACE_NAME_FIELD
    active = _resolve_with(interface_name_field)
    resolved = {kind: active[kind] for kind in RELATIONSHIP_KINDS}
    claims = {kind: active["claimed_by_kind"][kind] for kind in RELATIONSHIP_KINDS}
    conflicted = dict(active["conflicted"])
    from_fallback = dict.fromkeys(RELATIONSHIP_KINDS, False)

    # Take each empty map from the other field without mixing fields within a map.
    if not all(resolved.values()):
        fallback_field = next(field for field in INTERFACE_NAME_FIELDS if field != interface_name_field)
        fallback = _resolve_with(fallback_field, active["claimed_pairs"])
        for kind in RELATIONSHIP_KINDS:
            if resolved[kind] or claims[kind] or conflicted[kind]:
                continue
            logger.debug(
                "The %s map from %s is empty. The resolver uses %s alone.",
                kind,
                interface_name_field,
                fallback_field,
            )
            resolved[kind] = fallback[kind]
            # The active pass produced no surviving edge of this kind, so its claims must not
            # mask a pair the pass that was actually adopted left unclassified.
            claims[kind] = fallback["claimed_by_kind"][kind]
            conflicted[kind] = fallback["conflicted"][kind]
            from_fallback[kind] = True

    all_pairs = {
        pair_key
        for high_port, low_port in filtered_port_pairs
        if (pair_key := normalized_pair_key(high_port, low_port)) is not None
    }
    unclassified_pairs = all_pairs.difference(*claims.values())
    return {
        **resolved,
        # A pair LibreNMS reported that no rule claimed is still a relationship: only its kind
        # and its direction are unknown. It used to be dropped here, which made a Linux bridge
        # look exactly like a device LibreNMS has no port_stack for (#179 item 10).
        "stacked_ports": undirected_adjacency(unclassified_pairs),
        "diagnostics": {
            "name_field": interface_name_field,
            "ports_seen": len(ports_with_id),
            "pairs_seen": pair_counts["seen"],
            "pairs_malformed": pair_counts["malformed"],
            "pairs_unresolved_end": pair_counts["unresolved_end"],
            "pairs_skipped_sap": pair_counts["sap"],
            "pairs_usable": len(filtered_port_pairs),
            "pairs_unclassified": len(unclassified_pairs),
            "kinds": [
                {
                    "key": kind,
                    "claimed": len(claims[kind]),
                    "edges": len(resolved[kind]),
                    "conflicted": conflicted[kind],
                    "from_fallback": from_fallback[kind],
                }
                for kind in RELATIONSHIP_KINDS
            ],
            "patterns": {
                "lag": [getattr(pattern, "pattern", "") for pattern in compiled_patterns],
                "bridge": [getattr(pattern, "pattern", "") for pattern in compiled_bridge_patterns],
                "sap": [getattr(pattern, "pattern", "") for pattern in compiled_sap_patterns],
            },
        },
    }


def build_relationship_maps(cached_data):
    """Normalize relationship maps and index host ports by stable ID."""
    relationships = cached_data.get("port_stack_relationships")
    lag_members, sub_interfaces, bridge_members = normalize_relationship_maps(relationships)
    stacked_ports = normalize_stacked_ports(relationships)
    ports = cached_data.get("ports", [])
    if not is_list_of_dicts(ports):
        ports = []
    ports_by_id = {}
    for port in ports:
        if port.get("_source") == OOB_INVENTORY_SOURCE:
            continue
        port_id = normalize_librenms_port_id(port.get("port_id"))
        if port_id is not None:
            ports_by_id[port_id] = port
    return RelationshipMaps(lag_members, sub_interfaces, ports_by_id, bridge_members, stacked_ports)


# How many unclassified pairs the report names before it stops listing them.
UNCLASSIFIED_PAIR_LIMIT = 25

# Presentation labels for the typed relationship kinds, keyed as the resolver reports them.
_KIND_LABELS = {
    "sub_interfaces": "Sub-interface",
    "bridge_members": "Bridge",
    "lag_members": "LAG",
}


def _diagnostics_verdict(diagnostics):
    """Return the one sentence that says which of the empty cases this device is in."""
    # A snapshot cached by an older release can be missing a counter the report now reads.
    diagnostics = {
        key: diagnostics.get(key, 0)
        for key in (
            "pairs_seen",
            "pairs_usable",
            "pairs_unclassified",
            "pairs_skipped_sap",
            "pairs_unresolved_end",
            "pairs_malformed",
        )
    }
    if not diagnostics["pairs_seen"]:
        return (
            "LibreNMS reports no port_stack rows for this device, so it has no interface "
            "relationship data to read. No name pattern can add one."
        )
    if not diagnostics["pairs_usable"]:
        if diagnostics["pairs_skipped_sap"] == diagnostics["pairs_seen"]:
            return (
                "Every row LibreNMS reported names a service access point, which describes a "
                "service rather than an interface relationship. Check this OS's SAP name pattern."
            )
        if diagnostics["pairs_unresolved_end"] == diagnostics["pairs_seen"]:
            return (
                "Every row LibreNMS reported names a port it does not poll, so neither end can be "
                "resolved. The relationships exist on the device but not in LibreNMS's port list."
            )
        return "LibreNMS reported stack rows, but none could be used. See the rejection counts below."
    if diagnostics["pairs_unclassified"]:
        return (
            "LibreNMS reports these ports as stacked, but no rule says what kind of relationship "
            "it is. Naming the composite side in this OS's bridge or LAG name pattern classifies "
            "them; until then they are shown untyped."
        )
    return "Every usable pair LibreNMS reported was classified."


def relationship_diagnostics_report(cached_data, interface_name_field="ifName"):
    """
    Summarize what this device's port_stack contained and what each rule made of it.

    "The name pattern is wrong" and "LibreNMS has no data for this device" both render as an empty
    Relationships column, and telling them apart used to need the server log (issue #179 item 10).
    The counts come from the cached snapshot rather than a fresh resolve, so the report describes
    exactly the rows the table is showing and cannot drift from them.

    Args:
        cached_data (dict): The cached LibreNMS ports snapshot.
        interface_name_field (str): The field the tab names interfaces by.

    Returns:
        dict | None: The template-ready report, or None when the snapshot predates it.

    """
    if not isinstance(cached_data, dict):
        return None
    relationships = cached_data.get("port_stack_relationships")
    if not isinstance(relationships, dict):
        return None
    diagnostics = relationships.get("diagnostics")
    if not isinstance(diagnostics, dict) or "pairs_seen" not in diagnostics:
        return None

    ports = cached_data.get("ports", [])
    names_by_id = {}
    if is_list_of_dicts(ports):
        for port in ports:
            port_id = normalize_librenms_port_id(port.get("port_id"))
            name = port.get(interface_name_field)
            if port_id is not None and isinstance(name, str) and name:
                names_by_id[port_id] = name

    # Rebuild the pairs from the symmetric adjacency, keeping each one once.
    stacked_ports = normalize_stacked_ports(relationships)
    pairs = sorted(
        {
            (min(port_id, partner_id), max(port_id, partner_id))
            for port_id, partners in stacked_ports.items()
            for partner_id in partners
        }
    )
    named_pairs = [
        (names_by_id.get(first_id, f"port {first_id}"), names_by_id.get(second_id, f"port {second_id}"))
        for first_id, second_id in pairs[:UNCLASSIFIED_PAIR_LIMIT]
    ]

    cached_kinds = diagnostics.get("kinds")
    kinds = [
        {**kind, "label": _KIND_LABELS.get(kind.get("key"), kind.get("key"))}
        for kind in (cached_kinds if isinstance(cached_kinds, list) else [])
        if isinstance(kind, dict)
    ]
    return {
        **{key: value for key, value in diagnostics.items() if key != "kinds"},
        "kinds": kinds,
        "unclassified_pairs": named_pairs,
        "unclassified_truncated": max(len(pairs) - UNCLASSIFIED_PAIR_LIMIT, 0),
        "verdict": _diagnostics_verdict(diagnostics),
    }


def build_candidate_relationship_context(obj, server_key, user, can_write, port_ids, names):
    """Build one permission-aware relationship context for a bounded row set."""
    interface_queryset = interface_queryset_for_object(obj)
    candidate_queryset = interface_queryset.filter(
        relationship_candidate_q(interface_queryset.model, server_key, port_ids, names)
    )
    catalog_ids = set(candidate_queryset.values_list("pk", flat=True))
    catalog_index = build_interface_index(obj, server_key, allowed_ids=catalog_ids)

    owner_model = Device if isinstance(obj, Device) else VirtualMachine
    owner_field = "device_id" if isinstance(obj, Device) else "virtual_machine_id"
    owner_ids = candidate_queryset.values_list(owner_field, flat=True)
    actionable_owner_ids = set(
        owner_model.objects.restrict(user, "view").filter(pk__in=owner_ids).values_list("pk", flat=True)
    )
    permitted_queryset = candidate_queryset.filter(**{f"{owner_field}__in": actionable_owner_ids})
    viewable_ids = set(permitted_queryset.restrict(user, "view").values_list("pk", flat=True))
    changeable_ids = set(permitted_queryset.restrict(user, "change").values_list("pk", flat=True))
    display_index = build_interface_index(obj, server_key, allowed_ids=viewable_ids | changeable_ids)
    source_index = filter_interface_index(display_index, changeable_ids)
    return RelationshipResolutionContext(
        obj=obj,
        server_key=server_key,
        catalog_index=catalog_index,
        display_index=display_index,
        related_index=display_index,
        source_index=source_index,
        actionable_owner_ids=actionable_owner_ids,
        changeable_interface_ids=changeable_ids,
        can_write=can_write,
    )


def enrich_port_relationships(
    port,
    relationship_maps,
    interface_name_field="ifName",
    server_key="",
):
    """Add LAG, parent, and bridge comparison fields to a cached port row."""
    port_id = normalize_librenms_port_id(port.get("port_id"))
    netbox_interface = port.get("netbox_interface")

    def related_interface_matches(netbox_related, librenms_related):
        if netbox_related is None or librenms_related is None:
            return False
        server = server_key or "default"
        target_id = normalize_librenms_port_id(librenms_related.get("port_id"))
        if not name_match_may_be_port(netbox_related, server=server, port_id=target_id):
            return False
        stored_id = normalize_librenms_port_id(read_mapping(netbox_related).own_id(server))
        if stored_id is not None and target_id is not None:
            return True
        return netbox_related.name in (
            librenms_related.get("ifName"),
            librenms_related.get("ifDescr"),
            librenms_related.get(interface_name_field),
        )

    def relationship_context(port_id_to_related, related_attribute):
        related_port_id = normalize_librenms_port_id(port_id_to_related.get(port_id)) if port_id else None
        related_port = relationship_maps.ports_by_id.get(related_port_id) if related_port_id else None
        related_name = related_port.get(interface_name_field) if related_port else None
        netbox_related = getattr(netbox_interface, related_attribute, None) if netbox_interface else None
        if related_port_id and netbox_interface:
            if netbox_related and related_interface_matches(netbox_related, related_port):
                status = "match"
            elif netbox_related:
                status = "mismatch"
            else:
                status = "missing_nb"
        elif related_port_id:
            status = "missing_nb"
        elif netbox_related:
            status = "missing_lnms"
        else:
            status = None
        return related_name, related_port_id, status

    lag_name, lag_port_id, lag_status = relationship_context(relationship_maps.lag_members, "lag")
    port["librenms_lag_name"] = lag_name
    port["librenms_lag_port_id"] = lag_port_id
    port["lag_sync_status"] = lag_status

    parent_name, parent_port_id, parent_status = relationship_context(
        relationship_maps.sub_interfaces,
        "parent",
    )
    port["librenms_parent_name"] = parent_name
    port["librenms_parent_port_id"] = parent_port_id
    port["parent_sync_status"] = parent_status

    bridge_name, bridge_port_id, bridge_status = relationship_context(
        relationship_maps.bridge_members,
        "bridge",
    )
    port["librenms_bridge_name"] = bridge_name
    port["librenms_bridge_port_id"] = bridge_port_id
    port["bridge_sync_status"] = bridge_status

    def member_names(members_by_aggregate):
        """Name every member LibreNMS attaches to this row, in the inversion's order."""
        names = []
        for member_id in members_by_aggregate.get(port_id, ()) if port_id else ():
            member_port = relationship_maps.ports_by_id.get(member_id)
            member_name = member_port.get(interface_name_field) if member_port else None
            # Only a str is a name: LibreNMS copies ifName unvalidated, and joining an int would
            # 500 the table. A trimmed snapshot must not shrink the count, so the slot is kept.
            if not isinstance(member_name, str) or not member_name:
                member_name = f"port {member_id}"
            names.append(member_name)
        return names

    port["librenms_lag_member_names"] = member_names(relationship_maps.lag_members_by_aggregate)
    port["librenms_sub_interface_names"] = member_names(relationship_maps.sub_interfaces_by_parent)
    port["librenms_bridge_member_names"] = member_names(relationship_maps.bridge_members_by_bridge)
    # The untyped view reads the same way, because the adjacency is already the downward one.
    port["librenms_stacked_port_names"] = member_names(relationship_maps.stacked_ports)


def _row_relationship_source_is_actionable(
    context,
    owner,
    port_id,
    resolved_interface,
    *,
    unique_host_port_ids,
    name_fallback_allowed,
    name_hint,
    expected_owner,
):
    """Return whether this row can write an interface relationship."""
    if not (
        context.can_write
        and owner.pk in context.actionable_owner_ids
        and port_id in unique_host_port_ids
        and resolved_interface is not None
    ):
        return False

    catalog_matches = context.catalog_index["by_lnms_id"].get(port_id, [])
    if catalog_matches:
        return (
            len(catalog_matches) == 1
            and catalog_matches[0].pk == resolved_interface.pk
            and resolved_interface.pk in context.changeable_interface_ids
        )

    if not name_fallback_allowed:
        return False

    source_interface, error = resolve_interface_by_port_id(
        context.obj,
        str(port_id),
        context.server_key,
        name_hint=name_hint,
        expected_owner=expected_owner,
        index=context.source_index,
    )
    return error is None and getattr(source_interface, "pk", None) == resolved_interface.pk


def resolve_relationship_row(
    context,
    port,
    owner,
    interface_name_field,
    unique_host_port_ids,
    unambiguous_name_port_ids,
    relationship_maps,
):
    """Resolve and enrich one relationship row using the shared table and verify rules."""
    port_id = normalize_librenms_port_id(port.get("port_id"))
    if port_id is not None:
        port["port_id"] = port_id
    if port.get("_source") == OOB_INVENTORY_SOURCE:
        # An OOB port syncs onto this device, so it can own an interface here: resolve that by
        # the stable port_id. Never pass a name hint — an OOB "eth0" would otherwise bind to the
        # host's unrelated "eth0". Relationship topology stays host-only: LAG/parent/bridge edges
        # describe the host's LibreNMS device, not the controller's.
        oob_interface = None
        if port_id is not None:
            oob_interface, _ = resolve_interface_by_port_id(
                context.obj,
                str(port_id),
                context.server_key,
                name_hint="",
                expected_owner=interface_owner_for_object(owner),
                index=context.display_index,
            )
        port["netbox_interface"] = oob_interface
        port["exists_in_netbox"] = oob_interface is not None
        port["relationship_source_resolvable"] = False
        port["lag_target_resolvable"] = False
        port["parent_target_resolvable"] = False
        port["bridge_target_resolvable"] = False
        return oob_interface

    name_fallback_allowed = port_id in unambiguous_name_port_ids
    name_hint = (port.get(interface_name_field) or "") if name_fallback_allowed else ""
    expected_owner = interface_owner_for_object(owner)
    resolved_interface = None
    if port_id is not None:
        resolved_interface, _ = resolve_interface_by_port_id(
            context.obj,
            str(port_id),
            context.server_key,
            name_hint=name_hint,
            expected_owner=expected_owner,
            index=context.display_index,
        )
    port["netbox_interface"] = resolved_interface
    port["exists_in_netbox"] = resolved_interface is not None

    port["relationship_source_resolvable"] = _row_relationship_source_is_actionable(
        context,
        owner,
        port_id,
        resolved_interface,
        unique_host_port_ids=unique_host_port_ids,
        name_fallback_allowed=name_fallback_allowed,
        name_hint=name_hint,
        expected_owner=expected_owner,
    )

    enrich_port_relationships(port, relationship_maps, interface_name_field, context.server_key)
    for relation in ("lag", "parent", "bridge"):
        related_port_id = port.get(f"librenms_{relation}_port_id")
        related_port = relationship_maps.ports_by_id.get(related_port_id)
        related_interface = None
        if related_port is not None and related_port_id in unique_host_port_ids:
            related_name_hint = (
                (related_port.get(interface_name_field) or "") if related_port_id in unambiguous_name_port_ids else ""
            )
            catalog_interface, catalog_error = resolve_interface_by_port_id(
                context.obj,
                str(related_port_id),
                context.server_key,
                name_hint=related_name_hint,
                index=context.catalog_index,
            )
            if catalog_error is None:
                permitted_interface, permitted_error = resolve_interface_by_port_id(
                    context.obj,
                    str(related_port_id),
                    context.server_key,
                    name_hint=related_name_hint,
                    index=context.related_index,
                )
                if (
                    permitted_error is None
                    and catalog_interface is not None
                    and getattr(permitted_interface, "pk", None) == catalog_interface.pk
                ):
                    related_interface = permitted_interface
            if (
                relation == "lag"
                and related_interface is not None
                and getattr(related_interface, "type", None) != "lag"
                and related_interface.pk not in context.changeable_interface_ids
            ):
                related_interface = None
        port[f"{relation}_target_resolvable"] = related_interface is not None
    return resolved_interface
