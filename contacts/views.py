import json
import logging
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Count, ProtectedError
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET, require_http_methods

from core.models import Organization
from .lead_dossier import extract_contact_lead_details, label_for
from .lead_editor import contact_lead_editor, apply_lead_edits
from .models import Contact, ContactField, ContactList, ImportBatch
from .services import (
    save_contact,
    update_contact,
    preview_csv,
    suggest_column_mappings,
    import_contacts_csv,
    ingest_lead_data,
    fetch_and_import_remote_leads,
)

logger = logging.getLogger(__name__)


def _is_ajax(request) -> bool:
    """
    Returns True only if the request explicitly asks for a JSON response
    (via X-Requested-With: XMLHttpRequest or Accept: application/json without text/html).
    Standard browser form submissions send Accept: text/html,...*/* and should receive HTML redirects.
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.headers.get("Accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    return False


@login_required
def export_contacts_csv(request):
    """
    Exports organization contacts as a CSV file.
    Respects active filters: query (q), status, and list.
    Includes phone, first_name, last_name, email, sms_status, stage, lists, created_at,
    and all registered custom fields.
    """
    import csv
    from django.utils import timezone

    org = request.user.organization
    if not org:
        return HttpResponse("No organization", status=400)

    contacts_qs = Contact.objects.filter(organization=org).prefetch_related("lists").order_by("-created_at")

    # Search query (supports single terms and full name "First Last")
    q = request.GET.get("q", "").strip()
    if q:
        from django.db.models import Value
        from django.db.models.functions import Concat
        contacts_qs = contacts_qs.annotate(
            full_name=Concat("first_name", Value(" "), "last_name")
        ).filter(
            Q(phone__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(full_name__icontains=q) |
            Q(email__icontains=q)
        )

    # Status filter
    status = request.GET.get("status", "").strip()
    if status in Contact.SMSStatus.values:
        contacts_qs = contacts_qs.filter(sms_status=status)

    # List filter
    list_id = request.GET.get("list", "").strip()
    if list_id:
        try:
            selected_list = ContactList.objects.get(id=list_id, organization=org)
            contacts_qs = contacts_qs.filter(lists=selected_list)
        except (ContactList.DoesNotExist, ValueError):
            pass

    # Approved custom fields for this org
    custom_field_keys = list(
        ContactField.objects.filter(organization=org).values_list("key", flat=True)
    )

    response = HttpResponse(content_type="text/csv")
    filename = f"sparktext_contacts_{timezone.now().strftime('%Y%m%d_%H%M%S')}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    header = [
        "Phone",
        "First Name",
        "Last Name",
        "Email",
        "SMS Status",
        "Stage",
        "Lists",
        "Created At (UTC)",
    ] + [f"Field: {k}" for k in custom_field_keys]
    writer.writerow(header)

    for c in contacts_qs.iterator(chunk_size=1000):
        c_fields = c.custom_fields if isinstance(c.custom_fields, dict) else {}
        stage = c_fields.get("stage", "")
        list_names = ", ".join(l.name for l in c.lists.all())
        row = [
            c.phone,
            c.first_name,
            c.last_name,
            c.email,
            c.get_sms_status_display(),
            stage,
            list_names,
            c.created_at.strftime("%Y-%m-%d %H:%M:%S") if c.created_at else "",
        ] + [c_fields.get(k, "") for k in custom_field_keys]
        writer.writerow(row)

    return response


@login_required
def contacts_list(request):
    """
    Main Contacts management page:
    - Lists organization's contacts with pagination
    - Filter by sms_status (opted_in, opted_out, unknown)
    - Search by phone, name, email
    - Provides ContactLists and ContactFields for modals/actions
    """
    org = request.user.organization
    if not org:
        return render(request, "contacts/no_organization.html")

    contacts_qs = Contact.objects.filter(organization=org).order_by("-created_at")

    # Search query (supports single terms and full name "First Last")
    q = request.GET.get("q", "").strip()
    if q:
        from django.db.models import Value
        from django.db.models.functions import Concat
        contacts_qs = contacts_qs.annotate(
            full_name=Concat("first_name", Value(" "), "last_name")
        ).filter(
            Q(phone__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(full_name__icontains=q) |
            Q(email__icontains=q)
        )

    # Status filter
    status = request.GET.get("status", "").strip()
    if status in Contact.SMSStatus.values:
        contacts_qs = contacts_qs.filter(sms_status=status)

    # List filter
    list_id = request.GET.get("list", "").strip()
    selected_list = None
    if list_id:
        try:
            selected_list = ContactList.objects.get(id=list_id, organization=org)
            contacts_qs = contacts_qs.filter(lists=selected_list)
        except (ContactList.DoesNotExist, ValueError):
            pass

    # Status counts for filter pills (aggregated in 1 single query)
    status_group = dict(
        Contact.objects.filter(organization=org)
        .values("sms_status")
        .annotate(c=Count("id"))
        .values_list("sms_status", "c")
    )
    status_counts = {
        "all": sum(status_group.values()),
        "opted_in": status_group.get(Contact.SMSStatus.OPTED_IN, 0),
        "opted_out": status_group.get(Contact.SMSStatus.OPTED_OUT, 0),
        "unknown": status_group.get(Contact.SMSStatus.UNKNOWN, 0),
    }

    paginator = Paginator(contacts_qs, 25)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    contact_lists = (
        ContactList.objects.filter(organization=org)
        .annotate(contact_count=Count("contacts"))
        .order_by("name")
    )
    approved_fields = list(ContactField.objects.filter(organization=org).order_by("label"))

    # Discover any custom field keys present in page contacts that might not be in ContactField
    known_keys = {f.key for f in approved_fields}
    extra_keys = set()
    for c in page_obj:
        if isinstance(c.custom_fields, dict):
            for k in c.custom_fields.keys():
                if k not in known_keys:
                    extra_keys.add(k)

    class VirtualField:
        def __init__(self, key):
            self.key = key
            self.label = key.replace("_", " ").title()

    all_display_fields = list(approved_fields) + [VirtualField(k) for k in sorted(extra_keys)]

    # Attach custom field values mapping to each contact for direct column rendering
    for c in page_obj:
        cf = c.custom_fields if isinstance(c.custom_fields, dict) else {}
        c.custom_values_list = [
            {"key": f.key, "label": f.label, "value": cf.get(f.key, "")}
            for f in all_display_fields
        ]

    context = {
        "org": org,
        "page_obj": page_obj,
        "q": q,
        "status": status,
        "selected_list": selected_list,
        "status_counts": status_counts,
        "contact_lists": contact_lists,
        "contact_fields": all_display_fields,
    }
    if request.GET.get("partial") == "1":
        return render(request, "contacts/partials/contacts_table.html", context)
    return render(request, "contacts/contacts_list.html", context)


@login_required
@require_POST
def add_contact(request):
    """
    Handles manual single contact creation via AJAX modal or standard POST.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization associated with this account."}, status=400)

    phone = request.POST.get("phone", "").strip()
    first_name = request.POST.get("first_name", "").strip()
    last_name = request.POST.get("last_name", "").strip()
    email = request.POST.get("email", "").strip()
    source = (request.POST.get("source") or request.POST.get("vendor") or "").strip()
    consent = request.POST.get("consent") in ("1", "true", "on", True)
    contact_list_id = request.POST.get("contact_list_id", "").strip()

    if not source:
        err_msg = "Source is compulsory. Please specify a lead source."
        if _is_ajax(request):
            return JsonResponse({"status": "error", "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("contacts:list")

    # Process custom fields
    custom_fields = {}
    for key, value in request.POST.items():
        if key.startswith("custom_field_"):
            field_key = key.replace("custom_field_", "", 1)
            if value.strip():
                custom_fields[field_key] = value.strip()

    data = {
        "phone": phone,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "source": source,
        "custom_fields": custom_fields,
    }

    try:
        contact, created = save_contact(
            organization=org,
            data=data,
            source=Contact.Source.MANUAL,
        )

        if consent:
            from compliance.services import record_consent
            from compliance.models import ConsentEvent
            record_consent(
                contact=contact,
                event_type=ConsentEvent.EventType.OPT_IN,
                source=ConsentEvent.Source.MANUAL,
                proof_text=f"Express consent registered manually upon creation (Source: {source})",
            )

        # Attach to contact list if selected
        if contact_list_id:
            try:
                cl = ContactList.objects.get(id=contact_list_id, organization=org)
                cl.contacts.add(contact)
            except ContactList.DoesNotExist:
                pass

        if _is_ajax(request):
            return JsonResponse({
                "status": "success",
                "created": created,
                "phone": contact.phone,
                "first_name": contact.first_name,
                "last_name": contact.last_name,
            })
        messages.success(request, f"Contact {contact.phone} saved successfully.")
        return redirect("contacts:list")

    except ValidationError as ve:
        err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
        if _is_ajax(request):
            return JsonResponse({"status": "error", "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("contacts:list")


@login_required
@require_POST
def import_preview(request):
    """
    CSV Step 1: Takes uploaded CSV file, decodes it, returns column headers,
    sample rows, and auto-suggested mappings.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return JsonResponse({"status": "error", "message": "Please select a CSV or Excel file."}, status=400)

    try:
        raw_bytes = uploaded_file.read()
        preview = preview_csv(raw_bytes, sample_size=5, filename=uploaded_file.name)

        if not preview["headers"]:
            return JsonResponse({"status": "error", "message": "The uploaded file appears to be empty."}, status=400)

        suggestions = suggest_column_mappings(org, preview["headers"])
        approved_fields = list(ContactField.objects.filter(organization=org).values("key", "label"))

        return JsonResponse({
            "status": "success",
            "headers": preview["headers"],
            "rows": preview["rows"],
            "duplicate_headers": preview["duplicate_headers"],
            "warning": preview["warning"],
            "suggestions": suggestions,
            "approved_fields": approved_fields,
        })
    except ValidationError as ve:
        err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
        return JsonResponse({"status": "error", "message": err_msg}, status=400)
    except Exception as exc:
        return JsonResponse({"status": "error", "message": f"Could not read file: {str(exc)}"}, status=400)


@login_required
@require_POST
def import_process(request):
    """
    CSV Step 3: Takes file and user's finalized column_mapping decisions,
    runs import_contacts_csv, and returns detailed results.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return JsonResponse({"status": "error", "message": "Missing file."}, status=400)

    raw_mapping = request.POST.get("mapping", "{}")
    try:
        mapping = json.loads(raw_mapping)
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid mapping format."}, status=400)

    contact_list_id = request.POST.get("contact_list_id", "").strip()
    target_list = None
    if contact_list_id:
        try:
            target_list = ContactList.objects.get(id=contact_list_id, organization=org)
        except ContactList.DoesNotExist:
            pass

    consent_attested = request.POST.get("consent_attested", "").lower() in ("true", "1", "yes", "on")
    attestation_text = request.POST.get("attestation_text", "").strip()
    default_source = (request.POST.get("default_source") or request.POST.get("default_vendor") or "").strip() or "Direct"

    try:
        report = import_contacts_csv(
            organization=org,
            file_or_content=uploaded_file.read(),
            column_mapping=mapping,
            contact_list=target_list,
            default_source=default_source,
            consent_attestation=consent_attested,
            attestation_text=attestation_text,
            filename=uploaded_file.name,
        )
        return JsonResponse({
            "status": "success",
            "report": report,
        })
    except ValidationError as ve:
        err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
        return JsonResponse({"status": "error", "message": err_msg}, status=400)
    except Exception as exc:
        return JsonResponse({"status": "error", "message": f"Import failed: {str(exc)}"}, status=400)


# =========================================================================
# Contact Detail, Edit, and Delete Views
# =========================================================================

@login_required
def contact_detail(request, contact_id):
    """
    Detailed profile view for a single Contact:
    - View and edit all contact fields (name, phone, email, source, custom fields)
    - View lists the contact belongs to, with option to manage list assignments
    - Full SMS conversation history + live inline reply capability
    - Complete timeline of consent events from the append-only ledger (with exact proof text)
    """
    from messaging.models import PhoneNumber
    from messaging.services import can_send_manual

    org = request.user.organization
    if not org:
        return render(request, "contacts/no_organization.html")

    contact = get_object_or_404(Contact, id=contact_id, organization=org)
    consent_events = contact.consent_events.all().order_by("-created_at")
    all_lists = ContactList.objects.filter(organization=org).order_by("name")
    contact_list_ids = set(contact.lists.values_list("id", flat=True))
    all_fields = list(ContactField.objects.filter(organization=org).order_by("label"))
    # Filter out internal raw JSON fields (like detail) from standard text inputs
    all_fields = [f for f in all_fields if f.key not in ("detail", "lead_data", "payload", "json_data")]
    current_values = contact.custom_fields if isinstance(contact.custom_fields, dict) else {}
    for f in all_fields:
        # "current_insurer" -> "Current Insurer" when the label is just the raw key
        f.display_label = label_for(f.key) if f.label.strip() == f.key else f.label
        value = current_values.get(f.key)
        f.current_value = "" if value is None else value

    lead_dossier = extract_contact_lead_details(contact)
    lead_editor = contact_lead_editor(contact)

    # Fetch conversations for this contact (newest first)
    conversations = list(
        contact.conversations.select_related("phone_number", "assigned_to", "last_campaign").order_by("-last_message_at")
    )
    active_conv = None
    conv_id = request.GET.get("conv", "").strip()
    if conv_id and conv_id.isdigit():
        for c in conversations:
            if str(c.id) == conv_id:
                active_conv = c
                break
    if not active_conv and conversations:
        active_conv = conversations[0]

    from messaging.models import Message
    from messaging.views import get_recent_thread_slice

    chat_messages = []
    has_older_messages = False
    oldest_msg_id = None
    total_msg_count = 0
    non_sys_total = 0
    inbound_count = 0
    outbound_count = 0
    ai_count = 0
    campaign_count = 0
    human_count = 0
    if active_conv:
        chat_messages, has_older_messages, oldest_msg_id = get_recent_thread_slice(active_conv)
        msg_stats = active_conv.messages.aggregate(
            total_all=Count("id"),
            non_sys=Count("id", filter=~Q(sender_type=Message.SenderType.SYSTEM)),
            inbound=Count(
                "id",
                filter=Q(direction=Message.Direction.INBOUND) & ~Q(sender_type=Message.SenderType.SYSTEM),
            ),
            outbound=Count(
                "id",
                filter=Q(direction=Message.Direction.OUTBOUND) & ~Q(sender_type=Message.SenderType.SYSTEM),
            ),
            ai_count=Count("id", filter=Q(direction=Message.Direction.OUTBOUND, sender_type=Message.SenderType.AI)),
            campaign_count=Count("id", filter=Q(direction=Message.Direction.OUTBOUND, sender_type=Message.SenderType.CAMPAIGN)),
            human_count=Count("id", filter=Q(direction=Message.Direction.OUTBOUND, sender_type=Message.SenderType.HUMAN)),
        )
        total_msg_count = msg_stats["total_all"] or 0
        non_sys_total = msg_stats["non_sys"] or 0
        inbound_count = msg_stats["inbound"] or 0
        outbound_count = msg_stats["outbound"] or 0
        ai_count = msg_stats["ai_count"] or 0
        campaign_count = msg_stats["campaign_count"] or 0
        human_count = msg_stats["human_count"] or 0

    # Build instant structured conversation summary for right-hand summary panel
    qual_answers = (
        active_conv.qualification_answers
        if active_conv and isinstance(active_conv.qualification_answers, dict)
        else {}
    )
    qualification_items = [
        {"label": str(k).replace("_", " ").title(), "value": str(v)}
        for k, v in qual_answers.items()
        if v not in (None, "")
    ]
    conv_summary = {
        "total": non_sys_total,
        "inbound": inbound_count,
        "outbound": outbound_count,
        "ai_count": ai_count,
        "campaign_count": campaign_count,
        "human_count": human_count,
        "qualification_items": qualification_items,
        "stage": (contact.custom_fields or {}).get("stage", "") if isinstance(contact.custom_fields, dict) else "",
    }

    active_phones = list(
        PhoneNumber.objects.filter(organization=org, is_active=True).order_by("-is_default", "e164")
    )

    from core.models import User
    org_members = list(
        User.objects.filter(organization=org, is_active=True).order_by("first_name", "email")
    )
    standard_stages = ["New Lead", "Contacted", "Qualified", "Not Qualified", "Quote Sent", "Won", "Lost"]

    if not active_phones and not active_conv:
        can_send = False
        block_reason = "No active business phone number is configured for your organization yet."
    else:
        can_send, block_reason = can_send_manual(contact, active_conv)

    context = {
        "org": org,
        "contact": contact,
        "consent_events": consent_events,
        "all_lists": all_lists,
        "contact_list_ids": contact_list_ids,
        "all_fields": all_fields,
        "conversations": conversations,
        "active_conv": active_conv,
        "chat_messages": chat_messages,
        "total_msg_count": total_msg_count,
        "has_older_messages": has_older_messages,
        "oldest_msg_id": oldest_msg_id or 0,
        "conv_summary": conv_summary,
        "active_phones": active_phones,
        "org_members": org_members,
        "standard_stages": standard_stages,
        "lead_dossier": lead_dossier,
        "lead_editor": lead_editor,
        "can_send": can_send,
        "block_reason": block_reason,
    }
    return render(request, "contacts/contact_detail.html", context)


@login_required
@require_POST
def send_contact_message(request, contact_id):
    """
    Sends a manual outbound SMS directly from the Contact Detail page.
    - Resolves or creates a Conversation row cleanly.
    - Enforces TCPA compliance guards via send_message().
    - Performs automatic Human Takeover (pauses AI) if active.
    - Supports AJAX requests with full message JSON response.
    """
    from django.core.exceptions import PermissionDenied
    from messaging.models import Conversation, Message, PhoneNumber
    from messaging.services import send_message
    from messaging.ws_broadcast import broadcast_chat_event, broadcast_inbox_event
    from django.db.models import Sum

    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    contact = get_object_or_404(Contact, id=contact_id, organization=org)
    body = request.POST.get("body", "").strip()
    conv_id = request.POST.get("conversation_id", "").strip()
    phone_number_id = request.POST.get("phone_number_id", "").strip()

    is_ajax = (
        _is_ajax(request)
        or request.content_type == "application/json"
    )

    conversation = None
    if conv_id and conv_id.isdigit():
        conversation = (
            Conversation.objects.filter(id=int(conv_id), contact=contact, organization=org)
            .select_related("phone_number", "contact")
            .first()
        )

    if not conversation:
        phone_num = None
        if phone_number_id and phone_number_id.isdigit():
            phone_num = PhoneNumber.objects.filter(id=int(phone_number_id), organization=org, is_active=True).first()
        if not phone_num:
            phone_num = (
                PhoneNumber.objects.filter(organization=org, is_active=True, is_default=True).first()
                or PhoneNumber.objects.filter(organization=org, is_active=True).first()
            )
        if not phone_num:
            err = "No active business phone number is configured yet. Add one in Settings first."
            if is_ajax:
                return JsonResponse({"status": "error", "message": err}, status=400)
            messages.error(request, err)
            return redirect("contacts:detail", contact_id=contact.id)

        conversation, _ = Conversation.objects.get_or_create(
            organization=org,
            contact=contact,
            phone_number=phone_num,
            defaults={"status": Conversation.Status.OPEN},
        )

    try:
        msg = send_message(conversation=conversation, body=body, user=request.user)

        # Clear unread count since the agent just replied
        fields_to_update = []
        if conversation.unread_count > 0:
            conversation.unread_count = 0
            fields_to_update.append("unread_count")

        # Automatic Human Takeover: Pause AI when human rep replies manually
        sys_msg_data = None
        ai_mode_changed = False
        if conversation.ai_mode != Conversation.AIMode.OFF:
            conversation.ai_mode = Conversation.AIMode.OFF
            fields_to_update.append("ai_mode")
            ai_mode_changed = True

        if fields_to_update:
            conversation.save(update_fields=fields_to_update)

        if ai_mode_changed:
            from messaging.utils import format_chat_timestamp
            sys_msg = Message.objects.create(
                conversation=conversation,
                organization=org,
                direction=Message.Direction.OUTBOUND,
                sender_type=Message.SenderType.SYSTEM,
                body=f"Human agent {request.user.first_name or request.user.email} took over. AI paused for this chat.",
                status=Message.Status.DELIVERED,
            )
            sys_msg_data = {
                "id": sys_msg.id,
                "body": sys_msg.body,
                "direction": sys_msg.direction,
                "sender_type": sys_msg.sender_type,
                "created_at": format_chat_timestamp(sys_msg.created_at),
                "status": sys_msg.status,
            }
            try:
                broadcast_chat_event(
                    conversation.id,
                    {"event": "new_message", "message": sys_msg_data},
                )
            except Exception:
                pass

        # Broadcast updated unread count if cleared
        if "unread_count" in fields_to_update:
            try:
                total_unread = (
                    Conversation.objects.filter(
                        organization_id=org.id,
                        status__in=[Conversation.Status.OPEN, Conversation.Status.NEEDS_HUMAN],
                    ).aggregate(total=Sum("unread_count"))["total"]
                    or 0
                )
                broadcast_inbox_event(
                    org.id,
                    {
                        "event": "inbox_update",
                        "conversation": {"id": conversation.id, "unread_count": 0},
                        "nav_unread_count": total_unread,
                    },
                )
            except Exception:
                pass

        if is_ajax:
            from messaging.utils import format_chat_timestamp
            return JsonResponse({
                "status": "success",
                "conversation_id": conversation.id,
                "phone_e164": conversation.phone_number.e164,
                "message": {
                    "id": msg.id,
                    "body": msg.body,
                    "direction": msg.direction,
                    "sender_type": msg.sender_type,
                    "sent_by": request.user.first_name or request.user.email,
                    "status": msg.status,
                    "created_at": format_chat_timestamp(msg.sent_at or msg.created_at),
                },
                "system_message": sys_msg_data,
                "ai_mode": conversation.ai_mode,
                "ai_mode_changed": ai_mode_changed,
            })

        messages.success(request, "Message sent.")
        return redirect("contacts:detail", contact_id=contact.id)

    except (ValidationError, PermissionDenied) as exc:
        err_msg = exc.message_dict.get("non_field_errors", [str(exc)])[0] if hasattr(exc, "message_dict") else str(exc)
        if is_ajax:
            return JsonResponse({"status": "error", "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("contacts:detail", contact_id=contact.id)


@login_required
@require_POST
def summarize_contact_conversation(request, contact_id):
    """
    Generates a concise AI summary of the contact's SMS conversation history using Gemma.
    Falls back cleanly to a structured extractive summary if the local LLM endpoint is offline.
    """
    from messaging.models import Conversation, Message
    from messaging.ai import get_ai_client

    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    contact = get_object_or_404(Contact, id=contact_id, organization=org)
    conv_id = request.POST.get("conversation_id", "").strip()

    conv_qs = contact.conversations.filter(organization=org)
    if conv_id and conv_id.isdigit():
        conv = conv_qs.filter(id=int(conv_id)).first()
    else:
        conv = conv_qs.order_by("-last_message_at").first()

    if not conv:
        return JsonResponse({
            "status": "success",
            "summary": "No conversation started with this contact yet.",
            "source": "instant",
        })

    msgs = list(
        conv.messages.exclude(sender_type=Message.SenderType.SYSTEM)
        .order_by("-created_at", "-id")[:30]
    )
    msgs.reverse()

    if not msgs:
        return JsonResponse({
            "status": "success",
            "summary": "No SMS messages have been exchanged in this thread yet.",
            "source": "instant",
        })

    transcript_lines = []
    for m in msgs:
        speaker = contact.first_name or "Customer" if m.direction == Message.Direction.INBOUND else (
            "AI" if m.sender_type == Message.SenderType.AI else (
                "Campaign" if m.sender_type == Message.SenderType.CAMPAIGN else "Agent"
            )
        )
        transcript_lines.append(f"{speaker}: {m.body}")
    transcript = "\n".join(transcript_lines)

    qual_info = ""
    if isinstance(conv.qualification_answers, dict) and conv.qualification_answers:
        qual_pairs = [f"{k}: {v}" for k, v in conv.qualification_answers.items() if v]
        if qual_pairs:
            qual_info = "\nCollected Details: " + ", ".join(qual_pairs)

    system_prompt = (
        f"You are a concise CRM assistant for {org.name}. Summarize this SMS conversation "
        "in 2 to 3 clear bullet points covering:\n"
        "- Customer intent or main inquiry\n"
        "- Key details shared by the customer\n"
        "- Recommended next step for the sales rep\n"
        "Be brief, factual, and direct. Do not invent facts."
    )
    user_prompt = f"SMS Transcript:\n{transcript}{qual_info}"

    try:
        client = get_ai_client()
        resp = client.generate(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            schema=None,
        )
        raw_text = (resp.text or "").strip()
        # Handle JSON-formatted responses cleanly (e.g. FakeAIClient default in tests)
        if raw_text.startswith("{"):
            try:
                parsed = json.loads(raw_text)
                raw_text = parsed.get("summary") or parsed.get("reply") or raw_text
            except Exception:
                pass
        if raw_text:
            return JsonResponse({"status": "success", "summary": raw_text, "source": "ai"})
    except Exception as exc:
        logger.debug("AI summary fallback triggered for contact %s: %s", contact.id, exc)

    # Instant deterministic extractive fallback
    inbound = [m for m in msgs if m.direction == Message.Direction.INBOUND]
    outbound = [m for m in msgs if m.direction == Message.Direction.OUTBOUND]
    parts = [f"• {len(msgs)} total message(s) ({len(inbound)} from contact, {len(outbound)} sent)."]
    if inbound:
        last_in = inbound[-1].body.strip()
        parts.append(f"• Latest customer message: \"{last_in[:120]}{'...' if len(last_in) > 120 else ''}\"")
    else:
        parts.append("• Awaiting customer's first reply.")
    if conv.handoff_reason:
        parts.append(f"• Attention note: {conv.handoff_reason}")
    elif outbound:
        last_out = outbound[-1].body.strip()
        parts.append(f"• Last outreach: \"{last_out[:100]}{'...' if len(last_out) > 100 else ''}\"")

    return JsonResponse({
        "status": "success",
        "summary": "\n".join(parts),
        "source": "instant",
    })


@login_required
@require_POST
def edit_contact(request, contact_id):
    """
    Updates an existing contact's details and custom fields without duplicating.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    contact = get_object_or_404(Contact, id=contact_id, organization=org)
    contact_custom_fields_before = dict(contact.custom_fields or {}) if isinstance(contact.custom_fields, dict) else {}

    phone = request.POST.get("phone", contact.phone).strip()
    first_name = request.POST.get("first_name", "").strip()
    last_name = request.POST.get("last_name", "").strip()
    email = request.POST.get("email", "").strip()
    source = (request.POST.get("source") or request.POST.get("vendor", contact.source)).strip()

    if ("source" in request.POST or "vendor" in request.POST) and not source:
        err_msg = "Source is compulsory. Please specify a lead source."
        if _is_ajax(request):
            return JsonResponse({"status": "error", "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("contacts:detail", contact_id=contact.id)

    # Process custom fields
    custom_fields = {}
    for key, value in request.POST.items():
        if key.startswith("custom_field_"):
            field_key = key.replace("custom_field_", "", 1)
            if value.strip():
                custom_fields[field_key] = value.strip()

    data = {
        "phone": phone,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "source": source,
        "custom_fields": custom_fields,
    }

    try:
        updated = update_contact(contact=contact, data=data)
        # The form only edits the fields it shows: keep everything else the contact carries
        # (lead-form JSON - hidden from the form even when it's an approved field -, stage,
        # qualification notes...), which saving used to wipe. A shown field left empty is removed.
        on_form = {k[len("custom_field_"):] for k in request.POST if k.startswith("custom_field_")}
        previous = contact_custom_fields_before if isinstance(contact_custom_fields_before, dict) else {}
        kept = {k: v for k, v in previous.items() if k not in on_form}
        if kept:
            updated.custom_fields = {**kept, **(updated.custom_fields or {})}
            updated.save(update_fields=["custom_fields"])
        # Edited lead-form values: only the ones that changed are written back into the lead JSON
        lead_edits = [(k, v) for k, v in request.POST.items() if k.startswith("lead::")]
        if lead_edits:
            new_fields, n_changed = apply_lead_edits(dict(updated.custom_fields or {}), lead_edits)
            if n_changed:
                updated.custom_fields = new_fields
                updated.save(update_fields=["custom_fields"])

        # Update lists if provided in POST
        list_ids = request.POST.getlist("lists")
        if list_ids or "lists_submitted" in request.POST:
            valid_lists = ContactList.objects.filter(id__in=list_ids, organization=org)
            updated.lists.set(valid_lists)

        if _is_ajax(request):
            return JsonResponse({
                "status": "success",
                "phone": updated.phone,
                "first_name": updated.first_name,
                "last_name": updated.last_name,
            })
        messages.success(request, "Contact updated successfully.")
        return redirect("contacts:detail", contact_id=updated.id)

    except ValidationError as ve:
        err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
        if _is_ajax(request):
            return JsonResponse({"status": "error", "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("contacts:detail", contact_id=contact.id)
    except Exception as exc:
        if _is_ajax(request):
            return JsonResponse({"status": "error", "message": str(exc)}, status=400)
        messages.error(request, str(exc))
        return redirect("contacts:detail", contact_id=contact.id)


@login_required
@require_POST
def delete_contact(request, contact_id):
    """
    Permanently deletes a contact.
    """
    org = request.user.organization
    if not org:
        if _is_ajax(request):
            return JsonResponse({"status": "error", "message": "No organization."}, status=400)
        messages.error(request, "No organization.")
        return redirect("contacts:list")

    contact = get_object_or_404(Contact, id=contact_id, organization=org)
    phone = contact.phone
    try:
        contact.delete()
    except ProtectedError:
        msg = f"Cannot delete contact {phone} because referenced records are protected."
        if _is_ajax(request):
            return JsonResponse({"status": "error", "message": msg}, status=400)
        messages.error(request, msg)
        return redirect("contacts:list")

    if _is_ajax(request):
        return JsonResponse({"status": "success", "message": f"Contact {phone} deleted."})
    messages.success(request, f"Contact {phone} deleted.")
    return redirect("contacts:list")


@login_required
@require_POST
def contacts_bulk_action(request):
    """
    Handles bulk operations on selected contacts:
    - add_to_list: assign selected contacts to a ContactList
    - remove_from_list: unassign selected contacts from a ContactList
    - delete: bulk delete selected contacts
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    action = request.POST.get("action", "").strip()
    list_id = request.POST.get("list_id", "").strip()
    contact_ids_raw = request.POST.getlist("contact_ids")
    if not contact_ids_raw:
        # Check if single JSON array passed
        try:
            contact_ids_raw = json.loads(request.POST.get("contact_ids_json", "[]"))
        except json.JSONDecodeError:
            contact_ids_raw = []

    if not contact_ids_raw:
        return JsonResponse({"status": "error", "message": "No contacts selected."}, status=400)

    contacts_qs = Contact.objects.filter(id__in=contact_ids_raw, organization=org)
    count = contacts_qs.count()

    if action == "add_to_list":
        if not list_id:
            return JsonResponse({"status": "error", "message": "Please choose a target list."}, status=400)
        target_list = get_object_or_404(ContactList, id=list_id, organization=org)
        target_list.contacts.add(*contacts_qs)
        msg = f"Added {count} contact(s) to '{target_list.name}'."

    elif action == "remove_from_list":
        if not list_id:
            return JsonResponse({"status": "error", "message": "Please choose a target list."}, status=400)
        target_list = get_object_or_404(ContactList, id=list_id, organization=org)
        target_list.contacts.remove(*contacts_qs)
        msg = f"Removed {count} contact(s) from '{target_list.name}'."

    elif action == "delete":
        try:
            contacts_qs.delete()
            msg = f"Deleted {count} contact(s)."
        except ProtectedError:
            msg = "Some selected contacts could not be deleted because protected records reference them."
            if _is_ajax(request):
                return JsonResponse({"status": "error", "message": msg}, status=400)
            messages.error(request, msg)
            return redirect("contacts:list")

    else:
        return JsonResponse({"status": "error", "message": f"Unsupported action '{action}'."}, status=400)

    if _is_ajax(request):
        return JsonResponse({"status": "success", "message": msg, "count": count})
    messages.success(request, msg)
    return redirect("contacts:list")


# =========================================================================
# Contact Lists Management Views
# =========================================================================

@login_required
def lists_index(request):
    """
    Overview page of all contact lists/segments for the organization.
    """
    org = request.user.organization
    if not org:
        return render(request, "contacts/no_organization.html")

    lists = list(
        ContactList.objects.filter(organization=org)
        .annotate(
            contact_count=Count("contacts", distinct=True),
            opted_in_count=Count(
                "contacts",
                filter=Q(contacts__sms_status=Contact.SMSStatus.OPTED_IN),
                distinct=True,
            ),
        )
        .order_by("name")
    )

    all_contacts = Contact.objects.filter(organization=org).order_by("first_name", "last_name", "phone")

    # Fetch all list memberships in a single query for modal status badges
    memberships = {cl.id: [] for cl in lists}
    if lists:
        for cl_id, c_id in ContactList.contacts.through.objects.filter(
            contactlist_id__in=list(memberships.keys())
        ).values_list("contactlist_id", "contact_id"):
            memberships[cl_id].append(c_id)

    total_memberships = sum(cl.contact_count for cl in lists)
    total_opted_in = sum(cl.opted_in_count for cl in lists)

    context = {
        "org": org,
        "lists": lists,
        "total_lists": len(lists),
        "total_memberships": total_memberships,
        "total_opted_in": total_opted_in,
        "all_contacts": all_contacts,
        "memberships_json": json.dumps(memberships),
    }
    return render(request, "contacts/lists_index.html", context)


@login_required
@require_POST
def add_contacts_to_list(request, list_id):
    """
    Directly adds selected existing contacts or a newly created contact into a ContactList.
    """
    org = request.user.organization
    is_json = _is_ajax(request)

    if not org:
        if is_json:
            return JsonResponse({"status": "error", "message": "No organization."}, status=400)
        messages.error(request, "No organization associated with this account.")
        return redirect("contacts:lists")

    cl = get_object_or_404(ContactList, id=list_id, organization=org)
    added_count = 0

    # Option A: Quick-add a new contact
    new_phone = request.POST.get("new_phone", "").strip()
    if new_phone:
        first_name = request.POST.get("new_first_name", "").strip()
        last_name = request.POST.get("new_last_name", "").strip()
        try:
            contact, created = save_contact(
                organization=org,
                data={
                    "phone": new_phone,
                    "first_name": first_name,
                    "last_name": last_name,
                    "sms_status": Contact.SMSStatus.OPTED_IN,
                },
            )
            cl.contacts.add(contact)
            added_count += 1
        except ValidationError as ve:
            err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
            if is_json:
                return JsonResponse({"status": "error", "message": err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect("contacts:lists")

    # Option B: Add existing selected contacts
    contact_ids = request.POST.getlist("contact_ids")
    if not contact_ids:
        raw_json = request.POST.get("contact_ids_json", "")
        if raw_json:
            try:
                contact_ids = json.loads(raw_json)
            except json.JSONDecodeError:
                contact_ids = []

    if contact_ids:
        valid_contacts = Contact.objects.filter(id__in=contact_ids, organization=org)
        cl.contacts.add(*valid_contacts)
        added_count += valid_contacts.count()

    msg = f"Added {added_count} contact(s) to '{cl.name}'."
    if is_json:
        return JsonResponse({"status": "success", "message": msg, "count": added_count})

    messages.success(request, msg)
    return redirect("contacts:lists")


@login_required
@require_POST
def create_list(request):
    """
    Create a new ContactList for the organization.
    """
    org = request.user.organization
    is_json = _is_ajax(request)

    if not org:
        if is_json:
            return JsonResponse({"status": "error", "message": "No organization."}, status=400)
        messages.error(request, "No organization associated with this account.")
        return redirect("contacts:lists")

    name = request.POST.get("name", "").strip()
    if not name:
        if is_json:
            return JsonResponse({"status": "error", "message": "List name cannot be blank."}, status=400)
        messages.error(request, "List name cannot be blank.")
        return redirect("contacts:lists")

    max_len = ContactList._meta.get_field("name").max_length
    if len(name) > max_len:
        if is_json:
            return JsonResponse({"status": "error", "message": f"List name cannot exceed {max_len} characters."}, status=400)
        messages.error(request, f"List name cannot exceed {max_len} characters.")
        return redirect("contacts:lists")

    if ContactList.objects.filter(organization=org, name__iexact=name).exists():
        if is_json:
            return JsonResponse({"status": "error", "message": f"A list named '{name}' already exists."}, status=400)
        messages.error(request, f"A list named '{name}' already exists.")
        return redirect("contacts:lists")

    cl = ContactList.objects.create(organization=org, name=name)

    if is_json:
        return JsonResponse({"status": "success", "id": cl.id, "name": cl.name})
    
    messages.success(request, f"Contact list '{cl.name}' created successfully.")
    return redirect("contacts:lists")


@login_required
@require_POST
def edit_list(request, list_id):
    """
    Rename an existing ContactList.
    """
    org = request.user.organization
    is_json = _is_ajax(request)

    if not org:
        if is_json:
            return JsonResponse({"status": "error", "message": "No organization."}, status=400)
        messages.error(request, "No organization associated with this account.")
        return redirect("contacts:lists")

    cl = get_object_or_404(ContactList, id=list_id, organization=org)
    name = request.POST.get("name", "").strip()
    if not name:
        if is_json:
            return JsonResponse({"status": "error", "message": "List name cannot be blank."}, status=400)
        messages.error(request, "List name cannot be blank.")
        return redirect("contacts:lists")

    if ContactList.objects.filter(organization=org, name__iexact=name).exclude(id=cl.id).exists():
        if is_json:
            return JsonResponse({"status": "error", "message": f"A list named '{name}' already exists."}, status=400)
        messages.error(request, f"A list named '{name}' already exists.")
        return redirect("contacts:lists")

    cl.name = name
    cl.save()

    if is_json:
        return JsonResponse({"status": "success", "id": cl.id, "name": cl.name})
    
    messages.success(request, f"Contact list renamed to '{cl.name}'.")
    return redirect("contacts:lists")


@login_required
@require_POST
def delete_list(request, list_id):
    """
    Delete a ContactList. Contacts themselves remain in the database.
    """
    org = request.user.organization
    is_json = _is_ajax(request)

    if not org:
        if is_json:
            return JsonResponse({"status": "error", "message": "No organization."}, status=400)
        messages.error(request, "No organization associated with this account.")
        return redirect("contacts:lists")

    cl = get_object_or_404(ContactList, id=list_id, organization=org)
    name = cl.name
    cl.delete()

    if is_json:
        return JsonResponse({"status": "success", "message": f"List '{name}' deleted."})
    
    messages.success(request, f"Contact list '{name}' deleted.")
    return redirect("contacts:lists")


# =========================================================================
# Custom Contact Fields Management Views
# =========================================================================

@login_required
def fields_index(request):
    """
    Overview page of approved extra fields for the organization.
    """
    org = request.user.organization
    if not org:
        return render(request, "contacts/no_organization.html")

    fields_qs = ContactField.objects.filter(organization=org).order_by("label")

    context = {
        "org": org,
        "fields": fields_qs,
        "total_fields": fields_qs.count(),
    }
    return render(request, "contacts/fields_index.html", context)


@login_required
@require_POST
def create_field(request):
    """
    Create a new approved custom field for the organization.
    """
    org = request.user.organization
    is_json = _is_ajax(request)

    if not org:
        if is_json:
            return JsonResponse({"status": "error", "message": "No organization."}, status=400)
        messages.error(request, "No organization associated with this account.")
        return redirect("contacts:fields")

    key = request.POST.get("key", "").strip().lower()
    label = request.POST.get("label", "").strip()

    if not label:
        if is_json:
            return JsonResponse({"status": "error", "message": "Field label cannot be blank."}, status=400)
        messages.error(request, "Field label cannot be blank.")
        return redirect("contacts:fields")

    if not key:
        import re
        key = re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_")
        if not key or not key[0].isalpha():
            key = f"field_{key}"

    field = ContactField(organization=org, key=key, label=label)
    try:
        field.full_clean()
        field.save()
        if is_json:
            return JsonResponse({"status": "success", "id": field.id, "key": field.key, "label": field.label})
        messages.success(request, f"Custom field '{field.label}' created successfully.")
        return redirect("contacts:fields")
    except ValidationError as ve:
        err_msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
        if is_json:
            return JsonResponse({"status": "error", "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("contacts:fields")


@login_required
@require_POST
def edit_field(request, field_id):
    """
    Update the display label of an approved custom field (key is read-only).
    """
    org = request.user.organization
    is_json = _is_ajax(request)

    if not org:
        if is_json:
            return JsonResponse({"status": "error", "message": "No organization."}, status=400)
        messages.error(request, "No organization.")
        return redirect("contacts:fields")

    field = get_object_or_404(ContactField, id=field_id, organization=org)
    label = request.POST.get("label", "").strip()
    if not label:
        if is_json:
            return JsonResponse({"status": "error", "message": "Field label cannot be blank."}, status=400)
        messages.error(request, "Field label cannot be blank.")
        return redirect("contacts:fields")

    field.label = label
    field.save(update_fields=["label"])

    if is_json:
        return JsonResponse({"status": "success", "id": field.id, "label": field.label})
    
    messages.success(request, f"Custom field '{field.label}' updated.")
    return redirect("contacts:fields")


@login_required
@require_POST
def delete_field(request, field_id):
    """
    Delete an approved custom field from the organization.
    """
    org = request.user.organization
    is_json = _is_ajax(request)

    if not org:
        if is_json:
            return JsonResponse({"status": "error", "message": "No organization."}, status=400)
        messages.error(request, "No organization.")
        return redirect("contacts:fields")

    field = get_object_or_404(ContactField, id=field_id, organization=org)
    key = field.key
    field.delete()

    if is_json:
        return JsonResponse({"status": "success", "message": f"Field '{key}' deleted."})
    
    messages.success(request, f"Custom field '{key}' deleted.")
    return redirect("contacts:fields")


# =========================================================================
# LEAD INGESTION REST / WEBHOOK API & REMOTE URL IMPORT
# =========================================================================

def _add_cors(resp):
    resp["Access-Control-Allow-Origin"] = "*"
    resp["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    resp["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-API-Key, ngrok-skip-browser-warning, X-CSRFToken"
    return resp


@csrf_exempt
def lead_ingest_api(request, api_key=None):
    """
    Public Ingestion REST & Webhook API endpoint for uploading leads.
    Supports:
    - Path parameter: /api/v1/leads/<api_key>/
    - Query parameter: ?api_key=<api_key>
    - Headers: Authorization: Bearer <api_key> OR X-API-Key: <api_key>
    - Active session fallback (for dashboard testing)
    """
    if request.method == "OPTIONS":
        return _add_cors(HttpResponse(status=200))

    token = api_key or request.GET.get("api_key") or request.POST.get("api_key")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.replace("Bearer ", "").strip()
        elif auth_header:
            token = auth_header.strip()
        if not token:
            token = request.headers.get("X-API-Key", "").strip()

    org = None
    if token:
        org = Organization.objects.filter(api_key=token).first()
    elif request.user.is_authenticated and getattr(request.user, "organization", None):
        org = request.user.organization

    if not org:
        return _add_cors(JsonResponse({
            "status": "error",
            "message": "Unauthorized. A valid organization API key is required via ?api_key=, header 'X-API-Key', or Bearer token."
        }, status=401))

    # If GET: return schema, documentation, and sample payload
    if request.method == "GET":
        base_url = f"{request.scheme}://{request.get_host()}"
        return _add_cors(JsonResponse({
            "status": "ok",
            "service": "SparkText Lead Ingestion API v1",
            "organization": org.name,
            "api_url": f"{base_url}/api/v1/leads/?api_key={org.api_key}",
            "webhook_url": f"{base_url}/api/v1/leads/{org.api_key}/",
            "supported_methods": ["POST"],
            "payload_format": "JSON object, JSON array (batch), or URL-encoded form data",
            "required_fields": ["phone"],
            "optional_fields": [
                "first_name", "last_name", "email", "list", "stage", "form", "consent", "custom_fields"
            ],
            "sample_json": {
                "phone": "+16505550199",
                "first_name": "Jane",
                "last_name": "Smith",
                "email": "jane@example.com",
                "list": "Web Leads",
                "stage": "Intake",
                "form": "Contact Us Form",
                "consent": True,
            }
        }))

    if request.method != "POST":
        return _add_cors(JsonResponse({"status": "error", "message": "Method not allowed. Use POST to upload leads."}, status=405))

    # Parse body
    payload = None
    content_type = request.content_type.lower() if hasattr(request, "content_type") else ""
    if "application/json" in content_type or request.body.strip().startswith((b"{", b"[")):
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except Exception as e:
            return _add_cors(JsonResponse({"status": "error", "message": f"Malformed JSON: {e}"}, status=400))
    else:
        payload = request.POST.dict()

    if not payload:
        return _add_cors(JsonResponse({"status": "error", "message": "No lead data received in request."}, status=400))

    # Check if batch list
    leads_list = []
    if isinstance(payload, list):
        leads_list = payload
    elif isinstance(payload, dict):
        for k in ["leads", "contacts", "data", "results", "items"]:
            if isinstance(payload.get(k), list):
                leads_list = payload[k]
                break

    if leads_list:
        created_count = 0
        updated_count = 0
        failed_count = 0
        results = []
        errors = []

        for idx, item in enumerate(leads_list, start=1):
            if not isinstance(item, dict):
                failed_count += 1
                errors.append({"index": idx, "error": "Item is not a valid JSON dictionary."})
                continue
            try:
                contact, created = ingest_lead_data(organization=org, raw_dict=item, source=Contact.Source.API)
                if created:
                    created_count += 1
                else:
                    updated_count += 1
                results.append({
                    "id": contact.id,
                    "phone": contact.phone,
                    "first_name": contact.first_name,
                    "last_name": contact.last_name,
                    "sms_status": contact.sms_status,
                    "created": created,
                })
            except ValidationError as ve:
                failed_count += 1
                msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
                errors.append({"index": idx, "error": msg})
            except Exception as exc:
                failed_count += 1
                errors.append({"index": idx, "error": str(exc)})

        return _add_cors(JsonResponse({
            "status": "success",
            "message": f"Processed {len(leads_list)} lead(s): {created_count} created, {updated_count} updated, {failed_count} failed.",
            "total": len(leads_list),
            "created_count": created_count,
            "updated_count": updated_count,
            "failed_count": failed_count,
            "contacts": results,
            "errors": errors[:50],
        }, status=201 if created_count > 0 else 200))

    # Single lead dict
    if not isinstance(payload, dict):
        return _add_cors(JsonResponse({"status": "error", "message": "Expected JSON object or array."}, status=400))

    try:
        contact, created = ingest_lead_data(organization=org, raw_dict=payload, source=Contact.Source.API)
        return _add_cors(JsonResponse({
            "status": "success",
            "message": f"Lead {contact.phone} {'created' if created else 'updated'} successfully.",
            "contact": {
                "id": contact.id,
                "phone": contact.phone,
                "first_name": contact.first_name,
                "last_name": contact.last_name,
                "email": contact.email,
                "sms_status": contact.sms_status,
                "created": created,
            }
        }, status=201 if created else 200))
    except ValidationError as ve:
        msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
        return _add_cors(JsonResponse({"status": "error", "message": msg}, status=400))
    except Exception as exc:
        logger.exception("Lead ingestion error")
        return _add_cors(JsonResponse({"status": "error", "message": str(exc)}, status=500))


@login_required
@require_POST
def fetch_remote_leads_view(request):
    """
    Imports leads by pulling from a remote API URL (e.g. JSON feed, webhook endpoint, external CRM).
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    url = request.POST.get("url", "").strip()
    if not url:
        return JsonResponse({"status": "error", "message": "API URL is required."}, status=400)

    method = request.POST.get("method", "GET").strip().upper()
    auth_header = request.POST.get("auth_header", "").strip()
    list_id_raw = request.POST.get("contact_list_id", "").strip()
    contact_list_id = int(list_id_raw) if list_id_raw.isdigit() else None
    consent_attested = request.POST.get("consent_attested", "1") in ("1", "true", "yes", "on")

    try:
        res = fetch_and_import_remote_leads(
            organization=org,
            url=url,
            method=method,
            auth_header=auth_header,
            contact_list_id=contact_list_id,
            consent_attested=consent_attested,
        )
        msg = f"Imported {res['created']} new lead(s) ({res['updated']} updated, {res['skipped']} skipped) from API URL."
        return JsonResponse({
            "status": "success",
            "message": msg,
            "data": res,
        })
    except ValidationError as ve:
        msg = "; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)
        return JsonResponse({"status": "error", "message": msg}, status=400)
    except Exception as exc:
        logger.exception("Remote API fetch error")
        return JsonResponse({"status": "error", "message": str(exc)}, status=500)


@login_required
@require_POST
def regenerate_api_key_view(request):
    """
    Regenerates the organization's unique API key.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    new_key = org.regenerate_api_key()
    base_url = f"{request.scheme}://{request.get_host()}"
    return JsonResponse({
        "status": "success",
        "api_key": new_key,
        "api_url": f"{base_url}/api/v1/leads/?api_key={new_key}",
        "webhook_url": f"{base_url}/api/v1/leads/{new_key}/",
        "message": "New API key generated successfully.",
    })


