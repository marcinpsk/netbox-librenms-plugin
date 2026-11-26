"""Move the LibreNMS mapping data of virtual chassis members to the chassis master."""

from functools import partial

from dcim.models import Device
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.views import View
from utilities.exceptions import PermissionsViolation

from netbox_librenms_plugin.mapping_owner import NO_MASTER_MESSAGE, OwnerProblem, chassis_ownership
from netbox_librenms_plugin.server_mappings import (
    MappingMoveRefused,
    lock_device_chassis,
    move_to_master,
    persist_mapping,
    persist_merge,
    prefetch_mappings,
    save_mapping_fields,
)
from netbox_librenms_plugin.transactions import classify_conflict, outside_change_scope, run_transaction, saved_in_scope
from netbox_librenms_plugin.utils import exception_text_for
from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin, NetBoxObjectPermissionMixin
from netbox_librenms_plugin.views.sync.mapping_lifecycle import posted_tab, sync_page_url

MOVE_PERMISSION_MESSAGE = (
    "You need change permission on the master and on each member that holds LibreNMS mapping data. Nothing was moved."
)
NOTHING_TO_MOVE_MESSAGE = "No LibreNMS mapping data is stored outside the master of this virtual chassis."


class _MoveRefused(Exception):
    """The locked chassis cannot move its mapping data; the message and its level are for the user."""

    def __init__(self, message, level=messages.ERROR):
        super().__init__(message)
        self.level = level


class MoveChassisMappingsView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """
    Move the LibreNMS mapping data of the members of a virtual chassis to its master.

    The move locks the chassis and then its members in pk order. It refuses the whole move when two
    values differ for one key, and it writes every row through the mapping writers in one
    transaction. A row that leaves the change scope rolls the move back with its NetBox events.
    """

    required_object_permissions = {"POST": [("change", Device)]}

    def post(self, request, pk):
        """Move the mapping data of the chassis of Device *pk*, and return to the sync page of *pk*."""
        if error := self.require_all_permissions("POST"):
            return error
        page = self.restrict_object_or_404(Device, pk=pk)
        try:
            run_transaction(partial(self._move, page.pk))
        except _MoveRefused as exc:
            messages.add_message(request, exc.level, str(exc))
        except MappingMoveRefused as exc:
            messages.error(request, f"{exc} Nothing was moved.")
        except PermissionsViolation as exc:
            messages.error(request, str(exc))
        except ValidationError as exc:
            if classify_conflict(exc):
                raise
            messages.error(request, f"The mapping data was not moved: {exception_text_for(exc, Device, request.user)}")
        else:
            messages.success(request, "Moved the LibreNMS mapping data of the chassis members to the master.")
        return redirect(sync_page_url(page, tab=posted_tab(request.POST)))

    def _move(self, pk):
        """Lock the chassis of *pk*, and move the mapping data of its members to the master."""
        chassis, members = lock_device_chassis(pk)
        if chassis is None:
            raise _MoveRefused("This device is not a member of a virtual chassis.")
        ownership = chassis_ownership(chassis.master_id, prefetch_mappings(members))
        if ownership.problem is OwnerProblem.NO_MASTER:
            raise _MoveRefused(NO_MASTER_MESSAGE)
        if ownership.problem is None:
            raise _MoveRefused(NOTHING_TO_MOVE_MESSAGE, messages.INFO)
        rows = (ownership.master, *ownership.holders)
        if self.restricted_queryset(Device, "change").filter(pk__in=[row.pk for row in rows]).count() != len(rows):
            raise _MoveRefused(MOVE_PERMISSION_MESSAGE)
        move = move_to_master(ownership.master, ownership.holders)

        def write():
            for row in rows:
                row.snapshot()
                persist_mapping(row, move.change_for(row), write=save_mapping_fields)
            # A change constraint can read the mapping, so a moved value can take a row out of the scope.
            for row in rows:
                if not saved_in_scope(row, self.request.user, "change"):
                    raise outside_change_scope(row)

        persist_merge(move, write=write)
