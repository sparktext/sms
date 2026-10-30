import uuid
import logging
from django.conf import settings
from django.http import JsonResponse, Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.test import RequestFactory
from django.utils import timezone

from .models import Conversation
from .views_webhooks import twilio_inbound_sms

logger = logging.getLogger(__name__)


@login_required
@require_POST
def simulate_customer_reply(request, conversation_id):
    """
    DEV ONLY: Simulates an inbound SMS from the contact to your Twilio number.
    Executes the exact same twilio_inbound_sms webhook pipeline:
    - Creates inbound Message (sender_type='customer')
    - Handles STOP / START / HELP compliance keywords
    - Updates conversation unread_count and last_inbound_at
    - Enables the 24-hour conversational reply window

    To remove this simulator later, simply delete this file and its template.
    """
    if not getattr(settings, "DEBUG", False):
        raise Http404("Simulator is only available in DEBUG mode.")

    org = request.user.organization
    if not org:
        raise Http404("No organization found.")

    conversation = get_object_or_404(
        Conversation.objects.select_related("contact", "phone_number"),
        id=conversation_id,
        organization=org,
    )

    body = request.POST.get("body", "").strip()
    if not body:
        return HttpResponseBadRequest("Message body cannot be empty.")

    simulated_sid = f"SM_sim_{uuid.uuid4().hex[:24]}"

    # Construct the simulated Twilio inbound POST payload
    payload = {
        "From": conversation.contact.phone,
        "To": conversation.phone_number.e164,
        "Body": body,
        "MessageSid": simulated_sid,
        "NumMedia": "0",
    }

    # Use RequestFactory to dispatch directly into twilio_inbound_sms
    # Note: Setting TWILIO_VALIDATE_WEBHOOKS=False temporarily during this simulation call
    factory = RequestFactory()
    sim_request = factory.post("/webhooks/twilio/sms/", data=payload)
    
    # Bypass signature check specifically for the internal simulator call
    orig_validate = getattr(settings, "TWILIO_VALIDATE_WEBHOOKS", True)
    try:
        setattr(settings, "TWILIO_VALIDATE_WEBHOOKS", False)
        webhook_response = twilio_inbound_sms(sim_request)
    finally:
        setattr(settings, "TWILIO_VALIDATE_WEBHOOKS", orig_validate)

    logger.info(
        "Simulated inbound SMS from %s to %s (SID: %s, Text: '%s')",
        conversation.contact.phone,
        conversation.phone_number.e164,
        simulated_sid,
        body,
    )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accepts("application/json"):
        return JsonResponse({
            "status": "success",
            "message_sid": simulated_sid,
            "body": body,
            "from": conversation.contact.phone,
            "sent_at": timezone.now().strftime("%I:%M %p"),
        })

    return redirect("messaging:conversation_detail", conversation_id=conversation.id)

