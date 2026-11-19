from django.db import models
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from django.db.models.signals import m2m_changed
from django.dispatch import receiver
import re

RESERVED_FIELD_NAMES = frozenset([
    "phone", "first_name", "last_name", "email", "timezone", "vendor", "source",
    "custom_fields", "external_ids", "sms_status",
    "created_at", "updated_at", "id", "pk", "organization", "lists"
])

KEY_REGEX = re.compile(r"^[a-z][a-z0-9_]*$")


class ContactField(models.Model):
    """
    Approved extra field per organization (e.g. Pizza Palace + favorite_topping).
    """
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="contact_fields",
    )
    key = models.CharField(
        max_length=64,
        help_text=_("snake_case identifier starting with a letter"),
    )
    label = models.CharField(
        max_length=128,
        help_text=_("Display label for the business"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "key"],
                name="unique_organization_field_key",
            )
        ]
        ordering = ["key"]

    def clean(self):
        super().clean()
        if self.key:
            self.key = self.key.strip().lower()
            if not KEY_REGEX.match(self.key):
                raise ValidationError({
                    "key": _("Key must start with a lowercase letter and contain only lowercase letters, digits, and underscores.")
                })
            if self.key in RESERVED_FIELD_NAMES:
                raise ValidationError({
                    "key": _("'%(key)s' is a reserved field name and cannot be used as a custom field key.") % {"key": self.key}
                })

    def save(self, *args, **kwargs):
        if self.pk:
            orig = ContactField.objects.filter(pk=self.pk).values("key", "organization_id").first()
            if orig:
                if orig["key"] != self.key:
                    raise ValidationError({"key": _("ContactField key is read-only after creation.")})
                if orig["organization_id"] != self.organization_id:
                    raise ValidationError({"organization": _("Organization cannot be changed after creation.")})
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.label} ({self.key}) - {self.organization.name}"


class Contact(models.Model):
    """
    A single customer / lead phone contact belonging to an Organization.
    """
    class Source(models.TextChoices):
        ADMIN = "admin", _("Admin")
        MANUAL = "manual", _("Manual")
        API = "api", _("API")
        CSV = "csv", _("CSV")
        INBOUND = "inbound", _("Inbound")

    class SMSStatus(models.TextChoices):
        UNKNOWN = "unknown", _("No consent")
        OPTED_IN = "opted_in", _("Opted In")
        OPTED_OUT = "opted_out", _("Opted Out")

    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="contacts",
    )
    phone = models.CharField(
        max_length=20,
        help_text=_("E.164 normalized phone number (e.g. +16502530000)"),
    )
    first_name = models.CharField(max_length=100, blank=True, default="")
    last_name = models.CharField(max_length=100, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    source = models.CharField(
        max_length=100,
        blank=False,
        default="Direct",
        help_text=_("Lead source or provider (compulsory)"),
    )
    custom_fields = models.JSONField(default=dict, blank=True)
    external_ids = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Reserved for future CRM and Vicidial IDs"),
    )
    sms_status = models.CharField(
        max_length=20,
        choices=SMSStatus.choices,
        default=SMSStatus.UNKNOWN,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "phone"],
                name="unique_organization_phone",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "sms_status"],
                name="idx_contact_org_sms_status",
            )
        ]
        ordering = ["-created_at"]

    def clean(self):
        super().clean()
        from .utils import normalize_phone
        if self.phone:
            self.phone = normalize_phone(self.phone)
        if not self.source or not str(self.source).strip():
            raise ValidationError({"source": _("Source is compulsory.")})

    @property
    def vendor(self):
        """Backwards compatibility alias for source."""
        return self.source

    @vendor.setter
    def vendor(self, value):
        self.source = value

    def save(self, *args, **kwargs):
        if self.pk:
            orig = Contact.objects.filter(pk=self.pk).values("organization_id").first()
            if orig and orig["organization_id"] != self.organization_id:
                raise ValidationError({"organization": _("Organization cannot be changed after creation.")})

        from .utils import normalize_phone
        if self.phone:
            self.phone = normalize_phone(self.phone)
        super().save(*args, **kwargs)

    def get_custom_field(self, key, default=""):
        if isinstance(self.custom_fields, dict):
            return self.custom_fields.get(key, default)
        return default

    def __str__(self):
        full = f"{self.first_name} {self.last_name}".strip()
        if full:
            return f"{full} ({self.phone})"
        return self.phone


class ContactList(models.Model):
    """
    A list of contacts belonging to an Organization for campaigns / segmentation.
    """
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="contact_lists",
    )
    name = models.CharField(max_length=200)
    contacts = models.ManyToManyField(
        Contact,
        related_name="lists",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="unique_organization_list_name",
            )
        ]
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if self.pk:
            orig = ContactList.objects.filter(pk=self.pk).values("organization_id").first()
            if orig and orig["organization_id"] != self.organization_id:
                raise ValidationError({"organization": _("Organization cannot be changed after creation.")})
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} - {self.organization.name}"


class ImportBatch(models.Model):
    """
    Tracks CSV import runs for auditing and optional consent attestation.
    """
    class Status(models.TextChoices):
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")

    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="import_batches",
    )
    filename = models.CharField(max_length=255)
    total_rows = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    consent_attested = models.BooleanField(
        default=False,
        help_text=_("Whether the user attested prior express consent for all contacts in this batch"),
    )
    attestation_text = models.TextField(
        blank=True,
        default="",
        help_text=_("Proof text or legal disclaimer agreed to at upload"),
    )
    contact_list = models.ForeignKey(
        ContactList,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_batches",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = _("Import Batches")

    def __str__(self):
        return f"ImportBatch {self.id} ({self.filename}) - {self.organization.name}"


@receiver(m2m_changed, sender=ContactList.contacts.through)
def validate_contact_list_organization_m2m(sender, instance, action, pk_set, **kwargs):
    """
    Ensure ContactList cannot mix organizations.
    Any contact added to a ContactList must belong to the exact same organization.
    """
    if action in ("pre_add",):
        if pk_set:
            mismatched = Contact.objects.filter(pk__in=pk_set).exclude(
                organization_id=instance.organization_id
            ).exists()
            if mismatched:
                raise ValidationError(_("All contacts in a list must belong to the list's organization."))
