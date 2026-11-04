import json
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client, override_settings
from django.utils import timezone

from core.models import Organization, User
from contacts.models import Contact
from compliance.models import ConsentEvent
from messaging.models import PhoneNumber, Conversation, Message
from messaging.providers.telnyx_provider import TelnyxProvider
from messaging.providers import get_sms_provider


class TelnyxIntegrationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Telnyx Org", slug="telnyx-org")
        self.user = User.objects.create_user(
            email="owner@telnyxorg.com",
            password="password123",
            organization=self.org,
            role=User.Role.OWNER,
        )
        self.phone = PhoneNumber.objects.create(
            organization=self.org,
            e164="+16612718246",
            label="Telnyx 10DLC",
            is_active=True,
        )
        self.contact = Contact.objects.create(
            organization=self.org,
            phone="+16614603931",
            first_name="Test",
            last_name="Customer",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        self.conversation = Conversation.objects.create(
            organization=self.org,
            contact=self.contact,
            phone_number=self.phone,
        )
        self.client = Client()

    @override_settings(MESSAGING_PROVIDER="telnyx")
    def test_get_sms_provider_returns_telnyx_provider(self):
        provider = get_sms_provider()
        self.assertIsInstance(provider, TelnyxProvider)

    @patch("requests.post")
    def test_telnyx_send_sms_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "id": "telnyx_msg_12345",
                "direction": "outbound",
                "to": [{"phone_number": "+16614603931", "status": "queued"}],
            }
        }
        mock_post.return_value = mock_resp

        provider = TelnyxProvider(api_key="KEY_TEST_123")
        res = provider.send_sms(
            from_number="+16612718246",
            to_number="+16614603931",
            body="Test message via Telnyx",
        )

        self.assertTrue(res.success)
        self.assertEqual(res.provider_message_sid, "telnyx_msg_12345")
        self.assertEqual(res.status, "queued")

    @patch("requests.post")
    def test_telnyx_send_sms_handles_opt_out_error(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = '{"errors":[{"code":"40003","detail":"Recipient is opted out"}]}'
        mock_resp.json.return_value = {
            "errors": [
                {"code": "40003", "detail": "Recipient has opted out of messaging"}
            ]
        }
        mock_post.return_value = mock_resp

        provider = TelnyxProvider(api_key="KEY_TEST_123")
        res = provider.send_sms(
            from_number="+16612718246",
            to_number="+16614603931",
            body="Test text",
        )

        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "21610")  # Standardized opt-out code
        self.assertIn("opted out", res.error_message.lower())

    def test_telnyx_inbound_webhook_creates_message_and_conversation(self):
        payload = {
            "data": {
                "event_type": "message.received",
                "id": "evt_inbound_1",
                "payload": {
                    "id": "telnyx_msg_inbound_123",
                    "direction": "inbound",
                    "from": {"phone_number": "+16614603931"},
                    "to": [{"phone_number": "+16612718246"}],
                    "text": "Hello Telnyx, I need assistance with a quote",
                },
            }
        }

        res = self.client.post(
            "/webhooks/telnyx/sms/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)

        # Verify inbound message created
        msg = Message.objects.filter(provider_message_sid="telnyx_msg_inbound_123").first()
        self.assertIsNotNone(msg)
        self.assertEqual(msg.body, "Hello Telnyx, I need assistance with a quote")
        self.assertEqual(msg.direction, Message.Direction.INBOUND)
        self.assertEqual(msg.provider, "telnyx")
        self.assertEqual(msg.conversation, self.conversation)

    def test_telnyx_inbound_stop_keyword_records_opt_out(self):
        payload = {
            "data": {
                "event_type": "message.received",
                "id": "evt_inbound_stop",
                "payload": {
                    "id": "telnyx_stop_123",
                    "direction": "inbound",
                    "from": {"phone_number": "+16614603931"},
                    "to": [{"phone_number": "+16612718246"}],
                    "text": "STOP",
                },
            }
        }

        res = self.client.post(
            "/webhooks/telnyx/sms/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)

        self.contact.refresh_from_db()
        self.assertEqual(self.contact.sms_status, Contact.SMSStatus.OPTED_OUT)

        evt = ConsentEvent.objects.filter(
            contact=self.contact,
            event_type=ConsentEvent.EventType.OPT_OUT,
        ).first()
        self.assertIsNotNone(evt)

    def test_telnyx_status_callback_advances_delivery_status(self):
        msg = Message.objects.create(
            conversation=self.conversation,
            organization=self.org,
            direction=Message.Direction.OUTBOUND,
            sender_type=Message.SenderType.HUMAN,
            body="Outbound text",
            status=Message.Status.SENT,
            provider="telnyx",
            provider_message_sid="telnyx_out_999",
        )

        payload = {
            "data": {
                "event_type": "message.delivered",
                "id": "evt_del_1",
                "payload": {
                    "id": "telnyx_out_999",
                    "to": [{"phone_number": "+16614603931", "status": "delivered"}],
                },
            }
        }

        res = self.client.post(
            "/webhooks/telnyx/status/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)

        msg.refresh_from_db()
        self.assertEqual(msg.status, Message.Status.DELIVERED)
        self.assertIsNotNone(msg.delivered_at)

