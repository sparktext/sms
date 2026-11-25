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

    def test_opt_outs_in_their_own_words_are_stop(self):
        from compliance.services import is_stop_phrase
        for text in ("please stop texting me", "Stop messaging me!", "don't text me anymore", "do not contact me",
                     "remove me from your list", "take me off your list please", "unsubscribe me",
                     "opt me out", "no more texts", "lose my number", "leave me alone", "stop please",
                     "Please STOP", "STOP NOW", "not interested, stop"):
            self.assertEqual(classify_keyword(text), "STOP", text)
            self.assertTrue(is_stop_phrase(text), text)
        self.assertFalse(is_stop_phrase("STOP"))            # the bare keyword: the carrier confirms it

    def test_everyday_sentences_with_stop_words_are_not_opt_outs(self):
        for text in ("actually please cancel the call", "can I stop by the office tomorrow?",
                     "don't stop, I want the quote", "can you text me the details?", "when does my policy end?",
                     "I want to quit my current insurer", "call me after work"):
            self.assertIsNone(classify_keyword(text), text)

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



class QuietHoursTests(TestCase):
    """Fixed texting hours for every SparkText customer: federal TCPA + stricter state rules, in the lead's time."""

    def setUp(self):
        self.org = Organization.objects.create(name="QH", slug="qh", timezone="America/Los_Angeles")
        self.n = 0

    def lead(self, state=None, phone=None):
        self.n += 1
        return Contact.objects.create(organization=self.org, phone=phone or f"+1404555{self.n:04d}",
                                      custom_fields={"state": state} if state else {})

    def at(self, zone, y, m, d, hh, mm=0):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(zone))

    def ok(self, contact, moment):
        from compliance.quiet_hours import can_text_at
        return can_text_at(contact, moment)

    def test_federal_window_in_a_state_without_its_own_rule(self):
        ga = self.lead("GA")                                    # Wed Nov 25, 2026
        self.assertFalse(self.ok(ga, self.at("America/New_York", 2026, 11, 25, 7, 59)))
        self.assertTrue(self.ok(ga, self.at("America/New_York", 2026, 11, 25, 8, 0)))
        self.assertTrue(self.ok(ga, self.at("America/New_York", 2026, 11, 25, 20, 59)))
        self.assertFalse(self.ok(ga, self.at("America/New_York", 2026, 11, 25, 21, 0)))

    def test_stricter_states(self):
        fl, tx, ri = self.lead("FL"), self.lead("TX"), self.lead("RI")
        self.assertFalse(self.ok(fl, self.at("America/New_York", 2026, 11, 25, 20, 30)))      # FL ends at 8 PM
        self.assertFalse(self.ok(tx, self.at("America/Chicago", 2026, 11, 29, 11, 0)))       # TX Sunday from noon
        self.assertTrue(self.ok(tx, self.at("America/Chicago", 2026, 11, 29, 12, 0)))
        self.assertFalse(self.ok(ri, self.at("America/New_York", 2026, 11, 28, 9, 30)))      # RI Saturday 10-5
        self.assertTrue(self.ok(ri, self.at("America/New_York", 2026, 11, 28, 10, 30)))
        self.assertFalse(self.ok(ri, self.at("America/New_York", 2026, 11, 25, 18, 30)))     # RI weekdays end at 6

    def test_pennsylvania_rule_starts_oct_18_2026(self):
        pa = self.lead("PA")
        self.assertTrue(self.ok(pa, self.at("America/New_York", 2026, 10, 11, 10, 0)))       # a Sunday, before
        self.assertFalse(self.ok(pa, self.at("America/New_York", 2026, 11, 29, 10, 0)))      # a Sunday, after
        self.assertFalse(self.ok(pa, self.at("America/New_York", 2026, 11, 25, 19, 30)))     # after 7 PM
        self.assertTrue(self.ok(pa, self.at("America/New_York", 2026, 11, 25, 9, 0)))

    def test_holidays(self):
        from compliance.quiet_hours import federal_holidays
        from datetime import date
        days = federal_holidays(2026)
        self.assertIn(date(2026, 11, 26), days)                 # Thanksgiving
        self.assertIn(date(2026, 7, 3), days)                   # July 4 is a Saturday: observed Friday
        ut, ga = self.lead("UT"), self.lead("GA")
        self.assertFalse(self.ok(ut, self.at("America/Denver", 2026, 11, 26, 12, 0)))        # UT: no holidays
        self.assertTrue(self.ok(ga, self.at("America/New_York", 2026, 11, 26, 12, 0)))

    def test_area_code_when_the_lead_has_no_state(self):
        from compliance.quiet_hours import lead_location
        from scheduling.calls import lead_timezone
        philly = self.lead(phone="+12155550123")
        self.assertEqual(lead_location(philly).state, "PA")
        self.assertEqual(lead_location(philly).source, "phone area code")
        self.assertFalse(self.ok(philly, self.at("America/New_York", 2026, 11, 29, 12, 0)))  # PA: no Sundays
        ny = self.lead(phone="+12125550123")
        self.assertEqual(lead_timezone(ny, self.org), "America/New_York")     # not the agency's Pacific time

    def test_unknown_location_uses_the_safe_window(self):
        from compliance.quiet_hours import lead_location
        tollfree = self.lead(phone="+18885550123")
        self.assertEqual(lead_location(tollfree).source, "unknown")
        self.assertFalse(self.ok(tollfree, self.at("America/New_York", 2026, 11, 25, 11, 0)))  # 8 AM Pacific
        self.assertTrue(self.ok(tollfree, self.at("America/New_York", 2026, 11, 25, 13, 0)))   # 10 AM Pacific
        self.assertFalse(self.ok(tollfree, self.at("America/New_York", 2026, 11, 25, 20, 30)))
        self.assertFalse(self.ok(tollfree, self.at("America/New_York", 2026, 11, 29, 14, 0)))  # Sunday
