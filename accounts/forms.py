"""
accounts/forms.py
-----------------
Custom forms for SparkText auth pages.
"""
from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from allauth.account.forms import LoginForm as AllauthLoginForm
from allauth.account.adapter import get_adapter

User = get_user_model()


class CustomLoginForm(AllauthLoginForm):
    """
    Extends allauth's LoginForm. When password authentication fails, checks whether
    the account was registered via Google OAuth and provides an explicit instruction
    to sign in with Google instead of a generic wrong-password error.
    """

    def _clean_with_password(self, credentials: dict):
        adapter = get_adapter(self.request)
        user = adapter.authenticate(self.request, **credentials)
        if user:
            from allauth.account.models import Login
            from allauth.account.internal import flows
            from allauth.core import context
            login = Login(user=user, email=credentials.get("email"))
            if flows.login.is_login_rate_limited(context.request, login):
                raise adapter.validation_error("too_many_login_attempts")
            self._login = login
            self.user = user
        else:
            login_field_val = (self.cleaned_data.get("login") or "").strip().lower()
            if login_field_val:
                try:
                    target_user = User.objects.filter(email__iexact=login_field_val).first()
                    if target_user:
                        is_google = (
                            getattr(target_user, "signup_method", "") == "google"
                            or target_user.socialaccount_set.filter(provider="google").exists()
                        )
                        if is_google and not target_user.has_usable_password():
                            raise forms.ValidationError(
                                "This account was created with Google. Please click 'Continue with Google' to sign in."
                            )
                except forms.ValidationError:
                    raise
                except Exception:
                    pass
            from allauth.account.internal import flows
            login_method = flows.login.derive_login_method(
                login=self.cleaned_data["login"]
            )
            raise adapter.validation_error(f"{login_method.value}_password_mismatch")
        return self.cleaned_data


class SetPasswordForm(forms.Form):
    """
    Password-set form used immediately after email verification.
    Enforces min 10 characters (also enforced by Django's AUTH_PASSWORD_VALIDATORS).
    """

    password1 = forms.CharField(
        label="New password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "minlength": "10"}),
        min_length=10,
    )
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1")
        p2 = cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            raise ValidationError("Passwords do not match.")
        return cleaned


class OnboardingForm(forms.Form):
    """Minimal onboarding form — full name required, rest optional."""

    full_name = forms.CharField(
        label="Full name",
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "Jane Smith", "autocomplete": "name"}),
    )
    phone = forms.CharField(
        label="Phone number",
        max_length=30,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "+1 (555) 000-0000", "autocomplete": "tel"}),
    )
    company = forms.CharField(
        label="Company name",
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Acme Corp", "autocomplete": "organization"}),
    )
    timezone = forms.ChoiceField(
        label="Timezone",
        required=False,
        choices=[],  # populated in __init__
    )
    marketing_opt_in = forms.BooleanField(
        label="Send me product updates and tips",
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import zoneinfo
        tz_choices = [("", "— Select timezone —")] + [
            (tz, tz) for tz in sorted(zoneinfo.available_timezones())
        ]
        self.fields["timezone"].choices = tz_choices


class AccountDeleteForm(forms.Form):
    """Requires the user to type CONFIRM to submit a delete request."""

    confirm = forms.CharField(label='Type "CONFIRM" to delete your account')

    def clean_confirm(self):
        value = self.cleaned_data.get("confirm", "")
        if value.strip().upper() != "CONFIRM":
            raise ValidationError('Please type CONFIRM (all caps) to proceed.')
        return value

