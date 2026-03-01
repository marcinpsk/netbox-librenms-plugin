"""Restore NormalizationRule model.

The table was created by earlier migrations (0013 + 0014 in a previous branch)
and already exists in the database.  This migration uses SeparateDatabaseAndState
so Django's ORM knows about the model without trying to CREATE the table again.
If the table doesn't exist (fresh install), the database_operations handle creation.
"""

import django.db.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dcim", "0001_initial"),
        ("extras", "0001_initial"),
        ("netbox_librenms_plugin", "0012_add_is_regex_to_modulebaymapping"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
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
                        (
                            "tags",
                            taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag"),
                        ),
                    ],
                    options={
                        "ordering": ["scope", "priority", "pk"],
                    },
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    CREATE TABLE IF NOT EXISTS "netbox_librenms_plugin_normalizationrule" (
                        "id" bigserial NOT NULL PRIMARY KEY,
                        "created" timestamp with time zone NULL,
                        "last_updated" timestamp with time zone NULL,
                        "custom_field_data" jsonb NOT NULL DEFAULT '{}'::jsonb,
                        "scope" varchar(50) NOT NULL,
                        "match_pattern" varchar(500) NOT NULL,
                        "replacement" varchar(500) NOT NULL,
                        "priority" integer NOT NULL DEFAULT 100 CHECK ("priority" >= 0),
                        "description" text NOT NULL DEFAULT '',
                        "manufacturer_id" bigint NULL REFERENCES "dcim_manufacturer" ("id")
                            DEFERRABLE INITIALLY DEFERRED
                    );
                    CREATE INDEX IF NOT EXISTS "netbox_librenms_plugin_norm_mfg_idx"
                        ON "netbox_librenms_plugin_normalizationrule" ("manufacturer_id");
                    """,
                    reverse_sql="DROP TABLE IF EXISTS netbox_librenms_plugin_normalizationrule;",
                ),
            ],
        ),
    ]
