"""Add InventoryIgnoreRule model and insert built-in IDPROM rule.

Cisco IOS-XR reports every hardware component's EEPROM as a child
ENTITY-MIB entry with the same model/serial as the parent (e.g.
"Optics0/0/0/0-IDPROM").  The built-in rule replicates the previous
hardcoded _is_idprom_entry() behaviour but is now admin-configurable.
"""

import utilities.json
from django.db import migrations, models


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
                            ("ends_with", "Ends with"),
                            ("starts_with", "Starts with"),
                            ("contains", "Contains"),
                            ("regex", "Regex"),
                        ],
                        default="ends_with",
                        max_length=20,
                    ),
                ),
                ("pattern", models.CharField(max_length=200)),
                ("require_serial_match_parent", models.BooleanField(default=True)),
                ("enabled", models.BooleanField(default=True)),
                ("description", models.TextField(blank=True)),
            ],
            options={
                "ordering": ["name", "pk"],
            },
        ),
        # Built-in default rule: replicates the previous hardcoded _is_idprom_entry()
        migrations.RunPython(
            code=lambda apps, schema_editor: apps.get_model(
                "netbox_librenms_plugin", "InventoryIgnoreRule"
            ).objects.create(
                name="Cisco IOS-XR IDPROM entries",
                match_type="ends_with",
                pattern="IDPROM",
                require_serial_match_parent=True,
                enabled=True,
                description=(
                    "Cisco IOS-XR reports every hardware component's EEPROM chip as a child "
                    "ENTITY-MIB entity with the same model name and serial number as the parent "
                    "(e.g. 'Optics0/0/0/0-IDPROM', '0/FT0-FT IDPROM', 'Rack 0-Chassis IDPROM'). "
                    "These are not installable modules. This rule replicates the previous "
                    "hardcoded _is_idprom_entry() behaviour."
                ),
            ),
            reverse_code=lambda apps, schema_editor: (
                apps.get_model("netbox_librenms_plugin", "InventoryIgnoreRule")
                .objects.filter(name="Cisco IOS-XR IDPROM entries")
                .delete()
            ),
        ),
    ]
