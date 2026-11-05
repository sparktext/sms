from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError


class Campaign(models.Model):
    """
    A bulk or targeted SMS broadcast campaign sent to contacts.
    Supports targeting multiple contact lists, filtering by custom fields/stage,
    and immediate or future scheduled delivery.
    """
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SCHEDULED = "scheduled", _("Scheduled")
        SENDING = "sending", _("Sending")
        COMPLETED = "completed", _("Completed")
        CANCELLED = "cancelled", _("Cancelled")

    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="campaigns",
    )
    name = models.CharField(
        max_length=200,
        help_text=_("Campaign name or objective (e.g. 'Fall Insurance Review')"),
    )
    phone_number = models.ForeignKey(
        "messaging.PhoneNumber",
        on_delete=models.PROTECT,
        related_name="campaigns",
        help_text=_("The business phone number sending this campaign"),
    )
    message_template = models.TextField(
        help_text=_("Message text with optional merge tags like {{ first_name }}, {{ last_name }}"),
    )
    lists = models.ManyToManyField(
        "contacts.ContactList",
        blank=True,
        related_name="campaigns",
        help_text=_("Target contact lists"),
    )
    filter_criteria = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Optional custom field filters (e.g. {'stage': 'Intake'})"),
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    scheduled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Scheduled date & time to start sending (empty = immediate)"),
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_campaigns",
    )

    # Denormalized counter metrics
    total_recipients = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    delivered_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    opt_out_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "status"], name="idx_camp_org_status"),
            models.Index(fields=["status", "scheduled_at"], name="idx_camp_status_sched"),
        ]

    def clean(self):
        super().clean()
        if self.phone_number_id and self.organization_id:
            if self.phone_number.organization_id != self.organization_id:
                raise ValidationError({"phone_number": _("Sender phone number must belong to your business.")})

    @property
    def progress_percent(self) -> int:
        if not self.total_recipients:
            return 0
        processed = self.sent_count + self.failed_count + self.skipped_count
        return min(100, int((processed / self.total_recipients) * 100))

    @property
    def delivery_rate(self) -> int:
        if not self.sent_count:
            return 0
        return int((self.delivered_count / self.sent_count) * 100)

    def __str__(self):
        return f"{self.name} ({self.get_status_display()}) - {self.organization.name}"


class CampaignRecipient(models.Model):
    """
    A single target recipient within a campaign.
    Tracks personalized message text, compliance checks, and delivery outcomes.
    """
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        QUEUED = "queued", _("Queued")
        SENT = "sent", _("Sent")
        DELIVERED = "delivered", _("Delivered")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")

    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="recipients",
    )
    contact = models.ForeignKey(
        "contacts.Contact",
        on_delete=models.CASCADE,
        related_name="campaign_receipts",
    )
    message = models.ForeignKey(
        "messaging.Message",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="campaign_recipient",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    skip_reason = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_("Reason if skipped (e.g. 'Not opted in', 'Opted out')"),
    )
    personalized_body = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "contact"],
                name="unique_campaign_recipient",
            )
        ]
        indexes = [
            models.Index(fields=["campaign", "status"], name="idx_recip_camp_status"),
        ]

    def __str__(self):
        return f"Recipient {self.contact.phone} for Campaign #{self.campaign_id} ({self.status})"


class CampaignTemplate(models.Model):
    """
    Reusable message templates for broadcast campaigns, follow-ups, and quick replies.
    Supports merge tags like {{ first_name }}, {{ last_name }}, {{ business_name }}, {{ phone }}.
    """
    class Category(models.TextChoices):
        MARKETING = "marketing", _("Marketing & Broadcasts")
        QUICK_REPLY = "quick_reply", _("Quick Replies")
        FOLLOW_UP = "follow_up", _("Lead Follow-up")
        TRANSACTIONAL = "transactional", _("Transactional & Reminders")

    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="campaign_templates",
    )
    name = models.CharField(max_length=150)
    category = models.CharField(
        max_length=30,
        choices=Category.choices,
        default=Category.MARKETING,
    )
    body = models.TextField(
        help_text=_("Message copy supporting merge tags like {{ first_name }}, {{ last_name }}, {{ business_name }}"),
    )
    is_default = models.BooleanField(
        default=False,
        help_text=_("Whether this is a system default starter template"),
    )
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_campaign_templates",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "name"]
        indexes = [
            models.Index(fields=["organization", "category"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"

    @property
    def character_count(self) -> int:
        return len(self.body)

    @property
    def segment_count(self) -> int:
        length = len(self.body)
        if length <= 160:
            return 1 if length > 0 else 0
        return (length + 152) // 153

    @property
    def merge_tags(self) -> list:
        import re
        matches = re.findall(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", self.body)
        return sorted(list(set(matches)))


MessageTemplate = CampaignTemplate

