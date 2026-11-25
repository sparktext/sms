from unittest.mock import patch, MagicMock
from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.contrib.admin.sites import AdminSite

from core.models import Organization
from contacts.models import Contact, ContactList, ContactField, ImportBatch
from contacts.utils import normalize_phone
from contacts.services import (
    save_contact,
    update_contact,
    validate_custom_fields,
    preview_csv,
    suggest_column_mappings,
    validate_csv_mapping,
    import_contacts_csv,
    ingest_lead_data,
)
from contacts.admin import (
    ContactAdmin,
    ContactAdminForm,
    ContactListAdmin,
    ContactListAdminForm,
    ContactFieldAdmin,
    ContactFieldAdminForm,
    ImportBatchAdmin,
)


class PhoneNormalizationTests(TestCase):
    def test_phone_normalization_formats(self):
        """Different valid US phone formats normalize to the exact same E.164 string."""
        valid_inputs = [
            "(650) 253-0000",
            "650-253-0000",
            "650 253 0000",
            "650.253.0000",
            "+1 650 253 0000",
            "1-650-253-0000",
            "+16502530000",
        ]
        for phone_in in valid_inputs:
            normalized = normalize_phone(phone_in, default_region="US")
            self.assertEqual(normalized, "+16502530000")

    def test_phone_normalization_invalid_rejected(self):
        """Invalid phone inputs are rejected with a ValidationError."""
        invalid_inputs = [
            "123",
            "abc-def-ghij",
            "555-0100",
            "",
            "   ",
        ]
        for invalid_in in invalid_inputs:
            with self.assertRaises(ValidationError):
                normalize_phone(invalid_in)

    def test_phone_extensions_rejected(self):
        """Phone numbers with extensions (e.g. ext 123, x45) are rejected for SMS."""
        ext_inputs = [
            "(650) 253-0000 ext 123",
            "+16502530000 x45",
            "650-253-0000 ext. 9",
        ]
        for phone_in in ext_inputs:
            with self.assertRaises(ValidationError) as ctx:
                normalize_phone(phone_in)
            self.assertIn("extension", str(ctx.exception).lower())

    @override_settings(DEFAULT_PHONE_REGION="GB")
    def test_phone_region_from_settings(self):
        """Phone region defaults to settings.DEFAULT_PHONE_REGION when not provided."""
        # 020 7946 0991 is a valid UK number
        normalized = normalize_phone("020 7946 0991")
        self.assertEqual(normalized, "+442079460991")


class ContactFieldModelTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme Corp", slug="acme")

    def test_contact_field_key_validations(self):
        """Key must start with a lowercase letter, use only lowercase alphanumeric + _, and not be reserved."""
        cf = ContactField.objects.create(organization=self.org, key="valid_key_1", label="Valid Key")
        self.assertEqual(cf.key, "valid_key_1")

        with self.assertRaises(ValidationError):
            ContactField.objects.create(organization=self.org, key="1_digit_start", label="Digit Start")

        with self.assertRaises(ValidationError):
            ContactField.objects.create(organization=self.org, key="bad-key", label="Bad Hyphen")

        with self.assertRaises(ValidationError):
            ContactField.objects.create(organization=self.org, key="phone", label="Phone Custom")

        with self.assertRaises(ValidationError):
            ContactField.objects.create(organization=self.org, key="email", label="Email Custom")

    def test_contact_field_key_and_org_readonly_after_creation(self):
        """Key and Organization cannot be changed after creation."""
        cf = ContactField.objects.create(organization=self.org, key="lead_score", label="Lead Score")
        cf.key = "new_score"
        with self.assertRaises(ValidationError):
            cf.save()

        cf.refresh_from_db()
        org2 = Organization.objects.create(name="Org 2", slug="org2")
        cf.organization = org2
        with self.assertRaises(ValidationError):
            cf.save()


class ContactModelAndServiceTests(TestCase):
    def setUp(self):
        self.org1 = Organization.objects.create(name="Acme Corp", slug="acme")
        self.org2 = Organization.objects.create(name="Beta LLC", slug="beta")

        self.field_topping = ContactField.objects.create(
            organization=self.org1,
            key="favorite_topping",
            label="Favorite Topping",
        )

    def test_duplicate_phone_in_same_org_blocked_at_db_level(self):
        """Duplicate (organization, phone) is rejected by unique constraint."""
        Contact.objects.create(
            organization=self.org1,
            phone="+16502530000",
            first_name="Alice",
        )
        with self.assertRaises(IntegrityError):
            Contact.objects.create(
                organization=self.org1,
                phone="+16502530000",
                first_name="Alice Duplicate",
            )

    def test_same_phone_allowed_in_different_organizations(self):
        """The same phone number can exist across two different organizations."""
        c1 = Contact.objects.create(
            organization=self.org1,
            phone="+16502530000",
            first_name="Alice",
            last_name="Acme",
        )
        c2 = Contact.objects.create(
            organization=self.org2,
            phone="+16502530000",
            first_name="Alice",
            last_name="Beta",
        )
        self.assertNotEqual(c1.pk, c2.pk)
        self.assertEqual(c1.phone, c2.phone)
        self.assertEqual(Contact.objects.filter(phone="+16502530000").count(), 2)

    def test_contact_organization_readonly_after_creation(self):
        """Organization cannot be changed on an existing contact."""
        c = Contact.objects.create(
            organization=self.org1,
            phone="+16502530000",
            first_name="Bob",
        )
        c.organization = self.org2
        with self.assertRaises(ValidationError):
            c.save()

    def test_update_contact_edits_in_place_without_duplicate(self):
        """update_contact modifies the contact in place, normalizes phone, and prevents duplicates."""
        c = Contact.objects.create(
            organization=self.org1,
            phone="+16502530000",
            first_name="Old",
            last_name="Name",
            source=Contact.Source.CSV,
        )

        updated = update_contact(
            contact=c,
            data={
                "phone": "(206) 266-1000",
                "first_name": "New",
                "last_name": "Name",
                "custom_fields": {"favorite_topping": "Pepperoni"},
            },
        )
        self.assertEqual(updated.pk, c.pk)
        self.assertEqual(updated.phone, "+12062661000")
        self.assertEqual(updated.first_name, "New")
        self.assertEqual(updated.last_name, "Name")
        self.assertEqual(updated.source, Contact.Source.CSV)
        self.assertEqual(Contact.objects.filter(organization=self.org1).count(), 1)

    def test_update_contact_detects_phone_collision(self):
        """Changing phone on existing contact to an existing phone in same org raises ValidationError."""
        c1 = Contact.objects.create(organization=self.org1, phone="+16502530000", first_name="C1")
        c2 = Contact.objects.create(organization=self.org1, phone="+12062661000", first_name="C2")

        with self.assertRaises(ValidationError):
            update_contact(c2, {"phone": "+16502530000"})

    def test_save_contact_overwrites_only_non_empty_values_and_sets_source_only_on_creation(self):
        """Re-saving/importing does not blank out existing data and preserves source on update."""
        c1, created1 = save_contact(
            self.org1,
            {
                "phone": "+16502530000",
                "first_name": "Alice",
                "last_name": "Smith",
                "email": "alice@example.com",
                "custom_fields": {"favorite_topping": "Cheese"},
            },
            source=Contact.Source.CSV,
        )
        self.assertEqual(c1.source, Contact.Source.CSV)

        c2, created2 = save_contact(
            self.org1,
            {
                "phone": "+16502530000",
                "first_name": "",
                "last_name": "",
                "email": "",
                "custom_fields": {"favorite_topping": "Mushrooms"},
            },
            source=Contact.Source.API,
        )
        self.assertFalse(created2)
        c2.refresh_from_db()
        self.assertEqual(c2.first_name, "Alice")
        self.assertEqual(c2.last_name, "Smith")
        self.assertEqual(c2.email, "alice@example.com")
        self.assertEqual(c2.custom_fields["favorite_topping"], "Mushrooms")
        self.assertEqual(c2.source, Contact.Source.CSV)

    def test_input_validations_email_lengths_value_types(self):
        """Validates email format, string lengths, and custom field value types."""
        with self.assertRaises(ValidationError):
            save_contact(self.org1, {"phone": "+16502530000", "email": "not-an-email"})

        # Timezone is no longer a contact attribute: a stray value is ignored, not stored or validated.
        c, _created = save_contact(self.org1, {"phone": "+16502530000", "timezone": "Mars/Olympus_Mons"})
        self.assertFalse(hasattr(c, "timezone"))

        with self.assertRaises(ValidationError):
            save_contact(self.org1, {
                "phone": "+16502530000",
                "custom_fields": {"favorite_topping": ["mushrooms", "peppers"]},
            })

        c, _created = save_contact(self.org1, {
            "phone": "+16502530000",
            "custom_fields": {"favorite_topping": True},
        })
        self.assertEqual(c.custom_fields["favorite_topping"], True)


class ContactListM2MValidationTests(TestCase):
    def setUp(self):
        self.org1 = Organization.objects.create(name="Org 1", slug="org1")
        self.org2 = Organization.objects.create(name="Org 2", slug="org2")

        self.c1 = Contact.objects.create(organization=self.org1, phone="+16502530000")
        self.c2 = Contact.objects.create(organization=self.org2, phone="+12062661000")

        self.list1 = ContactList.objects.create(organization=self.org1, name="List 1")

    def test_m2m_changed_blocks_cross_organization_contacts(self):
        """ContactList cannot mix contacts from different organizations."""
        self.list1.contacts.add(self.c1)
        self.assertIn(self.c1, self.list1.contacts.all())

        with self.assertRaises(ValidationError):
            self.list1.contacts.add(self.c2)

    def test_contact_list_org_readonly_after_creation(self):
        """Organization cannot be changed on existing ContactList."""
        self.list1.organization = self.org2
        with self.assertRaises(ValidationError):
            self.list1.save()


class CSVImportTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Pizza Shop", slug="pizza")
        self.cf_notes = ContactField.objects.create(
            organization=self.org,
            key="customer_notes",
            label="Customer Notes",
        )
        self.target_list = ContactList.objects.create(organization=self.org, name="VIP List")

    def test_csv_preview_keeps_blank_and_flags_duplicates(self):
        """Preview keeps blank headers and flags duplicate headers."""
        csv_data = "Phone,Name,,Phone,Email\n(650) 253-0000,Alice,X,650-253-0000,a@example.com\n"
        preview = preview_csv(csv_data)
        self.assertIn("", preview["headers"])
        self.assertEqual(preview["duplicate_headers"], ["Phone"])

    def test_csv_decoding_cp1252_fallback_and_warning(self):
        """Fallback to cp1252 when bytes are not valid UTF-8, and warn user."""
        cp1252_bytes = b"Phone,Name\n(650) 253-0000,Ren\x96e\n"
        preview = preview_csv(cp1252_bytes)
        self.assertIsNotNone(preview["warning"])
        self.assertIn("cp1252", preview["warning"])
        # 0x96 in Windows-1252 maps to unicode en-dash '\u2013'
        self.assertEqual(preview["rows"][0][1], "Ren\u2013e")

    def test_validate_csv_mapping_checks(self):
        """Upfront mapping validation enforces single phone column, valid targets, valid add keys, no reserved names."""
        headers = ["Phone", "Name", "BadKey", "ReservedKey"]

        with self.assertRaises(ValidationError) as ctx:
            validate_csv_mapping(self.org, headers, {
                "Name": {"decision": "map", "target": "name"},
            })
        self.assertIn("exactly one 'phone' column", str(ctx.exception))

        with self.assertRaises(ValidationError) as ctx:
            validate_csv_mapping(self.org, headers, {
                "Phone": {"decision": "map", "target": "phone"},
                "Name": {"decision": "map", "target": "phone"},
            })
        self.assertIn("cannot map more than one column to 'phone'", str(ctx.exception))

        with self.assertRaises(ValidationError) as ctx:
            validate_csv_mapping(self.org, headers, {
                "Phone": {"decision": "map", "target": "phone"},
                "BadKey": {"decision": "map", "target": "non_existent_field"},
            })
        self.assertIn("is not an approved field", str(ctx.exception))

        with self.assertRaises(ValidationError) as ctx:
            validate_csv_mapping(self.org, headers, {
                "Phone": {"decision": "map", "target": "phone"},
                "ReservedKey": {"decision": "add", "target": "email"},
            })
        self.assertIn("reserved field name", str(ctx.exception))

        with self.assertRaises(ValidationError) as ctx:
            validate_csv_mapping(self.org, headers, {
                "Phone": {"decision": "map", "target": "phone"},
                "BadKey": {"decision": "add", "target": "1_bad_key"},
            })
        self.assertIn("must start with a letter", str(ctx.exception))

        with self.assertRaises(ValidationError) as ctx:
            validate_csv_mapping(self.org, headers, {
                "Phone": {"decision": "map", "target": "phone"},
                "BadKey": {"decision": "add", "target": "custom_score"},
                "ReservedKey": {"decision": "add", "target": "custom_score"},
            })
        self.assertIn("Duplicate custom field key", str(ctx.exception))

    def test_import_batch_attestation_list_and_duplicate_phones(self):
        """Import tracks ImportBatch, handles duplicate in-file phones, attaches contact_list, and records consent attestation."""
        csv_data = (
            "Phone,Name,VipStatus\n"
            "(650) 253-0000,John Doe,Gold\n"
            "(650) 253-0000,John Duplicate,Silver\n"  # in-file duplicate phone
            "(206) 266-1000,Jane Smith,Platinum\n"
        )

        mapping = {
            "Phone": {"decision": "map", "target": "phone"},
            "Name": {"decision": "map", "target": "name"},
            "VipStatus": {"decision": "add", "target": "vip_status"},
        }

        report = import_contacts_csv(
            organization=self.org,
            file_or_content=csv_data,
            column_mapping=mapping,
            contact_list=self.target_list,
            consent_attestation=True,
            attestation_text="Lead sign-up form timestamp 2026-10-30",
            filename="vip_leads.csv",
        )

        self.assertEqual(report["created"], 2)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("Duplicate phone number", report["errors"][0]["error"])

        # Check ImportBatch record
        batch = ImportBatch.objects.get(pk=report["import_batch_id"])
        self.assertEqual(batch.filename, "vip_leads.csv")
        self.assertEqual(batch.created_count, 2)
        self.assertEqual(batch.skipped_count, 1)
        self.assertTrue(batch.consent_attested)
        self.assertEqual(batch.contact_list, self.target_list)

        # Check contact list association
        self.assertEqual(self.target_list.contacts.count(), 2)

        # Check consent was recorded because consent_attestation was True
        from compliance.models import ConsentEvent
        events = ConsentEvent.objects.filter(organization=self.org, source=ConsentEvent.Source.CSV_ATTESTATION)
        self.assertEqual(events.count(), 2)
        for contact in self.target_list.contacts.all():
            self.assertEqual(contact.sms_status, Contact.SMSStatus.OPTED_IN)


class AdminFormValidationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme", slug="acme")
        ContactField.objects.create(organization=self.org, key="tier", label="Tier")

    def test_contact_admin_form_validates_custom_fields_without_500(self):
        """Unknown custom field in admin form clean() raises ValidationError on the form instead of 500 error."""
        form = ContactAdminForm(
            data={
                "organization": self.org.pk,
                "phone": "(650) 253-0000",
                "custom_fields_json": '{"unknown_field": "val"}',
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("custom_fields_json", form.errors)
        self.assertIn("Unknown custom field 'unknown_field'", form.errors["custom_fields_json"][0])

    def test_contact_admin_edit_calls_update_contact_no_duplicate(self):
        """Editing an existing contact through ContactAdmin calls update_contact and modifies in-place."""
        c = Contact.objects.create(organization=self.org, phone="+16502530000", first_name="Original", last_name="Lead")

        site = AdminSite()
        admin = ContactAdmin(Contact, site)

        class MockRequest:
            pass

        class MockForm:
            cleaned_data = {
                "phone": "(206) 266-1000",
                "first_name": "Admin",
                "last_name": "Edited",
                "email": "edited@example.com",
                "custom_fields_json": {"tier": "Gold"},
                "external_ids": {},
            }

        admin.save_model(MockRequest(), c, MockForm(), change=True)
        c.refresh_from_db()
        self.assertEqual(c.phone, "+12062661000")
        self.assertEqual(c.first_name, "Admin")
        self.assertEqual(c.last_name, "Edited")
        self.assertEqual(Contact.objects.filter(organization=self.org).count(), 1)


class ContactsViewAndTemplateTests(TestCase):
    def setUp(self):
        from core.models import User
        self.org = Organization.objects.create(name="Starlight Cafe", slug="starlight-cafe")
        self.user = User.objects.create_user(
            email="manager@starlight.com",
            password="securepassword123",
            organization=self.org,
        )
        self.client.force_login(self.user)

    def test_contacts_list_view_renders_cleanly(self):
        Contact.objects.create(
            organization=self.org,
            phone="+16502530000",
            first_name="John",
            last_name="Doe",
            email="john@example.com",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        resp = self.client.get("/contacts/")
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "contacts/contacts_list.html")
        self.assertContains(resp, "John")
        self.assertContains(resp, "Doe")
        self.assertContains(resp, "+16502530000")
        self.assertContains(resp, "Opted In")

    def test_contacts_list_filters_by_search_and_status(self):
        Contact.objects.create(organization=self.org, phone="+16502530001", first_name="Alice", last_name="Smith", sms_status=Contact.SMSStatus.OPTED_IN)
        Contact.objects.create(organization=self.org, phone="+16502530002", first_name="Bob", last_name="Jones", sms_status=Contact.SMSStatus.OPTED_OUT)

        # Filter by status
        resp = self.client.get("/contacts/?status=opted_in")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Alice")
        self.assertNotContains(resp, "Bob")

        # Search by query
        resp_q = self.client.get("/contacts/?q=Jones")
        self.assertEqual(resp_q.status_code, 200)
        self.assertContains(resp_q, "Bob")
        self.assertNotContains(resp_q, "Alice")

        # Full-name search + live partial response
        resp_full = self.client.get("/contacts/?q=Alice+Smith")
        self.assertEqual(resp_full.status_code, 200)
        self.assertContains(resp_full, "Alice")
        self.assertNotContains(resp_full, "Bob")
        self.assertContains(resp_full, 'oninput="scheduleContactsSearch()"')
        self.assertContains(resp_full, 'id="contacts-table-container"')

        resp_partial = self.client.get("/contacts/?partial=1&q=Alice+Smith", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp_partial.status_code, 200)
        self.assertTemplateUsed(resp_partial, "contacts/partials/contacts_table.html")
        self.assertContains(resp_partial, "Alice")
        self.assertNotContains(resp_partial, "Bob")
        self.assertNotContains(resp_partial, "<header")

    def test_add_contact_ajax_success(self):
        resp = self.client.post(
            "/contacts/new/",
            data={
                "phone": "(650) 253-1111",
                "first_name": "Jane",
                "last_name": "Taylor",
                "email": "jane@example.com",
                "source": "Direct",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        json_data = resp.json()
        self.assertEqual(json_data["status"], "success")
        self.assertEqual(json_data["phone"], "+16502531111")
        self.assertEqual(json_data["first_name"], "Jane")
        self.assertEqual(json_data["last_name"], "Taylor")
        self.assertTrue(Contact.objects.filter(organization=self.org, phone="+16502531111", source="Direct").exists())

        # Test source is compulsory
        fail_resp = self.client.post(
            "/contacts/new/",
            data={
                "phone": "(650) 253-2222",
                "first_name": "No",
                "last_name": "Source",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(fail_resp.status_code, 400)

    def test_import_preview_and_process_flow(self):
        import io
        import json
        from django.core.files.uploadedfile import SimpleUploadedFile

        csv_content = b"First Name,Last Name,Phone Number\nCharlie,Brown,650 253 2222\n"
        upload_file = SimpleUploadedFile("test.csv", csv_content, content_type="text/csv")

        # Preview step
        resp_prev = self.client.post(
            "/contacts/import/preview/",
            {"file": upload_file},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_prev.status_code, 200)
        prev_data = resp_prev.json()
        self.assertEqual(prev_data["status"], "success")
        self.assertIn("Phone Number", prev_data["headers"])

        # Process step
        mapping = {
            "First Name": {"decision": "map", "target": "first_name"},
            "Last Name": {"decision": "map", "target": "last_name"},
            "Phone Number": {"decision": "map", "target": "phone"},
        }
        upload_file2 = SimpleUploadedFile("test.csv", csv_content, content_type="text/csv")
        resp_proc = self.client.post(
            "/contacts/import/process/",
            {
                "file": upload_file2,
                "mapping": json.dumps(mapping),
                "consent_attested": "1",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_proc.status_code, 200)
        proc_data = resp_proc.json()
        self.assertEqual(proc_data["status"], "success")
        self.assertEqual(proc_data["report"]["created"], 1)
        c = Contact.objects.get(organization=self.org, phone="+16502532222")
        self.assertEqual(c.first_name, "Charlie")
        self.assertEqual(c.last_name, "Brown")

    def test_import_excel_xlsx_flow(self):
        import io
        import json
        import openpyxl
        from django.core.files.uploadedfile import SimpleUploadedFile

        # Create in-memory XLSX file
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["First Name", "Last Name", "Phone Number", "Email"])
        ws.append(["David", "Miller", "+16502533333", "david@example.com"])
        buf = io.BytesIO()
        wb.save(buf)
        xlsx_bytes = buf.getvalue()

        upload_file = SimpleUploadedFile("contacts.xlsx", xlsx_bytes, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        # Preview step
        resp_prev = self.client.post(
            "/contacts/import/preview/",
            {"file": upload_file},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_prev.status_code, 200)
        prev_data = resp_prev.json()
        self.assertEqual(prev_data["status"], "success")
        self.assertIn("Phone Number", prev_data["headers"])

        # Process step
        mapping = {
            "First Name": {"decision": "map", "target": "first_name"},
            "Last Name": {"decision": "map", "target": "last_name"},
            "Phone Number": {"decision": "map", "target": "phone"},
            "Email": {"decision": "map", "target": "email"},
        }
        upload_file2 = SimpleUploadedFile("contacts.xlsx", xlsx_bytes, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp_proc = self.client.post(
            "/contacts/import/process/",
            {
                "file": upload_file2,
                "mapping": json.dumps(mapping),
                "consent_attested": "1",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_proc.status_code, 200)
        proc_data = resp_proc.json()
        self.assertEqual(proc_data["status"], "success")
        self.assertEqual(proc_data["report"]["created"], 1)
        c = Contact.objects.get(organization=self.org, phone="+16502533333")
        self.assertEqual(c.first_name, "David")
        self.assertEqual(c.last_name, "Miller")
        self.assertEqual(c.email, "david@example.com")

    def test_sms_status_unknown_displays_no_consent(self):
        c = Contact.objects.create(first_name="Test", last_name="Lead", phone="+16502530000", organization=self.org)
        self.assertEqual(c.sms_status, Contact.SMSStatus.UNKNOWN)
        self.assertEqual(c.get_sms_status_display(), "No consent")

    def test_contact_detail_view_and_edit_flow(self):
        c = Contact.objects.create(
            organization=self.org,
            phone="+16502530000",
            first_name="Antonio",
            last_name="Schjang",
            email="antonio@example.com",
            custom_fields={"tier": "Gold"},
        )
        from compliance.models import ConsentEvent
        from compliance.services import record_consent
        record_consent(
            contact=c,
            event_type=ConsentEvent.EventType.OPT_IN,
            source=ConsentEvent.Source.WEB_FORM,
            proof_text="Signed contract on portal",
        )

        # GET detail view
        resp = self.client.get(f"/contacts/{c.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "contacts/contact_detail.html")
        self.assertContains(resp, "Antonio")
        self.assertContains(resp, "Schjang")
        self.assertContains(resp, "Signed contract on portal")

        # POST edit
        resp_edit = self.client.post(
            f"/contacts/{c.id}/edit/",
            data={
                "phone": "(650) 253-9999",
                "first_name": "Antonio",
                "last_name": "Updated",
                "email": "antonio.new@example.com",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_edit.status_code, 200)
        c.refresh_from_db()
        self.assertEqual(c.phone, "+16502539999")
        self.assertEqual(c.first_name, "Antonio")
        self.assertEqual(c.last_name, "Updated")

    def test_bulk_assign_to_list(self):
        c1 = Contact.objects.create(organization=self.org, phone="+16502530001", first_name="Lead", last_name="One")
        c2 = Contact.objects.create(organization=self.org, phone="+16502530002", first_name="Lead", last_name="Two")
        cl = ContactList.objects.create(organization=self.org, name="October Leads")

        resp = self.client.post(
            "/contacts/bulk-action/",
            data={
                "action": "add_to_list",
                "list_id": cl.id,
                "contact_ids": [c1.id, c2.id],
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(cl.contacts.count(), 2)

    def test_contact_lists_crud(self):
        # Create
        resp_c = self.client.post("/contacts/lists/new/", {"name": "VIP Club"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp_c.status_code, 200)
        cl_id = resp_c.json()["id"]

        # Rename
        resp_e = self.client.post(f"/contacts/lists/{cl_id}/edit/", {"name": "VIP Premium"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp_e.status_code, 200)
        self.assertTrue(ContactList.objects.filter(organization=self.org, name="VIP Premium").exists())

        # Index
        resp_idx = self.client.get("/contacts/lists/")
        self.assertEqual(resp_idx.status_code, 200)
        self.assertContains(resp_idx, "VIP Premium")
        self.assertContains(resp_idx, "Active Lists")
        self.assertContains(resp_idx, "Opted In Reach")

        # Delete
        resp_d = self.client.post(f"/contacts/lists/{cl_id}/delete/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp_d.status_code, 200)
        self.assertFalse(ContactList.objects.filter(id=cl_id).exists())

    def test_custom_fields_crud(self):
        # Create
        resp_c = self.client.post("/contacts/fields/new/", {"label": "Shoe Size", "key": "shoe_size"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp_c.status_code, 200)
        f_id = resp_c.json()["id"]

        # Rename
        resp_e = self.client.post(f"/contacts/fields/{f_id}/edit/", {"label": "Footwear Size"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp_e.status_code, 200)
        self.assertTrue(ContactField.objects.filter(organization=self.org, label="Footwear Size").exists())

        # Index
        resp_idx = self.client.get("/contacts/fields/")
        self.assertEqual(resp_idx.status_code, 200)
        self.assertContains(resp_idx, "Footwear Size")

        # Delete
        resp_d = self.client.post(f"/contacts/fields/{f_id}/delete/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp_d.status_code, 200)
        self.assertFalse(ContactField.objects.filter(id=f_id).exists())

    def test_delete_contact_with_conversations_cascades(self):
        from messaging.models import Conversation, PhoneNumber
        phone_obj = PhoneNumber.objects.create(
            organization=self.org,
            e164="+16505550199",
            label="Main Line",
            is_active=True,
        )
        c = Contact.objects.create(
            organization=self.org,
            phone="+16505559876",
            first_name="Delete",
            last_name="Me",
        )
        conv = Conversation.objects.create(
            organization=self.org,
            contact=c,
            phone_number=phone_obj,
        )

        resp = self.client.post(f"/contacts/{c.id}/delete/")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Contact.objects.filter(id=c.id).exists())
        self.assertFalse(Conversation.objects.filter(id=conv.id).exists())

    def test_lead_ingest_api_get_schema(self):
        resp = self.client.get(f"/api/v1/leads/?api_key={self.org.api_key}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["organization"], self.org.name)
        self.assertIn("api_url", data)

    def test_lead_ingest_api_single_lead(self):
        import json
        payload = {
            "phone": "(650) 555-8833",
            "first_name": "Sarah",
            "last_name": "Connor",
            "email": "sarah@cyberdyne.com",
            "list": "Inbound Web Leads",
            "stage": "Intake",
            "form": "Landing Page Beta",
            "city": "Los Angeles",
            "consent": True,
        }
        resp = self.client.post(
            f"/api/v1/leads/?api_key={self.org.api_key}",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["contact"]["phone"], "+16505558833")

        contact = Contact.objects.get(organization=self.org, phone="+16505558833")
        self.assertEqual(contact.first_name, "Sarah")
        self.assertEqual(contact.last_name, "Connor")
        self.assertEqual(contact.sms_status, Contact.SMSStatus.OPTED_IN)
        self.assertEqual(contact.source, Contact.Source.API)
        self.assertEqual(contact.custom_fields.get("stage"), "Intake")
        self.assertEqual(contact.custom_fields.get("city"), "Los Angeles")

        # List assignment
        target_list = ContactList.objects.get(organization=self.org, name="Inbound Web Leads")
        self.assertTrue(target_list.contacts.filter(id=contact.id).exists())

        # TCPA consent event
        from compliance.models import ConsentEvent
        self.assertTrue(ConsentEvent.objects.filter(contact=contact, source=ConsentEvent.Source.API).exists())

    def test_lead_ingest_api_batch_and_auth_headers(self):
        import json
        batch = [
            {"phone": "+16505557701", "name": "Batch Alice", "email": "alice@test.com"},
            {"phone": "+16505557702", "name": "Batch Bob", "email": "bob@test.com"},
        ]
        # Ingestion via path parameter and Header
        resp = self.client.post(
            f"/api/v1/leads/{self.org.api_key}/",
            data=json.dumps(batch),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.org.api_key}",
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["created_count"], 2)
        self.assertTrue(Contact.objects.filter(organization=self.org, phone="+16505557701").exists())
        self.assertTrue(Contact.objects.filter(organization=self.org, phone="+16505557702").exists())

    def test_lead_ingest_api_invalid_key_and_missing_phone(self):
        import json
        # Invalid key
        resp_401 = self.client.post(
            "/api/v1/leads/?api_key=invalid_key_xyz",
            data=json.dumps({"phone": "+16505551111"}),
            content_type="application/json",
        )
        self.assertEqual(resp_401.status_code, 401)

        # Missing phone
        resp_400 = self.client.post(
            f"/api/v1/leads/?api_key={self.org.api_key}",
            data=json.dumps({"first_name": "No Phone"}),
            content_type="application/json",
        )
        self.assertEqual(resp_400.status_code, 400)

    @patch("requests.get")
    def test_fetch_remote_leads_view(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "leads": [
                {"phone": "+16505559901", "first_name": "Remote", "last_name": "One"},
                {"phone": "+16505559902", "first_name": "Remote", "last_name": "Two"},
            ]
        }
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        target_list = ContactList.objects.create(organization=self.org, name="Remote Leads")
        resp = self.client.post(
            "/contacts/api/fetch-remote/",
            data={
                "url": "https://api.external.com/leads.json",
                "method": "GET",
                "contact_list_id": target_list.id,
                "consent_attested": "1",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["data"]["created"], 2)
        self.assertEqual(target_list.contacts.count(), 2)

    def test_regenerate_api_key_view(self):
        old_key = self.org.api_key
        resp = self.client.post("/contacts/api/regenerate-key/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertNotEqual(data["api_key"], old_key)
        self.org.refresh_from_db()
        self.assertEqual(self.org.api_key, data["api_key"])

    def test_export_contacts_csv(self):
        """Export contacts view streams valid CSV with correct headers and contact data."""
        self.client.force_login(self.user)
        Contact.objects.create(
            organization=self.org,
            phone="+14155552671",
            first_name="ExportTest",
            last_name="User",
            email="export@example.com",
            sms_status=Contact.SMSStatus.OPTED_IN,
            custom_fields={"stage": "Qualified"},
        )
        resp = self.client.get("/contacts/export/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        self.assertIn("sparktext_contacts_", resp["Content-Disposition"])
        content = resp.content.decode("utf-8")
        self.assertIn("Phone,First Name,Last Name,Email,SMS Status,Stage,Lists,Created At (UTC)", content)
        self.assertIn("+14155552671,ExportTest,User,export@example.com,Opted In,Qualified", content)

    @override_settings(MESSAGING_PROVIDER="fake")
    def test_contact_detail_conversation_history_and_inline_reply(self):
        from messaging.models import PhoneNumber, Conversation, Message

        phone_num = PhoneNumber.objects.create(
            organization=self.org,
            e164="+14155550100",
            label="Main Sales",
            is_active=True,
            is_default=True,
        )
        contact = Contact.objects.create(
            organization=self.org,
            phone="+16505553322",
            first_name="Alex",
            last_name="Taylor",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv = Conversation.objects.create(
            organization=self.org,
            contact=contact,
            phone_number=phone_num,
            status=Conversation.Status.OPEN,
            unread_count=2,
        )

        Message.objects.create(
            conversation=conv,
            organization=self.org,
            direction=Message.Direction.OUTBOUND,
            sender_type=Message.SenderType.CAMPAIGN,
            body="Fall promo broadcast!",
            status=Message.Status.DELIVERED,
        )
        Message.objects.create(
            conversation=conv,
            organization=self.org,
            direction=Message.Direction.INBOUND,
            sender_type=Message.SenderType.CUSTOMER,
            body="Tell me more about auto rates.",
            status=Message.Status.RECEIVED,
        )
        Message.objects.create(
            conversation=conv,
            organization=self.org,
            direction=Message.Direction.OUTBOUND,
            sender_type=Message.SenderType.AI,
            body="What is your ZIP code?",
            status=Message.Status.DELIVERED,
        )

        # 1. GET contact detail renders the full conversation history & inline reply form
        get_resp = self.client.get(f"/contacts/{contact.id}/")
        self.assertEqual(get_resp.status_code, 200)
        self.assertContains(get_resp, "id=\"contact-conversation-card\"")
        self.assertContains(get_resp, "3 messages")
        self.assertContains(get_resp, "Fall promo broadcast!")
        self.assertContains(get_resp, "Tell me more about auto rates.")
        self.assertContains(get_resp, "What is your ZIP code?")
        self.assertContains(get_resp, 'id="contact-reply-form"')

        # 2. POST inline reply via AJAX sends SMS, pauses AI, and clears unread count
        post_resp = self.client.post(
            f"/contacts/{contact.id}/send/",
            data={"conversation_id": conv.id, "body": "Hi Alex, calling you shortly!"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(post_resp.status_code, 200)
        pdata = post_resp.json()
        self.assertEqual(pdata["status"], "success")
        self.assertEqual(pdata["message"]["body"], "Hi Alex, calling you shortly!")
        self.assertTrue(pdata["ai_mode_changed"])

        conv.refresh_from_db()
        self.assertEqual(conv.ai_mode, Conversation.AIMode.OFF)
        self.assertEqual(conv.unread_count, 0)

        # 3. Sending first ever message to a fresh contact with no existing conversation creates one cleanly
        fresh_contact = Contact.objects.create(
            organization=self.org,
            phone="+16505554499",
            first_name="Sam",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        first_resp = self.client.post(
            f"/contacts/{fresh_contact.id}/send/",
            data={"body": "Welcome Sam!"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(first_resp.status_code, 200)
        fdata = first_resp.json()
        self.assertEqual(fdata["status"], "success")
        self.assertTrue(Conversation.objects.filter(contact=fresh_contact, organization=self.org).exists())

        # 4. Opted-out contact shows block notice and rejects reply
        opted_out_contact = Contact.objects.create(
            organization=self.org,
            phone="+16505558800",
            first_name="Blocked",
            sms_status=Contact.SMSStatus.OPTED_OUT,
        )
        blocked_get = self.client.get(f"/contacts/{opted_out_contact.id}/")
        self.assertContains(blocked_get, "Sending Currently Blocked")
        self.assertNotContains(blocked_get, 'id="contact-reply-form"')

        blocked_post = self.client.post(
            f"/contacts/{opted_out_contact.id}/send/",
            data={"body": "Trying to send"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(blocked_post.status_code, 400)

    @override_settings(AI_CLIENT="fake", MESSAGING_PROVIDER="fake")
    def test_contact_conversation_summary_and_ai_endpoint(self):
        from messaging.models import PhoneNumber, Conversation, Message
        from messaging.ai.fake_client import FakeAIClient

        FakeAIClient.reset()

        phone_num = PhoneNumber.objects.create(
            organization=self.org,
            e164="+14155550199",
            is_active=True,
        )
        contact = Contact.objects.create(
            organization=self.org,
            phone="+16505557788",
            first_name="Maya",
            sms_status=Contact.SMSStatus.OPTED_IN,
            custom_fields={"stage": "Qualified"},
        )
        conv = Conversation.objects.create(
            organization=self.org,
            contact=contact,
            phone_number=phone_num,
            status=Conversation.Status.NEEDS_HUMAN,
            handoff_reason="Requested custom commercial quote",
            qualification_answers={"coverage_type": "Auto & Home", "zip_code": "94030"},
        )
        Message.objects.create(
            conversation=conv,
            organization=self.org,
            direction=Message.Direction.INBOUND,
            sender_type=Message.SenderType.CUSTOMER,
            body="Looking for auto and home bundle in 94030.",
            status=Message.Status.RECEIVED,
        )
        Message.objects.create(
            conversation=conv,
            organization=self.org,
            direction=Message.Direction.OUTBOUND,
            sender_type=Message.SenderType.AI,
            body="Great, passing this to our licensed rep for your custom bundle quote!",
            status=Message.Status.DELIVERED,
        )

        # 1. GET contact detail renders the Conversation Summary card with metrics & qualification answers
        resp = self.client.get(f"/contacts/{contact.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Lead summary")
        self.assertContains(resp, "Auto &amp; Home")
        self.assertContains(resp, "94030")
        self.assertContains(resp, "Requested custom commercial quote")
        self.assertContains(resp, "generateContactAiSummary()")
        self.assertNotContains(resp, "Latest Exchange")

        # 2. On-demand AI summary endpoint with FakeAIClient response
        FakeAIClient.push_response("- Customer wants Auto & Home bundle.\n- ZIP: 94030.\n- Next: Send personalized bundle quote.")
        sum_resp = self.client.post(
            f"/contacts/{contact.id}/summary/",
            data={"conversation_id": conv.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(sum_resp.status_code, 200)
        sdata = sum_resp.json()
        self.assertEqual(sdata["status"], "success")
        self.assertEqual(sdata["source"], "ai")
        self.assertIn("Auto & Home bundle", sdata["summary"])

        # 3. Fallback extractive summary when LLM raises an exception (e.g. offline)
        FakeAIClient.push_response(RuntimeError("Connection refused"))
        fallback_resp = self.client.post(
            f"/contacts/{contact.id}/summary/",
            data={"conversation_id": conv.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(fallback_resp.status_code, 200)
        fdata = fallback_resp.json()
        self.assertEqual(fdata["status"], "success")
        self.assertEqual(fdata["source"], "instant")
        self.assertIn("2 total message(s)", fdata["summary"])

    def test_contact_detail_stage_status_and_assignee_controls(self):
        from messaging.models import PhoneNumber, Conversation, Message

        phone_num = PhoneNumber.objects.create(
            organization=self.org,
            e164="+14155550188",
            is_active=True,
        )
        contact = Contact.objects.create(
            organization=self.org,
            phone="+16505558899",
            first_name="Liam",
            sms_status=Contact.SMSStatus.OPTED_IN,
            custom_fields={"stage": "Intake"},
        )
        conv = Conversation.objects.create(
            organization=self.org,
            contact=contact,
            phone_number=phone_num,
            status=Conversation.Status.NEEDS_HUMAN,
        )
        Message.objects.create(
            conversation=conv,
            organization=self.org,
            direction=Message.Direction.INBOUND,
            sender_type=Message.SenderType.CUSTOMER,
            body="Can you send me an updated quote?",
            status=Message.Status.RECEIVED,
        )

        # 1. GET renders interactive stage, conv status, assignee controls, and fixed draft-action URL
        resp = self.client.get(f"/contacts/{contact.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn('id="contact-stage-select"', html)
        self.assertIn('id="contact-conv-status-select"', html)
        self.assertIn('id="contact-assign-select"', html)
        self.assertIn("/draft-action/", html)
        self.assertNotIn("/ai-draft/", html)
        # 60px global header carries rep availability; page header in <main> has icon-only Back arrow before contact title
        header_idx = html.index("<header")
        main_idx = html.index("<main")
        self.assertIn("h-[60px]", html[header_idx:main_idx])
        self.assertIn('id="nav-availability-btn"', html[header_idx:main_idx])
        self.assertNotIn('id="nav-availability-btn"', html[main_idx:])
        self.assertIn('aria-label="Back"', html[main_idx:])
        self.assertIn('data-back', html[main_idx:])            # goes back to the page you came from
        self.assertNotIn("← Back to Contacts", html)
        self.assertLess(
            html.index('aria-label="Back"', main_idx),
            html.index("Liam", main_idx),
        )
        # Name & metadata row sits below the top back arrow + right-side action toolbar
        self.assertLess(
            html.index("Message Contact", main_idx),
            html.index("Liam", main_idx),
        )
        # Consent lives behind the status badge by the name (no separate Consent Status box)
        self.assertNotIn("Consent Status", html[main_idx:])
        self.assertIn('onclick="openAuditHistoryModal()" title="View consent history"', html[main_idx:])
        # Sticky right column: Conversation Summary on top, then the conversation + reply box
        self.assertLess(html.index("Lead summary", main_idx), html.index("contact-conversation-card", main_idx))
        self.assertIn("lg:col-start-2 lg:row-start-1 lg:sticky", html[main_idx:])
        self.assertLess(html.index('id="contact-summary-card"'), html.index('id="contact-conversation-card"'))
        self.assertIn("overflow-x-clip", html[main_idx - 300:main_idx + 300])
        # Non-standard custom stage is preserved in the dropdown
        self.assertRegex(html, r'<option[^>]*value="Intake"[^>]*selected')

        # 2. Update lead stage via AJAX
        stage_resp = self.client.post(
            f"/contacts/{contact.id}/stage/",
            data={"stage": "Quote Sent"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(stage_resp.status_code, 200)
        self.assertEqual(stage_resp.json()["status"], "success")
        self.assertEqual(stage_resp.json()["stage"], "Quote Sent")
        contact.refresh_from_db()
        self.assertEqual(contact.custom_fields.get("stage"), "Quote Sent")

        # 3. Assign rep via AJAX
        assign_resp = self.client.post(
            f"/inbox/{conv.id}/assign/",
            data={"assigned_to_id": str(self.user.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(assign_resp.status_code, 200)
        self.assertEqual(assign_resp.json()["status"], "success")
        conv.refresh_from_db()
        self.assertEqual(conv.assigned_to_id, self.user.id)

        # 4. Update conversation status via AJAX
        status_resp = self.client.post(
            f"/inbox/{conv.id}/status/",
            data={"status": "closed"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(status_resp.status_code, 200)
        self.assertEqual(status_resp.json()["status"], "success")
        self.assertEqual(status_resp.json()["new_status"], "closed")
        conv.refresh_from_db()
        self.assertEqual(conv.status, Conversation.Status.CLOSED)

        # 5. Subsequent GET reflects all persisted updates
        updated_resp = self.client.get(f"/contacts/{contact.id}/")
        self.assertEqual(updated_resp.status_code, 200)
        updated_html = updated_resp.content.decode("utf-8")
        self.assertRegex(updated_html, r'<option[^>]*value="Quote Sent"[^>]*selected')
        self.assertRegex(updated_html, r'<option[^>]*value="closed"[^>]*selected')
        self.assertRegex(updated_html, rf'<option[^>]*value="{self.user.id}"[^>]*selected')

        # 6. Contact with 0 messages still renders the interactive Lead Stage dropdown
        empty_contact = Contact.objects.create(
            organization=self.org,
            phone="+16505559900",
            first_name="Nora",
            sms_status=Contact.SMSStatus.OPTED_IN,
            custom_fields={"stage": "New Lead"},
        )
        empty_resp = self.client.get(f"/contacts/{empty_contact.id}/")
        self.assertEqual(empty_resp.status_code, 200)
        empty_html = empty_resp.content.decode("utf-8")
        self.assertIn('id="contact-stage-select"', empty_html)
        self.assertRegex(empty_html, r'<option[^>]*value="New Lead"[^>]*selected')

    def test_contact_detail_loads_latest_35_with_full_summary_counts(self):
        from datetime import timedelta
        from django.utils import timezone
        from messaging.models import PhoneNumber, Conversation, Message

        phone_num = PhoneNumber.objects.create(
            organization=self.org,
            e164="+14155550177",
            is_active=True,
        )
        contact = Contact.objects.create(
            organization=self.org,
            phone="+16505559977",
            first_name="Chloe",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv = Conversation.objects.create(
            organization=self.org,
            contact=contact,
            phone_number=phone_num,
        )
        base_dt = timezone.now() - timedelta(days=2)
        created_msgs = []
        for idx in range(1, 46):
            is_sys = idx <= 5
            m = Message.objects.create(
                conversation=conv,
                organization=self.org,
                direction=Message.Direction.OUTBOUND if (idx % 2 == 0 or is_sys) else Message.Direction.INBOUND,
                sender_type=(
                    Message.SenderType.SYSTEM
                    if is_sys
                    else (Message.SenderType.CUSTOMER if idx % 2 else Message.SenderType.AI)
                ),
                body=f"Contact history item #{idx:02d}",
                status=Message.Status.DELIVERED if (idx % 2 == 0 and not is_sys) else Message.Status.RECEIVED,
            )
            Message.objects.filter(pk=m.pk).update(created_at=base_dt + timedelta(minutes=idx))
            created_msgs.append(m)

        resp = self.client.get(f"/contacts/{contact.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["chat_messages"]), 35)
        self.assertEqual(resp.context["chat_messages"][0].id, created_msgs[10].id)  # #11
        self.assertEqual(resp.context["chat_messages"][-1].id, created_msgs[-1].id)  # #45
        self.assertTrue(resp.context["has_older_messages"])
        self.assertEqual(resp.context["oldest_msg_id"], created_msgs[10].id)
        self.assertEqual(resp.context["total_msg_count"], 45)
        self.assertEqual(resp.context["conv_summary"]["total"], 40)

        html = resp.content.decode("utf-8")
        self.assertIn("45 messages", html)
        self.assertIn('id="contact-older-messages-banner"', html)
        self.assertIn("Contact history item #45", html)
        self.assertNotIn("Contact history item #01", html)








class InlineListCreationAndNoTimezoneTests(TestCase):
    """Lists can be created from where they are picked; timezone is gone from the contact UI."""

    def setUp(self):
        from core.models import User
        self.org = Organization.objects.create(name="Inline Lists Co", slug="inline-lists-co")
        self.other_org = Organization.objects.create(name="Other Co", slug="other-co")
        self.user = User.objects.create_user(
            email="owner@inline.com", password="securepassword123", organization=self.org,
        )
        self.client.force_login(self.user)
        self.ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

    # ---- create_list JSON contract the inline widget relies on
    def test_create_list_ajax_returns_id_and_name(self):
        resp = self.client.post("/contacts/lists/new/", {"name": "Hot Leads"}, **self.ajax)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["name"], "Hot Leads")
        self.assertTrue(ContactList.objects.filter(id=data["id"], organization=self.org).exists())

    def test_create_list_rejects_blank_duplicate_and_too_long(self):
        ContactList.objects.create(organization=self.org, name="Hot Leads")
        for payload in ({"name": "  "}, {"name": "hot leads"}, {"name": "x" * 201}):
            resp = self.client.post("/contacts/lists/new/", payload, **self.ajax)
            self.assertEqual(resp.status_code, 400, payload)
            self.assertEqual(resp.json()["status"], "error")
        self.assertEqual(ContactList.objects.filter(organization=self.org).count(), 1)

    def test_same_list_name_allowed_in_different_orgs(self):
        ContactList.objects.create(organization=self.other_org, name="Hot Leads")
        resp = self.client.post("/contacts/lists/new/", {"name": "Hot Leads"}, **self.ajax)
        self.assertEqual(resp.status_code, 200)

    # ---- a freshly created list can be used straight away
    def test_add_contact_into_just_created_list(self):
        new_id = self.client.post("/contacts/lists/new/", {"name": "Fresh"}, **self.ajax).json()["id"]
        resp = self.client.post("/contacts/new/", {
            "phone": "(650) 253-0000", "first_name": "Ann", "source": "Direct",
            "contact_list_id": str(new_id),
        }, **self.ajax)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ContactList.objects.get(id=new_id).contacts.count(), 1)

    def test_edit_contact_assigns_just_created_list(self):
        c = Contact.objects.create(organization=self.org, phone="+16502530000", source="Direct")
        new_id = self.client.post("/contacts/lists/new/", {"name": "Fresh"}, **self.ajax).json()["id"]
        resp = self.client.post(f"/contacts/{c.id}/edit/", {
            "phone": "+16502530000", "source": "Direct", "lists_submitted": "1", "lists": [str(new_id)],
        }, **self.ajax)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(c.lists.values_list("id", flat=True)), [new_id])

    # ---- the widget is rendered everywhere a list is picked
    def test_list_pages_render_inline_create_widget(self):
        ContactList.objects.create(organization=self.org, name="Existing")
        c = Contact.objects.create(organization=self.org, phone="+16502530000", source="Direct")

        html = self.client.get("/contacts/").content.decode()
        for select_id in ("ac_list_id", "csvTargetList", "remote_list_id"):
            self.assertIn(f'data-select-id="{select_id}"', html)
        self.assertIn("lists/new/", html)

        self.assertNotIn("{#", html)  # multi-line {# #} comments leak as visible text
        self.assertNotIn("Inline \"create a contact list\" widget", html)

        html = self.client.get(f"/contacts/{c.id}/").content.decode()
        self.assertNotIn("{#", html)
        self.assertIn('data-chips-id="detailListChips"', html)
        self.assertIn('id="detailListChips"', html)
        self.assertIn("lists/new/", html)

    # ---- timezone is gone from the contact UI and ignored on input
    def test_no_timezone_inputs_on_contact_pages(self):
        c = Contact.objects.create(organization=self.org, phone="+16502530000", source="Direct")
        for url in ("/contacts/", f"/contacts/{c.id}/"):
            html = self.client.get(url).content.decode().lower()
            self.assertNotIn('name="timezone"', html, url)
            self.assertNotIn("america/new_york", html, url)
            self.assertNotIn("ac_timezone", html, url)

    def test_timezone_in_post_or_lead_payload_is_ignored(self):
        resp = self.client.post("/contacts/new/", {
            "phone": "(650) 253-0000", "source": "Direct", "timezone": "Not/AZone",
        }, **self.ajax)
        self.assertEqual(resp.status_code, 200)
        contact, _ = ingest_lead_data(self.org, {"phone": "+16502530001", "timezone": "Mars/Base", "city": "Fresno"})
        self.assertFalse(hasattr(contact, "timezone"))
        self.assertNotIn("timezone", contact.custom_fields)
        self.assertFalse(ContactField.objects.filter(organization=self.org, key="timezone").exists())

    def test_csv_timezone_column_is_skipped_by_mapping_suggestions(self):
        sug = suggest_column_mappings(self.org, ["Phone", "Timezone", "TZ"])
        self.assertEqual(sug["Phone"]["target"], "phone")
        self.assertEqual(sug["Timezone"]["decision"], "skip")
        self.assertEqual(sug["TZ"]["decision"], "skip")


class SpreadsheetUsDateTests(TestCase):
    """Spreadsheet dates are stored as US MM/DD/YYYY; Excel's day/month mix-up is undone."""

    @staticmethod
    def _xlsx(rows):
        import io
        import openpyxl
        wb = openpyxl.Workbook()
        for r in rows:
            wb.active.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def _column(self, rows):
        from contacts.services import extract_tabular_data
        _headers, data, _warning = extract_tabular_data(self._xlsx(rows), "leads.xlsx")
        return [r[1] for r in data]

    def test_us_file_misread_by_day_month_excel_is_repaired(self):
        import datetime
        col = self._column([["phone", "birthdate"],
                            ["+16502530001", datetime.datetime(1965, 11, 12)],   # Excel read 12/11/1965 as 12 Nov
                            ["+16502530002", "12/25/1965"]])                    # Excel left this as text
        self.assertEqual(col, ["12/11/1965", "12/25/1965"])

    def test_correct_dates_are_kept_and_shown_in_us_format(self):
        import datetime
        col = self._column([["phone", "birthdate"],
                            ["+16502530001", datetime.datetime(1965, 12, 11)],
                            ["+16502530002", datetime.datetime(1965, 12, 25)]])
        self.assertEqual(col, ["12/11/1965", "12/25/1965"])


class EditContactKeepsLeadDataTests(TestCase):
    """Saving the contact form edits only approved fields; lead-form JSON and stage must survive."""

    def test_saving_form_keeps_lead_json_and_stage(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        org = Organization.objects.create(name="Keep Org", slug="keep-org")
        user = get_user_model().objects.create_user(email="keep@example.com", password="pw12345!", organization=org,
                                                    role=get_user_model().Role.OWNER)
        ContactField.objects.create(organization=org, key="city", label="City")
        ContactField.objects.create(organization=org, key="detail", label="detail")   # approved, but hidden on the form
        ContactField.objects.create(organization=org, key="county", label="County")
        contact = Contact.objects.create(organization=org, phone="+16502530111", first_name="Ann", source="Direct",
                                         custom_fields={"city": "Tulare", "detail": '{"person": {"firstName": "Ann"}}',
                                                        "stage": "Qualified", "county": "Tulare"})
        self.client.force_login(user)
        self.client.post(reverse("contacts:edit", args=[contact.id]),
                         {"phone": "+16502530112", "first_name": "Ann", "source": "Direct", "custom_field_city": "Fresno",
                          "custom_field_county": ""})
        contact.refresh_from_db()
        self.assertEqual(contact.custom_fields["city"], "Fresno")
        self.assertEqual(contact.custom_fields["detail"], '{"person": {"firstName": "Ann"}}')
        self.assertEqual(contact.custom_fields["stage"], "Qualified")
        self.assertNotIn("county", contact.custom_fields)          # shown on the form and cleared -> removed
        self.assertEqual(contact.phone, "+16502530112")


class LeadEditorTests(TestCase):
    """Every lead key is shown (even empty ones) and edits are written back in the original's type."""

    PAYLOAD = {
        "person": {"firstName": "Ann", "lastName": "Lee", "occupation": None, "phone": "5594679858",
                   "birthDate": "5/9/1963", "address": {"street": "1 Main St", "city": "Tulare", "county": None}},
        "requestForQuote": None,
        "autoPolicy": {"monthsInsured": 50, "insuranceExpiration": {"year": 2026, "month": 9},
                       "completedTs": {"low": 1788118742, "high": 0, "unsigned": False}, "homeowner": True,
                       "additionalCars": [{"make": "TOYOTA", "year": 2021, "model": "CAMRY", "address": {}}]},
        "vertical": "auto",
    }

    def _names(self, editor):
        return {f["label"]: f for s in editor["sections"]
                for c in s["cards"] + [c for g in s["groups"] for c in g["cards"]] for f in c["fields"]}

    def test_empty_keys_are_shown(self):
        from contacts.lead_editor import build_lead_editor
        fields = self._names(build_lead_editor(self.PAYLOAD))
        for label in ("Request For Quote", "Occupation", "County", "Address", "Vertical", "First Name"):
            self.assertIn(label, fields)
        self.assertTrue(fields["Occupation"]["empty"])
        self.assertEqual(fields["Date of Birth"]["hint"][-3:], "yrs")

    def test_edits_keep_types_and_untouched_values(self):
        import json
        from contacts.lead_editor import build_lead_editor, apply_lead_edits
        cf = {"detail": json.dumps(self.PAYLOAD), "city": "Tulare"}
        fields = self._names(build_lead_editor(self.PAYLOAD))
        posted = [(f["name"], f["value"]) for f in fields.values()]          # everything unchanged
        same, n = apply_lead_edits(cf, posted)
        self.assertEqual(n, 0)
        self.assertIs(same, cf)

        edits = dict(posted)
        edits[fields["Months Continuously Insured"]["name"]] = "60"
        edits[fields["Policy Expiration"]["name"]] = "10/2027"
        edits[fields["Homeowner"]["name"]] = "no"
        edits[fields["Phone"]["name"]] = "(559) 111-2222"
        edits[fields["Occupation"]["name"]] = "Teacher"
        edits[fields["Make"]["name"]] = "HONDA"
        edits[fields["City"]["name"]] = ""
        new_cf, n = apply_lead_edits(cf, edits.items())
        self.assertEqual(n, 7)
        data = json.loads(new_cf["detail"])
        self.assertEqual(data["autoPolicy"]["monthsInsured"], 60)
        self.assertEqual(data["autoPolicy"]["insuranceExpiration"], {"year": 2027, "month": 10})
        self.assertIs(data["autoPolicy"]["homeowner"], False)
        self.assertEqual(data["person"]["phone"], "5591112222")
        self.assertEqual(data["person"]["occupation"], "Teacher")
        self.assertEqual(data["autoPolicy"]["additionalCars"][0]["make"], "HONDA")
        self.assertIsNone(data["person"]["address"]["city"])
        self.assertEqual(data["autoPolicy"]["completedTs"], self.PAYLOAD["autoPolicy"]["completedTs"])
        self.assertEqual(new_cf["city"], "Tulare")

    def test_saving_contact_form_writes_lead_edits(self):
        import json
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        from contacts.lead_editor import build_lead_editor
        org = Organization.objects.create(name="Edit Org", slug="edit-org")
        user = get_user_model().objects.create_user(email="edit@example.com", password="pw12345!", organization=org,
                                                    role=get_user_model().Role.OWNER)
        contact = Contact.objects.create(organization=org, phone="+16502530222", first_name="Ann", source="Direct",
                                         custom_fields={"detail": json.dumps(self.PAYLOAD), "stage": "Qualified"})
        self.client.force_login(user)
        page = self.client.get(reverse("contacts:detail", args=[contact.id]))
        self.assertContains(page, "Lead details")
        self.assertContains(page, 'id="leadSearch"')
        self.assertContains(page, 'name="lead::[&quot;person&quot;,&quot;occupation&quot;]"')
        name = self._names(build_lead_editor(self.PAYLOAD))["Occupation"]["name"]
        self.client.post(reverse("contacts:edit", args=[contact.id]),
                         {"phone": "+16502530222", "first_name": "Ann", "source": "Direct", name: "Nurse"})
        contact.refresh_from_db()
        self.assertEqual(json.loads(contact.custom_fields["detail"])["person"]["occupation"], "Nurse")
        self.assertEqual(contact.custom_fields["stage"], "Qualified")


class ImportColumnSuggestionTests(TestCase):
    """CSV headers that match an existing custom field (key or label) are pre-mapped to it."""

    def test_headers_match_custom_fields_by_key_or_label(self):
        from contacts.services import suggest_column_mappings
        org = Organization.objects.create(name="Map Org", slug="map-org")
        ContactField.objects.create(organization=org, key="zip_code", label="zip_code")
        ContactField.objects.create(organization=org, key="current_insurer", label="Current Insurer")
        ContactField.objects.create(organization=org, key="city", label="city")
        s = suggest_column_mappings(org, ["City", "Zip-Code", "current insurer", "Phone", "notes"])
        self.assertEqual((s["City"]["type"], s["City"]["target"]), ("custom", "city"))
        self.assertEqual(s["Zip-Code"]["target"], "zip_code")
        self.assertEqual(s["current insurer"]["target"], "current_insurer")
        self.assertEqual((s["Phone"]["type"], s["Phone"]["target"]), ("core", "phone"))
        self.assertEqual(s["notes"]["decision"], "skip")


class RemoveFromListTests(TestCase):
    """Taking people out of a contact list (they stay in Contacts)."""

    def setUp(self):
        from core.models import User
        self.org = Organization.objects.create(name="Lists Org", slug="lists-org")
        self.user = User.objects.create_user(email="owner@lists.test", password="pw-123456789", organization=self.org,
                                             role=User.Role.OWNER)
        self.lst = ContactList.objects.create(organization=self.org, name="Pilot")
        self.other = ContactList.objects.create(organization=self.org, name="Other")
        self.a = Contact.objects.create(organization=self.org, phone="+14155550401", first_name="Ann")
        self.b = Contact.objects.create(organization=self.org, phone="+14155550402", first_name="Ben")
        self.lst.contacts.add(self.a, self.b)
        self.other.contacts.add(self.a)
        self.client.force_login(self.user)

    def test_remove_selected_contacts_from_a_list(self):
        r = self.client.post("/contacts/bulk-action/", {"action": "remove_from_list", "list_id": self.lst.id,
                                                       "contact_ids": [self.a.id]},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(r.json()["status"], "success")
        self.assertIn("Removed 1 contact(s) from 'Pilot'", r.json()["message"])
        self.assertEqual(list(self.lst.contacts.all()), [self.b])
        self.assertTrue(Contact.objects.filter(id=self.a.id).exists())            # still a contact
        self.assertIn(self.a, self.other.contacts.all())                           # other lists untouched

    def test_only_real_members_are_counted(self):
        outsider = Contact.objects.create(organization=self.org, phone="+14155550403")
        r = self.client.post("/contacts/bulk-action/", {"action": "remove_from_list", "list_id": self.lst.id,
                                                       "contact_ids": [self.a.id, outsider.id]},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertIn("Removed 1 contact(s)", r.json()["message"])

    def test_list_view_offers_remove_from_this_list(self):
        page = self.client.get(f"/contacts/?list={self.lst.id}").content.decode()
        self.assertIn("Remove from “Pilot”", page)
        self.assertIn("Remove from List", page)
        self.assertNotIn("Remove from “", self.client.get("/contacts/").content.decode())

    def test_another_workspaces_list_is_off_limits(self):
        theirs = ContactList.objects.create(organization=Organization.objects.create(name="X", slug="x-lists"), name="T")
        r = self.client.post("/contacts/bulk-action/", {"action": "remove_from_list", "list_id": theirs.id,
                                                       "contact_ids": [self.a.id]},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(r.status_code, 404)


class ListDetailPageTests(TestCase):
    """One page per contact list: see members, add, remove, rename, delete, send a campaign."""

    def setUp(self):
        from core.models import User
        self.org = Organization.objects.create(name="List Page Org", slug="list-page-org")
        self.user = User.objects.create_user(email="owner@listpage.test", password="pw-123456789", organization=self.org,
                                             role=User.Role.OWNER)
        self.lst = ContactList.objects.create(organization=self.org, name="Pilot")
        self.ann = Contact.objects.create(organization=self.org, phone="+14155550501", first_name="Ann",
                                          sms_status=Contact.SMSStatus.OPTED_IN)
        self.bob = Contact.objects.create(organization=self.org, phone="+14155550502", first_name="Bob",
                                          sms_status=Contact.SMSStatus.OPTED_OUT)
        self.cat = Contact.objects.create(organization=self.org, phone="+14155550503", first_name="Cat")
        self.lst.contacts.add(self.ann, self.bob)
        self.client.force_login(self.user)
        self.ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

    def page(self, query=""):
        r = self.client.get(f"/contacts/lists/{self.lst.id}/{query}")
        self.assertEqual(r.status_code, 200)
        return r

    def test_shows_members_counts_and_who_can_be_added(self):
        r = self.page()
        self.assertEqual(r.context["counts"], {"total": 2, "opted_in": 1, "opted_out": 1, "no_consent": 0})
        self.assertEqual([c.first_name for c in r.context["page_obj"]], ["Ann", "Bob"])
        self.assertEqual([c.first_name for c in r.context["candidates"]], ["Cat"])     # not already in it
        html = r.content.decode()
        for text in ("Add Contacts", "Rename", "Delete List", "Send Campaign", "Remove"):
            self.assertIn(text, html)

    def test_search_and_consent_filters(self):
        self.assertEqual([c.first_name for c in self.page("?q=ann").context["page_obj"]], ["Ann"])
        self.assertEqual([c.first_name for c in self.page("?status=opted_out").context["page_obj"]], ["Bob"])

    def test_lists_page_opens_it(self):
        html = self.client.get("/contacts/lists/").content.decode()
        self.assertIn(f'href="/contacts/lists/{self.lst.id}/"', html)

    def test_add_existing_and_remove(self):
        r = self.client.post(f"/contacts/lists/{self.lst.id}/add-contacts/", {"contact_ids": [self.cat.id]}, **self.ajax)
        self.assertEqual(r.json()["status"], "success")
        self.assertIn(self.cat, self.lst.contacts.all())
        r = self.client.post("/contacts/bulk-action/", {"action": "remove_from_list", "list_id": self.lst.id,
                                                       "contact_ids": [self.ann.id, self.cat.id]}, **self.ajax)
        self.assertIn("Removed 2 contact(s)", r.json()["message"])
        self.assertEqual(list(self.lst.contacts.all()), [self.bob])
        self.assertEqual(Contact.objects.filter(organization=self.org).count(), 3)     # nobody deleted

    def test_new_contact_consent_tick_box_is_respected(self):
        from compliance.models import ConsentEvent
        url = f"/contacts/lists/{self.lst.id}/add-contacts/"
        self.client.post(url, {"new_phone": "+14155550504", "new_first_name": "Dee", "new_sms_status": "opted_in"}, **self.ajax)
        dee = Contact.objects.get(phone="+14155550504")
        self.assertEqual(dee.sms_status, Contact.SMSStatus.OPTED_IN)
        self.assertTrue(ConsentEvent.objects.filter(contact=dee, event_type=ConsentEvent.EventType.OPT_IN).exists())
        self.client.post(url, {"new_phone": "+14155550505", "new_first_name": "Eve"}, **self.ajax)
        self.assertEqual(Contact.objects.get(phone="+14155550505").sms_status, Contact.SMSStatus.UNKNOWN)
        # someone who opted out is never opted back in by this form
        r = self.client.post(url, {"new_phone": self.bob.phone, "new_sms_status": "opted_in"}, **self.ajax)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.sms_status, Contact.SMSStatus.OPTED_OUT)
        self.assertIn("stays opted out", r.json()["message"])

    def test_rename_and_delete(self):
        r = self.client.post(f"/contacts/lists/{self.lst.id}/edit/", {"name": "Pilot 2"}, **self.ajax)
        self.assertEqual(r.json()["name"], "Pilot 2")
        r = self.client.post(f"/contacts/lists/{self.lst.id}/delete/")
        self.assertRedirects(r, "/contacts/lists/", fetch_redirect_response=False)
        self.assertFalse(ContactList.objects.filter(id=self.lst.id).exists())
        self.assertEqual(Contact.objects.filter(organization=self.org).count(), 3)

    def test_send_campaign_starts_with_the_list_ticked(self):
        from messaging.models import PhoneNumber
        PhoneNumber.objects.create(organization=self.org, e164="+14155550599", is_active=True)
        html = self.client.get(f"/campaigns/new/?list={self.lst.id}").content.decode()
        self.assertRegex(html, rf'value="{self.lst.id}"\s+checked')

    def test_another_workspaces_list_is_off_limits(self):
        theirs = ContactList.objects.create(organization=Organization.objects.create(name="Y", slug="y-lp"), name="T")
        self.assertEqual(self.client.get(f"/contacts/lists/{theirs.id}/").status_code, 404)


class ListDetailLiveSearchTests(TestCase):
    """The list page's search-as-you-type and paginator (only the members table is reloaded)."""

    def setUp(self):
        from core.models import User
        self.org = Organization.objects.create(name="Live Org", slug="live-org")
        self.user = User.objects.create_user(email="owner@live.test", password="pw-123456789", organization=self.org,
                                             role=User.Role.OWNER)
        self.lst = ContactList.objects.create(organization=self.org, name="Big")
        people = [Contact(organization=self.org, phone=f"+1415556{n:04d}", first_name=f"Person{n:03d}",
                          sms_status=Contact.SMSStatus.OPTED_IN if n % 2 else Contact.SMSStatus.UNKNOWN)
                  for n in range(130)]
        Contact.objects.bulk_create(people)
        self.lst.contacts.add(*Contact.objects.filter(organization=self.org))
        self.client.force_login(self.user)
        self.url = f"/contacts/lists/{self.lst.id}/"

    def test_typing_returns_just_the_table(self):
        r = self.client.get(self.url, {"q": "Person012", "partial": "1"})
        html = r.content.decode()
        self.assertNotIn("<html", html)                                  # only the members piece
        self.assertIn('id="membersResult" data-total="1"', html)
        self.assertIn("Person012", html)
        self.assertNotIn("Person013", html)

    def test_paginator_pages_and_per_page(self):
        r = self.client.get(self.url, {"partial": "1"})
        html = r.content.decode()
        self.assertEqual(len(r.context["page_obj"].object_list), 25)
        self.assertIn("1–25", html)
        self.assertIn("goToPage(2)", html)
        self.assertIn("…", html)                                        # 6 pages: 1 2 … 6
        r = self.client.get(self.url, {"partial": "1", "page": "6"})
        self.assertIn("126–130", r.content.decode())
        r = self.client.get(self.url, {"partial": "1", "per_page": "100"})
        self.assertEqual(len(r.context["page_obj"].object_list), 100)
        r = self.client.get(self.url, {"partial": "1", "per_page": "9999"})   # only 25/50/100 allowed
        self.assertEqual(len(r.context["page_obj"].object_list), 25)

    def test_search_and_consent_filter_together(self):
        r = self.client.get(self.url, {"partial": "1", "q": "Person00", "status": "opted_in"})
        names = [c.first_name for c in r.context["page_obj"]]
        self.assertEqual(names, ["Person001", "Person003", "Person005", "Person007", "Person009"])

    def test_no_match_message(self):
        html = self.client.get(self.url, {"partial": "1", "q": "zzz"}).content.decode()
        self.assertIn("No members match", html)
        self.assertIn("zzz", html)

    def test_search_box_has_no_browser_autofill(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('id="memberSearch"', html)
        self.assertIn('autocomplete="off"', html)


    def test_short_list_has_no_paginator(self):
        self.lst.contacts.set(Contact.objects.filter(organization=self.org)[:3])
        html = self.client.get(self.url, {"partial": "1"}).content.decode()
        self.assertNotIn("Per page", html)
        self.assertNotIn("goToPage(", html)

    def test_stylesheet_link_changes_when_the_css_changes(self):
        html = self.client.get(self.url).content.decode()
        self.assertRegex(html, r'/static/css/app\.css\?v=\d+')


class ListMemberSearchRuleTests(TestCase):
    """Names match from the start ("ar" is not "spark"); phone numbers match any digits."""

    def setUp(self):
        from core.models import User
        self.org = Organization.objects.create(name="Search Org", slug="search-org")
        user = User.objects.create_user(email="o@search.test", password="pw-123456789", organization=self.org,
                                        role=User.Role.OWNER)
        self.lst = ContactList.objects.create(organization=self.org, name="L")
        self.lst.contacts.add(
            Contact.objects.create(organization=self.org, phone="+15306841631", first_name="spark"),
            Contact.objects.create(organization=self.org, phone="+14155550111", first_name="Arjun", last_name="Mehta",
                                   email="arjun@example.com"),
            Contact.objects.create(organization=self.org, phone="+14155550112", first_name="Maria", last_name="Arce"),
        )
        self.client.force_login(user)

    def names(self, q):
        r = self.client.get(f"/contacts/lists/{self.lst.id}/", {"q": q, "partial": "1"})
        return sorted(c.first_name for c in r.context["page_obj"])

    def test_names_match_from_the_start(self):
        self.assertEqual(self.names("ar"), ["Arjun", "Maria"])      # Arjun, and Maria Arce; not "spark"
        self.assertEqual(self.names("sp"), ["spark"])
        self.assertEqual(self.names("arjun me"), ["Arjun"])         # full name
        self.assertEqual(self.names("rju"), [])

    def test_phone_digits_and_email(self):
        self.assertEqual(self.names("530 684"), ["spark"])
        self.assertEqual(self.names("+1 (530) 684-1631"), ["spark"])
        self.assertEqual(self.names("555011"), ["Arjun", "Maria"])
        self.assertEqual(self.names("arjun@"), ["Arjun"])
