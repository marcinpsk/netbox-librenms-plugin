"""Database resolution and port owners shared by cable acquisition and writes."""

from dcim.models import CableTermination, Device, Interface
from django.contrib.contenttypes.models import ContentType

from netbox_librenms_plugin.breakout_cables import is_channel
from netbox_librenms_plugin.librenms_ids import coerce_librenms_id
from netbox_librenms_plugin.server_mappings import MappingRole, identity_q, read_mapping
from netbox_librenms_plugin.server_selection import get_librenms_sync_device


def remote_name_candidates(hostname):
    """Return the trimmed full name and its short-name fallback."""
    if not isinstance(hostname, str) or not (hostname := hostname.strip()):
        return ()
    return hostname, hostname.split(".")[0]


def resolve_remote_device(remote_device_id, hostname, *, server_key, queryset=None, catalog=None):
    """Resolve an id, full name or short name; a hidden or ambiguous match stops resolution."""
    candidates = []
    if (remote_device_id := coerce_librenms_id(remote_device_id)) is not None:
        candidates.append(
            ("id", remote_device_id, f"Multiple devices found with the same LibreNMS ID: {remote_device_id}.")
        )
    candidates.extend(
        ("name", name, f"Multiple devices found with the same name: {hostname}.")
        for name in remote_name_candidates(hostname)
    )
    for kind, value, ambiguous in candidates:
        if catalog is None:
            if kind == "id":
                matches = Device.objects.filter(
                    identity_q(Device, server=server_key, identities=(value,), roles=(MappingRole.OWN,))
                )
            else:
                matches = Device.objects.filter(name__iexact=value)
            matches = list(matches.order_by("pk")[:2])
        else:
            by_name, by_id, _visible_ids = catalog
            matches = by_id.get(value, []) if kind == "id" else by_name.get(value.lower(), [])
        if len(matches) > 1:
            return None, False, ambiguous
        if not matches:
            continue
        device = matches[0]
        if catalog is not None and device.pk not in catalog[2]:
            return None, False, None
        if queryset is not None and not queryset.filter(pk=device.pk).exists():
            return None, False, None
        return device, True, None
    return None, False, None


def displaced_interfaces(*terminations):
    """Return interfaces a replacement detaches, keyed by model and id with their owner ids."""
    cable_ids = {term.cable_id for term in terminations if term.cable_id is not None and not is_channel(term)}
    if not cable_ids:
        return {}
    endpoints = {(type(term), term.pk) for term in terminations}
    far_ids = CableTermination.objects.filter(
        cable_id__in=cable_ids, termination_type=ContentType.objects.get_for_model(Interface)
    ).values_list("termination_id", flat=True)
    return {
        (Interface, pk): device_id
        for pk, device_id in Interface.objects.filter(pk__in=far_ids).values_list("pk", "device_id")
        if (Interface, pk) not in endpoints
    }


def cable_rule_interfaces(*terminations):
    """Return the interfaces a cable write can attach or detach before it takes locks."""
    interfaces = {term.pk: term for term in terminations if isinstance(term, Interface)}
    interfaces.update(
        (interface.pk, interface)
        for interface in Interface.objects.filter(pk__in=[pk for _model, pk in displaced_interfaces(*terminations)])
    )
    return tuple(interfaces.values())


def cable_port_owner_ids(*terminations, server_key):
    """Return each host, chassis or controller identity whose ports a cable write can need."""
    owner_ids = {interface.device_id for interface in cable_rule_interfaces(*terminations)}
    identities = set()
    for owner in Device.objects.filter(pk__in=owner_ids).order_by("pk"):
        source_owner = get_librenms_sync_device(owner, server_key=server_key) or owner
        for candidate in (owner, source_owner):
            mapping = read_mapping(candidate)
            identities.update(
                device_id
                for device_id in (mapping.own_id(server_key), mapping.oob_id(server_key))
                if device_id is not None
            )
    return tuple(sorted(identities))
