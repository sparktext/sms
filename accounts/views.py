"""
accounts/views.py
-----------------
Custom views for the SparkText authentication + onboarding flow.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth import get_user_model, login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts.forms import AccountDeleteForm, OnboardingForm, SetPasswordForm

logger = logging.getLogger(__name__)
User = get_user_model()


# ---------------------------------------------------------------------------
# Check-email interstitial
# ---------------------------------------------------------------------------

def check_email(request):
    """
    Shown after signup to prompt the user to verify their email.
    Includes a Resend button with a 60-second client-side cooldown.
    """
    # If already authenticated via Google or with verified email, skip straight to onboarding/dashboard
    if request.user.is_authenticated:
        if getattr(request.user, "signup_method", "") == "google" or getattr(request.user, "email_verified_at", None):
            from accounts.adapters import AccountAdapter
            return redirect(AccountAdapter().get_login_redirect_url(request))

    # Try multiple session keys that allauth uses to store the pending email
    email = (
        request.session.get("account_verified_email")  # post-verification key
        or _get_stashed_login_email(request)            # pre-verification stashed login
        or (request.user.email if request.user.is_authenticated else "")
    )
    return render(request, "accounts/check_email.html", {"email": email})


def _get_stashed_login_email(request) -> str:
    """Extract the email from the allauth stashed login (pre-verification stage)."""
    try:
        from allauth.account.internal.stagekit import unstash_login
        login_obj = unstash_login(request, peek=True)
        if login_obj and login_obj.email:
            return login_obj.email
        if login_obj and login_obj.user:
            return login_obj.user.email or ""
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Check Auth Method API (Email-first login)
# ---------------------------------------------------------------------------

def check_auth_method(request):
    """
    Email-first login pre-check endpoint:
    Checks whether the email has an account and whether it is Google or password-based.
    Returns:
    - {'status': 'google', 'email': email, 'message': 'This account is linked with Google.'}
    - {'status': 'password', 'email': email}
    - {'status': 'both', 'email': email}
    - {'status': 'not_found', 'email': email, 'message': 'No account found with this email.'}
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        import json
        body = json.loads(request.body.decode("utf-8") if request.body else "{}")
        raw_email = body.get("email", "")
    except Exception:
        raw_email = request.POST.get("email", "")

    email = (raw_email or "").strip().lower()
    if not email or "@" not in email:
        return JsonResponse({"error": "Please enter a valid email address."}, status=400)

    user = User.objects.filter(email__iexact=email).first()
    if not user:
        return JsonResponse({
            "status": "not_found",
            "email": email,
            "message": "No account found with this email."
        })

    is_google = (
        getattr(user, "signup_method", "") == "google"
        or user.socialaccount_set.filter(provider="google").exists()
    )
    has_password = user.has_usable_password()

    if is_google and not has_password:
        return JsonResponse({
            "status": "google",
            "email": email,
            "message": "This account was registered using Google Sign-In."
        })
    elif is_google and has_password:
        return JsonResponse({
            "status": "both",
            "email": email,
            "message": "You can sign in with your password or Google."
        })
    else:
        return JsonResponse({
            "status": "password",
            "email": email
        })


# ---------------------------------------------------------------------------
# Set Password (after email verification)
# ---------------------------------------------------------------------------

@login_required
def set_password(request):
    """
    Post-verification step: the user (who has no usable password yet or is
    setting it for the first time) chooses their password.
    """
    user = request.user

    # Google users and users who already have a password and finished onboarding should not be stuck here
    if getattr(user, "signup_method", "") == "google" or (user.has_usable_password() and getattr(getattr(user, "profile", None), "onboarding_completed_at", None)):
        if getattr(getattr(user, "profile", None), "onboarding_completed_at", None):
            return redirect(f"/app/{user.public_id}/")
        return redirect("accounts:onboarding")

    if request.method == "POST":
        form = SetPasswordForm(request.POST)
        if form.is_valid():
            raw_password = form.cleaned_data["password1"]
            try:
                validate_password(raw_password, user)
            except ValidationError as exc:
                form.add_error("password1", exc)
            else:
                user.set_password(raw_password)
                user.password_set_at = timezone.now()
                user.save(update_fields=["password", "password_set_at"])

                # Keep the session alive after the password change
                update_session_auth_hash(request, user)

                # Log the event
                from accounts.signals import log_event
                from core.models import AccountEvent
                log_event(user, AccountEvent.EventType.PASSWORD_SET, request=request)

                # Send welcome email (once)
                from accounts.email_service import send_welcome_email
                send_welcome_email(user)

                messages.success(request, "Password set successfully!")
                return redirect("accounts:onboarding")
    else:
        form = SetPasswordForm()

    return render(request, "accounts/set_password.html", {"form": form})


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------

@login_required
def onboarding(request):
    """
    Minimal onboarding form: full name + optional fields.
    Can be skipped (minus full_name which is required).
    """
    user = request.user
    profile, _ = user.profile.__class__.objects.get_or_create(user=user)

    if request.method == "POST":
        form = OnboardingForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            profile.full_name = cd["full_name"]
            profile.phone = cd.get("phone", "")
            profile.company = cd.get("company", "")
            profile.onboarding_completed_at = timezone.now()
            profile.save()

            # Ensure an Organization workspace is created and assigned to the user
            if not user.organization:
                from core.models import Organization
                from django.utils.text import slugify
                from campaigns.services import ensure_default_campaign_templates

                company_name = (profile.company or "").strip()
                org_name = company_name or (f"{profile.full_name}'s Workspace" if profile.full_name else "My Workspace")
                base_slug = slugify(org_name) or "workspace"
                slug = base_slug
                counter = 1
                while Organization.objects.filter(slug=slug).exists():
                    slug = f"{base_slug}-{counter}"
                    counter += 1

                org = Organization.objects.create(
                    name=org_name,
                    slug=slug,
                    notification_email=user.email,
                )
                ensure_default_campaign_templates(org)
                user.organization = org
                user.role = "admin"
                user.marketing_opt_in = cd.get("marketing_opt_in", False)
                user.save(update_fields=["organization", "role", "marketing_opt_in"])
            else:
                user.marketing_opt_in = cd.get("marketing_opt_in", False)
                user.save(update_fields=["marketing_opt_in"])

            # Audit log
            from accounts.signals import log_event
            from core.models import AccountEvent
            log_event(user, AccountEvent.EventType.ONBOARDING_COMPLETED, request=request)

            messages.success(request, "Welcome to SparkText!")
            return redirect(f"/app/{user.public_id}/")
    else:
        form = OnboardingForm(initial={
            "full_name": profile.full_name,
            "phone": profile.phone,
            "company": profile.company,
            "marketing_opt_in": user.marketing_opt_in,
        })

    return render(request, "accounts/onboarding.html", {"form": form, "user": user})


# ---------------------------------------------------------------------------
# /app/ and /app/me/ helpers
# ---------------------------------------------------------------------------

def app_root_redirect(request):
    """
    Handles /app/ root path:
    - If user is logged in: redirects to their dashboard shell /app/<public_id>/ (which checks onboarding)
    - If not logged in: redirects to login
    """
    if not request.user.is_authenticated:
        return redirect("account_login")
    if not getattr(request.user, "public_id", None):
        return redirect("accounts:onboarding")
    return redirect(f"/app/{request.user.public_id}/")


@login_required
def me_redirect(request):
    """
    Used as LOGIN_REDIRECT_URL = '/app/me/'.
    Immediately bounces to /app/<public_id>/ so the accounts adapter can
    enforce onboarding / set-password checks.
    """
    return redirect(f"/app/{request.user.public_id}/")


# ---------------------------------------------------------------------------
# Dashboard shell  /app/<public_id>/
# ---------------------------------------------------------------------------

@login_required
def dashboard_shell(request, public_id):
    """
    Ownership-checked gateway to the main messaging dashboard.
    /app/<public_id>/ → verifies the UUID belongs to the logged-in user,
    then redirects to messaging:dashboard.
    """
    if str(request.user.public_id) != str(public_id):
        # Redirect to the correct URL rather than raising 403
        return redirect(f"/app/{request.user.public_id}/")

    # Lazy-create profile if missing
    try:
        profile = request.user.profile
        if not profile.onboarding_completed_at:
            return redirect("accounts:onboarding")
    except Exception:
        return redirect("accounts:onboarding")

    # If user has no organization yet, send to onboarding to complete setup
    if not request.user.organization:
        return redirect("accounts:onboarding")

    return redirect("messaging:dashboard")


# ---------------------------------------------------------------------------
# Account Settings
# ---------------------------------------------------------------------------

@login_required
def account_settings(request):
    """Profile, password change, connected accounts."""
    user = request.user
    try:
        profile = user.profile
    except Exception:
        from core.models import UserProfile
        profile, _ = UserProfile.objects.get_or_create(user=user)

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "update_profile":
            profile.full_name = request.POST.get("full_name", profile.full_name)
            profile.phone = request.POST.get("phone", profile.phone)
            profile.company = request.POST.get("company", profile.company)
            profile.save()

            from accounts.signals import log_event
            from core.models import AccountEvent
            log_event(user, AccountEvent.EventType.PROFILE_UPDATED, request=request)
            messages.success(request, "Profile updated.")

        elif action == "change_password":
            old_pw = request.POST.get("old_password", "")
            new_pw1 = request.POST.get("new_password1", "")
            new_pw2 = request.POST.get("new_password2", "")

            if not user.check_password(old_pw):
                messages.error(request, "Current password is incorrect.")
            elif new_pw1 != new_pw2:
                messages.error(request, "New passwords do not match.")
            else:
                try:
                    validate_password(new_pw1, user)
                    user.set_password(new_pw1)
                    user.password_set_at = timezone.now()
                    user.save(update_fields=["password", "password_set_at"])
                    update_session_auth_hash(request, user)

                    from accounts.signals import log_event
                    from core.models import AccountEvent
                    log_event(user, AccountEvent.EventType.PASSWORD_CHANGED, request=request)

                    from accounts.email_service import send_password_changed_email
                    send_password_changed_email(user)

                    messages.success(request, "Password changed successfully.")
                except ValidationError as exc:
                    for err in exc.messages:
                        messages.error(request, err)

        return redirect("accounts:account_settings")

    return render(request, "accounts/account_settings.html", {
        "profile": profile,
        "user": user,
    })


# ---------------------------------------------------------------------------
# Account Delete (soft delete with 30-day recovery window)
# ---------------------------------------------------------------------------

@login_required
@require_http_methods(["GET", "POST"])
def account_delete(request):
    """
    Soft-delete: sets user.status=deleted and user.deleted_at, then logs out.
    The account can be recovered within 30 days by contacting support.
    """
    user = request.user

    if request.method == "POST":
        form = AccountDeleteForm(request.POST)
        if form.is_valid():
            from django.contrib.auth import logout as auth_logout
            from accounts.signals import log_event
            from core.models import AccountEvent

            user.status = "deleted"
            user.deleted_at = timezone.now()
            user.is_active = False
            user.save(update_fields=["status", "deleted_at", "is_active"])

            log_event(user, AccountEvent.EventType.ACCOUNT_DELETED, request=request)
            auth_logout(request)
            messages.info(request, "Your account has been scheduled for deletion.")
            return redirect("/")
    else:
        form = AccountDeleteForm()

    return render(request, "accounts/account_delete.html", {"form": form})

