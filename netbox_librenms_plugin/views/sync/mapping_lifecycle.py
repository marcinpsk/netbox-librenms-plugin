"""Apply a confirmed link of a NetBox device or VM to the LibreNMS device that it matches."""

from functools import partial
from urllib.parse import urlencode, urlsplit, urlunsplit

from dcim.models import Device, VirtualChassis
from django.apps import apps
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from ipam.models import IPAddress
from utilities.exceptions import PermissionsViolation
from virtualization.models import VirtualMachine

from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin.assessment.mapping_lifecycle import Link, build_link
from netbox_librenms_plugin.import_utils.disclosure import visible_object_label
from netbox_librenms_plugin.ip_addressing import primary_ips
from netbox_librenms_plugin.librenms_api import LibreNMSAPI
from netbox_librenms_plugin.matching import LookupInputs
from netbox_librenms_plugin.server_selection import ServerSelectionState, resolve_object_server
from netbox_librenms_plugin.server_mappings import (
    AmbiguousLibreNMSIdError,
    IdentityOwned,
    assign_own,
    lock_librenms_id_assignment,
    persist_mapping,
    save_mapping_fields,
)
from netbox_librenms_plugin.sync_cache import SyncTab
from netbox_librenms_plugin.transactions import classify_conflict, row_savepoint, run_transaction, saved_in_scope
from netbox_librenms_plugin.utils import exception_text_for
from netbox_librenms_plugin.views.mixins import (
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    validated_referer,
)

_ENTRIES = frozenset({(intent.Resource.MAPPING_LIFECYCLE, Link.ACTION, intent.Role.PRIMARY)})
LINK_POLICY = intent.RoutePolicy(_ENTRIES)
BULK_LINK_POLICY = intent.RoutePolicy(_ENTRIES, subject_per_token=True)
_MODELS = {"device": Device, "virtualmachine": VirtualMachine}
_STATUS_LISTS = {"device": "device_status_list", "virtualmachine": "vm_status_list"}
_SYNC_PAGES = {"device": "device_librenms_sync", "virtualmachine": "vm_librenms_sync"}
_INVALID_CONFIRMATION = "The link confirmation is invalid or has expired. Refresh the page and try again."


def _lock_chassis_members(pk):
    """
    Lock the virtual chassis of Device *pk* and its members in pk order, the order of the interface writers.

    The link rebuild then reads the chassis sync device while no member can change its mapping.
    Returns the chassis ID that the locks cover, None for none: the caller compares it with the
    chassis of the locked row, because the device can join or leave a chassis before that lock.
    """
    chassis_id = Device.objects.filter(pk=pk).values_list("virtual_chassis_id", flat=True).first()
    if chassis_id is not None:
        VirtualChassis.objects.select_for_update().filter(pk=chassis_id).order_by().first()
        list(Device.objects.select_for_update(of=("self",)).filter(virtual_chassis_id=chassis_id).order_by("pk"))
    return chassis_id


class _LinkWriter(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Verify the submitted link tokens under the locks, and store each verified own ID."""

    required_object_permissions = {"POST": [("change", Device), ("change", VirtualMachine)]}

    def _model(self, object_type):
        model = _MODELS.get(object_type)
        if model is None:
            raise Http404("Invalid object type.")
        # Only the model that the route names is changed, so only its permission is required.
        self.required_object_permissions = {"POST": [("change", model)]}
        return model

    def _rebuild(self, approved, *, model, server_key, locked):
        """Claim each approved ID, lock the object and its primary IPs, and rebuild each link from that state."""
        changeable = self.restricted_queryset(model, "change")
        pairs = []
        for operation in sorted(approved, key=lambda operation: operation.identity.pk):
            identity = operation.identity
            if identity.model != model._meta.label_lower or identity.server != server_key:
                pairs.append((operation, intent.Unavailable("subject.available")))
                continue
            owned = False
            locked_chassis = _lock_chassis_members(identity.pk) if model is Device else None
            try:
                row, conflict = lock_librenms_id_assignment(
                    operation.effects.own_id, server_key, owner_queryset=changeable, owner_pk=identity.pk
                )
                owned = conflict is not None
            except AmbiguousLibreNMSIdError:
                # The claim locked the row before the owner search found several owners.
                row, owned = changeable.filter(pk=identity.pk).select_for_update(of=("self",)).first(), True
            except model.DoesNotExist:
                row = None
            if row is None:
                pairs.append((operation, intent.Unavailable("subject.available")))
                continue
            if getattr(row, "virtual_chassis_id", None) != locked_chassis:
                # The device joined or left a chassis after the read that chose the chassis locks.
                pairs.append((operation, intent.Unavailable("assumptions.chassis")))
                continue
            # Each address is a lookup input of the locked row, and the row's change scope admitted it.
            relocked = (self.relock_scoped_row(IPAddress, pk=ip.pk) for ip in primary_ips(row))
            locked[identity.pk] = row
            inputs = LookupInputs.of(row, tuple(ip for ip in relocked if ip is not None))
            pairs.append(
                (operation, build_link(row, server_key, choices=operation.choices, inputs=inputs, owned=owned))
            )
        return pairs

    def _label(self, identity):
        """Return the name of the object that a token names, for an outcome line, when the user may view it."""
        obj = self.restricted_queryset(apps.get_model(identity.model)).filter(pk=identity.pk).first()
        return f"'{obj.name}'" if obj is not None else "An object outside your view scope"

    def _stale_lines(self, result):
        if not result.members:
            return [(messages.ERROR, _INVALID_CONFIRMATION)]
        changed = ", ".join(result.paths)
        return [
            (
                messages.ERROR,
                f"{self._label(operation.identity)}: the confirmed link is no longer current ({changed}). "
                "Refresh the page and try again.",
            )
            for operation in result.members
        ]

    def _write(self, request, operation, row, server_name):
        """Store the verified own ID on the locked row in one savepoint, and return its outcome line."""
        own_id = operation.effects.own_id
        try:
            with row_savepoint():
                row.snapshot()
                persist_mapping(row, assign_own(row, operation.identity.server, own_id), write=save_mapping_fields)
                # A change constraint can read the mapping, so the stored link can move the row out of the scope.
                if not saved_in_scope(row, request.user, "change"):
                    raise PermissionsViolation()
        except PermissionsViolation:
            return messages.ERROR, f"'{row.name}': the linked object is outside your change permission; not linked."
        except IdentityOwned as exc:
            visible = visible_object_label(exc.owner, request.user)
            owner = f"{visible[0]} '{visible[1]}'" if visible else "another object outside your view scope"
            return messages.ERROR, f"'{row.name}': LibreNMS ID {own_id} is already linked to {owner}."
        except ValidationError as exc:
            if classify_conflict(exc):
                raise
            return (
                messages.ERROR,
                f"'{row.name}': the link was not saved: {exception_text_for(exc, type(row), request.user)}",
            )
        return messages.SUCCESS, f"Linked '{row.name}' to LibreNMS ID {own_id} on {server_name}."

    def _apply(self, request, tokens, *, policy, context, model, server_key):
        """Verify and write in one transaction, which a lock conflict runs once more; then report each outcome."""
        server_name = LibreNMSAPI.get_available_servers().get(server_key, server_key)

        def attempt():
            locked = {}
            verification = intent.verify(
                tokens,
                policy=policy,
                context=context,
                rebuild=partial(self._rebuild, model=model, server_key=server_key, locked=locked),
            )
            lines = []
            for result in verification.results:
                if isinstance(result, intent.Stale):
                    lines.extend(self._stale_lines(result))
                    continue
                for member in result.members:
                    row = locked[member.operation.identity.pk]
                    lines.append(self._write(request, member.operation, row, server_name))
            return lines

        for level, text in run_transaction(attempt):
            messages.add_message(request, level, text)


class MappingLifecycleView(_LinkWriter):
    """Link one device or VM, on its sync page, to the LibreNMS device that the page proposed."""

    confirmation_policy = LINK_POLICY

    def post(self, request, object_type, pk):
        """Apply the one submitted link token of the object, and return to its sync page."""
        model = self._model(object_type)
        if error := self.require_all_permissions("POST"):
            return error
        obj = self.restrict_object_or_404(model, "change", pk=pk)
        server_key = self.rebind_api_for_posted_server(request.POST)
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return self._redirect(object_type, pk, None)
        self._apply(
            request,
            request.POST.getlist(intent.FIELD),
            policy=LINK_POLICY,
            context=intent.IntentContext(request.user.pk, (obj._meta.label_lower, obj.pk), server_key),
            model=model,
            server_key=server_key,
        )
        return self._redirect(object_type, pk, self._page_server_key(model, pk, server_key))

    def _page_server_key(self, model, pk, server_key):
        """Return *server_key* when the sync page accepts it for the object; else the page resolves its own server."""
        obj = self.restricted_queryset(model).filter(pk=pk).first()
        if obj is None or resolve_object_server(obj, server_key).state is ServerSelectionState.INVALID:
            return None
        return server_key

    def _redirect(self, object_type, pk, server_key):
        query = {key: value for key, value in (("server_key", server_key), ("tab", self._tab())) if value}
        url = reverse(f"plugins:netbox_librenms_plugin:{_SYNC_PAGES[object_type]}", kwargs={"pk": pk})
        return redirect(f"{url}?{urlencode(query)}" if query else url)

    def _tab(self):
        tab = self.request.POST.get("tab")
        return tab if tab in {sync_tab.value for sync_tab in SyncTab} else None


class BulkLinkView(_LinkWriter):
    """Link the selected rows of a status list; each token names its own device or VM."""

    confirmation_policy = BULK_LINK_POLICY

    def post(self, request, object_type):
        """Apply every submitted link token whose object the user may change, and return to the list."""
        model = self._model(object_type)
        if error := self.require_all_permissions("POST"):
            return error
        server_key = self.rebind_api_for_posted_server(request.POST)
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return self._redirect(object_type)
        tokens = request.POST.getlist(intent.FIELD)
        if not any(tokens):
            messages.error(request, "Select at least one object to link.")
            return self._redirect(object_type)
        # A signed identity only narrows the scope query; verify compares each token's own subject with it.
        named = {operation.identity.pk for operation in intent.signed_operations(tokens, policy=BULK_LINK_POLICY)}
        label = model._meta.label_lower
        changeable = self.restricted_queryset(model, "change").filter(pk__in=named).values_list("pk", flat=True)
        self._apply(
            request,
            tokens,
            policy=BULK_LINK_POLICY,
            context=intent.IntentScope(request.user.pk, server_key, frozenset((label, pk) for pk in changeable)),
            model=model,
            server_key=server_key,
        )
        return self._redirect(object_type)

    def _redirect(self, object_type):
        """Return to the list page the form was posted from: htmx paging changes the page after it rendered the form."""
        url = reverse(f"plugins:netbox_librenms_plugin:{_STATUS_LISTS[object_type]}")
        referer = validated_referer(self.request)
        if referer is None or urlsplit(referer).path != url:
            return redirect(url)
        return redirect(urlunsplit(("", "", url, urlsplit(referer).query, "")))
