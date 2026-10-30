from datetime import timedelta
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.http import JsonResponse, Http404
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.contrib import messages

from contacts.models import Contact
from .models import PhoneNumber, Conversation, Message
from .services import can_send_manual, send_message


def _setup_steps(org):
    """Checklist shown on the dashboard. Fields that don't exist yet count as not done."""
    reg_status = getattr(org, "registration_status", "")
    has_contacts = Contact.objects.filter(organization=org).exists() if org else False
    has_sent = Message.objects.filter(organization=org, direction=Message.Direction.OUTBOUND).exists() if org else False

    return [
        {"title": "Create your account", "desc": "Your company and login are ready.", "done": True, "url": ""},
        {"title": "Add business details", "desc": "Legal name, EIN, address and website. Carriers check these.", "done": bool(getattr(org, "ein", "")), "url": "#"},
        {"title": "Register with carriers", "desc": "Submit your brand and campaign so your texts can be delivered.", "done": reg_status == "approved", "url": "#"},
        {"title": "Import your contacts", "desc": "Upload a CSV and record how each person agreed to receive texts.", "done": has_contacts, "url": "/contacts/"},
        {"title": "Set up the AI Assistant", "desc": "Tell it about your business so it can answer customers.", "done": False, "url": "#"},
        {"title": "Send a test message", "desc": "Text your own phone to see the full flow.", "done": has_sent, "url": "/inbox/"},
    ]


@login_required
def dashboard(request):
    org = request.user.organization
    steps = _setup_steps(org)
    done = sum(1 for s in steps if s["done"])

    # Real counts now that models exist
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    stats = {
        "sent_today": Message.objects.filter(organization=org, direction=Message.Direction.OUTBOUND, created_at__gte=today_start).count() if org else 0,
        "replies": Message.objects.filter(organization=org, direction=Message.Direction.INBOUND, created_at__gte=today_start).count() if org else 0,
        "needs_human": Conversation.objects.filter(organization=org, status=Conversation.Status.NEEDS_HUMAN).count() if org else 0,
        "opt_outs": Contact.objects.filter(organization=org, sms_status=Contact.SMSStatus.OPTED_OUT).count() if org else 0,
    }

    context = {
        "steps": steps,
        "steps_done": done,
        "steps_total": len(steps),
        "percent": int(done / len(steps) * 100) if steps else 0,
        "setup_complete": done == len(steps),
        "stats": stats,
    }
    return render(request, "messaging/dashboard.html", context)


def _get_inbox_data(org, status_filter="open", q=""):
    """Helper to fetch conversations, counts, and active phone numbers for the two-pane inbox."""
    valid_statuses = [Conversation.Status.OPEN, Conversation.Status.NEEDS_HUMAN, Conversation.Status.CLOSED]
    conversations_qs = Conversation.objects.filter(organization=org).select_related("contact", "phone_number")
    
    if status_filter in valid_statuses:
        conversations_qs = conversations_qs.filter(status=status_filter)

    if q:
        from django.db.models import Q
        conversations_qs = conversations_qs.filter(
            Q(contact__phone__icontains=q) |
            Q(contact__first_name__icontains=q) |
            Q(contact__last_name__icontains=q) |
            Q(messages__body__icontains=q)
        ).distinct()

    conversations = list(conversations_qs.order_by("-last_message_at"))

    counts = {
        "open": Conversation.objects.filter(organization=org, status=Conversation.Status.OPEN).count(),
        "needs_human": Conversation.objects.filter(organization=org, status=Conversation.Status.NEEDS_HUMAN).count(),
        "closed": Conversation.objects.filter(organization=org, status=Conversation.Status.CLOSED).count(),
    }
    has_active_number = PhoneNumber.objects.filter(organization=org, is_active=True).exists()
    all_contacts = list(Contact.objects.filter(organization=org).order_by("first_name", "last_name", "phone")[:100])

    return conversations, counts, has_active_number, all_contacts


@login_required
def inbox(request):
    """
    Inbox view (two-pane WhatsApp style layout):
    Left pane lists all conversations, right pane shows selected chat or placeholder.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    status_filter = request.GET.get("status", "open").strip().lower()
    q = request.GET.get("q", "").strip()
    conversations, counts, has_active_number, all_contacts = _get_inbox_data(org, status_filter, q)

    context = {
        "conversations": conversations,
        "conversation": None,
        "messages": [],
        "status_filter": status_filter,
        "q": q,
        "counts": counts,
        "has_active_number": has_active_number,
        "all_contacts": all_contacts,
        "is_debug": getattr(settings, "DEBUG", False),
    }
    return render(request, "messaging/inbox.html", context)


@login_required
def conversation_detail(request, conversation_id):
    """
    Two-pane WhatsApp style conversation view:
    Left pane lists contacts/conversations with active selection highlighted.
    Right pane shows the active chat thread, delivery ticks, composer, and simulator.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    conversation = get_object_or_404(
        Conversation.objects.select_related("contact", "phone_number", "assigned_to"),
        id=conversation_id,
        organization=org,
    )

    # Mark conversation as read on open
    if conversation.unread_count > 0:
        conversation.unread_count = 0
        conversation.save(update_fields=["unread_count"])

    status_filter = request.GET.get("status", "").strip().lower()
    q = request.GET.get("q", "").strip()
    valid_statuses = [Conversation.Status.OPEN, Conversation.Status.NEEDS_HUMAN, Conversation.Status.CLOSED]
    if status_filter not in valid_statuses:
        status_filter = conversation.status

    conversations, counts, has_active_number, all_contacts = _get_inbox_data(org, status_filter, q)

    messages_qs = conversation.messages.select_related("sent_by").order_by("created_at")
    message_list = list(messages_qs)

    can_send, block_reason = can_send_manual(conversation.contact, conversation)

    context = {
        "conversations": conversations,
        "conversation": conversation,
        "messages": message_list,
        "can_send": can_send,
        "block_reason": block_reason,
        "status_filter": status_filter,
        "q": q,
        "counts": counts,
        "has_active_number": has_active_number,
        "all_contacts": all_contacts,
        "is_debug": getattr(settings, "DEBUG", False),
    }
    return render(request, "messaging/inbox.html", context)


@login_required
@require_POST
def send_conversation_message(request, conversation_id):
    """
    Sends an outbound SMS message in this conversation.
    Handles AJAX requests and standard form submissions.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    conversation = get_object_or_404(
        Conversation.objects.select_related("contact", "phone_number"),
        id=conversation_id,
        organization=org,
    )

    body = request.POST.get("body", "").strip()

    try:
        msg = send_message(conversation=conversation, body=body, user=request.user)

        is_ajax = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.content_type == "application/json"
        )
        if is_ajax:
            return JsonResponse({
                "status": "success",
                "message_id": msg.id,
                "delivery_status": msg.status,
                "sent_at": msg.created_at.strftime("%I:%M %p"),
                "body": msg.body,
            })

        messages.success(request, "Message sent.")
        return redirect("messaging:conversation_detail", conversation_id=conversation.id)

    except (ValidationError, PermissionDenied) as exc:
        err_msg = exc.message_dict.get("non_field_errors", [str(exc)])[0] if hasattr(exc, "message_dict") else str(exc)
        is_ajax = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.content_type == "application/json"
        )
        if is_ajax:
            return JsonResponse({"status": "error", "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("messaging:conversation_detail", conversation_id=conversation.id)


@login_required
def poll_messages(request, conversation_id):
    """
    Polling endpoint for fetching new messages in a conversation.
    Returns JSON list of messages created after `after_id`.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    conversation = get_object_or_404(Conversation, id=conversation_id, organization=org)

    after_id = request.GET.get("after_id", 0)
    try:
        after_id = int(after_id)
    except ValueError:
        after_id = 0

    new_messages = conversation.messages.filter(id__gt=after_id).select_related("sent_by").order_by("created_at")

    data = [
        {
            "id": m.id,
            "direction": m.direction,
            "sender_type": m.sender_type,
            "sent_by": m.sent_by.get_full_name() or m.sent_by.email if m.sent_by else "",
            "body": m.body,
            "status": m.status,
            "created_at": m.created_at.strftime("%I:%M %p"),
            "date_iso": m.created_at.isoformat(),
        }
        for m in new_messages
    ]

    return JsonResponse({"messages": data})


@login_required
def contact_message_redirect(request, contact_id):
    """
    'Message' button handler from Contacts:
    - Gets or creates a Conversation between the Contact and the Organization's active PhoneNumber.
    - If no active phone number exists, alerts the user to configure one.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    contact = get_object_or_404(Contact, id=contact_id, organization=org)

    phone_number = PhoneNumber.objects.filter(organization=org, is_active=True).first()
    if not phone_number:
        messages.error(
            request,
            "Your business does not have an active phone number configured yet. Please add an active phone number in Admin.",
        )
        return redirect("contacts:detail", contact_id=contact.id)

    conversation, _ = Conversation.objects.get_or_create(
        organization=org,
        contact=contact,
        phone_number=phone_number,
        defaults={"status": Conversation.Status.OPEN},
    )

    return redirect("messaging:conversation_detail", conversation_id=conversation.id)


@login_required
@require_POST
def update_conversation_status(request, conversation_id):
    """
    Updates status of conversation (open, needs_human, closed).
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    conversation = get_object_or_404(Conversation, id=conversation_id, organization=org)
    new_status = request.POST.get("status", "").strip().lower()

    if new_status in [Conversation.Status.OPEN, Conversation.Status.NEEDS_HUMAN, Conversation.Status.CLOSED]:
        conversation.status = new_status
        conversation.save(update_fields=["status"])
        messages.success(request, f"Conversation marked as {conversation.get_status_display()}.")

    return redirect("messaging:conversation_detail", conversation_id=conversation.id)


@login_required
@require_POST
def start_new_chat(request):
    """
    Direct 'New Chat' creation from within the Inbox modal:
    Accepts either an existing contact_id OR a new phone number (+ name).
    Finds/creates contact and conversation, returning redirect to open the chat.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    phone_number = PhoneNumber.objects.filter(organization=org, is_active=True).first()
    if not phone_number:
        return JsonResponse({
            "status": "error",
            "message": "Your business does not have an active phone number configured yet. Please add an active phone number in Admin.",
        }, status=400)

    contact_id = request.POST.get("contact_id", "").strip()
    phone = request.POST.get("phone", "").strip()
    first_name = request.POST.get("first_name", "").strip()
    last_name = request.POST.get("last_name", "").strip()

    if contact_id:
        contact = get_object_or_404(Contact, id=contact_id, organization=org)
    elif phone:
        from contacts.services import save_contact
        try:
            contact, _ = save_contact(
                organization=org,
                data={
                    "phone": phone,
                    "first_name": first_name,
                    "last_name": last_name,
                },
                source=Contact.Source.MANUAL,
            )
        except ValidationError as exc:
            msg = exc.message_dict.get("non_field_errors", [str(exc)])[0] if hasattr(exc, "message_dict") else str(exc)
            return JsonResponse({"status": "error", "message": str(msg)}, status=400)
    else:
        return JsonResponse({"status": "error", "message": "Please enter a phone number or select a contact."}, status=400)

    conversation, _ = Conversation.objects.get_or_create(
        organization=org,
        contact=contact,
        phone_number=phone_number,
        defaults={"status": Conversation.Status.OPEN},
    )

    from django.urls import reverse
    target_url = reverse("messaging:conversation_detail", kwargs={"conversation_id": conversation.id})

    is_ajax = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.content_type == "application/json"
    )
    if is_ajax:
        return JsonResponse({
            "status": "success",
            "conversation_id": conversation.id,
            "url": target_url,
        })

    return redirect("messaging:conversation_detail", conversation_id=conversation.id)

