def effective_inventory(filtered, full, *, params, http_status):
    # ruleid: inventory-fallback-normalizes-indices
    return [row for row in full.value if all(str(row.get(field)) == str(value) for field, value in params)]


def effective_inventory(filtered, full, *, params, http_status):
    # ruleid: inventory-fallback-normalizes-indices
    return [row for row in full.value if all(str(value) == str(row.get(field)) for field, value in params)]


def effective_inventory(filtered, full, *, params, http_status):
    # ok: inventory-fallback-normalizes-indices
    return [row for row in full.value if inventory_index(row.get("entPhysicalContainedIn")) == inventory_index(value)]


def effective_inventory(filtered, full, *, params, http_status):
    # ok: inventory-fallback-normalizes-indices
    return [row for row in full.value if row.get("entPhysicalClass") == value]


def effective_inventory(filtered, full, *, params, http_status):
    # ok: inventory-fallback-normalizes-indices
    return [row for row in full.value if str(row.get("entPhysicalClass")) == str(value)]


def other_filter(row, field, value):
    # ok: inventory-fallback-normalizes-indices
    return str(row.get(field)) == str(value)
