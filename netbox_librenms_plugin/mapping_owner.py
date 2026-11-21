"""
Decide which object holds the LibreNMS mappings of a Device or VirtualMachine.

A virtual machine, and a device outside a virtual chassis, holds its own mappings. A virtual
chassis has one owner: its master. Every server mapping of the chassis lives on the master: the
own and OOB IDs of each server, a legacy bare value and the preferred server. A chassis without a
master has no owner. A chassis in which another member holds mapping data has no owner until
that data moves to the master. Every reader and writer asks this module for the owner,
so no caller decides it again.
"""

from dataclasses import dataclass
from enum import StrEnum

from netbox_librenms_plugin.server_mappings import prefetch_mappings, read_mapping


class OwnerProblem(StrEnum):
    """Why a virtual chassis has no mapping owner."""

    NO_MASTER = "no_master"
    MISPLACED = "misplaced"


NO_MASTER_MESSAGE = (
    "This virtual chassis has no master. Set the master of the virtual chassis: "
    "the master holds the LibreNMS mappings of the chassis."
)
MISPLACED_MESSAGE = (
    "A member of this virtual chassis that is not the master holds LibreNMS mapping data. "
    "Move the data to the master on the LibreNMS sync page first."
)
NOT_OWNER_MESSAGE = "The master of this virtual chassis holds the LibreNMS mappings of the chassis. Use the master."
_PROBLEM_MESSAGES = {OwnerProblem.NO_MASTER: NO_MASTER_MESSAGE, OwnerProblem.MISPLACED: MISPLACED_MESSAGE}


@dataclass(frozen=True)
class MappingOwnership:
    """
    The object that holds the mappings of one Device or VM, or why its virtual chassis has none.

    ``owner`` is None when ``problem`` is set. ``master`` is the chassis master, when the chassis
    has one. ``holders`` are the members that are not the master and hold mapping data, in pk order.
    """

    owner: object | None
    problem: OwnerProblem | None = None
    master: object | None = None
    holders: tuple = ()

    def require(self):
        """
        Return the owner.

        Raises:
            MappingOwnerError: The virtual chassis has no owner.

        """
        if self.problem is not None:
            raise MappingOwnerError(self)
        return self.owner


class MappingOwnerError(Exception):
    """
    A request needs the mapping owner of a virtual chassis, and the chassis has none or the object is not the owner.

    The text names no object, so every answer can show it.
    """

    def __init__(self, ownership, message=None):
        self.ownership = ownership
        super().__init__(message or _PROBLEM_MESSAGES[ownership.problem])


def chassis_ownership(master_id, members) -> MappingOwnership:
    """Return the ownership of one chassis from its master pk and its prefetched *members* in pk order."""
    master = next((member for member in members if member.pk == master_id), None)
    if master is None:
        return MappingOwnership(owner=None, problem=OwnerProblem.NO_MASTER)
    # nosemgrep: mapping-read-loop-needs-prefetch  # noqa: ERA001  # each caller prefetches members
    holders = tuple(member for member in members if member.pk != master.pk and read_mapping(member).has_recorded_state)
    if holders:
        return MappingOwnership(owner=None, problem=OwnerProblem.MISPLACED, master=master, holders=holders)
    return MappingOwnership(owner=master, master=master)


def mapping_ownership(obj) -> MappingOwnership:
    """Return the mapping owner of the Device or VirtualMachine *obj*, read from the database, or why it has none."""
    from dcim.models import Device, VirtualChassis

    chassis_id = getattr(obj, "virtual_chassis_id", None)
    if chassis_id is None:
        return MappingOwnership(owner=obj)
    master_id = VirtualChassis.objects.filter(pk=chassis_id).values_list("master_id", flat=True).first()
    members = prefetch_mappings(
        Device.objects.filter(virtual_chassis_id=chassis_id).select_related("primary_ip4", "primary_ip6").order_by("pk")
    )
    ownership = chassis_ownership(master_id, members)
    if ownership.owner is not None and ownership.owner.pk == obj.pk:
        # The caller's instance stays the owner, so a later read or write of the owner reaches it.
        return MappingOwnership(owner=obj, master=obj)
    return ownership


def chassis_ownerships(devices) -> dict:
    """
    Return the ownership of each virtual chassis that *devices* belong to, keyed by chassis ID.

    A list of many rows uses this instead of one :func:`mapping_ownership` call per row: one query
    loads the members of every chassis. A device outside a chassis is skipped.
    """
    from dcim.models import Device, VirtualChassis

    chassis_ids = {device.virtual_chassis_id for device in devices if device.virtual_chassis_id is not None}
    if not chassis_ids:
        return {}
    masters = dict(VirtualChassis.objects.filter(pk__in=chassis_ids).values_list("pk", "master_id"))
    members_by_chassis = {chassis_id: [] for chassis_id in masters}
    members = Device.objects.filter(virtual_chassis_id__in=masters).select_related("primary_ip4", "primary_ip6")
    for member in prefetch_mappings(members.order_by("pk")):
        members_by_chassis[member.virtual_chassis_id].append(member)
    return {
        chassis_id: chassis_ownership(masters[chassis_id], chassis_members)
        for chassis_id, chassis_members in members_by_chassis.items()
    }


def mapping_owner(obj):
    """
    Return the object that holds the mappings of *obj*: *obj* itself, or the master of its virtual chassis.

    Raises:
        MappingOwnerError: The virtual chassis has no owner.

    """
    return mapping_ownership(obj).require()


def ensure_mapping_owner(obj):
    """
    Return *obj* when it holds its own mappings: it is outside a virtual chassis, or it is the chassis owner.

    Raises:
        MappingOwnerError: The virtual chassis has no owner, or another member is the owner.

    """
    ownership = mapping_ownership(obj)
    if ownership.require().pk != obj.pk:
        raise MappingOwnerError(ownership, NOT_OWNER_MESSAGE)
    return obj


def lock_owner_chassis(model, pk):
    """
    Lock the virtual chassis of Device *pk* for share before the caller locks the row; return the chassis id.

    A master change then waits for the commit, so :func:`ensure_locked_mapping_owner` reads an owner
    that holds until the commit. A VirtualMachine has no chassis: the call returns None.
    """
    from dcim.models import Device

    from netbox_librenms_plugin.serial_identity import lock_chassis_of

    return lock_chassis_of(pk) if model is Device else None


def ensure_locked_mapping_owner(row, chassis_id):
    """
    Return the locked *row* when it holds its own mappings; *chassis_id* is what :func:`lock_owner_chassis` returned.

    Raises:
        ChassisChanged: The row moved to another chassis before its lock, so the chassis lock does not cover it.
        MappingOwnerError: The virtual chassis has no owner, or another member is the owner.

    """
    from netbox_librenms_plugin.server_mappings import CHASSIS_CHANGED_MESSAGE, ChassisChanged
    from netbox_librenms_plugin.transactions import recorded_conflict

    if getattr(row, "virtual_chassis_id", None) != chassis_id:
        raise recorded_conflict(ChassisChanged(CHASSIS_CHANGED_MESSAGE))
    return ensure_mapping_owner(row)
