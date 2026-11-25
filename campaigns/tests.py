import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from campaigns.models import Campaign, CampaignRecipient, CampaignTemplate
from campaigns.services import (
    DEFAULT_CAMPAIGN_TEMPLATES,
    build_campaign_recipients,
    cancel_campaign,
    ensure_default_campaign_templates,
    process_campaign_batch,
    render_campaign_message,
    resolve_campaign_contacts,
)
from contacts.models import Contact, ContactField, ContactList
from core.models import Organization, User
from messaging.models import Conversation, Message, PhoneNumber


@override_settings(MESSAGING_PROVIDER="fake", TWILIO_VALIDATE_WEBHOOKS=True)
class CampaignsTestSuite(TestCase):
    def setUp(self):
        # These tests send whenever they run: keep quiet hours (8 AM-9 PM where each contact is) out of the way
        quiet = patch("campaigns.services.can_text_now", return_value=True)
        quiet.start()
        self.addCleanup(quiet.stop)
        # Organization A
        self.org_a = Organization.objects.create(name="Pacific Haven", slug="pacific-haven")
        self.user_a = User.objects.create_user(
            email="agent_a@example.com",
            password="password123",
            organization=self.org_a,
            role=User.Role.ADMIN,
            first_name="Alice",
        )
        self.phone_a = PhoneNumber.objects.create(
            organization=self.org_a,
            e164="+14153796901",
            is_active=True,
            label="Sales",
        )

        # Organization B (isolation checks)
        self.org_b = Organization.objects.create(name="Beta Insurance", slug="beta-ins")
        self.user_b = User.objects.create_user(
            email="agent_b@example.com",
            password="password123",
            organization=self.org_b,
            role=User.Role.ADMIN,
        )
        self.phone_b = PhoneNumber.objects.create(
            organization=self.org_b,
            e164="+16502530002",
            is_active=True,
        )

        # Approved custom field
        ContactField.objects.create(organization=self.org_a, key="stage", label="Stage")

        # Contacts for Org A
        # Contact 1: Opted in, Stage = Intake
        self.c1 = Contact.objects.create(
            organization=self.org_a,
            phone="+16502530001",
            first_name="John",
            last_name="Doe",
            sms_status=Contact.SMSStatus.OPTED_IN,
            custom_fields={"stage": "Intake"},
        )
        # Contact 2: Opted in, Stage = Qualified
        self.c2 = Contact.objects.create(
            organization=self.org_a,
            phone="+16502530002",
            first_name="Jane",
            last_name="Smith",
            sms_status=Contact.SMSStatus.OPTED_IN,
            custom_fields={"stage": "Qualified"},
        )
        # Contact 3: Opted Out (STOP received), Stage = Intake
        self.c3 = Contact.objects.create(
            organization=self.org_a,
            phone="+16502530003",
            first_name="Bob",
            last_name="Brown",
            sms_status=Contact.SMSStatus.OPTED_OUT,
            custom_fields={"stage": "Intake"},
        )
        # Contact 4: Unknown consent (No prior opt-in), Stage = Intake
        self.c4 = Contact.objects.create(
            organization=self.org_a,
            phone="+16502530004",
            first_name="Charlie",
            last_name="Davis",
            sms_status=Contact.SMSStatus.UNKNOWN,
            custom_fields={"stage": "Intake"},
        )

        # Lists for Org A
        self.list_1 = ContactList.objects.create(organization=self.org_a, name="Fall Leads")
        self.list_1.contacts.add(self.c1, self.c2, self.c3)

        self.list_2 = ContactList.objects.create(organization=self.org_a, name="VIP Auto")
        self.list_2.contacts.add(self.c1, self.c4)  # c1 is in both list 1 and list 2

        self.client = Client()

    def test_render_campaign_message_personalization_and_opt_out(self):
        template = "Hi {{ first_name }}, welcome to {{ business_name }}! Special promo for {{ phone }}."
        rendered = render_campaign_message(template, self.c1, self.org_a)
        
        self.assertIn("Hi John,", rendered)
        self.assertIn("welcome to Pacific Haven!", rendered)
        self.assertIn("Reply STOP to opt out", rendered)

        # If STOP is already in template, do not duplicate
        template_with_stop = "Hello {{ first_name }}. To cancel reply stop anytime."
        rendered2 = render_campaign_message(template_with_stop, self.c1, self.org_a)
        self.assertEqual(rendered2.lower().count("stop"), 1)

    def test_resolve_campaign_contacts_multi_lists_deduplication(self):
        # Selecting both list 1 and list 2 should deduplicate contact 1
        contacts = resolve_campaign_contacts(
            organization=self.org_a,
            list_ids=[self.list_1.id, self.list_2.id],
        )
        self.assertEqual(contacts.count(), 4)
        c_ids = list(contacts.values_list("id", flat=True))
        self.assertEqual(len(c_ids), len(set(c_ids)))  # No duplicates

    def test_resolve_campaign_contacts_filter_by_stage(self):
        # Targeting list 1 (c1, c2, c3), filtered by stage="Intake" (c1, c3)
        contacts = resolve_campaign_contacts(
            organization=self.org_a,
            list_ids=[self.list_1.id],
            filter_criteria={"stage": "Intake"},
        )
        self.assertEqual(contacts.count(), 2)
        c_ids = set(contacts.values_list("id", flat=True))
        self.assertEqual(c_ids, {self.c1.id, self.c3.id})

    def test_build_campaign_recipients_compliance_guards(self):
        campaign = Campaign.objects.create(
            organization=self.org_a,
            name="Fall Auto Outreach",
            phone_number=self.phone_a,
            message_template="Hello {{ first_name }}!",
            filter_criteria={"stage": "Intake"},
        )
        campaign.lists.add(self.list_1)  # Contains c1 (opted_in), c2 (opted_in but filtered out by stage), c3 (opted_out)

        total, eligible = build_campaign_recipients(campaign)
        self.assertEqual(total, 2)  # c1 and c3
        self.assertEqual(eligible, 1)  # Only c1 is opted in

        recips = campaign.recipients.all()
        r1 = recips.filter(contact=self.c1).first()
        r3 = recips.filter(contact=self.c3).first()

        self.assertEqual(r1.status, CampaignRecipient.Status.PENDING)
        self.assertEqual(r3.status, CampaignRecipient.Status.SKIPPED)
        self.assertIn("opted out", r3.skip_reason.lower())

    def test_dispatch_campaign_batch_and_conversation_creation(self):
        campaign = Campaign.objects.create(
            organization=self.org_a,
            created_by=self.user_a,
            name="Instant Broadcast",
            phone_number=self.phone_a,
            message_template="Hi {{ first_name }}, special insurance review!",
            status=Campaign.Status.SCHEDULED,
        )
        campaign.lists.add(self.list_1)
        build_campaign_recipients(campaign)

        processed = process_campaign_batch(campaign, limit=10)
        self.assertGreater(processed, 0)

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.COMPLETED)
        self.assertEqual(campaign.sent_count, 2)  # c1 and c2 are opted in
        self.assertEqual(campaign.skipped_count, 1)  # c3 opted out

        # Verify 2-way conversation was created for recipient and linked to campaign
        conv = Conversation.objects.filter(
            organization=self.org_a,
            contact=self.c1,
            phone_number=self.phone_a,
        ).first()
        self.assertIsNotNone(conv)
        self.assertEqual(conv.last_campaign, campaign)

        # Verify Message row exists with sender_type CAMPAIGN
        msg = Message.objects.filter(conversation=conv).first()
        self.assertIsNotNone(msg)
        self.assertEqual(msg.sender_type, Message.SenderType.CAMPAIGN)
        self.assertEqual(msg.sent_by, self.user_a)

    def test_campaign_create_with_custom_ai_playbook(self):
        """Creating a campaign via POST correctly stores custom AI playbook and qualification questions."""
        self.client.force_login(self.user_a)
        questions_payload = json.dumps([
            {"key": "roof_age", "question": "How old is your roof?", "required": True},
            {"key": "home_type", "question": "Single family or condo?", "required": False},
        ])
        resp = self.client.post("/campaigns/new/", {
            "name": "Home Insurance Playbook Campaign",
            "phone_number_id": self.phone_a.id,
            "message_template": "Hello {{ first_name }}, update your home policy!",
            "timing": "immediate",
            "lists": [self.list_1.id],
            "ai_mode": "custom",
            "ai_campaign_goal": "Qualify homeowners for switching insurance.",
            "ai_qualification_questions_json": questions_payload,
            "ai_special_instructions": "Offer $450 bundling credit.",
        })
        self.assertEqual(resp.status_code, 302)
        camp = Campaign.objects.filter(name="Home Insurance Playbook Campaign").first()
        self.assertIsNotNone(camp)
        self.assertEqual(camp.ai_mode, Campaign.AIMode.CUSTOM)
        self.assertEqual(camp.ai_campaign_goal, "Qualify homeowners for switching insurance.")
        self.assertEqual(len(camp.ai_qualification_questions), 2)
        self.assertEqual(camp.ai_qualification_questions[0]["key"], "roof_age")
        self.assertTrue(camp.ai_qualification_questions[0]["required"])
        self.assertEqual(camp.ai_special_instructions, "Offer $450 bundling credit.")

        # Check detail view renders the playbook
        detail_resp = self.client.get(f"/campaigns/{camp.id}/")
        self.assertEqual(detail_resp.status_code, 200)
        self.assertContains(detail_resp, "Custom Playbook Active")
        self.assertContains(detail_resp, "Qualify homeowners for switching insurance.")
        self.assertContains(detail_resp, "roof_age")

    def test_campaign_clone(self):
        """1-Click cloning duplicates all settings, target lists, and AI playbook into a DRAFT campaign."""
        self.client.force_login(self.user_a)
        original = Campaign.objects.create(
            organization=self.org_a,
            created_by=self.user_a,
            name="Fall Auto Promotion",
            phone_number=self.phone_a,
            message_template="Hi {{ first_name }}, fall discount copy!",
            filter_criteria={"stage": "Intake"},
            status=Campaign.Status.COMPLETED,
            ai_mode=Campaign.AIMode.CUSTOM,
            ai_campaign_goal="Qualify auto leads",
            ai_qualification_questions=[{"key": "vehicle_make", "question": "What make?", "required": True}],
            ai_special_instructions="Mention $200 discount.",
        )
        original.lists.add(self.list_1)

        resp = self.client.get(f"/campaigns/{original.id}/clone/")
        self.assertEqual(resp.status_code, 302)

        clone = Campaign.objects.filter(name="[Copy] Fall Auto Promotion").first()
        self.assertIsNotNone(clone)
        self.assertEqual(clone.status, Campaign.Status.DRAFT)
        self.assertEqual(clone.phone_number, original.phone_number)
        self.assertEqual(clone.message_template, original.message_template)
        self.assertEqual(clone.filter_criteria, {"stage": "Intake"})
        self.assertEqual(clone.ai_mode, Campaign.AIMode.CUSTOM)
        self.assertEqual(clone.ai_campaign_goal, "Qualify auto leads")
        self.assertEqual(clone.ai_special_instructions, "Mention $200 discount.")
        self.assertEqual(list(clone.lists.values_list("id", flat=True)), [self.list_1.id])

    def test_run_campaign_worker_command(self):
        campaign = Campaign.objects.create(
            organization=self.org_a,
            created_by=self.user_a,
            name="Worker Test Broadcast",
            phone_number=self.phone_a,
            message_template="Worker text for {{ first_name }}",
            status=Campaign.Status.SCHEDULED,
            scheduled_at=timezone.now() - timedelta(minutes=1),
        )
        campaign.lists.add(self.list_2)
        build_campaign_recipients(campaign)

        call_command("run_campaign_worker", "--once")

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.COMPLETED)
        self.assertEqual(campaign.sent_count, 1)  # c1 is opted in, c4 unknown consent skipped

    def test_cancel_campaign(self):
        campaign = Campaign.objects.create(
            organization=self.org_a,
            name="To Cancel",
            phone_number=self.phone_a,
            message_template="Will not send",
            status=Campaign.Status.SCHEDULED,
        )
        campaign.lists.add(self.list_1)
        build_campaign_recipients(campaign)

        success = cancel_campaign(campaign)
        self.assertTrue(success)

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.CANCELLED)

        # Pending recipients should now be SKIPPED
        pending_count = campaign.recipients.filter(status=CampaignRecipient.Status.PENDING).count()
        self.assertEqual(pending_count, 0)

    def test_audience_preview_ajax_endpoint(self):
        self.client.force_login(self.user_a)
        res = self.client.get(
            f"/campaigns/api/audience-preview/?lists[]={self.list_1.id}&stage=Intake"
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        # Lead-stage filtering was removed: a stray ?stage= param is ignored
        self.assertEqual(data["total"], 3)  # c1, c2, c3
        self.assertEqual(data["opted_in"], 2)  # c1, c2
        self.assertEqual(data["opted_out"], 1)  # c3

    def test_cross_organization_isolation(self):
        self.client.force_login(self.user_a)
        
        # Campaign for Org B
        camp_b = Campaign.objects.create(
            organization=self.org_b,
            name="Beta Exclusive",
            phone_number=self.phone_b,
            message_template="Secret promo",
            status=Campaign.Status.DRAFT,
        )

        # Org A user attempting to view Org B campaign detail
        res = self.client.get(f"/campaigns/{camp_b.id}/")
        self.assertEqual(res.status_code, 404)

        # Org A user attempting action on Org B campaign
        res_post = self.client.post(f"/campaigns/{camp_b.id}/action/", {"action": "cancel"})
        self.assertEqual(res_post.status_code, 404)

    def test_campaign_batch_dispatches_and_broadcasts_progress(self):
        from unittest.mock import patch
        campaign = Campaign.objects.create(
            organization=self.org_a,
            name="Broadcast Test",
            phone_number=self.phone_a,
            message_template="Hello {{ first_name }}",
            status=Campaign.Status.SCHEDULED,
        )
        campaign.lists.add(self.list_1)
        build_campaign_recipients(campaign)

        with patch("campaigns.ws_broadcast.broadcast_campaign_progress") as mock_broadcast:
            processed = process_campaign_batch(campaign, limit=10)
            self.assertEqual(processed, 2)  # c1 sent, c3 skipped
            self.assertTrue(mock_broadcast.called)
            call_args = mock_broadcast.call_args
            camp_arg = call_args[0][0]
            recip_updates = call_args[0][1]
            self.assertEqual(camp_arg.id, campaign.id)
            self.assertEqual(len(recip_updates), 2)
            self.assertIn("status", recip_updates[0])

    def test_campaign_detail_renders_websocket_elements(self):
        self.client.force_login(self.user_a)
        campaign = Campaign.objects.create(
            organization=self.org_a,
            created_by=self.user_a,
            name="Live Progress Test",
            phone_number=self.phone_a,
            message_template="Hello {{ first_name }}",
            status=Campaign.Status.SENDING,
        )
        resp = self.client.get(f"/campaigns/{campaign.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="campaign-progress-bar"')
        self.assertContains(resp, 'id="campaign-live-indicator"')
        self.assertContains(resp, f"const campaignId = {campaign.id};")
        self.assertContains(resp, '"/ws/campaign/" + campaignId')

    def test_campaign_clone_and_edit_workflow(self):
        self.client.force_login(self.user_a)
        original = Campaign.objects.create(
            organization=self.org_a,
            created_by=self.user_a,
            name="Fall Auto Rebate",
            phone_number=self.phone_a,
            message_template="Hi {{ first_name }}, claim your fall rebate today!",
            status=Campaign.Status.DRAFT,
            ai_mode=Campaign.AIMode.CUSTOM,
            ai_campaign_goal="Qualify auto insurance drivers",
            ai_qualification_questions=[{"key": "vehicle_year", "question": "What year is your vehicle?", "required": True}],
        )
        original.lists.add(self.list_1)

        # 1. Clone redirects to the Edit view for the clone
        clone_resp = self.client.get(f"/campaigns/{original.id}/clone/")
        self.assertEqual(clone_resp.status_code, 302)
        clone_campaign = Campaign.objects.filter(name="[Copy] Fall Auto Rebate").first()
        self.assertIsNotNone(clone_campaign)
        self.assertRedirects(clone_resp, f"/campaigns/{clone_campaign.id}/edit/")

        # 2. GET Edit page for clone returns 200 and pre-fills campaign details
        get_edit_resp = self.client.get(f"/campaigns/{clone_campaign.id}/edit/")
        self.assertEqual(get_edit_resp.status_code, 200)
        self.assertContains(get_edit_resp, "[Copy] Fall Auto Rebate")
        self.assertContains(get_edit_resp, "claim your fall rebate today!")
        self.assertContains(get_edit_resp, "vehicle_year")

        # 3. POST Save as Draft updates campaign data and rebuilds recipients cleanly
        post_draft_resp = self.client.post(
            f"/campaigns/{clone_campaign.id}/edit/",
            data={
                "name": "Updated Fall Auto Rebate",
                "phone_number_id": self.phone_a.id,
                "lists": [self.list_1.id],
                "message_template": "Hello {{ first_name }}, new draft copy!",
                "ai_mode": "custom",
                "ai_campaign_goal": "Goal updated",
                "ai_qualification_questions_json": json.dumps([{"key": "mileage", "question": "What is current mileage?", "required": True}]),
                "ai_special_instructions": "Special note",
                "timing": "immediate",
                "save_draft": "1",
            },
        )
        self.assertRedirects(post_draft_resp, f"/campaigns/{clone_campaign.id}/")
        clone_campaign.refresh_from_db()
        self.assertEqual(clone_campaign.name, "Updated Fall Auto Rebate")
        self.assertEqual(clone_campaign.status, Campaign.Status.DRAFT)
        self.assertEqual(clone_campaign.message_template, "Hello {{ first_name }}, new draft copy!")
        self.assertEqual(clone_campaign.ai_campaign_goal, "Goal updated")
        self.assertEqual(clone_campaign.recipients.count(), 3)  # c1, c2, c3
        self.assertEqual(clone_campaign.recipients.filter(status=CampaignRecipient.Status.PENDING).count(), 2)  # c1, c2
        self.assertEqual(clone_campaign.recipients.filter(status=CampaignRecipient.Status.SKIPPED).count(), 1)  # c3 (opted out)

        # 4. Block editing if campaign is active (SENDING)
        clone_campaign.status = Campaign.Status.SENDING
        clone_campaign.save()
        blocked_resp = self.client.get(f"/campaigns/{clone_campaign.id}/edit/")
        self.assertRedirects(blocked_resp, f"/campaigns/{clone_campaign.id}/")

    def test_campaign_create_save_as_draft(self):
        from django.urls import reverse
        self.client.force_login(self.user_a)
        get_resp = self.client.get(reverse("campaigns:create"))
        self.assertEqual(get_resp.status_code, 200)
        self.assertContains(get_resp, "Save as Draft")
        self.assertContains(get_resp, "name=\"save_draft\"")

        post_resp = self.client.post(
            reverse("campaigns:create"),
            data={
                "name": "Winter Early Bird Draft",
                "phone_number_id": self.phone_a.id,
                "lists": [self.list_1.id],
                "message_template": "Hi {{ first_name }}, draft copy for winter savings.",
                "ai_mode": "default",
                "timing": "immediate",
                "save_draft": "1",
            },
        )
        new_draft = Campaign.objects.filter(name="Winter Early Bird Draft").first()
        self.assertIsNotNone(new_draft)
        self.assertRedirects(post_resp, f"/campaigns/{new_draft.id}/")
        self.assertEqual(new_draft.status, Campaign.Status.DRAFT)
        self.assertIsNone(new_draft.started_at)
        # Recipients computed
        self.assertEqual(new_draft.recipients.count(), 3)
        self.assertEqual(new_draft.recipients.filter(status=CampaignRecipient.Status.PENDING).count(), 2)
        # No messages dispatched
        self.assertEqual(new_draft.recipients.filter(status=CampaignRecipient.Status.SENT).count(), 0)

        # Also support saving draft without message template yet
        post_partial = self.client.post(
            reverse("campaigns:create"),
            data={
                "name": "Work In Progress No Copy",
                "phone_number_id": self.phone_a.id,
                "lists": [self.list_1.id],
                "save_draft": "1",
            },
        )
        partial_draft = Campaign.objects.filter(name="Work In Progress No Copy").first()
        self.assertIsNotNone(partial_draft)
        self.assertRedirects(post_partial, f"/campaigns/{partial_draft.id}/")
        self.assertEqual(partial_draft.status, Campaign.Status.DRAFT)
        self.assertEqual(partial_draft.message_template, "")

    def test_campaign_delete_workflow(self):
        self.client.force_login(self.user_a)

        draft = Campaign.objects.create(
            organization=self.org_a,
            created_by=self.user_a,
            name="Draft To Delete",
            phone_number=self.phone_a,
            message_template="Temp copy",
            status=Campaign.Status.DRAFT,
        )
        completed = Campaign.objects.create(
            organization=self.org_a,
            created_by=self.user_a,
            name="Completed Historical",
            phone_number=self.phone_a,
            message_template="Sent copy",
            status=Campaign.Status.COMPLETED,
            sent_count=10,
        )

        # 1. List renders Delete button on draft
        list_resp = self.client.get("/campaigns/")
        self.assertEqual(list_resp.status_code, 200)
        self.assertContains(list_resp, f"openDeleteCampaignModal({draft.id}, 'Draft To Delete')")
        self.assertNotContains(list_resp, f"openDeleteCampaignModal({completed.id}")

        # 2. Detail page renders Delete button on draft
        detail_resp = self.client.get(f"/campaigns/{draft.id}/")
        self.assertEqual(detail_resp.status_code, 200)
        self.assertContains(detail_resp, 'id="deleteDetailCampaignModal"')

        # 3. Deleting draft succeeds and redirects to list
        del_resp = self.client.post(f"/campaigns/{draft.id}/action/", {"action": "delete"})
        self.assertRedirects(del_resp, "/campaigns/")
        self.assertFalse(Campaign.objects.filter(id=draft.id).exists())

        # 4. Attempting to delete completed campaign is blocked
        block_resp = self.client.post(f"/campaigns/{completed.id}/action/", {"action": "delete"})
        self.assertRedirects(block_resp, f"/campaigns/{completed.id}/")
        self.assertTrue(Campaign.objects.filter(id=completed.id).exists())


class CampaignTemplateModelTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Apex Insurance", slug="apex-ins")
        self.user = User.objects.create_user(
            email="agent@apex.com",
            password="password123",
            organization=self.org,
        )

    def test_default_templates_seeder(self):
        self.assertEqual(CampaignTemplate.objects.filter(organization=self.org).count(), 0)
        ensure_default_campaign_templates(self.org)
        count = CampaignTemplate.objects.filter(organization=self.org).count()
        self.assertEqual(count, len(DEFAULT_CAMPAIGN_TEMPLATES))
        self.assertTrue(count >= 5)

        # Idempotent call doesn't duplicate
        ensure_default_campaign_templates(self.org)
        self.assertEqual(CampaignTemplate.objects.filter(organization=self.org).count(), count)

    def test_template_properties(self):
        t1 = CampaignTemplate.objects.create(
            organization=self.org,
            name="Short Promo",
            category=CampaignTemplate.Category.MARKETING,
            body="Hello {{ first_name }}, welcome to {{ business_name }}! Reply STOP to opt out.",
        )
        self.assertEqual(t1.character_count, len(t1.body))
        self.assertEqual(t1.segment_count, 1)
        self.assertEqual(t1.merge_tags, ["business_name", "first_name"])

        # Long message segment count
        long_body = "A" * 165
        t2 = CampaignTemplate.objects.create(
            organization=self.org,
            name="Long Message",
            category=CampaignTemplate.Category.FOLLOW_UP,
            body=long_body,
        )
        self.assertEqual(t2.segment_count, 2)


class CampaignTemplateViewTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Prime Care", slug="prime-care")
        self.user = User.objects.create_user(
            email="owner@prime.com",
            password="securepassword",
            organization=self.org,
            role=User.Role.OWNER,
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_templates_index_loads_and_seeds(self):
        # Test both /campaigns/templates/ and top-level /templates/
        for url in ["/campaigns/templates/", "/templates/"]:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200)
            self.assertTemplateUsed(resp, "campaigns/templates_index.html")
            self.assertTrue(CampaignTemplate.objects.filter(organization=self.org).exists())
            self.assertContains(resp, "Auto Insurance")

    def test_templates_index_filtering(self):
        ensure_default_campaign_templates(self.org)
        # Category filter
        resp_cat = self.client.get("/campaigns/templates/?category=quick_reply")
        self.assertEqual(resp_cat.status_code, 200)
        self.assertContains(resp_cat, "Office Hours")

        # Search filter
        resp_search = self.client.get("/campaigns/templates/?q=Consultation")
        self.assertEqual(resp_search.status_code, 200)
        self.assertContains(resp_search, "Consultation Appointment Reminder")

    def test_template_crud_workflow(self):
        # 1. Create
        resp_c = self.client.post(
            "/campaigns/templates/new/",
            data={
                "name": "Custom VIP Offer",
                "category": "marketing",
                "body": "Hi {{ first_name }}, here is 20% off for being a VIP client! Reply STOP to opt out.",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_c.status_code, 201)
        t_id = resp_c.json()["template"]["id"]
        t = CampaignTemplate.objects.get(id=t_id, organization=self.org)
        self.assertEqual(t.name, "Custom VIP Offer")

        # 2. Get Edit details
        resp_g = self.client.get(f"/campaigns/templates/{t_id}/edit/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp_g.status_code, 200)
        self.assertEqual(resp_g.json()["template"]["name"], "Custom VIP Offer")

        # 3. Post Edit update
        resp_u = self.client.post(
            f"/campaigns/templates/{t_id}/edit/",
            data={
                "name": "Custom VIP Offer Updated",
                "category": "marketing",
                "body": "Hi {{ first_name }}, updated VIP discount copy.",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_u.status_code, 200)
        t.refresh_from_db()
        self.assertEqual(t.name, "Custom VIP Offer Updated")

        # 4. Delete
        resp_d = self.client.post(f"/campaigns/templates/{t_id}/delete/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp_d.status_code, 200)
        self.assertFalse(CampaignTemplate.objects.filter(id=t_id).exists())

    def test_templates_api_list(self):
        ensure_default_campaign_templates(self.org)
        resp = self.client.get("/campaigns/templates/api/list/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertTrue(len(data["templates"]) >= 5)
        self.assertIn("merge_tags", data["templates"][0])

    def test_templates_reset_defaults(self):
        ensure_default_campaign_templates(self.org)
        # Delete quick reply templates
        CampaignTemplate.objects.filter(organization=self.org, category="quick_reply").delete()
        self.assertEqual(CampaignTemplate.objects.filter(organization=self.org, category="quick_reply").count(), 0)

        # Call reset-defaults endpoint for category quick_reply
        resp = self.client.post("/campaigns/templates/reset-defaults/", {"category": "quick_reply"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")

        # Verify quick_reply templates were restored
        restored_count = CampaignTemplate.objects.filter(organization=self.org, category="quick_reply").count()
        self.assertTrue(restored_count >= 2)

    def test_custom_category_creation_and_filtering(self):
        # 1. Create a template with a brand-new custom category
        resp = self.client.post(
            "/campaigns/templates/new/",
            data={
                "name": "Black Friday VIP Early Access",
                "category": "__new__",
                "custom_category": "Seasonal Promos",
                "body": "Hi {{ first_name }}, early Black Friday access is now open! Reply YES to view deals.",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        tmpl_id = data["template"]["id"]
        self.assertEqual(data["template"]["category"], "seasonal_promos")
        self.assertEqual(data["template"]["category_display"], "Seasonal Promos")

        t = CampaignTemplate.objects.get(id=tmpl_id)
        self.assertEqual(t.category, "seasonal_promos")
        self.assertEqual(t.category_display, "Seasonal Promos")

        # 2. View templates index - should include dynamic filter pill for Seasonal Promos
        resp_index = self.client.get("/campaigns/templates/")
        self.assertEqual(resp_index.status_code, 200)
        self.assertContains(resp_index, "Seasonal Promos (1)")
        self.assertContains(resp_index, "category=seasonal_promos")

        # Filter by the custom category
        resp_filtered = self.client.get("/campaigns/templates/?category=seasonal_promos")
        self.assertEqual(resp_filtered.status_code, 200)
        self.assertContains(resp_filtered, "Black Friday VIP Early Access")

        # 3. Edit template to another custom category
        resp_edit = self.client.post(
            f"/campaigns/templates/{tmpl_id}/edit/",
            data={
                "name": "Black Friday VIP Early Access",
                "category": "__new__",
                "custom_category": "VIP Retention",
                "body": "Hi {{ first_name }}, VIP exclusive offer.",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_edit.status_code, 200)
        t.refresh_from_db()
        self.assertEqual(t.category, "vip_retention")
        self.assertEqual(t.category_display, "VIP Retention")

        # 4. API list includes category_label
        resp_api = self.client.get("/campaigns/templates/api/list/?category=vip_retention")
        self.assertEqual(resp_api.status_code, 200)
        api_data = resp_api.json()
        self.assertEqual(len(api_data["templates"]), 1)
        self.assertEqual(api_data["templates"][0]["category_label"], "VIP Retention")

    def test_custom_category_delete_and_rename(self):
        from campaigns.models import TemplateCategory
        ensure_default_campaign_templates(self.org)

        # Create templates in custom category 'typo_cat'
        t1 = CampaignTemplate.objects.create(
            organization=self.org,
            name="Template 1",
            category="typo_cat",
            body="Hello {{ first_name }}",
        )
        t2 = CampaignTemplate.objects.create(
            organization=self.org,
            name="Template 2",
            category="typo_cat",
            body="Hello {{ first_name }} again",
        )

        # 1. Rename typo category (typo_cat -> "Fixed Category")
        resp_rename = self.client.post(
            "/campaigns/templates/categories/rename/",
            data={"old_category": "typo_cat", "new_name": "Fixed Category"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_rename.status_code, 200)
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual(t1.category, "fixed_category")
        self.assertEqual(t2.category, "fixed_category")
        self.assertEqual(t1.category_display, "Fixed Category")

        # 2. Rename a default starter category (marketing -> "Promotions")
        resp_std_rename = self.client.post(
            "/campaigns/templates/categories/rename/",
            data={"old_category": "marketing", "new_name": "Promotions"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_std_rename.status_code, 200)
        self.assertTrue(TemplateCategory.objects.filter(organization=self.org, slug="promotions", name="Promotions").exists())
        self.assertFalse(TemplateCategory.objects.filter(organization=self.org, slug="marketing").exists())

        # 3. Delete custom category -> reassigns templates to 'promotions'
        resp_del = self.client.post(
            "/campaigns/templates/categories/delete/",
            data={"category": "fixed_category", "reassign_to": "promotions"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_del.status_code, 200)
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual(t1.category, "promotions")
        self.assertEqual(t2.category, "promotions")

        # 4. Delete a default starter category ('transactional') and verify it stays deleted on index reload
        resp_std_del = self.client.post(
            "/campaigns/templates/categories/delete/",
            data={"category": "transactional", "delete_templates": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_std_del.status_code, 200)
        self.assertFalse(TemplateCategory.objects.filter(organization=self.org, slug="transactional").exists())
        self.assertFalse(CampaignTemplate.objects.filter(organization=self.org, category="transactional").exists())

        # Reloading templates index should NOT resurrect 'transactional'
        resp_reload = self.client.get("/campaigns/templates/")
        self.assertEqual(resp_reload.status_code, 200)
        self.assertFalse(TemplateCategory.objects.filter(organization=self.org, slug="transactional").exists())

        # 5. Quick create category endpoint
        resp_add = self.client.post(
            "/campaigns/templates/categories/new/",
            data={"name": "VIP Retention"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_add.status_code, 201)
        self.assertTrue(TemplateCategory.objects.filter(organization=self.org, slug="vip_retention", name="VIP Retention").exists())

    def test_campaign_recipient_csv_export_and_cleanup_links(self):
        """Campaign detail shows export & consent cleanup links and /export/ respects status filters safely."""
        phone = PhoneNumber.objects.create(
            organization=self.org,
            e164="+14155550111",
            is_active=True,
            label="Main",
        )
        c_failed = Contact.objects.create(
            organization=self.org,
            phone="+16505558101",
            first_name="=CMD_INJ",
            last_name="FailUser",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        c_skipped = Contact.objects.create(
            organization=self.org,
            phone="+16505558102",
            first_name="Skip",
            last_name="User",
            sms_status=Contact.SMSStatus.UNKNOWN,
        )
        c_delivered = Contact.objects.create(
            organization=self.org,
            phone="+16505558103",
            first_name="Done",
            last_name="User",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )
        camp = Campaign.objects.create(
            organization=self.org,
            name="Export Test Broadcast",
            phone_number=phone,
            message_template="Hi {{ first_name }}!",
            status=Campaign.Status.COMPLETED,
            total_recipients=3,
            sent_count=2,
            delivered_count=1,
            failed_count=1,
            skipped_count=1,
            created_by=self.user,
        )
        CampaignRecipient.objects.create(
            campaign=camp,
            contact=c_failed,
            status=CampaignRecipient.Status.FAILED,
            error_message="Carrier unreachable",
            personalized_body="Hi =CMD_INJ!",
        )
        CampaignRecipient.objects.create(
            campaign=camp,
            contact=c_skipped,
            status=CampaignRecipient.Status.SKIPPED,
            skip_reason="Missing opt-in consent",
            personalized_body="Hi Skip!",
        )
        CampaignRecipient.objects.create(
            campaign=camp,
            contact=c_delivered,
            status=CampaignRecipient.Status.DELIVERED,
            personalized_body="Hi Done!",
        )

        detail_res = self.client.get(f"/campaigns/{camp.id}/")
        self.assertEqual(detail_res.status_code, 200)
        self.assertContains(detail_res, f"/campaigns/{camp.id}/export/")
        self.assertContains(detail_res, "/contacts/?status=unknown")

        export_res = self.client.get(f"/campaigns/{camp.id}/export/?status=failed")
        self.assertEqual(export_res.status_code, 200)
        self.assertEqual(export_res["Content-Type"], "text/csv")
        csv_body = export_res.content.decode("utf-8")
        self.assertIn("+16505558101", csv_body)
        self.assertIn("'=CMD_INJ", csv_body)
        self.assertIn("Carrier unreachable", csv_body)
        self.assertNotIn("+16505558103", csv_body)






class LeadStageFilterRemovedTests(TestCase):
    """The campaign composer no longer offers or applies a lead-stage filter."""

    def setUp(self):
        from core.models import User
        from messaging.models import PhoneNumber
        self.org = Organization.objects.create(name="No Stage Org", slug="no-stage-org")
        self.user = User.objects.create_user(email="ns@example.com", password="password123", organization=self.org)
        self.phone = PhoneNumber.objects.create(organization=self.org, e164="+16502530100", is_active=True)
        self.lst = ContactList.objects.create(organization=self.org, name="All")
        for i, (status, stage) in enumerate([
            (Contact.SMSStatus.OPTED_IN, "Intake"),
            (Contact.SMSStatus.OPTED_IN, "Qualified"),
        ]):
            c = Contact.objects.create(organization=self.org, phone=f"+1650253020{i}", source="Direct",
                                       sms_status=status, custom_fields={"stage": stage})
            self.lst.contacts.add(c)
        self.client.force_login(self.user, backend="django.contrib.auth.backends.ModelBackend")

    def test_composer_has_no_stage_filter(self):
        html = self.client.get("/campaigns/new/").content.decode()
        self.assertNotIn("Filter by Lead Stage", html)
        self.assertNotIn('name="filter_stage"', html)
        self.assertNotIn('id="filter-stage"', html)

    def test_posted_filter_stage_is_ignored(self):
        self.client.post("/campaigns/new/", {
            "name": "No stage", "phone_number_id": self.phone.id,
            "message_template": "Hi {{ first_name }}", "timing": "immediate",
            "lists": [self.lst.id], "filter_stage": "Intake", "save_draft": "1",
        })
        camp = Campaign.objects.get(organization=self.org, name="No stage")
        self.assertEqual(camp.filter_criteria, {})


@override_settings(MESSAGING_PROVIDER="fake")
class DraftEditAndLaunchTests(TestCase):
    """Live bug: a draft saved earlier couldn't be launched (a hidden date picker held a past time), the audience
    pill never left "Calculating...", and editing a scheduled campaign shifted it by the UTC offset."""

    def setUp(self):
        self.org = Organization.objects.create(name="Pacific Haven", slug="ph-drafts")
        self.user = User.objects.create_user(email="owner@drafts.test", password="pw-123456789", organization=self.org,
                                             role=User.Role.OWNER)
        self.phone = PhoneNumber.objects.create(organization=self.org, e164="+14155550100", is_active=True)
        self.lst = ContactList.objects.create(organization=self.org, name="Pilot")
        c = Contact.objects.create(organization=self.org, phone="+14155550111", first_name="Maria",
                                   sms_status=Contact.SMSStatus.OPTED_IN)
        self.lst.contacts.add(c)
        self.client.force_login(self.user)

    def form(self, **extra):
        return {"name": "Pilot campaign", "phone_number_id": self.phone.id, "lists": [self.lst.id],
                "message_template": "Hi {{ first_name }}, quick question! STOP to opt out.", "ai_mode": "default",
                "timing": "immediate", **extra}

    def test_a_draft_has_no_schedule_time_and_can_be_launched_later(self):
        self.client.post("/campaigns/new/", self.form(save_draft="1"))
        campaign = Campaign.objects.get(name="Pilot campaign")
        self.assertEqual(campaign.status, Campaign.Status.DRAFT)
        self.assertIsNone(campaign.scheduled_at)                 # was stamped "now", which later blocked launching

        page = self.client.get(f"/campaigns/{campaign.id}/edit/").content.decode()
        picker = page[page.index('id="scheduled-at"'):page.index('id="scheduled-at-iso"')]
        self.assertIn("disabled", picker)                        # a hidden picker can't block the Launch button
        self.assertIn('data-utc=""', picker)
        self.assertNotIn('min="', picker)

        with patch("campaigns.services.can_text_now", return_value=True):          # not in quiet hours
            self.client.post(f"/campaigns/{campaign.id}/edit/", self.form())   # Save & Launch, later on
        campaign.refresh_from_db()
        self.assertIn(campaign.status, (Campaign.Status.SENDING, Campaign.Status.COMPLETED))
        self.assertEqual(Message.objects.filter(conversation__contact__phone="+14155550111").count(), 1)

    def test_editing_a_scheduled_campaign_keeps_its_time(self):
        when = (timezone.now() + timedelta(days=2)).replace(second=0, microsecond=0)
        self.client.post("/campaigns/new/", self.form(timing="scheduled", scheduled_at_iso=when.isoformat()))
        campaign = Campaign.objects.get(name="Pilot campaign")
        self.assertEqual((campaign.status, campaign.scheduled_at), (Campaign.Status.SCHEDULED, when))
        page = self.client.get(f"/campaigns/{campaign.id}/edit/").content.decode()
        self.assertIn(f'data-utc="{when.isoformat()}"', page)  # the page converts it to the viewer's local time

    def test_scheduling_in_the_past_is_refused(self):
        past = (timezone.now() - timedelta(hours=1)).isoformat()
        self.client.post("/campaigns/new/", self.form(timing="scheduled", scheduled_at_iso=past))
        self.assertFalse(Campaign.objects.filter(name="Pilot campaign").exists())

    def test_audience_count_for_the_pill(self):
        data = self.client.get(f"/campaigns/api/audience-preview/?lists[]={self.lst.id}").json()
        self.assertEqual((data["status"], data["will_send"], data["will_skip"]), ("success", 1, 0))


@override_settings(MESSAGING_PROVIDER="fake")
class CampaignPageAuditTests(TestCase):
    """Everything found in the full review of the campaign pages."""

    def setUp(self):
        self.org = Organization.objects.create(name="Pacific Haven", slug="ph-audit", timezone="America/Los_Angeles")
        self.user = User.objects.create_user(email="owner@audit.test", password="pw-123456789", organization=self.org,
                                             role=User.Role.OWNER)
        self.phone = PhoneNumber.objects.create(organization=self.org, e164="+14155550200", is_active=True)
        self.lst = ContactList.objects.create(organization=self.org, name="Pilot")
        self.maria = Contact.objects.create(organization=self.org, phone="+14155550201", first_name="Maria",
                                            sms_status=Contact.SMSStatus.OPTED_IN, custom_fields={"state": "CA"})
        self.lst.contacts.add(self.maria)
        self.client.force_login(self.user)

    def form(self, **extra):
        return {"name": "Audit", "phone_number_id": self.phone.id, "lists": [self.lst.id],
                "message_template": "Hi {{ first_name }}! STOP to opt out.", "ai_mode": "default",
                "timing": "immediate", **extra}

    def draft(self, **extra):
        self.client.post("/campaigns/new/", self.form(save_draft="1", **extra))
        return Campaign.objects.get(name=extra.get("name", "Audit"))

    def sent(self):
        return Message.objects.filter(conversation__contact__organization=self.org,
                                      sender_type=Message.SenderType.CAMPAIGN).count()

    # ---- the error in the screenshot: editing a draft, ticking a list, Save Draft
    def test_editing_a_draft_without_text_saves_it(self):
        campaign = self.draft(message_template="", lists=[])
        r = self.client.post(f"/campaigns/{campaign.id}/edit/", self.form(save_draft="1", message_template=""))
        self.assertRedirects(r, f"/campaigns/{campaign.id}/", fetch_redirect_response=False)
        campaign.refresh_from_db()
        self.assertEqual(list(campaign.lists.values_list("id", flat=True)), [self.lst.id])

    def test_a_failed_save_keeps_what_was_typed(self):
        campaign = self.draft(message_template="", lists=[])
        r = self.client.post(f"/campaigns/{campaign.id}/edit/", self.form(name="Renamed", message_template=""))
        self.assertEqual(r.status_code, 200)                      # shown again, not redirected and wiped
        page = r.content.decode()
        self.assertIn("Please enter message text", page)
        self.assertIn('value="Renamed"', page)
        self.assertRegex(page, rf'value="{self.lst.id}"\s+checked')
        campaign.refresh_from_db()
        self.assertEqual(campaign.name, "Audit")                  # nothing saved
        r = self.client.post("/campaigns/new/", self.form(name="", message_template="Typed text"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Typed text", r.content.decode())

    # ---- Send Now on the detail page
    def test_send_now_refuses_a_draft_without_text(self):
        campaign = self.draft(message_template="")
        self.client.post(f"/campaigns/{campaign.id}/action/", {"action": "send_now"})
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.DRAFT)
        self.assertEqual(self.sent(), 0)

    def test_send_now_uses_todays_audience(self):
        campaign = self.draft()
        late = Contact.objects.create(organization=self.org, phone="+14155550202", first_name="Late",
                                      sms_status=Contact.SMSStatus.OPTED_IN, custom_fields={"state": "CA"})
        self.lst.contacts.add(late)                               # joined the list after the draft was saved
        with patch("campaigns.services.can_text_now", return_value=True):
            self.client.post(f"/campaigns/{campaign.id}/action/", {"action": "send_now"})
        self.assertEqual(self.sent(), 2)

    def test_detail_send_now_asks_first(self):
        campaign = self.draft()
        page = self.client.get(f"/campaigns/{campaign.id}/").content.decode()
        self.assertIn("Send Campaign Now", page)
        self.assertIn("now to 1 recipient", page)

    # ---- editing a campaign that already went out
    def test_a_cancelled_campaign_cannot_be_edited_and_resent(self):
        campaign = self.draft()
        campaign.status = Campaign.Status.CANCELLED
        campaign.save()
        self.client.post(f"/campaigns/{campaign.id}/edit/", self.form())
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.CANCELLED)
        self.assertEqual(self.sent(), 0)

    # ---- quiet hours and stuck sends
    def test_quiet_hours_hold_texts_until_morning_their_time(self):
        hawaii = Contact.objects.create(organization=self.org, phone="+18085550203", first_name="Kai",
                                        sms_status=Contact.SMSStatus.OPTED_IN, custom_fields={"state": "HI"})
        self.lst.contacts.add(hawaii)
        night_in_ca = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)      # 10 PM in CA, 8 PM in HI
        with patch("campaigns.services.timezone.now", return_value=night_in_ca):
            self.client.post("/campaigns/new/", self.form())
        campaign = Campaign.objects.get(name="Audit")
        self.assertEqual(campaign.status, Campaign.Status.SENDING)                 # not completed: Maria waits
        statuses = dict(campaign.recipients.values_list("contact__first_name", "status"))
        self.assertEqual(statuses, {"Maria": "pending", "Kai": "sent"})
        morning_in_ca = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)    # 9 AM in CA
        with patch("campaigns.services.timezone.now", return_value=morning_in_ca):
            process_campaign_batch(campaign)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.COMPLETED)

    def test_a_stuck_claim_is_released_but_old_ones_are_left_alone(self):
        campaign = self.draft()
        campaign.status = Campaign.Status.SENDING
        campaign.save()
        recip = campaign.recipients.get()
        recip.status, recip.queued_at = CampaignRecipient.Status.QUEUED, timezone.now() - timedelta(minutes=30)
        recip.save()
        with patch("campaigns.services.can_text_now", return_value=True):
            process_campaign_batch(campaign)
        recip.refresh_from_db()
        self.assertEqual(recip.status, CampaignRecipient.Status.SENT)
        # a stuck row from before claim times existed (no queued_at) is not resent automatically
        other = self.draft(name="Old")
        other.status = Campaign.Status.SENDING
        other.save()
        old = other.recipients.get()
        old.status = CampaignRecipient.Status.QUEUED
        old.save()
        process_campaign_batch(other)
        old.refresh_from_db()
        self.assertEqual(old.status, CampaignRecipient.Status.QUEUED)

    # ---- times shown in the workspace zone
    def test_scheduled_time_is_shown_in_workspace_time(self):
        when = datetime(2030, 1, 15, 17, 0, tzinfo=UTC)              # 9 AM Pacific
        r = self.client.post("/campaigns/new/", self.form(timing="scheduled", scheduled_at_iso=when.isoformat()),
                             follow=True)
        page = r.content.decode()
        self.assertIn("09:00 AM PST", page)
        self.assertIn("Jan 15, 2030 09:00 AM", page)

    # ---- every page and the template tools answer without a server error
    def test_every_campaign_page_and_template_action_works(self):
        campaign = self.draft()
        for url in ("/campaigns/", "/campaigns/new/", f"/campaigns/{campaign.id}/", f"/campaigns/{campaign.id}/edit/",
                    f"/campaigns/{campaign.id}/export/", "/campaigns/templates/", "/campaigns/templates/api/list/",
                    f"/campaigns/api/audience-preview/?lists[]={self.lst.id}"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
        r = self.client.post("/campaigns/templates/new/", {"name": "T1", "category": "follow_up",
                                                           "body": "Hi {{ first_name }}"}, **ajax)
        self.assertEqual(r.status_code, 201)
        tid = r.json()["template"]["id"]
        for url, data in ((f"/campaigns/templates/{tid}/edit/", {"name": "T2", "category": "follow_up", "body": "Hello"}),
                          ("/campaigns/templates/categories/new/", {"name": "Promos"}),
                          ("/campaigns/templates/categories/rename/", {"slug": "promos", "name": "Promotions"}),
                          ("/campaigns/templates/categories/delete/", {"slug": "promotions"}),
                          (f"/campaigns/templates/{tid}/delete/", {}),
                          ("/campaigns/templates/reset-defaults/", {})):
            self.assertLess(self.client.post(url, data, **ajax).status_code, 500, url)
        self.assertEqual(self.client.get(f"/campaigns/{campaign.id}/clone/").status_code, 302)
        self.client.post(f"/campaigns/{campaign.id}/action/", {"action": "delete"})
        self.assertFalse(Campaign.objects.filter(id=campaign.id).exists())


    def test_live_badge_only_while_sending(self):
        campaign = self.draft()
        page = self.client.get(f"/campaigns/{campaign.id}/").content.decode()
        self.assertIn('id="campaign-live-wrap" class="hidden"', page)
        Campaign.objects.filter(id=campaign.id).update(status=Campaign.Status.SENDING)
        from campaigns.services import record_worker_heartbeat
        record_worker_heartbeat()                                               # the worker is running
        with patch("compliance.quiet_hours.allowed_for", return_value=True):    # and it's daytime there
            page = self.client.get(f"/campaigns/{campaign.id}/").content.decode()
        self.assertIn('id="campaign-live-wrap" class=""', page)



@override_settings(MESSAGING_PROVIDER="fake")
class CampaignFlowTests(TestCase):
    """End-to-end campaign flows: scheduling, the worker, cancelling, big audiences, consent at send time."""

    def setUp(self):
        quiet = patch("campaigns.services.can_text_now", return_value=True)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.org = Organization.objects.create(name="Pacific Haven", slug="ph-flow", timezone="America/Los_Angeles")
        self.user = User.objects.create_user(email="owner@flow.test", password="pw-123456789", organization=self.org,
                                             role=User.Role.OWNER)
        self.phone = PhoneNumber.objects.create(organization=self.org, e164="+14155550300", is_active=True)
        self.a = ContactList.objects.create(organization=self.org, name="A")
        self.b = ContactList.objects.create(organization=self.org, name="B")
        self.client.force_login(self.user)

    def contact(self, n, lst=None, status=Contact.SMSStatus.OPTED_IN, **fields):
        c = Contact.objects.create(organization=self.org, phone=f"+1415555{n:04d}", first_name=f"Lead{n}",
                                   sms_status=status, custom_fields=fields)
        if lst:
            lst.contacts.add(c)
        return c

    def launch(self, lists, **extra):
        data = {"name": extra.pop("name", "Flow"), "phone_number_id": self.phone.id, "lists": [lst.id for lst in lists],
                "message_template": extra.pop("message_template", "Hi {{ first_name }}! STOP to opt out."),
                "ai_mode": "default", "timing": "immediate", **extra}
        self.client.post("/campaigns/new/", data)
        return Campaign.objects.get(name=data["name"])

    def texts(self, campaign):
        return list(Message.objects.filter(conversation__organization=self.org, sender_type=Message.SenderType.CAMPAIGN)
                    .values_list("body", flat=True))

    def test_scheduled_campaign_goes_out_only_when_due(self):
        self.contact(1, self.a)
        when = timezone.now() + timedelta(hours=2)
        campaign = self.launch([self.a], timing="scheduled", scheduled_at_iso=when.isoformat())
        call_command("run_campaign_worker", once=True)
        campaign.refresh_from_db()
        self.assertEqual((campaign.status, self.texts(campaign)), (Campaign.Status.SCHEDULED, []))
        Campaign.objects.filter(id=campaign.id).update(scheduled_at=timezone.now() - timedelta(minutes=1))
        call_command("run_campaign_worker", once=True)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.COMPLETED)
        self.assertEqual(len(self.texts(campaign)), 1)

    def test_big_audience_finishes_through_the_worker(self):
        for n in range(120):
            self.contact(n, self.a)
        campaign = self.launch([self.a])
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.SENDING)      # the page sends the first 50
        self.assertEqual(len(self.texts(campaign)), 50)
        for _ in range(5):                                               # each worker pass sends 25
            call_command("run_campaign_worker", once=True)
        campaign.refresh_from_db()
        self.assertEqual((campaign.status, campaign.sent_count), (Campaign.Status.COMPLETED, 120))
        self.assertEqual(len(self.texts(campaign)), 120)                 # nobody texted twice

    def test_cancel_stops_the_rest(self):
        for n in range(80):
            self.contact(n, self.a)
        campaign = self.launch([self.a])
        self.client.post(f"/campaigns/{campaign.id}/action/", {"action": "cancel"})
        call_command("run_campaign_worker", once=True)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.CANCELLED)
        self.assertEqual(len(self.texts(campaign)), 50)

    def test_overlapping_lists_text_each_person_once(self):
        both = self.contact(1, self.a)
        self.b.contacts.add(both)
        self.contact(2, self.b)
        campaign = self.launch([self.a, self.b])
        self.assertEqual(sorted(self.texts(campaign)), ["Hi Lead1! STOP to opt out.", "Hi Lead2! STOP to opt out."])

    def test_consent_is_checked_again_at_send_time(self):
        maria = self.contact(1, self.a)
        self.contact(2, self.a, status=Contact.SMSStatus.OPTED_OUT)
        self.contact(3, self.a, status=Contact.SMSStatus.UNKNOWN)
        self.client.post("/campaigns/new/", {"name": "Later", "phone_number_id": self.phone.id, "lists": [self.a.id],
                                             "message_template": "Hi!", "ai_mode": "default", "timing": "immediate",
                                             "save_draft": "1"})
        campaign = Campaign.objects.get(name="Later")
        maria.sms_status = Contact.SMSStatus.OPTED_OUT                    # replied STOP to something else meanwhile
        maria.save()
        self.client.post(f"/campaigns/{campaign.id}/action/", {"action": "send_now"})
        campaign.refresh_from_db()
        self.assertEqual(self.texts(campaign), [])
        self.assertEqual((campaign.status, campaign.skipped_count), (Campaign.Status.COMPLETED, 3))

    def test_merge_tags_fill_in_or_fall_back(self):
        self.contact(1, self.a, current_insurer="GEICO")
        nameless = Contact.objects.create(organization=self.org, phone="+14155559999", sms_status=Contact.SMSStatus.OPTED_IN)
        self.a.contacts.add(nameless)
        campaign = self.launch([self.a], message_template="Hi {{ first_name }}, still with {{ current_insurer }}? STOP to opt out.")
        self.assertEqual(sorted(self.texts(campaign)), ["Hi Lead1, still with GEICO? STOP to opt out.",
                                                        "Hi there, still with ? STOP to opt out."])

    def test_live_progress_tells_the_page_when_it_is_done(self):
        self.contact(1, self.a)
        sent = []
        with patch("campaigns.ws_broadcast.broadcast_campaign_progress",
                   side_effect=lambda c, recips: sent.append((c.status, recips))):
            self.launch([self.a])
        self.assertTrue(sent)
        self.assertEqual(sent[-1][0], Campaign.Status.COMPLETED)

    def test_delete_from_the_list_page(self):
        self.contact(1, self.a)
        when = timezone.now() + timedelta(days=1)
        campaign = self.launch([self.a], timing="scheduled", scheduled_at_iso=when.isoformat())
        r = self.client.post(f"/campaigns/{campaign.id}/action/", {"action": "delete"})
        self.assertRedirects(r, "/campaigns/", fetch_redirect_response=False)
        self.assertFalse(Campaign.objects.filter(id=campaign.id).exists())
        done = self.launch([self.a], name="Done")
        self.client.post(f"/campaigns/{done.id}/action/", {"action": "delete"})
        self.assertTrue(Campaign.objects.filter(id=done.id).exists())       # sent campaigns are kept

    def test_other_workspaces_campaigns_are_off_limits(self):
        other = Organization.objects.create(name="Other", slug="other-flow")
        phone = PhoneNumber.objects.create(organization=other, e164="+14155550399", is_active=True)
        theirs = Campaign.objects.create(organization=other, name="Theirs", phone_number=phone, message_template="x",
                                         status=Campaign.Status.DRAFT)
        for url in (f"/campaigns/{theirs.id}/", f"/campaigns/{theirs.id}/edit/", f"/campaigns/{theirs.id}/export/"):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(self.client.post(f"/campaigns/{theirs.id}/action/", {"action": "send_now"}).status_code, 404)


@override_settings(MESSAGING_PROVIDER="fake")
class CampaignWaitingReasonTests(TestCase):
    """A sending campaign says why it isn't sending: allowed texting hours, or the worker is stopped."""

    def setUp(self):
        self.org = Organization.objects.create(name="Wait Co", slug="wait-co", timezone="America/Los_Angeles",
                                               address_state="CA")
        self.user = User.objects.create_user(email="owner@wait.test", password="pw-123456789", organization=self.org,
                                             role=User.Role.OWNER)
        self.phone = PhoneNumber.objects.create(organization=self.org, e164="+14155550700", is_active=True)
        self.lst = ContactList.objects.create(organization=self.org, name="L")
        self.lst.contacts.add(Contact.objects.create(organization=self.org, phone="+15305550701", first_name="Cal",
                                                     sms_status=Contact.SMSStatus.OPTED_IN,
                                                     custom_fields={"state": "CA"}))
        self.client.force_login(self.user)
        self.campaign = Campaign.objects.create(organization=self.org, name="Night", phone_number=self.phone,
                                                message_template="Hi! STOP to opt out.", status=Campaign.Status.SENDING)
        self.campaign.lists.add(self.lst)
        build_campaign_recipients(self.campaign)

    def page(self, when):
        with patch("campaigns.services.timezone.now", return_value=when):
            return self.client.get(f"/campaigns/{self.campaign.id}/").content.decode()

    def test_night_in_california_says_waiting_and_when(self):
        from campaigns.services import record_worker_heartbeat
        night = datetime(2026, 11, 25, 8, 25, tzinfo=UTC)            # 12:25 AM in California
        with patch("campaigns.services.timezone.now", return_value=night):
            record_worker_heartbeat()
        html = self.page(night)
        pill = html[html.index('id="campaign-status-pill-container"'):html.index('id="campaign-live-wrap"')]
        self.assertIn("Waiting · sends 9:00 AM PST", pill)
        self.assertNotIn("Sending...", pill)
        self.assertIn("On hold until 9:00 AM PST today", html)
        self.assertIn("(in about 9 hours)", html)                                  # 8 h 35 min
        self.assertIn("It's <strong>12:25 AM</strong> in <strong>California</strong> right now", html)
        self.assertIn("Under California law, marketing texts can only be sent <strong>between 9 AM and 9 PM</strong>", html)
        self.assertIn("You don't need to do anything", html)
        self.assertIn('id="campaign-live-wrap" class="hidden"', html)               # nothing is going out

    def test_a_stopped_worker_is_called_out(self):
        midday = datetime(2026, 11, 25, 20, 0, tzinfo=UTC)            # noon in California
        html = self.page(midday)                                                   # no heartbeat at all
        self.assertIn("Sending has stopped", html)
        self.assertIn("isn't running", html)
        self.assertIn("> Paused", html)

    def test_unknown_leads_use_the_agency_state(self):
        from compliance.quiet_hours import lead_location
        tollfree = Contact.objects.create(organization=self.org, phone="+18885550702")
        where = lead_location(tollfree)
        self.assertEqual((where.state, where.source), ("CA", "agency state"))
        self.org.address_state = ""
        self.org.save()
        tollfree.refresh_from_db()
        self.assertEqual(lead_location(tollfree, self.org).source, "unknown")       # no agency state: safe window

    def test_company_profile_requires_the_state(self):
        self.client.post("/settings/profile/", {"name": "Wait Co", "address_state": ""})
        self.org.refresh_from_db()
        self.assertEqual(self.org.address_state, "CA")                             # refused, unchanged
        self.client.post("/settings/profile/", {"name": "Wait Co", "address_state": "tx"})
        self.org.refresh_from_db()
        self.assertEqual(self.org.address_state, "TX")
        html = self.client.get("/settings/profile/").content.decode()
        self.assertIn('<option value="TX" selected>Texas</option>', html)


    def test_campaign_list_says_waiting_not_sending(self):
        from campaigns.services import record_worker_heartbeat
        night = datetime(2026, 11, 25, 8, 25, tzinfo=UTC)
        with patch("campaigns.services.timezone.now", return_value=night):
            record_worker_heartbeat()
            html = self.client.get("/campaigns/").content.decode()
        row = html[html.index(">Night<") if ">Night<" in html else html.index("Night"):]
        self.assertIn("Waiting · 9:00 AM PST", row[:4000])
        self.assertNotIn("Sending...", row[:2500])

    def test_plain_hours_wording(self):
        from compliance.quiet_hours import LeadLocation, plain_hours
        self.assertEqual(plain_hours(LeadLocation("PA", ("America/New_York",), "lead details")),
                         ("between 9 AM and 7 PM, and never on Sundays or holidays", "Pennsylvania law"))
        self.assertEqual(plain_hours(LeadLocation("RI", ("America/New_York",), "lead details"))[0],
                         "between 9 AM and 6 PM (Saturdays 10 AM to 5 PM), and never on Sundays or holidays")
        self.assertEqual(plain_hours(LeadLocation("GA", ("America/New_York",), "lead details")),
                         ("between 8 AM and 9 PM", "federal law"))


@override_settings(MESSAGING_PROVIDER="fake")
class EditWaitingCampaignTests(TestCase):
    """A launched campaign that hasn't sent anything (e.g. waiting for texting hours) can be edited: it's moved
    back to draft first. Once a text went out, it can't."""

    def setUp(self):
        self.org = Organization.objects.create(name="Edit Co", slug="edit-co", address_state="CA")
        self.user = User.objects.create_user(email="owner@edit.test", password="pw-123456789", organization=self.org,
                                             role=User.Role.OWNER)
        self.phone = PhoneNumber.objects.create(organization=self.org, e164="+14155550800", is_active=True)
        self.lst = ContactList.objects.create(organization=self.org, name="L")
        for n in range(3):
            self.lst.contacts.add(Contact.objects.create(organization=self.org, phone=f"+1530555081{n}",
                                                         sms_status=Contact.SMSStatus.OPTED_IN,
                                                         custom_fields={"state": "CA"}))
        self.client.force_login(self.user)
        self.campaign = Campaign.objects.create(organization=self.org, name="Waiting", phone_number=self.phone,
                                                message_template="Hi! STOP to opt out.", status=Campaign.Status.SENDING,
                                                started_at=timezone.now())
        self.campaign.lists.add(self.lst)
        build_campaign_recipients(self.campaign)

    def test_waiting_campaign_shows_edit_and_moves_to_draft(self):
        html = self.client.get(f"/campaigns/{self.campaign.id}/").content.decode()
        self.assertIn('id="pauseEditCampaignForm"', html)
        self.assertIn(f'id="pauseEditForm{self.campaign.id}"', self.client.get("/campaigns/").content.decode())
        r = self.client.post(f"/campaigns/{self.campaign.id}/action/", {"action": "edit"})
        self.assertRedirects(r, f"/campaigns/{self.campaign.id}/edit/", fetch_redirect_response=False)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.DRAFT)
        self.assertEqual(self.client.get(f"/campaigns/{self.campaign.id}/edit/").status_code, 200)

    def test_a_paused_campaign_is_never_sent_by_the_worker(self):
        self.client.post(f"/campaigns/{self.campaign.id}/action/", {"action": "edit"})
        stale = Campaign.objects.get(id=self.campaign.id)
        stale.status = Campaign.Status.SENDING          # a worker still holding the old "sending" copy
        with patch("campaigns.services.can_text_now", return_value=True):
            sent = process_campaign_batch(stale)
        self.assertEqual(sent, 0)
        self.assertFalse(Message.objects.filter(conversation__organization=self.org).exists())

    def test_relaunch_after_editing(self):
        self.client.post(f"/campaigns/{self.campaign.id}/action/", {"action": "edit"})
        with patch("campaigns.services.can_text_now", return_value=True):
            self.client.post(f"/campaigns/{self.campaign.id}/edit/", {
                "name": "Waiting v2", "phone_number_id": self.phone.id, "lists": [self.lst.id],
                "message_template": "Hello again! STOP to opt out.", "ai_mode": "default", "timing": "immediate"})
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.name, "Waiting v2")
        bodies = set(Message.objects.filter(conversation__organization=self.org).values_list("body", flat=True))
        self.assertEqual(bodies, {"Hello again! STOP to opt out."})                 # only the edited text

    def test_cannot_edit_once_something_was_sent(self):
        with patch("campaigns.services.can_text_now", return_value=True):
            process_campaign_batch(self.campaign, limit=1)                         # one text goes out
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.sent_count, 1)
        self.assertNotIn('id="pauseEditCampaignForm"', self.client.get(f"/campaigns/{self.campaign.id}/").content.decode())
        self.client.post(f"/campaigns/{self.campaign.id}/action/", {"action": "edit"})
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.SENDING)


@override_settings(MESSAGING_PROVIDER="fake")
class CampaignTestSendTests(TestCase):
    """"Send test": one text to your own (opted-in) phone, right now, any hour."""

    def setUp(self):
        self.org = Organization.objects.create(name="Test Co", slug="test-send-co", address_state="CA")
        self.user = User.objects.create_user(email="owner@testsend.test", password="pw-123456789",
                                             organization=self.org, role=User.Role.OWNER)
        self.phone = PhoneNumber.objects.create(organization=self.org, e164="+14155550900", is_active=True)
        self.me = Contact.objects.create(organization=self.org, phone="+15306841631", first_name="spark",
                                         sms_status=Contact.SMSStatus.OPTED_IN)
        self.client.force_login(self.user)
        self.ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

    def send(self, **data):
        base = {"message_template": "Hi {{ first_name }}! STOP to opt out.", "phone_number_id": self.phone.id,
                "to_phone": "(530) 684-1631", "confirm_own": "1"}
        return self.client.post("/campaigns/test-send/", {**base, **data}, **self.ajax)

    def test_sends_now_even_at_night_and_shows_in_the_inbox(self):
        night = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)             # 1 AM in California
        with patch("django.utils.timezone.now", return_value=night):
            r = self.send()
        self.assertEqual(r.json()["status"], "success", r.json())
        texts = list(Message.objects.filter(conversation__contact=self.me, sender_type=Message.SenderType.CAMPAIGN)
                     .values_list("body", flat=True))
        self.assertEqual(texts, ["Hi spark! STOP to opt out."])                    # merge tags filled
        self.assertTrue(Message.objects.filter(conversation__contact=self.me, body__startswith="🧪 Test text sent by").exists())
        self.assertIn("/inbox/", r.json()["chat_url"])

    def test_refuses_numbers_without_consent_or_confirmation(self):
        self.assertIn("confirm", self.send(confirm_own="").json()["message"])
        self.assertIn("isn't in your contacts", self.send(to_phone="+14155559999").json()["message"])
        self.me.sms_status = Contact.SMSStatus.OPTED_OUT
        self.me.save()
        self.assertIn("opted out", self.send().json()["message"])
        self.assertFalse(Message.objects.filter(sender_type=Message.SenderType.CAMPAIGN).exists())

    def test_limit_per_hour(self):
        for _ in range(10):
            self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.send().status_code, 429)

    def test_button_is_on_the_composer(self):
        html = self.client.get("/campaigns/new/").content.decode()
        self.assertIn("openTestSend()", html)
        self.assertIn('id="testSendModal"', html)
