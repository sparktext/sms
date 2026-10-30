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


