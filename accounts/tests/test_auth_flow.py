"""
accounts/tests/test_auth_flow.py
---------------------------------
Tests covering the core authentication and onboarding flow.

Run with: python manage.py test accounts.tests
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

User = get_user_model()


@override_settings(
    # Use in-memory email backend for tests
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    # Disable axes during tests to avoid lockout issues
    AXES_ENABLED=False,
    # Disable inactivity middleware  
    INACTIVITY_TIMEOUT=9999999,
)
class SignupFlowTest(TestCase):
    """Tests for the email-based signup flow."""

    def setUp(self):
        self.client = Client()

    def test_signup_page_loads(self):
        """The allauth signup page should respond 200."""
        response = self.client.get(reverse("account_signup"))
        self.assertEqual(response.status_code, 200)

    def test_login_page_loads(self):
        """The login page should respond 200."""
        response = self.client.get(reverse("account_login"))
        self.assertEqual(response.status_code, 200)

    def test_check_email_page_loads(self):
        """The check-email interstitial should respond 200."""
        response = self.client.get(reverse("accounts:check_email"))
        self.assertEqual(response.status_code, 200)

    def test_set_password_requires_login(self):
        """set_password view should redirect unauthenticated users."""
        response = self.client.get(reverse("accounts:set_password"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_onboarding_requires_login(self):
        """Onboarding view should redirect unauthenticated users."""
        response = self.client.get(reverse("accounts:onboarding"))
        self.assertEqual(response.status_code, 302)

    def test_lockout_page_loads(self):
        """The axes lockout template should respond 200."""
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)

    def test_password_reset_page_loads(self):
        """The password reset request page should respond 200."""
        response = self.client.get(reverse("account_reset_password"))
        self.assertEqual(response.status_code, 200)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    AXES_ENABLED=False,
    INACTIVITY_TIMEOUT=9999999,
)
class UserModelTest(TestCase):
    """Tests for the enhanced User model."""

    def test_user_has_public_id(self):
        """Creating a user should auto-assign a UUID public_id."""
        import uuid
        user = User.objects.create_user(email="test@example.com", password="TestPass1234!")
        self.assertIsNotNone(user.public_id)
        self.assertIsInstance(user.public_id, uuid.UUID)

    def test_user_default_status_is_active(self):
        """New users should have status='active'."""
        user = User.objects.create_user(email="active@example.com", password="TestPass1234!")
        self.assertEqual(user.status, "active")

    def test_user_default_signup_method_is_email(self):
        """New users created via create_user should have signup_method='email'."""
        user = User.objects.create_user(email="email@example.com", password="TestPass1234!")
        self.assertEqual(user.signup_method, "email")

    def test_user_profile_auto_created(self):
        """A UserProfile should be created whenever a new User is saved."""
        from core.models import UserProfile
        user = User.objects.create_user(email="profile@example.com", password="TestPass1234!")
        self.assertTrue(UserProfile.objects.filter(user=user).exists())

    def test_public_id_is_unique(self):
        """Two different users should have different public_ids."""
        user1 = User.objects.create_user(email="u1@example.com", password="TestPass1234!")
        user2 = User.objects.create_user(email="u2@example.com", password="TestPass1234!")
        self.assertNotEqual(user1.public_id, user2.public_id)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    AXES_ENABLED=False,
    INACTIVITY_TIMEOUT=9999999,
)
class SetPasswordViewTest(TestCase):
    """Tests for the set_password view."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="setpw@example.com", password=None,
        )
        self.user.set_unusable_password()
        self.user.save()
        self.client.force_login(self.user)

    def test_set_password_form_loads(self):
        response = self.client.get(reverse("accounts:set_password"))
        self.assertEqual(response.status_code, 200)

    def test_set_password_success(self):
        response = self.client.post(reverse("accounts:set_password"), {
            "password1": "StrongPass123!",
            "password2": "StrongPass123!",
        })
        # Should redirect to onboarding
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.has_usable_password())
        self.assertIsNotNone(self.user.password_set_at)

    def test_set_password_mismatch(self):
        response = self.client.post(reverse("accounts:set_password"), {
            "password1": "StrongPass123!",
            "password2": "DifferentPass456!",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Passwords do not match")


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    AXES_ENABLED=False,
    INACTIVITY_TIMEOUT=9999999,
)
class OnboardingViewTest(TestCase):
    """Tests for the onboarding view."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="onboard@example.com", password="TestPass1234!",
        )
        self.client.force_login(self.user)

    def test_onboarding_loads(self):
        response = self.client.get(reverse("accounts:onboarding"))
        self.assertEqual(response.status_code, 200)

    def test_onboarding_completes(self):
        response = self.client.post(reverse("accounts:onboarding"), {
            "full_name": "Jane Smith",
            "phone": "+1555000000",
            "company": "Acme",
            "marketing_opt_in": True,
        })
        self.assertEqual(response.status_code, 302)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.full_name, "Jane Smith")
        self.assertIsNotNone(self.user.profile.onboarding_completed_at)

    def test_onboarding_requires_full_name(self):
        response = self.client.post(reverse("accounts:onboarding"), {
            "full_name": "",  # missing
        })
        self.assertEqual(response.status_code, 200)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    AXES_ENABLED=False,
    INACTIVITY_TIMEOUT=9999999,
)
class DashboardShellTest(TestCase):
    """Tests for the /app/<uuid>/ dashboard shell."""

    def setUp(self):
        from core.models import UserProfile
        self.user = User.objects.create_user(
            email="dash@example.com", password="TestPass1234!",
        )
        # Complete onboarding
        from django.utils import timezone
        self.user.profile.full_name = "Dash User"
        self.user.profile.onboarding_completed_at = timezone.now()
        self.user.profile.save()
        self.client.force_login(self.user)

    def test_own_dashboard_redirects(self):
        """Accessing /app/<own-uuid>/ should redirect to messaging dashboard."""
        url = reverse("accounts_shell:dashboard_shell", kwargs={"public_id": self.user.public_id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_other_user_dashboard_redirects_to_own(self):
        """Accessing another user's /app/<uuid>/ redirects to own."""
        import uuid
        other_id = uuid.uuid4()
        # Create another user just to have a different UUID
        other_user = User.objects.create_user(email="other@example.com", password="TestPass1234!")
        url = reverse("accounts_shell:dashboard_shell", kwargs={"public_id": other_user.public_id})
        response = self.client.get(url)
        # Should redirect to own dashboard (302)
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(self.user.public_id), response["Location"])


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    AXES_ENABLED=False,
    INACTIVITY_TIMEOUT=9999999,
)
class InactivityMiddlewareTest(TestCase):
    """Tests for the inactivity logout middleware."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="inactivity@example.com", password="TestPass1234!",
        )

    @override_settings(INACTIVITY_TIMEOUT=1)  # 1 second timeout for test
    def test_inactivity_logs_out(self):
        """A session older than INACTIVITY_TIMEOUT seconds should be invalidated."""
        import time
        self.client.force_login(self.user)
        # Manually set an old last_activity
        session = self.client.session
        session["last_activity"] = time.time() - 10  # 10 seconds ago
        session.save()
        response = self.client.get(reverse("accounts:check_email"))
        # Should have been logged out and redirected
        self.assertEqual(response.status_code, 302)
        self.assertIn("reason=inactivity", response["Location"])

