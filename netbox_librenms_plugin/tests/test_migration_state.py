"""Guard against model/migration state drift for the plugin's own models."""

import importlib

import pytest


def test_migration_0013_field_help_text_matches_model():
    """Migration 0013's PortStackLagPattern fields must carry the same help_text as the model (else the migration state drifts and makemigrations tracks a phantom AlterField)."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    # Migration modules start with a digit (not a valid identifier), so import by string.
    mod = importlib.import_module("netbox_librenms_plugin.migrations.0013_portstacklagpattern")
    create_op = next(
        op
        for op in mod.Migration.operations
        if op.__class__.__name__ == "CreateModel" and op.name == "PortStackLagPattern"
    )
    migration_fields = dict(create_op.fields)

    for field_name in ("librenms_os", "lag_name_pattern"):
        model_help = PortStackLagPattern._meta.get_field(field_name).help_text
        assert migration_fields[field_name].help_text == model_help, (
            f"{field_name}: migration help_text drifted from the model"
        )


def test_migration_0014_librenms_os_help_text_matches_model():
    """Migration 0014 re-declares librenms_os via AlterField, so 0014 (not 0013's CreateModel) is the authoritative migration state makemigrations compares librenms_os against — its help_text must match the model too."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    mod = importlib.import_module("netbox_librenms_plugin.migrations.0014_portstacklagpattern_ci_unique")
    alter_op = next(
        op
        for op in mod.Migration.operations
        if op.__class__.__name__ == "AlterField" and op.model_name == "portstacklagpattern" and op.name == "librenms_os"
    )
    model_help = PortStackLagPattern._meta.get_field("librenms_os").help_text
    assert alter_op.field.help_text == model_help, "0014 AlterField librenms_os help_text drifted from the model"


@pytest.mark.django_db
def test_plugin_migrations_do_not_redeclare_squashed_core_ancestors():
    """A plugin migration must not repeat a squashed core dependency from its plugin parent."""
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection, ignore_no_migrations=True)
    plugin_migrations = {
        key: migration for key, migration in loader.disk_migrations.items() if key[0] == "netbox_librenms_plugin"
    }
    assert plugin_migrations, "No plugin migrations were loaded"

    for migration_key, migration in plugin_migrations.items():
        plugin_parents = [dependency for dependency in migration.dependencies if dependency[0] == migration_key[0]]
        inherited_dependencies = {
            ancestor for parent in plugin_parents for ancestor in loader.graph.forwards_plan(parent)
        }
        for dependency in migration.dependencies:
            dependency_migration = loader.disk_migrations.get(dependency)
            if (
                dependency[0] != migration_key[0]
                and dependency in inherited_dependencies
                and dependency_migration is not None
                and dependency_migration.replaces
            ):
                raise AssertionError(
                    f"{migration_key} repeats squashed core dependency {dependency}; "
                    "the plugin parent already reaches it"
                )


def test_migration_0017_serial_sensor_field_help_text_matches_model():
    """Same drift guard for SerialSensorTypePattern: migration 0017's fields must carry the model's help_text, else makemigrations tracks a phantom AlterField."""
    from netbox_librenms_plugin.models import SerialSensorTypePattern

    mod = importlib.import_module("netbox_librenms_plugin.migrations.0017_serialsensortypepattern")
    create_op = next(
        op
        for op in mod.Migration.operations
        if op.__class__.__name__ == "CreateModel" and op.name == "SerialSensorTypePattern"
    )
    migration_fields = dict(create_op.fields)

    for field_name in ("sensor_type", "port_name_pattern"):
        model_help = SerialSensorTypePattern._meta.get_field(field_name).help_text
        assert migration_fields[field_name].help_text == model_help, (
            f"{field_name}: migration help_text drifted from the model"
        )


def test_migration_0018_librenms_settings_field_help_text_matches_model():
    """Same drift guard for LibreNMSSettings's cable-sync fields: migration 0018's AddField ops must carry the model's help_text, else makemigrations tracks a phantom AlterField."""
    from netbox_librenms_plugin.models import LibreNMSSettings

    mod = importlib.import_module("netbox_librenms_plugin.migrations.0018_librenmssettings_cable_sync")

    for field_name in ("cable_sync_tag", "cable_sync_tag_color", "cable_sync_description"):
        add_op = next(
            op
            for op in mod.Migration.operations
            if op.__class__.__name__ == "AddField" and op.model_name == "librenmssettings" and op.name == field_name
        )
        model_help = LibreNMSSettings._meta.get_field(field_name).help_text
        assert add_op.field.help_text == model_help, f"{field_name}: migration help_text drifted from the model"


@pytest.mark.django_db
def test_conftest_restores_exactly_the_rules_migration_0010_seeds():
    """The seed-restore signatures in conftest must match what the migration's own insert produces.

    conftest re-runs the migration's insert to repair a transactional flush, but its intactness
    check compares signatures it declares itself. Pin the two against each other so a change to
    the migration's seeded rules cannot silently leave the restore reporting "intact".
    """
    from types import SimpleNamespace

    from django.apps import apps as global_apps

    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.tests.conftest import _seeded_ignore_rule_signatures

    mod = importlib.import_module("netbox_librenms_plugin.migrations.0010_inventory_and_mapping_models")
    schema_editor = SimpleNamespace(connection=SimpleNamespace(alias="default"))
    signature_fields = ("name", "match_type", "pattern", "action", "require_serial_match_parent")

    InventoryIgnoreRule.objects.all().delete()
    mod._insert_default_inventory_ignore_rules(global_apps, schema_editor)

    produced = set(InventoryIgnoreRule.objects.values_list(*signature_fields))
    declared = {
        tuple(signature[field] for field in signature_fields) for _model, signature in _seeded_ignore_rule_signatures()
    }

    assert produced == declared


@pytest.mark.django_db
def test_the_seeded_ignore_rules_survive_a_seed_restore():
    """restore_seeded_state() must put migration 0010's rules back after a flush removes them."""
    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.tests.conftest import restore_seeded_state

    InventoryIgnoreRule.objects.all().delete()

    assert restore_seeded_state(force=False) is True
    assert InventoryIgnoreRule.objects.count() == 2
    # A second pass must not duplicate them.
    restore_seeded_state(force=True)
    assert InventoryIgnoreRule.objects.count() == 2


@pytest.mark.django_db
def test_the_seeded_ignore_rules_are_present_before_a_test_body_runs():
    """Every test starts with migration 0010's rules, including one that follows a flush.

    A ``transaction=True`` test truncates the tables, so without the restore this passes or fails
    purely on xdist scheduling: the modules sync reads these rules on every render.
    """
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    assert InventoryIgnoreRule.objects.count() == 2
