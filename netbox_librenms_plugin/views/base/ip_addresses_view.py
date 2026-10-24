import json
import logging
from ipaddress import ip_interface

from dcim.models import Device, Interface
from django.contrib import messages
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views import View
from ipam.models import IPAddress
from virtualization.models import VirtualMachine

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.constants import is_supported_interface_name_field
from netbox_librenms_plugin.ip_addressing import parse_address_with_prefix
from netbox_librenms_plugin.ip_rules import ip_rules_for_request, row_ip_rule_block
from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, request_actor_id
from netbox_librenms_plugin.sync_filters import filter_sync_table, parse_table_filters
from netbox_librenms_plugin.tables.ipaddresses import IPAddressTable
from netbox_librenms_plugin.utils import (
    get_interface_name_field,
    index_ip_sync_rows,
    normalize_ip_sync_row_id,
    resolve_create_missing_interfaces,
    resolve_set_primary_ip,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)

from netbox_librenms_plugin.assessment.ip_addresses import assess, interface_scope

logger = logging.getLogger(__name__)


def ip_interface_scope(view, obj):
    """Return the interface scope used by assessment and the writer."""
    return interface_scope(obj, view.request.user)


def read_ip_assessment(request, obj, snapshot, server_key, interface_name_field=None):
    """Assess source facts and add the URLs used by the web readers."""
    if not is_supported_interface_name_field(interface_name_field):
        interface_name_field = get_interface_name_field(request, obj)
    result = assess(snapshot, subject=obj, actor=request.user, interface_name_field=interface_name_field)
    for row in result.rows:
        row["device_url"] = obj.get_absolute_url()
        if ip := row.pop("netbox_ip", None):
            row["ip_url"] = ip.get_absolute_url()
        if interface := row.pop("netbox_interface", None):
            row["interface_url"] = interface.get_absolute_url()
        if row.get("can_create_vrf"):
            row["vrf_create_url"] = reverse(
                "plugins:netbox_librenms_plugin:create_ip_row_vrf",
                kwargs={"object_type": obj._meta.model_name, "pk": obj.pk},
            )
    return result


class BaseIPAddressTableView(LibreNMSPermissionMixin, LibreNMSAPIMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """Base view for synchronizing IP address information from LibreNMS."""

    partial_template_name = "netbox_librenms_plugin/_ipaddress_sync_content.html"

    def get_object(self, pk):
        return self.restrict_object_or_404(self.model, pk=pk)

    def _prepare_context(self, request, obj, server_key):
        """Return the tab context of the stored snapshot, or None when there is no snapshot to show."""
        snapshot = source_cache.read(obj, Resource.IP_ADDRESSES, server_key)
        if snapshot is None:
            return None
        return self._snapshot_context(request, obj, snapshot, server_key)

    def _snapshot_context(self, request, obj, snapshot, server_key):
        """Return the tab context of one snapshot, assessed for the request user."""
        ip_data = read_ip_assessment(request, obj, snapshot, server_key).rows
        cache_ttl = source_cache.remaining_ttl(obj, Resource.IP_ADDRESSES, server_key)
        cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl) if cache_ttl else None

        # The management rows and the row identities describe the whole snapshot, so they come before the filter.
        mgmt_row_ids = list(dict.fromkeys(row["row_id"] for row in ip_data if row.get("is_mgmt_ip") and row["row_id"]))
        table, filters, filtered_from = filter_sync_table(
            request, obj, ip_data, lambda rows: IPAddressTable(rows, mgmt_row_ids=mgmt_row_ids), server_key=server_key
        )
        table.configure(request)

        return {
            "table": table,
            "filters": filters,
            # The row count before the filter, shown only while a filter narrows the table.
            "filtered_from": filtered_from,
            "object": obj,
            "cache_expiry": cache_expiry,
            "server_key": server_key,
            "set_primary_ip": resolve_set_primary_ip(request),
            "create_missing_interfaces": resolve_create_missing_interfaces(request),
            # Donor "Move IP to winner" candidates (empty unless this device carries a
            # _migrated_to marker for server_key); drives the migrated-mode action card.
            "movable_ips": self._movable_ips_for_migration(obj, server_key),
        }

    def _empty_context(self, request, obj, server_key, migration_server_key):
        """
        Return the tab context without a table.

        The set-primary-IP and create-missing switches keep their state, and the donor's move card
        stays: its moves are NetBox operations that never read LibreNMS.
        """
        return {
            "table": None,
            "object": obj,
            "cache_expiry": None,
            "server_key": server_key,
            "set_primary_ip": resolve_set_primary_ip(request),
            "create_missing_interfaces": resolve_create_missing_interfaces(request),
            "movable_ips": self._movable_ips_for_migration(obj, migration_server_key),
        }

    @staticmethod
    def _movable_ips_for_migration(obj, server_key):
        """
        List the donor's interface-assigned IPs as Move-to-winner candidates (empty unless migrated).

        Only NetBox Device ``Interface`` assignments are listed: MoveIPAddressToWinnerView re-homes an
        IP from a donor interface to the winner's same-named interface and rejects non-Interface (e.g.
        VMInterface) assignments, so VM-owned IPs are intentionally excluded. Gated on the marker so a
        non-migrated device pays no extra query.

        Args:
            obj (Device | VirtualMachine): The candidate donor object.
            server_key (str | None): The LibreNMS server key for the migration marker.

        Returns:
            list[dict[str, int | str]]: The movable IP candidates, or an empty list.
        """
        from dcim.models import Device
        from django.contrib.contenttypes.models import ContentType

        if not isinstance(obj, Device) or not read_mapping(obj).migrated_to(server_key or "default"):
            return []
        name_by_id = {iface.pk: iface.name for iface in obj.interfaces.all()}
        if not name_by_id:
            return []
        iface_ct = ContentType.objects.get_for_model(Interface)
        return [
            {"id": ip.pk, "address": str(ip.address), "interface_name": name_by_id.get(ip.assigned_object_id, "")}
            for ip in IPAddress.objects.filter(
                assigned_object_type=iface_ct, assigned_object_id__in=list(name_by_id)
            ).order_by("address")
        ]

    def get_context_data(self, request, obj):
        """Get the context data for the IP address sync view."""
        # The filters show their errors also when no snapshot is cached.
        filters = parse_table_filters(request.GET, lambda rows: IPAddressTable(rows, mgmt_row_ids=[]))
        # Read the snapshot of ?server_key; a key that no longer resolves renders an empty table, never the default's.
        scoped, unresolved = self.resolve_get_render_server_key(request)
        server_key = None if unresolved else scoped or self._render_server_key()
        context = self._prepare_context(request, obj, server_key) if server_key is not None else None
        if context is None:
            context = self._empty_context(request, obj, scoped, scoped)
        context.setdefault("filters", filters)
        return context

    def post(self, request, pk):
        """Refresh the IP snapshot from LibreNMS and render the tab."""
        obj = self.get_object(pk)
        # Rebind the API to the POSTed server, so discovery and acquisition use the server of the tab.
        server_key = self.rebind_api_for_posted_server(request.POST)
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            # rebind_api_for_server() returned None to avoid building a missing/misconfigured
            # default client; reading the lazy `librenms_api` property here would reconstruct it
            # and can raise (a 500 on this HTMX error path). Use the already-cached client's key.
            active_server_key = self.active_server_key
            # render_sync_partial injects the migrated-donor context (resolved from the active
            # session key, since the POSTed key is now known-invalid) so a stale server_key can't
            # silently re-enable IP sync on a migrated donor.
            return self.render_sync_partial(
                request,
                obj,
                active_server_key,
                {"ip_sync": self._empty_context(request, obj, None, active_server_key)},
            )

        # Discovery may find the LibreNMS device by IP or hostname and store the mapping; acquisition
        # then reads that mapping and never writes.
        librenms_id, lookup_error = self.resolve_librenms_id(obj)
        if librenms_id is None:
            # A failed refresh must not leave an older snapshot to act on.
            source_cache.delete(obj, Resource.IP_ADDRESSES, server_key)
            error_message = self.scoped_lookup_message(lookup_error) if lookup_error else "Device not found in LibreNMS"
        else:
            result = source_cache.refresh(request, obj, Resource.IP_ADDRESSES, server_key)
            snapshot = result.snapshot
            error_message = result.error and f"Failed to fetch IP addresses from LibreNMS: {result.error}"

        if error_message is not None:
            messages.error(request, error_message)
            SyncCacheConsistency(obj).mark_refresh_failure(
                SyncTab.IP_ADDRESSES,
                server_key,
                actor_id=request_actor_id(request),
            )
            return self.render_sync_partial(
                request, obj, server_key, {"ip_sync": self._empty_context(request, obj, server_key, server_key)}
            )

        if SyncCacheConsistency(obj).mark_refresh_outcome(
            SyncTab.IP_ADDRESSES,
            server_key,
            actor_id=request_actor_id(request),
        ):
            messages.success(request, "IP address data refreshed successfully.")
        else:
            # The rows below come from this refresh and are rendered, so the message must not
            # claim there is nothing to show. Only the snapshot write failed, and a sync posts
            # against that snapshot, so say what the user has to do before syncing.
            messages.error(
                request,
                "IP address data could not be cached. The rows shown come from this refresh only "
                "and will not survive a reload. Refresh again before syncing; see server logs for details.",
            )
        return self.render_sync_partial(
            request, obj, server_key, {"ip_sync": self._snapshot_context(request, obj, snapshot, server_key)}
        )


class SingleIPAddressVerifyView(NetBoxObjectPermissionMixin, LibreNMSPermissionMixin, CacheMixin, View):
    """View for verifying single IP address data with different VRF."""

    # A fetch() caller reads the answer, so the middleware answers with a JSON error.
    answers_json = True

    # Read-only verify endpoint: require object-view permission (mirrors the interface/module/
    # cable verify views). Without it any user with mere plugin-view rights could POST an
    # arbitrary device id and read back that object's name/url/IP rows. The effective
    # requirement is narrowed per-request in post() to the model the request actually targets
    # (Device vs VirtualMachine); this class-level default (both) is the fail-closed fallback the
    # permission-wiring test asserts.
    required_object_permissions = {"POST": [("view", Device), ("view", VirtualMachine)]}

    def require_object_permissions_json(self, method, *, object_id=None, object_type=None):
        """
        Resolve the per-object view permission for this verify request, then run the base gate.

        Computing the requirement here (rather than inline in :meth:`post`) keeps the DB
        model-resolution behind the same method the post-flow tests mock out — so those tests
        exercise the request flow without a DB hit, while the real gate path resolves Device vs
        VirtualMachine and checks the matching view permission.

        Args:
            method: HTTP method to gate (``"POST"``).
            object_id: The posted object id, used to resolve the model when no type is given.
            object_type: The posted ``object_type`` (``"device"``/``"virtualmachine"``), if any.

        Returns:
            None if permitted, or a 403 JsonResponse if denied.
        """
        self.required_object_permissions = {method: self._required_perms_for_object(object_id, object_type)}
        return super().require_object_permissions_json(method)

    def _required_perms_for_object(self, object_id, object_type):
        """
        Object-view perms a verify POST must hold for the object it targets.

        Mirrors :meth:`_get_object`'s resolution so the gate matches the model whose name/url/cache
        the response would expose: an explicit ``object_type`` gates on exactly that model; with no
        type, resolve the id to its model (an existence check that reads no object data), Device
        first, then VirtualMachine. Fall back to requiring BOTH view perms (fail closed) when the id
        is unusable or resolves to neither, so a Device-only (or VM-only) caller can never read the
        other model's data through this endpoint.

        Args:
            object_id (int | str | None): The posted object ID.
            object_type (str | None): The posted object type, if provided.

        Returns:
            list[tuple[str, type]]: The required model permissions.
        """
        if object_type == "device":
            return [("view", Device)]
        if object_type == "virtualmachine":
            return [("view", VirtualMachine)]
        try:
            if Device.objects.filter(pk=object_id).exists():
                return [("view", Device)]
            if VirtualMachine.objects.filter(pk=object_id).exists():
                return [("view", VirtualMachine)]
        except (ValueError, TypeError):
            pass
        return [("view", Device), ("view", VirtualMachine)]

    def _get_object(self, object_id, object_type=None):
        """
        Retrieve the object (Device or VirtualMachine) based on ID and optional type.

        Resolves through permission-restricted querysets: the POST gate only checks model-level
        view perms, so a constrained grant must not resolve an out-of-scope id here. It returns a 404
        instead of exposing the object's assessed IP rows.

        Args:
            object_id (int): The object ID.
            object_type (str | None): The object type, if provided.

        Returns:
            Device | VirtualMachine: The permitted object.

        Raises:
            Http404: If no permitted Device or VirtualMachine matches the request.
        """
        if object_type == "device":
            return self.restrict_object_or_404(Device, pk=object_id)
        elif object_type == "virtualmachine":
            return self.restrict_object_or_404(VirtualMachine, pk=object_id)
        else:
            # Try to find object without knowing its type (scoped to the caller's viewable objects).
            obj = self.restricted_queryset(Device).filter(pk=object_id).first()
            if obj:
                return obj

            obj = self.restricted_queryset(VirtualMachine).filter(pk=object_id).first()
            if obj:
                return obj

            raise Http404(f"Object with ID {object_id} not found in Device or VirtualMachine models")

    def _parse_ip_address(self, ip_address):
        """
        Parse IP address string into address and prefix length.

        Works with both IPv4 and IPv6 addresses.

        Args:
            ip_address (str): The IP address with its prefix length.

        Returns:
            tuple[str, int]: The host address and prefix length.

        Raises:
            ValueError: If the IP address or prefix length is invalid.
        """
        parsed = parse_address_with_prefix(ip_address)
        return str(parsed.ip), parsed.network.prefixlen

    @staticmethod
    def _find_row(rows, address, prefix_len, row_id=None):
        """Resolve one assessed source row without guessing among equal addresses."""
        target = str(parse_address_with_prefix(address, prefix_len))
        key = normalize_ip_sync_row_id(row_id) if row_id is not None else target
        if key.partition("@")[0] != target:
            return None, None, None
        index, duplicates = index_ip_sync_rows(rows)
        row = index.get(key) if key not in duplicates else None
        if row is None:
            return None, None, None
        return row, row.get("vrf_id"), row.get("port_id")

    def _find_existing_ip(self, address_no_mask, prefix_len, vrf_id=None):
        """Find existing IP address in NetBox, optionally with specific VRF."""
        ip_with_mask = str(parse_address_with_prefix(address_no_mask, prefix_len))

        matches = list(IPAddress.objects.filter(address=ip_with_mask).order_by("pk"))
        if not matches:
            return False, False, None

        if vrf_id is not None:
            matching_vrf_rows = [row for row in matches if row.vrf_id == vrf_id]
        else:
            matching_vrf_rows = [row for row in matches if row.vrf_id is None]

        if len(matching_vrf_rows) == 1:
            return True, True, matching_vrf_rows[0].get_absolute_url()
        return True, False, matches[0].get_absolute_url() if len(matches) == 1 else None

    def _determine_status(self, exists_any_vrf, exists_specific_vrf, original_vrf_id, vrf_id):
        """Determine the status of an IP address based on existence and VRF."""
        if exists_any_vrf:
            # IP exists in NetBox
            if exists_specific_vrf:
                return "matched"
            else:
                return "update"
        else:
            # IP doesn't exist in NetBox, check if restoring to original VRF
            if original_vrf_id is not None and original_vrf_id == vrf_id:
                return "matched"
            else:
                return "sync"

    def post(self, request):  # noqa: C901
        """POST request to return json response with formatted IP address status."""
        # Parse the (caller-supplied) body first — this reads no object data — so the permission
        # gate can target the model the request actually addresses. _get_object() resolves a Device
        # OR a VirtualMachine, so a static Device-only gate would let a user with only
        # dcim.view_device read VM name/url/cache data (and vice versa). Gate BELOW still runs
        # before any object is resolved or any cached row is read.
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"status": "error", "message": "Invalid JSON payload"}, status=400)
        # json.loads accepts a bare list/string/number; calling .get() on those raises before the
        # permission gate and the broad handler below, so 500s on a non-object body. Reject it.
        if not isinstance(data, dict):
            return JsonResponse({"status": "error", "message": "JSON payload must be an object"}, status=400)
        object_id = data.get("device_id")
        object_type = data.get("object_type")
        if error := self.require_object_permissions_json("POST", object_id=object_id, object_type=object_type):
            return error
        try:
            from netbox_librenms_plugin.librenms_api import LibreNMSAPI

            ip_address = data.get("ip_address")
            vrf_id = data.get("vrf_id")
            # Validate the requested server key before using it as a cache namespace: an
            # unconfigured/forged key would otherwise let a caller probe arbitrary server-key cache
            # namespaces via get_cache_key(). Mirror the sync/cable membership check; anything
            # unrecognized falls back to "default" (the prior missing-key behaviour), so a caller
            # can only ever address a configured key or the fixed default — never an arbitrary one.
            requested_server_key = data.get("server_key")
            if isinstance(requested_server_key, str) and requested_server_key in LibreNMSAPI.get_available_servers():
                server_key = requested_server_key
            else:
                server_key = "default"

            if not ip_address:
                return JsonResponse({"status": "error", "message": "No IP address provided"}, status=400)

            # Reject JSON booleans explicitly before the falsy check: bool is an int
            # subclass, so True/False would otherwise pass int() and validate as IDs 1/0,
            # and object_id=False would be misreported as "No object ID provided".
            if isinstance(object_id, bool):
                return JsonResponse({"status": "error", "message": "Invalid object ID"}, status=400)
            # Reject JSON floats too: int(1.9) silently truncates to 1, so a fractional id
            # would coerce to a different object instead of returning a clean 400.
            if isinstance(object_id, float):
                return JsonResponse({"status": "error", "message": "Invalid object ID"}, status=400)

            if not object_id:
                return JsonResponse({"status": "error", "message": "No object ID provided"}, status=400)

            # Validate the client-supplied numeric IDs up front so a bad value returns a
            # clean 400 instead of raising deep in the ORM and being caught as a generic 500.
            try:
                object_id = int(object_id)
            except (TypeError, ValueError):
                return JsonResponse({"status": "error", "message": "Invalid object ID"}, status=400)
            if vrf_id in (None, ""):
                vrf_id = None
            elif isinstance(vrf_id, bool):
                return JsonResponse({"status": "error", "message": "Invalid VRF ID"}, status=400)
            elif isinstance(vrf_id, float):
                return JsonResponse({"status": "error", "message": "Invalid VRF ID"}, status=400)
            else:
                try:
                    vrf_id = int(vrf_id)
                except (TypeError, ValueError):
                    return JsonResponse({"status": "error", "message": "Invalid VRF ID"}, status=400)

            # Get the object (Device or VirtualMachine)
            try:
                obj = self._get_object(object_id, object_type)
            except Http404:
                return JsonResponse({"status": "error", "message": f"Object with ID {object_id} not found"}, status=404)

            # Validate both address and source selector before reading the snapshot.
            try:
                address_no_mask, prefix_len = self._parse_ip_address(ip_address)
                if data.get("row_id") is not None:
                    normalize_ip_sync_row_id(data["row_id"])
            except ValueError:
                return JsonResponse(
                    {"status": "error", "message": "Invalid IP address or source row identity"},
                    status=400,
                )

            snapshot = source_cache.read(obj, Resource.IP_ADDRESSES, server_key)
            rows = (
                read_ip_assessment(request, obj, snapshot, server_key, data.get("interface_name_field")).rows
                if snapshot is not None
                else []
            )

            # Basic record with default values
            updated_record = {
                "ip_address": address_no_mask,
                "prefix_length": prefix_len,
                "ip_with_mask": str(parse_address_with_prefix(address_no_mask, prefix_len)),
                "device": obj.name,
                "device_url": obj.get_absolute_url(),
                "vrf_id": vrf_id,
                "exists": False,
                "status": "sync",
            }

            # Try to find the IP in the assessed rows
            cache_entry, original_vrf_id, original_port_id = self._find_row(
                rows, address_no_mask, prefix_len, data.get("row_id")
            )

            if cache_entry is None and (
                data.get("row_id") is not None
                or sum(row["ip_with_mask"] == updated_record["ip_with_mask"] for row in rows) > 1
            ):
                return JsonResponse(
                    {"status": "error", "message": "Refresh the IP data and select a current source row."}, status=400
                )

            # Update record with the assessed row if found
            if cache_entry:
                # Update with all fields except the ones the chosen VRF decides
                for key, value in cache_entry.items():
                    if key not in ["vrf_id", "status", "ip_rule_block"]:
                        updated_record[key] = value

            # If no source row was found, use first device interface
            if original_port_id is None:
                interface = obj.interfaces.first()
                if interface:
                    updated_record["interface_name"] = interface.name
                    updated_record["interface_url"] = interface.get_absolute_url()

            # Check if IP exists in NetBox
            exists_any_vrf, exists_specific_vrf, ip_url = self._find_existing_ip(address_no_mask, prefix_len, vrf_id)

            if exists_any_vrf:
                updated_record["exists"] = True
                updated_record["ip_url"] = ip_url

            # Determine status based on existence and VRF
            updated_record["status"] = self._determine_status(
                exists_any_vrf, exists_specific_vrf, original_vrf_id, vrf_id
            )

            ip_rule = ip_rules_for_request(request).decide(ip_interface(updated_record["ip_with_mask"]).ip, vrf_id)
            if ip_rule is not None:
                updated_record["ip_rule_block"] = row_ip_rule_block(ip_rule)

            # Render status HTML
            table = IPAddressTable([], mgmt_row_ids=[])
            status_html = table.render_status(updated_record["status"], updated_record)

            return JsonResponse(
                {
                    "status": "success",
                    "ip_address": ip_address,
                    "formatted_row": {"status": status_html, "selectable": ip_rule is None},
                }
            )

        except Exception:
            logger.exception("Unexpected error in IP address sync")
            return JsonResponse(
                {"status": "error", "message": "An internal error occurred. Please check server logs."}, status=500
            )
