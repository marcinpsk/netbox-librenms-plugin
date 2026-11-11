import logging
from collections import Counter
from dataclasses import dataclass

from dcim.models import Device
from django.contrib import messages
from django.db.models import Case, CharField, Value, When
from django.urls import reverse
from netbox.views import generic
from virtualization.models import VirtualMachine

from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin.assessment.mapping_lifecycle import (
    LinkState,
    link_state_without_matching,
    read_link_assessment,
)
from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN
from netbox_librenms_plugin.filtersets import DeviceStatusFilterSet, VMStatusFilterSet
from netbox_librenms_plugin.forms import (
    DeviceStatusFilterForm,
    VirtualMachineStatusFilterForm,
)
from netbox_librenms_plugin.import_utils.disclosure import visible_object_label
from netbox_librenms_plugin.matching import CANDIDATE_LABELS, Deadline
from netbox_librenms_plugin.server_mappings import prefetch_mappings
from netbox_librenms_plugin.server_selection import chassis_sync_devices
from netbox_librenms_plugin.tables.device_status import DeviceStatusTable
from netbox_librenms_plugin.tables.VM_status import VMStatusTable
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin, LibreNMSGenericPermissionMixin

logger = logging.getLogger(__name__)

# One request matches the unlinked rows of its page for at most this long.
STATUS_MATCH_DEADLINE_SECONDS = 15
# A row state that only the other rows of the page decide: two rows propose one LibreNMS ID.
AMBIGUOUS = "ambiguous"
_UNCHECKED = frozenset({LinkState.UNREACHABLE.value, LinkState.NOT_CHECKED.value})


@dataclass(frozen=True)
class LinkRow:
    """What one unlinked row of the visible page shows: the state, the match, and the offered token."""

    state: str
    librenms_id: int | None = None
    hostname: str | None = None
    matched_by: str | None = None
    owner_label: str | None = None
    token: str | None = None


class _StatusListMixin(LibreNMSGenericPermissionMixin, LibreNMSAPIMixin):
    """Show the stored link of every filtered row, and match the unlinked rows of the visible page."""

    template_name = "netbox_librenms_plugin/status_check.html"
    actions = {}
    model = None
    # The sync member of each listed chassis, keyed by chassis ID; the device list fills it.
    sync_by_chassis = {}

    def get_required_permission(self):
        """Return the NetBox permission required to view the list."""
        from utilities.permissions import get_permission_for_model

        return get_permission_for_model(self.model, "view")

    def _filtered(self, queryset):
        """Return the filtered rows, annotated with their stored link state on the server; LibreNMS is not asked."""
        queryset = self.filterset(self.request.GET, queryset=queryset).qs
        self._rows = prefetch_mappings(queryset)
        server_key = self.librenms_api.server_key
        states = {row.pk: link_state_without_matching(row, server_key) for row in self._rows}
        return queryset.annotate(
            librenms_status=Case(
                *(
                    When(pk__in=[pk for pk, found in states.items() if found is state], then=Value(state.value))
                    for state in (LinkState.LINKED, LinkState.REFUSED)
                ),
                default=Value(None),
                output_field=CharField(),
            )
        )

    def get_table(self, data, request, bulk_actions=True):
        """Return the configured table, with the match of each unlinked row of its page."""
        table = super().get_table(data, request, bulk_actions)
        table.link_rows = (
            self._link_rows(request, [row.record for row in table.page.object_list]) if request.GET else {}
        )
        return table

    def _own_sync_member(self, record):
        """Return whether *record* is outside a chassis, or is the sync member that the list shows for it."""
        sync_device = self.sync_by_chassis.get(getattr(record, "virtual_chassis_id", None))
        return sync_device is None or sync_device.pk == record.pk

    def _link_rows(self, request, page):
        """Match the unlinked rows of *page* in table order until the deadline, and report the rows not checked."""
        api = self.librenms_api
        candidates = [record for record in page if record.librenms_status is None and self._own_sync_member(record)]
        changeable = set()
        # One scope query for the whole page, so the query count does not depend on how many rows match.
        if page and request.user.has_perm(PERM_CHANGE_PLUGIN):
            changeable = set(
                self.model.objects.restrict(request.user, "change")
                .filter(pk__in=[record.pk for record in page])
                .values_list("pk", flat=True)
            )
        deadline = Deadline(STATUS_MATCH_DEADLINE_SECONDS)
        assessments = {}
        for record in prefetch_mappings(candidates):
            context = intent.context_for(request.user, record, api.server_key) if record.pk in changeable else None
            assessments[record.pk] = read_link_assessment(record, api, context, deadline=deadline)
        matched = Counter(
            assessment.match.librenms_id for assessment in assessments.values() if assessment.state is LinkState.MATCH
        )
        rows = {pk: self._link_row(request, assessment, matched) for pk, assessment in assessments.items()}
        unchecked = sum(row.state in _UNCHECKED for row in rows.values())
        if unchecked:
            messages.warning(
                request,
                f"{unchecked} of {len(rows)} unlinked objects on this page were not checked against LibreNMS. "
                "Reload the page to check them.",
            )
        return rows

    @staticmethod
    def _link_row(request, assessment, matched):
        match = assessment.match
        if match is None:
            return LinkRow(assessment.state.value)
        state, token = assessment.state.value, assessment.offer.token if assessment.offer is not None else None
        if assessment.state is LinkState.MATCH and matched[match.librenms_id] > 1:
            state, token = AMBIGUOUS, None
        owner_label = None
        if assessment.owner is not None and (visible := visible_object_label(assessment.owner, request.user)):
            owner_label = f"{visible[0]} '{visible[1]}'"
        return LinkRow(
            state=state,
            librenms_id=match.librenms_id,
            hostname=match.hostname or match.sysname,
            matched_by=CANDIDATE_LABELS[match.candidate],
            owner_label=owner_label,
            token=token,
        )

    def get_extra_context(self, request):
        """
        Return the target of the link forms, which the Link controls of every page post to.

        The forms do not depend on the offers of this page: htmx swaps a later page in as a table only.
        """
        context = super().get_extra_context(request)
        if not request.GET or not request.user.has_perm(PERM_CHANGE_PLUGIN):
            return context
        return {
            **context,
            "link_selected_url": reverse(
                "plugins:netbox_librenms_plugin:link_selected", kwargs={"object_type": self.model._meta.model_name}
            ),
            "link_server_key": self.librenms_api.server_key,
        }


class DeviceStatusListView(_StatusListMixin, generic.ObjectListView):
    """
    Check NetBox device status in LibreNMS.

    Shows NetBox devices with their LibreNMS status.
    """

    queryset = Device.objects.none()  # Start with empty queryset
    model = Device
    table = DeviceStatusTable
    filterset = DeviceStatusFilterSet
    filterset_form = DeviceStatusFilterForm
    title = "Device LibreNMS Status"

    def get_queryset(self, request):
        """Return the filtered devices with their stored link state; a list without filters is empty."""
        self.sync_by_chassis = {}
        if not self.request.GET:
            return Device.objects.none()
        queryset = self._filtered(
            Device.objects.restrict(request.user, "view")
            .select_related("device_type__manufacturer")
            .prefetch_related("site", "location", "rack")
        )
        # A chassis row links to its sync member; one member query serves every listed chassis.
        self.sync_by_chassis = chassis_sync_devices(self._rows)
        return queryset

    def get_table(self, data, request, bulk_actions=True):
        """Return the status table with the sync member of each listed chassis."""
        table = super().get_table(data, request, bulk_actions)
        table.sync_by_chassis = self.sync_by_chassis
        return table


class VMStatusListView(_StatusListMixin, generic.ObjectListView):
    """Check the status of virtual machines in NetBox against LibreNMS."""

    queryset = VirtualMachine.objects.select_related("cluster", "site")
    model = VirtualMachine
    table = VMStatusTable
    filterset = VMStatusFilterSet
    filterset_form = VirtualMachineStatusFilterForm
    title = "Virtual Machine LibreNMS Status"

    def get_queryset(self, request):
        """Return the filtered VMs with their stored link state; a list without filters is empty."""
        if not self.request.GET:
            return VirtualMachine.objects.none()
        return self._filtered(VirtualMachine.objects.restrict(request.user, "view").select_related("cluster", "site"))
