"""
accounts/tasks.py
-----------------
Celery tasks for async email sending.
All tasks are idempotent — safe to retry.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils import timezone

logger = logging.getLogger(__name__)

User = get_user_model()


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def task_send_welcome_email(self, user_pk: int) -> None:
    """Async task: send a welcome email to the newly registered user."""
    from accounts.email_service import _send_welcome_email_sync

    try:
        user = User.objects.get(pk=user_pk)
    except User.DoesNotExist:
        logger.warning("task_send_welcome_email: user %s not found", user_pk)
        return

    if user.welcome_email_sent_at:
        logger.info("task_send_welcome_email: already sent for user %s", user_pk)
        return

    try:
        _send_welcome_email_sync(user)
    except Exception as exc:
        logger.exception("task_send_welcome_email failed for user %s: %s", user_pk, exc)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def task_send_password_changed_email(self, user_pk: int) -> None:
    """Async task: notify the user that their password was changed."""
    from accounts.email_service import _send_password_changed_email_sync

    try:
        user = User.objects.get(pk=user_pk)
    except User.DoesNotExist:
        logger.warning("task_send_password_changed_email: user %s not found", user_pk)
        return

    try:
        _send_password_changed_email_sync(user)
    except Exception as exc:
        logger.exception("task_send_password_changed_email failed for user %s: %s", user_pk, exc)
        raise self.retry(exc=exc)

