"""
Raw stored mapping values stay in the tests of the legacy runtime and of the storage shapes.

A behaviour test seeds and reads a mapping through the semantic helpers of ``mapping_fixtures``, so a
change of the storage changes the helpers and not the tests. The scan finds a direct call of a raw
helper and a ``librenms_cf=`` keyword. It does not follow a raw value through another local helper.
"""

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
RAW_HELPERS = frozenset({"seed_stored_mapping", "seed_stored_mapping_row", "stored_mapping_for_test"})
LEGACY = "legacy runtime: increment 5 deletes these tests"
SHAPE = "storage shape: stored forms, malformed entries or change records"

# The files that may call a raw helper, and why.
RAW_HELPER_FILES = {
    "conftest.py": "make_device(librenms_cf=...) and map_device_to_librenms store a raw value",
    "test_coverage_actions.py": f"{LEGACY}; {SHAPE}",
    "test_coverage_api.py": SHAPE,
    "test_coverage_device_fields.py": f"{LEGACY}; {SHAPE}",
    "test_coverage_device_operations.py": LEGACY,
    "test_coverage_sync_interfaces.py": SHAPE,
    "test_coverage_sync_view.py": f"{LEGACY}; {SHAPE}",
    "test_coverage_utils.py": f"{LEGACY}; {SHAPE}",
    "test_data_shapes_capture.py": SHAPE,
    "test_import_identity_replacement.py": SHAPE,
    "test_import_server_mapping_link.py": SHAPE,
    "test_import_utils.py": LEGACY,
    "test_ip_tab_port_lookup.py": SHAPE,
    "test_librenms_api.py": LEGACY,
    "test_mapping_classifier.py": SHAPE,
    "test_migrate_views.py": SHAPE,
    "test_module_inventory_cache_contract.py": SHAPE,
    "test_partial_save_change_log.py": SHAPE,
    "test_server_mapping_metadata.py": SHAPE,
    "test_server_mappings.py": SHAPE,
    "test_sync_devices.py": LEGACY,
    "test_sync_view_mismatch.py": f"{LEGACY}; {SHAPE}",
    "test_utils.py": f"{LEGACY}; {SHAPE}",
    "test_utils_shared_helpers.py": SHAPE,
}

# A follow-up converts these ordinary seeds to seed_mapping and removes librenms_cf from make_device.
PENDING = "ordinary mapping not yet seeded through seed_mapping"

# The files that may pass librenms_cf=, and why.
LIBRENMS_CF_FILES = {
    "test_coverage_actions.py": f"{LEGACY}; {SHAPE}",
    "test_coverage_api.py": SHAPE,
    "test_coverage_device_fields.py": f"{LEGACY}; {SHAPE}",
    "test_coverage_device_operations.py": f"{LEGACY}; {SHAPE}",
    "test_coverage_sync_view.py": f"{LEGACY}; {SHAPE}",
    "test_import_identity_replacement.py": SHAPE,
    "test_import_server_mapping_link.py": SHAPE,
    "test_import_utils.py": LEGACY,
    "test_ip_tab_port_lookup.py": PENDING,
    "test_librenms_api.py": f"{LEGACY}; {PENDING}",
    "test_module_inventory_cache_contract.py": SHAPE,
    "test_partial_save_change_log.py": f"{SHAPE}; {PENDING}",
    "test_server_mapping_metadata.py": SHAPE,
    "test_sync_devices.py": LEGACY,
    "test_data_shapes_view.py": SHAPE,
    "test_object_server_preference.py": SHAPE,
    "test_sync_page_server_key_forms.py": LEGACY,
    "test_validation_error_text_device_fields.py": f"{LEGACY}; {PENDING}",
    "test_coverage_base_views2.py": f"{SHAPE}; {PENDING}",
    "test_object_server_selection.py": f"{LEGACY}; {PENDING}",
    "test_multiserver_get_cache_scoping.py": PENDING,
    "test_acquisition_ip_addresses.py": PENDING,
    "test_acquisition_vlans.py": PENDING,
    "test_background_jobs.py": PENDING,
    "test_before_state_writes.py": PENDING,
    "test_bulk_import_job_control.py": PENDING,
    "test_cable_blocker_notifications.py": PENDING,
    "test_confirmation_intent_contract.py": PENDING,
    "test_import_server_selection.py": PENDING,
    "test_interface_port_claim_race.py": PENDING,
    "test_interface_relationship_change_log.py": PENDING,
    "test_interface_relationship_refresh.py": PENDING,
    "test_ip_sync_retry.py": PENDING,
    "test_ip_tab_source_snapshot.py": PENDING,
    "test_ip_verify.py": PENDING,
    "test_ip_verify_interface_choice.py": PENDING,
    "test_junos_virtual_chassis.py": PENDING,
    "test_librenms_stub.py": PENDING,
    "test_lock_conflict_middleware.py": PENDING,
    "test_messages_container.py": PENDING,
    "test_object_server_default_alias.py": PENDING,
    "test_recordings.py": PENDING,
    "test_sync_device_add_and_location.py": PENDING,
    "test_sync_interface_concurrency.py": PENDING,
    "test_sync_tab_page_size.py": PENDING,
    "test_sync_view_permission_gates.py": PENDING,
    "test_validation_error_text_broad.py": PENDING,
    "test_validation_error_text_imports.py": PENDING,
    "test_validation_error_text_sync.py": PENDING,
    "test_vlan_name_rules.py": PENDING,
    "test_vm_operations.py": PENDING,
}


def _called_name(call):
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _raw_uses(path):
    """Return the raw helper names and the librenms_cf keyword lines that *path* uses."""
    helpers, keywords = set(), []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if not isinstance(node, ast.Call):
            continue
        if _called_name(node) in RAW_HELPERS:
            helpers.add(_called_name(node))
        keywords.extend(node.lineno for keyword in node.keywords if keyword.arg == "librenms_cf")
    return helpers, keywords


def _scan():
    found = {}
    for path in sorted(TESTS.rglob("*.py")):
        if path.name in {"mapping_fixtures.py", Path(__file__).name}:
            continue
        helpers, keywords = _raw_uses(path)
        if helpers or keywords:
            found[path.relative_to(TESTS).as_posix()] = (helpers, keywords)
    return found


def test_only_legacy_and_storage_shape_tests_use_raw_mapping_values():
    found = _scan()

    raw_helpers = {name: sorted(helpers) for name, (helpers, _lines) in found.items() if helpers}
    raw_keywords = {name: lines for name, (_helpers, lines) in found.items() if lines}

    assert {name: uses for name, uses in raw_helpers.items() if name not in RAW_HELPER_FILES} == {}, (
        "Seed and read the mapping through seed_mapping, seed_unmapped, seed_quarantined and mapping_summary."
    )
    assert {name: lines for name, lines in raw_keywords.items() if name not in LIBRENMS_CF_FILES} == {}, (
        "Seed the mapping with seed_mapping(make_device(...), server, own=...), not librenms_cf=."
    )


def test_the_allowlists_name_only_files_that_still_use_raw_values():
    found = _scan()

    assert sorted(set(RAW_HELPER_FILES) - {name for name, (helpers, _lines) in found.items() if helpers}) == []
    assert sorted(set(LIBRENMS_CF_FILES) - {name for name, (_helpers, lines) in found.items() if lines}) == []
