from datetime import timedelta
from django.test import TestCase, Client
from django.utils import timezone
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
            "timezone": "America/New_York",
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
        self.assertEqual(self.org.timezone, "America/New_York")
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
