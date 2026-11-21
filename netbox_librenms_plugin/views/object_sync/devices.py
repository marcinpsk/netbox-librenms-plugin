import copy

from dcim.models import Device
from django.http import JsonResponse
from django.views import View
from utilities.permissions import get_permission_for_model
from utilities.views import ViewTab, register_model_view

from netbox_librenms_plugin.constants import PERM_VIEW_PLUGIN
from netbox_librenms_plugin.module_grants import ACTION_VERIFY_MODULE, GrantRefusal, GrantRefused, read_module_grant
from netbox_librenms_plugin.tables.cables import (
    LibreNMSCableTable,
    VCCableTable,
)
from netbox_librenms_plugin.tables.interfaces import (
    LibreNMSInterfaceTable,
    VCInterfaceTable,
)
from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable, VCModuleTable
from netbox_librenms_plugin.utils import (
    coerce_model_pk,
    get_interface_name_field,
)

from ..base.cables_view import BaseCableTableView
from ..base.interfaces_view import BaseInterfaceTableView
from ..base.ip_addresses_view import BaseIPAddressTableView
from ..base.librenms_sync_view import BaseLibreNMSSyncView
from ..base.modules_view import BaseModuleTableView
from ..base.vlan_table_view import BaseVLANTableView
from ..mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    parse_request_json,
    server_unavailable_json,
)


@register_model_view(Device, name="librenms_sync", path="librenms-sync")
class DeviceLibreNMSSyncView(BaseLibreNMSSyncView):
    """Device detail tab showing LibreNMS sync information."""

    queryset = Device.objects.all()
    model = Device
    tab = ViewTab(label="LibreNMS Sync", permission=PERM_VIEW_PLUGIN)

    def get_interface_context(self, request, obj):
        """Return interface sync context for the device."""
        interface_name_field = get_interface_name_field(request, obj)
        interface_table_view = DeviceInterfaceTableView()
        interface_table_view.request = copy.copy(request)
        return interface_table_view.get_context_data(request, obj, interface_name_field)

    def get_cable_context(self, request, obj):
        """Return cable sync context for the device."""
        cable_table_view = DeviceCableTableView()
        cable_table_view.request = copy.copy(request)
        return cable_table_view.get_context_data(request, obj)

    def get_ip_context(self, request, obj):
        """Return IP address sync context for the device."""
        ipaddress_table_view = DeviceIPAddressTableView()
        ipaddress_table_view.request = copy.copy(request)
        return ipaddress_table_view.get_context_data(request, obj)

    def get_vlan_context(self, request, obj):
        vlan_table_view = DeviceVLANTableView()
        vlan_table_view.request = copy.copy(request)
        return vlan_table_view.get_vlan_context(request, obj)

    def get_module_context(self, request, obj):
        """Return module sync context for the device."""
        module_table_view = DeviceModuleTableView()
        module_table_view.request = copy.copy(request)
        return module_table_view.get_context_data(request, obj)


class DeviceInterfaceTableView(BaseInterfaceTableView):
    """Interface synchronization table for Devices."""

    model = Device

    def get_interfaces(self, obj):
        """Return all interfaces for the device."""
        return obj.interfaces.all()

    def get_table(self, data, obj, interface_name_field, vlan_groups=None):
        """Return the appropriate interface table, selecting VC variant if needed."""
        server_key = self._render_server_key()
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            table = VCInterfaceTable(
                data,
                device=obj,
                chassis=self.sync_view_scope(obj).chassis,
                interface_name_field=interface_name_field,
                vlan_groups=vlan_groups,
                server_key=server_key,
                user=self.request.user,
            )
        else:
            table = LibreNMSInterfaceTable(
                data,
                device=obj,
                interface_name_field=interface_name_field,
                vlan_groups=vlan_groups,
                server_key=server_key,
                user=self.request.user,
            )
        return table


class SingleModuleVerifyView(
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    LibreNMSAPIMixin,
    CacheMixin,
    View,
):
    """Verify module row data against cached LibreNMS inventory for a selected VC member."""

    # A fetch() caller reads the answer, so the middleware answers with a JSON error.
    answers_json = True

    # JSON endpoint that returns the rendered module row — only viewers of the
    # underlying Device should be able to surface its inventory data.
    required_object_permissions = {"POST": [("view", Device)]}

    def post(self, request):
        # Bind the request so require_object_permissions_json() (which reads self.request)
        # works even when post() is invoked directly rather than through dispatch().
        self.request = request

        data, err = parse_request_json(request)
        if err:
            return err
        # Gate BEFORE resolving the device: without this an unauthorized caller could probe
        # arbitrary device IDs (existence via 404) through this endpoint.
        if error := self.require_object_permissions_json("POST"):
            return error
        parsed = self._parse_request(data)
        if isinstance(parsed, JsonResponse):
            return parsed
        selected_device_id, page_device_id, ent_physical_index, row_depth = parsed
        # Only a configured string key: a forged/non-string key must neither probe another
        # namespace nor TypeError-500.
        server_key = self.resolve_requested_server_key(data)
        if server_key is None:
            return server_unavailable_json()

        # Restrict the lookups to the caller's viewable devices: the gate above only checked the
        # model-level view_device perm, so a site-scoped grant would otherwise read another
        # device's cached verify payload by raw pk.
        page_device = self.restrict_object_or_404(Device, pk=page_device_id)
        selected_device = self.restrict_object_or_404(Device, pk=selected_device_id)
        if selected_device.pk != page_device.pk and (
            not page_device.virtual_chassis_id or selected_device.virtual_chassis_id != page_device.virtual_chassis_id
        ):
            return JsonResponse(
                {"status": "error", "message": "The selected device is not a member of this Virtual Chassis"},
                status=400,
            )

        module_table_view = DeviceModuleTableView()
        # Shallow-copy the request so the child view can mutate request.GET /
        # request.POST without affecting this request object.
        module_table_view.request = copy.copy(request)
        module_table_view.cache_only = True
        context = module_table_view.get_verify_context_data(
            module_table_view.request,
            page_device,
            selected_device,
            server_key=server_key,
        )
        if (rule_error := context.get("inventory_rule_error")) is not None:
            return JsonResponse({"status": "error", "message": rule_error.public_message}, status=409)
        if context.get("table") is None:
            return JsonResponse({"status": "error", "message": "No cached inventory data"}, status=404)
        if context.get("inventory_index_ambiguous"):
            return JsonResponse({"status": "error", "message": GrantRefusal.AMBIGUOUS_INDEX.message}, status=409)
        return self._verified_row_response(data, context, page_device, selected_device, ent_physical_index, row_depth)

    @staticmethod
    def _parse_request(data):
        """Return ``(device id, page device id, index, depth)`` from the request body, or a 400 response."""
        # Coerced like the sibling verify views: a raw JSON value reaching the pk lookup turns a
        # bad request into a database error and a 500 where the siblings answer 400.
        selected_device_id = coerce_model_pk(data.get("device_id"))
        page_device_id = coerce_model_pk(data.get("page_device_id"))
        ent_physical_index = data.get("ent_physical_index")
        if selected_device_id is None:
            return JsonResponse({"status": "error", "message": "No device ID provided"}, status=400)
        if page_device_id is None:
            return JsonResponse({"status": "error", "message": "No page device ID provided"}, status=400)
        if ent_physical_index in (None, ""):
            return JsonResponse({"status": "error", "message": "No entPhysicalIndex provided"}, status=400)
        try:
            ent_physical_index = int(ent_physical_index)
        except (TypeError, ValueError):
            return JsonResponse({"status": "error", "message": "Invalid entPhysicalIndex"}, status=400)
        try:
            row_depth = int(data.get("depth", 0))
        except (TypeError, ValueError):
            row_depth = 0
        return selected_device_id, page_device_id, ent_physical_index, row_depth

    def _verified_row_response(self, data, context, page_device, selected_device, ent_physical_index, row_depth):
        """Return the pinned row with its grants when the verify grant holds and both renders show the row."""
        table = context["table"]
        try:
            grant = read_module_grant(
                data.get("verify_binding"),
                action=ACTION_VERIFY_MODULE,
                page_device_id=page_device.pk,
                server_key=context["server_key"],
            ).require(ent_index=ent_physical_index)
        except GrantRefused as exc:
            return JsonResponse({"status": "error", "message": exc.refusal.message}, status=409)
        # The pinned table reads the same snapshot and policy load that a page table would.
        if grant.inventory_digest != table.inventory_snapshot_digest:
            return JsonResponse({"status": "error", "message": GrantRefusal.SNAPSHOT.message}, status=409)
        if grant.policy_fingerprint != table.policy_fingerprint:
            return JsonResponse({"status": "error", "message": GrantRefusal.POLICY.message}, status=409)

        # A grant needs both renders: the signed page rows show the row, and the table pinned to the member shows it.
        # A row the page withholds now is never signed again, also from a grant signed before it was withheld.
        page_rows = [index for (index,) in grant.rows if index not in context["withheld_indices"]]
        row = self._pinned_row(table.data, selected_device.pk, ent_physical_index, row_depth)
        if row is None or ent_physical_index not in page_rows:
            return JsonResponse(
                {"status": "error", "message": "This inventory row is not available for the selected member"},
                status=404,
            )
        row = {
            **row,
            "branch_rows": [entry for entry in row.get("branch_rows", []) if entry[0] in page_rows],
            "verify_rows": page_rows,
        }
        if not any(index != ent_physical_index for index, _target in row["branch_rows"]):
            row["has_installable_children"] = False
        if row.get("selectable") and table.selection_allowed:
            row["selection_grant"] = table.selection_override(row)
        formatted_row = table.format_module_data(row)
        return JsonResponse({"status": "success", "formatted_row": formatted_row})

    @staticmethod
    def _pinned_row(table_data, device_id, ent_physical_index, row_depth):
        """Return the pinned render's row of one index; the depth separates malformed duplicates."""
        candidates = [
            candidate
            for candidate in table_data
            if candidate.get("selected_device_id") == device_id
            and candidate.get("ent_physical_index") == ent_physical_index
        ]
        return next((c for c in candidates if c.get("depth", 0) == row_depth), None) or next(iter(candidates), None)


class DeviceCableTableView(BaseCableTableView):
    """Cable synchronization view for Devices."""

    model = Device

    def get_table(self, data, obj):
        """Return the appropriate cable table, selecting VC variant if needed."""
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            return VCCableTable(data, device=obj, chassis=self.sync_view_scope(obj).chassis)
        return LibreNMSCableTable(data, device=obj)


class DeviceIPAddressTableView(BaseIPAddressTableView):
    """IP address synchronization view for Devices."""

    model = Device


class DeviceVLANTableView(BaseVLANTableView):
    """VLAN synchronization table view for Devices."""

    model = Device


class DeviceModuleTableView(BaseModuleTableView):
    """Module/inventory synchronization view for Devices."""

    model = Device

    def get_table(self, data, obj):
        """Return the module sync table."""
        from ..sync.modules import module_interface_type_preview_permissions

        user = self.request.user
        has_write_permission = self.has_write_permission()
        server_key = getattr(self, "_active_server_key", None) or self.librenms_api.server_key
        table_class, member_kwargs = LibreNMSModuleTable, {}
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            table_class = VCModuleTable
            member_kwargs["chassis"] = self.sync_view_scope(obj).chassis
        table = table_class(
            data,
            device=obj,
            **member_kwargs,
            server_key=server_key,
            has_write_permission=has_write_permission,
            can_add_module=has_write_permission and user.has_perm("dcim.add_module"),
            can_change_module=has_write_permission and user.has_perm("dcim.change_module"),
            can_change_interface=has_write_permission and user.has_perm("dcim.change_interface"),
            can_review_interface_types=(
                has_write_permission
                and user.has_perm("dcim.change_interface")
                and all(
                    user.has_perm(get_permission_for_model(model, action))
                    for action, model in module_interface_type_preview_permissions()
                )
            ),
            can_delete_module=has_write_permission and user.has_perm("dcim.delete_module"),
            can_add_module_bay_template=(has_write_permission and user.has_perm("dcim.add_modulebaytemplate")),
            can_add_module_type=(has_write_permission and user.has_perm("dcim.add_moduletype")),
            can_add_carrier_rule=(
                has_write_permission and user.has_perm("netbox_librenms_plugin.add_carrierautoinstallrule")
            ),
            can_add_module_bay_mapping=(
                has_write_permission and user.has_perm("netbox_librenms_plugin.add_modulebaymapping")
            ),
            can_map_existing_bay=(
                has_write_permission
                and user.has_perm("netbox_librenms_plugin.add_modulebaymapping")
                and user.has_perm("dcim.view_device")
                and user.has_perm("dcim.view_modulebay")
            ),
            can_add_module_type_mapping=(
                has_write_permission and user.has_perm("netbox_librenms_plugin.add_moduletypemapping")
            ),
            can_change_device_type=has_write_permission and user.has_perm("dcim.change_devicetype"),
            can_add_inventory_rule=(
                has_write_permission and user.has_perm("netbox_librenms_plugin.add_inventoryignorerule")
            ),
            can_change_inventory_rule=(
                has_write_permission and user.has_perm("netbox_librenms_plugin.change_inventoryignorerule")
            ),
        )
        return table
