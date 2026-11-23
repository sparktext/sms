import logging
import re
from typing import Any

from django.db import transaction
from django.utils import timezone

from compliance.services import can_send_marketing
from contacts.models import Contact
from core.models import Organization
from messaging.models import Conversation, Message
from messaging.services import send_message

from .models import Campaign, CampaignRecipient

logger = logging.getLogger(__name__)


def render_campaign_message(
    template: str,
    contact: Contact,
    organization: Organization,
    auto_append_opt_out: bool = True,
) -> str:
    """
    Renders message template with personalized contact attributes.
    Supports:
      {{ first_name }}, {{ last_name }}, {{ phone }}, {{ business_name }}
      and any custom field (e.g. {{ stage }}).
    Ensures standard TCPA opt-out disclosure is present.
    """
    first_name = contact.first_name.strip() if contact.first_name else ""
    last_name = contact.last_name.strip() if contact.last_name else ""
    full_name = f"{first_name} {last_name}".strip() or first_name or "Valued Customer"
    business_name = organization.name.strip() if organization else "SparkText"

    context_vars = {
        "first_name": first_name or "there",
        "last_name": last_name,
        "full_name": full_name,
        "phone": contact.phone,
        "business_name": business_name,
    }

    # Add custom fields if any
    if isinstance(contact.custom_fields, dict):
        for k, v in contact.custom_fields.items():
            context_vars[k.lower()] = str(v) if v is not None else ""

    rendered = template
    for key, val in context_vars.items():
        pattern = re.compile(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", re.IGNORECASE)
        rendered = pattern.sub(val, rendered)

    # Clean up any un-matched {{ ... }} placeholders cleanly
    rendered = re.sub(r"\{\{\s*[\w\.\-_]+\s*\}\}", "", rendered).strip()

    # Append TCPA opt-out disclosure if missing
    if auto_append_opt_out:
        lower_body = rendered.lower()
        if "stop" not in lower_body:
            rendered = f"{rendered}\n\nReply STOP to opt out"

    return rendered.strip()


def resolve_campaign_contacts(
    organization: Organization,
    list_ids: list[int] = None,
    filter_criteria: dict[str, Any] = None,
):
    """
    Resolves the targeted contact queryset based on selected contact lists
    and custom field criteria (e.g. {'stage': 'Intake'}).
    Returns a distinct queryset of Contact objects belonging to the organization.
    """
    qs = Contact.objects.filter(organization=organization)

    if list_ids:
        qs = qs.filter(lists__id__in=list_ids)

    if filter_criteria and isinstance(filter_criteria, dict):
        for field_key, expected_val in filter_criteria.items():
            if field_key and expected_val is not None:
                cleaned_val = str(expected_val).strip()
                if cleaned_val:
                    # Filter matching JSON field key
                    qs = qs.filter(custom_fields__contains={field_key: cleaned_val})

    return qs.distinct()


@transaction.atomic
def build_campaign_recipients(campaign: Campaign) -> tuple[int, int]:
    """
    Populates CampaignRecipient rows for a campaign:
    - Renders personalized message for each contact.
    - Evaluates can_send_marketing:
        * Opted In -> status=PENDING
        * Opted Out -> status=SKIPPED (reason="Contact opted out")
        * Unknown -> status=SKIPPED (reason="No opt-in consent on file")
    Returns (total_recipients, eligible_count).
    """
    # Delete any existing recipient rows (in case editing a draft)
    campaign.recipients.all().delete()

    list_ids = list(campaign.lists.values_list("id", flat=True))
    contacts = resolve_campaign_contacts(
        organization=campaign.organization,
        list_ids=list_ids,
        filter_criteria=campaign.filter_criteria,
    )

    recipients_to_create = []
    eligible_count = 0
    skipped_count = 0

    for contact in contacts:
        is_opted_in = can_send_marketing(contact)
        personalized = render_campaign_message(
            template=campaign.message_template,
            contact=contact,
            organization=campaign.organization,
        )

        if not is_opted_in:
            skipped_count += 1
            if contact.sms_status == Contact.SMSStatus.OPTED_OUT:
                reason = "Contact opted out (STOP received)"
            else:
                reason = "No active opt-in consent on file"

            recipients_to_create.append(
                CampaignRecipient(
                    campaign=campaign,
                    contact=contact,
                    status=CampaignRecipient.Status.SKIPPED,
                    skip_reason=reason,
                    personalized_body=personalized,
                )
            )
        else:
            eligible_count += 1
            recipients_to_create.append(
                CampaignRecipient(
                    campaign=campaign,
                    contact=contact,
                    status=CampaignRecipient.Status.PENDING,
                    personalized_body=personalized,
                )
            )

    if recipients_to_create:
        CampaignRecipient.objects.bulk_create(recipients_to_create, batch_size=500)

    total = len(recipients_to_create)
    campaign.total_recipients = total
    campaign.skipped_count = skipped_count
    campaign.save(update_fields=["total_recipients", "skipped_count"])

    return total, eligible_count


def process_campaign_batch(campaign: Campaign, limit: int = 50) -> int:
    """
    Dispatches a batch of pending recipients for a campaign.
    Uses messaging.services.send_message with sender_type=CAMPAIGN.
    Returns the number of messages processed in this batch.
    """
    if campaign.status not in [Campaign.Status.SCHEDULED, Campaign.Status.SENDING]:
        return 0

    if campaign.status == Campaign.Status.SCHEDULED:
        campaign.status = Campaign.Status.SENDING
        campaign.started_at = timezone.now()
        campaign.save(update_fields=["status", "started_at"])

    # Atomically claim pending recipients for this batch using select_for_update
    # and mark them as QUEUED so concurrent workers or requests never process the same recipients.
    with transaction.atomic():
        pending_ids = list(
            CampaignRecipient.objects.select_for_update(skip_locked=True)
            .filter(
                campaign=campaign,
                status=CampaignRecipient.Status.PENDING,
            )
            .order_by("id")
            .values_list("id", flat=True)[:limit]
        )
        if not pending_ids:
            # Check if all recipients have finished
            remaining = CampaignRecipient.objects.filter(
                campaign=campaign,
                status__in=[CampaignRecipient.Status.PENDING, CampaignRecipient.Status.QUEUED],
            ).count()
            if remaining == 0 and campaign.status == Campaign.Status.SENDING:
                campaign.status = Campaign.Status.COMPLETED
                campaign.completed_at = timezone.now()
                campaign.save(update_fields=["status", "completed_at"])
                try:
                    from .ws_broadcast import broadcast_campaign_progress
                    broadcast_campaign_progress(campaign, [])
                except Exception:
                    pass
            return 0

        CampaignRecipient.objects.filter(id__in=pending_ids).update(
            status=CampaignRecipient.Status.QUEUED
        )

    # Fetch only the claimed QUEUED recipients
    recipients = (
        CampaignRecipient.objects.filter(
            id__in=pending_ids,
            campaign=campaign,
            status=CampaignRecipient.Status.QUEUED,
        )
        .select_related("contact")
        .order_by("id")
    )

    processed = 0
    recipient_updates = []

    for recip in recipients:
        contact = recip.contact

        # Re-check consent before dispatch (in case contact opted out since creation)
        if not can_send_marketing(contact):
            recip.status = CampaignRecipient.Status.SKIPPED
            recip.skip_reason = "Contact opted out prior to delivery"
            recip.save(update_fields=["status", "skip_reason"])
            campaign.skipped_count += 1
            processed += 1
            recipient_updates.append({
                "id": recip.id,
                "status": recip.status,
                "status_display": recip.get_status_display(),
                "sent_at": "",
                "conversation_id": None,
                "error_message": "",
                "skip_reason": recip.skip_reason,
            })
            continue

        # Get or create 2-way conversation for this contact and sender phone number
        conversation, _ = Conversation.objects.get_or_create(
            organization=campaign.organization,
            contact=contact,
            phone_number=campaign.phone_number,
            defaults={"status": Conversation.Status.OPEN, "last_campaign": campaign},
        )
        if conversation.last_campaign_id != campaign.id:
            conversation.last_campaign = campaign
            conversation.save(update_fields=["last_campaign"])

        try:
            msg = send_message(
                conversation=conversation,
                body=recip.personalized_body,
                user=campaign.created_by,
                sender_type=Message.SenderType.CAMPAIGN,
            )
            recip.message = msg
            recip.status = CampaignRecipient.Status.SENT
            recip.sent_at = timezone.now()
            recip.save(update_fields=["message", "status", "sent_at"])
            campaign.sent_count += 1

            if msg.status == Message.Status.DELIVERED:
                recip.status = CampaignRecipient.Status.DELIVERED
                recip.save(update_fields=["status"])
                campaign.delivered_count += 1

        except Exception as exc:
            logger.error("Failed to dispatch campaign message for recipient %s: %s", recip.id, exc)
            recip.status = CampaignRecipient.Status.FAILED
            recip.error_message = str(exc)
            recip.save(update_fields=["status", "error_message"])
            campaign.failed_count += 1

        processed += 1
        recipient_updates.append({
            "id": recip.id,
            "status": recip.status,
            "status_display": recip.get_status_display(),
            "sent_at": recip.sent_at.strftime("%b %d, %I:%M %p") if recip.sent_at else "",
            "conversation_id": recip.message.conversation_id if recip.message else (conversation.id if conversation else None),
            "error_message": recip.error_message,
            "skip_reason": recip.skip_reason,
        })

    # Check if all recipients have finished
    remaining = CampaignRecipient.objects.filter(
        campaign=campaign,
        status__in=[CampaignRecipient.Status.PENDING, CampaignRecipient.Status.QUEUED],
    ).count()

    if remaining == 0:
        campaign.status = Campaign.Status.COMPLETED
        campaign.completed_at = timezone.now()
        campaign.save(update_fields=["status", "completed_at", "sent_count", "delivered_count", "failed_count", "skipped_count"])
    else:
        campaign.save(update_fields=["sent_count", "delivered_count", "failed_count", "skipped_count"])

    # Broadcast real-time progress update to all WebSocket viewers on /campaigns/<id>/
    try:
        from .ws_broadcast import broadcast_campaign_progress
        broadcast_campaign_progress(campaign, recipient_updates)
    except Exception as exc:
        logger.warning("Failed to broadcast campaign progress for %s: %s", campaign.id, exc)

    return processed


def cancel_campaign(campaign: Campaign) -> bool:
    """
    Cancels an active or scheduled campaign.
    Marks any remaining pending recipients as skipped.
    """
    if campaign.status in [Campaign.Status.COMPLETED, Campaign.Status.CANCELLED]:
        return False

    campaign.status = Campaign.Status.CANCELLED
    campaign.save(update_fields=["status"])

    CampaignRecipient.objects.filter(
        campaign=campaign,
        status__in=[CampaignRecipient.Status.PENDING, CampaignRecipient.Status.QUEUED],
    ).update(
        status=CampaignRecipient.Status.SKIPPED,
        skip_reason="Campaign cancelled by user",
    )

    try:
        from .ws_broadcast import broadcast_campaign_progress
        broadcast_campaign_progress(campaign)
    except Exception as exc:
        logger.warning("Failed to broadcast campaign cancellation for %s: %s", campaign.id, exc)

    return True


def check_and_dispatch_due_campaigns(organization: Organization = None, limit: int = 50) -> int:
    """
    Checks for any SCHEDULED campaigns whose scheduled_at has arrived (or is null),
    and dispatches them. Safe to call on page load or from background tasks.
    """
    from django.db.models import Q
    now = timezone.now()
    qs = Campaign.objects.filter(
        Q(status=Campaign.Status.SCHEDULED, scheduled_at__lte=now)
        | Q(status=Campaign.Status.SCHEDULED, scheduled_at__isnull=True)
        | Q(status=Campaign.Status.SENDING)
    )
    if organization:
        qs = qs.filter(organization=organization)

    total_dispatched = 0
    for camp in qs.order_by("created_at")[:10]:
        try:
            total_dispatched += process_campaign_batch(camp, limit=limit)
        except Exception as exc:
            logger.error("Error processing due campaign #%s: %s", camp.id, exc)
    return total_dispatched


DEFAULT_CAMPAIGN_TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "Auto Insurance Annual Review",
        "category": "marketing",
        "body": "Hi {{ first_name }}, this is {{ business_name }}. Drivers in your area are saving an average of $450/year by reviewing their auto coverage. Reply YES to compare your rates today! Reply STOP to opt out.",
    },
    {
        "name": "New Quote Request Follow-up",
        "category": "follow_up",
        "body": "Hi {{ first_name }}! Thanks for requesting a quote from {{ business_name }}. I am preparing your customized rate options right now. What time today works best for a quick 5-min review? Reply STOP to opt out.",
    },
    {
        "name": "Office Hours & Location",
        "category": "quick_reply",
        "body": "Our office is located at 475 El Camino Real, Suite 301, Millbrae, CA 94030. We are open Mon - Fri, 9:00 AM - 6:00 PM PST. Feel free to call us anytime!",
    },
    {
        "name": "Consultation Appointment Reminder",
        "category": "transactional",
        "body": "Appointment Reminder: Hi {{ first_name }}, your insurance consultation with {{ business_name }} is set for tomorrow. Reply C to confirm or RESCHEDULE if you need to pick a new time. Reply STOP to opt out.",
    },
    {
        "name": "Home & Auto Bundle Promotion",
        "category": "marketing",
        "body": "Exclusive VIP Deal: Hi {{ first_name }}, bundle your home and auto policies with {{ business_name }} this month to unlock up to 25% in multi-policy savings! Reply REVIEW to see your numbers. Reply STOP to opt out.",
    },
    {
        "name": "Quote In Progress Update",
        "category": "quick_reply",
        "body": "Thank you for your patience! A licensed agent from our team is reviewing your details to prepare your customized quotes and will message you shortly.",
    },
    {
        "name": "Quick Phone Call Invitation",
        "category": "quick_reply",
        "body": "Would you prefer a quick 5-minute phone call to go over the policy options and rates together? Let us know what time works best for you!",
    },
]


def ensure_default_campaign_templates(organization):
    """
    Ensures starter categories and templates are initialized ONCE per organization.
    If the organization later deletes or renames any categories (including defaults),
    we respect those changes permanently instead of resurrecting deleted defaults.
    """
    from core.models import Organization

    from .models import CampaignTemplate, TemplateCategory

    # Check fresh DB flag to avoid stale in-memory instances re-seeding
    is_seeded = Organization.objects.filter(id=organization.id, templates_seeded=True).exists()
    if is_seeded:
        return

    has_templates = CampaignTemplate.objects.filter(organization=organization).exists()
    has_categories = TemplateCategory.objects.filter(organization=organization).exists()

    if not has_templates and not has_categories:
        # Brand new workspace: create starter categories
        for idx, (slug, name) in enumerate(CampaignTemplate.Category.choices):
            TemplateCategory.objects.get_or_create(
                organization=organization,
                slug=slug,
                defaults={"name": str(name), "order": idx + 1},
            )
        # Create starter templates
        created_list = []
        for item in DEFAULT_CAMPAIGN_TEMPLATES:
            created_list.append(
                CampaignTemplate(
                    organization=organization,
                    name=item["name"],
                    category=item["category"],
                    body=item["body"],
                    is_default=True,
                )
            )
        CampaignTemplate.objects.bulk_create(created_list)
    elif has_templates and not has_categories:
        # Backfill existing workspace categories once
        standard_labels = dict(CampaignTemplate.Category.choices)
        existing_slugs = list(
            CampaignTemplate.objects.filter(organization=organization)
            .values_list("category", flat=True)
            .distinct()
        )
        order_idx = 1
        for slug, label in CampaignTemplate.Category.choices:
            TemplateCategory.objects.get_or_create(
                organization=organization,
                slug=slug,
                defaults={"name": str(label), "order": order_idx},
            )
            order_idx += 1
        for slug in existing_slugs:
            if slug and slug not in standard_labels:
                TemplateCategory.objects.get_or_create(
                    organization=organization,
                    slug=slug,
                    defaults={"name": slug.replace("_", " ").title(), "order": order_idx},
                )
                order_idx += 1

    Organization.objects.filter(id=organization.id).update(templates_seeded=True)
    organization.templates_seeded = True


# Alias
ensure_default_templates = ensure_default_campaign_templates

