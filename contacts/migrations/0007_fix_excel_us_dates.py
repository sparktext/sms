"""
Repair date custom fields imported from Excel before dates were formatted for a US audience.

Earlier imports stored Excel date cells as "YYYY-MM-DD 00:00:00". When a file of US dates
(MM/DD/YYYY) had been opened in Excel with day/month regional settings, Excel turned
"12/11/1965" (Dec 11) into 12 Nov 1965, so those values were saved with month and day
swapped, while dates it could not read that way (e.g. "12/25/1965") stayed as text.

For each organisation and field this migration:
  * swaps month/day back, but ONLY when the field shows that signature (every Excel
    date has a day <= 12 and some text dates have a second number > 12), and
  * writes every such Excel date as MM/DD/YYYY.
Other values are left untouched.
"""
import re
from collections import defaultdict

from django.db import migrations

ISO_MIDNIGHT = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) 00:00:00$")
US_TEXT = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")


def forwards(apps, schema_editor):
    Contact = apps.get_model("contacts", "Contact")

    # Pass 1: per (organisation, field) evidence
    evidence = defaultdict(lambda: {"iso": 0, "iso_day_gt12": False, "us_gt12": False})
    for org_id, cf in Contact.objects.values_list("organization_id", "custom_fields").iterator():
        if not isinstance(cf, dict):
            continue
        for key, val in cf.items():
            if not isinstance(val, str):
                continue
            m = ISO_MIDNIGHT.match(val)
            if m:
                ev = evidence[(org_id, key)]
                ev["iso"] += 1
                if int(m.group(3)) > 12:
                    ev["iso_day_gt12"] = True
                continue
            m = US_TEXT.match(val)
            if m and int(m.group(2)) > 12:
                evidence[(org_id, key)]["us_gt12"] = True

    swap = {k for k, ev in evidence.items() if ev["iso"] and ev["us_gt12"] and not ev["iso_day_gt12"]}
    touch = {k for k, ev in evidence.items() if ev["iso"]}
    if not touch:
        return

    # Pass 2: rewrite affected values
    for contact in Contact.objects.only("id", "organization_id", "custom_fields").iterator():
        cf = contact.custom_fields
        if not isinstance(cf, dict):
            continue
        changed = False
        for key, val in list(cf.items()):
            if (contact.organization_id, key) not in touch or not isinstance(val, str):
                continue
            m = ISO_MIDNIGHT.match(val)
            if not m:
                continue
            year, month, day = m.group(1), int(m.group(2)), int(m.group(3))
            if (contact.organization_id, key) in swap:
                month, day = day, month
            cf[key] = f"{month:02d}/{day:02d}/{year}"
            changed = True
        if changed:
            Contact.objects.filter(pk=contact.pk).update(custom_fields=cf)


class Migration(migrations.Migration):

    dependencies = [
        ("contacts", "0006_remove_contact_timezone"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
