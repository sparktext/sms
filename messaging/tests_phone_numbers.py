import json
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import Organization, User
from contacts.models import Contact
from messaging.models import PhoneNumber, Conversation, Message


class MultiNumberRoutingTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name="Acme Multi-Line Inc", slug="acme-multi-line")
        self.user = User.objects.create_user(
            email="agent@acmemulti.com",
            password="securepassword123",
            first_name="Agent",
            last_name="Smith",
            organization=self.org,
            role=User.Role.AGENT,
            terms_accepted_at=timezone.now(),
        )
        self.client.login(email="agent@acmemulti.com", password="securepassword123")

        # Two phone numbers for Acme
        self.num_main = PhoneNumber.objects.create(
            organization=self.org,
            e164="+16612718246",
            label="Main Support Line",
            is_active=True,
            is_default=True,
        )
        self.num_sales = PhoneNumber.objects.create(
            organization=self.org,
            e164="+18312931374",
            label="Direct Sales Line",
            is_active=True,
            is_default=False,
        )

        # Another org for isolation testing
        self.other_org = Organization.objects.create(name="Other Corp", slug="other-corp")
        self.other_num = PhoneNumber.objects.create(
            organization=self.other_org,
            e164="+15125550199",
            label="Other Org Line",
            is_active=True,
            is_default=True,
        )

        self.contact = Contact.objects.create(
            organization=self.org,
            phone="+16614603931",
            first_name="Customer",
            last_name="Mann",
            sms_status=Contact.SMSStatus.OPTED_IN,
        )

    def test_single_default_per_organization(self):
        """Setting a second number as default must unset the previous default for that org."""
        self.assertTrue(self.num_main.is_default)
        self.assertFalse(self.num_sales.is_default)

        # Make sales the default
        self.num_sales.is_default = True
        self.num_sales.save()

        self.num_main.refresh_from_db()
        self.num_sales.refresh_from_db()

        self.assertTrue(self.num_sales.is_default)
        self.assertFalse(self.num_main.is_default)

        # Other organization default must not be touched
        self.other_num.refresh_from_db()
        self.assertTrue(self.other_num.is_default)

    def test_get_default_for_org(self):
        """PhoneNumber.get_default_for_org() returns the designated default line."""
        default_num = PhoneNumber.get_default_for_org(self.org)
        self.assertEqual(default_num.id, self.num_main.id)

        # Switch default
        self.num_sales.is_default = True
        self.num_sales.save()

        default_num = PhoneNumber.get_default_for_org(self.org)
        self.assertEqual(default_num.id, self.num_sales.id)

    def test_start_new_chat_with_explicit_sender_number(self):
        """Starting a chat with sender_phone_number_id creates conversation on that specific line."""
        url = reverse("messaging:start_new_chat")
        resp = self.client.post(
            url,
            {
                "contact_id": self.contact.id,
                "sender_phone_number_id": self.num_sales.id,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")

        # Verify conversation in DB
        conv = Conversation.objects.get(id=data["conversation_id"])
        self.assertEqual(conv.phone_number.id, self.num_sales.id)
        self.assertEqual(conv.contact.id, self.contact.id)

    def test_start_new_chat_defaults_to_primary_number(self):
        """Starting a chat without specifying sender_phone_number_id uses the primary default number."""
        url = reverse("messaging:start_new_chat")
        resp = self.client.post(
            url,
            {
                "contact_id": self.contact.id,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")

        conv = Conversation.objects.get(id=data["conversation_id"])
        self.assertEqual(conv.phone_number.id, self.num_main.id)

    def test_phone_numbers_settings_page_renders(self):
        """Settings page displays all organization phone numbers."""
        url = reverse("messaging:phone_numbers_settings")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "+16612718246")
        self.assertContains(resp, "+18312931374")
        self.assertContains(resp, "Main Support Line")
        self.assertContains(resp, "Direct Sales Line")
        self.assertNotContains(resp, "+15125550199")  # Other org number

    def test_phone_number_action_make_default(self):
        """Action endpoint switches the primary default phone number."""
        url = reverse("messaging:phone_number_action", kwargs={"phone_id": self.num_sales.id})
        resp = self.client.post(url, {"action": "make_default"}, follow=True)
        self.assertEqual(resp.status_code, 200)

        self.num_sales.refresh_from_db()
        self.num_main.refresh_from_db()

        self.assertTrue(self.num_sales.is_default)
        self.assertFalse(self.num_main.is_default)

    def test_phone_number_action_update_label(self):
        """Action endpoint updates the friendly label."""
        url = reverse("messaging:phone_number_action", kwargs={"phone_id": self.num_sales.id})
        resp = self.client.post(url, {"action": "update_label", "label": "VIP Concierge Line"}, follow=True)
        self.assertEqual(resp.status_code, 200)

        self.num_sales.refresh_from_db()
        self.assertEqual(self.num_sales.label, "VIP Concierge Line")

    def test_phone_number_action_toggle_active(self):
        """Deactivating a secondary line works, but deactivating the default line is blocked."""
        # Deactivate secondary line
        url = reverse("messaging:phone_number_action", kwargs={"phone_id": self.num_sales.id})
        resp = self.client.post(url, {"action": "toggle_active"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.num_sales.refresh_from_db()
        self.assertFalse(self.num_sales.is_active)

        # Attempt to deactivate default line
        url_main = reverse("messaging:phone_number_action", kwargs={"phone_id": self.num_main.id})
        resp_main = self.client.post(url_main, {"action": "toggle_active"}, follow=True)
        self.assertEqual(resp_main.status_code, 200)
        self.num_main.refresh_from_db()
        self.assertTrue(self.num_main.is_active)  # Must remain active

