import re
from typing import Optional

from dcim.models import Device
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpRequest
from netbox.config import get_config
from netbox.plugins import get_plugin_config
from utilities.paginator import get_paginate_count as netbox_get_paginate_count


def convert_speed_to_kbps(speed_bps: int) -> int:
    """
    Convert speed from bits per second to kilobits per second.

    Args:
        speed_bps (int): Speed in bits per second.

    Returns:
        int: Speed in kilobits per second.
    """
    if speed_bps is None:
        return None
    return speed_bps // 1000


def format_mac_address(mac_address: str) -> str:
    """
    Validate and format MAC address string for table display.

    Args:
        mac_address (str): The MAC address string to format.

    Returns:
        str: The MAC address formatted as XX:XX:XX:XX:XX:XX.
    """
    if not mac_address:
        return ""

    mac_address = mac_address.strip().replace(":", "").replace("-", "")

    if len(mac_address) != 12:
        return "Invalid MAC Address"  # Return a message if the address is not valid

    formatted_mac = ":".join(mac_address[i : i + 2] for i in range(0, len(mac_address), 2))
    return formatted_mac.upper()


def get_virtual_chassis_member(device: Device, port_name: str) -> Device:
    """
    Determines the likely virtual chassis member based on the device's vc_position and port name.

    Args:
        device (Device): The NetBox device instance.
        port_name (str): The name of the port (e.g., 'Ethernet1').

    Returns:
        Device: The virtual chassis member device corresponding to the port.
                Returns the original device if not part of a virtual chassis or if matching fails.
    """
    if not hasattr(device, "virtual_chassis") or not device.virtual_chassis:
        return device

    try:
        match = re.match(r"^[A-Za-z]+(\d+)", port_name)
        if not match:
            return device

        # Get the port number and use it
        vc_position = int(match.group(1))
        return device.virtual_chassis.members.get(vc_position=vc_position)
    except (re.error, ValueError, ObjectDoesNotExist):
        return device


def get_librenms_sync_device(device: Device) -> Optional[Device]:
    """
    Determine which Virtual Chassis member should handle LibreNMS sync operations.

    LibreNMS treats a Virtual Chassis as a single logical device, so only one member
    should have the librenms_id custom field set and be used for sync operations.

    Priority order for selecting the sync device:
    1. Any member with librenms_id custom field set (highest priority - already configured)
    2. Master device with primary IP (if master is designated)
    3. Any member with primary IP (fallback when no master or master lacks IP)
    4. Member with lowest vc_position (for error messages when no IPs configured)

    Args:
        device (Device): Any device in the virtual chassis.

    Returns:
        Optional[Device]: The device that should handle LibreNMS sync, or None if
                         the device is not in a virtual chassis.
    """
    if not hasattr(device, "virtual_chassis") or not device.virtual_chassis:
        return device

    vc = device.virtual_chassis
    all_members = vc.members.all()

    # Priority 1: Check if ANY member has librenms_id configured
    for member in all_members:
        if member.cf.get("librenms_id"):
            return member

    # Priority 2: Use master device if it has primary IP
    if vc.master and vc.master.primary_ip:
        return vc.master

    # Priority 3: Find any member with primary IP
    for member in all_members:
        if member.primary_ip:
            return member

    # Priority 4: Use member with lowest vc_position as fallback
    try:
        return min(all_members, key=lambda m: m.vc_position, default=None)
    except (ValueError, TypeError):
        return None


def get_table_paginate_count(request: HttpRequest, table_prefix: str) -> int:
    """
    Extends Netbox pagination to support multiple tables by using table-specific prefixes

    Args:
        request: HTTP request object
        table_prefix: Prefix for the table

    Returns:
        int: Number of items to display per page
    """
    config = get_config()
    if f"{table_prefix}per_page" in request.GET:
        try:
            per_page = int(request.GET.get(f"{table_prefix}per_page"))
            return min(per_page, config.MAX_PAGE_SIZE)
        except ValueError:
            pass

    return netbox_get_paginate_count(request)


def _get_user_pref(request, path, default=None):
    """Get a user preference value via request.user.config.

    Underscore prefix signals package-internal helper, not public plugin API.
    Cross-module imports within this package are intentional.
    """
    if hasattr(request, "user") and hasattr(request.user, "config"):
        return request.user.config.get(path, default)
    return default


def _save_user_pref(request, path, value):
    """Save a user preference value via request.user.config."""
    if hasattr(request, "user") and hasattr(request.user, "config"):
        try:
            request.user.config.set(path, value, commit=True)
        except (TypeError, ValueError):
            pass


def save_import_toggle_prefs(request):
    """Persist use-sysname and strip-domain toggle values from POST to user prefs.

    For checkboxes included via hx-include, unchecked values are not sent.
    We save both values whenever any import action occurs, treating absent as False.
    """
    _save_user_pref(
        request,
        "plugins.netbox_librenms_plugin.use_sysname",
        request.POST.get("use-sysname-toggle") == "on",
    )
    _save_user_pref(
        request,
        "plugins.netbox_librenms_plugin.strip_domain",
        request.POST.get("strip-domain-toggle") == "on",
    )


def get_interface_name_field(request: Optional[HttpRequest] = None) -> str:
    """
    Get interface name field with request override support.

    Checks in order: GET/POST params, user preference, plugin config default.
    When a param is explicitly provided, persists it to user preferences.

    Args:
        request: Optional HTTP request object that may contain override

    Returns:
        str: Interface name field to use
    """
    if request:
        # Explicit override from request params
        param_val = request.GET.get("interface_name_field") or request.POST.get("interface_name_field")
        if param_val:
            existing = _get_user_pref(request, "plugins.netbox_librenms_plugin.interface_name_field")
            if param_val != existing:
                _save_user_pref(request, "plugins.netbox_librenms_plugin.interface_name_field", param_val)
            return param_val

        # Check user preference
        pref_val = _get_user_pref(request, "plugins.netbox_librenms_plugin.interface_name_field")
        if pref_val:
            return pref_val

    # Fall back to plugin config
    return get_plugin_config("netbox_librenms_plugin", "interface_name_field")


def match_librenms_hardware_to_device_type(hardware_name: str) -> dict:
    """
    Match LibreNMS hardware string to a NetBox DeviceType.

    Only performs exact matching on part_number and model fields (case-insensitive).

    Args:
        hardware_name (str): Hardware string from LibreNMS API (e.g., 'C9200L-48P-4X')

    Returns:
        dict: Dictionary containing:
            - matched (bool): Whether a match was found
            - device_type (DeviceType|None): The matched DeviceType object
            - match_type (str|None): Always 'exact' if found, None otherwise
    """
    from dcim.models import DeviceType

    if not hardware_name or hardware_name == "-":
        return {"matched": False, "device_type": None, "match_type": None}

    # Try part number exact match
    try:
        device_type = DeviceType.objects.get(part_number__iexact=hardware_name)
        return {
            "matched": True,
            "device_type": device_type,
            "match_type": "exact",
        }
    except DeviceType.DoesNotExist:
        pass
    except DeviceType.MultipleObjectsReturned:
        device_type = DeviceType.objects.filter(part_number__iexact=hardware_name).first()
        return {
            "matched": True,
            "device_type": device_type,
            "match_type": "exact",
        }

    # Try exact model match (case-insensitive)
    try:
        device_type = DeviceType.objects.get(model__iexact=hardware_name)
        return {"matched": True, "device_type": device_type, "match_type": "exact"}
    except DeviceType.DoesNotExist:
        pass
    except DeviceType.MultipleObjectsReturned:
        device_type = DeviceType.objects.filter(model__iexact=hardware_name).first()
        return {"matched": True, "device_type": device_type, "match_type": "exact"}

    return {"matched": False, "device_type": None, "match_type": None}


def find_matching_site(librenms_location: str) -> dict:
    """
    Find exact matching NetBox site for a LibreNMS location.

    Only performs exact name matching (case-insensitive).

    Args:
        librenms_location (str): Location string from LibreNMS

    Returns:
        dict: Dictionary containing:
            - found (bool): Whether a match was found
            - site (Site|None): The matched Site object
            - match_type (str|None): Always 'exact' if found, None otherwise
            - confidence (float): Always 1.0 if found, 0.0 otherwise
    """
    from dcim.models import Site

    if not librenms_location or librenms_location == "-":
        return {"found": False, "site": None, "match_type": None, "confidence": 0.0}

    # Try case-insensitive exact match
    try:
        site = Site.objects.get(name__iexact=librenms_location)
        return {"found": True, "site": site, "match_type": "exact", "confidence": 1.0}
    except Site.DoesNotExist:
        pass
    except Site.MultipleObjectsReturned:
        site = Site.objects.filter(name__iexact=librenms_location).first()
        return {"found": True, "site": site, "match_type": "exact", "confidence": 1.0}

    return {"found": False, "site": None, "match_type": None, "confidence": 0.0}


def find_matching_platform(librenms_os: str) -> dict:
    """
    Find exact matching NetBox platform for a LibreNMS OS.

    Only performs exact name matching (case-insensitive).

    Args:
        librenms_os (str): OS string from LibreNMS (e.g., 'ios', 'linux', 'junos')

    Returns:
        dict: Dictionary containing:
            - found (bool): Whether a match was found
            - platform (Platform|None): The matched Platform object
            - match_type (str|None): Always 'exact' if found, None otherwise
    """
    from dcim.models import Platform

    if not librenms_os or librenms_os == "-":
        return {"found": False, "platform": None, "match_type": None}

    # Try case-insensitive exact name match
    try:
        platform = Platform.objects.get(name__iexact=librenms_os)
        return {"found": True, "platform": platform, "match_type": "exact"}
    except Platform.DoesNotExist:
        pass
    except Platform.MultipleObjectsReturned:
        platform = Platform.objects.filter(name__iexact=librenms_os).first()
        return {"found": True, "platform": platform, "match_type": "exact"}

    return {"found": False, "platform": None, "match_type": None}
