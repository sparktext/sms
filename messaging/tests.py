from datetime import timedelta
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from django.core.exceptions import ValidationError, PermissionDenied

from core.models import Organization, User
from contacts.models import Contact
from compliance.models import ConsentEvent
from messaging.models import PhoneNumber, Conversation, Message
from messaging.services import can_send_manual, send_message
from messaging.providers import FakeProvider


@override_settings(MESSAGING_PROVIDER="fake", TWILIO_VALIDATE_WEBHOOKS=True)
class MessagingTests(TestCase):
    def setUp(self):
        FakeProvider.reset()

        # Org A
        self.org_a = Organization.objects.create(name="Business A", slug="biz-a")
        self.user_a = User.objects.create_user(
            email="owner_a@example.com",
            password="password123",
            organization=self.org_a,
            role=User.Role.OWNER,
        )
        self.phone_a = PhoneNumber.objects.create(
            organization=self.org_a,
            e164="+12137997090",
            label="Main Support",
            is_active=True,
        )

        # Org B
        self.org_b = Organization.objects.create(name="Business B", slug="biz-b")
        self.user_b = User.objects.create_user(
            email="owner_b@example.com",
            password="password123",
            organization=self.org_b,
            role=User.Role.OWNER,
        )
        self.phone_b = PhoneNumber.objects.create(
            organization=self.org_b,
            e164="+14155552671",
            label="Sales",
            is_active=True,
        )

        self.client = Client()

    def test_webhook_signature_validation(self):
        """Webhook rejects invalid signature (403) and accepts valid one."""
        payload = {
            "From": "+16502530000",
            "To": "+12137997090",
            "Body": "Hello",
            "MessageSid": "SM_sig_test_1",
        }

        # Invalid signature
        res = self.client.post(
            "/webhooks/twilio/sms/",
            payload,
            HTTP_X_TWILIO_SIGNATURE="invalid_signature_xyz",
        )
        self.assertEqual(res.status_code, 403)

        # Valid signature
        res_ok = self.client.post(
            "/webhooks/twilio/sms/",
            payload,
            HTTP_X_TWILIO_SIGNATURE="valid_test_signature",
        )
        self.assertEqual(res_ok.status_code, 200)
        self.assertIn("<Response></Response>", res_ok.content.decode("utf-8"))

    def test_inbound_sms_creates_contact_and_conversation(self):
        """Inbound text creates contact (source inbound, status unknown), conversation and message."""
        payload = {
            "From": "+16502531111",
            "To": "+12137997090",
            "Body": "Interested in your service",
            "MessageSid": "SM_new_contact_1",
        }
        res = self.client.post(
            "/webhooks/twilio/sms/",
            payload,
            HTTP_X_TWILIO_SIGNATURE="valid_test_signature",
        )
        self.assertEqual(res.status_code, 200)

        # Contact check
        contact = Contact.objects.get(organization=self.org_a, phone="+16502531111")
        self.assertEqual(contact.source, Contact.Source.INBOUND)
        self.assertEqual(contact.sms_status, Contact.SMSStatus.UNKNOWN)

        # Conversation check
        conv = Conversation.objects.get(organization=self.org_a, contact=contact, phone_number=self.phone_a)
        self.assertEqual(conv.status, Conversation.Status.OPEN)
        self.assertEqual(conv.unread_count, 1)

        # Message check
        msg = Message.objects.get(conversation=conv, provider_message_sid="SM_new_contact_1")
        self.assertEqual(msg.direction, Message.Direction.INBOUND)
        self.assertEqual(msg.sender_type, Message.SenderType.CUSTOMER)
        self.assertEqual(msg.status, Message.Status.RECEIVED)
        self.assertEqual(msg.body, "Interested in your service")

    def test_inbound_idempotency(self):
        """The same MessageSid twice creates only one message."""
        payload = {
            "From": "+16502532222",
            "To": "+12137997090",
            "Body": "Testing duplicate delivery",
            "MessageSid": "SM_dup_123",
        }
        res1 = self.client.post("/webhooks/twilio/sms/", payload, HTTP_X_TWILIO_SIGNATURE="valid_test_signature")
        self.assertEqual(res1.status_code, 200)

        res2 = self.client.post("/webhooks/twilio/sms/", payload, HTTP_X_TWILIO_SIGNATURE="valid_test_signature")
        self.assertEqual(res2.status_code, 200)

        self.assertEqual(Message.objects.filter(provider_message_sid="SM_dup_123").count(), 1)

    def test_compliance_keywords(self):
        """STOP -> opted_out + ConsentEvent; START -> opted_in + ConsentEvent; 'yes' and HELP change nothing."""
        # Setup existing contact
        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16502533333",
            sms_status=Contact.SMSStatus.UNKNOWN,
        )

        def send_inbound(body, sid):
            return self.client.post(
                "/webhooks/twilio/sms/",
                {"From": "+16502533333", "To": "+12137997090", "Body": body, "MessageSid": sid},
                HTTP_X_TWILIO_SIGNATURE="valid_test_signature",
            )

        # 1. STOP
        send_inbound("STOP", "SM_stop_1")
        contact.refresh_from_db()
        self.assertEqual(contact.sms_status, Contact.SMSStatus.OPTED_OUT)
        self.assertTrue(ConsentEvent.objects.filter(contact=contact, event_type=ConsentEvent.EventType.OPT_OUT, source=ConsentEvent.Source.KEYWORD).exists())

        # 2. START
        send_inbound("start", "SM_start_1")
        contact.refresh_from_db()
        self.assertEqual(contact.sms_status, Contact.SMSStatus.OPTED_IN)
        self.assertTrue(ConsentEvent.objects.filter(contact=contact, event_type=ConsentEvent.EventType.OPT_IN, source=ConsentEvent.Source.KEYWORD).exists())

        # 3. YES (should NOT be treated as start, must not alter anything)
        contact.sms_status = Contact.SMSStatus.OPTED_OUT
        contact.save()
        send_inbound("yes", "SM_yes_1")
        contact.refresh_from_db()
        self.assertEqual(contact.sms_status, Contact.SMSStatus.OPTED_OUT)

        # 4. HELP (must not change status)
        send_inbound("HELP please", "SM_help_1")
        contact.refresh_from_db()
        self.assertEqual(contact.sms_status, Contact.SMSStatus.OPTED_OUT)

    def test_opted_out_contact_is_blocked_and_provider_never_called(self):
        """A message to an opted-out contact is blocked and provider is never called."""
        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16502534444",
            sms_status=Contact.SMSStatus.OPTED_OUT,
        )
        conv = Conversation.objects.create(
            organization=self.org_a,
            contact=contact,
            phone_number=self.phone_a,
        )

        with self.assertRaises(ValidationError) as ctx:
            send_message(conv, "Hello there!", self.user_a)

        self.assertIn("opted out", str(ctx.exception).lower())
        self.assertEqual(len(FakeProvider.sent_messages), 0)

    def test_unknown_contact_reply_window(self):
        """
        A message to an unknown contact is blocked,
        then allowed after a recent inbound text,
        then blocked again after the window expires.
        """
        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16502535555",
            sms_status=Contact.SMSStatus.UNKNOWN,
        )
        conv = Conversation.objects.create(
            organization=self.org_a,
            contact=contact,
            phone_number=self.phone_a,
        )

        # 1. Blocked initially (no inbound message ever received)
        allowed, reason = can_send_manual(contact, conv)
        self.assertFalse(allowed)
        with self.assertRaises(ValidationError):
            send_message(conv, "Hello stranger", self.user_a)

        # 2. Customer sent inbound text 2 hours ago -> Allowed!
        conv.last_inbound_at = timezone.now() - timedelta(hours=2)
        conv.save()
        allowed, _ = can_send_manual(contact, conv)
        self.assertTrue(allowed)

        msg = send_message(conv, "Replying to your text!", self.user_a)
        self.assertEqual(msg.status, Message.Status.QUEUED)
        self.assertEqual(len(FakeProvider.sent_messages), 1)

        # 3. Customer sent inbound text 25 hours ago -> Expired & Blocked!
        conv.last_inbound_at = timezone.now() - timedelta(hours=25)
        conv.save()
        allowed, reason = can_send_manual(contact, conv)
        self.assertFalse(allowed)
        self.assertIn("window expired", reason.lower())

    def test_opted_in_contact_can_be_messaged(self):
        """An opted-in contact can be messaged; row is saved before provider call."""
        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16502536666",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv = Conversation.objects.create(
            organization=self.org_a,
            contact=contact,
            phone_number=self.phone_a,
        )

        msg = send_message(conv, "Welcome back!", self.user_a)
        self.assertIsNotNone(msg.id)
        self.assertEqual(msg.status, Message.Status.QUEUED)
        self.assertTrue(msg.provider_message_sid.startswith("SM_fake_"))
        self.assertEqual(len(FakeProvider.sent_messages), 1)
        self.assertGreaterEqual(conv.last_message_at, msg.created_at)

    @override_settings(PUBLIC_BASE_URL="https://example.ngrok-free.dev")
    def test_send_message_passes_status_callback_url(self):
        """send_message passes status_callback = PUBLIC_BASE_URL + '/webhooks/twilio/status/' to provider."""
        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16502536677",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv = Conversation.objects.create(
            organization=self.org_a,
            contact=contact,
            phone_number=self.phone_a,
        )

        send_message(conv, "Testing status callback URL transmission", self.user_a)

        self.assertEqual(len(FakeProvider.sent_messages), 1)
        sent_record = FakeProvider.sent_messages[0]
        self.assertEqual(
            sent_record["status_callback_url"],
            "https://example.ngrok-free.dev/webhooks/twilio/status/",
        )

    def test_provider_failure_and_21610_handling(self):
        """Provider failure -> message marked failed without crash. Error 21610 -> contact opted out with ledger entry."""
        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16502537777",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv = Conversation.objects.create(
            organization=self.org_a,
            contact=contact,
            phone_number=self.phone_a,
        )

        # Case A: Generic provider error
        FakeProvider.fail_with_code = "30008"
        FakeProvider.fail_with_message = "Unknown carrier error"
        msg1 = send_message(conv, "Test generic fail", self.user_a)
        self.assertEqual(msg1.status, Message.Status.FAILED)
        self.assertEqual(msg1.error_code, "30008")

        # Case B: Twilio error 21610 (unsubscribed recipient)
        FakeProvider.fail_with_code = "21610"
        FakeProvider.fail_with_message = "Attempt to send to unsubscribed recipient"
        msg2 = send_message(conv, "Test 21610 fail", self.user_a)
        self.assertEqual(msg2.status, Message.Status.FAILED)
        self.assertEqual(msg2.error_code, "21610")

        # Verify contact was opted out and ledger entry recorded
        contact.refresh_from_db()
        self.assertEqual(contact.sms_status, Contact.SMSStatus.OPTED_OUT)
        ledger_entry = ConsentEvent.objects.filter(
            contact=contact,
            event_type=ConsentEvent.EventType.OPT_OUT,
            source=ConsentEvent.Source.PROVIDER,
        ).first()
        self.assertIsNotNone(ledger_entry)
        self.assertIn("21610", ledger_entry.proof_text)

    def test_status_callback_monotonic(self):
        """Status callback updates status monotonically and never moves it backward."""
        contact = Contact.objects.create(organization=self.org_a, phone="+16502538888")
        conv = Conversation.objects.create(organization=self.org_a, contact=contact, phone_number=self.phone_a)
        msg = Message.objects.create(
            conversation=conv,
            organization=self.org_a,
            direction=Message.Direction.OUTBOUND,
            sender_type=Message.SenderType.HUMAN,
            body="Status progression test",
            status=Message.Status.QUEUED,
            provider_message_sid="SM_status_test_100",
        )

        def post_status(status_str, error=""):
            return self.client.post(
                "/webhooks/twilio/status/",
                {"MessageSid": "SM_status_test_100", "MessageStatus": status_str, "ErrorCode": error},
                HTTP_X_TWILIO_SIGNATURE="valid_test_signature",
            )

        # 1. Update to sending
        post_status("sending")
        msg.refresh_from_db()
        self.assertEqual(msg.status, Message.Status.SENDING)

        # 2. Update to sent
        post_status("sent")
        msg.refresh_from_db()
        self.assertEqual(msg.status, Message.Status.SENT)

        # 3. Update to delivered
        post_status("delivered")
        msg.refresh_from_db()
        self.assertEqual(msg.status, Message.Status.DELIVERED)
        self.assertIsNotNone(msg.delivered_at)

        # 4. Out-of-order 'sent' arrives -> must NOT downgrade 'delivered'
        post_status("sent")
        msg.refresh_from_db()
        self.assertEqual(msg.status, Message.Status.DELIVERED)

    def test_cross_organization_isolation(self):
        """Cross-organization isolation for views and sending; number routes to right organization."""
        contact_a = Contact.objects.create(organization=self.org_a, phone="+16502539991")
        conv_a = Conversation.objects.create(organization=self.org_a, contact=contact_a, phone_number=self.phone_a)

        contact_b = Contact.objects.create(organization=self.org_b, phone="+16502539992")
        conv_b = Conversation.objects.create(organization=self.org_b, contact=contact_b, phone_number=self.phone_b)

        # User A can view Conversation A and renders successfully
        self.client.force_login(self.user_a)
        res_view_a = self.client.get(f"/inbox/{conv_a.id}/")
        self.assertEqual(res_view_a.status_code, 200)

        # User B cannot view Conversation A
        self.client.force_login(self.user_b)
        res_view = self.client.get(f"/inbox/{conv_a.id}/")
        self.assertEqual(res_view.status_code, 404)

        # User B cannot send message in Conversation A
        with self.assertRaises(PermissionDenied):
            send_message(conv_a, "Unauthorized text", self.user_b)

        # Inbound to Phone A routes to Org A, inbound to Phone B routes to Org B
        self.client.post(
            "/webhooks/twilio/sms/",
            {"From": "+16505550001", "To": "+12137997090", "Body": "Hello A", "MessageSid": "SM_iso_a"},
            HTTP_X_TWILIO_SIGNATURE="valid_test_signature",
        )
        self.client.post(
            "/webhooks/twilio/sms/",
            {"From": "+16505550002", "To": "+14155552671", "Body": "Hello B", "MessageSid": "SM_iso_b"},
            HTTP_X_TWILIO_SIGNATURE="valid_test_signature",
        )

        msg_a = Message.objects.get(provider_message_sid="SM_iso_a")
        msg_b = Message.objects.get(provider_message_sid="SM_iso_b")

        self.assertEqual(msg_a.organization, self.org_a)
        self.assertEqual(msg_b.organization, self.org_b)

    def test_start_new_chat_with_existing_contact(self):
        """Starting chat from modal with existing contact returns JSON conversation redirect."""
        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16505553322",
            first_name="Alice",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        self.client.force_login(self.user_a)
        res = self.client.post(
            "/inbox/new/",
            {"contact_id": contact.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        conv = Conversation.objects.get(id=data["conversation_id"])
        self.assertEqual(conv.organization, self.org_a)
        self.assertEqual(conv.contact, contact)
        self.assertIn(f"/inbox/{conv.id}/", data["url"])

    def test_start_new_chat_with_new_phone_number(self):
        """Starting chat directly with a new phone number creates the contact and conversation."""
        self.client.force_login(self.user_a)
        res = self.client.post(
            "/inbox/new/",
            {
                "phone": "+16505559876",
                "first_name": "Jane",
                "last_name": "Doe",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")

        # Contact was created
        contact = Contact.objects.get(organization=self.org_a, phone="+16505559876")
        self.assertEqual(contact.first_name, "Jane")
        self.assertEqual(contact.last_name, "Doe")

        # Conversation was created
        conv = Conversation.objects.get(id=data["conversation_id"])
        self.assertEqual(conv.organization, self.org_a)
        self.assertEqual(conv.contact, contact)

    def test_start_new_chat_cross_org_isolation(self):
        """Starting chat with another organization's contact raises 404."""
        contact_b = Contact.objects.create(
            organization=self.org_b,
            phone="+16505557766",
            first_name="Bob",
        )
        self.client.force_login(self.user_a)
        res = self.client.post(
            "/inbox/new/",
            {"contact_id": contact_b.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(res.status_code, 404)

    def test_start_new_chat_without_active_number(self):
        """Starting chat when business has no active number returns 400 error."""
        self.phone_a.is_active = False
        self.phone_a.save()

        self.client.force_login(self.user_a)
        res = self.client.post(
            "/inbox/new/",
            {"phone": "+16505551122"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("active phone number", res.json()["message"])

    def test_inbox_assigned_filter(self):
        """Testing server-side filtering for 'assigned=mine' and 'assigned=unassigned'."""
        contact_1 = Contact.objects.create(
            organization=self.org_a,
            phone="+16505550001",
            first_name="AssignedToUserA",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        contact_2 = Contact.objects.create(
            organization=self.org_a,
            phone="+16505550002",
            first_name="UnassignedLead",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv_1 = Conversation.objects.create(
            organization=self.org_a,
            contact=contact_1,
            phone_number=self.phone_a,
            assigned_to=self.user_a,
        )
        conv_2 = Conversation.objects.create(
            organization=self.org_a,
            contact=contact_2,
            phone_number=self.phone_a,
            assigned_to=None,
        )

        self.client.force_login(self.user_a)

        # 1. Filter assigned=mine
        res = self.client.get("/inbox/?assigned=mine")
        self.assertEqual(res.status_code, 200)
        conv_ids = [c.id for c in res.context["conversations"]]
        self.assertIn(conv_1.id, conv_ids)
        self.assertNotIn(conv_2.id, conv_ids)

        # 2. Filter assigned=unassigned
        res_unassigned = self.client.get("/inbox/?assigned=unassigned")
        self.assertEqual(res_unassigned.status_code, 200)
        conv_unassigned_ids = [c.id for c in res_unassigned.context["conversations"]]
        self.assertNotIn(conv_1.id, conv_unassigned_ids)
        self.assertIn(conv_2.id, conv_unassigned_ids)

    def test_inbox_search_match_snippet_clarification(self):
        """When searching for text present in message history, matching snippet is attached to conversation."""
        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16505550009",
            first_name="Sam",
            last_name="Taylor",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv = Conversation.objects.create(
            organization=self.org_a,
            contact=contact,
            phone_number=self.phone_a,
        )
        # Message with search keyword
        Message.objects.create(
            organization=self.org_a,
            conversation=conv,
            direction=Message.Direction.INBOUND,
            body="I need a commercial fleet policy discount",
        )
        # Later message without search keyword
        Message.objects.create(
            organization=self.org_a,
            conversation=conv,
            direction=Message.Direction.OUTBOUND,
            body="Sounds good, let me check that for you.",
        )

        self.client.force_login(self.user_a)
        res = self.client.get("/inbox/?q=commercial")
        self.assertEqual(res.status_code, 200)
        convs = res.context["conversations"]
        self.assertEqual(len(convs), 1)
        found_conv = convs[0]
        self.assertEqual(found_conv.id, conv.id)
        self.assertEqual(getattr(found_conv, "search_match_type", None), "message")
        self.assertIn("commercial fleet policy discount", getattr(found_conv, "search_match_snippet", ""))
        self.assertContains(res, "commercial fleet policy discount")

    def test_inbox_search_live_endpoint(self):
        """Testing the live asynchronous search endpoint /inbox/search/."""
        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16505559988",
            first_name="Marcus",
            last_name="Brody",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv = Conversation.objects.create(
            organization=self.org_a,
            contact=contact,
            phone_number=self.phone_a,
        )
        Message.objects.create(
            organization=self.org_a,
            conversation=conv,
            direction=Message.Direction.INBOUND,
            body="Can you quote comprehensive deductible 500",
        )

        self.client.force_login(self.user_a)
        res = self.client.get(
            "/inbox/search/?q=deductible",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Marcus")
        self.assertContains(res, "comprehensive deductible 500")
        self.assertContains(res, "🔍 Match:")

    def test_conversation_detail_ajax_switching(self):
        """AJAX request to /inbox/<id>/ renders only the chat pane template without page reload."""
        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16505557766",
            first_name="Diana",
            last_name="Prince",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv = Conversation.objects.create(
            organization=self.org_a,
            contact=contact,
            phone_number=self.phone_a,
            unread_count=3,
        )
        Message.objects.create(
            organization=self.org_a,
            conversation=conv,
            direction=Message.Direction.INBOUND,
            body="Need insurance quote for my vehicle",
        )

        self.client.force_login(self.user_a)

        # 1. AJAX fetch for conversation detail
        res = self.client.get(
            f"/inbox/{conv.id}/",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, "messaging/inbox_chat_pane.html")
        self.assertContains(res, "Diana Prince")
        self.assertContains(res, "Need insurance quote for my vehicle")

        # Verify unread count was reset to 0
        conv.refresh_from_db()
        self.assertEqual(conv.unread_count, 0)

        # 2. AJAX status update
        status_res = self.client.post(
            f"/inbox/{conv.id}/status/",
            {"status": "closed"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(status_res.status_code, 200)
        self.assertEqual(status_res.json().get("status"), "success")
        conv.refresh_from_db()
        self.assertEqual(conv.status, Conversation.Status.CLOSED)

        # 3. AJAX resume AI
        conv.status = Conversation.Status.NEEDS_HUMAN
        conv.handoff_reason = "Human needed"
        conv.save()

        resume_res = self.client.post(
            f"/inbox/{conv.id}/resume-ai/",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resume_res.status_code, 200)
        self.assertEqual(resume_res.json().get("status"), "success")
        conv.refresh_from_db()
        self.assertEqual(conv.status, Conversation.Status.OPEN)
        self.assertEqual(conv.handoff_reason, "")

    def test_inbox_latest_35_messages_and_before_id_pagination(self):
        """Initial inbox open renders only the latest 35 messages and loads older chunks via ?before_id=."""
        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16505558844",
            first_name="Long",
            last_name="Thread",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv = Conversation.objects.create(
            organization=self.org_a,
            contact=contact,
            phone_number=self.phone_a,
        )
        base_dt = timezone.now() - timedelta(days=3)
        created_msgs = []
        for idx in range(1, 53):
            msg = Message.objects.create(
                organization=self.org_a,
                conversation=conv,
                direction=Message.Direction.INBOUND if idx % 2 else Message.Direction.OUTBOUND,
                sender_type=Message.SenderType.CUSTOMER if idx % 2 else Message.SenderType.AI,
                body=f"History message #{idx:02d}",
                status=Message.Status.RECEIVED if idx % 2 else Message.Status.DELIVERED,
            )
            Message.objects.filter(pk=msg.pk).update(created_at=base_dt + timedelta(hours=idx))
            created_msgs.append(msg)

        self.client.force_login(self.user_a)
        res = self.client.get(f"/inbox/{conv.id}/")
        self.assertEqual(res.status_code, 200)

        chat_messages = res.context["chat_messages"]
        self.assertEqual(len(chat_messages), 35)
        self.assertEqual(chat_messages[0].id, created_msgs[17].id)  # #18
        self.assertEqual(chat_messages[-1].id, created_msgs[-1].id)  # #52
        self.assertTrue(res.context["has_older_messages"])
        self.assertEqual(res.context["oldest_msg_id"], created_msgs[17].id)

        # Left sidebar snippet uses latest message directly without loading full message history
        sidebar_convs = res.context["conversations"]
        target_conv = next(c for c in sidebar_convs if c.id == conv.id)
        self.assertEqual(target_conv.cached_last_msg.id, created_msgs[-1].id)

        html = res.content.decode("utf-8")
        self.assertIn("History message #52", html)
        self.assertNotIn("History message #01", html)
        self.assertIn('id="older-messages-banner"', html)
        self.assertIn("Scroll up to load older messages", html)
        self.assertIn("shouldTriggerOlderMessagesLoad", html)
        self.assertNotIn("scheduleBackgroundOlderLoad", html)
        self.assertIn(f'data-last-msg-id="{created_msgs[-1].id}"', html)
        self.assertIn(f'data-oldest-msg-id="{created_msgs[17].id}"', html)

        # Page 1 of older messages before #18 (limit=10 -> #08..#17)
        older_res_1 = self.client.get(f"/inbox/{conv.id}/messages/?before_id={created_msgs[17].id}&limit=10")
        self.assertEqual(older_res_1.status_code, 200)
        payload_1 = older_res_1.json()
        self.assertTrue(payload_1["has_more"])
        self.assertEqual(payload_1["oldest_id"], created_msgs[7].id)
        bodies_1 = [m["body"] for m in payload_1["messages"]]
        self.assertEqual(bodies_1[0], "History message #08")
        self.assertEqual(bodies_1[-1], "History message #17")
        self.assertTrue(all("date_key" in m and "date_label" in m for m in payload_1["messages"]))

        # Final older page before #08 (limit=10 -> #01..#07, has_more=False)
        older_res_2 = self.client.get(f"/inbox/{conv.id}/messages/?before_id={created_msgs[7].id}&limit=10")
        self.assertEqual(older_res_2.status_code, 200)
        payload_2 = older_res_2.json()
        self.assertFalse(payload_2["has_more"])
        self.assertEqual(payload_2["oldest_id"], created_msgs[0].id)
        bodies_2 = [m["body"] for m in payload_2["messages"]]
        self.assertEqual(bodies_2, [f"History message #{i:02d}" for i in range(1, 8)])

    def test_ajax_status_and_ai_mode_do_not_leak_flash_messages(self):
        """AJAX status and AI mode updates must not queue stale Django session flash banners."""
        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16505551122",
            first_name="Flash",
            last_name="Free",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv = Conversation.objects.create(
            organization=self.org_a,
            contact=contact,
            phone_number=self.phone_a,
        )
        self.client.force_login(self.user_a)

        for status_val in ("needs_human", "closed", "open"):
            r = self.client.post(
                f"/inbox/{conv.id}/status/",
                {"status": status_val},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json().get("new_status"), status_val)

        for mode_val in ("off", "suggest", "auto", "default"):
            r = self.client.post(
                f"/inbox/{conv.id}/ai-mode/",
                {"ai_mode": mode_val},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json().get("ai_mode"), mode_val)

        # Navigating to Contact Detail must not show queued banners from earlier AJAX updates
        page_res = self.client.get(f"/contacts/{contact.id}/")
        self.assertEqual(page_res.status_code, 200)
        self.assertNotContains(page_res, "Conversation marked as")
        self.assertNotContains(page_res, "AI Assistant turned off")
        self.assertNotContains(page_res, "Switched to Suggest Mode")
        self.assertNotContains(page_res, "Switched to Auto Mode")
        self.assertNotContains(page_res, "AI mode set to Business Default")

    def test_mark_conversation_read_and_active_badge_suppressed(self):
        """Active conversation suppresses sidebar badge and POST /inbox/<id>/read/ clears unread_count."""
        c1 = Contact.objects.create(
            organization=self.org_a,
            phone="+16505553311",
            first_name="ActiveOne",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        c2 = Contact.objects.create(
            organization=self.org_a,
            phone="+16505553322",
            first_name="OtherTwo",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv_1 = Conversation.objects.create(
            organization=self.org_a,
            contact=c1,
            phone_number=self.phone_a,
            unread_count=5,
        )
        conv_2 = Conversation.objects.create(
            organization=self.org_a,
            contact=c2,
            phone_number=self.phone_a,
            unread_count=3,
        )
        self.client.force_login(self.user_a)

        # Partial sidebar list with active_id=conv_1.id must not render conv-unread-badge on conv_1
        list_res = self.client.get(
            f"/inbox/search/?active_id={conv_1.id}",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(list_res.status_code, 200)
        list_html = list_res.content.decode("utf-8")
        self.assertIn('id="conv-item-' + str(conv_1.id) + '"', list_html)
        conv1_slice_start = list_html.index(f'id="conv-item-{conv_1.id}"')
        conv1_slice_end = list_html.index("</a>", conv1_slice_start)
        self.assertNotIn("conv-unread-badge", list_html[conv1_slice_start:conv1_slice_end])

        # POST /inbox/<id>/read/ clears conv_1 unread_count and returns remaining total (3)
        read_res = self.client.post(
            f"/inbox/{conv_1.id}/read/",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(read_res.status_code, 200)
        self.assertEqual(read_res.json(), {"status": "success", "unread_count": 0, "nav_unread_count": 3})
        conv_1.refresh_from_db()
        self.assertEqual(conv_1.unread_count, 0)
        conv_2.refresh_from_db()
        self.assertEqual(conv_2.unread_count, 3)

        # Conversation detail AJAX pane exposes fresh data-nav-unread="3"
        pane_res = self.client.get(
            f"/inbox/{conv_1.id}/?ajax=1",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(pane_res.status_code, 200)
        self.assertContains(pane_res, 'data-nav-unread="3"')

    def test_global_quick_search_org_isolation_and_base_ui(self):
        """Quick-jump search returns contacts/conversations/campaigns strictly for the user's org."""
        from campaigns.models import Campaign

        contact_a = Contact.objects.create(
            organization=self.org_a,
            phone="+16505554401",
            first_name="Horizon",
            last_name="Lead",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv_a = Conversation.objects.create(
            organization=self.org_a,
            contact=contact_a,
            phone_number=self.phone_a,
        )
        camp_a = Campaign.objects.create(
            organization=self.org_a,
            name="Horizon Auto Promo",
            phone_number=self.phone_a,
            message_template="Hi there!",
        )

        # Other organization with matching keyword must never leak
        contact_b = Contact.objects.create(
            organization=self.org_b,
            phone="+16505554402",
            first_name="Horizon",
            last_name="Secret",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        Conversation.objects.create(
            organization=self.org_b,
            contact=contact_b,
            phone_number=self.phone_b,
        )
        Campaign.objects.create(
            organization=self.org_b,
            name="Horizon Secret Campaign",
            phone_number=self.phone_b,
            message_template="Secret!",
        )

        self.client.force_login(self.user_a)
        res = self.client.get("/search/quick/?q=Horizon")
        self.assertEqual(res.status_code, 200)
        payload = res.json()
        self.assertEqual([c["id"] for c in payload["contacts"]], [contact_a.id])
        self.assertEqual([c["id"] for c in payload["conversations"]], [conv_a.id])
        self.assertEqual([c["id"] for c in payload["campaigns"]], [camp_a.id])

        # Base layout exposes quick-jump button, quick-jump modal, shared confirm modal, and compiled Tailwind CSS
        dash = self.client.get("/dashboard/")
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, 'id="global-quick-jump-btn"')
        self.assertContains(dash, 'id="quick-jump-modal"')
        self.assertContains(dash, 'id="global-confirm-modal"')
        self.assertContains(dash, "/static/css/app.css")
        self.assertNotContains(dash, "@tailwindcss/browser")

        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertContains(home, "/static/css/website.css")
        self.assertNotContains(home, "@tailwindcss/browser")

    def test_inbox_pagination_modal_contact_search_and_mark_unread(self):
        """Inbox left pane paginates at 30 items, + New Chat searches all contacts, and Mark Unread works."""
        base_dt = timezone.now() - timedelta(days=5)
        convs = []
        for idx in range(1, 37):
            c = Contact.objects.create(
                organization=self.org_a,
                phone=f"+165055560{idx:02d}",
                first_name=f"Paged{idx:02d}",
                sms_status=Contact.SMSStatus.OPTED_IN,
            )
            conv = Conversation.objects.create(
                organization=self.org_a,
                contact=c,
                phone_number=self.phone_a,
                last_message_at=base_dt + timedelta(hours=idx),
            )
            convs.append(conv)

        # Cross-org contact that must never show up in search
        Contact.objects.create(
            organization=self.org_b,
            phone="+16505559999",
            first_name="PagedSecret",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )

        self.client.force_login(self.user_a)

        # 1. Initial inbox renders 30 conversations + Load More button
        inbox_res = self.client.get("/inbox/")
        self.assertEqual(inbox_res.status_code, 200)
        self.assertEqual(len(inbox_res.context["conversations"]), 30)
        self.assertTrue(inbox_res.context["has_more_conversations"])
        self.assertContains(inbox_res, 'id="inbox-load-more-btn"')
        self.assertContains(inbox_res, 'data-next-offset="30"')

        # 2. Second page via /inbox/search/?offset=30&append=1 returns remaining 6 items
        page2_res = self.client.get(
            "/inbox/search/?status=open&offset=30&append=1",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(page2_res.status_code, 200)
        self.assertEqual(len(page2_res.context["conversations"]), 6)
        self.assertFalse(page2_res.context["has_more_conversations"])
        self.assertNotContains(page2_res, 'id="inbox-load-more-btn"')

        # 3. Live + New Chat modal contact search searches beyond initial slice & respects org boundary
        search_res = self.client.get("/inbox/contacts-search/?q=Paged36")
        self.assertEqual(search_res.status_code, 200)
        sdata = search_res.json()
        self.assertEqual(sdata["total_count"], 36)
        self.assertEqual([c["phone"] for c in sdata["contacts"]], ["+16505556036"])

        # 4. Header buttons for mark-unread and sound-toggle are removed per design
        target_conv = convs[-1]
        detail_res = self.client.get(f"/inbox/{target_conv.id}/")
        self.assertEqual(detail_res.status_code, 200)
        self.assertNotContains(detail_res, 'id="btn-mark-unread"')
        self.assertNotContains(detail_res, 'id="btn-sound-toggle"')

        unread_res = self.client.post(
            f"/inbox/{target_conv.id}/unread/",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(unread_res.status_code, 200)
        self.assertEqual(unread_res.json()["unread_count"], 1)
        self.assertEqual(unread_res.json()["nav_unread_count"], 1)
        target_conv.refresh_from_db()
        self.assertEqual(target_conv.unread_count, 1)

    def test_status_transition_returns_live_tab_counts_and_snippet_row_marker(self):
        """AJAX status updates and resume-ai return updated status tab counts and unambiguous snippet rows."""
        contact_1 = Contact.objects.create(
            organization=self.org_a,
            phone="+16505557001",
            first_name="Tab",
            last_name="One",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        contact_2 = Contact.objects.create(
            organization=self.org_a,
            phone="+16505557002",
            first_name="Tab",
            last_name="Two",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        conv_1 = Conversation.objects.create(
            organization=self.org_a,
            contact=contact_1,
            phone_number=self.phone_a,
            status=Conversation.Status.OPEN,
            unread_count=2,
        )
        conv_2 = Conversation.objects.create(
            organization=self.org_a,
            contact=contact_2,
            phone_number=self.phone_a,
            status=Conversation.Status.OPEN,
            unread_count=1,
        )
        self.client.force_login(self.user_a)

        # Open conv_1: clears unread_count and marks snippet rows cleanly with conv-snippet-row
        detail_res = self.client.get(f"/inbox/{conv_1.id}/?status=open")
        self.assertEqual(detail_res.status_code, 200)
        self.assertContains(detail_res, "conv-snippet-row")
        conv_1.refresh_from_db()
        self.assertEqual(conv_1.unread_count, 0)

        # Switch conv_1 Open -> Needs Human returns live counts for tabs
        to_human_res = self.client.post(
            f"/inbox/{conv_1.id}/status/",
            {"status": "needs_human"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(to_human_res.status_code, 200)
        self.assertEqual(
            to_human_res.json(),
            {
                "status": "success",
                "new_status": "needs_human",
                "counts": {"open": 1, "needs_human": 1, "closed": 0},
            },
        )

        # Resume AI moves conv_1 Needs Human -> Open and returns live counts
        resume_res = self.client.post(
            f"/inbox/{conv_1.id}/resume-ai/",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resume_res.status_code, 200)
        self.assertEqual(
            resume_res.json(),
            {
                "status": "success",
                "new_status": "open",
                "counts": {"open": 2, "needs_human": 0, "closed": 0},
            },
        )

        # Switch conv_2 Open -> Closed returns live counts
        to_closed_res = self.client.post(
            f"/inbox/{conv_2.id}/status/",
            {"status": "closed"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(to_closed_res.status_code, 200)
        self.assertEqual(
            to_closed_res.json(),
            {
                "status": "success",
                "new_status": "closed",
                "counts": {"open": 1, "needs_human": 0, "closed": 1},
            },
        )

    def test_dashboard_kpi_links_and_ai_assistant_sandbox(self):
        """Dashboard KPI cards link to actionable queues and AI sandbox simulates replies without persisting DB records."""
        from unittest.mock import patch, MagicMock, MagicMock
        from messaging.ai.base import AIResponse

        self.client.force_login(self.user_a)

        dash_res = self.client.get("/dashboard/")
        self.assertEqual(dash_res.status_code, 200)
        self.assertContains(dash_res, 'href="/inbox/?status=needs_human"')
        self.assertContains(dash_res, 'href="/contacts/?status=opted_out"')
        self.assertContains(dash_res, 'href="/inbox/?assigned=replied"')

        settings_res = self.client.get("/ai-assistant/")
        self.assertEqual(settings_res.status_code, 200)
        self.assertContains(settings_res, 'id="ai-sandbox-input"')
        self.assertContains(settings_res, 'id="ai-sandbox-run-btn"')

        fake_client = MagicMock()
        fake_client.model = "mock-gemma"
        fake_client.generate.return_value = AIResponse(
            text='{"reply": "We are open Mon-Fri 9am-5pm! What service are you interested in?", "intent": "inquiry", "collected": {"service_type": "auto"}, "needs_human": false, "handoff_reason": null}',
            input_tokens=40,
            output_tokens=20,
            latency_ms=85,
        )

        msg_before = Message.objects.count()
        conv_before = Conversation.objects.count()

        with patch("messaging.ai.pipeline.get_ai_client", return_value=fake_client):
            sandbox_res = self.client.post(
                "/ai-assistant/test/",
                {
                    "test_message": "What are your hours?",
                    "business_name": "Acme Agency",
                    "knowledge": "Hours: Mon-Fri 9am-5pm.",
                    "never_say": "forbidden_phrase",
                    "max_reply_chars": "160",
                },
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(sandbox_res.status_code, 200)
        sdata = sandbox_res.json()
        self.assertEqual(sdata["status"], "ok")
        self.assertTrue(sdata["validation_ok"])
        self.assertEqual(sdata["collected"], {"service_type": "auto"})
        self.assertEqual(Message.objects.count(), msg_before)
        self.assertEqual(Conversation.objects.count(), conv_before)

        # Forbidden phrase in reply is flagged as validation_blocked
        fake_client.generate.return_value = AIResponse(
            text='{"reply": "Sure, forbidden_phrase is included!", "intent": "inquiry", "collected": {}, "needs_human": false, "handoff_reason": null}',
            input_tokens=20,
            output_tokens=15,
            latency_ms=42,
        )
        with patch("messaging.ai.pipeline.get_ai_client", return_value=fake_client):
            blocked_res = self.client.post(
                "/ai-assistant/test/",
                {
                    "test_message": "Do you promise this?",
                    "never_say": "forbidden_phrase",
                    "max_reply_chars": "160",
                },
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(blocked_res.status_code, 200)
        bdata = blocked_res.json()
        self.assertEqual(bdata["status"], "validation_blocked")
        self.assertFalse(bdata["validation_ok"])
        self.assertTrue(bdata["needs_human"])

    def test_inbound_sms_notification_toast_only_for_needs_human(self):
        """Inbound SMS broadcasts toast ONLY when conversation is needs_human, otherwise silent."""
        with patch("messaging.ws_broadcast.broadcast_inbox_event") as mock_broadcast:
            # 1. Inbound SMS to an open conversation (AI handled)
            payload = {
                "From": "+16502538888",
                "To": "+12137997090",
                "Body": "Hello there",
                "MessageSid": "SM_open_test_1",
            }
            res = self.client.post(
                "/webhooks/twilio/sms/",
                payload,
                HTTP_X_TWILIO_SIGNATURE="valid_test_signature",
            )
            self.assertEqual(res.status_code, 200)
            self.assertTrue(mock_broadcast.called)
            org_id, event_data = mock_broadcast.call_args[0]
            self.assertEqual(event_data.get("event"), "inbox_update")
            # Routine message must NOT have an alert toast
            self.assertIsNone(event_data.get("toast"))

            # 2. Conversation is in needs_human status
            conv = Conversation.objects.get(contact__phone="+16502538888", organization=self.org_a)
            conv.status = Conversation.Status.NEEDS_HUMAN
            conv.save(update_fields=["status"])

            mock_broadcast.reset_mock()

            # 3. Inbound SMS to a needs_human conversation
            payload_needs_human = {
                "From": "+16502538888",
                "To": "+12137997090",
                "Body": "I need help with my account right now",
                "MessageSid": "SM_needs_human_test_2",
            }
            res2 = self.client.post(
                "/webhooks/twilio/sms/",
                payload_needs_human,
                HTTP_X_TWILIO_SIGNATURE="valid_test_signature",
            )
            self.assertEqual(res2.status_code, 200)
            self.assertTrue(mock_broadcast.called)
            _, event_data2 = mock_broadcast.call_args[0]
            self.assertEqual(event_data2.get("event"), "inbox_update")
            # Must attach needs_human alert toast
            self.assertIsNotNone(event_data2.get("toast"))
            self.assertEqual(event_data2["toast"]["type"], "needs_human")
            self.assertEqual(event_data2["toast"]["title"], "Needs Human Attention")
            self.assertEqual(event_data2["toast"]["conversation_id"], conv.id)
            self.assertIsNone(event_data2["toast"]["assigned_to_id"])

            # 4. Inbound SMS when assigned to user_a
            conv.assigned_to = self.user_a
            conv.save(update_fields=["assigned_to"])
            mock_broadcast.reset_mock()

            payload_assigned = {
                "From": "+16502538888",
                "To": "+12137997090",
                "Body": "Follow up message",
                "MessageSid": "SM_needs_human_test_3",
            }
            res3 = self.client.post(
                "/webhooks/twilio/sms/",
                payload_assigned,
                HTTP_X_TWILIO_SIGNATURE="valid_test_signature",
            )
            self.assertEqual(res3.status_code, 200)
            self.assertTrue(mock_broadcast.called)
            _, event_data3 = mock_broadcast.call_args[0]
            self.assertEqual(event_data3["toast"]["assigned_to_id"], self.user_a.id)

    @patch("messaging.ws_broadcast.broadcast_inbox_event")
    @patch("messaging.views_webhooks.get_sms_provider")
    def test_inbound_sms_toast_strictly_for_needs_human_and_lead_claim_broadcast(self, mock_get_provider, mock_broadcast):
        """Inbound SMS toasts strictly for needs_human; lead assignment broadcasts conversation_assigned."""
        mock_provider = MagicMock()
        mock_provider.validate_webhook.return_value = True
        mock_get_provider.return_value = mock_provider

        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16502537777",
            first_name="Alice",
            last_name="Smith",
        )

        # 1. OPEN conversation must NEVER generate an alert toast in notification center
        conv = Conversation.objects.create(
            organization=self.org_a,
            contact=contact,
            phone_number=self.phone_a,
            status=Conversation.Status.OPEN,
            ai_mode=Conversation.AIMode.OFF,
        )

        payload = {
            "From": contact.phone,
            "To": self.phone_a.e164,
            "Body": "Can we schedule a call for tomorrow morning?",
            "MessageSid": "SM_open_human_1",
        }
        res = self.client.post(
            "/webhooks/twilio/sms/",
            payload,
            HTTP_X_TWILIO_SIGNATURE="valid_test_signature",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(mock_broadcast.called)
        _, event_data = mock_broadcast.call_args[0]
        self.assertEqual(event_data.get("event"), "inbox_update")
        self.assertIsNone(event_data.get("toast"))

        # 2. Claiming or assigning conversation broadcasts conversation_assigned event
        mock_broadcast.reset_mock()
        self.client.force_login(self.user_a)
        res_assign = self.client.post(
            f"/inbox/{conv.id}/assign/",
            {"assigned_to_id": self.user_a.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(res_assign.status_code, 200)
        self.assertTrue(mock_broadcast.called)
        _, assign_event = mock_broadcast.call_args[0]
        self.assertEqual(assign_event.get("event"), "conversation_assigned")
        self.assertEqual(assign_event.get("assigned_to_id"), self.user_a.id)

    def test_notification_context_processor_targeted_routing(self):
        """Context processor only shows needs_human notifications for unassigned or assigned to current user."""
        from messaging.context_processors import unread_inbox_count
        from django.test import RequestFactory
        import json

        rf = RequestFactory()
        user_b = User.objects.create_user(
            email="agent_b@example.com",
            password="password123",
            organization=self.org_a,
            role=User.Role.AGENT,
        )

        contact1 = Contact.objects.create(organization=self.org_a, phone="+16505550001", first_name="Unassigned", last_name="Lead")
        c_unassigned = Conversation.objects.create(
            organization=self.org_a,
            contact=contact1,
            phone_number=self.phone_a,
            status=Conversation.Status.NEEDS_HUMAN,
            handoff_reason="Customer needs pricing help",
        )

        contact2 = Contact.objects.create(organization=self.org_a, phone="+16505550002", first_name="Assigned", last_name="ToUserA")
        c_user_a = Conversation.objects.create(
            organization=self.org_a,
            contact=contact2,
            phone_number=self.phone_a,
            status=Conversation.Status.NEEDS_HUMAN,
            assigned_to=self.user_a,
            handoff_reason="Assigned to user A",
        )

        contact3 = Contact.objects.create(organization=self.org_a, phone="+16505550003", first_name="Assigned", last_name="ToUserB")
        c_user_b = Conversation.objects.create(
            organization=self.org_a,
            contact=contact3,
            phone_number=self.phone_a,
            status=Conversation.Status.NEEDS_HUMAN,
            assigned_to=user_b,
            handoff_reason="Assigned to user B",
        )

        # Request as user_a: should see unassigned and user_a's, but NOT user_b's
        req_a = rf.get("/dashboard/")
        req_a.user = self.user_a
        ctx_a = unread_inbox_count(req_a)
        notifs_a = json.loads(ctx_a["initial_notifications_json"])
        conv_ids_a = [n["conversation_id"] for n in notifs_a]
        self.assertIn(c_unassigned.id, conv_ids_a)
        self.assertIn(c_user_a.id, conv_ids_a)
        self.assertNotIn(c_user_b.id, conv_ids_a)

        # Request as user_b: should see unassigned and user_b's, but NOT user_a's
        req_b = rf.get("/dashboard/")
        req_b.user = user_b
        ctx_b = unread_inbox_count(req_b)
        notifs_b = json.loads(ctx_b["initial_notifications_json"])
        conv_ids_b = [n["conversation_id"] for n in notifs_b]
        self.assertIn(c_unassigned.id, conv_ids_b)
        self.assertIn(c_user_b.id, conv_ids_b)
        self.assertNotIn(c_user_a.id, conv_ids_b)

        # Request viewing c_user_a's conversation directly: should mark that conversation as read=True
        req_viewing = rf.get(f"/inbox/{c_user_a.id}/")
        req_viewing.user = self.user_a
        ctx_viewing = unread_inbox_count(req_viewing)
        notifs_viewing = json.loads(ctx_viewing["initial_notifications_json"])
        user_a_notif = next(n for n in notifs_viewing if n["conversation_id"] == c_user_a.id)
        self.assertTrue(user_a_notif["read"])
        unassigned_notif = next(n for n in notifs_viewing if n["conversation_id"] == c_unassigned.id)
        self.assertFalse(unassigned_notif["read"])

    @patch("messaging.ws_broadcast.broadcast_inbox_event")
    def test_assign_qualified_lead_broadcasts_conversation_assigned(self, mock_broadcast):
        """assign_qualified_lead broadcasts conversation_assigned so other reps remove it from drawer."""
        from messaging.routing import assign_qualified_lead
        self.org_a.lead_routing_strategy = Organization.LeadRoutingStrategy.WORKLOAD_BALANCED
        self.org_a.save(update_fields=["lead_routing_strategy"])

        contact = Contact.objects.create(organization=self.org_a, phone="+16505559988", first_name="Routed", last_name="Lead")
        conv = Conversation.objects.create(
            organization=self.org_a,
            contact=contact,
            phone_number=self.phone_a,
            status=Conversation.Status.NEEDS_HUMAN,
            handoff_reason="Needs human agent",
        )

        chosen = assign_qualified_lead(conv, trigger_reason="Test routing")
        self.assertIsNotNone(chosen)
        self.assertTrue(mock_broadcast.called)
        assigned_events = [
            call_arg[0][1]
            for call_arg in mock_broadcast.call_args_list
            if call_arg[0][1].get("event") == "conversation_assigned"
        ]
        self.assertTrue(len(assigned_events) > 0)
        self.assertEqual(assigned_events[0]["conversation_id"], conv.id)
        self.assertEqual(assigned_events[0]["assigned_to_id"], chosen.id)

    def test_readonly_user_messaging_restrictions(self):
        """Read-only role users can view chats but cannot send messages or alter chat state."""
        viewer = User.objects.create_user(
            email="viewer@org-a.com",
            password="ViewerPassword123!",
            organization=self.org_a,
            role=User.Role.READONLY,
        )
        contact = Contact.objects.create(
            organization=self.org_a,
            phone="+16505551234",
            first_name="John",
            last_name="Doe",
            sms_status="opted_in",
        )
        conv = Conversation.objects.create(
            organization=self.org_a,
            contact=contact,
            phone_number=self.phone_a,
            status=Conversation.Status.OPEN,
        )
        self.client.force_login(viewer)

        # 1. View conversation: can_send must be False with read-only notice
        resp = self.client.get(f"/inbox/{conv.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["can_send"])
        self.assertIn("read-only access", resp.context["block_reason"].lower())

        # 2. Cannot send message via POST
        send_resp = self.client.post(
            f"/inbox/{conv.id}/send/",
            {"body": "Trying to send a message"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(send_resp.status_code, 403)
        self.assertIn("read-only access", send_resp.json()["message"])

        # 3. Cannot start a new chat
        new_chat_resp = self.client.post(
            "/inbox/new/",
            {"phone": "+16505550099", "first_name": "Test"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(new_chat_resp.status_code, 403)

        # 4. Cannot update conversation status
        status_resp = self.client.post(
            f"/inbox/{conv.id}/status/",
            {"status": "closed"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(status_resp.status_code, 403)

