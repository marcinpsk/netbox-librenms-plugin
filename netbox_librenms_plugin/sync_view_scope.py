"""
Decide what one actor may see on the sync tabs and the sync page.

The source snapshots are shared by every user, so this module applies the actor's view scope after
the cache: assessment, the tab views, the verifies and the writers ask it, and tables and templates
render only the projections it returns. A NetBox object the actor cannot view leaves this module
only as :class:`Withheld`, with no pk and no name.
"""

from dataclasses import dataclass

from dcim.models import Device, Interface
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.utils import get_virtual_chassis_members


class ServerKeyRequired(Exception):
    """A binding-dependent operation ran on a scope that has no validated server key."""


@dataclass(frozen=True)
class Visible:
    """An object the actor may view."""

    obj: object


@dataclass(frozen=True)
class Withheld:
    """An object the actor may not view, or one that does not exist: the two look the same."""


WITHHELD = Withheld()


@dataclass(frozen=True)
class ChassisScope:
    """
    The members of the subject's virtual chassis, split by what the actor may view.

    ``attribution_members`` holds every member, for owner attribution and matching only: a hidden
    owner must stay an owner, or its rows would fall back to a visible member. Never render it.
    """

    attribution_members: tuple
    visible: tuple
    visible_ids: frozenset

    def owner(self, pk):
        """Return the member *pk* as :class:`Visible`, or :data:`WITHHELD`."""
        for member in self.visible:
            if member.pk == pk:
                return Visible(member)
        return WITHHELD


class SyncViewScope:
    """The view scope of one actor on one sync subject (a Device or a VirtualMachine)."""

    def __init__(self, actor, subject, server_key=None):
        if not isinstance(subject, (Device, VirtualMachine)):
            raise TypeError(f"A sync subject is a Device or a VirtualMachine, not {type(subject).__name__}.")
        self.actor = actor
        self.subject = subject
        self._server_key = server_key
        self._chassis = None

    @classmethod
    def for_request(cls, actor, subject, server_key=None):
        """Return the scope of *actor* on *subject*, which the caller resolved through its view scope."""
        return cls(actor, subject, server_key)

    @property
    def server_key(self):
        """Return the validated server key, for the operations that resolve LibreNMS bindings."""
        if self._server_key is None:
            raise ServerKeyRequired("This operation needs the validated LibreNMS server key.")
        return self._server_key

    @property
    def chassis(self):
        """Return the subject's :class:`ChassisScope`; a VM or a standalone device is its one member."""
        if self._chassis is None:
            if isinstance(self.subject, VirtualMachine):
                members = (self.subject,)
                visible = members
            else:
                members = tuple(get_virtual_chassis_members(self.subject))
                visible_ids = set(
                    Device.objects.restrict(self.actor, "view")
                    .filter(pk__in=[member.pk for member in members])
                    .values_list("pk", flat=True)
                )
                visible = tuple(member for member in members if member.pk in visible_ids)
            self._chassis = ChassisScope(
                attribution_members=members,
                visible=visible,
                visible_ids=frozenset(member.pk for member in visible),
            )
        return self._chassis

    def owner(self, model, pk):
        """
        Return the *model* object *pk* as :class:`Visible`, or :data:`WITHHELD` when the actor may not view it.

        The lookup stays in *model*: a Device pk never resolves a VirtualMachine.
        """
        if model not in (Device, VirtualMachine):
            raise TypeError(f"An owner is a Device or a VirtualMachine, not {getattr(model, '__name__', model)}.")
        if pk is None:
            return WITHHELD
        obj = model.objects.restrict(self.actor, "view").filter(pk=pk).first()
        return Visible(obj) if obj is not None else WITHHELD

    def interfaces(self):
        """
        Return the view-restricted interfaces on the visible members, for lists and indexes that are shown.

        Resolution of a LibreNMS port never uses this list: a port bound on a hidden interface must
        resolve to that interface and then be withheld, not fall back by name to a visible one.
        """
        if isinstance(self.subject, VirtualMachine):
            return list(VMInterface.objects.restrict(self.actor, "view").filter(virtual_machine=self.subject))
        return list(Interface.objects.restrict(self.actor, "view").filter(device__in=self.chassis.visible))
