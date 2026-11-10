"""
Raw stored mapping values stay in the tests of the legacy runtime and of the storage shapes.

A behaviour test seeds and reads a mapping through the semantic helpers of ``mapping_fixtures``, so a
change of the storage changes the helpers and not the tests. The scan counts, per file, each call of
a raw helper (through an import alias, a module attribute or a local alias too), each
``librenms_cf=`` keyword and each call of ``map_device_to_librenms`` that can pass ``oob``. It does
not follow a raw value through any other local helper.
"""

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
FIXTURES = "netbox_librenms_plugin.tests.mapping_fixtures"
RAW_HELPERS = frozenset({"seed_stored_mapping", "seed_stored_mapping_row", "stored_mapping_for_test"})
# It stores the OOB entry as the caller gives it.
RAW_WRAPPERS = frozenset({"map_device_to_librenms"})

LEGACY = "LEGACY: the legacy runtime, which increment 5 deletes with these tests"
SHAPE = "SHAPE: stored forms, malformed entries, change records or the seam's own encoding"
PENDING = "PENDING: an ordinary seed that the follow-up before the cutover converts"

# The pinned number of raw uses per file, and why. A pin can only go down.
PINNED = {
    "conftest.py": (2, f"{PENDING}: make_device(librenms_cf=...) and map_device_to_librenms store the raw value"),
    "test_acquisition_cables.py": (1, f"{PENDING}; {SHAPE}"),
    "test_acquisition_interfaces.py": (2, f"{PENDING}; {SHAPE}"),
    "test_acquisition_ip_addresses.py": (1, PENDING),
    "test_acquisition_modules.py": (2, f"{PENDING}; {SHAPE}"),
    "test_acquisition_vlans.py": (1, PENDING),
    "test_background_jobs.py": (1, PENDING),
    "test_before_state_writes.py": (1, PENDING),
    "test_bulk_import_job_control.py": (1, PENDING),
    "test_bulk_import_refresh_state.py": (1, PENDING),
    "test_cable_blocker_notifications.py": (1, PENDING),
    "test_cable_tab_source_snapshot.py": (1, PENDING),
    "test_confirmation_intent_contract.py": (6, PENDING),
    "test_coverage_actions.py": (41, f"{LEGACY}; {SHAPE}"),
    "test_coverage_api.py": (4, SHAPE),
    "test_coverage_base_views.py": (1, PENDING),
    "test_coverage_base_views2.py": (4, f"{SHAPE}; {PENDING}"),
    "test_coverage_cables_view_paths.py": (4, f"{PENDING}; {SHAPE}"),
    "test_coverage_device_fields.py": (22, f"{LEGACY}; {SHAPE}"),
    "test_coverage_device_operations.py": (2, LEGACY),
    "test_coverage_interfaces_view_refresh.py": (3, f"{PENDING}; {SHAPE}"),
    "test_coverage_sync_interfaces.py": (7, SHAPE),
    "test_coverage_sync_view.py": (5, f"{LEGACY}; {SHAPE}"),
    "test_coverage_utils.py": (8, f"{LEGACY}; {SHAPE}"),
    "test_data_shapes_capture.py": (1, SHAPE),
    "test_data_shapes_view.py": (1, SHAPE),
    "test_import_identity_replacement.py": (2, SHAPE),
    "test_import_server_mapping_link.py": (4, SHAPE),
    "test_import_server_selection.py": (1, PENDING),
    "test_import_utils.py": (5, f"{LEGACY}; {SHAPE}"),
    "test_interface_port_claim_race.py": (1, PENDING),
    "test_interface_relationship_change_log.py": (1, PENDING),
    "test_interface_relationship_refresh.py": (1, PENDING),
    "test_interface_rule_cables.py": (3, PENDING),
    "test_interface_tab_source_snapshot.py": (2, PENDING),
    "test_ip_sync_retry.py": (1, PENDING),
    "test_ip_tab_port_lookup.py": (1, SHAPE),
    "test_ip_tab_source_snapshot.py": (1, PENDING),
    "test_ip_verify.py": (3, PENDING),
    "test_ip_verify_interface_choice.py": (1, PENDING),
    "test_junos_virtual_chassis.py": (1, PENDING),
    "test_librenms_api.py": (2, LEGACY),
    "test_librenms_stub.py": (1, PENDING),
    "test_lock_conflict_middleware.py": (1, PENDING),
    "test_mapping_classifier.py": (2, SHAPE),
    "test_messages_container.py": (1, PENDING),
    "test_migrate_views.py": (1, SHAPE),
    "test_module_inventory_cache_contract.py": (3, f"{LEGACY}; {SHAPE}"),
    "test_multiserver_get_cache_scoping.py": (4, PENDING),
    "test_object_server_default_alias.py": (1, PENDING),
    "test_object_server_preference.py": (1, SHAPE),
    "test_object_server_selection.py": (14, f"{LEGACY}; {PENDING}"),
    "test_partial_save_change_log.py": (2, SHAPE),
    "test_recordings.py": (1, PENDING),
    "test_server_mappings.py": (64, f"{SHAPE}; {LEGACY}"),
    "test_sync_device_add_and_location.py": (3, PENDING),
    "test_sync_devices.py": (13, LEGACY),
    "test_sync_interface_concurrency.py": (1, PENDING),
    "test_sync_page_server_key_forms.py": (1, LEGACY),
    "test_sync_tab_page_size.py": (1, PENDING),
    "test_sync_view_mismatch.py": (3, f"{LEGACY}; {SHAPE}"),
    "test_sync_view_permission_gates.py": (1, PENDING),
    "test_utils.py": (6, f"{LEGACY}; {SHAPE}"),
    "test_utils_shared_helpers.py": (1, SHAPE),
    "test_validation_error_text_broad.py": (1, PENDING),
    "test_validation_error_text_device_fields.py": (8, f"{LEGACY}; {PENDING}"),
    "test_validation_error_text_imports.py": (1, PENDING),
    "test_validation_error_text_sync.py": (1, PENDING),
    "test_vlan_name_rules.py": (1, PENDING),
    "test_vm_operations.py": (1, PENDING),
}


def _name(node):
    return node.id if isinstance(node, ast.Name) else None


def _raw_callables(tree):
    """Return the local names that call a raw helper and the local names of a raw wrapper."""
    helpers, wrappers = set(), set(RAW_WRAPPERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == FIXTURES:
            helpers.update(alias.asname or alias.name for alias in node.names if alias.name in RAW_HELPERS)
    for node in ast.walk(tree):
        # A local alias, such as ``x = partial(map_device_to_librenms, ...)``.
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and _name(node.targets[0]):
            value = node.value
            if isinstance(value, ast.Call) and _name(value.func) == "partial" and value.args:
                value = value.args[0]
            if _name(value) in helpers:
                helpers.add(node.targets[0].id)
            elif _name(value) in wrappers:
                wrappers.add(node.targets[0].id)
    return helpers, wrappers


def _is_raw_call(call, helpers, wrappers):
    if _name(call.func) in helpers:
        return True
    if _name(call.func) in wrappers:
        # Without oob, or **kwargs that can hold it, the wrapper seeds an ordinary own id.
        return any(keyword.arg in ("oob", None) for keyword in call.keywords)
    # A call through the module, such as ``mapping_fixtures.seed_stored_mapping(...)``.
    return isinstance(call.func, ast.Attribute) and call.func.attr in RAW_HELPERS


def _raw_uses(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    helpers, wrappers = _raw_callables(tree)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            count += _is_raw_call(node, helpers, wrappers)
            count += sum(keyword.arg == "librenms_cf" for keyword in node.keywords)
    return count


def _scan():
    found = {}
    for path in sorted(TESTS.rglob("*.py")):
        if path.name in {"mapping_fixtures.py", Path(__file__).name}:
            continue
        count = _raw_uses(path)
        if count:
            found[path.relative_to(TESTS).as_posix()] = count
    return found


def _pinned(name):
    return PINNED.get(name, (0, ""))[0]


def test_no_file_adds_a_raw_mapping_value():
    grown = {name: (count, _pinned(name)) for name, count in _scan().items() if count > _pinned(name)}

    assert grown == {}, (
        "(found, pinned) raw uses: seed and read the mapping through seed_mapping, seed_unmapped, "
        "seed_quarantined, seed_mapping_row and mapping_summary."
    )


def test_every_pin_equals_the_raw_uses_found():
    found = _scan()

    stale = {
        name: (found.get(name, 0), pinned) for name, (pinned, _reason) in PINNED.items() if found.get(name, 0) < pinned
    }

    assert stale == {}, "(found, pinned) raw uses: lower the pin to the count found."
