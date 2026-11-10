from dataclasses import dataclass, field
from functools import partial

from dcim.models import Device
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from ipam.models import VLAN, VLANGroup
from utilities.exceptions import PermissionsViolation

from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.assessment.vlans import VLANCreate, VLANUpdate, assess, build_operation
from netbox_librenms_plugin.sync_cache import (
    SyncTab,
    apply_request_cache_transition,
    schedule_request_cache_mutation,
)
from netbox_librenms_plugin.sync_query import page_query_url
from netbox_librenms_plugin.transactions import saved_in_scope
from netbox_librenms_plugin.utils import (
    acquire_advisory_transaction_lock,
    coerce_model_pk,
    index_vlan_source_rows,
    normalize_vlan_vid,
    vlan_source_namer,
)
from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    VlanAssignmentMixin,
)

VLAN_SYNC_POLICY = intent.RoutePolicy(
    frozenset(
        {
            (intent.Resource.VLAN, VLANCreate.ACTION, intent.Role.PRIMARY),
            (intent.Resource.VLAN, VLANUpdate.ACTION, intent.Role.PRIMARY),
        }
    )
)
_INVALID_CONFIRMATION = "VLAN confirmation is invalid or has expired. Refresh the VLAN data and try again."


@dataclass
class _VLANSyncOutcome:
    """Collect the observable outcomes of one VLAN synchronization request."""

    created_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    permission_skipped_count: int = 0
    concurrent_change_count: int = 0
    invalid_name_count: int = 0
    name_taken_count: int = 0
    add_permission_skipped_count: int = 0
    confirmation_error_count: int = 0
    conflicts: list[dict] = field(default_factory=list)

    def success_parts(self):
        """Return summary fragments for successful synchronization outcomes."""
        parts = []
        if self.created_count:
            parts.append(f"{self.created_count} created")
        if self.updated_count:
            parts.append(f"{self.updated_count} updated")
        if self.skipped_count:
            parts.append(f"{self.skipped_count} unchanged")
        return parts

    def skip_reasons(self):
        """Return summary fragments for rejected synchronization outcomes."""
        reasons = []
        if self.permission_skipped_count:
            reasons.append(f"{self.permission_skipped_count} skipped (change permission missing)")
        if self.add_permission_skipped_count:
            reasons.append(f"{self.add_permission_skipped_count} skipped (add permission constraints)")
        if self.concurrent_change_count:
            reasons.append(f"{self.concurrent_change_count} skipped (concurrent VLAN change)")
        if self.invalid_name_count:
            reasons.append(f"{self.invalid_name_count} skipped (invalid VLAN name)")
        if self.name_taken_count:
            reasons.append(f"{self.name_taken_count} skipped (VLAN name already in the group)")
        if self.confirmation_error_count:
            reasons.append(f"{self.confirmation_error_count} skipped (confirmation no longer current)")
        return reasons


def _acquire_global_vlan_locks(vids):
    """Lock global VLAN VIDs in stable order for the current transaction."""
    for vid in sorted(set(vids)):
        acquire_advisory_transaction_lock(f"netbox-librenms-plugin:global-vlan:{vid}")


class SyncVLANsView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, VlanAssignmentMixin, LibreNMSAPIMixin, CacheMixin, View
):
    """Create or update NetBox VLANs from a LibreNMS source snapshot."""

    confirmation_policy = VLAN_SYNC_POLICY
    required_object_permissions = {
        "POST": [
            # The owner device is resolved through a restricted queryset (see get_object), so
            # state that read here: a missing grant is an explicit 403, not a 404 at the lookup.
            ("view", Device),
            ("add", VLAN),
            ("change", VLAN),
        ],
    }

    def _required_post_permissions(self, request):
        """Require VLANGroup access when a submitted confirmation, or a reviewed row, names a group."""
        permissions = list(type(self).required_object_permissions["POST"])
        operations = intent.signed_operations(request.POST.getlist(intent.FIELD), policy=VLAN_SYNC_POLICY)
        reviewed_groups = [request.POST.get(f"vlan_group_{vid}") for vid in self._reviewed_vids(request)[0]]
        if any(operation.choices.group is not None for operation in operations) or any(reviewed_groups):
            permissions.append(("view", VLANGroup))
        return permissions

    @staticmethod
    def _reviewed_vids(request):
        """Return the canonical VIDs that a review request names, and the values that name no VID."""
        if not request.POST.get("review_choices"):
            return [], []
        vids, invalid = [], []
        for value in request.POST.getlist("select"):
            vid = normalize_vlan_vid(value)
            if vid is None:
                invalid.append(value)
            elif vid not in vids:
                vids.append(vid)
        return vids, invalid

    def post(self, request, object_type: str, object_id: int):
        """
        Process sync request.

        Expected POST data:
        - action: 'create_vlans'
        - confirmation_intent: The token of each selected VLAN row, repeated
        - confirm_conflicts: '1' to apply the renames that a disclosure showed

        Args:
            request (HttpRequest): The request that contains the VLAN synchronization data.
            object_type (str): The type of the target object.
            object_id (int): The target object's ID.

        Returns:
            HttpResponse: A permission error, confirmation disclosure, or synchronization redirect.

        Raises:
            Http404: If the object type is invalid or no permitted device matches the object ID.

        """
        self.required_object_permissions = {"POST": self._required_post_permissions(request)}

        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        obj = self.get_object(object_type, object_id)
        # Use the exact server the user was viewing; an unknown, malformed or repeated key writes nothing.
        self._post_server_key = self.rebind_api_for_posted_server(request.POST)
        if self._post_server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return self._redirect(object_type, object_id)
        action = request.POST.get("action", "")

        if action == "create_vlans":
            return self._handle_create_vlans(request, obj, object_type, object_id)
        else:
            messages.error(request, "Invalid action specified.")
            return self._redirect(object_type, object_id)

    def get_object(self, object_type: str, object_id: int):
        """Get the target object (Device or VM)."""
        if object_type == "device":
            return self.restrict_object_or_404(Device, pk=object_id)
        raise Http404("Invalid object type.")

    def _redirect(self, object_type: str, object_id: int):
        """Redirect back to sync page with VLAN tab active."""
        url = self.get_vlan_tab_url(object_type, object_id)
        if self.request.htmx:
            return HttpResponse("", headers={"HX-Redirect": url})
        return redirect(url)

    def get_vlan_tab_url(self, object_type: str, object_id: int) -> str:
        """Return the URL for the VLAN synchronization tab, with the page query the request carries."""
        url_name = (
            "dcim:device_librenms_sync"
            if object_type == "device"
            else "plugins:netbox_librenms_plugin:vm_librenms_sync"
        )
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        # The action URL carries the page query, so the tab comes back with every tab's filters and page.
        return page_query_url(
            self.request, reverse(url_name, kwargs={"pk": object_id}), {"tab": "vlans", "server_key": server_key}
        )

    def _render_conflicts(self, request, conflicts, obj, object_type, object_id, *, review=False):
        """Render VLAN rename disclosures, or a review of chosen operations, for an HTMX or native submission."""
        context = {
            "conflicts": conflicts,
            "review": review,
            "object_type": object_type,
            "object": obj,
            "server_key": self._post_server_key,
            "cancel_url": self.get_vlan_tab_url(object_type, object_id),
        }
        if request.htmx:
            template_name = "netbox_librenms_plugin/htmx/vlan_conflicts.html"
        else:
            context["full_page"] = True
            # NetBox marks a tab active only for the same ViewTab instance.
            context["tab"] = DeviceLibreNMSSyncView.tab
            template_name = "netbox_librenms_plugin/vlan_conflicts_page.html"
        return render(request, template_name, context)

    def _rebuild(self, approved, *, obj):
        """Lock the scope of every approved VLAN operation and rebuild each one from current state."""
        _acquire_global_vlan_locks(operation.identity.vid for operation in approved if operation.choices.group is None)
        # The group row lock keeps a chosen group, and the absence of a VID in it, until the writes commit.
        group_pks = {operation.choices.group for operation in approved} - {None}
        locked_groups = set(
            self.restricted_queryset(VLANGroup)
            .select_for_update(of=("self",))
            .filter(pk__in=group_pks)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        scope = Q()
        for operation in approved:
            group = operation.choices.group
            scope |= Q(
                vid=operation.identity.vid, **({"group__isnull": True} if group is None else {"group_id": group})
            )
        # Unscoped on purpose: a VLAN the caller cannot see still holds the VID, and the writer must not miss it.
        vlans = list(VLAN.objects.select_for_update().filter(scope).order_by("pk"))
        # The subject, the source rows and the name rules are read after the locks, so a change made while this waited
        # counts: the name rules apply by the manufacturer of the device's current type.
        subject = self.restricted_queryset(Device).select_related("device_type__manufacturer").filter(pk=obj.pk).first()
        snapshot = source_cache.read(obj, Resource.VLANS, self._post_server_key)
        source_by_vid = {} if snapshot is None else index_vlan_source_rows(snapshot.value(Resource.VLANS, "vlans"))[0]
        source_name = vlan_source_namer(subject) if subject is not None else None
        groups = []
        for operation in approved:
            vid, group = operation.identity.vid, operation.choices.group
            source_row = source_by_vid.get(vid)
            if subject is None:
                rebuilt = intent.Unavailable("subject.available")
            elif snapshot is None:
                rebuilt = intent.Unavailable("source.available")
            elif group is not None and group not in locked_groups:
                rebuilt = intent.Unavailable("target.group")
            elif source_row is None:
                rebuilt = intent.Unavailable("source.row")
            else:
                targets = [vlan for vlan in vlans if vlan.vid == vid and vlan.group_id == group]
                rebuilt = build_operation(vid=vid, group=group, name=source_name(source_row, vid), targets=targets)
            groups.append(((operation, rebuilt),))
        return groups

    @staticmethod
    def _report_stale(request, result, outcome):
        """Report one refused group by its VIDs and changed facts, or one refused token."""
        if not result.members:
            messages.error(request, _INVALID_CONFIRMATION)
            outcome.confirmation_error_count += 1
            return
        changed = ", ".join(result.paths)
        for operation in result.members:
            messages.error(
                request,
                f"VLAN {operation.identity.vid}: the confirmed change is no longer current ({changed}). "
                "Refresh the VLAN data and try again.",
            )
            outcome.confirmation_error_count += 1

    @staticmethod
    def _valid_name(request, operation, outcome):
        """Return whether the proposed name passes the NetBox model field, and report it when not."""
        try:
            VLAN._meta.get_field("name").clean(operation.effects.name, None)
        except ValidationError:
            messages.error(request, f"VLAN {operation.identity.vid}: the LibreNMS name is invalid; skipped.")
            outcome.invalid_name_count += 1
            return False
        return True

    @staticmethod
    def _report_name_conflict(request, *, vid, group, name, outcome):
        """Report a write that the group's unique VLAN name, or a concurrent writer, refused."""
        # NetBox keeps VLAN names unique in a group, and a VLAN name rule can make two VIDs share a name.
        if VLAN.objects.filter(group_id=group, name=name).exists():
            messages.error(request, f'VLAN {vid}: the VLAN group already has a VLAN named "{name}"; skipped.')
            outcome.name_taken_count += 1
        else:
            messages.error(request, f"VLAN {vid}: the VLAN could not be written after a concurrent change; skipped.")
            outcome.concurrent_change_count += 1

    def _create(self, request, operation, outcome):
        """Create the VLAN of one verified create operation under the caller's add constraints."""
        effects = operation.effects
        if not self._valid_name(request, operation, outcome):
            return
        try:
            with transaction.atomic():
                vlan = VLAN.objects.create(
                    vid=effects.vid, group_id=effects.group, name=effects.name, status=effects.status
                )
                if not saved_in_scope(vlan, request.user, "add"):
                    raise PermissionsViolation()
        except PermissionsViolation:
            messages.error(
                request,
                f"VLAN {effects.vid}: the new VLAN is outside your add permission constraints; skipped.",
            )
            outcome.add_permission_skipped_count += 1
        except IntegrityError:
            self._report_name_conflict(
                request, vid=effects.vid, group=effects.group, name=effects.name, outcome=outcome
            )
        else:
            outcome.created_count += 1

    def _changeable_target(self, request, operation, changeable_vlans, outcome):
        """Return the verified target VLAN when it is in the caller's change scope, and report it when not."""
        vlan = changeable_vlans.select_related("group").filter(pk=operation.effects.vlan).first()
        if vlan is None:
            messages.error(
                request,
                f"VLAN {operation.identity.vid}: an existing VLAN in this scope is outside your change permission; "
                "skipped.",
            )
            outcome.permission_skipped_count += 1
        return vlan

    def _rename(self, request, operation, vlan, outcome):
        """Apply one verified, confirmed rename to its target VLAN, which the rebuild locked."""
        if not self._valid_name(request, operation, outcome):
            return
        vlan.snapshot()
        vlan.name = operation.effects.name
        try:
            with transaction.atomic():
                vlan.save(update_fields=["name", "last_updated"])
                if not saved_in_scope(vlan, request.user, "change"):
                    raise PermissionsViolation()
        except PermissionsViolation:
            messages.error(
                request,
                f"VLAN {operation.identity.vid}: the renamed VLAN is outside your change permission; skipped.",
            )
            outcome.permission_skipped_count += 1
        except IntegrityError:
            self._report_name_conflict(
                request, vid=operation.identity.vid, group=vlan.group_id, name=operation.effects.name, outcome=outcome
            )
        else:
            outcome.updated_count += 1

    def _apply(self, request, member, *, confirm, changeable_vlans, outcome):
        """Write one verified operation, or disclose a rename that the request has not confirmed."""
        operation = member.operation
        if isinstance(operation, VLANCreate):
            self._create(request, operation, outcome)
            return
        if not operation.renames:
            outcome.skipped_count += 1
            return
        vlan = self._changeable_target(request, operation, changeable_vlans, outcome)
        if vlan is None:
            return
        if confirm:
            self._rename(request, operation, vlan, outcome)
            return
        outcome.conflicts.append(
            {
                "vid": operation.identity.vid,
                "group": str(vlan.group) if vlan.group is not None else "Global",
                "current_name": operation.assumptions.target.name,
                "proposed_name": operation.effects.name,
                # The disclosure passes on the token of the verified operation; it never mints one.
                "token": member.token,
            }
        )

    @staticmethod
    def _report_outcome(request, outcome):
        """Add one batch summary message without claiming rejected rows as successes."""
        parts = outcome.success_parts()
        skip_reasons = outcome.skip_reasons()
        if parts:
            messages.success(request, f"VLANs synced: {', '.join(parts + skip_reasons)}.")
        elif skip_reasons:
            messages.warning(request, f"No VLANs synced: {', '.join(skip_reasons)}.")
        elif not outcome.conflicts:
            messages.warning(request, "No VLANs were created or updated.")

    def _reviewed_choices(self, request, vids):
        """Return the posted group choice of each reviewed VID that names a usable scope, and report the others."""
        choices, groups = {}, {}
        posted = {vid: request.POST.get(f"vlan_group_{vid}") for vid in vids}
        group_pks = {coerce_model_pk(value) for value in posted.values() if value} - {None}
        visible = {group.pk: group for group in self.restricted_queryset(VLANGroup).filter(pk__in=group_pks)}
        for vid, value in posted.items():
            if value is None:
                messages.error(request, f"VLAN {vid}: the VLAN group selection is missing; nothing to review.")
            elif value == "":
                choices[vid] = None
            elif (group := visible.get(coerce_model_pk(value))) is None:
                messages.error(request, f"VLAN {vid}: the selected VLAN group is not available; nothing to review.")
            else:
                choices[vid], groups[group.pk] = group.pk, group
        return choices, list(groups.values())

    def _review(self, request, obj, object_type, object_id):
        """
        Reassess the ticked rows for their posted group choices and show the resulting operations.

        This is the first step of a page without JavaScript. It writes nothing; an explicit apply of the
        offered tokens follows in another request.
        """
        vids, invalid = self._reviewed_vids(request)
        if invalid:
            messages.error(request, "A selected VLAN has no usable VID; nothing to review.")
        snapshot = source_cache.read(obj, Resource.VLANS, self._post_server_key)
        if snapshot is None:
            messages.error(request, "No cached VLAN data. Please refresh VLANs first.")
            return self._redirect(object_type, object_id)
        choices, groups = self._reviewed_choices(request, vids)
        rows = assess(
            snapshot,
            subject=obj,
            lookup_maps=self._build_vlan_lookup_maps(groups, user=request.user),
            context=intent.IntentContext(request.user.pk, (obj._meta.label_lower, obj.pk), self._post_server_key),
            choices=choices,
            vids=set(choices),
        ).rows
        by_vid = {}
        for row in rows:
            by_vid.setdefault(normalize_vlan_vid(row["raw_vlan_id"]), []).append(row)
        entries = []
        for vid in choices:
            matched = by_vid.get(vid, [])
            if len(matched) != 1 or matched[0]["vid_error"] or not matched[0]["confirmation_intent"]:
                messages.error(
                    request, f"VLAN {vid}: the VLAN data offers no change for this choice; nothing to review."
                )
                continue
            operation = matched[0]["operation"]
            if isinstance(operation, VLANUpdate) and not operation.renames:
                messages.info(request, f"VLAN {vid}: unchanged.")
                continue
            group = next((group for group in groups if group.pk == operation.choices.group), None)
            entries.append(
                {
                    "vid": vid,
                    "group": str(group) if group is not None else "Global",
                    "current_name": operation.assumptions.target.name if isinstance(operation, VLANUpdate) else None,
                    "proposed_name": operation.effects.name,
                    "token": matched[0]["confirmation_intent"],
                }
            )
        if not entries:
            return self._redirect(object_type, object_id)
        return self._render_conflicts(request, entries, obj, object_type, object_id, review=True)

    def _handle_create_vlans(self, request, obj, object_type, object_id):
        """
        Apply the verified VLAN operations of the submitted tokens, and disclose unconfirmed renames.

        Args:
            request (HttpRequest): The request that contains the selected VLAN data.
            obj (Device): The device that owns the synchronization request.
            object_type (str): The type of the target object.
            object_id (int): The target object's ID.

        Returns:
            HttpResponse: A confirmation disclosure or redirect to the VLAN synchronization page.

        """
        # A review reassesses only; one request never reassesses and applies.
        if request.POST.get("review_choices"):
            return self._review(request, obj, object_type, object_id)
        tokens = request.POST.getlist(intent.FIELD)
        if not any(tokens):
            messages.error(request, "No VLAN changes selected.")
            return self._redirect(object_type, object_id)

        # The rebuild reads the snapshot again under its locks; this read only refuses a request without one.
        if source_cache.read(obj, Resource.VLANS, self._post_server_key) is None:
            messages.error(request, "No cached VLAN data. Please refresh VLANs first.")
            return self._redirect(object_type, object_id)

        context = intent.IntentContext(request.user.pk, (obj._meta.label_lower, obj.pk), self._post_server_key)
        confirm = request.POST.get("confirm_conflicts") == "1"
        changeable_vlans = self.restricted_queryset(VLAN, "change")
        outcome = _VLANSyncOutcome()
        with transaction.atomic():
            verification = intent.verify(
                tokens,
                policy=VLAN_SYNC_POLICY,
                context=context,
                rebuild=partial(self._rebuild, obj=obj),
            )
            for result in verification.results:
                if isinstance(result, intent.Stale):
                    self._report_stale(request, result, outcome)
                    continue
                for member in result.members:
                    self._apply(request, member, confirm=confirm, changeable_vlans=changeable_vlans, outcome=outcome)

        self._report_outcome(request, outcome)

        if outcome.created_count or outcome.updated_count:
            schedule_request_cache_mutation(
                request,
                obj,
                SyncTab.VLANS,
                self._post_server_key,
                source_fragment_required=bool(outcome.conflicts),
            )
        if outcome.conflicts:
            return apply_request_cache_transition(
                request, self._render_conflicts(request, outcome.conflicts, obj, object_type, object_id)
            )
        return apply_request_cache_transition(request, self._redirect(object_type, object_id))
