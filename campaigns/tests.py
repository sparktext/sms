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

        # Verify 2-way conversation was created for recipient
        conv = Conversation.objects.filter(
            organization=self.org_a,
            contact=self.c1,
            phone_number=self.phone_a,
        ).first()
        self.assertIsNotNone(conv)

        # Verify Message row exists with sender_type CAMPAIGN
        msg = Message.objects.filter(conversation=conv).first()
        self.assertIsNotNone(msg)
        self.assertEqual(msg.sender_type, Message.SenderType.CAMPAIGN)
        self.assertEqual(msg.sent_by, self.user_a)

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
        self.client.login(username=self.user_a.email, password="password123")
        res = self.client.get(
            f"/campaigns/api/audience-preview/?lists[]={self.list_1.id}&stage=Intake"
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["total"], 2)  # c1 and c3
        self.assertEqual(data["opted_in"], 1)  # c1
        self.assertEqual(data["opted_out"], 1)  # c3

    def test_cross_organization_isolation(self):
        self.client.login(username=self.user_a.email, password="password123")
        
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



