from django.db import migrations, models

import utilities.json


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0012_interfacenamerule"),
    ]

    operations = [
        migrations.CreateModel(
            name="NormalizationRule",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "created",
                    models.DateTimeField(auto_now_add=True, null=True),
                ),
                (
                    "last_updated",
                    models.DateTimeField(auto_now=True, null=True),
                ),
                (
                    "custom_field_data",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=utilities.json.CustomFieldJSONEncoder,
                    ),
                ),
                (
                    "scope",
                    models.CharField(
                        choices=[
                            ("module_type", "Module Type"),
                            ("device_type", "Device Type"),
                            ("module_bay", "Module Bay"),
                        ],
                        help_text="Which matching lookup this rule applies to",
                        max_length=50,
                    ),
                ),
                (
                    "match_pattern",
                    models.CharField(
                        help_text="Regex pattern to match against input string (Python re syntax)",
                        max_length=500,
                    ),
                ),
                (
                    "replacement",
                    models.CharField(
                        help_text="Replacement string (supports regex back-references \\1, \\2, …)",
                        max_length=500,
                    ),
                ),
                (
                    "priority",
                    models.PositiveIntegerField(
                        default=100,
                        help_text="Lower values run first. Rules chain: each transforms the output of the previous.",
                    ),
                ),
                (
                    "description",
                    models.TextField(
                        blank=True,
                        help_text="Optional description or notes about this rule",
                    ),
                ),
                (
                    "tags",
                    models.ManyToManyField(blank=True, to="extras.tag"),
                ),
            ],
            options={
                "ordering": ["scope", "priority", "pk"],
            },
        ),
    ]
