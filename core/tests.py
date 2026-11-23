from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, Client
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from core.models import Organization, User, OrganizationInvite


class SettingsAndTeamTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Apex Auto Shield",
            slug="apex-auto-shield",
            timezone="America/Los_Angeles",
            ein="12-3456789",
        )
        self.owner = User.objects.create_user(
            email="owner@apex.com",
            password="password123",
            first_name="Alice",
            last_name="Owner",
            organization=self.org,
            role=User.Role.OWNER,
        )
        self.admin = User.objects.create_user(
            email="admin@apex.com",
            password="password123",
            first_name="Bob",
            last_name="Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.agent = User.objects.create_user(
            email="agent@apex.com",
            password="password123",
            first_name="Charlie",
            last_name="Agent",
            organization=self.org,
            role=User.Role.AGENT,
        )
        self.client = Client()

    def test_settings_redirect(self):
        self.client.force_login(self.owner)
        resp = self.client.get("/settings/")
        self.assertRedirects(resp, "/settings/profile/")

    def test_company_profile_update_by_owner(self):
        self.client.force_login(self.owner)
        resp = self.client.post("/settings/profile/", {
            "name": "Apex Auto Shield Inc",
            "ein": "98-7654321",
            "website": "https://apexshield.com",
            "support_phone": "+16505550199",
            "notification_email": "alerts@apexshield.com",
            "address_street": "100 Pine St, Suite 400",
            "address_city": "San Francisco",
            "address_state": "CA",
            "address_postal_code": "94111",
            "address_country": "USA",
        })
        self.assertRedirects(resp, "/settings/profile/")
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, "Apex Auto Shield Inc")
        self.assertEqual(self.org.ein, "98-7654321")
        self.assertEqual(self.org.notification_email, "alerts@apexshield.com")
        self.assertEqual(self.org.address_city, "San Francisco")

    def test_company_profile_update_forbidden_for_agent(self):
        self.client.force_login(self.agent)
        resp = self.client.post("/settings/profile/", {
            "name": "Hacked Name",
        })
        self.assertRedirects(resp, "/settings/profile/")
        self.org.refresh_from_db()
        self.assertNotEqual(self.org.name, "Hacked Name")

    def test_notification_sounds_view_by_owner(self):
        self.client.force_login(self.owner)
        resp = self.client.get("/settings/sounds/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Agency Alert Chime")
        self.assertContains(resp, "Cash Register")
        self.assertContains(resp, "Sales Flourish")
        self.assertContains(resp, "Custom Audio Sound")

    @patch("messaging.ws_broadcast.broadcast_inbox_event")
    def test_notification_sound_update_by_owner(self, mock_broadcast):
        self.client.force_login(self.owner)
        resp = self.client.post("/settings/sounds/", {
            "notification_sound": Organization.NotificationSound.CASH_REGISTER,
        })
        self.assertRedirects(resp, "/settings/sounds/")
        self.org.refresh_from_db()
        self.assertEqual(self.org.notification_sound, Organization.NotificationSound.CASH_REGISTER)
        mock_broadcast.assert_called()
        call_args = mock_broadcast.call_args[0]
        self.assertEqual(call_args[0], self.org.id)
        self.assertEqual(call_args[1]["event"], "notification_sound_update")
        self.assertEqual(call_args[1]["notification_sound"], Organization.NotificationSound.CASH_REGISTER)

    def test_notification_sound_update_forbidden_for_agent(self):
        self.client.force_login(self.agent)
        resp = self.client.post("/settings/sounds/", {
            "notification_sound": Organization.NotificationSound.BRIGHT_POP,
        })
        self.assertRedirects(resp, "/settings/sounds/")
        self.org.refresh_from_db()
        self.assertNotEqual(self.org.notification_sound, Organization.NotificationSound.BRIGHT_POP)

    def test_custom_notification_sound_upload_and_removal(self):
        self.client.force_login(self.owner)
        dummy_audio = SimpleUploadedFile("chime.mp3", b"ID3\x03\x00\x00\x00\x00\x00\x00dummy audio content", content_type="audio/mpeg")
        resp = self.client.post("/settings/sounds/", {
            "notification_sound": "custom",
            "custom_notification_sound": dummy_audio,
        })
        self.assertRedirects(resp, "/settings/sounds/")
        self.org.refresh_from_db()
        self.assertEqual(self.org.notification_sound, Organization.NotificationSound.CUSTOM)
        self.assertTrue(bool(self.org.custom_notification_sound))
        self.assertTrue(self.org.custom_notification_sound.name.endswith(".mp3"))

        # Test remove custom sound
        resp2 = self.client.post("/settings/sounds/", {
            "action": "remove_custom",
        })
        self.assertRedirects(resp2, "/settings/sounds/")
        self.org.refresh_from_db()
        self.assertEqual(self.org.notification_sound, Organization.NotificationSound.SALES_FLOURISH)
        self.assertFalse(bool(self.org.custom_notification_sound))

    def test_team_members_view(self):
        self.client.force_login(self.owner)
        resp = self.client.get("/settings/team/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Alice Owner")
        self.assertContains(resp, "Bob Admin")
        self.assertContains(resp, "Charlie Agent")

    def test_team_invite_create_ajax(self):
        self.client.force_login(self.owner)
        resp = self.client.post(
            "/settings/team/invite/",
            {"email": "newhire@apex.com", "role": "agent"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("invite_url", data["invite"])

        invite = OrganizationInvite.objects.get(email="newhire@apex.com", organization=self.org)
        self.assertEqual(invite.role, "agent")
        self.assertEqual(invite.status, OrganizationInvite.Status.PENDING)
        self.assertTrue(invite.is_valid)

    def test_team_invite_cannot_invite_existing_member(self):
        self.client.force_login(self.owner)
        resp = self.client.post(
            "/settings/team/invite/",
            {"email": "agent@apex.com", "role": "agent"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already a team member", resp.json()["message"])

    def test_team_invite_revoke(self):
        self.client.force_login(self.owner)
        invite = OrganizationInvite.objects.create(
            organization=self.org,
            email="temp@apex.com",
            role=User.Role.AGENT,
            invited_by=self.owner,
        )
        resp = self.client.post(
            f"/settings/team/invites/{invite.id}/revoke/",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        invite.refresh_from_db()
        self.assertEqual(invite.status, OrganizationInvite.Status.REVOKED)
        self.assertFalse(invite.is_valid)

    def test_team_member_update_role(self):
        self.client.force_login(self.owner)
        resp = self.client.post(
            f"/settings/team/members/{self.agent.id}/role/",
            {"role": "admin"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.role, User.Role.ADMIN)

    def test_cannot_demote_sole_owner(self):
        self.client.force_login(self.owner)
        resp = self.client.post(
            f"/settings/team/members/{self.owner.id}/role/",
            {"role": "agent"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("at least one active Owner", resp.json()["message"])
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.role, User.Role.OWNER)

    def test_cannot_remove_sole_owner(self):
        self.client.force_login(self.owner)
        resp = self.client.post(
            f"/settings/team/members/{self.owner.id}/remove/",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("only Owner", resp.json()["message"])

    def test_team_member_remove_success(self):
        self.client.force_login(self.owner)
        resp = self.client.post(
            f"/settings/team/members/{self.agent.id}/remove/",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        self.agent.refresh_from_db()
        self.assertIsNone(self.agent.organization)
        self.assertFalse(self.agent.is_active)

    def test_accept_invite_flow(self):
        invite = OrganizationInvite.objects.create(
            organization=self.org,
            email="recruit@example.com",
            role=User.Role.AGENT,
            invited_by=self.owner,
        )

        # 1. View invite page
        resp_get = self.client.get(f"/join/{invite.token}/")
        self.assertEqual(resp_get.status_code, 200)
        self.assertContains(resp_get, "Join Apex Auto Shield")

        # 2. Submit form
        resp_post = self.client.post(f"/join/{invite.token}/", {
            "first_name": "Dave",
            "last_name": "Recruit",
            "password": "strongPassword123",
            "confirm_password": "strongPassword123",
        })
        self.assertRedirects(resp_post, "/inbox/")

        # 3. Verify user created
        new_user = User.objects.get(email="recruit@example.com")
        self.assertEqual(new_user.first_name, "Dave")
        self.assertEqual(new_user.organization, self.org)
        self.assertEqual(new_user.role, User.Role.AGENT)
        self.assertTrue(new_user.check_password("strongPassword123"))

        # 4. Verify invite accepted
        invite.refresh_from_db()
        self.assertEqual(invite.status, OrganizationInvite.Status.ACCEPTED)

        # 5. Subsequent visit shows invalid/expired page
        resp_again = self.client.get(f"/join/{invite.token}/")
        self.assertEqual(resp_again.status_code, 200)
        self.assertContains(resp_again, "Invitation No Longer Valid")

    def test_60px_global_header_and_adapted_pages(self):
        from messaging.models import PhoneNumber
        from campaigns.models import Campaign

        phone = PhoneNumber.objects.create(
            organization=self.org,
            e164="+14155550100",
            is_active=True,
        )
        campaign = Campaign.objects.create(
            organization=self.org,
            created_by=self.owner,
            name="Spring Promo",
            phone_number=phone,
            message_template="Hello {{ first_name }}",
            status=Campaign.Status.DRAFT,
        )

        self.client.force_login(self.owner)

        routes_and_breadcrumbs = [
            ("/dashboard/", "Dashboard"),
            ("/inbox/", "Inbox"),
            ("/ai-assistant/", "AI Assistant"),
            ("/contacts/", "Contacts"),
            ("/contacts/lists/", "Contact Lists"),
            ("/contacts/fields/", "Custom Fields"),
            ("/campaigns/", "Campaigns"),
            ("/campaigns/new/", "New Campaign"),
            (f"/campaigns/{campaign.id}/", "Spring Promo"),
            ("/campaigns/templates/", "SMS Templates"),
            ("/compliance/", "Compliance"),
            ("/settings/profile/", "Company Profile"),
            ("/settings/team/", "Team Members"),
            ("/settings/routing/", "Lead Routing"),
            ("/settings/phone-numbers/", "Phone Numbers"),
        ]

        for url, expected_crumb in routes_and_breadcrumbs:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, f"Unexpected status for {url}")
            html = resp.content.decode("utf-8")
            header_idx = html.index("<header")
            main_idx = html.index("<main")
            global_header = html[header_idx:main_idx]
            self.assertIn("h-[60px]", global_header, f"Missing 60px header on {url}")
            self.assertIn('id="nav-availability-btn"', global_header, f"Missing availability pill in global header on {url}")
            self.assertIn(expected_crumb, global_header, f"Missing breadcrumb '{expected_crumb}' on {url}")
            self.assertNotIn('id="nav-availability-btn"', html[main_idx:], f"Availability pill leaked into <main> on {url}")

        # Dashboard consolidates heading inside Page Header without duplicate overview block
        dash_html = self.client.get("/dashboard/").content.decode("utf-8")
        self.assertIn("Welcome, Alice", dash_html[dash_html.index("<main"):])
        self.assertNotIn("Overview and activity", dash_html)

        # Inbox omits <main> page header and uses adapted viewport calc height
        inbox_html = self.client.get("/inbox/").content.decode("utf-8")
        self.assertNotIn("<!-- Page Header -->", inbox_html)
        self.assertIn("sm:h-[calc(100vh-7rem)]", inbox_html)

        # Centered 5xl settings/AI pages align Page Header width with cards
        for aligned_url in (
            "/ai-assistant/",
            "/settings/profile/",
            "/settings/team/",
            "/settings/routing/",
            "/settings/phone-numbers/",
            "/settings/sounds/",
        ):
            aligned_html = self.client.get(aligned_url).content.decode("utf-8")
            self.assertIn("max-w-5xl w-full mx-auto", aligned_html[aligned_html.index("<main"):], aligned_url)
            if "/settings/" in aligned_url:
                self.assertRegex(aligned_html[aligned_html.index("<main"):], r"<h1[^>]*>\s*Settings\s*</h1>")

        # Campaign create & detail pages use icon-only back arrows inline in the Page Header
        create_html = self.client.get("/campaigns/new/").content.decode("utf-8")
        create_main = create_html.index("<main")
        self.assertIn("max-w-4xl", create_html[create_main:])
        self.assertIn('aria-label="Back"', create_html[create_main:])
        self.assertLess(
            create_html.index("Create SMS Campaign", create_main),
            create_html.index("Save Draft", create_main),
        )
        self.assertNotIn("← Cancel &amp; Back", create_html)
        self.assertNotIn("← Cancel & Back", create_html)

        detail_html = self.client.get(f"/campaigns/{campaign.id}/").content.decode("utf-8")
        detail_main = detail_html.index("<main")
        self.assertIn('aria-label="Back to Campaigns"', detail_html[detail_main:])
        self.assertLess(
            detail_html.index("Spring Promo", detail_main),
            detail_html.index("Duplicate", detail_main),
        )
        self.assertNotIn("← All Campaigns", detail_html)
