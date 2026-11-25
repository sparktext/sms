from django.db import migrations, models


def clear_untouched_default(apps, schema_editor):
    # 'Asia/Kolkata' was the old built-in default; no screen ever let people choose it, so treat it as
    # "not chosen" and let these people follow their workspace time zone.
    UserProfile = apps.get_model("core", "UserProfile")
    UserProfile.objects.filter(timezone="Asia/Kolkata").update(timezone="")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_merge_20261117_0427"),
    ]

    operations = [
        migrations.AlterField(
            model_name="userprofile",
            name="timezone",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.RunPython(clear_untouched_default, migrations.RunPython.noop),
    ]
