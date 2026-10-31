"""Physical inventory indices and serial comparison, without capture or discovery."""

from netbox_librenms_plugin.utils import (
    normalize_inventory_serial,
    normalize_stack_serial,
    preload_normalization_rules,
)


def inventory_index(value) -> int | None:
    """Return an ENTITY-MIB number (position, index, containment) as an int, or None."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def select_vc_parent_index(root_rows: list) -> int | None:
    """Return the index of the root that holds the stack members: a stack root first, else a chassis root."""
    for wanted in ("stack", "chassis"):
        for row in root_rows:
            if isinstance(row, dict) and row.get("entPhysicalClass") == wanted:
                index = inventory_index(row.get("entPhysicalIndex"))
                if index is not None:
                    return index
    return None


def vc_serial_key(manufacturer=None, *, preloaded_rules=None):
    """
    Return the function that turns a serial into the form that stack member matching compares.

    This is the one member-serial equality: the serial rules of *manufacturer* and the global
    rules (Juniper's "S/N " mark, for example). A placeholder gives an empty key, before and
    after the rules. The master decision, module attribution, the serials modal and
    :func:`serials_match` all compare serials through it.

    Args:
        manufacturer: The Manufacturer whose serial rules apply, or None for global rules only.
        preloaded_rules (dict | None): Serial rules already loaded, as ``preload_normalization_rules`` returns them.

    """
    if preloaded_rules is None:
        preloaded_rules = preload_normalization_rules("serial", manufacturer=manufacturer)
    rules = preloaded_rules

    def key(value) -> str:
        # A rule such as ^(.+)$ -> X\1 must not turn a placeholder into a real serial.
        if not normalize_stack_serial(value):
            return ""
        return normalize_stack_serial(
            normalize_inventory_serial(value, manufacturer=manufacturer, preloaded_rules=rules)
        )

    return key


def serials_match(stored, incoming, serial_key) -> bool:
    """
    Return whether a stored ``Device.serial`` and a LibreNMS serial name the same hardware.

    Both sides go through *serial_key*, so a rule change applies to both at once. An empty
    key matches nothing.

    Args:
        stored: The serial that NetBox stores.
        incoming: The serial that LibreNMS reports.
        serial_key: From :func:`vc_serial_key` or :func:`chassis_serial_key`.

    """
    stored_key = serial_key(stored)
    return bool(stored_key) and stored_key == serial_key(incoming)
