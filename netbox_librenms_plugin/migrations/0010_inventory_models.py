"""
Add inventory/modules sync models: DeviceTypeMapping, ModuleTypeMapping,
ModuleBayMapping, NormalizationRule, and InventoryIgnoreRule, along with
two default InventoryIgnoreRule entries.
"""

import django.db.models.deletion
import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


def _insert_default_rules(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    InventoryIgnoreRule = apps.get_model("netbox_librenms_plugin", "InventoryIgnoreRule")
    InventoryIgnoreRule.objects.using(db_alias).create(
        name="Cisco IOS-XR IDPROM entries",
        match_type="ends_with",
        pattern="IDPROM",
        action="skip",
        require_serial_match_parent=True,
        enabled=True,
        description=(
            "Cisco IOS-XR reports every hardware component's EEPROM as a child entity "
            'whose entPhysicalName ends in "IDPROM". These entries duplicate the parent '
            "module's serial number and are not real installable modules. "
            "The serial-match guard ensures only genuine EEPROM duplicates are skipped — "
            'a module whose name happens to end in "IDPROM" but has a different serial '
            "will not be filtered."
        ),
    )
    InventoryIgnoreRule.objects.using(db_alias).create(
        name="Embedded RP / fixed-chassis system board",
        match_type="serial_matches_device",
        pattern="",
        action="transparent",
        require_serial_match_parent=False,
        enabled=True,
        description=(
            "Fixed-form routers report the built-in RP as an ENTITY-MIB module whose "
            "serial number equals the device's own serial. Marking it transparent hides "
            "the RP row in the sync table while promoting its children (transceivers, "
            "fans, PSUs) to device-level bay matching. No pattern is needed — detection "
            "is purely serial-based."
        ),
    )


def _delete_default_rules(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    InventoryIgnoreRule = apps.get_model("netbox_librenms_plugin", "InventoryIgnoreRule")
    # Delete only the exact seeded defaults — match on ALL seeded fields so
    # admin-created rules that happen to share a name/pattern are not accidentally removed.
    InventoryIgnoreRule.objects.using(db_alias).filter(
        name="Cisco IOS-XR IDPROM entries",
        match_type="ends_with",
        pattern="IDPROM",
        action="skip",
        require_serial_match_parent=True,
        enabled=True,
    ).delete()
    InventoryIgnoreRule.objects.using(db_alias).filter(
        name="Embedded RP / fixed-chassis system board",
        match_type="serial_matches_device",
        pattern="",
        action="transparent",
        require_serial_match_parent=False,
        enabled=True,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("dcim", "0001_initial"),
        ("extras", "0001_initial"),
        ("netbox_librenms_plugin", "0009_convert_librenms_id_to_json"),
    ]

    operations = [
        # DeviceTypeMapping
        migrations.CreateModel(
            name="DeviceTypeMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("librenms_hardware", models.CharField(max_length=255, unique=True)),
                ("description", models.TextField(blank=True)),
                (
                    "netbox_device_type",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="librenms_mappings",
                        to="dcim.devicetype",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "ordering": ["librenms_hardware"],
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        # InterfaceTypeMapping ordering
        migrations.AlterModelOptions(
            name="interfacetypemapping",
            options={"ordering": ["librenms_type", "librenms_speed"]},
        ),
        # ModuleTypeMapping
        migrations.CreateModel(
            name="ModuleTypeMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("librenms_model", models.CharField(max_length=255, unique=True)),
                ("description", models.TextField(blank=True)),
                (
                    "netbox_module_type",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="librenms_mappings",
                        to="dcim.moduletype",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "ordering": ["librenms_model"],
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        # ModuleBayMapping (with is_regex included)
        migrations.CreateModel(
            name="ModuleBayMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("librenms_name", models.CharField(max_length=255)),
                ("librenms_class", models.CharField(blank=True, max_length=50)),
                ("netbox_bay_name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
                (
                    "is_regex",
                    models.BooleanField(
                        default=False,
                        help_text="Treat LibreNMS Name as a regex pattern with backreferences in NetBox Bay Name",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "ordering": ["librenms_name"],
                "unique_together": {("librenms_name", "librenms_class")},
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        # NormalizationRule
        migrations.CreateModel(
            name="NormalizationRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                (
                    "scope",
                    models.CharField(
                        choices=[
                            ("module_type", "Module Type"),
                            ("device_type", "Device Type"),
                            ("module_bay", "Module Bay"),
                        ],
                        max_length=50,
                    ),
                ),
                ("match_pattern", models.CharField(max_length=500)),
                ("replacement", models.CharField(max_length=500)),
                ("priority", models.PositiveIntegerField(default=100)),
                ("description", models.TextField(blank=True)),
                (
                    "manufacturer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="normalization_rules",
                        to="dcim.manufacturer",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "ordering": ["scope", "priority", "pk"],
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        # InventoryIgnoreRule
        migrations.CreateModel(
            name="InventoryIgnoreRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("name", models.CharField(max_length=100)),
                (
                    "match_type",
                    models.CharField(
                        choices=[
                            ("ends_with", "Ends with (entPhysicalName)"),
                            ("starts_with", "Starts with (entPhysicalName)"),
                            ("contains", "Contains (entPhysicalName)"),
                            ("regex", "Regex (entPhysicalName)"),
                            ("serial_matches_device", "Serial matches device (entPhysicalSerialNum = Device.serial)"),
                        ],
                        default="ends_with",
                        max_length=25,
                    ),
                ),
                ("pattern", models.CharField(blank=True, max_length=200)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("skip", "Skip (remove from table)"),
                            ("transparent", "Transparent (hide row, promote children to device level)"),
                        ],
                        default="skip",
                        max_length=15,
                    ),
                ),
                ("require_serial_match_parent", models.BooleanField(default=True)),
                ("enabled", models.BooleanField(default=True)),
                ("description", models.TextField(blank=True)),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "ordering": ["name", "pk"],
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.RunPython(code=_insert_default_rules, reverse_code=_delete_default_rules),
    ]
