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
        1. Non-social user without usable password → set-password page
        2. Incomplete onboarding profile or missing organization → onboarding page
        3. Otherwise → user's dashboard shell
        """
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return reverse("account_login")

        # Social (Google) users do not need a password
        if getattr(user, "signup_method", "") != "google" and not user.has_usable_password():
            return reverse("accounts:set_password")

        # Drive incomplete onboarding
        try:
            if not user.profile.onboarding_completed_at:
                return reverse("accounts:onboarding")
        except Exception:
            return reverse("accounts:onboarding")

        # If user has no organization yet, send to onboarding to complete setup
        if not getattr(user, "organization", None):
            return reverse("accounts:onboarding")

        # Fallback if public_id not yet set
        if not getattr(user, "public_id", None):
            return reverse("accounts:onboarding")

        return f"/app/{user.public_id}/"

    def get_signup_redirect_url(self, request) -> str:
        """
        After signup:
        - If user signed up via Google or is already authenticated with a verified email,
          send them directly to onboarding (or dashboard) via get_login_redirect_url(request).
        - If email verification is mandatory/pending (standard email+password signup),
          send them to the check-email interstitial.
        """
        user = getattr(request, "user", None)
        if user and user.is_authenticated:
            # Google / social signups or any verified user should NEVER see "check-email"
            if getattr(user, "signup_method", "") == "google" or getattr(user, "email_verified_at", None):
                return self.get_login_redirect_url(request)
        return reverse("accounts:check_email")

    def get_email_verification_redirect_url(self, email_address) -> str:
        """After clicking the verification link, proceed to onboarding or dashboard."""
        user = getattr(email_address, "user", None)
        if not user:
            return reverse("account_login")
        try:
            if not user.profile.onboarding_completed_at:
                return reverse("accounts:onboarding")
        except Exception:
            return reverse("accounts:onboarding")
        if not getattr(user, "organization", None):
            return reverse("accounts:onboarding")
        return f"/app/{user.public_id}/" if getattr(user, "public_id", None) else reverse("accounts:onboarding")

    def is_open_for_signup(self, request) -> bool:
        return True

    def is_login_by_code_required(self, login) -> bool:
        """SparkText uses standard password/social auth; disable login-by-code stage to prevent allauth session bug."""
        return False

    def respond_email_verification_sent(self, request, user=None):
        """After verification email is dispatched, redirect to styled check-email view."""
        if user and getattr(user, "email", None):
            # NOT allauth's "account_verified_email": that key tells allauth the address is ALREADY
            # verified, so a second signup in the same browser skipped verification entirely.
            request.session["sparktext_check_email"] = user.email
        from django.shortcuts import redirect
        return redirect("accounts:check_email")

    def send_mail(self, template_prefix: str, email: str, context: dict) -> None:
        """Override allauth's send_mail to track verification and password reset emails in EmailLog."""
        import logging
        from django.utils import timezone
        from core.models import EmailLog

        logger = logging.getLogger(__name__)
        user = context.get("user")
        log = EmailLog.objects.create(
            user=user if getattr(user, "pk", None) else None,
            email_type="verification" if "confirmation" in template_prefix else template_prefix,
            to_address=email,
            state=EmailLog.State.QUEUED,
        )
        try:
            super().send_mail(template_prefix, email, context)
            log.state = EmailLog.State.SENT
            log.sent_at = timezone.now()
            log.save(update_fields=["state", "sent_at"])
            logger.info("Email '%s' sent to %s via Brevo", template_prefix, email)
        except Exception as exc:
            log.state = EmailLog.State.FAILED
            log.error = str(exc)
            log.save(update_fields=["state", "error"])
            logger.exception("Failed sending email '%s' to %s: %s", template_prefix, email, exc)
            raise

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
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return reverse("account_login")
        try:
            if not user.profile.onboarding_completed_at:
                return reverse("accounts:onboarding")
        except Exception:
            return reverse("accounts:onboarding")
        if not getattr(user, "organization", None):
            return reverse("accounts:onboarding")
        if not getattr(user, "public_id", None):
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

