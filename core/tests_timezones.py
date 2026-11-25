"""Times match the agency's time zone everywhere (the server runs in UTC): pages, chat messages, and the times pushed
live to open chats. Someone who picked their own zone on the Calendar sees theirs."""
from datetime import UTC, datetime
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from contacts.models import Contact
from core.models import Organization, User, UserProfile
from messaging.models import Conversation, Message, PhoneNumber
from messaging.utils import format_chat_timestamp, format_clock

AT = datetime(2026, 11, 25, 18, 53, tzinfo=UTC)          # 10:53 AM in California, 06:53 PM UTC


@override_settings(MESSAGING_PROVIDER="fake")
class AgencyTimeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Pacific", slug="pacific-tz", timezone="America/Los_Angeles")
        self.user = User.objects.create_user(email="t@tz.test", password="pw-123456789", organization=self.org,
                                             role=User.Role.OWNER)
        phone = PhoneNumber.objects.create(organization=self.org, e164="+14155550900", is_active=True)
        contact = Contact.objects.create(organization=self.org, phone="+15305550900", first_name="Sam")
        self.conv = Conversation.objects.create(organization=self.org, contact=contact, phone_number=phone)
        self.msg = Message.objects.create(organization=self.org, conversation=self.conv, body="call me now",
                                          direction=Message.Direction.INBOUND, sender_type=Message.SenderType.CUSTOMER,
                                          status=Message.Status.RECEIVED)
        Message.objects.filter(pk=self.msg.pk).update(created_at=AT)
        self.client.force_login(self.user)

    def chat_times(self):
        return [m["created_at"] for m in self.client.get(f"/inbox/{self.conv.id}/messages/").json()["messages"]]

    def test_chat_messages_show_the_agencys_time(self):
        self.assertEqual(self.chat_times(), ["Nov 25, 10:53 AM"])          # was "Nov 25, 06:53 PM" (UTC)

    def test_someone_with_their_own_time_zone_sees_theirs(self):
        UserProfile.objects.update_or_create(user=self.user, defaults={"timezone": "America/New_York"})
        self.assertEqual(self.chat_times(), ["Nov 25, 01:53 PM"])

    def test_the_inbox_page_shows_the_agencys_time(self):
        html = self.client.get(f"/inbox/{self.conv.id}/").content.decode()
        self.assertIn("10:53", html)
        self.assertNotIn("06:53 PM", html)

    def test_times_pushed_live_use_the_agencys_zone_whoever_is_signed_in(self):
        self.assertEqual(format_chat_timestamp(AT, org=self.org), "Nov 25, 10:53 AM")
        self.assertEqual(format_clock(AT, org=self.org), "10:53 AM")
        with timezone.override("Asia/Kolkata"):                          # whoever triggered it is elsewhere
            self.assertEqual(format_chat_timestamp(AT, org=self.org), "Nov 25, 10:53 AM")

    def test_an_incoming_text_is_pushed_with_the_agencys_time(self):
        pushed = []
        with patch("messaging.ws_broadcast.broadcast_chat_event", side_effect=lambda cid, p: pushed.append(p)), \
                patch("django.utils.timezone.now", return_value=AT), self.settings(TWILIO_VALIDATE_WEBHOOKS=True):
            self.client.logout()                                          # Twilio isn't signed in
            self.client.post("/webhooks/twilio/sms/", {"From": "+15305550900", "To": "+14155550900",
                                                       "Body": "hello", "MessageSid": "SM_tz_1"},
                             HTTP_X_TWILIO_SIGNATURE="valid_test_signature")
        times = [p["message"]["created_at"] for p in pushed if p.get("message")]
        self.assertTrue(times)
        self.assertTrue(all(t.endswith("10:53 AM") for t in times), times)

    def test_a_request_never_leaks_its_zone_into_the_next_one(self):
        UserProfile.objects.update_or_create(user=self.user, defaults={"timezone": "Asia/Kolkata"})
        self.client.get(f"/inbox/{self.conv.id}/messages/")
        self.assertEqual(timezone.get_current_timezone_name(), "UTC")      # back to the server default
