from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class PhoneNumber(models.Model):
    """
    A provisioned or imported phone number belonging to an Organization.
    Globally unique: a number belongs to exactly one organization.
    """
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="phone_numbers",
    )
    e164 = models.CharField(
        max_length=20,
        unique=True,
        help_text=_("E.164 normalized phone number (e.g. +16502530000)"),
    )
    twilio_sid = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_("Twilio IncomingPhoneNumber SID (PN...)"),
    )
    label = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text=_("Descriptive label (e.g. 'Main Support', 'Sales')"),
    )
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(
        default=False,
        help_text=_("Primary default sending line for this organization"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_default", "-created_at"]
        verbose_name = _("Phone Number")
        verbose_name_plural = _("Phone Numbers")

    def clean(self):
        super().clean()
        from contacts.utils import normalize_phone
        if self.e164:
            self.e164 = normalize_phone(self.e164)

    def save(self, *args, **kwargs):
        self.clean()
        if self.is_default and self.organization_id:
            PhoneNumber.objects.filter(
                organization_id=self.organization_id,
                is_default=True,
            ).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

    @classmethod
    def get_default_for_org(cls, organization):
        """Returns the primary default active phone number, falling back to the first active number."""
        return (
            cls.objects.filter(organization=organization, is_active=True, is_default=True).first()
            or cls.objects.filter(organization=organization, is_active=True).order_by("-id").first()
        )

    def __str__(self):
        label_part = f" ({self.label})" if self.label else ""
        default_part = " [Default]" if self.is_default else ""
        return f"{self.e164}{label_part}{default_part} - {self.organization.name}"


class Conversation(models.Model):
    """
    A 2-way message thread between an organization's PhoneNumber and a Contact.
    Unique on (organization, contact, phone_number).
    """
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        NEEDS_HUMAN = "needs_human", _("Needs Human")
        CLOSED = "closed", _("Closed")

    class AIMode(models.TextChoices):
        DEFAULT = "default", _("Default (Use Business Settings)")
        AUTO = "auto", _("Auto (AI Direct Replies)")
        SUGGEST = "suggest", _("Suggest (Drafts Only)")
        OFF = "off", _("Off (Human Takeover)")

    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    contact = models.ForeignKey(
        "contacts.Contact",
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    phone_number = models.ForeignKey(
        PhoneNumber,
        on_delete=models.PROTECT,
        related_name="conversations",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
    )
    assigned_to = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_conversations",
    )
    last_message_at = models.DateTimeField(default=timezone.now)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    unread_count = models.PositiveIntegerField(default=0)
    qualification_answers = models.JSONField(default=dict, blank=True)
    ai_draft_reply = models.TextField(blank=True, default="")
    handoff_reason = models.CharField(max_length=255, blank=True, default="")
    ai_mode = models.CharField(
        max_length=15,
        choices=AIMode.choices,
        default=AIMode.DEFAULT,
        help_text=_("Per-conversation AI mode override"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def get_effective_ai_mode(self, config=None) -> str:
        """Returns the conversation's active AI mode, falling back to organization config."""
        if self.ai_mode and self.ai_mode != self.AIMode.DEFAULT:
            return self.ai_mode
        if config:
            return config.mode
        cfg = getattr(self.organization, "ai_config", None)
        return cfg.mode if cfg else "off"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "contact", "phone_number"],
                name="unique_organization_contact_phone_number",
            )
        ]
        ordering = ["-last_message_at"]
        indexes = [
            models.Index(fields=["organization", "status"], name="idx_conv_org_status"),
            models.Index(fields=["organization", "-last_message_at"], name="idx_conv_org_last_msg"),
        ]

    def clean(self):
        super().clean()
        if self.contact_id and self.organization_id:
            if self.contact.organization_id != self.organization_id:
                raise ValidationError({"contact": _("Contact must belong to the same organization as the conversation.")})
        if self.phone_number_id and self.organization_id:
            if self.phone_number.organization_id != self.organization_id:
                raise ValidationError({"phone_number": _("PhoneNumber must belong to the same organization as the conversation.")})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Conv with {self.contact.phone} via {self.phone_number.e164} ({self.organization.name})"


class Message(models.Model):
    """
    A single SMS message within a conversation.
    Append-only except for provider status callback updates.
    """
    class Direction(models.TextChoices):
        INBOUND = "inbound", _("Inbound")
        OUTBOUND = "outbound", _("Outbound")

    class SenderType(models.TextChoices):
        CUSTOMER = "customer", _("Customer")
        AI = "ai", _("AI")
        HUMAN = "human", _("Human")
        SYSTEM = "system", _("System")
        CAMPAIGN = "campaign", _("Campaign")

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        SENDING = "sending", _("Sending")
        SENT = "sent", _("Sent")
        DELIVERED = "delivered", _("Delivered")
        UNDELIVERED = "undelivered", _("Undelivered")
        FAILED = "failed", _("Failed")
        RECEIVED = "received", _("Received")

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="messages",
    )
    direction = models.CharField(
        max_length=10,
        choices=Direction.choices,
    )
    sender_type = models.CharField(
        max_length=10,
        choices=SenderType.choices,
    )
    sent_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_messages",
    )
    body = models.TextField(max_length=1600)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    provider = models.CharField(
        max_length=30,
        default="twilio",
    )
    provider_message_sid = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
    )
    error_code = models.CharField(
        max_length=32,
        blank=True,
        default="",
    )
    error_message = models.TextField(
        blank=True,
        default="",
    )
    num_segments = models.PositiveSmallIntegerField(null=True, blank=True)
    is_ai_assisted = models.BooleanField(
        default=False,
        help_text=_("Whether this outbound message was drafted or suggested by AI"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["conversation", "created_at"], name="idx_msg_conv_created"),
            models.Index(fields=["organization", "-created_at"], name="idx_msg_org_created"),
        ]

    def clean(self):
        super().clean()
        if self.conversation_id and self.organization_id:
            if self.conversation.organization_id != self.organization_id:
                raise ValidationError({"organization": _("Message organization must match conversation organization.")})

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Messages cannot be deleted."))

    def __str__(self):
        return f"{self.direction.upper()} ({self.status}) - {self.conversation.contact.phone} [{self.created_at}]"


class AIAssistantConfig(models.Model):
    """
    Per-organization AI Assistant settings.
    Controls whether the AI is OFF, in SUGGEST mode, or in AUTO mode.
    Holds business knowledge, custom tone, safety rules, and qualification questions.
    """
    class Mode(models.TextChoices):
        OFF = "off", _("Off")
        SUGGEST = "suggest", _("Suggest (Draft for Human Approval)")
        AUTO = "auto", _("Auto (Directly Send AI Replies)")

    organization = models.OneToOneField(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="ai_config",
    )
    mode = models.CharField(
        max_length=15,
        choices=Mode.choices,
        default=Mode.OFF,
    )
    business_name = models.CharField(max_length=120, blank=True, default="")
    business_description = models.TextField(blank=True, default="")
    knowledge = models.TextField(
        blank=True,
        default="",
        help_text=_("Business knowledge base: services, office hours, FAQ, policies"),
    )
    tone = models.CharField(
        max_length=100,
        blank=True,
        default="friendly, professional, concise",
    )
    qualification_questions = models.JSONField(
        default=list,
        blank=True,
        help_text=_("List of qualification questions: [{key, question, required}]"),
    )
    never_say = models.TextField(
        blank=True,
        default="",
        help_text=_("Strict negative rules/phrases the AI must never mention (one per line)"),
    )
    handoff_rules = models.TextField(
        blank=True,
        default="",
        help_text=_("Custom rules describing when to hand off to a human agent"),
    )
    max_reply_chars = models.PositiveIntegerField(
        default=320,
        help_text=_("Maximum character length for AI reply (default 320 for ~2 SMS segments)"),
    )
    max_ai_replies_per_hour_per_contact = models.PositiveIntegerField(
        default=6,
        help_text=_("Safety rate limit cap per contact per rolling hour"),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("AI Assistant Configuration")
        verbose_name_plural = _("AI Assistant Configurations")

    def __str__(self):
        return f"AI Config ({self.mode}) - {self.organization.name}"


class AIJob(models.Model):
    """
    Background job queue for processing incoming SMS messages through the AI Assistant.
    Supports a short debounce window (e.g. 4 seconds) to collapse multiple fast incoming texts.
    """
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        RUNNING = "running", _("Running")
        DONE = "done", _("Done")
        FAILED = "failed", _("Failed")

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="ai_jobs",
    )
    trigger_message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="ai_trigger_jobs",
    )
    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    run_after = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["run_after", "created_at"]
        indexes = [
            models.Index(fields=["status", "run_after"], name="idx_aijob_status_runafter"),
        ]

    def __str__(self):
        return f"AIJob #{self.id} [{self.status}] conv={self.conversation_id} msg={self.trigger_message_id}"


class AIReply(models.Model):
    """
    Comprehensive audit ledger of all AI generation events, including skipped triggers,
    draft creations, auto-sends, human handoffs, tokens, and response latencies.
    """
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SENT = "sent", _("Sent")
        BLOCKED = "blocked", _("Blocked")
        HANDED_OFF = "handed_off", _("Handed Off")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="ai_replies",
    )
    trigger_message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="ai_replies_triggered",
    )
    job = models.ForeignKey(
        AIJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replies",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    skip_reason = models.CharField(max_length=255, blank=True, default="")
    model_name = models.CharField(max_length=100, blank=True, default="")
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    queue_wait_ms = models.PositiveIntegerField(default=0)
    raw_output = models.TextField(blank=True, default="")
    parsed_output = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    resulting_message = models.ForeignKey(
        Message,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_reply_origin",
    )
    cost = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("AI Reply")
        verbose_name_plural = _("AI Replies")

    def __str__(self):
        return f"AIReply #{self.id} [{self.status}] conv={self.conversation_id}"

