"""
accounts/signals.py
--------------------
Signal handlers for the auth system:
- Login / logout / login_failed → AccountEvent
- User post_save → ensure UserProfile exists

Also exports the `log_event` helper used by other modules.
"""
from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def log_event(
    user,
    event_type: str,
    request=None,
    **meta: Any,
) -> None:
    """
    Create an AccountEvent row. Safe to call from anywhere; never raises.
    """
    try:
        from core.models import AccountEvent

        ip: str | None = None
        ua: str = ""
        if request is not None:
            x_fwd = request.META.get("HTTP_X_FORWARDED_FOR")
            ip = x_fwd.split(",")[0].strip() if x_fwd else request.META.get("REMOTE_ADDR")
            ua = request.META.get("HTTP_USER_AGENT", "")

        AccountEvent.objects.create(
            user=user,
            user_public_id=getattr(user, "public_id", None),
            event_type=event_type,
            metadata=meta,
            ip_address=ip,
            user_agent=ua,
        )
    except Exception as exc:
        logger.exception("log_event failed: %s", exc)


# ---------------------------------------------------------------------------
# Auth signals
# ---------------------------------------------------------------------------

@receiver(user_logged_in)
def on_user_logged_in(sender, request, user, **kwargs) -> None:
    from core.models import AccountEvent

    # Persist last login IP on the user record
    try:
        x_fwd = request.META.get("HTTP_X_FORWARDED_FOR")
        ip = x_fwd.split(",")[0].strip() if x_fwd else request.META.get("REMOTE_ADDR")
        if ip:
            user.last_login_ip = ip
            user.save(update_fields=["last_login_ip"])
    except Exception:
        pass

    log_event(user, AccountEvent.EventType.LOGIN, request=request)


@receiver(user_logged_out)
def on_user_logged_out(sender, request, user, **kwargs) -> None:
    if user and user.pk:
        from core.models import AccountEvent
        log_event(user, AccountEvent.EventType.LOGOUT, request=request)


@receiver(user_login_failed)
def on_user_login_failed(sender, credentials, request, **kwargs) -> None:
    from core.models import AccountEvent

    # Don't expose the actual password in the log
    safe_creds = {k: v for k, v in credentials.items() if k != "password"}
    log_event(None, AccountEvent.EventType.LOGIN_FAILED, request=request, credentials=safe_creds)


# ---------------------------------------------------------------------------
# Ensure UserProfile exists whenever a User is saved
# ---------------------------------------------------------------------------

@receiver(post_save, sender="core.User")
def ensure_user_profile(sender, instance, created: bool, **kwargs) -> None:
    """Auto-create a UserProfile for every new User."""
    if created:
        try:
            from core.models import UserProfile, AccountEvent
            UserProfile.objects.get_or_create(user=instance)
            log_event(instance, AccountEvent.EventType.ACCOUNT_CREATED)
        except Exception as exc:
            logger.exception("ensure_user_profile failed: %s", exc)

