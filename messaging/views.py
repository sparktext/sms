import json
from datetime import timedelta
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.db.models import Q, Count, OuterRef, Subquery, Sum
from django.http import JsonResponse, Http404, HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.contrib import messages

from contacts.models import Contact
from core.models import User
from campaigns.models import Campaign, CampaignTemplate
from campaigns.services import ensure_default_campaign_templates
from .models import PhoneNumber, Conversation, Message
from .services import can_send_manual, send_message
from .utils import format_chat_timestamp, get_org_status_counts

INITIAL_CHAT_LIMIT = 35


def _is_ajax_request(request):
    """Return True if the request expects a JSON or partial AJAX response."""
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.content_type == "application/json"
    )


def _with_last_msg_id(queryset, search_query=""):
    """Annotate conversations with latest (and optional search-match) message IDs."""
    latest_msg_subq = (
        Message.objects.filter(conversation=OuterRef("pk"))
        .order_by("-created_at", "-id")
        .values("id")[:1]
    )
    qs = queryset.annotate(_last_msg_id=Subquery(latest_msg_subq))
    if search_query:
        matching_msg_subq = (
            Message.objects.filter(conversation=OuterRef("pk"), body__icontains=search_query)
            .order_by("-created_at", "-id")
            .values("id")[:1]
        )
        qs = qs.annotate(_matching_msg_id=Subquery(matching_msg_subq))
    return qs


def _attach_latest_messages(conversations, attr_name="cached_last_msg"):
    """Bulk-hydrate each conversation's latest (and search-match) message in one query."""
    msg_ids = []
    for conv in conversations:
        last_id = getattr(conv, "_last_msg_id", None)
        match_id = getattr(conv, "_matching_msg_id", None)
        if last_id:
            msg_ids.append(last_id)
        if match_id:
            msg_ids.append(match_id)

    msgs_by_id = Message.objects.in_bulk(set(msg_ids)) if msg_ids else {}
    for conv in conversations:
        setattr(conv, attr_name, msgs_by_id.get(getattr(conv, "_last_msg_id", None)))
        if hasattr(conv, "_matching_msg_id"):
            conv.cached_matching_msg = msgs_by_id.get(getattr(conv, "_matching_msg_id", None))
    return conversations


def get_recent_thread_slice(conversation, limit=INITIAL_CHAT_LIMIT):
    """Fetch the latest `limit` messages in chronological order plus pagination flags."""
    if not conversation:
        return [], False, None
    rows = list(
        conversation.messages.select_related("sent_by")
        .order_by("-created_at", "-id")[: limit + 1]
    )
    has_older = len(rows) > limit
    if has_older:
        rows = rows[:limit]
    rows.reverse()
    oldest_id = rows[0].id if rows else None
    return rows, has_older, oldest_id


def serialize_chat_message(m):
    """Serialize a Message instance into JSON-ready chat payload fields."""
    local_dt = timezone.localtime(m.created_at) if timezone.is_aware(m.created_at) else m.created_at
    return {
        "id": m.id,
        "direction": m.direction,
        "sender_type": m.sender_type,
        "sent_by": (m.sent_by.get_full_name() or m.sent_by.email) if m.sent_by else "",
        "is_ai_assisted": bool(m.is_ai_assisted),
        "body": m.body,
        "status": m.status,
        "error_message": m.error_message or "",
        "created_at": format_chat_timestamp(m.created_at),
        "date_iso": m.created_at.isoformat(),
        "date_key": local_dt.strftime("%Y-%m-%d"),
        "date_label": local_dt.strftime("%b %d, %Y"),
    }


def _setup_steps(org):
    """Checklist shown on the dashboard. Fields that don't exist yet count as not done."""
    reg_status = getattr(org, "registration_status", "")
    has_contacts = Contact.objects.filter(organization=org).exists() if org else False
    has_sent = Message.objects.filter(organization=org, direction=Message.Direction.OUTBOUND).exists() if org else False
    has_ai = False
    if org:
        ai_cfg = getattr(org, "ai_config", None)
        if ai_cfg and (ai_cfg.mode != "off" or ai_cfg.knowledge.strip()):
            has_ai = True

    return [
        {"title": "Create your account", "desc": "Your company and login are ready.", "done": True, "url": ""},
        {"title": "Add business details", "desc": "Legal name, EIN, address and website. Carriers check these.", "done": bool(getattr(org, "ein", "")), "url": "/settings/profile/"},
        {"title": "Register with carriers", "desc": "Submit your brand and campaign so your texts can be delivered.", "done": reg_status == "approved", "url": "/compliance/"},
        {"title": "Import your contacts", "desc": "Upload a CSV and record how each person agreed to receive texts.", "done": has_contacts, "url": "/contacts/"},
        {"title": "Set up the AI Assistant", "desc": "Tell it about your business so it can answer customers.", "done": has_ai, "url": "/ai-assistant/"},
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

    recent_conversations = []
    if org:
        recent_qs = _with_last_msg_id(
            Conversation.objects.filter(organization=org).select_related("contact", "phone_number")
        )
        recent_conversations = list(recent_qs.order_by("-last_message_at")[:6])
        _attach_latest_messages(recent_conversations, attr_name="last_msg")

    context = {
        "steps": steps,
        "steps_done": done,
        "steps_total": len(steps),
        "percent": int(done / len(steps) * 100) if steps else 0,
        "setup_complete": done == len(steps),
        "stats": stats,
        "recent_conversations": recent_conversations,
    }
    return render(request, "messaging/dashboard.html", context)


INBOX_PAGE_SIZE = 30


def _get_inbox_data(
    org,
    status_filter="open",
    q="",
    user=None,
    assigned_filter="",
    offset=0,
    limit=INBOX_PAGE_SIZE,
    include_shell_data=True,
):
    """Fetch paginated inbox conversations, counts, and optional modal/quick-reply shell data."""
    valid_statuses = [Conversation.Status.OPEN, Conversation.Status.NEEDS_HUMAN, Conversation.Status.CLOSED]
    conversations_qs = Conversation.objects.filter(organization=org).select_related("contact", "phone_number", "assigned_to")

    if status_filter in valid_statuses:
        conversations_qs = conversations_qs.filter(status=status_filter)

    if assigned_filter == "mine" and user:
        conversations_qs = conversations_qs.filter(assigned_to=user)
    elif assigned_filter == "unassigned":
        conversations_qs = conversations_qs.filter(assigned_to__isnull=True)
    elif assigned_filter == "replied":
        conversations_qs = conversations_qs.filter(last_inbound_at__isnull=False)
    elif assigned_filter == "unread":
        conversations_qs = conversations_qs.filter(unread_count__gt=0)

    if q:
        conversations_qs = conversations_qs.filter(
            Q(contact__phone__icontains=q) |
            Q(contact__first_name__icontains=q) |
            Q(contact__last_name__icontains=q) |
            Q(messages__body__icontains=q)
        ).distinct()

    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or INBOX_PAGE_SIZE), 100))

    conversations_qs = _with_last_msg_id(conversations_qs, search_query=q).order_by("-last_message_at", "-id")
    rows = list(conversations_qs[safe_offset : safe_offset + safe_limit + 1])
    has_more = len(rows) > safe_limit
    conversations = rows[:safe_limit]
    _attach_latest_messages(conversations, attr_name="cached_last_msg")

    # Search match identification (Issue #5)
    if q:
        q_lower = q.lower()
        for conv in conversations:
            contact_text = f"{conv.contact.first_name} {conv.contact.last_name} {conv.contact.phone}".lower()
            if q_lower in contact_text:
                conv.search_match_type = "contact"
                conv.search_match_snippet = ""
            elif getattr(conv, "cached_matching_msg", None):
                conv.search_match_type = "message"
                conv.search_match_snippet = conv.cached_matching_msg.body

    result = {
        "conversations": conversations,
        "has_more_conversations": has_more,
        "next_conv_offset": safe_offset + len(conversations),
    }
    if not include_shell_data:
        return result

    counts = get_org_status_counts(org)
    active_phone_numbers = list(
        PhoneNumber.objects.filter(organization=org, is_active=True).order_by("-is_default", "label", "e164")
    )
    total_contacts_count = Contact.objects.filter(organization=org).count()
    all_contacts = list(Contact.objects.filter(organization=org).order_by("first_name", "last_name", "phone")[:50])

    ensure_default_campaign_templates(org)
    quick_replies_qs = CampaignTemplate.objects.filter(
        organization=org,
        category=CampaignTemplate.Category.QUICK_REPLY,
    ).order_by("id")
    quick_replies_data = [
        {
            "id": qr.id,
            "label": qr.name,
            "text": qr.body,
        }
        for qr in quick_replies_qs
    ]

    result.update({
        "counts": counts,
        "has_active_number": len(active_phone_numbers) > 0,
        "all_contacts": all_contacts,
        "total_contacts_count": total_contacts_count,
        "active_phone_numbers": active_phone_numbers,
        "quick_replies_json": json.dumps(quick_replies_data),
    })
    return result


@login_required
def inbox(request):
    """
    Inbox view (two-pane WhatsApp style layout):
    Left pane lists paginated conversations, right pane shows selected chat or placeholder.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    status_filter = request.GET.get("status", "open").strip().lower()
    assigned_filter = request.GET.get("assigned", "").strip().lower()
    q = request.GET.get("q", "").strip()
    inbox_data = _get_inbox_data(
        org, status_filter, q, user=request.user, assigned_filter=assigned_filter
    )

    context = {
        **inbox_data,
        "conversation": None,
        "chat_messages": [],
        "status_filter": status_filter,
        "assigned_filter": assigned_filter,
        "q": q,
        "is_debug": getattr(settings, "DEBUG", False),
    }
    return render(request, "messaging/inbox.html", context)


@login_required
def inbox_search(request):
    """
    Live asynchronous search and pagination endpoint:
    Searches contact names, phones, and full historical message content across the organization.
    Returns rendered HTML conversation item rows for instant replacement or appending in the left pane.
    """
    org = request.user.organization
    if not org:
        return HttpResponse("")

    status_filter = request.GET.get("status", "open").strip().lower()
    assigned_filter = request.GET.get("assigned", "").strip().lower()
    q = request.GET.get("q", "").strip()
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
    except ValueError:
        offset = 0

    inbox_data = _get_inbox_data(
        org,
        status_filter=status_filter,
        q=q,
        user=request.user,
        assigned_filter=assigned_filter,
        offset=offset,
        include_shell_data=False,
    )

    active_id = request.GET.get("active_id")
    active_conv = None
    if active_id:
        try:
            active_conv = Conversation.objects.filter(id=int(active_id), organization=org).first()
        except (ValueError, TypeError):
            pass

    context = {
        **inbox_data,
        "conversation": active_conv,
        "status_filter": status_filter,
        "assigned_filter": assigned_filter,
        "q": q,
        "request": request,
    }
    return render(request, "messaging/inbox_conversation_items.html", context)


def _get_org_unread_total(org):
    """Return total unread messages across open and needs_human conversations for an org."""
    return (
        Conversation.objects.filter(
            organization=org,
            status__in=[Conversation.Status.OPEN, Conversation.Status.NEEDS_HUMAN],
        ).aggregate(total=Sum("unread_count"))["total"]
        or 0
    )


def _mark_conversation_read(conversation, org, force_broadcast=False):
    """Reset a conversation's unread_count to 0 and broadcast the updated org total."""
    had_unread = conversation.unread_count > 0
    if had_unread:
        Conversation.objects.filter(id=conversation.id, unread_count__gt=0).update(unread_count=0)
        conversation.unread_count = 0

    total_unread = _get_org_unread_total(org)
    if had_unread or force_broadcast:
        try:
            from .ws_broadcast import broadcast_inbox_event
            last_msg = conversation.messages.order_by("-created_at", "-id").first()
            broadcast_inbox_event(
                org.id,
                {
                    "event": "inbox_update",
                    "conversation": {
                        "id": conversation.id,
                        "last_body": last_msg.body if last_msg else "",
                        "direction": last_msg.direction if last_msg else "",
                        "last_message_at_display": _timesince_short(conversation.last_message_at),
                        "unread_count": 0,
                    },
                    "nav_unread_count": total_unread,
                },
            )
        except Exception:
            pass
    return total_unread


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
    total_unread = _mark_conversation_read(conversation, org)

    status_filter = request.GET.get("status", "").strip().lower()
    assigned_filter = request.GET.get("assigned", "").strip().lower()
    q = request.GET.get("q", "").strip()
    valid_statuses = [Conversation.Status.OPEN, Conversation.Status.NEEDS_HUMAN, Conversation.Status.CLOSED]

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.GET.get("ajax") == "1"

    message_list, has_older_messages, oldest_msg_id = get_recent_thread_slice(conversation)
    can_send, block_reason = can_send_manual(conversation.contact, conversation)
    org_members = list(User.objects.filter(organization=org).order_by("first_name", "email"))

    context = {
        "conversation": conversation,
        "chat_messages": message_list,
        "has_older_messages": has_older_messages,
        "oldest_msg_id": oldest_msg_id or 0,
        "can_send": can_send,
        "block_reason": block_reason,
        "status_filter": status_filter or conversation.status,
        "assigned_filter": assigned_filter,
        "q": q,
        "org_members": org_members,
        "nav_unread_count": total_unread,
        "is_debug": getattr(settings, "DEBUG", False),
    }

    if is_ajax:
        return render(request, "messaging/inbox_chat_pane.html", context)

    # If an explicit status filter was requested and it differs from this conversation's status:
    if status_filter in valid_statuses and conversation.status != status_filter:
        matching_conv = Conversation.objects.filter(
            organization=org,
            status=status_filter,
        )
        if assigned_filter == "mine":
            matching_conv = matching_conv.filter(assigned_to=request.user)
        elif assigned_filter == "unassigned":
            matching_conv = matching_conv.filter(assigned_to__isnull=True)

        matching_conv = matching_conv.order_by("-last_message_at", "-created_at").first()
        assign_query = f"&assigned={assigned_filter}" if assigned_filter else ""
        if matching_conv:
            return redirect(f"{reverse('messaging:conversation_detail', kwargs={'conversation_id': matching_conv.id})}?status={status_filter}{assign_query}")
        else:
            return redirect(f"{reverse('messaging:inbox')}?status={status_filter}{assign_query}")

    if status_filter not in valid_statuses:
        status_filter = conversation.status

    inbox_data = _get_inbox_data(
        org, status_filter, q, user=request.user, assigned_filter=assigned_filter
    )
    context.update(inbox_data)
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

        # Automatic Human Takeover (GoHighLevel/Podium model):
        # When a staff agent manually replies, automatically pause AI for this conversation.
        ai_mode_changed = False
        if conversation.ai_mode != Conversation.AIMode.OFF:
            conversation.ai_mode = Conversation.AIMode.OFF
            conversation.save(update_fields=["ai_mode"])
            ai_mode_changed = True
            Message.objects.create(
                conversation=conversation,
                organization=org,
                direction=Message.Direction.OUTBOUND,
                sender_type=Message.SenderType.SYSTEM,
                body=f"Human agent {request.user.first_name or request.user.email} took over. AI paused for this chat.",
                status=Message.Status.DELIVERED,
            )

        is_ajax = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.content_type == "application/json"
        )
        if is_ajax:
            return JsonResponse({
                "status": "success",
                "message_id": msg.id,
                "delivery_status": msg.status,
                "sent_at": format_chat_timestamp(msg.created_at),
                "body": msg.body,
                "ai_mode": conversation.ai_mode,
                "ai_mode_changed": ai_mode_changed,
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
    Polling and history pagination endpoint for a conversation.
    Supports `?before_id=<id>&limit=60` (older messages) and `?after_id=<id>` (new messages).
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    conversation = get_object_or_404(Conversation, id=conversation_id, organization=org)

    before_id_raw = request.GET.get("before_id")
    if before_id_raw is not None:
        try:
            before_id = int(before_id_raw)
        except ValueError:
            before_id = 0

        try:
            limit = int(request.GET.get("limit", 60))
        except ValueError:
            limit = 60
        limit = max(10, min(limit, 100))

        anchor = conversation.messages.filter(id=before_id).values("created_at", "id").first()
        if anchor:
            older_qs = conversation.messages.filter(
                Q(created_at__lt=anchor["created_at"])
                | Q(created_at=anchor["created_at"], id__lt=anchor["id"])
            )
        elif before_id > 0:
            older_qs = conversation.messages.filter(id__lt=before_id)
        else:
            older_qs = conversation.messages.none()

        rows = list(older_qs.select_related("sent_by").order_by("-created_at", "-id")[: limit + 1])
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        rows.reverse()

        oldest_id = rows[0].id if rows else None
        return JsonResponse(
            {
                "messages": [serialize_chat_message(m) for m in rows],
                "has_more": has_more,
                "oldest_id": oldest_id,
            }
        )

    after_id = request.GET.get("after_id", 0)
    try:
        after_id = int(after_id)
    except ValueError:
        after_id = 0

    new_messages = (
        conversation.messages.filter(id__gt=after_id)
        .select_related("sent_by")
        .order_by("created_at", "id")
    )

    return JsonResponse({"messages": [serialize_chat_message(m) for m in new_messages]})


@login_required
@require_POST
def mark_conversation_read(request, conversation_id):
    """Mark an open conversation as read and broadcast the updated unread total."""
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    conversation = get_object_or_404(Conversation, id=conversation_id, organization=org)
    total_unread = _mark_conversation_read(conversation, org)
    return JsonResponse({
        "status": "success",
        "unread_count": 0,
        "nav_unread_count": total_unread,
    })


@login_required
@require_POST
def mark_conversation_unread(request, conversation_id):
    """Explicitly mark a conversation as unread for follow-up triage and broadcast updated count."""
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    conversation = get_object_or_404(Conversation, id=conversation_id, organization=org)
    if conversation.unread_count == 0:
        conversation.unread_count = 1
        conversation.save(update_fields=["unread_count"])

    total_unread = _get_org_unread_total(org)
    try:
        from .ws_broadcast import broadcast_inbox_event
        last_msg = conversation.messages.order_by("-created_at", "-id").first()
        broadcast_inbox_event(
            org.id,
            {
                "event": "inbox_update",
                "conversation": {
                    "id": conversation.id,
                    "last_body": last_msg.body if last_msg else "",
                    "direction": last_msg.direction if last_msg else "",
                    "last_message_at_display": _timesince_short(conversation.last_message_at),
                    "unread_count": conversation.unread_count,
                },
                "nav_unread_count": total_unread,
            },
        )
    except Exception:
        pass

    return JsonResponse({
        "status": "success",
        "unread_count": conversation.unread_count,
        "nav_unread_count": total_unread,
    })


@login_required
def search_inbox_contacts(request):
    """Org-scoped live contact search for the + New Chat modal."""
    org = request.user.organization
    if not org:
        return JsonResponse({"contacts": [], "total_count": 0})

    q = request.GET.get("q", "").strip()
    qs = Contact.objects.filter(organization=org)
    if q:
        qs = qs.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(phone__icontains=q)
            | Q(email__icontains=q)
        )
    contacts_list = qs.order_by("first_name", "last_name", "phone")[:30]

    data = []
    for c in contacts_list:
        full_name = f"{c.first_name} {c.last_name}".strip()
        initials = (
            (c.first_name[:1] or c.last_name[:1]).upper()
            if (c.first_name or c.last_name)
            else "#FF"
        )
        data.append({
            "id": c.id,
            "name": full_name or c.phone,
            "phone": c.phone,
            "initials": initials,
            "sms_status": c.sms_status,
        })

    return JsonResponse({
        "contacts": data,
        "total_count": Contact.objects.filter(organization=org).count(),
    })


@login_required
def global_quick_search(request):
    """Org-scoped quick-jump search across contacts, conversations, and campaigns."""
    org = request.user.organization
    q = request.GET.get("q", "").strip()
    if not org or not q:
        return JsonResponse({"contacts": [], "conversations": [], "campaigns": []})

    contacts_qs = (
        Contact.objects.filter(organization=org)
        .filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(phone__icontains=q)
            | Q(email__icontains=q)
        )
        .order_by("first_name", "last_name", "id")[:5]
    )
    contacts_data = [
        {
            "id": c.id,
            "title": (f"{c.first_name} {c.last_name}".strip() or c.phone),
            "subtitle": c.phone,
            "badge": c.get_sms_status_display(),
            "url": reverse("contacts:detail", kwargs={"contact_id": c.id}),
        }
        for c in contacts_qs
    ]

    convs_qs = (
        Conversation.objects.filter(organization=org)
        .select_related("contact")
        .filter(
            Q(contact__first_name__icontains=q)
            | Q(contact__last_name__icontains=q)
            | Q(contact__phone__icontains=q)
        )
        .order_by("-last_message_at", "-id")[:5]
    )
    convs_data = [
        {
            "id": conv.id,
            "title": (f"{conv.contact.first_name} {conv.contact.last_name}".strip() or conv.contact.phone),
            "subtitle": f"{conv.contact.phone} · {conv.get_status_display()}",
            "badge": conv.get_status_display(),
            "url": reverse("messaging:conversation_detail", kwargs={"conversation_id": conv.id}),
        }
        for conv in convs_qs
    ]

    campaigns_qs = (
        Campaign.objects.filter(organization=org, name__icontains=q)
        .order_by("-created_at", "-id")[:5]
    )
    campaigns_data = [
        {
            "id": camp.id,
            "title": camp.name,
            "subtitle": f"{camp.get_status_display()} · {camp.total_recipients} recipients",
            "badge": camp.get_status_display(),
            "url": reverse("campaigns:detail", kwargs={"campaign_id": camp.id}),
        }
        for camp in campaigns_qs
    ]

    return JsonResponse({
        "contacts": contacts_data,
        "conversations": convs_data,
        "campaigns": campaigns_data,
    })


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
        counts = get_org_status_counts(org)

        try:
            from messaging.ws_broadcast import broadcast_chat_event, broadcast_inbox_event
            broadcast_chat_event(
                conversation.id,
                {
                    "event": "status_change",
                    "conversation_id": conversation.id,
                    "status": new_status,
                    "counts": counts,
                },
            )
            broadcast_inbox_event(
                org.id,
                {
                    "event": "inbox_update",
                    "conversation": {
                        "id": conversation.id,
                        "status": new_status,
                    },
                    "counts": counts,
                },
            )
        except Exception as exc:
            logger.warning("Failed to broadcast conversation status update: %s", exc)

        if _is_ajax_request(request):
            return JsonResponse({"status": "success", "new_status": conversation.status, "counts": counts})

        messages.success(request, f"Conversation marked as {conversation.get_status_display()}.")
    elif _is_ajax_request(request):
        return JsonResponse({"status": "success", "new_status": conversation.status, "counts": get_org_status_counts(org)})

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

    phone_number = None
    sender_phone_number_id = request.POST.get("sender_phone_number_id", "").strip()
    if sender_phone_number_id:
        phone_number = PhoneNumber.objects.filter(
            id=sender_phone_number_id,
            organization=org,
            is_active=True,
        ).first()

    if not phone_number:
        phone_number = PhoneNumber.get_default_for_org(org)

    if not phone_number:
        return JsonResponse({
            "status": "error",
            "message": "Your business does not have an active phone number configured yet. Please add an active phone number in Settings.",
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



@login_required
def ai_assistant_ping(request):
    """
    Lightweight health check endpoint to verify whether the configured LLM backend
    (e.g., LM Studio at localhost:1235) is reachable.
    """
    import requests
    base_url = getattr(settings, "AI_BASE_URL", "http://localhost:1235/v1").rstrip("/")
    model = getattr(settings, "AI_MODEL", "google/gemma-4-12b-qat")
    try:
        resp = requests.get(f"{base_url}/models", timeout=1.5)
        if resp.status_code == 200:
            return JsonResponse({
                "status": "online",
                "base_url": base_url,
                "model": model,
            })
    except Exception as exc:
        return JsonResponse({
            "status": "offline",
            "base_url": base_url,
            "model": model,
            "error": str(exc),
        })
    return JsonResponse({"status": "offline", "base_url": base_url, "model": model})


def _get_or_create_ai_config(org):
    """Fetch or provision default AIAssistantConfig for an organization."""
    from .models import AIAssistantConfig

    config, _ = AIAssistantConfig.objects.get_or_create(
        organization=org,
        defaults={
            "business_name": org.name,
            "business_description": f"{org.name} provides professional services.",
            "mode": AIAssistantConfig.Mode.OFF,
            "tone": "friendly, professional, concise",
            "knowledge": "Hours: Monday-Friday 9am - 5pm.\nServices: Consultations, support, and services.\nLocation: Online / Nationwide.",
            "qualification_questions": [
                {"key": "service_type", "question": "What type of service are you looking for?", "required": True},
                {"key": "timeline", "question": "When are you hoping to get started?", "required": False},
            ],
            "never_say": "free guarantee\nrefund immediately\nwe promise 100%",
            "handoff_rules": "If the customer requests a phone call or mentions an emergency, hand off immediately.",
            "max_reply_chars": 160,
            "max_ai_replies_per_hour_per_contact": 6,
        },
    )
    return config


def _apply_ai_config_form_data(config, data, strict_questions=False):
    """Apply submitted AI settings form fields onto an AIAssistantConfig instance (without saving)."""
    from .models import AIAssistantConfig

    if "mode" in data:
        mode = data.get("mode", AIAssistantConfig.Mode.OFF).strip()
        if mode in AIAssistantConfig.Mode.values:
            config.mode = mode

    if "business_name" in data:
        config.business_name = data.get("business_name", "").strip()
    if "business_description" in data:
        config.business_description = data.get("business_description", "").strip()
    if "knowledge" in data:
        config.knowledge = data.get("knowledge", "").strip()
    if "tone" in data:
        config.tone = data.get("tone", "").strip()
    if "never_say" in data:
        config.never_say = data.get("never_say", "").strip()
    if "handoff_rules" in data:
        config.handoff_rules = data.get("handoff_rules", "").strip()

    if "max_reply_chars" in data:
        try:
            config.max_reply_chars = max(50, min(1600, int(data.get("max_reply_chars", 160))))
        except (ValueError, TypeError):
            config.max_reply_chars = 160

    if "max_ai_replies_per_hour_per_contact" in data:
        try:
            config.max_ai_replies_per_hour_per_contact = max(
                1, min(60, int(data.get("max_ai_replies_per_hour_per_contact", 6)))
            )
        except (ValueError, TypeError):
            config.max_ai_replies_per_hour_per_contact = 6

    raw_questions = data.get("qualification_questions_json", "").strip()
    if raw_questions:
        try:
            parsed_q = json.loads(raw_questions)
            if isinstance(parsed_q, list):
                clean_q = []
                for item in parsed_q:
                    if isinstance(item, dict) and item.get("key") and item.get("question"):
                        clean_q.append({
                            "key": str(item["key"]).strip().lower().replace(" ", "_"),
                            "question": str(item["question"]).strip(),
                            "required": bool(item.get("required", False)),
                        })
                config.qualification_questions = clean_q
        except Exception:
            if strict_questions:
                raise


@login_required
def ai_assistant_settings(request):
    """
    Per-organization AI Assistant settings page.
    Allows configuring AI mode, business knowledge, tone, negative rules,
    and interactive qualification questions.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    config = _get_or_create_ai_config(org)

    if request.method == "POST":
        try:
            _apply_ai_config_form_data(config, request.POST, strict_questions=True)
        except Exception as exc:
            messages.error(request, f"Could not parse qualification questions: {exc}")

        config.save()
        messages.success(request, "AI Assistant settings saved successfully.")
        return redirect("messaging:ai_assistant_settings")

    questions_json_str = json.dumps(config.qualification_questions or [], indent=2)

    context = {
        "config": config,
        "questions_json": questions_json_str,
    }
    return render(request, "messaging/ai_assistant_settings.html", context)


@login_required
@require_POST
def ai_assistant_sandbox_test(request):
    """Simulate an inbound customer SMS against the current (or unsaved form) AI Assistant settings."""
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    test_message = request.POST.get("test_message", "").strip()
    if not test_message:
        return JsonResponse({"status": "error", "error": "Please enter a sample customer message."}, status=400)

    from .models import AIAssistantConfig
    from .ai.pipeline import simulate_ai_reply

    saved_config = _get_or_create_ai_config(org)
    temp_config = AIAssistantConfig(
        organization=org,
        mode=saved_config.mode,
        business_name=saved_config.business_name,
        business_description=saved_config.business_description,
        knowledge=saved_config.knowledge,
        tone=saved_config.tone,
        never_say=saved_config.never_say,
        handoff_rules=saved_config.handoff_rules,
        max_reply_chars=saved_config.max_reply_chars,
        max_ai_replies_per_hour_per_contact=saved_config.max_ai_replies_per_hour_per_contact,
        qualification_questions=list(saved_config.qualification_questions or []),
    )
    _apply_ai_config_form_data(temp_config, request.POST, strict_questions=False)

    result = simulate_ai_reply(temp_config, test_message)
    return JsonResponse(result)


@login_required
@require_POST
def handle_ai_draft_action(request, conversation_id):
    """
    Handles actions on the AI Suggestion draft (Send, Discard).
    When sent, records that the message was AI-assisted and sent by the human user.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    conversation = get_object_or_404(Conversation, id=conversation_id, organization=org)
    action = request.POST.get("action", "").strip().lower()

    if action == "send":
        draft_body = request.POST.get("body", "").strip() or conversation.ai_draft_reply.strip()
        if not draft_body:
            return JsonResponse({"status": "error", "message": "Draft message is empty."}, status=400)

        try:
            msg = send_message(
                conversation=conversation,
                body=draft_body,
                user=request.user,
                sender_type=Message.SenderType.HUMAN,
                is_ai_assisted=True,
            )
            # Clear draft
            conversation.ai_draft_reply = ""
            conversation.save(update_fields=["ai_draft_reply"])

            if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.content_type == "application/json":
                return JsonResponse({
                    "status": "success",
                    "message_id": msg.id,
                    "body": msg.body,
                    "sent_at": format_chat_timestamp(msg.created_at),
                })

            messages.success(request, "AI draft sent.")
            return redirect("messaging:conversation_detail", conversation_id=conversation.id)

        except (ValidationError, PermissionDenied) as exc:
            err = str(exc)
            return JsonResponse({"status": "error", "message": err}, status=400)

    elif action == "discard":
        conversation.ai_draft_reply = ""
        conversation.save(update_fields=["ai_draft_reply"])

        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.content_type == "application/json":
            return JsonResponse({"status": "success"})

        messages.info(request, "AI draft discarded.")
        return redirect("messaging:conversation_detail", conversation_id=conversation.id)

    return JsonResponse({"status": "error", "message": f"Unknown action: {action}"}, status=400)


@login_required
@require_POST
def resume_ai_conversation(request, conversation_id):
    """
    Clears the needs_human handoff status and re-opens the conversation for AI replies.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    conversation = get_object_or_404(Conversation, id=conversation_id, organization=org)
    conversation.status = Conversation.Status.OPEN
    conversation.handoff_reason = ""
    conversation.save(update_fields=["status", "handoff_reason"])

    # Log system note
    sys_msg = Message.objects.create(
        conversation=conversation,
        organization=org,
        direction=Message.Direction.OUTBOUND,
        sender_type=Message.SenderType.SYSTEM,
        body=f"AI Assistant resumed by {request.user.first_name or request.user.email}.",
        status=Message.Status.DELIVERED,
    )

    counts = get_org_status_counts(org)

    try:
        from messaging.ws_broadcast import broadcast_chat_event, broadcast_inbox_event
        broadcast_chat_event(
            conversation.id,
            {
                "event": "status_change",
                "conversation_id": conversation.id,
                "status": "open",
                "counts": counts,
                "message": {
                    "id": sys_msg.id,
                    "body": sys_msg.body,
                    "direction": sys_msg.direction,
                    "sender_type": sys_msg.sender_type,
                    "created_at": format_chat_timestamp(sys_msg.created_at),
                    "status": sys_msg.status,
                },
            },
        )
        broadcast_inbox_event(
            org.id,
            {
                "event": "inbox_update",
                "conversation": {
                    "id": conversation.id,
                    "status": "open",
                    "last_body": sys_msg.body,
                    "direction": sys_msg.direction,
                    "last_message_at_display": "just now",
                },
                "counts": counts,
            },
        )
    except Exception as exc:
        logger.warning("Failed to broadcast resume AI status change: %s", exc)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.content_type == "application/json":
        return JsonResponse({"status": "success", "new_status": conversation.status, "counts": counts})

    messages.success(request, "Conversation marked Open and resumed with AI Assistant.")
    return redirect("messaging:conversation_detail", conversation_id=conversation.id)


@login_required
@require_POST
def update_conversation_ai_mode(request, conversation_id):
    """
    Updates the per-conversation AI mode (default, auto, suggest, off).
    Allows an agent to take over (turn off AI), switch to suggest-only drafts,
    or re-enable full auto sending for this specific conversation.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    conversation = get_object_or_404(Conversation, id=conversation_id, organization=org)
    new_mode = request.POST.get("ai_mode", "").strip().lower()

    if new_mode in Conversation.AIMode.values:
        conversation.ai_mode = new_mode
        conversation.save(update_fields=["ai_mode"])

        label = conversation.get_ai_mode_display()
        if new_mode == Conversation.AIMode.OFF:
            # When human takes over, log a small system note in the thread
            Message.objects.create(
                conversation=conversation,
                organization=org,
                direction=Message.Direction.OUTBOUND,
                sender_type=Message.SenderType.SYSTEM,
                body=f"Human agent {request.user.first_name or request.user.email} took over. AI paused for this chat.",
                status=Message.Status.DELIVERED,
            )

        if _is_ajax_request(request):
            return JsonResponse({
                "status": "success",
                "ai_mode": conversation.ai_mode,
                "label": label,
            })

        if new_mode == Conversation.AIMode.OFF:
            messages.info(request, "AI Assistant turned off for this chat. You have taken over.")
        elif new_mode == Conversation.AIMode.SUGGEST:
            messages.info(request, "Switched to Suggest Mode (Gemma will draft replies for your approval).")
        elif new_mode == Conversation.AIMode.AUTO:
            messages.success(request, "Switched to Auto Mode (AI will reply automatically).")
        else:
            messages.success(request, "AI mode set to Business Default.")

    return redirect("messaging:conversation_detail", conversation_id=conversation.id)


@login_required
def phone_numbers_settings(request):
    """
    Settings page for managing organization phone numbers, primary default sending line,
    labels, and messaging webhook endpoints.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    is_admin = request.user.role in [User.Role.OWNER, User.Role.ADMIN]

    if request.method == "POST":
        if not is_admin:
            messages.error(request, "Only Organization Owners and Admins can add or configure phone numbers.")
            return redirect("messaging:phone_numbers_settings")

        action = request.POST.get("action", "").strip()
        if action == "add_number":
            raw_phone = request.POST.get("phone", "").strip()
            label = request.POST.get("label", "").strip()
            is_default = request.POST.get("is_default") == "on"
            if raw_phone:
                from contacts.utils import normalize_phone
                try:
                    norm_phone = normalize_phone(raw_phone)
                    if PhoneNumber.objects.filter(e164=norm_phone).exists():
                        messages.error(request, f"Phone number {norm_phone} is already registered in the system.")
                    else:
                        num = PhoneNumber.objects.create(
                            organization=org,
                            e164=norm_phone,
                            label=label,
                            is_default=is_default,
                            is_active=True,
                        )
                        messages.success(request, f"Added phone number {norm_phone} ({label or 'No label'}).")
                except ValidationError as exc:
                    messages.error(request, f"Invalid phone number: {exc}")
            else:
                messages.error(request, "Phone number cannot be empty.")
            return redirect("messaging:phone_numbers_settings")

    phone_numbers = PhoneNumber.objects.filter(organization=org).order_by("-is_default", "-is_active", "-created_at")
    from .utils import get_public_base_url
    public_base = get_public_base_url() or request.build_absolute_uri("/").rstrip("/")
    active_provider = getattr(settings, "MESSAGING_PROVIDER", "telnyx").lower()

    context = {
        "org": org,
        "is_admin": is_admin,
        "phone_numbers": phone_numbers,
        "public_base": public_base,
        "active_provider": active_provider,
        "telnyx_phone": getattr(settings, "TELNYX_PHONE_NUMBER", ""),
        "twilio_phone": getattr(settings, "TWILIO_PHONE_NUMBER", ""),
    }
    return render(request, "messaging/phone_numbers_settings.html", context)


@login_required
@require_POST
def phone_number_action(request, phone_id):
    """
    Handles actions on an individual phone number:
    - make_default: Sets as primary default sending line
    - toggle_active: Enables/disables the phone number
    - update_label: Modifies the phone number label
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    if request.user.role not in [User.Role.OWNER, User.Role.ADMIN]:
        messages.error(request, "Only Organization Owners and Admins can modify phone numbers.")
        return redirect("messaging:phone_numbers_settings")

    phone_number = get_object_or_404(PhoneNumber, id=phone_id, organization=org)
    action = request.POST.get("action", "").strip()

    if action == "make_default":
        phone_number.is_default = True
        phone_number.is_active = True
        phone_number.save()
        messages.success(request, f"{phone_number.e164} is now the Primary Default sending line.")
    elif action == "toggle_active":
        if phone_number.is_default and phone_number.is_active:
            messages.error(request, "Cannot deactivate the Primary Default number. Please designate another number as Primary first.")
        else:
            phone_number.is_active = not phone_number.is_active
            phone_number.save(update_fields=["is_active"])
            status_text = "activated" if phone_number.is_active else "deactivated"
            messages.info(request, f"{phone_number.e164} has been {status_text}.")
    elif action == "update_label":
        new_label = request.POST.get("label", "").strip()
        phone_number.label = new_label
        phone_number.save(update_fields=["label"])
        messages.success(request, f"Updated label for {phone_number.e164} to '{new_label}'.")

    return redirect("messaging:phone_numbers_settings")


@login_required
@require_POST
def update_contact_stage(request, contact_id):
    """
    AJAX endpoint to update a contact's CRM stage from the Lead Dossier panel.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    contact = get_object_or_404(Contact, id=contact_id, organization=org)
    new_stage = request.POST.get("stage", "").strip()

    if not isinstance(contact.custom_fields, dict):
        contact.custom_fields = {}

    contact.custom_fields["stage"] = new_stage
    contact.save(update_fields=["custom_fields"])

    return JsonResponse({
        "status": "success",
        "stage": new_stage,
        "contact_id": contact.id,
    })


@login_required
@require_POST
def update_conversation_assigned_to(request, conversation_id):
    """
    AJAX endpoint to assign a conversation to a team member from the Lead Dossier panel.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    conversation = get_object_or_404(Conversation, id=conversation_id, organization=org)
    user_id = request.POST.get("assigned_to_id", "").strip()

    if user_id:
        assigned_user = get_object_or_404(User, id=user_id, organization=org)
        conversation.assigned_to = assigned_user
        assigned_name = assigned_user.first_name or assigned_user.email
    else:
        conversation.assigned_to = None
        assigned_name = "Unassigned"

    conversation.save(update_fields=["assigned_to"])

    try:
        from .ws_broadcast import broadcast_inbox_event, broadcast_chat_event
        broadcast_inbox_event(
            org.id,
            {
                "event": "conversation_assigned",
                "conversation_id": conversation.id,
                "assigned_to_id": conversation.assigned_to_id,
                "assigned_to_name": assigned_name,
            },
        )
        broadcast_chat_event(
            conversation.id,
            {
                "event": "assignment_change",
                "conversation_id": conversation.id,
                "assigned_to_id": conversation.assigned_to_id,
                "assigned_to_name": assigned_name,
            },
        )
    except Exception as exc:
        pass

    return JsonResponse({
        "status": "success",
        "assigned_to": assigned_name,
        "conversation_id": conversation.id,
    })


@login_required
def inbox_live_feed(request):
    """
    Lightweight polling endpoint: returns recent conversation activity for the
    left-sidebar list. The browser polls this every 5 seconds and updates
    conversation cards without a full page reload.

    Returns up to 30 conversations updated in the last 60 seconds as JSON,
    ordered newest-first.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"conversations": []})

    since = timezone.now() - timedelta(seconds=60)
    convs = (
        Conversation.objects
        .filter(organization=org, last_message_at__gte=since)
        .select_related("contact")
        .order_by("-last_message_at")[:30]
    )

    result = []
    for c in convs:
        contact = c.contact
        first = (contact.first_name or "").strip()
        last = (contact.last_name or "").strip()
        contact_name = f"{first} {last}".strip() or contact.phone

        initials = (first[:1] or last[:1] or "#").upper()

        last_msg = c.messages.order_by("-created_at").first()
        last_body = ""
        direction = ""
        if last_msg:
            last_body = last_msg.body or ""
            direction = last_msg.direction or ""

        result.append({
            "id": c.id,
            "contact_name": contact_name,
            "contact_initials": initials,
            "last_body": last_body[:120],
            "direction": direction,
            "last_message_at_epoch": int(c.last_message_at.timestamp()) if c.last_message_at else 0,
            "last_message_at_display": _timesince_short(c.last_message_at),
            "unread_count": c.unread_count or 0,
        })

    return JsonResponse({"conversations": result})


def _timesince_short(dt):
    """Returns a compact timesince string like '2m ago' or 'just now'."""
    if not dt:
        return ""
    delta = timezone.now() - dt
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return "just now"
    if total_seconds < 3600:
        return f"{total_seconds // 60}m ago"
    if total_seconds < 86400:
        return f"{total_seconds // 3600}h ago"
    return f"{total_seconds // 86400}d ago"
