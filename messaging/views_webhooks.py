import logging
from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
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
from .models import PhoneNumber, Conversation, Message
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


def _verify_webhook_signature(request) -> bool:
    """
    Validates Twilio X-Twilio-Signature header against the full public URL.
    Respects PUBLIC_BASE_URL to handle ngrok / reverse proxy setups.
    """
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
    # POST parameters as plain dictionary
    post_params = dict(request.POST.items())
    return provider.validate_signature(target_url, post_params, signature)


@csrf_exempt
@require_POST
def twilio_inbound_sms(request):
    """
    Handles incoming customer SMS messages from Twilio:
    POST /webhooks/twilio/sms/
    - Validates signature.
    - Idempotent by MessageSid.
    - Routes to organization by To number.
    - Auto-creates contact (source='inbound', status='unknown').
    - Dispatches STOP/START/HELP compliance handling.
    - Returns empty TwiML <Response></Response>.
    """
    if not _verify_webhook_signature(request):
        logger.warning("Rejected Twilio inbound SMS: invalid signature.")
        return HttpResponseForbidden("Invalid Twilio signature.")

    from_phone = request.POST.get("From", "").strip()
    to_phone = request.POST.get("To", "").strip()
    body = request.POST.get("Body", "").strip()
    message_sid = request.POST.get("MessageSid", "").strip()
    num_media = int(request.POST.get("NumMedia", 0) or 0)

    if num_media > 0:
        media_note = f" [Received {num_media} media attachment(s)]"
        body = f"{body}{media_note}".strip()

    # Idempotency check: if MessageSid already processed, return 200 immediately
    if message_sid and Message.objects.filter(provider_message_sid=message_sid).exists():
        logger.info("Ignoring duplicate inbound SMS with MessageSid %s", message_sid)
        return HttpResponse("<Response></Response>", content_type="text/xml", status=200)

    try:
        normalized_to = normalize_phone(to_phone)
    except Exception:
        normalized_to = to_phone

    phone_number = PhoneNumber.objects.filter(e164=normalized_to, is_active=True).first()
    if not phone_number:
        logger.warning("Inbound SMS received for unassigned/inactive number: %s", to_phone)
        return HttpResponse("<Response></Response>", content_type="text/xml", status=200)

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
            provider="twilio",
            provider_message_sid=message_sid or None,
        )

        now = timezone.now()
        conversation.last_message_at = now
        conversation.last_inbound_at = now
        conversation.unread_count = F("unread_count") + 1
        if conversation.status == Conversation.Status.CLOSED:
            conversation.status = Conversation.Status.OPEN
        conversation.save(update_fields=["last_message_at", "last_inbound_at", "unread_count", "status"])

    # Compliance keyword classification (outside the message-create transaction)
    try:
        keyword = classify_keyword(body)
        if keyword == "STOP":
            record_consent(
                contact=contact,
                event_type=ConsentEvent.EventType.OPT_OUT,
                source=ConsentEvent.Source.KEYWORD,
                proof_text=f"Inbound STOP keyword (MessageSid: {message_sid}): '{body}'",
            )
        elif keyword == "START":
            record_consent(
                contact=contact,
                event_type=ConsentEvent.EventType.OPT_IN,
                source=ConsentEvent.Source.KEYWORD,
                proof_text=f"Inbound START keyword (MessageSid: {message_sid}): '{body}'",
            )
        # HELP / unknown / 'yes' do not alter consent ledger
    except Exception as exc:
        logger.exception("Error processing consent keyword on inbound message %s: %s", message_sid, exc)

    # Queue or debounce AI job if AI Assistant is configured and applicable
    try:
        from datetime import timedelta
        from messaging.models import AIAssistantConfig, AIJob

        config = getattr(org, "ai_config", None) or AIAssistantConfig.objects.filter(organization=org).first()
        keyword = classify_keyword(body)
        effective_mode = conversation.get_effective_ai_mode(config)

        # Only queue if mode is not off, not a compliance keyword, contact not opted out, conv not needs_human/closed
        if (
            effective_mode != AIAssistantConfig.Mode.OFF
            and keyword not in ["STOP", "START", "HELP"]
            and contact.sms_status != Contact.SMSStatus.OPTED_OUT
            and conversation.status not in [Conversation.Status.NEEDS_HUMAN, Conversation.Status.CLOSED]
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

    return HttpResponse("<Response></Response>", content_type="text/xml", status=200)


@csrf_exempt
@require_POST
def twilio_status_callback(request):
    """
    Handles Twilio message delivery status callbacks:
    POST /webhooks/twilio/status/
    - Validates signature.
    - Updates message status monotonically (queued < sending < sent < delivered/failed).
    - Never downgrades terminal statuses.
    """
    if not _verify_webhook_signature(request):
        logger.warning("Rejected Twilio status callback: invalid signature.")
        return HttpResponseForbidden("Invalid Twilio signature.")

    message_sid = request.POST.get("MessageSid", "").strip()
    raw_status = request.POST.get("MessageStatus", "").strip().lower()
    error_code = request.POST.get("ErrorCode", "").strip()

    logger.info(
        "Twilio status callback received: SID=%s, MessageStatus=%s, ErrorCode=%s",
        message_sid,
        raw_status,
        error_code or "None",
    )

    if not message_sid:
        return HttpResponse("Missing MessageSid", status=200)

    message = Message.objects.filter(provider_message_sid=message_sid).first()
    if not message:
        logger.info("Twilio status callback: no local message found for SID=%s", message_sid)
        return HttpResponse("OK", status=200)

    target_status = TWILIO_STATUS_MAP.get(raw_status)
    if not target_status:
        logger.warning("Twilio status callback: unrecognized status '%s' for SID=%s", raw_status, message_sid)
        return HttpResponse("OK", status=200)

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
    else:
        logger.info(
            "Ignored status downgrade for Message %s (SID: %s): current=%s, received=%s",
            message.id,
            message.provider_message_sid,
            message.status,
            target_status,
        )

    return HttpResponse("OK", status=200)

