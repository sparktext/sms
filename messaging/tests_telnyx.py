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

    def test_telnyx_message_finalized_outcomes_and_sms_endpoint(self):
        """Telnyx v2 sends terminal delivery state inside message.finalized -> payload.to[0].status."""
        # 1. message.finalized + to[0].status='delivered' on /webhooks/telnyx/status/
        msg_del = Message.objects.create(
            conversation=self.conversation,
            organization=self.org,
            direction=Message.Direction.OUTBOUND,
            sender_type=Message.SenderType.HUMAN,
            body="Finalized delivered",
            status=Message.Status.SENT,
            provider="telnyx",
            provider_message_sid="telnyx_fin_del_1",
        )
        res1 = self.client.post(
            "/webhooks/telnyx/status/",
            data=json.dumps({
                "data": {
                    "event_type": "message.finalized",
                    "payload": {
                        "id": "telnyx_fin_del_1",
                        "to": [{"phone_number": "+16614603931", "status": "delivered"}],
                        "errors": [],
                    },
                }
            }),
            content_type="application/json",
        )
        self.assertEqual(res1.status_code, 200)
        msg_del.refresh_from_db()
        self.assertEqual(msg_del.status, Message.Status.DELIVERED)
        self.assertIsNotNone(msg_del.delivered_at)

        # 2. message.finalized + delivery_failed posted to /webhooks/telnyx/sms/ (profile webhook)
        msg_fail = Message.objects.create(
            conversation=self.conversation,
            organization=self.org,
            direction=Message.Direction.OUTBOUND,
            sender_type=Message.SenderType.AI,
            body="Finalized failed",
            status=Message.Status.SENT,
            provider="telnyx",
            provider_message_sid="telnyx_fin_fail_2",
        )
        res2 = self.client.post(
            "/webhooks/telnyx/sms/",
            data=json.dumps({
                "data": {
                    "event_type": "message.finalized",
                    "payload": {
                        "id": "telnyx_fin_fail_2",
                        "to": [{"phone_number": "+16614603931", "status": "delivery_failed"}],
                        "errors": [{"code": "40003", "detail": "Blocked as spam by carrier"}],
                    },
                }
            }),
            content_type="application/json",
        )
        self.assertEqual(res2.status_code, 200)
        msg_fail.refresh_from_db()
        self.assertEqual(msg_fail.status, Message.Status.UNDELIVERED)
        self.assertEqual(msg_fail.error_code, "40003")
        self.assertIn("Blocked as spam", msg_fail.error_message)

        # 3. message.finalized + delivery_unconfirmed keeps status as SENT (does not falsely mark delivered/failed)
        msg_unconf = Message.objects.create(
            conversation=self.conversation,
            organization=self.org,
            direction=Message.Direction.OUTBOUND,
            sender_type=Message.SenderType.HUMAN,
            body="Finalized unconfirmed",
            status=Message.Status.SENT,
            provider="telnyx",
            provider_message_sid="telnyx_fin_unconf_3",
        )
        res3 = self.client.post(
            "/webhooks/telnyx/status/",
            data=json.dumps({
                "data": {
                    "event_type": "message.finalized",
                    "payload": {
                        "id": "telnyx_fin_unconf_3",
                        "to": [{"phone_number": "+16614603931", "status": "delivery_unconfirmed"}],
                        "errors": [],
                    },
                }
            }),
            content_type="application/json",
        )
        self.assertEqual(res3.status_code, 200)
        msg_unconf.refresh_from_db()
        self.assertEqual(msg_unconf.status, Message.Status.SENT)
        self.assertIsNone(msg_unconf.delivered_at)

        # 4. Inbox chat pane renders date divider, date+time timestamp, and status DOM hooks
        self.client.force_login(self.user)
        inbox_resp = self.client.get(f"/inbox/{self.conversation.id}/")
        self.assertEqual(inbox_resp.status_code, 200)
        inbox_html = inbox_resp.content.decode("utf-8")
        self.assertIn(f'id="msg-status-{msg_del.id}"', inbox_html)
        self.assertIn(msg_del.created_at.strftime("%b %d, %I:%M %p"), inbox_html)
        self.assertIn(msg_del.created_at.strftime("%b %d, %Y"), inbox_html)
        self.assertIn("✕ Undelivered", inbox_html)
        self.assertIn("Blocked as spam by carrier", inbox_html)
        self.assertIn('id="global-breadcrumb-conv"', inbox_html)

