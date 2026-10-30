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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Phone Number")
        verbose_name_plural = _("Phone Numbers")

    def clean(self):
        super().clean()
        from contacts.utils import normalize_phone
        if self.e164:
            self.e164 = normalize_phone(self.e164)

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        label_part = f" ({self.label})" if self.label else ""
        return f"{self.e164}{label_part} - {self.organization.name}"


class Conversation(models.Model):
    """
    A 2-way message thread between an organization's PhoneNumber and a Contact.
    Unique on (organization, contact, phone_number).
    """
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        NEEDS_HUMAN = "needs_human", _("Needs Human")
        CLOSED = "closed", _("Closed")

    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    contact = models.ForeignKey(
        "contacts.Contact",
        on_delete=models.PROTECT,
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
    created_at = models.DateTimeField(auto_now_add=True)

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
