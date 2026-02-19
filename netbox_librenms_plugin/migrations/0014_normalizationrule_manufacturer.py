import django.db.models.deletion
import taggit.managers
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dcim", "0225_gfk_indexes"),
        ("extras", "0134_owner"),
        ("netbox_librenms_plugin", "0013_normalizationrule"),
    ]

    operations = [
        migrations.AddField(
            model_name="normalizationrule",
            name="manufacturer",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Optional: only apply this rule to items from this "
                    "manufacturer. Leave blank for vendor-agnostic rules."
                ),
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="normalization_rules",
                to="dcim.manufacturer",
            ),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="normalizationrule",
                    name="tags",
                    field=taggit.managers.TaggableManager(
                        through="extras.TaggedItem",
                        to="extras.Tag",
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]
