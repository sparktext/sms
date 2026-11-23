import csv
import json
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from contacts.models import Contact, ContactField, ContactList
from messaging.models import PhoneNumber

from .models import Campaign, CampaignRecipient, CampaignTemplate, MessageTemplate, TemplateCategory
from .services import (
    DEFAULT_CAMPAIGN_TEMPLATES,
    build_campaign_recipients,
    cancel_campaign,
    check_and_dispatch_due_campaigns,
    ensure_default_campaign_templates,
    ensure_default_templates,
    process_campaign_batch,
    resolve_campaign_contacts,
)
from .ws_broadcast import broadcast_campaign_list_update


@login_required
@never_cache
def campaign_list(request):
    """
    Main campaigns dashboard:
    - High-level delivery metrics (sent, delivered, opt-outs)
    - List of campaigns with progress bars and status filters
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    # Automatically dispatch any due scheduled campaigns for this organization
    check_and_dispatch_due_campaigns(org)

    status_filter = request.GET.get("status", "").strip().lower()
    q = request.GET.get("q", "").strip()

    campaigns_qs = Campaign.objects.filter(organization=org).select_related(
        "phone_number", "created_by"
    ).prefetch_related("lists")

    if status_filter in Campaign.Status.values:
        campaigns_qs = campaigns_qs.filter(status=status_filter)

    if q:
        campaigns_qs = campaigns_qs.filter(name__icontains=q)

    # Summary metrics
    all_campaigns = Campaign.objects.filter(organization=org)
    total_campaigns = all_campaigns.count()
    aggregates = all_campaigns.aggregate(
        total_sent=Sum("sent_count"),
        total_delivered=Sum("delivered_count"),
    )
    total_sent = aggregates["total_sent"] or 0
    total_delivered = aggregates["total_delivered"] or 0
    overall_delivery_rate = int(total_delivered / total_sent * 100) if total_sent > 0 else 100

    paginator = Paginator(campaigns_qs, 15)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    ensure_default_campaign_templates(org)
    templates_count = CampaignTemplate.objects.filter(organization=org).count()

    context = {
        "page_obj": page_obj,
        "status_filter": status_filter,
        "q": q,
        "total_campaigns": total_campaigns,
        "total_sent": total_sent,
        "total_delivered": total_delivered,
        "overall_delivery_rate": overall_delivery_rate,
        "templates_count": templates_count,
        "statuses": Campaign.Status.choices,
    }
    return render(request, "campaigns/campaign_list.html", context)


# Contact fields the campaign composer offers as extra merge tags (besides name / business / phone)
_NON_TAG_FIELDS = {"stage", "not_qualified_reason", "detail", "assigned_to", "best_call_time"}


def _lead_merge_tags(org, sample_size=400):
    """
    Lead fields usable as {{ tags }}: approved ContactFields plus the flat fields your leads actually
    carry (e.g. current_insurer, city, zip_code), most common first, each with a sample value for the preview.
    """
    from collections import Counter

    from contacts.lead_dossier import label_for
    counts, samples, labels = Counter(), {}, {}
    for f in ContactField.objects.filter(organization=org):
        key = f.key.lower()
        if key in _NON_TAG_FIELDS:
            continue
        labels[key] = f.label
        counts[key] += 0
    for cf in Contact.objects.filter(organization=org).order_by("-id").values_list("custom_fields", flat=True)[:sample_size]:
        if not isinstance(cf, dict):
            continue
        for key, val in cf.items():
            key = str(key).lower()
            if key in _NON_TAG_FIELDS or not isinstance(val, (str, int, float)):
                continue
            text = str(val).strip()
            if not text or len(text) > 60 or text.startswith(("{", "[")):
                continue
            counts[key] += 1
            samples.setdefault(key, text)
    return [{"key": k, "label": labels.get(k) or label_for(k), "count": n, "sample": samples.get(k, "")}
            for k, n in counts.most_common()]


@login_required
@never_cache
def campaign_create(request):
    """
    Campaign composer wizard:
    - Multi-list audience targeting
    - Template text with live preview and segment estimation
    - Immediate dispatch or future scheduling
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    phone_numbers = PhoneNumber.objects.filter(organization=org, is_active=True)
    if not phone_numbers.exists():
        messages.error(request, "You must have at least one active phone number to create a campaign.")
        return redirect("messaging:dashboard")

    contact_lists = ContactList.objects.filter(organization=org).annotate(
        total_contacts=Count("contacts")
    )
    custom_fields = ContactField.objects.filter(organization=org)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        phone_number_id = request.POST.get("phone_number_id")
        message_template = request.POST.get("message_template", "").strip()
        timing = request.POST.get("timing", "immediate").strip().lower()
        scheduled_at_str = request.POST.get("scheduled_at", "").strip()

        # Multi-selected list IDs
        selected_list_ids = request.POST.getlist("lists")
        selected_list_ids = [int(lid) for lid in selected_list_ids if str(lid).isdigit()]

        # No audience filters beyond list selection (lead-stage filter removed)
        filter_criteria = {}

        # Campaign-specific AI Playbook settings
        ai_mode = request.POST.get("ai_mode", Campaign.AIMode.DEFAULT).strip()
        if ai_mode not in Campaign.AIMode.values:
            ai_mode = Campaign.AIMode.DEFAULT

        ai_campaign_goal = request.POST.get("ai_campaign_goal", "").strip()
        ai_special_instructions = request.POST.get("ai_special_instructions", "").strip()

        # Parse qualification questions JSON if custom playbook
        ai_qualification_questions = []
        raw_questions = request.POST.get("ai_qualification_questions_json", "").strip()
        if raw_questions:
            try:
                parsed_q = json.loads(raw_questions)
                if isinstance(parsed_q, list):
                    for item in parsed_q:
                        if isinstance(item, dict) and item.get("key") and item.get("question"):
                            ai_qualification_questions.append({
                                "key": str(item["key"]).strip().lower().replace(" ", "_"),
                                "question": str(item["question"]).strip(),
                                "required": bool(item.get("required", False)),
                            })
            except Exception:
                pass

        save_draft_only = request.POST.get("save_draft") == "1"

        # Validation
        if not name:
            messages.error(request, "Please enter a campaign name.")
            return redirect("campaigns:create")

        if not save_draft_only and not message_template:
            messages.error(request, "Please enter message text for the campaign.")
            return redirect("campaigns:create")

        phone_num = get_object_or_404(phone_numbers, id=phone_number_id)

        scheduled_at = None
        scheduled_at_iso = request.POST.get("scheduled_at_iso", "").strip()
        if timing == "scheduled":
            if scheduled_at_iso:
                try:
                    # ISO-8601 string from browser client (e.g. 2026-11-03T19:35:00.000Z)
                    scheduled_at = datetime.fromisoformat(scheduled_at_iso.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    scheduled_at = None

            if not scheduled_at and scheduled_at_str:
                try:
                    # Fallback HTML5 datetime-local format: YYYY-MM-DDTHH:MM
                    scheduled_at = timezone.make_aware(datetime.strptime(scheduled_at_str, "%Y-%m-%dT%H:%M"))
                except ValueError:
                    messages.error(request, "Invalid scheduled date/time format.")
                    return redirect("campaigns:create")
        else:
            scheduled_at = timezone.now()

        # Determine status
        if save_draft_only:
            status = Campaign.Status.DRAFT
        elif timing == "immediate":
            status = Campaign.Status.SENDING
        else:
            status = Campaign.Status.SCHEDULED

        # Create Campaign
        campaign = Campaign.objects.create(
            organization=org,
            created_by=request.user,
            name=name,
            phone_number=phone_num,
            message_template=message_template,
            filter_criteria=filter_criteria,
            status=status,
            scheduled_at=scheduled_at,
            started_at=timezone.now() if timing == "immediate" and not save_draft_only else None,
            ai_mode=ai_mode,
            ai_campaign_goal=ai_campaign_goal,
            ai_qualification_questions=ai_qualification_questions,
            ai_special_instructions=ai_special_instructions,
        )

        if selected_list_ids:
            lists_qs = ContactList.objects.filter(id__in=selected_list_ids, organization=org)
            campaign.lists.set(lists_qs)

        # Build recipient records & evaluate marketing consent
        total, eligible = build_campaign_recipients(campaign)

        # Broadcast live event to connected users / tabs on /campaigns/
        broadcast_campaign_list_update(campaign, action="created")

        if save_draft_only:
            messages.success(
                request,
                f"Campaign draft '{campaign.name}' saved successfully ({eligible} eligible recipients)."
            )
        elif timing == "immediate":
            # Kick off first batch right away (thread-safe, atomically claims pending recipients)
            process_campaign_batch(campaign, limit=50)
            messages.success(
                request,
                f"Campaign '{campaign.name}' launched! Dispatched initial batch of {eligible} eligible recipients."
            )
        else:
            messages.success(
                request,
                f"Campaign '{campaign.name}' scheduled for {scheduled_at.strftime('%b %d, %Y at %I:%M %p')}. "
                f"({eligible} eligible recipients queued)."
            )

        return redirect("campaigns:detail", campaign_id=campaign.id)

    ensure_default_templates(org)
    saved_templates = MessageTemplate.objects.filter(organization=org)
    selected_template = None
    tmpl_id = request.GET.get("template_id")
    if tmpl_id and tmpl_id.isdigit():
        selected_template = saved_templates.filter(id=int(tmpl_id)).first()

    ai_config = getattr(org, "ai_config", None)
    default_questions = (ai_config.qualification_questions if ai_config and isinstance(ai_config.qualification_questions, list) else [])
    default_questions_json = json.dumps(default_questions)

    context = {
        "phone_numbers": phone_numbers,
        "contact_lists": contact_lists,
        "custom_fields": custom_fields,
        "lead_merge_tags": _lead_merge_tags(org),
        "saved_templates": saved_templates,
        "selected_template": selected_template,
        "now_iso": timezone.now().strftime("%Y-%m-%dT%H:%M"),
        "ai_config": ai_config,
        "default_questions_json": default_questions_json,
    }
    return render(request, "campaigns/campaign_create.html", context)


def _get_filtered_campaign_recipients(campaign, status_filter="", q=""):
    """Return campaign recipients queryset filtered by optional delivery status and contact search."""
    recipients_qs = campaign.recipients.select_related("contact", "message").order_by("id")
    if status_filter in CampaignRecipient.Status.values:
        recipients_qs = recipients_qs.filter(status=status_filter)
    if q:
        recipients_qs = recipients_qs.filter(
            Q(contact__phone__icontains=q)
            | Q(contact__first_name__icontains=q)
            | Q(contact__last_name__icontains=q)
        )
    return recipients_qs


@login_required
@never_cache
def campaign_detail(request, campaign_id):
    """
    Detailed analytics and recipient ledger for a specific campaign.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    campaign = get_object_or_404(
        Campaign.objects.select_related("phone_number", "created_by").prefetch_related("lists"),
        id=campaign_id,
        organization=org,
    )

    status_filter = request.GET.get("status", "").strip().lower()
    q = request.GET.get("q", "").strip()
    recipients_qs = _get_filtered_campaign_recipients(campaign, status_filter=status_filter, q=q)

    paginator = Paginator(recipients_qs, 25)
    page_number = request.GET.get("page", 1)
    recipients_page = paginator.get_page(page_number)

    context = {
        "campaign": campaign,
        "recipients_page": recipients_page,
        "status_filter": status_filter,
        "q": q,
        "statuses": CampaignRecipient.Status.choices,
    }
    return render(request, "campaigns/campaign_detail.html", context)


def _safe_csv_cell(value):
    """Neutralize leading spreadsheet formula characters before writing to CSV."""
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


@login_required
@require_GET
def campaign_recipient_export(request, campaign_id):
    """Export filtered campaign recipients (e.g. Failed / Skipped) to CSV."""
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    campaign = get_object_or_404(Campaign, id=campaign_id, organization=org)
    status_filter = request.GET.get("status", "").strip().lower()
    q = request.GET.get("q", "").strip()
    recipients = _get_filtered_campaign_recipients(campaign, status_filter=status_filter, q=q)

    suffix = f"_{status_filter}" if status_filter in CampaignRecipient.Status.values else "_all"
    filename = f"campaign_{campaign.id}{suffix}_recipients_{timezone.now().strftime('%Y%m%d_%H%M%S')}.csv"
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        "Phone",
        "First Name",
        "Last Name",
        "Contact SMS Status",
        "Delivery Status",
        "Reason / Error",
        "Dispatched At",
        "Message Body",
    ])
    for r in recipients.iterator(chunk_size=500):
        reason = r.error_message or r.skip_reason or ""
        sent_at = timezone.localtime(r.sent_at).strftime("%Y-%m-%d %H:%M:%S") if r.sent_at else ""
        writer.writerow([
            _safe_csv_cell(r.contact.phone),
            _safe_csv_cell(r.contact.first_name),
            _safe_csv_cell(r.contact.last_name),
            r.contact.get_sms_status_display(),
            r.get_status_display(),
            _safe_csv_cell(reason),
            sent_at,
            _safe_csv_cell(r.personalized_body),
        ])
    return response


@login_required
@require_POST
def campaign_action(request, campaign_id):
    """
    Performs management actions: 'cancel', 'send_now', 'delete'.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    campaign = get_object_or_404(Campaign, id=campaign_id, organization=org)
    action = request.POST.get("action", "").strip().lower()

    if action == "cancel":
        if cancel_campaign(campaign):
            broadcast_campaign_list_update(campaign, action="updated")
            messages.info(request, f"Campaign '{campaign.name}' was cancelled. Remaining messages stopped.")
        else:
            messages.warning(request, "Campaign cannot be cancelled in its current state.")

    elif action == "send_now":
        if campaign.status in [Campaign.Status.SCHEDULED, Campaign.Status.DRAFT]:
            campaign.status = Campaign.Status.SENDING
            campaign.scheduled_at = timezone.now()
            campaign.started_at = timezone.now()
            campaign.save(update_fields=["status", "scheduled_at", "started_at"])
            process_campaign_batch(campaign, limit=50)
            broadcast_campaign_list_update(campaign, action="updated")
            messages.success(request, f"Campaign '{campaign.name}' started sending.")

    elif action == "delete":
        can_delete = (
            campaign.status in [Campaign.Status.DRAFT, Campaign.Status.CANCELLED]
            or (campaign.status == Campaign.Status.SCHEDULED and campaign.sent_count == 0)
        )
        if can_delete:
            name = campaign.name
            broadcast_campaign_list_update(campaign, action="deleted")
            campaign.delete()
            messages.success(request, f"Campaign '{name}' deleted.")
            return redirect("campaigns:list")
        else:
            messages.error(request, "Only draft, cancelled, or unsent scheduled campaigns can be deleted.")

    return redirect("campaigns:detail", campaign_id=campaign.id)


@login_required
@require_http_methods(["GET", "POST"])
def campaign_clone(request, campaign_id):
    """
    1-Click clone/duplicate an existing campaign into a new DRAFT campaign.
    Duplicates target lists, filter criteria, message template, and AI playbook settings.
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    original = get_object_or_404(Campaign, id=campaign_id, organization=org)
    new_name = f"[Copy] {original.name}"[:200]

    clone = Campaign.objects.create(
        organization=org,
        created_by=request.user,
        name=new_name,
        phone_number=original.phone_number,
        message_template=original.message_template,
        filter_criteria=original.filter_criteria,
        status=Campaign.Status.DRAFT,
        ai_mode=original.ai_mode,
        ai_campaign_goal=original.ai_campaign_goal,
        ai_qualification_questions=original.ai_qualification_questions,
        ai_special_instructions=original.ai_special_instructions,
    )
    clone.lists.set(original.lists.all())
    broadcast_campaign_list_update(clone, action="created")
    messages.success(request, f"Campaign duplicated as '{clone.name}'! You can now edit its details, preview audience, and launch.")
    return redirect("campaigns:edit", campaign_id=clone.id)


@login_required
@never_cache
def campaign_edit(request, campaign_id):
    """
    Edit an existing DRAFT or SCHEDULED campaign:
    - Update name, phone_number, message_template, lists, filter_criteria, AI playbook
    - Re-evaluates recipient records and audience counts
    - Either save as draft, schedule for later, or launch immediately
    """
    org = request.user.organization
    if not org:
        raise PermissionDenied("User is not associated with an organization.")

    campaign = get_object_or_404(
        Campaign.objects.select_related("phone_number").prefetch_related("lists"),
        id=campaign_id,
        organization=org,
    )

    if campaign.status in [Campaign.Status.SENDING, Campaign.Status.COMPLETED]:
        messages.error(request, "Campaigns that are currently sending or completed cannot be edited.")
        return redirect("campaigns:detail", campaign_id=campaign.id)

    phone_numbers = PhoneNumber.objects.filter(organization=org, is_active=True)
    contact_lists = ContactList.objects.filter(organization=org).annotate(
        total_contacts=Count("contacts")
    )
    custom_fields = ContactField.objects.filter(organization=org)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        phone_number_id = request.POST.get("phone_number_id")
        message_template = request.POST.get("message_template", "").strip()
        timing = request.POST.get("timing", "immediate").strip().lower()
        scheduled_at_str = request.POST.get("scheduled_at", "").strip()
        save_draft_only = request.POST.get("save_draft") == "1"

        # Multi-selected list IDs
        selected_list_ids = request.POST.getlist("lists")
        selected_list_ids = [int(lid) for lid in selected_list_ids if str(lid).isdigit()]

        # No audience filters beyond list selection (lead-stage filter removed)
        filter_criteria = {}

        # Campaign-specific AI Playbook settings
        ai_mode = request.POST.get("ai_mode", Campaign.AIMode.DEFAULT).strip()
        if ai_mode not in Campaign.AIMode.values:
            ai_mode = Campaign.AIMode.DEFAULT

        ai_campaign_goal = request.POST.get("ai_campaign_goal", "").strip()
        ai_special_instructions = request.POST.get("ai_special_instructions", "").strip()

        # Parse qualification questions JSON if custom playbook
        ai_qualification_questions = []
        raw_questions = request.POST.get("ai_qualification_questions_json", "").strip()
        if raw_questions:
            try:
                parsed_q = json.loads(raw_questions)
                if isinstance(parsed_q, list):
                    for item in parsed_q:
                        if isinstance(item, dict) and item.get("key") and item.get("question"):
                            ai_qualification_questions.append({
                                "key": str(item["key"]).strip().lower().replace(" ", "_"),
                                "question": str(item["question"]).strip(),
                                "required": bool(item.get("required", False)),
                            })
            except Exception:
                pass

        # Validation
        if not name:
            messages.error(request, "Please enter a campaign name.")
            return redirect("campaigns:edit", campaign_id=campaign.id)

        if not message_template:
            messages.error(request, "Please enter message text for the campaign.")
            return redirect("campaigns:edit", campaign_id=campaign.id)

        phone_num = get_object_or_404(phone_numbers, id=phone_number_id)

        scheduled_at = None
        scheduled_at_iso = request.POST.get("scheduled_at_iso", "").strip()
        if timing == "scheduled":
            if scheduled_at_iso:
                try:
                    scheduled_at = datetime.fromisoformat(scheduled_at_iso.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    scheduled_at = None

            if not scheduled_at and scheduled_at_str:
                try:
                    scheduled_at = timezone.make_aware(datetime.strptime(scheduled_at_str, "%Y-%m-%dT%H:%M"))
                except ValueError:
                    messages.error(request, "Invalid scheduled date/time format.")
                    return redirect("campaigns:edit", campaign_id=campaign.id)
        else:
            scheduled_at = timezone.now()

        # Update Campaign fields
        campaign.name = name
        campaign.phone_number = phone_num
        campaign.message_template = message_template
        campaign.filter_criteria = filter_criteria
        campaign.ai_mode = ai_mode
        campaign.ai_campaign_goal = ai_campaign_goal
        campaign.ai_qualification_questions = ai_qualification_questions
        campaign.ai_special_instructions = ai_special_instructions
        campaign.scheduled_at = scheduled_at

        if save_draft_only:
            campaign.status = Campaign.Status.DRAFT
        elif timing == "immediate":
            campaign.status = Campaign.Status.SENDING
            campaign.started_at = timezone.now()
        else:
            campaign.status = Campaign.Status.SCHEDULED

        campaign.save()

        # Update lists ManyToMany
        if selected_list_ids:
            lists_qs = ContactList.objects.filter(id__in=selected_list_ids, organization=org)
            campaign.lists.set(lists_qs)
        else:
            campaign.lists.clear()

        # Clear existing un-sent recipients and rebuild
        campaign.recipients.filter(status__in=[CampaignRecipient.Status.PENDING, CampaignRecipient.Status.SKIPPED, CampaignRecipient.Status.QUEUED]).delete()
        total, eligible = build_campaign_recipients(campaign)

        # Broadcast update to active browser tabs viewing /campaigns/
        broadcast_campaign_list_update(campaign, action="updated")

        if save_draft_only:
            messages.success(request, f"Campaign draft '{campaign.name}' saved successfully ({eligible} eligible recipients).")
        elif timing == "immediate":
            process_campaign_batch(campaign, limit=50)
            messages.success(request, f"Campaign '{campaign.name}' updated and launched! Dispatched initial batch of {eligible} recipients.")
        else:
            messages.success(
                request,
                f"Campaign '{campaign.name}' updated and scheduled for {scheduled_at.strftime('%b %d, %Y at %I:%M %p')}. "
                f"({eligible} eligible recipients queued)."
            )

        return redirect("campaigns:detail", campaign_id=campaign.id)

    # GET request
    ensure_default_templates(org)
    saved_templates = MessageTemplate.objects.filter(organization=org)
    ai_config = getattr(org, "ai_config", None)
    default_questions = (ai_config.qualification_questions if ai_config and isinstance(ai_config.qualification_questions, list) else [])

    campaign_questions = campaign.ai_qualification_questions if campaign.ai_qualification_questions else default_questions
    campaign_questions_json = json.dumps(campaign_questions)
    default_questions_json = json.dumps(default_questions)

    campaign_list_ids = list(campaign.lists.values_list("id", flat=True))
    scheduled_iso = campaign.scheduled_at.strftime("%Y-%m-%dT%H:%M") if campaign.scheduled_at else timezone.now().strftime("%Y-%m-%dT%H:%M")

    context = {
        "is_edit": True,
        "campaign": campaign,
        "campaign_list_ids": campaign_list_ids,
        "phone_numbers": phone_numbers,
        "contact_lists": contact_lists,
        "custom_fields": custom_fields,
        "lead_merge_tags": _lead_merge_tags(org),
        "saved_templates": saved_templates,
        "selected_template": None,
        "now_iso": timezone.now().strftime("%Y-%m-%dT%H:%M"),
        "campaign_scheduled_iso": scheduled_iso,
        "ai_config": ai_config,
        "campaign_questions_json": campaign_questions_json,
        "default_questions_json": default_questions_json,
    }
    return render(request, "campaigns/campaign_create.html", context)


@login_required
def audience_preview_ajax(request):
    """
    Live AJAX audience calculator used in the campaign composer.
    Returns counts of total matching contacts, opted-in (eligible), and skipped.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "Unauthorized"}, status=401)

    list_ids = request.GET.getlist("lists[]") or request.GET.getlist("lists")
    list_ids = [int(lid) for lid in list_ids if str(lid).isdigit()]

    contacts = resolve_campaign_contacts(
        organization=org,
        list_ids=list_ids,
        filter_criteria={},
    )

    total = contacts.count()
    opted_in = contacts.filter(sms_status=Contact.SMSStatus.OPTED_IN).count()
    opted_out = contacts.filter(sms_status=Contact.SMSStatus.OPTED_OUT).count()
    no_consent = total - (opted_in + opted_out)

    return JsonResponse({
        "status": "success",
        "total": total,
        "opted_in": opted_in,
        "opted_out": opted_out,
        "no_consent": no_consent,
        "will_send": opted_in,
        "will_skip": total - opted_in,
    })


# =========================================================================
# CAMPAIGN TEMPLATES MANAGEMENT
# =========================================================================

def _is_ajax(request) -> bool:
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.headers.get("Accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    return False


def _resolve_category(raw_category: str, custom_category: str = "") -> str:
    """
    Resolves template category slug from standard or user-defined inputs.
    Converts custom inputs into clean snake_case identifiers (max 50 chars).
    """
    import re
    cat = (custom_category or "").strip()
    if not cat and raw_category and raw_category != "__new__":
        cat = raw_category.strip()

    if not cat:
        return CampaignTemplate.Category.MARKETING

    valid_standard = dict(CampaignTemplate.Category.choices)
    if cat in valid_standard:
        return cat

    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", cat).strip("_").lower()
    return cleaned[:50] or CampaignTemplate.Category.MARKETING


def _ensure_template_category(org, slug: str, display_name: str = "") -> TemplateCategory:
    """Ensures a TemplateCategory row exists for the given organization and slug."""
    existing = TemplateCategory.objects.filter(organization=org, slug=slug).first()
    if existing:
        return existing
    standard_labels = dict(CampaignTemplate.Category.choices)
    label = display_name.strip() if display_name and display_name.strip() else str(
        standard_labels.get(slug, slug.replace("_", " ").title())
    )
    next_order = TemplateCategory.objects.filter(organization=org).count() + 1
    return TemplateCategory.objects.create(
        organization=org,
        slug=slug,
        name=label[:80],
        order=next_order,
    )


@login_required
@never_cache
def templates_index(request):
    """
    Templates dashboard inside Campaigns.
    Dynamically surfaces organization categories (editable & deletable).
    """
    org = request.user.organization
    if not org:
        messages.error(request, "No organization associated with this account.")
        return redirect("messaging:dashboard")

    ensure_default_campaign_templates(org)

    all_org_templates = CampaignTemplate.objects.filter(organization=org)

    # Dynamic counts per category
    cat_counts_qs = all_org_templates.values("category").annotate(count=Count("id"))
    counts_map = {item["category"]: item["count"] for item in cat_counts_qs}
    total_count = sum(counts_map.values())

    # Sync any orphan template categories into TemplateCategory table (e.g. legacy/direct ORM records)
    db_categories = list(TemplateCategory.objects.filter(organization=org))
    known_slugs = {c.slug for c in db_categories}
    for slug in sorted(counts_map.keys()):
        if slug and slug not in known_slugs:
            new_cat = _ensure_template_category(org, slug)
            db_categories.append(new_cat)
            known_slugs.add(slug)

    cat_label_map = {c.slug: c.name for c in db_categories}

    all_categories = [
        {
            "code": c.slug,
            "label": c.name,
            "count": counts_map.get(c.slug, 0),
        }
        for c in db_categories
    ]

    templates_qs = all_org_templates
    selected_category = request.GET.get("category", "").strip()
    if selected_category and selected_category != "all":
        templates_qs = templates_qs.filter(category=selected_category)

    q = request.GET.get("q", "").strip()
    if q:
        templates_qs = templates_qs.filter(
            Q(name__icontains=q) | Q(body__icontains=q)
        )

    templates_list = list(templates_qs)
    for t in templates_list:
        t.display_category = cat_label_map.get(t.category, t.category.replace("_", " ").title())

    context = {
        "org": org,
        "templates": templates_list,
        "selected_category": selected_category or "all",
        "q": q,
        "total_count": total_count,
        "all_categories": all_categories,
        "counts": counts_map,
        "category_choices": [(c["code"], c["label"]) for c in all_categories],
    }
    return render(request, "campaigns/templates_index.html", context)


@login_required
@require_POST
def template_create(request):
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    name = request.POST.get("name", "").strip()
    raw_category = request.POST.get("category", CampaignTemplate.Category.MARKETING).strip()
    custom_category = request.POST.get("custom_category", "").strip()
    body = request.POST.get("body", "").strip()

    if not name:
        return JsonResponse({"status": "error", "message": "Template name is required."}, status=400)
    if not body:
        return JsonResponse({"status": "error", "message": "Template message body is required."}, status=400)

    category = _resolve_category(raw_category, custom_category)
    _ensure_template_category(org, category, custom_category)

    template = CampaignTemplate.objects.create(
        organization=org,
        name=name,
        category=category,
        body=body,
        created_by=request.user,
    )

    if _is_ajax(request):
        return JsonResponse({
            "status": "success",
            "message": f"Template '{template.name}' created.",
            "template": {
                "id": template.id,
                "name": template.name,
                "category": template.category,
                "category_display": template.category_display,
                "body": template.body,
                "char_count": template.character_count,
                "segment_count": template.segment_count,
            }
        }, status=201)

    messages.success(request, f"Template '{template.name}' created.")
    return redirect("campaigns:templates")


@login_required
@require_http_methods(["GET", "POST"])
def template_edit(request, template_id):
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    template = get_object_or_404(CampaignTemplate, id=template_id, organization=org)

    if request.method == "GET":
        return JsonResponse({
            "status": "success",
            "template": {
                "id": template.id,
                "name": template.name,
                "category": template.category,
                "category_display": template.category_display,
                "body": template.body,
                "char_count": template.character_count,
                "segment_count": template.segment_count,
            }
        })

    name = request.POST.get("name", "").strip()
    raw_category = request.POST.get("category", template.category).strip()
    custom_category = request.POST.get("custom_category", "").strip()
    body = request.POST.get("body", "").strip()

    if not name:
        return JsonResponse({"status": "error", "message": "Template name is required."}, status=400)
    if not body:
        return JsonResponse({"status": "error", "message": "Template message body is required."}, status=400)

    category = _resolve_category(raw_category, custom_category)
    _ensure_template_category(org, category, custom_category)

    template.name = name
    template.category = category
    template.body = body
    template.save()

    if _is_ajax(request):
        return JsonResponse({
            "status": "success",
            "message": f"Template '{template.name}' updated.",
            "template": {
                "id": template.id,
                "name": template.name,
                "category": template.category,
                "category_display": template.category_display,
                "body": template.body,
                "char_count": template.character_count,
                "segment_count": template.segment_count,
            }
        })

    messages.success(request, f"Template '{template.name}' updated.")
    return redirect("campaigns:templates")


@login_required
@require_POST
def template_delete(request, template_id):
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    template = get_object_or_404(CampaignTemplate, id=template_id, organization=org)
    name = template.name
    template.delete()

    if _is_ajax(request):
        return JsonResponse({"status": "success", "message": f"Template '{name}' deleted."})

    messages.success(request, f"Template '{name}' deleted.")
    return redirect("campaigns:templates")


@login_required
@require_POST
def category_create(request):
    """Creates a new category directly from the Manage Categories modal."""
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"status": "error", "message": "Category name is required."}, status=400)

    slug = _resolve_category("", name)
    cat = _ensure_template_category(org, slug, name)
    return JsonResponse({
        "status": "success",
        "message": f"Category '{cat.name}' created.",
        "category": {"code": cat.slug, "label": cat.name},
    }, status=201)


@login_required
@require_POST
def category_delete(request):
    """
    Deletes ANY template category (starter or custom).
    If delete_templates='1', removes templates inside it; otherwise moves templates to
    `reassign_to` (or another remaining category / 'uncategorized').
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    category_code = request.POST.get("category", "").strip().lower()
    if not category_code:
        return JsonResponse({"status": "error", "message": "Category code is required."}, status=400)

    delete_templates = request.POST.get("delete_templates") == "1"
    reassign_to = request.POST.get("reassign_to", "").strip().lower()

    cat_obj = TemplateCategory.objects.filter(organization=org, slug=category_code).first()
    display_name = cat_obj.name if cat_obj else category_code.replace("_", " ").title()

    # Delete the category definition
    TemplateCategory.objects.filter(organization=org, slug=category_code).delete()

    affected_templates = CampaignTemplate.objects.filter(organization=org, category=category_code)
    affected_count = affected_templates.count()

    if delete_templates:
        affected_templates.delete()
        msg = f"Category '{display_name}' and its {affected_count} template{'s' if affected_count != 1 else ''} deleted."
    else:
        target_slug = None
        if affected_count > 0:
            if reassign_to and reassign_to != category_code:
                target_cat = TemplateCategory.objects.filter(organization=org, slug=reassign_to).first()
                if target_cat:
                    target_slug = target_cat.slug
            if not target_slug:
                # Pick first remaining category, preferring marketing if it exists
                remaining = TemplateCategory.objects.filter(organization=org).order_by("order", "name")
                pref = remaining.filter(slug="marketing").first() or remaining.first()
                if pref:
                    target_slug = pref.slug
                else:
                    # User deleted every single category while keeping templates -> create 'general'
                    general_cat = _ensure_template_category(org, "general", "General")
                    target_slug = general_cat.slug

            affected_templates.update(category=target_slug)
            target_obj = TemplateCategory.objects.filter(organization=org, slug=target_slug).first()
            target_name = target_obj.name if target_obj else target_slug.replace("_", " ").title()
            msg = f"Category '{display_name}' deleted. {affected_count} template{'s' if affected_count != 1 else ''} moved to '{target_name}'."
        else:
            msg = f"Category '{display_name}' deleted."

    if _is_ajax(request):
        return JsonResponse({"status": "success", "message": msg, "category": category_code})

    messages.success(request, msg)
    return redirect("campaigns:templates")


@login_required
@require_POST
def category_rename(request):
    """
    Renames ANY category (starter or custom), fixing typos or customizing labels.
    Updates both the TemplateCategory record and all CampaignTemplate rows using the old slug.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    old_category = request.POST.get("old_category", "").strip().lower()
    new_name = request.POST.get("new_name", "").strip()

    if not old_category:
        return JsonResponse({"status": "error", "message": "Original category code is required."}, status=400)
    if not new_name:
        return JsonResponse({"status": "error", "message": "New category name is required."}, status=400)

    new_category_code = _resolve_category("", new_name)
    new_display = new_name[:80]

    # Check if old category object exists
    old_cat_obj = TemplateCategory.objects.filter(organization=org, slug=old_category).first()
    existing_target = TemplateCategory.objects.filter(organization=org, slug=new_category_code).exclude(slug=old_category).first()

    if existing_target:
        # Target slug already exists on a different category -> merge templates & delete old category
        affected_templates = CampaignTemplate.objects.filter(organization=org, category=old_category)
        affected_count = affected_templates.count()
        affected_templates.update(category=new_category_code)
        if old_cat_obj:
            old_cat_obj.delete()
        new_display = existing_target.name
    else:
        if old_cat_obj:
            old_cat_obj.slug = new_category_code
            old_cat_obj.name = new_display
            old_cat_obj.save(update_fields=["slug", "name"])
        else:
            _ensure_template_category(org, new_category_code, new_display)
        affected_templates = CampaignTemplate.objects.filter(organization=org, category=old_category)
        affected_count = affected_templates.count()
        if old_category != new_category_code:
            affected_templates.update(category=new_category_code)

    msg = f"Category renamed to '{new_display}'. {affected_count} template{'s' if affected_count != 1 else ''} updated."

    if _is_ajax(request):
        return JsonResponse({
            "status": "success",
            "message": msg,
            "old_category": old_category,
            "new_category": new_category_code,
            "new_label": new_display,
        })

    messages.success(request, msg)
    return redirect("campaigns:templates")


@login_required
@require_GET
def templates_api_list(request):
    """
    JSON API for loading templates in Campaign Creator or Inbox Quick Replies.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    ensure_default_campaign_templates(org)

    category = request.GET.get("category", "").strip()
    qs = CampaignTemplate.objects.filter(organization=org)
    if category:
        qs = qs.filter(category=category)

    cat_label_map = {c.slug: c.name for c in TemplateCategory.objects.filter(organization=org)}

    data = [
        {
            "id": t.id,
            "name": t.name,
            "category": t.category,
            "category_label": cat_label_map.get(t.category, t.category_display),
            "body": t.body,
            "character_count": t.character_count,
            "segment_count": t.segment_count,
            "merge_tags": t.merge_tags,
        }
        for t in qs
    ]
    return JsonResponse({"status": "success", "templates": data})


@login_required
@require_POST
def templates_reset_defaults(request):
    """
    Restore default starter templates for the organization.
    If 'category' is specified in POST, only templates in that category are reset.
    """
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "error", "message": "No organization."}, status=400)

    category = request.POST.get("category", "").strip()
    if category:
        _ensure_template_category(org, category)
        CampaignTemplate.objects.filter(organization=org, category=category).delete()
        to_create = [
            CampaignTemplate(
                organization=org,
                name=item["name"],
                category=item["category"],
                body=item["body"],
                is_default=True,
            )
            for item in DEFAULT_CAMPAIGN_TEMPLATES
            if item["category"] == category
        ]
        CampaignTemplate.objects.bulk_create(to_create)
    else:
        CampaignTemplate.objects.filter(organization=org).delete()
        org.templates_seeded = False
        org.save(update_fields=["templates_seeded"])
        ensure_default_campaign_templates(org)

    return JsonResponse({
        "status": "success",
        "message": f"Default templates restored{' for ' + category if category else ''}."
    })



