"""
accounts/email_service.py
--------------------------
High-level email sending helpers.

Each function:
  1. Guards against double-sends (idempotency)
  2. Creates an EmailLog row in state=QUEUED
  3. Dispatches to Celery (async) or falls back to synchronous send
  4. Updates the EmailLog row + relevant User timestamp on success/failure
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

if TYPE_CHECKING:
    from django.contrib.auth import get_user_model
    User = get_user_model()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_email_log_model():
    from core.models import EmailLog
    return EmailLog


def _send_email_now(
    email_type: str,
    to_address: str,
    subject: str,
    html_template: str,
    txt_template: str,
    context: dict,
    user=None,
) -> None:
    """
    Synchronously render templates, send via Django email backend, and
    update the EmailLog record accordingly.
    """
    EmailLog = _get_email_log_model()
    log = EmailLog.objects.create(
        user=user,
        email_type=email_type,
        to_address=to_address,
        state=EmailLog.State.QUEUED,
    )
    try:
        html_body = render_to_string(html_template, context)
        txt_body = render_to_string(txt_template, context)

        msg = EmailMultiAlternatives(
            subject=subject,
            body=txt_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to_address],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send()

        log.state = EmailLog.State.SENT
        log.sent_at = timezone.now()
        log.save(update_fields=["state", "sent_at"])
        logger.info("Email '%s' sent to %s", email_type, to_address)
    except Exception as exc:
        log.state = EmailLog.State.FAILED
        log.error = str(exc)
        log.save(update_fields=["state", "error"])
        logger.exception("Failed to send email '%s' to %s: %s", email_type, to_address, exc)
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_welcome_email(user) -> None:
    """
    Send a one-time welcome email after the user's first login/signup.
    Idempotent: will not send if welcome_email_sent_at is already set.
    Dispatched via a daemon background thread so HTTP response returns instantly
    and Brevo sends immediately without Celery/Redis connection delays.
    """
    if user.welcome_email_sent_at:
        return  # Already sent

    import threading

    def _dispatch():
        try:
            _send_welcome_email_sync(user)
        except Exception as exc:
            logger.warning("Could not send welcome email: %s", exc)

    threading.Thread(target=_dispatch, daemon=True).start()


def _send_welcome_email_sync(user) -> None:
    """Synchronous welcome email dispatched via background thread."""
    full_name = ""
    try:
        full_name = user.profile.full_name or ""
    except Exception:
        pass

    base_url = getattr(settings, "PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        base_url = "http://127.0.0.1:8000" if settings.DEBUG else "https://sparktext.si"

    app_url = f"{base_url}/app/{user.public_id}/" if getattr(user, "public_id", None) else f"{base_url}/app/"

    _send_email_now(
        email_type="welcome",
        to_address=user.email,
        subject="Welcome to SparkText 🎉",
        html_template="accounts/emails/welcome_email.html",
        txt_template="accounts/emails/welcome_email.txt",
        context={
            "user": user,
            "full_name": full_name or user.email.split("@")[0],
            "app_url": app_url,
            "base_url": base_url,
        },
        user=user,
    )
    user.welcome_email_sent_at = timezone.now()
    user.save(update_fields=["welcome_email_sent_at"])

    # Log the event
    from accounts.signals import log_event
    from core.models import AccountEvent
    log_event(user, AccountEvent.EventType.WELCOME_EMAIL_SENT)


def send_password_changed_email(user) -> None:
    """Security notification email when a user changes their password."""
    import threading

    def _dispatch():
        try:
            _send_password_changed_email_sync(user)
        except Exception as exc:
            logger.warning("Could not send password changed email: %s", exc)

    threading.Thread(target=_dispatch, daemon=True).start()


def _send_password_changed_email_sync(user) -> None:
    base_url = getattr(settings, "PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        base_url = "http://127.0.0.1:8000" if settings.DEBUG else "https://sparktext.si"

    _send_email_now(
        email_type="password_changed",
        to_address=user.email,
        subject="Your SparkText password was changed",
        html_template="accounts/emails/password_changed_email.html",
        txt_template="accounts/emails/password_changed_email.txt",
        context={"user": user, "base_url": base_url},
        user=user,
    )

