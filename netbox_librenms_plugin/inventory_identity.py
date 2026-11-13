"""Physical inventory indices, without capture or discovery."""


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
