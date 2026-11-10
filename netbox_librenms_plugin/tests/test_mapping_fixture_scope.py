"""
Raw stored mapping values stay in the tests of the legacy runtime and of the storage shapes.

A behaviour test seeds and reads a mapping through the semantic helpers of ``mapping_fixtures``, so a
change of the storage changes the helpers and not the tests. The scan counts, per file, each call of
a raw helper (through an import alias, a module attribute or a local alias too) and each
``update_fields`` that names the storage field. It does not follow a raw value through any other
local helper.
Limits: it does not see ``partial`` under another name, and it keeps local aliases per file, not
per function.
"""

import ast
import textwrap
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
FIXTURES = "netbox_librenms_plugin.tests.mapping_fixtures"
RAW_HELPERS = frozenset({"seed_stored_mapping", "seed_stored_mapping_row", "stored_mapping_for_test"})
# A save that names it writes the stored mapping past the fixtures.
STORAGE_FIELD = "custom_field_data"

LEGACY = "LEGACY: the legacy runtime, which increment 5 deletes with these tests"
SHAPE = "SHAPE: stored forms, malformed entries, change records or the seam's own encoding"

# The pinned number of raw uses per file, and why. A pin can only go down.
PINNED = {
    "test_acquisition_cables.py": (1, SHAPE),
    "test_acquisition_interfaces.py": (1, SHAPE),
    "test_acquisition_modules.py": (1, SHAPE),
    "test_coverage_actions.py": (43, f"{LEGACY}; {SHAPE}"),
    "test_coverage_api.py": (4, SHAPE),
    "test_coverage_base_views2.py": (1, SHAPE),
    "test_coverage_cables_view_paths.py": (1, SHAPE),
    "test_coverage_device_fields.py": (24, f"{LEGACY}; {SHAPE}"),
    "test_coverage_device_operations.py": (2, LEGACY),
    "test_coverage_interfaces_view_refresh.py": (1, SHAPE),
    "test_coverage_sync_interfaces.py": (7, SHAPE),
    "test_coverage_sync_view.py": (5, f"{LEGACY}; {SHAPE}"),
    "test_coverage_utils.py": (8, f"{LEGACY}; {SHAPE}"),
    "test_data_shapes_capture.py": (1, SHAPE),
    "test_data_shapes_view.py": (1, SHAPE),
    "test_import_identity_replacement.py": (2, SHAPE),
    "test_import_server_mapping_link.py": (4, SHAPE),
    "test_import_utils.py": (5, f"{LEGACY}; {SHAPE}"),
    "test_interface_tab_source_snapshot.py": (1, SHAPE),
    "test_ip_tab_port_lookup.py": (1, SHAPE),
    "test_librenms_api.py": (2, LEGACY),
    "test_mapping_classifier.py": (2, SHAPE),
    "test_migrate_views.py": (1, SHAPE),
    "test_module_inventory_cache_contract.py": (3, f"{LEGACY}; {SHAPE}"),
    "test_object_server_preference.py": (1, SHAPE),
    "test_object_server_selection.py": (2, f"{LEGACY}; {SHAPE}"),
    "test_partial_save_change_log.py": (2, SHAPE),
    "test_server_mappings.py": (64, f"{SHAPE}; {LEGACY}"),
    "test_sync_devices.py": (13, LEGACY),
    "test_sync_page_server_key_forms.py": (1, LEGACY),
    "test_sync_view_mismatch.py": (3, f"{LEGACY}; {SHAPE}"),
    "test_utils.py": (6, f"{LEGACY}; {SHAPE}"),
    "test_utils_shared_helpers.py": (1, SHAPE),
    "test_validation_error_text_device_fields.py": (2, LEGACY),
}


def _callee(node):
    """Return the called name: a bare name, or the last part of a dotted name."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _raw_helpers(tree):
    """
    Return the names that call a raw helper.

    A name is the helper itself, an import alias, or a local alias such as ``x = partial(helper, ...)``.
    A dotted call matches by its last name.
    """
    helpers = set(RAW_HELPERS)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.name.rpartition(".")[2]
                if name in RAW_HELPERS:
                    helpers.add(alias.asname or name)
    aliases = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
    ]
    # An alias can name an earlier alias, so resolve the chain.
    for _ in range(len(aliases)):
        for node in aliases:
            value = node.value
            if isinstance(value, ast.Call) and _callee(value.func) == "partial" and value.args:
                value = value.args[0]
            if _callee(value) in helpers:
                helpers.add(node.targets[0].id)
    return helpers


def _names_storage_field(keyword):
    if keyword.arg != "update_fields" or not isinstance(keyword.value, (ast.List, ast.Tuple, ast.Set)):
        return False
    return any(isinstance(item, ast.Constant) and item.value == STORAGE_FIELD for item in keyword.value.elts)


def _count_raw_uses(source):
    tree = ast.parse(source)
    helpers = _raw_helpers(tree)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            count += _callee(node.func) in helpers
            count += any(_names_storage_field(keyword) for keyword in node.keywords)
    return count


def _scan():
    found = {}
    for path in sorted(TESTS.rglob("*.py")):
        if path.name in {"mapping_fixtures.py", Path(__file__).name}:
            continue
        count = _count_raw_uses(path.read_text())
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


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("seed_stored_mapping(device, {'default': 1})", 1),
        ("from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping as raw\nraw(device, 1)", 1),
        (
            "from netbox_librenms_plugin.tests import mapping_fixtures as fixtures\nfixtures.stored_mapping_for_test(device)",
            1,
        ),
        ("raw = seed_stored_mapping_row\nraw(device, 1)", 1),
        ("bound = partial(seed_stored_mapping, save=True)\nraw = bound\nraw(device, 1)", 1),
        ("device.save(update_fields=['custom_field_data'])", 1),
        ("device.save(update_fields=('custom_field_data', 'parent'))", 1),
        ("device.save(update_fields=['parent'])", 0),
        ("seed_mapping(make_device('x'), own=1, oob=2)", 0),
    ],
    ids=[
        "helper",
        "helper-import-alias",
        "helper-module-attribute",
        "helper-local-alias",
        "alias-of-a-partial",
        "update-fields",
        "update-fields-with-another-field",
        "update-fields-without-the-storage-field",
        "semantic-seed",
    ],
)
def test_the_scan_counts_each_raw_form(source, expected):
    assert _count_raw_uses(textwrap.dedent(source)) == expected
