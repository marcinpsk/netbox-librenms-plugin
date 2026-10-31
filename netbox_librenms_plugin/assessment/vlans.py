"""Compare VLAN source facts with the NetBox VLANs visible to the caller."""

from dataclasses import dataclass
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.constants import LIBRENMS_VLAN_STATE_ACTIVE
from netbox_librenms_plugin.utils import index_vlan_source_rows, normalize_vlan_vid, vlan_source_namer


@dataclass(frozen=True)
class VLANAssessment:
    """Compared rows and the source identities used by a writer."""

    rows: list
    source_by_vid: dict
    invalid_rows: list


def assess(snapshot, *, subject, lookup_maps):
    """Assess VLAN rows without HTTP, cache access or writes."""
    source = snapshot.value(Resource.VLANS, "vlans")
    indexed, invalid = index_vlan_source_rows(source)
    return VLANAssessment(compare_rows(source, lookup_maps=lookup_maps, subject=subject), indexed, invalid)


def compare_vlan(netbox_vlan, name):
    """Return the existence and name comparison used by the tab and verify endpoint."""
    return {
        "exists_in_netbox": netbox_vlan is not None,
        "netbox_vlan_name": netbox_vlan.name if netbox_vlan is not None else None,
        "name_matches": netbox_vlan.name == name if netbox_vlan is not None else False,
    }


def compare_rows(librenms_vlans, *, lookup_maps, subject):
    """
    Compare LibreNMS VLANs against NetBox VLANs available to the device.

    Args:
        librenms_vlans: List of VLAN dicts from LibreNMS
        lookup_maps: Dict with vid_to_groups, vid_group_to_vlan, vid_to_vlans
        subject: NetBox Device object for scope-based prioritization

    Adds comparison flags:
    - exists_in_netbox: bool
    - netbox_vlan: VLAN object or None
    - netbox_vlan_group: VLANGroup name or None
    - name_matches: bool
    - auto_selected_group_id: ID of auto-selected group or None
    - auto_selected_group_name: Name of auto-selected group or None
    - is_ambiguous: bool - True if VID exists in multiple groups with no clear priority

    """
    lookup_maps = lookup_maps or {}
    vid_to_groups = lookup_maps.get("vid_to_groups", {})
    vid_to_vlans = lookup_maps.get("vid_to_vlans", {})

    rows_by_vid, _invalid_rows = index_vlan_source_rows(librenms_vlans)
    source_name = vlan_source_namer(subject)
    compared = []
    for vlan in librenms_vlans:
        raw_vid = vlan.get("vlan_vlan")
        canonical_vid = normalize_vlan_vid(raw_vid)
        if canonical_vid is None:
            vid_error = "Invalid VID"
        elif canonical_vid not in rows_by_vid:
            vid_error = "Duplicate VID"
        else:
            vid_error = None
        vid = canonical_vid if vid_error is None else None
        name = source_name(vlan, vid)

        # Auto-selection logic for VLAN group dropdown
        auto_selected_group_id = None
        auto_selected_group_name = None
        is_ambiguous = False
        netbox_vlan = None

        # Check if VID exists in groups for auto-selection
        if vid in vid_to_groups:
            groups = vid_to_groups[vid]
            if len(groups) == 1:
                auto_selected_group_id = groups[0].pk
                auto_selected_group_name = groups[0].name
                # Get the VLAN from this single group
                vlans_for_vid = vid_to_vlans.get(vid, [])
                if vlans_for_vid:
                    netbox_vlan = vlans_for_vid[0]
            elif len(groups) > 1:
                # Try to select the most specific group based on device context
                most_specific = select_most_specific_group(groups, subject)
                if most_specific:
                    auto_selected_group_id = most_specific.pk
                    auto_selected_group_name = most_specific.name
                    # Get the VLAN from the most specific group
                    vlans_for_vid = vid_to_vlans.get(vid, [])
                    for v in vlans_for_vid:
                        if v.group and v.group.pk == most_specific.pk:
                            netbox_vlan = v
                            break
                else:
                    is_ambiguous = True
        else:
            # Check if it exists as a global VLAN (no group)
            vlans_for_vid = vid_to_vlans.get(vid, [])
            for v in vlans_for_vid:
                if v.group is None:
                    netbox_vlan = v
                    break

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
                "auto_selected_group_id": auto_selected_group_id,
                "auto_selected_group_name": auto_selected_group_name,
                "is_ambiguous": is_ambiguous,
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
