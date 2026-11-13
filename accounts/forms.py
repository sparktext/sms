"""
accounts/forms.py
-----------------
Custom forms for SparkText auth pages.
"""
from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

User = get_user_model()


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

