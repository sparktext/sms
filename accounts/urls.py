"""
accounts/urls.py
-----------------
URL patterns for the custom accounts app.
Mounted at:
  /accounts/   — auth flow views (check-email, set-password, onboarding, settings)
  /app/        — dashboard shell /app/<uuid:public_id>/
"""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    # --- Auth flow ---
    # Shown after email signup: "check your inbox"
    path("check-email/", views.check_email, name="check_email"),

    # Pre-check login auth method (Google vs Password)
    path("check-auth-method/", views.check_auth_method, name="check_auth_method"),

    # Set password after email verification
    path("set-password/", views.set_password, name="set_password"),

    # Onboarding wizard (profile completion)
    path("onboarding/", views.onboarding, name="onboarding"),

    # Account settings (profile, password, delete)
    path("settings/account/", views.account_settings, name="account_settings"),

    # Account delete confirmation
    path("settings/account/delete/", views.account_delete, name="account_delete"),

    # LOGIN_REDIRECT_URL shortcut → resolves to user's /app/<public_id>/
    path("me/", views.me_redirect, name="me_redirect"),

    # --- Dashboard shell ---
    # /app/  →  resolves to logged in user's /app/<public_id>/
    path("", views.app_root_redirect, name="app_root"),
    # /app/<public_id>/  →  ownership check → redirect to messaging:dashboard
    path("<uuid:public_id>/", views.dashboard_shell, name="dashboard_shell"),
]
