import json
import logging
from datetime import timedelta
from typing import Optional, Dict, Any
from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from contacts.models import Contact
from contacts.services import save_contact
from contacts.utils import normalize_phone
from compliance.models import ConsentEvent
from compliance.services import classify_keyword, record_consent
from .models import PhoneNumber, Conversation, Message, AIAssistantConfig, AIJob
from .providers import get_sms_provider
from .utils import get_public_base_url

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

    provider = get_sms_provider()
    post_params = dict(request.POST.items())
    return provider.validate_signature(target_url, post_params, signature)


def _verify_telnyx_signature(request) -> bool:
    if not getattr(settings, "TELNYX_VALIDATE_WEBHOOKS", False):
        return True

    # Telnyx signature verification can be added here if public key is active
    return True


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
        from .ws_broadcast import broadcast_chat_event
        broadcast_chat_event(
            conversation.id,
            {
                "event": "new_message",
                "message": {
                    "id": message.id,
                    "body": message.body,
                    "direction": message.direction,
                    "sender_type": message.sender_type,
                    "created_at": message.created_at.strftime("%I:%M %p"),
                    "status": message.status,
                },
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
        elif keyword == "START":
            record_consent(
                contact=contact,
                event_type=ConsentEvent.EventType.OPT_IN,
                source=ConsentEvent.Source.KEYWORD,
                proof_text=f"Inbound START keyword ({provider_name} SID: {message_sid}): '{body}'",
            )
    except Exception as exc:
        logger.exception("Error processing consent keyword on inbound message %s: %s", message_sid, exc)

    # Queue or debounce AI job if AI Assistant is configured and applicable
    try:
        config = getattr(org, "ai_config", None) or AIAssistantConfig.objects.filter(organization=org).first()
        keyword = classify_keyword(body)
        effective_mode = conversation.get_effective_ai_mode(config)

        if (
            effective_mode != AIAssistantConfig.Mode.OFF
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


def _handle_status_update(
    message_sid: str,
    target_status: str,
    error_code: str = "",
) -> bool:
    """
    Monotonically advances a message's delivery status.
    Terminal statuses (delivered, failed, undelivered) are never downgraded.
    """
    if not message_sid:
        return False

    message = Message.objects.filter(provider_message_sid=message_sid).first()
    if not message:
        logger.info("Status callback: no local message found for SID=%s", message_sid)
        return False

    current_rank = STATUS_ORDER.get(message.status, 0)
    new_rank = STATUS_ORDER.get(target_status, 0)

    # Status only moves forward
    if new_rank >= current_rank:
        prev_status = message.status
        message.status = target_status
        update_fields = ["status"]

        if error_code:
            message.error_code = error_code
            update_fields.append("error_code")

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
                    },
                )
            except Exception as exc:
                logger.warning("Failed to broadcast status update %s: %s", message.id, exc)

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

    target_status = TWILIO_STATUS_MAP.get(raw_status)
    if target_status:
        _handle_status_update(message_sid, target_status, error_code)

    return HttpResponse("OK", status=200)


# ============================================================================
# TELNYX WEBHOOK ENDPOINTS
# ============================================================================

@csrf_exempt
@require_POST
def telnyx_inbound_sms(request):
    """
    Handles incoming customer SMS messages from Telnyx:
    POST /webhooks/telnyx/sms/
    Telnyx sends JSON payloads:
    event_type: "message.received"
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

    # Status delivery callbacks can also be delivered on the same webhook endpoint in Telnyx
    elif event_type in TELNYX_STATUS_MAP:
        message_sid = payload.get("id", "")
        target_status = TELNYX_STATUS_MAP.get(event_type, "sent")
        errors = payload.get("errors", [])
        err_code = str(errors[0].get("code", "")) if errors else ""
        _handle_status_update(message_sid, target_status, err_code)

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

    message_sid = payload.get("id", "")
    target_status = TELNYX_STATUS_MAP.get(event_type, "sent")
    errors = payload.get("errors", [])
    err_code = str(errors[0].get("code", "")) if errors else ""

    if message_sid:
        _handle_status_update(message_sid, target_status, err_code)

    return JsonResponse({"status": "ok"}, status=200)
