from django.test import TestCase, Client
from core.models import Organization, User
from templates_app.models import MessageTemplate
from templates_app.defaults import ensure_default_templates, DEFAULT_TEMPLATES


class MessageTemplateModelTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Apex Insurance", slug="apex-ins")
        self.user = User.objects.create_user(
            email="agent@apex.com",
            password="password123",
            organization=self.org,
        )

    def test_default_templates_seeder(self):
        self.assertEqual(MessageTemplate.objects.filter(organization=self.org).count(), 0)
        ensure_default_templates(self.org)
        count = MessageTemplate.objects.filter(organization=self.org).count()
        self.assertEqual(count, len(DEFAULT_TEMPLATES))
        self.assertTrue(count >= 5)

        # Idempotent call doesn't duplicate
        ensure_default_templates(self.org)
        self.assertEqual(MessageTemplate.objects.filter(organization=self.org).count(), count)

    def test_template_properties(self):
        t1 = MessageTemplate.objects.create(
            organization=self.org,
            name="Short Promo",
            category=MessageTemplate.Category.MARKETING,
            body="Hello {{ first_name }}, welcome to {{ business_name }}! Reply STOP to opt out.",
        )
        self.assertEqual(t1.character_count, len(t1.body))
        self.assertEqual(t1.segment_count, 1)
        self.assertEqual(t1.merge_tags, ["business_name", "first_name"])

        # Long message segment count
        long_body = "A" * 165
        t2 = MessageTemplate.objects.create(
            organization=self.org,
            name="Long Message",
            category=MessageTemplate.Category.FOLLOW_UP,
            body=long_body,
        )
        self.assertEqual(t2.segment_count, 2)


class MessageTemplateViewTests(TestCase):
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
        resp = self.client.get("/templates/")
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "templates_app/index.html")
        # Confirms default templates were populated
        self.assertTrue(MessageTemplate.objects.filter(organization=self.org).exists())
        self.assertContains(resp, "Auto Insurance")

    def test_templates_index_filtering(self):
        ensure_default_templates(self.org)
        # Category filter
        resp_cat = self.client.get("/templates/?category=quick_reply")
        self.assertEqual(resp_cat.status_code, 200)
        self.assertContains(resp_cat, "Office Hours")

        # Search filter
        resp_search = self.client.get("/templates/?q=Consultation")
        self.assertEqual(resp_search.status_code, 200)
        self.assertContains(resp_search, "Consultation Appointment Reminder")

    def test_template_crud_workflow(self):
        # 1. Create
        resp_c = self.client.post(
            "/templates/new/",
            data={
                "name": "Custom VIP Offer",
                "category": "marketing",
                "body": "Hi {{ first_name }}, here is 20% off for being a VIP client! Reply STOP to opt out.",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_c.status_code, 201)
        t_id = resp_c.json()["template"]["id"]
        t = MessageTemplate.objects.get(id=t_id, organization=self.org)
        self.assertEqual(t.name, "Custom VIP Offer")

        # 2. Get Edit details
        resp_g = self.client.get(f"/templates/{t_id}/edit/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp_g.status_code, 200)
        self.assertEqual(resp_g.json()["template"]["name"], "Custom VIP Offer")

        # 3. Post Edit update
        resp_u = self.client.post(
            f"/templates/{t_id}/edit/",
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
        resp_d = self.client.post(f"/templates/{t_id}/delete/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp_d.status_code, 200)
        self.assertFalse(MessageTemplate.objects.filter(id=t_id).exists())

    def test_templates_api_list(self):
        ensure_default_templates(self.org)
        resp = self.client.get("/templates/api/list/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertTrue(len(data["templates"]) >= 5)
        self.assertIn("merge_tags", data["templates"][0])
