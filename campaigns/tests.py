import json
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from django.core.management import call_command

from core.models import Organization, User
from contacts.models import Contact, ContactList, ContactField
from compliance.models import ConsentEvent
from messaging.models import PhoneNumber, Conversation, Message
from campaigns.models import Campaign, CampaignRecipient, CampaignTemplate, MessageTemplate
from campaigns.services import (
    render_campaign_message,
    resolve_campaign_contacts,
    build_campaign_recipients,
    process_campaign_batch,
    cancel_campaign,
    ensure_default_campaign_templates,
    ensure_default_templates,
    DEFAULT_CAMPAIGN_TEMPLATES,
)


@override_settings(MESSAGING_PROVIDER="fake", TWILIO_VALIDATE_WEBHOOKS=True)
class CampaignsTestSuite(TestCase):
    def setUp(self):
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
        resp_api = self.client.get(f"/campaigns/templates/api/list/?category=vip_retention")
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
