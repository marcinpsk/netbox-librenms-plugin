"""Apply the confirmed host changes of a NetBox device or VM and clear their interface cascade."""

from functools import partial
from urllib.parse import urlencode

from dcim.models import Device
from django.apps import apps
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from utilities.exceptions import PermissionsViolation
from virtualization.models import VirtualMachine

from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin.assessment.mapping_lifecycle import (
    HOST_CHANGES,
    HostChangeState,
    Relink,
    build_host_change,
    read_host_change_assessment,
    scope_permitted,
)
from netbox_librenms_plugin.import_utils.disclosure import visible_object_label
from netbox_librenms_plugin.librenms_api import LibreNMSAPI
from netbox_librenms_plugin.mapping_owner import ensure_mapping_owner, mapping_ownership
from netbox_librenms_plugin.server_mappings import (
    AmbiguousLibreNMSIdError,
    IdentityOwned,
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
)

LIFECYCLE_POLICY = intent.RoutePolicy(
    frozenset((intent.Resource.MAPPING_LIFECYCLE, action, intent.Role.PRIMARY) for action in HOST_CHANGES)
)
_MODELS = {"device": Device, "virtualmachine": VirtualMachine}
_SYNC_PAGES = {"device": "device_librenms_sync", "virtualmachine": "vm_librenms_sync"}
_INVALID_CONFIRMATION = "The confirmation is invalid or has expired. Refresh the page and try again."
# The noun of each operation in an outcome line.
_NOUNS = {"relink": "LibreNMS ID change", "change_oob": "OOB controller change"}


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


def render_host_change(request, obj, server_key, action, choices, *, tab=None, return_server_key=None):
    """
    Render the confirmation of a host change of *obj*, or return to its sync page with the refusal.

    The confirmation names the server, the IDs that the change removes or replaces, and each
    interface whose port ID on the server it clears. It writes nothing. Cancel and a refusal return
    to the sync page on *return_server_key*, the server that the page showed.
    """
    assessment = read_host_change_assessment(
        obj, server_key, action, choices, user=request.user, context=intent.context_for(request.user, obj, server_key)
    )
    if assessment.state is not HostChangeState.OFFERED or assessment.offer is None:
        messages.error(request, _refusal_text(assessment, obj, request.user))
        return redirect(sync_page_url(obj, server_key=return_server_key, tab=tab))
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
            "cancel_url": sync_page_url(obj, server_key=return_server_key, tab=tab),
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
        """Lock and rebuild each approved host change from current state."""
        changeable = self.restricted_queryset(model, "change")
        pairs = []
        for operation in sorted(approved, key=lambda operation: operation.identity.pk):
            identity = operation.identity
            if identity.model != model._meta.label_lower or identity.server != server_key:
                rebuilt = intent.Unavailable("subject.available")
            else:
                rebuilt = self._rebuild_host_change(operation, changeable, model, server_key, locked)
            pairs.append((operation, rebuilt))
        return pairs

    def _rebuild_host_change(self, operation, changeable, model, server_key, locked):
        """Lock the chassis, its members and the scope's interfaces, claim a new ID, and rebuild the change."""
        cls = type(operation)
        row = lock_host_change_owner(model, operation.identity.pk)
        if row is None or not changeable.filter(pk=row.pk).exists():
            return intent.Unavailable("subject.available")
        # The locked chassis decides the owner: a master change after the owner check makes the change stale.
        owner = mapping_ownership(row).owner
        if owner is None or owner.pk != row.pk:
            return intent.Unavailable("assumptions.chassis")
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
    if isinstance(operation, Relink):
        new_id, old_id, label = operation.effects.own_id, before.own_id, "LibreNMS ID"
    else:
        new_id, old_id, label = operation.effects.oob_id, before.oob_id, "OOB ID"
    change = f"replaced {label} {old_id} with {new_id}" if new_id is not None else f"removed {label} {old_id}"
    return f"'{row.name}' on {server_name}: {change}, and cleared {ports}."


def posted_tab(data):
    """Return the sync tab that *data* names, or None when it names no tab."""
    tab = data.get("tab")
    return tab if isinstance(tab, str) and tab in {sync_tab.value for sync_tab in SyncTab} else None


class MappingLifecycleView(_LifecycleWriter):
    """Apply the one confirmed host change of a device or VM."""

    confirmation_policy = LIFECYCLE_POLICY

    def post(self, request, object_type, pk):
        """Apply the one submitted lifecycle token of the object, and return to its sync page."""
        model = self._model(object_type)
        if error := self.require_all_permissions("POST"):
            return error
        obj = ensure_mapping_owner(self.restrict_object_or_404(model, "change", pk=pk))
        tab = posted_tab(request.POST)
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
        row = self.restricted_queryset(model).filter(pk=obj.pk).first()
        mapped = row is not None and host_entry(row, server_key).display_id is not None
        configured = server_key in LibreNMSAPI.get_available_servers()
        return redirect(sync_page_url(obj, server_key=server_key if configured and mapped else None, tab=tab))
