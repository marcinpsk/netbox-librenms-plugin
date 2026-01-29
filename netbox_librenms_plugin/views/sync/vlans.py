from dcim.models import Device
from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from ipam.models import VLAN, VLANGroup

from netbox_librenms_plugin.views.mixins import CacheMixin


class SyncVLANsView(CacheMixin, View):
    """
    Handle POST requests to create/update VLANs in NetBox from LibreNMS data.
    """

    def post(self, request, object_type: str, object_id: int):
        """
        Process sync request.

        Expected POST data:
        - vlan_group: ID of target VLAN group (optional)
        - action: 'create_vlans'
        - select: List of VLAN IDs to create
        """
        obj = self.get_object(object_type, object_id)
        action = request.POST.get("action", "")

        # Get optional VLAN group
        vlan_group_id = request.POST.get("vlan_group")
        vlan_group = None
        if vlan_group_id:
            try:
                vlan_group = VLANGroup.objects.get(pk=int(vlan_group_id))
            except (ValueError, VLANGroup.DoesNotExist):
                messages.error(request, "Invalid VLAN group selected.")
                return self._redirect(object_type, object_id)

        if action == "create_vlans":
            return self._handle_create_vlans(request, obj, vlan_group, object_type, object_id)
        else:
            messages.error(request, "Invalid action specified.")
            return self._redirect(object_type, object_id)

    def get_object(self, object_type: str, object_id: int):
        """Get the target object (Device or VM)."""
        if object_type == "device":
            return get_object_or_404(Device, pk=object_id)
        raise Http404("Invalid object type.")

    def _redirect(self, object_type: str, object_id: int):
        """Redirect back to sync page with VLAN tab active."""
        url_name = (
            "dcim:device_librenms_sync"
            if object_type == "device"
            else "plugins:netbox_librenms_plugin:vm_librenms_sync"
        )
        return redirect(reverse(url_name, kwargs={"pk": object_id}) + "?tab=vlans")

    def _handle_create_vlans(self, request, obj, vlan_group, object_type, object_id):
        """
        Handle creating selected VLANs in NetBox.

        Now reads per-row VLAN group selections from form fields named 'vlan_group_{vid}'.
        """
        selected_vlans = request.POST.getlist("select")

        if not selected_vlans:
            messages.error(request, "No VLANs selected for creation.")
            return self._redirect(object_type, object_id)

        # Get cached VLAN data
        cached_vlans = cache.get(self.get_cache_key(obj, "vlans"))
        if not cached_vlans:
            messages.error(request, "No cached VLAN data. Please refresh VLANs first.")
            return self._redirect(object_type, object_id)

        # Build lookup of LibreNMS VLANs by VID
        librenms_vlans = {str(v["vlan_vlan"]): v for v in cached_vlans}

        created_count = 0
        updated_count = 0
        skipped_count = 0

        with transaction.atomic():
            for vid_str in selected_vlans:
                try:
                    vid = int(vid_str)
                except ValueError:
                    continue

                vlan_data = librenms_vlans.get(vid_str)
                if not vlan_data:
                    continue

                # Get per-row VLAN group selection
                group_id_str = request.POST.get(f"vlan_group_{vid}", "")
                row_vlan_group = None
                if group_id_str:
                    try:
                        row_vlan_group = VLANGroup.objects.get(pk=int(group_id_str))
                    except (ValueError, VLANGroup.DoesNotExist):
                        pass  # Fall back to global VLAN (no group)

                librenms_name = vlan_data.get("vlan_name", f"VLAN {vid}")

                if row_vlan_group:
                    # Grouped VLAN: match by VID (unique constraint within group)
                    vlan, created = VLAN.objects.get_or_create(
                        vid=vid,
                        group=row_vlan_group,
                        defaults={
                            "name": librenms_name,
                            "status": "active" if vlan_data.get("vlan_state") == 1 else "reserved",
                        },
                    )
                    if created:
                        created_count += 1
                    elif vlan.name != librenms_name:
                        vlan.name = librenms_name
                        vlan.save()
                        updated_count += 1
                    else:
                        skipped_count += 1
                else:
                    # Global VLAN: try VID+name match first
                    vlan = VLAN.objects.filter(vid=vid, name=librenms_name, group__isnull=True).first()
                    if not vlan:
                        vlan = VLAN.objects.create(
                            vid=vid,
                            name=librenms_name,
                            group=None,
                            status="active" if vlan_data.get("vlan_state") == 1 else "reserved",
                        )
                        created_count += 1
                    else:
                        skipped_count += 1

        # Build summary message
        parts = []
        if created_count > 0:
            parts.append(f"{created_count} created")
        if updated_count > 0:
            parts.append(f"{updated_count} updated")
        if skipped_count > 0:
            parts.append(f"{skipped_count} unchanged")

        if parts:
            messages.success(request, f"VLANs synced: {', '.join(parts)}.")
        else:
            messages.warning(request, "No VLANs were created or updated.")

        return self._redirect(object_type, object_id)
