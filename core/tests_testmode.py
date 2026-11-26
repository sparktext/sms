"""Local test mode (TEST_IGNORE_TIME_LIMITS): every time limit is off for trying things at any hour, but it can never
be on under the test suite or in production (DEBUG off), and every page says so while it's on."""
from datetime import date, datetime
from datetime import time as time_cls
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.conf import settings
from django.test import TestCase, override_settings

from compliance.quiet_hours import LeadLocation, allowed_for
from contacts.models import Contact
from core.models import Organization, User
from scheduling import calls
from scheduling.models import ClosedDay

LA = ZoneInfo("America/Los_Angeles")
SAT_3AM = datetime(2027, 1, 9, 3, 0, tzinfo=LA)          # a Saturday, 3 AM: nothing is allowed normally


class TestModeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Test Mode", slug="test-mode", timezone="America/Los_Angeles",
                                               call_days=[0, 1, 2, 3, 4], call_start=time_cls(9), call_end=time_cls(18))
        ClosedDay.objects.create(organization=self.org, date=date(2027, 1, 9), name="Office party")
        self.where = LeadLocation("CA", ("America/Los_Angeles",), "lead details")

    def test_it_is_never_on_under_the_test_suite(self):
        self.assertFalse(settings.TEST_IGNORE_TIME_LIMITS)

    def test_normally_the_limits_apply(self):
        self.assertFalse(calls.can_call_at(SAT_3AM, "America/Los_Angeles", self.org))
        self.assertFalse(allowed_for(self.where, SAT_3AM))

    @override_settings(TEST_IGNORE_TIME_LIMITS=True)
    def test_test_mode_lifts_every_time_limit(self):
        org = Organization.objects.get(pk=self.org.pk)
        self.assertTrue(calls.can_call_at(SAT_3AM, "America/Los_Angeles", org))   # closed day, Saturday, 3 AM
        self.assertIsNone(calls.closed_reason_at(SAT_3AM, org))
        self.assertTrue(allowed_for(self.where, SAT_3AM))                           # campaign texting hours
        with patch("scheduling.calls.call_team") as team:                            # someone is available
            team.return_value.filter.return_value.exists.return_value = True
            self.assertEqual(calls.can_call_now(org, "America/Los_Angeles", SAT_3AM), (True, ""))
        contact = Contact.objects.create(organization=org, phone="+15305550101", custom_fields={"state": "CA"})
        from compliance.quiet_hours import can_text_at
        self.assertTrue(can_text_at(contact, SAT_3AM, org))

    @override_settings(TEST_IGNORE_TIME_LIMITS=True)
    def test_every_page_says_test_mode_is_on(self):
        user = User.objects.create_user(email="t@mode.test", password="pw-123456789", organization=self.org,
                                        role=User.Role.OWNER)
        self.client.force_login(user)
        self.assertIn("Test mode: all time limits are OFF", self.client.get("/calendar/").content.decode())

    def test_no_banner_normally(self):
        user = User.objects.create_user(email="n@mode.test", password="pw-123456789", organization=self.org,
                                        role=User.Role.OWNER)
        self.client.force_login(user)
        self.assertNotIn("Test mode: all time limits are OFF", self.client.get("/calendar/").content.decode())
