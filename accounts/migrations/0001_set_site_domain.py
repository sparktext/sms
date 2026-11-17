"""
accounts/migrations/0001_set_site_domain.py
--------------------------------------------
Sets the default django.contrib.sites Site to localhost:8000 / SparkText.
Run after: python manage.py migrate
"""
from django.db import migrations


def set_site_domain(apps, schema_editor):
    """Create or update Site id=1 to match SparkText dev environment."""
    Site = apps.get_model("sites", "Site")
    Site.objects.update_or_create(
        id=1,
        defaults={"domain": "localhost:8000", "name": "SparkText"},
    )


def revert_site_domain(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    Site.objects.update_or_create(
        id=1,
        defaults={"domain": "example.com", "name": "example.com"},
    )


class Migration(migrations.Migration):

    dependencies = [
        ("sites", "0002_alter_domain_unique"),
    ]

    operations = [
        migrations.RunPython(set_site_domain, revert_site_domain),
    ]

