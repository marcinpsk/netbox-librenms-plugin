"""Add InventoryIgnoreRule model and insert built-in default rules.

Two built-in rules are created:

1. **Cisco IOS-XR IDPROM entries** (ends_with "IDPROM", action=skip, require_serial_match_parent=True)
   Cisco IOS-XR reports every hardware component's EEPROM as a child ENTITY-MIB entry
   with the same model/serial as the parent (e.g. "Optics0/0/0/0-IDPROM").
   Replaces the previous hardcoded _is_idprom_entry() helper.

2. **Embedded RP / fixed-chassis system board** (serial_matches_device, action=transparent)
   Fixed-form routers (e.g. Cisco 8201-SYS) report the system board as an ENTITY-MIB
   module entry whose serial number equals the device's own serial.  Marking it
   "transparent" hides its row while promoting its children (transceivers, fans, PSUs)
   to device-level bay matching.

Updated in-place (no new migration file) because the plugin has not been deployed
outside the development container at this point.
"""

import utilities.json
from django.db import migrations, models


def _insert_default_rules(apps, schema_editor):
    InventoryIgnoreRule = apps.get_model("netbox_librenms_plugin", "InventoryIgnoreRule")
    InventoryIgnoreRule.objects.create(
        name="Cisco IOS-XR IDPROM entries",
        match_type="ends_with",
        pattern="IDPROM",
        action="skip",
        require_serial_match_parent=True,
        enabled=True,
        description=(
            "Cisco IOS-XR reports every hardware component's EEPROM chip as a child "
            "ENTITY-MIB entity with the same model name and serial number as the parent "
            "(e.g. 'Optics0/0/0/0-IDPROM', '0/FT0-FT IDPROM', 'Rack 0-Chassis IDPROM'). "
            "These are not installable modules. This rule replicates the previous "
            "hardcoded _is_idprom_entry() behaviour."
        ),
    )
    InventoryIgnoreRule.objects.create(
        name="Embedded RP / fixed-chassis system board",
        match_type="serial_matches_device",
        pattern="",
        action="transparent",
        require_serial_match_parent=False,
        enabled=True,
        description=(
            "Fixed-form routers (e.g. Cisco 8201-SYS, 8100 series) report the system board as "
            "an ENTITY-MIB module entry whose serial number equals the device's own serial. "
            "Marking the entry 'transparent' hides its row in the sync table while promoting "
            "its children (transceivers, fans, PSUs) to device-level bay matching."
        ),
    )


def _delete_default_rules(apps, schema_editor):
    apps.get_model("netbox_librenms_plugin", "InventoryIgnoreRule").objects.filter(
        name__in=[
            "Cisco IOS-XR IDPROM entries",
            "Embedded RP / fixed-chassis system board",
        ]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("extras", "0001_initial"),
        ("netbox_librenms_plugin", "0013_normalizationrule"),
    ]

    operations = [
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
            ],
            options={
                "ordering": ["name", "pk"],
            },
        ),
        migrations.RunPython(code=_insert_default_rules, reverse_code=_delete_default_rules),
    ]
