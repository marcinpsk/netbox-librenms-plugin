"""Compare VLAN source facts with the NetBox VLANs visible to the caller, and offer the VLAN operations."""

from dataclasses import dataclass

from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.constants import LIBRENMS_VLAN_STATE_ACTIVE
from netbox_librenms_plugin.utils import index_vlan_source_rows, normalize_vlan_vid, vlan_source_namer

# The status of every VLAN that a sync creates.
CREATED_VLAN_STATUS = "active"


@dataclass(frozen=True)
class VLANIdentity:
    """The source VID that one VLAN operation synchronizes."""

    vid: int


@dataclass(frozen=True)
class VLANChoices:
    """The VLAN group the operator chose for the VID; None is the global scope."""

    group: int | None


@dataclass(frozen=True)
class VLANTarget:
    """The NetBox VLAN that holds the VID in the chosen scope."""

    pk: int
    vid: int
    group: int | None
    name: str


@dataclass(frozen=True)
class VLANAssumptions:
    """The target VLAN the operation acts on, or None when no VLAN holds the VID in the chosen scope."""

    target: VLANTarget | None


@dataclass(frozen=True)
class VLANCreateEffects:
    """The VLAN that a create adds."""

    vid: int
    group: int | None
    name: str
    status: str


@dataclass(frozen=True)
class VLANUpdateEffects:
    """The name that an update gives the target VLAN."""

    vlan: int
    name: str


@intent.register
@dataclass(frozen=True)
class VLANCreate:
    """Create the VLAN of a source VID in the chosen scope, where no VLAN holds the VID."""

    RESOURCE = intent.Resource.VLAN
    ACTION = "create"

    identity: VLANIdentity
    choices: VLANChoices
    assumptions: VLANAssumptions
    effects: VLANCreateEffects


@intent.register
@dataclass(frozen=True)
class VLANUpdate:
    """Give the VLAN that holds a source VID in the chosen scope the proposed name."""

    RESOURCE = intent.Resource.VLAN
    ACTION = "update"

    identity: VLANIdentity
    choices: VLANChoices
    assumptions: VLANAssumptions
    effects: VLANUpdateEffects

    @property
    def renames(self):
        """Return whether the update changes the name of the target VLAN."""
        return self.effects.name != self.assumptions.target.name


def build_operation(*, vid, group, name, targets):
    """
    Return the operation that synchronizes one source VID into the chosen scope.

    Args:
        vid (int): The canonical source VID.
        group (int | None): The chosen VLAN group pk, or None for the global scope.
        name: The name that the VLAN name rules propose for the source row.
        targets: The NetBox VLANs that hold the VID in the chosen scope.

    Returns:
        VLANCreate | VLANUpdate | Unavailable: The operation, or the reason that no operation exists.

    """
    if len(targets) > 1:
        return intent.Unavailable("target.ambiguous")
    identity, choices = VLANIdentity(vid), VLANChoices(group)
    # A source name that is not text has no valid NetBox name, so the writer refuses it as invalid.
    proposed = name if isinstance(name, str) else ""
    if not targets:
        return VLANCreate(
            identity, choices, VLANAssumptions(None), VLANCreateEffects(vid, group, proposed, CREATED_VLAN_STATUS)
        )
    target = targets[0]
    return VLANUpdate(
        identity,
        choices,
        VLANAssumptions(VLANTarget(target.pk, target.vid, target.group_id, target.name)),
        VLANUpdateEffects(target.pk, proposed),
    )


@dataclass(frozen=True)
class VLANAssessment:
    """Compared rows, each with the token of the operation it offers, and the source identities."""

    rows: list
    source_by_vid: dict
    invalid_rows: list


def assess(snapshot, *, subject, lookup_maps, context, choices=None, vids=None):
    """
    Assess VLAN rows and offer each row's operation, without HTTP, cache access or writes.

    Args:
        snapshot: The VLAN source snapshot.
        subject: The device that owns the snapshot.
        lookup_maps: The VLAN lookup maps of the scope the caller may view.
        context (IntentContext | None): The context the offers belong to. None offers nothing.
        choices (dict | None): A chosen group pk (or None) by VID, in place of the automatic choice.
        vids (set | None): Assess only these VIDs.

    Returns:
        VLANAssessment: Each row carries ``confirmation_intent``, its token or None.

    """
    source = snapshot.value(Resource.VLANS, "vlans")
    indexed, invalid = index_vlan_source_rows(source)
    rows = compare_rows(source, lookup_maps=lookup_maps, subject=subject, choices=choices, vids=vids)
    for row in rows:
        operation = row["operation"]
        offered = context is not None and operation is not None and not isinstance(operation, intent.Unavailable)
        row["confirmation_intent"] = intent.offer(operation, context).token if offered else None
    return VLANAssessment(rows, indexed, invalid)


def compare_vlan(netbox_vlan, name):
    """Return the existence and name comparison used by the tab and verify endpoint."""
    return {
        "exists_in_netbox": netbox_vlan is not None,
        "netbox_vlan_name": netbox_vlan.name if netbox_vlan is not None else None,
        "name_matches": netbox_vlan.name == name if netbox_vlan is not None else False,
    }


def _auto_selected_group(vid, vid_to_groups, subject):
    """Return the group the tab selects for a VID, and whether several groups tie for it."""
    groups = vid_to_groups.get(vid, [])
    if len(groups) == 1:
        return groups[0], False
    if len(groups) > 1:
        most_specific = select_most_specific_group(groups, subject)
        return most_specific, most_specific is None
    return None, False


def compare_rows(librenms_vlans, *, lookup_maps, subject, choices=None, vids=None):
    """
    Compare LibreNMS VLANs against NetBox VLANs available to the device.

    Args:
        librenms_vlans: List of VLAN dicts from LibreNMS
        lookup_maps: Dict with vid_to_groups, vid_group_to_vlan, vid_to_vlans
        subject: NetBox Device object for scope-based prioritization
        choices: A chosen group pk (or None for global) by VID, in place of the automatic choice
        vids: Compare only the rows of these VIDs

    Adds comparison flags:
    - exists_in_netbox: bool
    - netbox_vlan: VLAN object or None
    - netbox_vlan_group: VLANGroup name or None
    - name_matches: bool
    - group_choice_id: ID of the group the row synchronizes into, or None for global
    - auto_selected_group_id: ID of auto-selected group or None
    - auto_selected_group_name: Name of auto-selected group or None
    - is_ambiguous: bool - True if VID exists in multiple groups with no clear priority
    - operation: the row's operation, an Unavailable reason, or None for a row without a usable VID

    """
    lookup_maps = lookup_maps or {}
    choices = choices or {}
    vid_to_groups = lookup_maps.get("vid_to_groups", {})
    vid_to_vlans = lookup_maps.get("vid_to_vlans", {})

    rows_by_vid, _invalid_rows = index_vlan_source_rows(librenms_vlans)
    source_name = vlan_source_namer(subject)
    compared = []
    for vlan in librenms_vlans:
        raw_vid = vlan.get("vlan_vlan")
        canonical_vid = normalize_vlan_vid(raw_vid)
        if vids is not None and canonical_vid not in vids:
            continue
        if canonical_vid is None:
            vid_error = "Invalid VID"
        elif canonical_vid not in rows_by_vid:
            vid_error = "Duplicate VID"
        else:
            vid_error = None
        vid = canonical_vid if vid_error is None else None
        name = source_name(vlan, vid)

        auto_group, is_ambiguous = _auto_selected_group(vid, vid_to_groups, subject)
        group_choice_id = choices[vid] if vid in choices else (auto_group.pk if auto_group else None)
        # The row compares, and offers, the VLAN that holds the VID in the chosen scope.
        targets = [v for v in vid_to_vlans.get(vid, []) if v.group_id == group_choice_id]
        netbox_vlan = targets[0] if targets else None
        operation = (
            build_operation(vid=vid, group=group_choice_id, name=name, targets=targets) if vid is not None else None
        )

        compared.append(
            {
                "vlan_id": vid,
                "raw_vlan_id": raw_vid,
                "vid_error": vid_error,
                "name": name,
                "librenms_name": vlan.get("vlan_name"),
                "type": vlan.get("vlan_type", "ethernet"),
                "state": vlan.get("vlan_state", LIBRENMS_VLAN_STATE_ACTIVE),
                **compare_vlan(netbox_vlan, name),
                "netbox_vlan_id": netbox_vlan.pk if netbox_vlan else None,
                "netbox_vlan_group": netbox_vlan.group.name if netbox_vlan and netbox_vlan.group else None,
                "netbox_vlan_group_id": netbox_vlan.group.pk if netbox_vlan and netbox_vlan.group else None,
                # Fields for per-row VLAN group selection
                "group_choice_id": group_choice_id,
                "auto_selected_group_id": auto_group.pk if auto_group else None,
                "auto_selected_group_name": auto_group.name if auto_group else None,
                "is_ambiguous": is_ambiguous,
                "operation": operation,
            }
        )

    return compared


def select_most_specific_group(groups, device):
    """
    Select the most specific VLAN group based on device context.

    Priority order (most specific to least specific):
    1. Rack-scoped (device's rack)
    2. Location-scoped (device's location, closer ancestors win)
    3. Site-scoped (device's site)
    4. Site Group-scoped (device's site's group, closer ancestors win)
    5. Region-scoped (device's site's region, closer ancestors win)
    6. Global (no scope)

    Args:
        groups: List of VLANGroup objects that all contain the same VID
        device: NetBox Device object

    Returns:
        VLANGroup or None if no clear winner (e.g., multiple groups at same priority level)

    """
    from dcim.models import Location, Rack, Region, Site, SiteGroup
    from django.contrib.contenttypes.models import ContentType

    if not device or not groups:
        return None

    # Build scope priority lookup for this device
    # Lower number = higher priority (more specific)
    scope_priority = {}
    priority = 0

    # Priority 1: Rack (most specific)
    if hasattr(device, "rack") and device.rack:
        rack_ct = ContentType.objects.get_for_model(Rack)
        scope_priority[(rack_ct.pk, device.rack.pk)] = priority
        priority += 1

    # Priority 2: Location hierarchy (device's location first, then ancestors)
    if hasattr(device, "location") and device.location:
        location_ct = ContentType.objects.get_for_model(Location)
        for loc in ancestors(device.location):
            scope_priority[(location_ct.pk, loc.pk)] = priority
            priority += 1

    # Priority 3: Site
    if hasattr(device, "site") and device.site:
        site_ct = ContentType.objects.get_for_model(Site)
        scope_priority[(site_ct.pk, device.site.pk)] = priority
        priority += 1

        # Priority 4: Site Group hierarchy
        if device.site.group:
            site_group_ct = ContentType.objects.get_for_model(SiteGroup)
            for sg in ancestors(device.site.group):
                scope_priority[(site_group_ct.pk, sg.pk)] = priority
                priority += 1

        # Priority 5: Region hierarchy
        if device.site.region:
            region_ct = ContentType.objects.get_for_model(Region)
            for reg in ancestors(device.site.region):
                scope_priority[(region_ct.pk, reg.pk)] = priority
                priority += 1

    # Priority 6: Global (no scope) - lowest priority
    global_priority = priority

    # Find the group with the highest priority (lowest number)
    best_group = None
    best_priority = float("inf")
    same_priority_count = 0

    for group in groups:
        if group.scope_type is None:
            # Global scope
            group_priority = global_priority
        else:
            scope_key = (group.scope_type.pk, group.scope_id)
            group_priority = scope_priority.get(scope_key, float("inf"))

        if group_priority < best_priority:
            best_priority = group_priority
            best_group = group
            same_priority_count = 1
        elif group_priority == best_priority:
            same_priority_count += 1

    # Only return a group if there's a single winner at the best priority level
    if same_priority_count == 1 and best_group is not None:
        return best_group

    return None


def ancestors(obj):
    """
    Get all ancestors of a hierarchical object (location, region, site group).

    Returns list including the object itself and all parents up to root.

    Args:
        obj (Location | Region | SiteGroup | None): The hierarchical object, or None.

    Returns:
        list: The object and all parents up to the root, or an empty list for None.

    """
    ancestors = []
    current = obj
    while current is not None:
        ancestors.append(current)
        current = getattr(current, "parent", None)
    return ancestors
