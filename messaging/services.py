import logging
from datetime import timedelta
from typing import Tuple, Optional
from django.conf import settings
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from contacts.models import Contact
from compliance.models import ConsentEvent
from compliance.services import record_consent
from .models import Conversation, Message
from .providers import get_sms_provider
from .utils import get_public_base_url

logger = logging.getLogger(__name__)


def can_send_manual(contact: Contact, conversation: Optional[Conversation]) -> Tuple[bool, str]:
    """
    Enforces compliance guards for manual two-way texting:
    - opted_out: BLOCKED ALWAYS.
    - opted_in: ALLOWED.
    - unknown: ALLOWED ONLY if the customer sent an inbound text in this conversation
      within the last MESSAGING_REPLY_WINDOW_HOURS (default 24h). Otherwise BLOCKED.
    """
    if contact.sms_status == Contact.SMSStatus.OPTED_OUT:
        return False, _("Contact has opted out of SMS messages (STOP received). You cannot send messages to this contact.")

    if contact.sms_status == Contact.SMSStatus.OPTED_IN:
        return True, ""

    # Contact is UNKNOWN (no prior opt-in consent recorded)
    reply_window_hours = getattr(settings, "MESSAGING_REPLY_WINDOW_HOURS", 24)
    if conversation and conversation.last_inbound_at:
        elapsed = timezone.now() - conversation.last_inbound_at
        if elapsed <= timedelta(hours=reply_window_hours):
            return True, ""
        return False, _(
            f"No active consent on file. Conversational reply window expired (can only reply within {reply_window_hours} hours of customer's incoming text)."
        )

    return False, _(
        "No consent on file. Manual texting is blocked until the contact opts in or texts your number first."
    )


def send_message(
    conversation: Conversation,
    body: str,
    user=None,
    sender_type: str = Message.SenderType.HUMAN,
    is_ai_assisted: bool = False,
) -> Message:
    """
    Sends an outbound SMS message:
    1. Validates sender credentials and message length.
    2. Runs compliance check via can_send_manual.
    3. Commits the Message row in 'queued' status BEFORE invoking provider.
    4. Dispatches via SMS provider outside any database transaction.
    5. Updates Message with provider SID / status, and handles Twilio 21610 opt-out.
    6. Updates conversation.last_message_at.
    """
    if sender_type == Message.SenderType.HUMAN:
        if not user or not user.is_authenticated:
            raise PermissionDenied(_("Authentication required to send messages."))

        if user.organization_id != conversation.organization_id:
            raise PermissionDenied(_("User does not belong to the conversation's business organization."))
    elif sender_type == Message.SenderType.AI:
        user = None

    cleaned_body = (body or "").strip()
    if not cleaned_body:
        raise ValidationError({"body": _("Message body cannot be empty.")})

    if len(cleaned_body) > 1600:
        raise ValidationError({"body": _("Message exceeds maximum length of 1,600 characters.")})

    if sender_type == Message.SenderType.CAMPAIGN:
        from compliance.services import can_send_marketing
        if not can_send_marketing(conversation.contact):
            raise ValidationError({"non_field_errors": _("Marketing campaigns require active opt-in consent.")})
    else:
        allowed, reason = can_send_manual(conversation.contact, conversation)
        if not allowed:
            raise ValidationError({"non_field_errors": reason})

    # Resolve provider and appropriate status callback URL dynamically
    provider = get_sms_provider()
    public_base = get_public_base_url()
    from .providers.telnyx_provider import TelnyxProvider
    if isinstance(provider, TelnyxProvider):
        provider_name = "telnyx"
        status_callback = f"{public_base}/webhooks/telnyx/status/" if public_base else None
    else:
        provider_name = "twilio"
        status_callback = f"{public_base}/webhooks/twilio/status/" if public_base else None

    # Step 1: Create and commit the queued message row first
    with transaction.atomic():
        message = Message.objects.create(
            conversation=conversation,
            organization=conversation.organization,
            direction=Message.Direction.OUTBOUND,
            sender_type=sender_type,
            sent_by=user,
            body=cleaned_body,
            status=Message.Status.QUEUED,
            provider=provider_name,
            is_ai_assisted=is_ai_assisted,
        )

    # Step 2: Call provider OUTSIDE open transaction
    logger.info(
        "Dispatching outbound SMS (msg_id=%s, provider=%s) to %s with status_callback=%s",
        message.id,
        provider_name,
        conversation.contact.phone,
        status_callback,
    )

    result = provider.send_sms(
        from_number=conversation.phone_number.e164,
        to_number=conversation.contact.phone,
        body=cleaned_body,
        status_callback_url=status_callback,
    )

    # Step 3: Update message row with provider result
    if result.success:
        message.provider_message_sid = result.provider_message_sid
        message.status = result.status or Message.Status.SENT
        message.num_segments = result.num_segments
        message.sent_at = timezone.now()
        message.save(update_fields=["provider_message_sid", "status", "num_segments", "sent_at"])
    else:
        message.status = Message.Status.FAILED
        message.error_code = result.error_code
        message.error_message = result.error_message
        message.save(update_fields=["status", "error_code", "error_message"])

        # Check for Twilio error 21610: recipient unsubscribed
        if str(result.error_code) == "21610":
            record_consent(
                contact=conversation.contact,
                event_type=ConsentEvent.EventType.OPT_OUT,
                source=ConsentEvent.Source.PROVIDER,
                proof_text=f"Twilio error 21610: Recipient is unsubscribed ({result.error_message})",
            )

    # Step 4: Update conversation.last_message_at
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=["last_message_at"])

    # Real-time WebSocket broadcast to open conversation windows
    try:
        from .ws_broadcast import broadcast_chat_event, broadcast_inbox_event
        from .utils import format_chat_timestamp
        broadcast_chat_event(
            conversation.id,
            {
                "event": "new_message",
                "message": {
                    "id": message.id,
                    "body": message.body,
                    "direction": message.direction,
                    "sender_type": message.sender_type,
                    "created_at": format_chat_timestamp(message.sent_at or message.created_at),
                    "status": message.status,
                },
            },
        )
        # Real-time WebSocket broadcast to sidebar inbox list
        total_unread = (
            Conversation.objects.filter(
                organization_id=conversation.organization_id,
                status__in=[Conversation.Status.OPEN, Conversation.Status.NEEDS_HUMAN],
            ).aggregate(total=Sum("unread_count"))["total"]
            or 0
        )
        broadcast_inbox_event(
            conversation.organization_id,
            {
                "event": "inbox_update",
                "conversation": {
                    "id": conversation.id,
                    "last_body": message.body,
                    "direction": message.direction,
                    "last_message_at_display": "just now",
                    "unread_count": conversation.unread_count,
                },
                "nav_unread_count": total_unread,
            },
        )
    except Exception as exc:
        logger.warning("Failed to broadcast outbound message %s: %s", message.id, exc)

    return message

