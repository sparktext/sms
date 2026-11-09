from datetime import timedelta
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
