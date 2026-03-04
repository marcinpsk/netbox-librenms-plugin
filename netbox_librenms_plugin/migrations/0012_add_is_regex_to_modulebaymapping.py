from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0011_modulebaymapping"),
    ]

    operations = [
        migrations.AddField(
            model_name="modulebaymapping",
            name="is_regex",
            field=models.BooleanField(
                default=False,
                help_text="Treat LibreNMS Name as a regex pattern with backreferences in NetBox Bay Name",
            ),
        ),
    ]
