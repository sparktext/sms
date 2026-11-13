"""
accounts/adapters.py
--------------------
Custom allauth adapters for SparkText:
- AccountAdapter  : controls login redirect, signup redirect, user saving
- SocialAccountAdapter : auto-connects Google login to existing accounts
"""
from __future__ import annotations

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.urls import reverse


class AccountAdapter(DefaultAccountAdapter):
    """Custom account adapter that enforces the SparkText onboarding flow."""

    def get_login_redirect_url(self, request) -> str:
        """
        Post-login redirect priority:
        1. No usable password → set-password page
        2. No onboarding profile → onboarding page
        3. Otherwise → user's dashboard shell
        """
        user = request.user
        # Google-only accounts might not have a password yet
        if not user.has_usable_password():
            return reverse("accounts:set_password")
        # Drive incomplete onboarding
        try:
            if not user.profile.onboarding_completed_at:
                return reverse("accounts:onboarding")
        except Exception:
            return reverse("accounts:onboarding")
        # Fallback if public_id not yet set
        if not getattr(user, "public_id", None):
            return reverse("accounts:onboarding")
        return f"/app/{user.public_id}/"

    def get_signup_redirect_url(self, request) -> str:
        """After email signup, go to the check-email interstitial."""
        return reverse("accounts:check_email")

    def get_email_confirmation_redirect_url(self, request) -> str:
        """After clicking the verification link, go to set-password."""
        return reverse("accounts:set_password")

    def is_open_for_signup(self, request) -> bool:
        return True

    def save_user(self, request, user, form, commit: bool = True):
        """Persist email-signup users; create a matching UserProfile."""
        user = super().save_user(request, user, form, commit=False)
        user.signup_method = "email"
        user.email = user.email.lower()
        if commit:
            user.save()
            from core.models import UserProfile
            UserProfile.objects.get_or_create(user=user)
        return user


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Custom social-account adapter for Google OAuth."""

    def get_connect_redirect_url(self, request, socialaccount):
        """After Google OAuth completes, run through the same redirect logic."""
        return self._post_login_redirect(request)

    def _post_login_redirect(self, request):
        user = request.user
        if not user.is_authenticated:
            return reverse("account_login")
        # Google accounts may not have a password set
        if not user.has_usable_password():
            return reverse("accounts:set_password")
        try:
            if not user.profile.onboarding_completed_at:
                return reverse("accounts:onboarding")
        except Exception:
            return reverse("accounts:onboarding")
        return f"/app/{user.public_id}/"

    def pre_social_login(self, request, sociallogin) -> None:
        """
        Auto-connect a new Google login to an existing account with the same
        verified email address, so users don't end up with duplicate accounts.
        """
        from django.contrib.auth import get_user_model

        if sociallogin.is_existing:
            return

        email = sociallogin.account.extra_data.get("email", "").lower()
        if not email:
            return

        User = get_user_model()
        try:
            existing = User.objects.get(email__iexact=email)
            sociallogin.connect(request, existing)
        except User.DoesNotExist:
            pass

    def save_user(self, request, sociallogin, form=None):
        """
        Save a Google-authenticated user:
        - Mark signup method and verify email (Google already verified it)
        - Create / update UserProfile with Google avatar + display name
        - Send welcome email (once)
        """
        from django.utils import timezone
        from core.models import UserProfile
        from accounts.email_service import send_welcome_email

        user = super().save_user(request, sociallogin, form=form)
        user.signup_method = "google"
        user.email = user.email.lower()

        if not user.email_verified_at:
            user.email_verified_at = timezone.now()
        user.save(update_fields=["signup_method", "email", "email_verified_at"])

        # Populate profile from Google data
        extra = sociallogin.account.extra_data
        profile, _ = UserProfile.objects.get_or_create(user=user)
        if not profile.full_name:
            profile.full_name = extra.get("name", "")
        if not profile.avatar_url:
            profile.avatar_url = extra.get("picture", "")
        profile.save()

        # Idempotent welcome email
        send_welcome_email(user)
        return user

