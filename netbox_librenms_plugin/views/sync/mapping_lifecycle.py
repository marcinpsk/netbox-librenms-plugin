"""Apply the confirmed mapping lifecycle operations of a NetBox device or VM: link, unlink, relink and change_oob."""

from functools import partial
from urllib.parse import urlencode, urlsplit, urlunsplit

from dcim.models import Device
from django.apps import apps
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from ipam.models import IPAddress
from utilities.exceptions import PermissionsViolation
from virtualization.models import VirtualMachine

from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin.assessment.mapping_lifecycle import (
    HOST_CHANGES,
    HostChangeState,
    Link,
    Relink,
    Unlink,
    UnlinkChoices,
    build_host_change,
    build_link,
    read_host_change_assessment,
    scope_permitted,
)
from netbox_librenms_plugin.import_utils.disclosure import visible_object_label
from netbox_librenms_plugin.ip_addressing import primary_ips
from netbox_librenms_plugin.librenms_api import LibreNMSAPI
from netbox_librenms_plugin.matching import LookupInputs
from netbox_librenms_plugin.server_mappings import (
    AmbiguousLibreNMSIdError,
    IdentityOwned,
    assign_own,
    edit_server,
    host_entry,
    lock_host_change_owner,
    lock_librenms_id_assignment,
    persist_mapping,
    read_mapping,
    release_server,
    ServerState,
    require_server_key,
    save_mapping_fields,
)
from netbox_librenms_plugin.sync_cache import SyncTab
from netbox_librenms_plugin.transactions import (
    classify_conflict,
    row_savepoint,
    run_transaction,
    saved_all_in_scope,
    saved_in_scope,
)
from netbox_librenms_plugin.utils import exception_text_for
from netbox_librenms_plugin.views.mixins import (
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    validated_referer,
)

_LINK_ENTRIES = frozenset({(intent.Resource.MAPPING_LIFECYCLE, Link.ACTION, intent.Role.PRIMARY)})
LIFECYCLE_POLICY = intent.RoutePolicy(
    _LINK_ENTRIES | {(intent.Resource.MAPPING_LIFECYCLE, action, intent.Role.PRIMARY) for action in HOST_CHANGES}
)
BULK_LINK_POLICY = intent.RoutePolicy(_LINK_ENTRIES, subject_per_token=True)
_MODELS = {"device": Device, "virtualmachine": VirtualMachine}
_STATUS_LISTS = {"device": "device_status_list", "virtualmachine": "vm_status_list"}
_SYNC_PAGES = {"device": "device_librenms_sync", "virtualmachine": "vm_librenms_sync"}
_INVALID_CONFIRMATION = "The confirmation is invalid or has expired. Refresh the page and try again."
# The noun of each operation in an outcome line.
_NOUNS = {"link": "link", "unlink": "unlink", "relink": "LibreNMS ID change", "change_oob": "OOB controller change"}


def _server_name(server_key):
    return LibreNMSAPI.get_available_servers().get(server_key, server_key)


def _owner_text(owner, user):
    visible = visible_object_label(owner, user) if owner is not None else None
    return f"{visible[0]} '{visible[1]}'" if visible else "another object outside your view scope"


def sync_page_url(obj, *, server_key=None, tab=None):
    """Return the sync page URL of *obj*, with the server and the tab when they are given."""
    url = reverse(f"plugins:netbox_librenms_plugin:{_SYNC_PAGES[obj._meta.model_name]}", kwargs={"pk": obj.pk})
    query = {key: value for key, value in (("server_key", server_key), ("tab", tab)) if value}
    return f"{url}?{urlencode(query)}" if query else url


def _refused_interfaces_text(refused, user):
    """Name the refused interfaces that *user* may view, and count the others."""
    model = type(refused[0])
    visible = set(
        model.objects.restrict(user, "view").filter(pk__in=[row.pk for row in refused]).values_list("pk", flat=True)
    )
    names = [f"{row.parent_object.name} {row.name}" for row in refused if row.pk in visible]
    if hidden := len(refused) - len(names):
        names.append(f"{hidden} outside your view scope")
    return ", ".join(names)


def _refusal_text(assessment, obj, user):
    """Return the message of a host change that the assessment did not offer."""
    state = assessment.state
    server = f"'{_server_name(assessment.server_key)}'"
    if state is HostChangeState.REFUSED:
        return "The stored LibreNMS mapping has a legacy or invalid format. Convert or repair it first."
    if state is HostChangeState.MIGRATED:
        return f"'{obj.name}' was merged into another device on {server}; the merge owns this mapping."
    if state is HostChangeState.UNCHANGED:
        return f"The change removes or replaces no LibreNMS ID of '{obj.name}' on {server}."
    if state is HostChangeState.OWNED:
        claimed = HOST_CHANGES[assessment.action].claimed_id(assessment.choices)
        return f"LibreNMS ID {claimed} is already assigned to {_owner_text(assessment.owner, user)}."
    if state is HostChangeState.INTERFACES:
        refused = assessment.scope.refused
        return (
            f"{len(refused)} interfaces in the scope have a legacy or invalid LibreNMS port value "
            f"({_refused_interfaces_text(refused, user)}). Convert or repair them first."
        )
    return "Your permissions do not cover every object and interface that this change clears. Nothing was changed."


def render_host_change(request, obj, server_key, action, choices, *, tab=None):
    """
    Render the confirmation of a host change of *obj*, or return to its sync page with the refusal.

    The confirmation names the server, the IDs that the change removes or replaces, and each
    interface whose port ID on the server it clears. It writes nothing.
    """
    assessment = read_host_change_assessment(
        obj, server_key, action, choices, user=request.user, context=intent.context_for(request.user, obj, server_key)
    )
    if assessment.state is not HostChangeState.OFFERED or assessment.offer is None:
        messages.error(request, _refusal_text(assessment, obj, request.user))
        return redirect(sync_page_url(obj, tab=tab))
    entry = assessment.entry
    return render(
        request,
        "netbox_librenms_plugin/mapping_lifecycle_confirm.html",
        {
            "object": obj,
            "action": action,
            "server_key": server_key,
            "server_name": _server_name(server_key),
            "before": entry,
            # An unlink leaves no entry: both IDs show as removed.
            "after": HOST_CHANGES[action].target(entry, choices) or ServerState(server_key),
            "interfaces": [
                {"name": row.interface.name, "parent": row.interface.parent_object.name}
                for row in assessment.scope.interfaces
            ],
            "members": [member.name for member in assessment.scope.members],
            "token": assessment.offer.token,
            "action_url": reverse(
                "plugins:netbox_librenms_plugin:mapping_lifecycle",
                kwargs={"object_type": obj._meta.model_name, "pk": obj.pk},
            ),
            "cancel_url": sync_page_url(obj, tab=tab),
            "tab": tab or "",
        },
    )


class _LifecycleWriter(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Verify the submitted lifecycle tokens under the locks, and write each verified operation."""

    required_object_permissions = {"POST": [("change", Device), ("change", VirtualMachine)]}

    def _model(self, object_type):
        model = _MODELS.get(object_type)
        if model is None:
            raise Http404("Invalid object type.")
        # Only the model that the route names is changed, so only its permission is required.
        self.required_object_permissions = {method: [("change", model)] for method in ("GET", "POST")}
        return model

    def _rebuild(self, approved, *, model, server_key, locked):
        """Lock and rebuild each approved operation from current state; a link and a host change lock differently."""
        changeable = self.restricted_queryset(model, "change")
        pairs = []
        for operation in sorted(approved, key=lambda operation: operation.identity.pk):
            identity = operation.identity
            if identity.model != model._meta.label_lower or identity.server != server_key:
                rebuilt = intent.Unavailable("subject.available")
            elif isinstance(operation, Link):
                rebuilt = self._rebuild_link(operation, changeable, model, server_key, locked)
            else:
                rebuilt = self._rebuild_host_change(operation, changeable, model, server_key, locked)
            pairs.append((operation, rebuilt))
        return pairs

    def _rebuild_link(self, operation, changeable, model, server_key, locked):
        """Claim the approved ID, lock the object and its primary IPs, and rebuild the link from that state."""
        identity = operation.identity
        if server_key not in LibreNMSAPI.get_available_servers():
            return intent.Unavailable("server.configured")
        owned = False
        # The chassis and its members first, so the sync device that the link binds cannot change meanwhile.
        lock_host_change_owner(model, identity.pk)
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
        # The outcome line names the object, so it must still be in the view scope.
        if row is None or not self.restricted_queryset(model).filter(pk=row.pk).exists():
            return intent.Unavailable("subject.available")
        # Each address is a lookup input of the locked row, and the row's change scope admitted it.
        relocked = (self.relock_scoped_row(IPAddress, pk=ip.pk) for ip in primary_ips(row))
        locked[identity.pk] = row
        inputs = LookupInputs.of(row, tuple(ip for ip in relocked if ip is not None))
        return build_link(row, server_key, choices=operation.choices, inputs=inputs, owned=owned)

    def _rebuild_host_change(self, operation, changeable, model, server_key, locked):
        """Lock the chassis, its members and the scope's interfaces, claim a new ID, and rebuild the change."""
        cls = type(operation)
        row = lock_host_change_owner(model, operation.identity.pk)
        if row is None or not changeable.filter(pk=row.pk).exists():
            return intent.Unavailable("subject.available")
        owned = False
        if (claimed := cls.claimed_id(operation.choices)) is not None:
            try:
                _row, conflict = lock_librenms_id_assignment(
                    claimed, server_key, owner_queryset=model.objects.all(), owner_pk=row.pk
                )
                owned = conflict is not None
            except AmbiguousLibreNMSIdError:
                owned = True
        rebuilt, scope = build_host_change(cls, row, server_key, operation.choices, owned=owned, lock=True)
        if not scope_permitted(scope, self.request.user):
            return intent.Unavailable("scope.permission")
        locked[row.pk] = (row, scope)
        return rebuilt

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
                f"{self._label(operation.identity)}: the confirmed {_NOUNS[operation.ACTION]} is no longer current "
                f"({changed}). Refresh the page and try again.",
            )
            for operation in result.members
        ]

    def _write(self, request, operation, locked, server_name):
        """Write one verified operation on its locked rows in one savepoint, and return its outcome line."""
        if isinstance(operation, Link):
            return self._write_link(request, operation, locked, server_name)
        row, scope = locked
        noun = _NOUNS[operation.ACTION]
        try:
            with row_savepoint():
                row.snapshot()
                persist_mapping(row, _entry_change(operation, row), write=save_mapping_fields)
                interfaces = [cascade.interface for cascade in scope.interfaces]
                for interface in interfaces:
                    interface.snapshot()
                    change = release_server(interface, operation.identity.server, clear_preference=False)
                    persist_mapping(interface, change, write=save_mapping_fields)
                # A change constraint can read the mapping, so a cleared entry can move a row out of the scope.
                if not saved_in_scope(row, request.user, "change") or not saved_all_in_scope(
                    interfaces, request.user, "change"
                ):
                    raise PermissionsViolation()
        except PermissionsViolation:
            return messages.ERROR, f"'{row.name}': a changed object is outside your change permission; not changed."
        except IdentityOwned as exc:
            return (
                messages.ERROR,
                f"'{row.name}': LibreNMS ID {exc.identity} is already linked to {_owner_text(exc.owner, request.user)}.",
            )
        except AmbiguousLibreNMSIdError:
            return messages.ERROR, f"'{row.name}': the new LibreNMS ID matches more than one object; not changed."
        except ValidationError as exc:
            if classify_conflict(exc):
                raise
            return (
                messages.ERROR,
                f"'{row.name}': the {noun} was not saved: {exception_text_for(exc, type(row), request.user)}",
            )
        except ValueError as exc:
            return messages.ERROR, f"'{row.name}': the {noun} was not saved: {exc}"
        return messages.SUCCESS, _success_text(operation, row, len(scope.interfaces), server_name)

    def _write_link(self, request, operation, row, server_name):
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
            return (
                messages.ERROR,
                f"'{row.name}': LibreNMS ID {own_id} is already linked to {_owner_text(exc.owner, request.user)}.",
            )
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
        server_name = _server_name(server_key)

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
                    operation = member.operation
                    lines.append(self._write(request, operation, locked[operation.identity.pk], server_name))
            return lines

        for level, text in run_transaction(attempt):
            messages.add_message(request, level, text)


def _entry_change(operation, row):
    """Return the change of the object's mapping that a verified host change writes."""
    server = operation.identity.server
    if isinstance(operation, Unlink):
        return release_server(row, server, clear_preference=False)
    entry = read_mapping(row).server(server)
    if isinstance(operation, Relink):
        own_id, oob_id, oob_type = operation.effects.own_id, entry.oob_id, entry.oob_type
    else:
        own_id, oob_id, oob_type = entry.own_id, operation.effects.oob_id, operation.effects.oob_type
    return edit_server(
        row,
        server,
        own_id=own_id,
        oob_id=oob_id,
        oob_type=oob_type,
        shown=entry,
        configured_servers=LibreNMSAPI.get_available_servers(),
    )


def _success_text(operation, row, cleared, server_name):
    before = operation.assumptions.entry
    ports = f"the {server_name} port IDs of {cleared} interfaces"
    if isinstance(operation, Unlink):
        removed = [f"LibreNMS ID {before.own_id}"] if before.own_id is not None else []
        removed += [f"OOB ID {before.oob_id}"] if before.oob_id is not None else []
        return f"Unlinked '{row.name}' from {server_name}: removed {', '.join(removed)} and {ports}."
    if isinstance(operation, Relink):
        new_id, old_id, label = operation.effects.own_id, before.own_id, "LibreNMS ID"
    else:
        new_id, old_id, label = operation.effects.oob_id, before.oob_id, "OOB ID"
    change = f"replaced {label} {old_id} with {new_id}" if new_id is not None else f"removed {label} {old_id}"
    return f"'{row.name}' on {server_name}: {change}, and cleared {ports}."


def _tab(data):
    tab = data.get("tab")
    return tab if isinstance(tab, str) and tab in {sync_tab.value for sync_tab in SyncTab} else None


class MappingLifecycleView(_LifecycleWriter):
    """Show the unlink confirmation of one device or VM, and apply its one confirmed lifecycle token."""

    confirmation_policy = LIFECYCLE_POLICY

    def get(self, request, object_type, pk):
        """Render the unlink confirmation of the object on the server that the query names; it writes nothing."""
        model = self._model(object_type)
        if error := self.require_all_permissions("GET"):
            return error
        obj = self.restrict_object_or_404(model, "change", pk=pk)
        if request.GET.get("action") != Unlink.ACTION:
            raise Http404("Unknown mapping lifecycle action.")
        try:
            server_key = require_server_key(request.GET.get("server_key", ""))
        except ValueError:
            messages.error(request, "Select the LibreNMS server to unlink.")
            return redirect(sync_page_url(obj))
        return render_host_change(request, obj, server_key, Unlink.ACTION, UnlinkChoices(), tab=_tab(request.GET))

    def post(self, request, object_type, pk):
        """Apply the one submitted lifecycle token of the object, and return to its sync page."""
        model = self._model(object_type)
        if error := self.require_all_permissions("POST"):
            return error
        obj = self.restrict_object_or_404(model, "change", pk=pk)
        tab = _tab(request.POST)
        posted = request.POST.getlist("server_key")
        try:
            server_key = require_server_key(posted[0] if len(posted) == 1 else "")
        except ValueError:
            messages.error(request, "Select one LibreNMS server.")
            return redirect(sync_page_url(obj, tab=tab))
        tokens = request.POST.getlist(intent.FIELD)
        if len(set(tokens)) > 1:
            # One confirmation names one change of the object.
            messages.error(request, _INVALID_CONFIRMATION)
            return redirect(sync_page_url(obj, tab=tab))
        self._apply(
            request,
            tokens,
            policy=LIFECYCLE_POLICY,
            context=intent.IntentContext(request.user.pk, (obj._meta.label_lower, obj.pk), server_key),
            model=model,
            server_key=server_key,
        )
        # After an unlink the page opens on the server that the selection resolves.
        row = self.restricted_queryset(model).filter(pk=obj.pk).first()
        mapped = row is not None and host_entry(row, server_key).display_id is not None
        configured = server_key in LibreNMSAPI.get_available_servers()
        return redirect(sync_page_url(obj, server_key=server_key if configured and mapped else None, tab=tab))


class BulkLinkView(_LifecycleWriter):
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
