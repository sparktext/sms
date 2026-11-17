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
                "timezone": "America/Los_Angeles",
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
        self.assertEqual(c2.timezone, "America/Los_Angeles")
        self.assertEqual(c2.custom_fields["favorite_topping"], "Mushrooms")
        self.assertEqual(c2.source, Contact.Source.CSV)

    def test_input_validations_email_timezone_lengths_value_types(self):
        """Validates email format, IANA timezone, string lengths, and custom field value types."""
        with self.assertRaises(ValidationError):
            save_contact(self.org1, {"phone": "+16502530000", "email": "not-an-email"})

        with self.assertRaises(ValidationError):
            save_contact(self.org1, {"phone": "+16502530000", "timezone": "Mars/Olympus_Mons"})

        c, _created = save_contact(self.org1, {"phone": "+16502530000", "timezone": "America/New_York"})
        self.assertEqual(c.timezone, "America/New_York")

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
                "timezone": "America/Chicago",
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
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        json_data = resp.json()
        self.assertEqual(json_data["status"], "success")
        self.assertEqual(json_data["phone"], "+16502531111")
        self.assertEqual(json_data["first_name"], "Jane")
        self.assertEqual(json_data["last_name"], "Taylor")
        self.assertTrue(Contact.objects.filter(organization=self.org, phone="+16502531111").exists())

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
                "timezone": "America/New_York",
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
        self.client.login(username=self.user.email, password="password123")
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
        self.assertContains(get_resp, "Conversation History")
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
        self.assertContains(resp, "Conversation Summary")
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
        self.assertIn('aria-label="Back to Contacts"', html[main_idx:])
        self.assertNotIn("← Back to Contacts", html)
        self.assertLess(
            html.index('aria-label="Back to Contacts"', main_idx),
            html.index("Liam", main_idx),
        )
        # Name & metadata row sits below the top back arrow + right-side action toolbar
        self.assertLess(
            html.index("Message Contact", main_idx),
            html.index("Liam", main_idx),
        )
        # Right column (Consent Status & Conversation Summary) streams in HTML before long Conversation History thread
        self.assertLess(html.index("Consent Status", main_idx), html.index("Conversation History", main_idx))
        self.assertLess(html.index("Conversation Summary", main_idx), html.index("Conversation History", main_idx))
        self.assertIn("lg:col-start-3 lg:row-start-1 lg:row-span-2", html[main_idx:])
        self.assertIn("lg:col-span-2 lg:col-start-1 lg:row-start-2", html[main_idx:])
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






