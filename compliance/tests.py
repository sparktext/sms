from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.test import TestCase

from compliance.admin import ConsentEventAdmin
from compliance.models import ConsentEvent
from compliance.services import (
    can_reply,
    can_send_marketing,
    classify_keyword,
    record_consent,
)
from contacts.models import Contact
from core.models import Organization


class KeywordClassificationTests(TestCase):
    def test_classify_stop_synonyms(self):
        """All standard STOP synonyms return 'STOP', case-insensitive with punctuation ignored."""
        stop_inputs = [
            "stop",
            "STOP",
            "Stop!",
            " stopALL ",
            "unsubscribe.",
            "CANCEL",
            "end",
            "quit?",
            "#STOP",
        ]
        for word in stop_inputs:
            self.assertEqual(classify_keyword(word), "STOP", f"Failed for {word}")

    def test_classify_start_synonyms(self):
        """All standard START synonyms return 'START'."""
        start_inputs = [
            "start",
            "START",
            " Start! ",
            "unstop",
            "UNSTOP.",
        ]
        for word in start_inputs:
            self.assertEqual(classify_keyword(word), "START", f"Failed for {word}")

    def test_classify_help(self):
        """HELP is recognized as 'HELP'."""
        help_inputs = ["help", "HELP", " Help! "]
        for word in help_inputs:
            self.assertEqual(classify_keyword(word), "HELP", f"Failed for {word}")

    def test_yes_is_never_start(self):
        """'yes' must NOT be treated as START (on purpose for AI conversational prompts)."""
        yes_inputs = ["yes", "YES", "Yes!", " yes ", "yEs"]
        for word in yes_inputs:
            self.assertIsNone(classify_keyword(word), f"'yes' variant '{word}' should return None")

    def test_arbitrary_sentences_return_none(self):
        """Conversational sentences return None."""
        other_inputs = [
            "Hello, what time do you open?",
            "I want a large pizza",
            "sounds good",
            "",
            None,
        ]
        for word in other_inputs:
            self.assertIsNone(classify_keyword(word))


class ConsentRecordingAndMessagingPermissionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Tacos Corp", slug="tacos")
        self.contact = Contact.objects.create(
            organization=self.org,
            phone="+16502530000",
            first_name="Bob",
            last_name="",
            sms_status=Contact.SMSStatus.UNKNOWN,
        )

    def test_initial_unknown_permissions(self):
        """No consent means can_send_marketing is False, but can_reply is True (customer texted first)."""
        self.assertEqual(self.contact.sms_status, Contact.SMSStatus.UNKNOWN)
        self.assertFalse(can_send_marketing(self.contact))
        self.assertTrue(can_reply(self.contact))

    def test_record_opt_in_updates_ledger_and_status(self):
        """record_consent opt_in writes ConsentEvent and updates contact.sms_status atomically."""
        event = record_consent(
            contact=self.contact,
            event_type=ConsentEvent.EventType.OPT_IN,
            source=ConsentEvent.Source.KEYWORD,
            proof_text="START text received at 10:00AM",
        )

        self.contact.refresh_from_db()
        self.assertEqual(self.contact.sms_status, Contact.SMSStatus.OPTED_IN)
        self.assertEqual(event.event_type, ConsentEvent.EventType.OPT_IN)
        self.assertEqual(event.contact, self.contact)
        self.assertEqual(event.organization, self.org)
        self.assertEqual(ConsentEvent.objects.filter(contact=self.contact).count(), 1)

        # Now marketing can be sent
        self.assertTrue(can_send_marketing(self.contact))
        self.assertTrue(can_reply(self.contact))

    def test_record_opt_out_updates_ledger_and_blocks_all(self):
        """record_consent opt_out writes ConsentEvent and updates contact.sms_status to OPTED_OUT."""
        # Start opted in
        record_consent(
            contact=self.contact,
            event_type=ConsentEvent.EventType.OPT_IN,
            source=ConsentEvent.Source.WEB_FORM,
            proof_text="Web form signup",
        )
        self.assertTrue(can_send_marketing(self.contact))

        # Now opt-out
        event_out = record_consent(
            contact=self.contact,
            event_type=ConsentEvent.EventType.OPT_OUT,
            source=ConsentEvent.Source.KEYWORD,
            proof_text="STOP message",
        )
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.sms_status, Contact.SMSStatus.OPTED_OUT)
        self.assertEqual(event_out.event_type, ConsentEvent.EventType.OPT_OUT)
        self.assertEqual(ConsentEvent.objects.filter(contact=self.contact).count(), 2)

        # Neither marketing nor conversational reply is permitted
        self.assertFalse(can_send_marketing(self.contact))
        self.assertFalse(can_reply(self.contact))

    def test_consent_event_immutability(self):
        """ConsentEvent ledger rows cannot be edited or deleted through the application."""
        event = record_consent(
            contact=self.contact,
            event_type=ConsentEvent.EventType.OPT_IN,
            source=ConsentEvent.Source.MANUAL,
            proof_text="Admin verbal opt-in",
        )

        # Attempting to save an existing row raises ValidationError
        event.proof_text = "Modified proof text"
        with self.assertRaises(ValidationError):
            event.save()

        # Attempting to delete raises ValidationError
        with self.assertRaises(ValidationError):
            event.delete()


class ComplianceAdminTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.admin = ConsentEventAdmin(ConsentEvent, self.site)

    def test_consent_event_admin_is_completely_read_only(self):
        """Admin disallows adding, changing, and deleting ConsentEvents."""
        class MockRequest:
            pass

        req = MockRequest()
        self.assertFalse(self.admin.has_add_permission(req))
        self.assertFalse(self.admin.has_change_permission(req))
        self.assertFalse(self.admin.has_delete_permission(req))


class ComplianceViewsTests(TestCase):
    def setUp(self):
        from core.models import User
        self.org = Organization.objects.create(name="Compliance Cafe", slug="comp-cafe")
        self.user = User.objects.create_user(
            email="legal@compcafe.com",
            password="secretpassword123",
            organization=self.org,
        )
        self.client.force_login(self.user)
        self.contact = Contact.objects.create(
            organization=self.org,
            phone="+16502535555",
            first_name="Alice",
            last_name="Walker",
            sms_status=Contact.SMSStatus.UNKNOWN,
        )

    def test_compliance_dashboard_renders_cleanly(self):
        record_consent(
            self.contact,
            ConsentEvent.EventType.OPT_IN,
            ConsentEvent.Source.WEB_FORM,
            "Web form signup via checkout",
        )
        resp = self.client.get("/compliance/")
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "compliance/dashboard.html")
        self.assertContains(resp, "+16502535555")
        self.assertContains(resp, "OPT IN")
        self.assertContains(resp, "Web form signup via checkout")

    def test_compliance_dashboard_filter_by_event_type(self):
        c2 = Contact.objects.create(organization=self.org, phone="+16502536666", first_name="Bob", last_name="")
        record_consent(self.contact, ConsentEvent.EventType.OPT_IN, ConsentEvent.Source.KEYWORD, "START text")
        record_consent(c2, ConsentEvent.EventType.OPT_OUT, ConsentEvent.Source.KEYWORD, "STOP text")

        resp = self.client.get("/compliance/?event_type=opt_out")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "+16502536666")
        self.assertNotContains(resp, "START text")

    def test_record_consent_view_ajax_updates_status(self):
        resp = self.client.post(
            "/compliance/record/",
            data={
                "contact_id": self.contact.id,
                "event_type": "opt_in",
                "source": "manual",
                "proof_text": "Signed consent document on file #1234",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.sms_status, Contact.SMSStatus.OPTED_IN)
        self.assertTrue(ConsentEvent.objects.filter(contact=self.contact, event_type="opt_in").exists())

    def test_test_keyword_view(self):
        resp = self.client.get("/compliance/test-keyword/?text=STOPALL", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["classification"], "STOP")

        resp_yes = self.client.get("/compliance/test-keyword/?text=YES", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp_yes.status_code, 200)
        self.assertIsNone(resp_yes.json()["classification"])

    def test_export_ledger_csv(self):
        record_consent(self.contact, ConsentEvent.EventType.OPT_IN, ConsentEvent.Source.WEB_FORM, "Legal checkout proof")
        resp = self.client.get("/compliance/export/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        self.assertIn("attachment; filename=", resp["Content-Disposition"])
        content = resp.content.decode("utf-8")
        self.assertIn("+16502535555", content)
        self.assertIn("Legal checkout proof", content)

