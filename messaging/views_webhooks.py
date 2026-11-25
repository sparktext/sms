import json
import logging
from datetime import timedelta
from typing import Optional, Dict, Any
from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from contacts.models import Contact
from contacts.services import save_contact
from contacts.utils import normalize_phone
from compliance.models import ConsentEvent
from compliance.services import classify_keyword, is_stop_phrase, record_consent
from .models import PhoneNumber, Conversation, Message, AIAssistantConfig, AIJob
from .providers import get_sms_provider
from .providers.telnyx_provider import verify_telnyx_webhook
from .providers.twilio_provider import TwilioProvider
from .providers.fake_provider import FakeProvider
from .utils import get_public_base_url, format_chat_timestamp

logger = logging.getLogger(__name__)

# Monotonic status progression ranking
STATUS_ORDER = {
    "queued": 1,
    "sending": 2,
    "sent": 3,
    "delivered": 4,
    "undelivered": 4,
    "failed": 4,
    "received": 4,
}

TWILIO_STATUS_MAP = {
    "queued": "queued",
    "accepted": "queued",
    "sending": "sending",
    "sent": "sent",
    "delivered": "delivered",
    "undelivered": "undelivered",
    "failed": "failed",
    "receiving": "queued",
    "received": "received",
}

TELNYX_STATUS_MAP = {
    "message.queued": "queued",
    "message.sending": "sending",
    "message.sent": "sent",
    "message.delivered": "delivered",
    "message.undelivered": "undelivered",
    "message.failed": "failed",
    "queued": "queued",
    "sending": "sending",
    "sent": "sent",
    "delivered": "delivered",
    "delivery_unconfirmed": "sent",
    "delivery_failed": "undelivered",
    "sending_failed": "failed",
    "undelivered": "undelivered",
    "failed": "failed",
}


def _verify_twilio_signature(request) -> bool:
    if not getattr(settings, "TWILIO_VALIDATE_WEBHOOKS", True):
        return True

    signature = request.headers.get("X-Twilio-Signature") or request.META.get("HTTP_X_TWILIO_SIGNATURE", "")
    if not signature:
        return False

    public_base = get_public_base_url()
    if public_base:
        target_url = f"{public_base}{request.path}"
        query = request.META.get("QUERY_STRING", "")
        if query:
            target_url = f"{target_url}?{query}"
    else:
        target_url = request.build_absolute_uri()

    # Validate Twilio webhooks with the Twilio validator whichever provider is active for sending,
    # so e.g. MESSAGING_PROVIDER=telnyx can never swap in another provider's logic. The only
    # exception is the in-memory FakeProvider used in tests/local dev (MESSAGING_PROVIDER=fake).
    active = get_sms_provider()
    validator = active if isinstance(active, FakeProvider) else TwilioProvider()
    post_params = dict(request.POST.items())
    return validator.validate_signature(target_url, post_params, signature)


def _verify_telnyx_signature(request) -> bool:
    """
    Verify the Telnyx Ed25519 webhook signature. Always enforced (there is no off-switch):
    an unsigned, forged, stale or unverifiable request is rejected, because these endpoints
    record STOP/START consent and create inbound messages.
    """
    return verify_telnyx_webhook(
        raw_body=request.body,
        signature_b64=request.headers.get("Telnyx-Signature-Ed25519", ""),
        timestamp=request.headers.get("Telnyx-Timestamp", ""),
    )


def _confirm_opt_out(conversation, org) -> None:
    """They opted out in their own words ("please stop texting me"). Carriers only auto-confirm the bare
    STOP keyword, so send the single confirmation text ourselves (no marketing, nothing after it)."""
    from .services import send_message
    config = getattr(org, "ai_config", None) or AIAssistantConfig.objects.filter(organization=org).first()
    business = ((getattr(config, "business_name", "") or org.name or "our") if config or org else "our").strip()
    try:
        send_message(conversation=conversation, sender_type=Message.SenderType.AI, opt_out_confirmation=True,
                     body=f"You're unsubscribed from {business} texts and won't get any more messages. "
                          f"Reply START to resubscribe.")
    except Exception as exc:
        logger.exception("Could not send the opt-out confirmation for conversation %s: %s", conversation.id, exc)


def _broadcast_send_permission(contact) -> None:
    """Push {can_send, block_reason} to every conversation window open for this contact."""
    try:
        from .services import can_send_manual
        from .ws_broadcast import broadcast_chat_event

        contact.refresh_from_db(fields=["sms_status"])
        for conv in Conversation.objects.filter(contact=contact):
            can_send, reason = can_send_manual(contact, conv)
            broadcast_chat_event(
                conv.id,
                {
                    "event": "send_permission",
                    "sms_status": contact.sms_status,
                    "can_send": bool(can_send),
                    "block_reason": str(reason or ""),
                },
            )
    except Exception as exc:
        logger.warning("Failed to broadcast send permission for contact %s: %s", getattr(contact, "id", None), exc)


def _handle_inbound_sms(
    from_phone: str,
    to_phone: str,
    body: str,
    message_sid: str,
    provider_name: str,
    num_media: int = 0,
) -> Optional[Message]:
    """
    Unified inbound SMS ingestion pipeline:
    - Normalizes phone numbers
    - Idempotent by message_sid
    - Routes to organization by To number
    - Finds or creates Contact (source='inbound')
    - Records inbound Message in active Conversation
    - Handles STOP / START compliance keywords
    - Debounces and triggers AI Assistant job if enabled
    """
    if num_media > 0:
        media_note = f" [Received {num_media} media attachment(s)]"
        body = f"{body}{media_note}".strip()

    # Idempotency check: if MessageSid already processed, return immediately
    if message_sid and Message.objects.filter(provider_message_sid=message_sid).exists():
        logger.info("Ignoring duplicate inbound SMS with MessageSid %s", message_sid)
        return Message.objects.filter(provider_message_sid=message_sid).first()

    try:
        normalized_to = normalize_phone(to_phone)
    except Exception:
        normalized_to = to_phone

    phone_number = PhoneNumber.objects.filter(e164=normalized_to, is_active=True).first()
    if not phone_number:
        logger.warning("Inbound SMS received for unassigned/inactive number: %s", to_phone)
        return None

    org = phone_number.organization

    try:
        normalized_from = normalize_phone(from_phone)
    except Exception:
        normalized_from = from_phone

    # Find or auto-create contact via save_contact
    contact = Contact.objects.filter(organization=org, phone=normalized_from).first()
    if not contact:
        contact, _ = save_contact(
            organization=org,
            data={"phone": normalized_from},
            source=Contact.Source.INBOUND,
        )

    # Process conversation and message creation atomically
    with transaction.atomic():
        conversation, _ = Conversation.objects.select_for_update().get_or_create(
            organization=org,
            contact=contact,
            phone_number=phone_number,
            defaults={"status": Conversation.Status.OPEN},
        )

        message = Message.objects.create(
            conversation=conversation,
            organization=org,
            direction=Message.Direction.INBOUND,
            sender_type=Message.SenderType.CUSTOMER,
            body=body,
            status=Message.Status.RECEIVED,
            provider=provider_name,
            provider_message_sid=message_sid or None,
        )

        now = timezone.now()
        conversation.last_message_at = now
        conversation.last_inbound_at = now
        conversation.unread_count = F("unread_count") + 1
        if conversation.status == Conversation.Status.CLOSED:
            conversation.status = Conversation.Status.OPEN
        conversation.save(update_fields=["last_message_at", "last_inbound_at", "unread_count", "status"])

    # Real-time WebSocket broadcast to open conversation windows
    try:
        from .ws_broadcast import broadcast_chat_event, broadcast_inbox_event
        from .utils import get_org_status_counts
        broadcast_chat_event(
            conversation.id,
            {
                "event": "new_message",
                "message": {
                    "id": message.id,
                    "body": message.body,
                    "direction": message.direction,
                    "sender_type": message.sender_type,
                    "created_at": format_chat_timestamp(message.created_at),
                    "status": message.status,
                },
            },
        )
        # Real-time WebSocket broadcast to sidebar inbox list and global toast notifications
        conversation.refresh_from_db(fields=["unread_count"])
        total_unread = (
            Conversation.objects.filter(
                organization=org,
                status__in=[Conversation.Status.OPEN, Conversation.Status.NEEDS_HUMAN],
            ).aggregate(total=Sum("unread_count"))["total"]
            or 0
        )
        contact_name = (contact.first_name + (" " + contact.last_name if contact.last_name else "")).strip() if (contact.first_name or contact.last_name) else contact.phone

        # Only notify (floating toast & chime) when conversation is in or needs a human agent.
        # Routine messages handled by AI update the inbox silently in real-time.
        toast_payload = None
        if conversation.status == Conversation.Status.NEEDS_HUMAN:
            assigned_name = (
                (conversation.assigned_to.get_full_name() or conversation.assigned_to.email)
                if conversation.assigned_to
                else None
            )
            custom_sound_url = org.custom_notification_sound.url if getattr(org, "custom_notification_sound", None) else None
            toast_payload = {
                "title": "Needs Human Attention",
                "body": f"{contact_name}: {message.body[:140]}",
                "conversation_id": conversation.id,
                "type": "needs_human",
                "status": "needs_human",
                "assigned_to_id": conversation.assigned_to_id,
                "assigned_to_name": assigned_name,
                "notification_sound": getattr(org, "notification_sound", "sales_flourish"),
                "custom_sound_url": custom_sound_url,
            }

        broadcast_inbox_event(
            org.id,
            {
                "event": "inbox_update",
                "conversation": {
                    "id": conversation.id,
                    "last_body": message.body,
                    "direction": message.direction,
                    "last_message_at_display": "just now",
                    "unread_count": conversation.unread_count,
                    "status": conversation.status,
                    "contact_name": contact_name,
                    "contact_phone": contact.phone,
                    "assigned_to_id": conversation.assigned_to_id,
                },
                "nav_unread_count": total_unread,
                "counts": get_org_status_counts(org),
                "toast": toast_payload,
            },
        )
    except Exception as exc:
        logger.warning("Failed to broadcast inbound message %s: %s", message.id, exc)

    # Compliance keyword classification (outside message transaction)
    try:
        keyword = classify_keyword(body)
        if keyword == "STOP":
            record_consent(
                contact=contact,
                event_type=ConsentEvent.EventType.OPT_OUT,
                source=ConsentEvent.Source.KEYWORD,
                proof_text=f"Inbound STOP keyword ({provider_name} SID: {message_sid}): '{body}'",
            )
            if is_stop_phrase(body):
                _confirm_opt_out(conversation, org)
        elif keyword == "START":
            record_consent(
                contact=contact,
                event_type=ConsentEvent.EventType.OPT_IN,
                source=ConsentEvent.Source.KEYWORD,
                proof_text=f"Inbound START keyword ({provider_name} SID: {message_sid}): '{body}'",
            )
    except Exception as exc:
        logger.exception("Error processing consent keyword on inbound message %s: %s", message_sid, exc)

    # Tell open chat windows whether replying is now allowed. STOP/START change consent, and any
    # inbound text opens the reply window for contacts without consent, so the "Sending Currently
    # Blocked" banner / composer must update live instead of only after a page refresh.
    _broadcast_send_permission(contact)

    # Queue or debounce AI job if AI Assistant is configured and applicable
    try:
        config = getattr(org, "ai_config", None) or AIAssistantConfig.objects.filter(organization=org).first()
        keyword = classify_keyword(body)
        effective_mode = conversation.get_effective_ai_mode(config)

        # Skip AI only if message is an echo of a recent outbound campaign broadcast
        ten_mins_ago = timezone.now() - timedelta(minutes=10)
        is_campaign_echo = Message.objects.filter(
            organization=org,
            sender_type=Message.SenderType.CAMPAIGN,
            body=body,
            created_at__gte=ten_mins_ago,
        ).exists()

        if (
            not is_campaign_echo
            and effective_mode != AIAssistantConfig.Mode.OFF
            and keyword not in ["STOP", "START", "HELP"]
            and contact.sms_status != Contact.SMSStatus.OPTED_OUT
            and conversation.status != Conversation.Status.CLOSED
        ):
            debounce_seconds = getattr(settings, "AI_DEBOUNCE_SECONDS", 4)
            run_after = timezone.now() + timedelta(seconds=debounce_seconds)

            with transaction.atomic():
                pending_job = (
                    AIJob.objects.select_for_update()
                    .filter(conversation=conversation, status=AIJob.Status.PENDING)
                    .first()
                )
                if pending_job:
                    pending_job.trigger_message = message
                    pending_job.run_after = run_after
                    pending_job.save(update_fields=["trigger_message", "run_after"])
                    logger.info("Debounced existing pending AIJob #%d for conv %d", pending_job.id, conversation.id)
                else:
                    new_job = AIJob.objects.create(
                        conversation=conversation,
                        trigger_message=message,
                        status=AIJob.Status.PENDING,
                        run_after=run_after,
                    )
                    logger.info("Created new AIJob #%d for conv %d (run_after=%s)", new_job.id, conversation.id, run_after)

    except Exception as exc:
        logger.exception("Failed to schedule AI job for message %s: %s", message_sid, exc)

    return message


def _extract_telnyx_status_update(event_type: str, payload: Dict[str, Any]):
    """
    Normalizes Telnyx v2 messaging status events (including message.sent and
    message.finalized where carrier delivery outcomes live inside payload.to[0].status).
    Returns (message_sid, target_status, error_code, error_message).
    """
    message_sid = str(payload.get("id") or "").strip()

    raw_to_status = ""
    to_list = payload.get("to")
    if isinstance(to_list, list) and to_list and isinstance(to_list[0], dict):
        raw_to_status = str(to_list[0].get("status") or "").strip().lower()

    errors = payload.get("errors")
    err_code = ""
    err_message = ""
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        err_code = str(errors[0].get("code") or "").strip()
        err_message = str(errors[0].get("detail") or errors[0].get("title") or "").strip()

    target_status = ""
    if raw_to_status and raw_to_status in TELNYX_STATUS_MAP:
        # Avoid letting a nested intermediate 'sent'/'queued' downgrade an explicit event_type like message.delivered
        mapped_to = TELNYX_STATUS_MAP[raw_to_status]
        mapped_evt = TELNYX_STATUS_MAP.get(event_type, "")
        if STATUS_ORDER.get(mapped_to, 0) >= STATUS_ORDER.get(mapped_evt, 0):
            target_status = mapped_to
        else:
            target_status = mapped_evt
    elif event_type in TELNYX_STATUS_MAP:
        target_status = TELNYX_STATUS_MAP[event_type]
    elif event_type == "message.finalized":
        fallback_status = str(payload.get("status") or "").strip().lower()
        target_status = TELNYX_STATUS_MAP.get(fallback_status) or ("failed" if err_code else "sent")

    return message_sid, target_status, err_code, err_message


def _handle_status_update(
    message_sid: str,
    target_status: str,
    error_code: str = "",
    error_message: str = "",
) -> bool:
    """
    Monotonically advances a message's delivery status.
    Terminal statuses (delivered, failed, undelivered) are never downgraded.
    """
    if not message_sid or not target_status:
        return False

    message = Message.objects.filter(provider_message_sid=message_sid).first()
    if not message:
        logger.info("Status callback: no local message found for SID=%s", message_sid)
        return False

    current_rank = STATUS_ORDER.get(message.status, 0)
    new_rank = STATUS_ORDER.get(target_status, 0)

    is_post_delivered_conflict = (
        message.status == Message.Status.DELIVERED
        and target_status != Message.Status.DELIVERED
    )

    # Status only moves forward
    if new_rank >= current_rank and not is_post_delivered_conflict:
        prev_status = message.status
        message.status = target_status
        update_fields = ["status"]

        if error_code:
            message.error_code = error_code[:32]
            update_fields.append("error_code")

        if error_message:
            message.error_message = error_message[:500]
            update_fields.append("error_message")

        if target_status == Message.Status.DELIVERED and not message.delivered_at:
            message.delivered_at = timezone.now()
            update_fields.append("delivered_at")

        message.save(update_fields=update_fields)
        logger.info(
            "Updated Message %s (SID: %s) status: %s -> %s",
            message.id,
            message.provider_message_sid,
            prev_status,
            target_status,
        )

        if message.conversation_id:
            try:
                from .ws_broadcast import broadcast_chat_event
                broadcast_chat_event(
                    message.conversation_id,
                    {
                        "event": "status_update",
                        "message_id": message.id,
                        "status": target_status,
                        "error_message": message.error_message or "",
                    },
                )
            except Exception as exc:
                logger.warning("Failed to broadcast status update %s: %s", message.id, exc)

            # If message belongs to a campaign, update delivery counts and broadcast to /campaigns/<id>/
            try:
                from campaigns.models import CampaignRecipient
                from campaigns.ws_broadcast import broadcast_campaign_progress
                recip = CampaignRecipient.objects.filter(message=message).select_related("campaign").first()
                if recip:
                    camp = recip.campaign
                    if target_status == Message.Status.DELIVERED and recip.status != CampaignRecipient.Status.DELIVERED:
                        recip.status = CampaignRecipient.Status.DELIVERED
                        recip.save(update_fields=["status"])
                        camp.delivered_count = CampaignRecipient.objects.filter(campaign=camp, status=CampaignRecipient.Status.DELIVERED).count()
                        camp.save(update_fields=["delivered_count"])
                        broadcast_campaign_progress(camp, [{
                            "id": recip.id,
                            "status": "delivered",
                            "status_display": "Delivered",
                        }])
                    elif target_status in [Message.Status.FAILED, Message.Status.UNDELIVERED] and recip.status != CampaignRecipient.Status.FAILED:
                        recip.status = CampaignRecipient.Status.FAILED
                        recip.error_message = error_message or error_code or "Carrier delivery failed"
                        recip.save(update_fields=["status", "error_message"])
                        camp.failed_count = CampaignRecipient.objects.filter(campaign=camp, status=CampaignRecipient.Status.FAILED).count()
                        camp.save(update_fields=["failed_count"])
                        broadcast_campaign_progress(camp, [{
                            "id": recip.id,
                            "status": "failed",
                            "status_display": "Failed",
                            "error_message": recip.error_message,
                        }])
            except Exception as camp_exc:
                logger.warning("Failed to update CampaignRecipient delivery status: %s", camp_exc)

        return True
    else:
        logger.info(
            "Ignored status downgrade for Message %s (SID: %s): current=%s, received=%s",
            message.id,
            message.provider_message_sid,
            message.status,
            target_status,
        )
        return False


# ============================================================================
# TWILIO WEBHOOK ENDPOINTS
# ============================================================================

@csrf_exempt
@require_POST
def twilio_inbound_sms(request):
    """
    Handles incoming customer SMS messages from Twilio:
    POST /webhooks/twilio/sms/
    """
    if not _verify_twilio_signature(request):
        logger.warning("Rejected Twilio inbound SMS: invalid signature.")
        return HttpResponseForbidden("Invalid Twilio signature.")

    from_phone = request.POST.get("From", "").strip()
    to_phone = request.POST.get("To", "").strip()
    body = request.POST.get("Body", "").strip()
    message_sid = request.POST.get("MessageSid", "").strip()
    num_media = int(request.POST.get("NumMedia", 0) or 0)

    _handle_inbound_sms(
        from_phone=from_phone,
        to_phone=to_phone,
        body=body,
        message_sid=message_sid,
        provider_name="twilio",
        num_media=num_media,
    )

    return HttpResponse("<Response></Response>", content_type="text/xml", status=200)


@csrf_exempt
@require_POST
def twilio_status_callback(request):
    """
    Handles Twilio message delivery status callbacks:
    POST /webhooks/twilio/status/
    """
    if not _verify_twilio_signature(request):
        logger.warning("Rejected Twilio status callback: invalid signature.")
        return HttpResponseForbidden("Invalid Twilio signature.")

    message_sid = request.POST.get("MessageSid", "").strip()
    raw_status = request.POST.get("MessageStatus", "").strip().lower()
    error_code = request.POST.get("ErrorCode", "").strip()
    error_message = request.POST.get("ErrorMessage", "").strip()

    target_status = TWILIO_STATUS_MAP.get(raw_status)
    if target_status:
        _handle_status_update(message_sid, target_status, error_code, error_message)

    return HttpResponse("OK", status=200)


# ============================================================================
# TELNYX WEBHOOK ENDPOINTS
# ============================================================================

@csrf_exempt
@require_POST
def telnyx_inbound_sms(request):
    """
    Handles incoming customer SMS messages and delivery callbacks from Telnyx:
    POST /webhooks/telnyx/sms/
    """
    if not _verify_telnyx_signature(request):
        logger.warning("Rejected Telnyx inbound SMS: invalid signature.")
        return HttpResponseForbidden("Invalid Telnyx signature.")

    try:
        body_data = json.loads(request.body.decode("utf-8"))
    except Exception as exc:
        logger.warning("Telnyx webhook: could not parse JSON body: %s", exc)
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)

    data = body_data.get("data", {})
    event_type = data.get("event_type", "")
    payload = data.get("payload", {})

    logger.info("Telnyx webhook received: event_type=%s, id=%s", event_type, payload.get("id"))

    # Inbound SMS
    if event_type == "message.received" or payload.get("direction") == "inbound":
        from_phone = payload.get("from", {}).get("phone_number", "")
        to_list = payload.get("to", [])
        to_phone = to_list[0].get("phone_number", "") if to_list else ""
        text = payload.get("text", "")
        message_sid = payload.get("id", "")
        media_list = payload.get("media", [])

        if from_phone and to_phone:
            _handle_inbound_sms(
                from_phone=from_phone,
                to_phone=to_phone,
                body=text,
                message_sid=message_sid,
                provider_name="telnyx",
                num_media=len(media_list) if isinstance(media_list, list) else 0,
            )

    # Status delivery callbacks (message.sent, message.finalized, etc.)
    elif event_type == "message.finalized" or event_type in TELNYX_STATUS_MAP:
        message_sid, target_status, err_code, err_msg = _extract_telnyx_status_update(event_type, payload)
        _handle_status_update(message_sid, target_status, err_code, err_msg)

    return JsonResponse({"status": "received"}, status=200)


@csrf_exempt
@require_POST
def telnyx_status_callback(request):
    """
    Handles Telnyx message delivery status callbacks:
    POST /webhooks/telnyx/status/
    """
    if not _verify_telnyx_signature(request):
        logger.warning("Rejected Telnyx status callback: invalid signature.")
        return HttpResponseForbidden("Invalid Telnyx signature.")

    try:
        body_data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)

    data = body_data.get("data", {})
    event_type = data.get("event_type", "")
    payload = data.get("payload", {})

    message_sid, target_status, err_code, err_msg = _extract_telnyx_status_update(event_type, payload)
    logger.info("Telnyx status callback: event_type=%s, id=%s -> %s", event_type, message_sid, target_status)

    if message_sid and target_status:
        _handle_status_update(message_sid, target_status, err_code, err_msg)

    return JsonResponse({"status": "ok"}, status=200)
