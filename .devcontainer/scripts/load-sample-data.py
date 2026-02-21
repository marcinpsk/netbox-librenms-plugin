#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
#
# Load sample data from contrib/ YAML files into the devcontainer NetBox.
# Run via: python manage.py shell < /path/to/load-sample-data.py
# Or:      python manage.py shell -c "exec(open('/path/to/load-sample-data.py').read())"

import os

import yaml

CONTRIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "contrib")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_yaml(filename):
    path = os.path.join(CONTRIB_DIR, filename)
    if not os.path.exists(path):
        print(f"  ⚠️  File not found: {path} — skipping")
        return []
    with open(path) as f:
        data = yaml.safe_load(f)
    return [r for r in (data or []) if isinstance(r, dict)]


def ok(label):
    print(f"  ✓ {label}")


def skip(label, reason):
    print(f"  · {label} — {reason}")


# ---------------------------------------------------------------------------
# Interface Type Mappings (no FK dependencies beyond choices)
# ---------------------------------------------------------------------------


def load_interface_type_mappings():
    from netbox_librenms_plugin.models import InterfaceTypeMapping

    print("📋 Loading interface type mappings…")
    rows = load_yaml("interface_type_mappings.yaml")
    created = updated = skipped = 0
    for row in rows:
        librenms_type = row.get("librenms_type", "")
        librenms_speed = row.get("librenms_speed")
        netbox_type = row.get("netbox_type", "")
        description = row.get("description", "")
        if not librenms_type or not netbox_type:
            skip(f"{librenms_type}", "missing required fields")
            skipped += 1
            continue
        obj, was_created = InterfaceTypeMapping.objects.update_or_create(
            librenms_type=librenms_type,
            librenms_speed=librenms_speed,
            defaults={"netbox_type": netbox_type, "description": description},
        )
        if was_created:
            ok(f"{librenms_type} → {netbox_type}")
            created += 1
        else:
            updated += 1
    print(f"  → {created} created, {updated} updated, {skipped} skipped\n")


# ---------------------------------------------------------------------------
# Module Bay Mappings (no FK dependencies)
# ---------------------------------------------------------------------------


def load_module_bay_mappings():
    from netbox_librenms_plugin.models import ModuleBayMapping

    print("📋 Loading module bay mappings…")
    rows = load_yaml("module_bay_mappings.yaml")
    created = updated = skipped = 0
    for row in rows:
        librenms_name = row.get("librenms_name", "")
        netbox_bay_name = row.get("netbox_bay_name", "")
        librenms_class = row.get("librenms_class") or ""
        is_regex = bool(row.get("is_regex", False))
        description = row.get("description", "")
        if not librenms_name or not netbox_bay_name:
            skip(f"{librenms_name}", "missing required fields")
            skipped += 1
            continue
        obj, was_created = ModuleBayMapping.objects.update_or_create(
            librenms_name=librenms_name,
            librenms_class=librenms_class,
            defaults={
                "netbox_bay_name": netbox_bay_name,
                "is_regex": is_regex,
                "description": description,
            },
        )
        if was_created:
            ok(f"{librenms_name!r} → {netbox_bay_name!r}")
            created += 1
        else:
            updated += 1
    print(f"  → {created} created, {updated} updated, {skipped} skipped\n")


# ---------------------------------------------------------------------------
# Normalization Rules (manufacturer FK is optional/soft — skip if not found)
# ---------------------------------------------------------------------------


def load_normalization_rules():
    from dcim.models import Manufacturer
    from netbox_librenms_plugin.models import NormalizationRule

    print("📋 Loading normalization rules…")
    rows = load_yaml("normalization_rules.yaml")
    created = updated = skipped = 0
    for row in rows:
        scope = row.get("scope", "")
        match_pattern = row.get("match_pattern", "")
        replacement = row.get("replacement", "")
        priority = int(row.get("priority", 100))
        description = row.get("description", "")
        manufacturer_name = row.get("manufacturer")
        if not scope or not match_pattern:
            skip(f"scope={scope}", "missing required fields")
            skipped += 1
            continue
        manufacturer = None
        if manufacturer_name:
            try:
                manufacturer = Manufacturer.objects.get(name=manufacturer_name)
            except Manufacturer.DoesNotExist:
                skip(f"{scope}/{match_pattern}", f"manufacturer {manufacturer_name!r} not found — skipping")
                skipped += 1
                continue
        obj, was_created = NormalizationRule.objects.update_or_create(
            scope=scope,
            match_pattern=match_pattern,
            manufacturer=manufacturer,
            defaults={
                "replacement": replacement,
                "priority": priority,
                "description": description,
            },
        )
        if was_created:
            ok(f"{scope} {match_pattern!r} → {replacement!r}")
            created += 1
        else:
            updated += 1
    print(f"  → {created} created, {updated} updated, {skipped} skipped\n")


# ---------------------------------------------------------------------------
# Device Type Mappings (DeviceType FK — skip if not found)
# ---------------------------------------------------------------------------


def load_device_type_mappings():
    from dcim.models import DeviceType
    from netbox_librenms_plugin.models import DeviceTypeMapping

    print("📋 Loading device type mappings…")
    rows = load_yaml("device_type_mappings.yaml")
    created = updated = skipped = 0
    for row in rows:
        librenms_hardware = row.get("librenms_hardware", "")
        netbox_device_type_name = row.get("netbox_device_type", "")
        description = row.get("description", "")
        if not librenms_hardware or not netbox_device_type_name:
            skip(f"{librenms_hardware}", "missing required fields")
            skipped += 1
            continue
        try:
            device_type = DeviceType.objects.get(model=netbox_device_type_name)
        except DeviceType.DoesNotExist:
            skip(f"{librenms_hardware}", f"DeviceType {netbox_device_type_name!r} not found")
            skipped += 1
            continue
        obj, was_created = DeviceTypeMapping.objects.update_or_create(
            librenms_hardware=librenms_hardware,
            defaults={"netbox_device_type": device_type, "description": description},
        )
        if was_created:
            ok(f"{librenms_hardware!r} → {netbox_device_type_name!r}")
            created += 1
        else:
            updated += 1
    print(f"  → {created} created, {updated} updated, {skipped} skipped\n")


# ---------------------------------------------------------------------------
# Module Type Mappings (ModuleType FK — skip if not found)
# ---------------------------------------------------------------------------


def load_module_type_mappings():
    from dcim.models import ModuleType
    from netbox_librenms_plugin.models import ModuleTypeMapping

    print("📋 Loading module type mappings…")
    rows = load_yaml("module_type_mappings.yaml")
    created = updated = skipped = 0
    for row in rows:
        librenms_model = row.get("librenms_model", "")
        netbox_module_type_name = row.get("netbox_module_type", "")
        description = row.get("description", "")
        if not librenms_model or not netbox_module_type_name:
            skip(f"{librenms_model}", "missing required fields")
            skipped += 1
            continue
        try:
            module_type = ModuleType.objects.get(model=netbox_module_type_name)
        except ModuleType.DoesNotExist:
            skip(f"{librenms_model}", f"ModuleType {netbox_module_type_name!r} not found")
            skipped += 1
            continue
        obj, was_created = ModuleTypeMapping.objects.update_or_create(
            librenms_model=librenms_model,
            defaults={"netbox_module_type": module_type, "description": description},
        )
        if was_created:
            ok(f"{librenms_model!r} → {netbox_module_type_name!r}")
            created += 1
        else:
            updated += 1
    print(f"  → {created} created, {updated} updated, {skipped} skipped\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

print("🗂  Loading LibreNMS plugin sample data from contrib/")
print()

load_interface_type_mappings()
load_module_bay_mappings()
load_normalization_rules()
load_device_type_mappings()
load_module_type_mappings()

print("✅ Done loading sample data.")
