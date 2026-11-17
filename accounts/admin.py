"""
accounts/admin.py
-----------------
Admin registrations for auth-enhancement models (AccountEvent, EmailLog, UserProfile).
Also replaces the base CustomUserAdmin from core/admin.py with an extended version
that includes the new fields and a UserProfile inline.
"""
from __future__ import annotations

from django.contrib import admin
from django.contrib.auth import get_user_model

from core.models import AccountEvent, EmailLog, UserProfile

User = get_user_model()


# ---------------------------------------------------------------------------
# UserProfile inline — shown on the User change page
# ---------------------------------------------------------------------------

class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = "Profile"
    fields = (
        "full_name", "phone", "company",
        "avatar_url", "timezone", "language", "onboarding_completed_at",
    )
    readonly_fields = ("onboarding_completed_at",)


# ---------------------------------------------------------------------------
# Unregister the User admin from core/admin.py and re-register with extras
# ---------------------------------------------------------------------------

try:
    from core.admin import CustomUserAdmin

    # Only unregister if already registered (safe guard)
    if admin.site.is_registered(User):
        admin.site.unregister(User)

    @admin.register(User)
    class EnhancedUserAdmin(CustomUserAdmin):
        inlines = [UserProfileInline]
        list_display = (
            "email", "organization", "role", "status",
            "signup_method", "is_staff", "created_at",
        )
        list_filter = ("status", "signup_method", "role", "is_staff", "is_active")
        readonly_fields = (
            "public_id", "created_at", "updated_at",
            "email_verified_at", "password_set_at",
            "welcome_email_sent_at", "last_login_ip",
            "suspended_at", "deleted_at",
        )
        fieldsets = CustomUserAdmin.fieldsets + (
            (
                "Auth Enhancement",
                {
                    "fields": (
                        "public_id", "status", "signup_method",
                        "created_at", "updated_at", "email_verified_at",
                        "password_set_at", "welcome_email_sent_at",
                        "last_login_ip", "suspended_at", "deleted_at",
                    )
                },
            ),
            (
                "Consent",
                {"fields": ("terms_version", "marketing_opt_in")},
            ),
        )

except Exception:
    # Graceful fallback: if core admin isn't available yet, skip enhancement
    pass


# ---------------------------------------------------------------------------
# AccountEvent — read-only audit trail
# ---------------------------------------------------------------------------

@admin.register(AccountEvent)
class AccountEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "user", "user_public_id", "ip_address", "created_at")
    list_filter = ("event_type",)
    search_fields = ("user__email", "user_public_id", "ip_address")
    readonly_fields = (
        "user", "user_public_id", "event_type",
        "metadata", "ip_address", "user_agent", "created_at",
    )
    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


# ---------------------------------------------------------------------------
# EmailLog — read-only delivery tracking
# ---------------------------------------------------------------------------

@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ("email_type", "to_address", "state", "queued_at", "sent_at")
    list_filter = ("state", "email_type")
    search_fields = ("to_address", "provider_message_id")
    readonly_fields = (
        "user", "email_type", "to_address", "state",
        "provider_message_id", "error", "queued_at", "sent_at",
    )
    ordering = ("-queued_at",)
    date_hierarchy = "queued_at"

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
