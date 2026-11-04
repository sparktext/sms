import json
from datetime import datetime
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_http_methods
from django.http import JsonResponse, Http404
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.utils import timezone
from django.contrib import messages
from django.db.models import Count, Q

from core.models import Organization
from contacts.models import Contact, ContactList, ContactField
from compliance.services import can_send_marketing
from messaging.models import PhoneNumber
from templates_app.models import MessageTemplate
from templates_app.defaults import ensure_default_templates
from .models import Campaign, CampaignRecipient
from .services import (
    render_campaign_message,
    resolve_campaign_contacts,
    build_campaign_recipients,
    process_campaign_batch,
    cancel_campaign,
    check_and_dispatch_due_campaigns,
)


@login_required
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
    total_sent = sum(c.sent_count for c in all_campaigns)
    total_delivered = sum(c.delivered_count for c in all_campaigns)
    overall_delivery_rate = int((total_delivered / total_sent * 100)) if total_sent > 0 else 100

    paginator = Paginator(campaigns_qs, 15)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "status_filter": status_filter,
        "q": q,
        "total_campaigns": total_campaigns,
        "total_sent": total_sent,
        "total_delivered": total_delivered,
        "overall_delivery_rate": overall_delivery_rate,
        "statuses": Campaign.Status.choices,
    }
    return render(request, "campaigns/campaign_list.html", context)


@login_required
def campaign_create(request):
    """
    Campaign composer wizard:
    - Multi-list audience targeting + custom field filter (e.g. stage="Intake")
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

    # Distinct values for 'stage' if stage field exists
    stage_options = []
    if custom_fields.filter(key="stage").exists():
        # Get unique stages from contacts
        contacts_with_stage = Contact.objects.filter(organization=org).exclude(custom_fields__stage__isnull=True)
        raw_stages = set()
        for c in contacts_with_stage[:300]:
            st = c.custom_fields.get("stage")
            if st:
                raw_stages.add(str(st))
        stage_options = sorted(list(raw_stages)) if raw_stages else ["Intake", "Qualified", "Unqualified"]
    else:
        stage_options = ["Intake", "Qualified"]

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        phone_number_id = request.POST.get("phone_number_id")
        message_template = request.POST.get("message_template", "").strip()
        timing = request.POST.get("timing", "immediate").strip().lower()
        scheduled_at_str = request.POST.get("scheduled_at", "").strip()

        # Multi-selected list IDs
        selected_list_ids = request.POST.getlist("lists")
        selected_list_ids = [int(lid) for lid in selected_list_ids if str(lid).isdigit()]

        # Filter criteria: stage and custom fields
        filter_criteria = {}
        stage_val = request.POST.get("filter_stage", "").strip()
        if stage_val and stage_val != "__all__":
            filter_criteria["stage"] = stage_val

        # Validation
        if not name:
            messages.error(request, "Please enter a campaign name.")
            return redirect("campaigns:create")

        if not message_template:
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

        # Create Campaign
        campaign = Campaign.objects.create(
            organization=org,
            created_by=request.user,
            name=name,
            phone_number=phone_num,
            message_template=message_template,
            filter_criteria=filter_criteria,
            status=Campaign.Status.SCHEDULED,
            scheduled_at=scheduled_at,
        )

        if selected_list_ids:
            lists_qs = ContactList.objects.filter(id__in=selected_list_ids, organization=org)
            campaign.lists.set(lists_qs)

        # Build recipient records & evaluate marketing consent
        total, eligible = build_campaign_recipients(campaign)

        if timing == "immediate":
            # Kick off first batch right away
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

    context = {
        "phone_numbers": phone_numbers,
        "contact_lists": contact_lists,
        "custom_fields": custom_fields,
        "stage_options": stage_options,
        "saved_templates": saved_templates,
        "selected_template": selected_template,
        "now_iso": timezone.now().strftime("%Y-%m-%dT%H:%M"),
    }
    return render(request, "campaigns/campaign_create.html", context)


@login_required
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

    recipients_qs = campaign.recipients.select_related("contact", "message").order_by("id")

    status_filter = request.GET.get("status", "").strip().lower()
    q = request.GET.get("q", "").strip()

    if status_filter in CampaignRecipient.Status.values:
        recipients_qs = recipients_qs.filter(status=status_filter)

    if q:
        recipients_qs = recipients_qs.filter(
            Q(contact__phone__icontains=q)
            | Q(contact__first_name__icontains=q)
            | Q(contact__last_name__icontains=q)
        )

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
            messages.info(request, f"Campaign '{campaign.name}' was cancelled. Remaining messages stopped.")
        else:
            messages.warning(request, "Campaign cannot be cancelled in its current state.")

    elif action == "send_now":
        if campaign.status in [Campaign.Status.SCHEDULED, Campaign.Status.DRAFT]:
            campaign.status = Campaign.Status.SENDING
            campaign.scheduled_at = timezone.now()
            campaign.save(update_fields=["status", "scheduled_at"])
            process_campaign_batch(campaign, limit=50)
            messages.success(request, f"Campaign '{campaign.name}' started sending.")

    elif action == "delete":
        if campaign.status in [Campaign.Status.DRAFT, Campaign.Status.CANCELLED]:
            name = campaign.name
            campaign.delete()
            messages.success(request, f"Campaign '{name}' deleted.")
            return redirect("campaigns:list")
        else:
            messages.error(request, "Only draft or cancelled campaigns can be deleted.")

    return redirect("campaigns:detail", campaign_id=campaign.id)


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

    stage_val = request.GET.get("stage", "").strip()
    filter_criteria = {}
    if stage_val and stage_val != "__all__":
        filter_criteria["stage"] = stage_val

    contacts = resolve_campaign_contacts(
        organization=org,
        list_ids=list_ids,
        filter_criteria=filter_criteria,
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

