from django.db import models
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class ConsentEvent(models.Model):
    """
    An append-only audit ledger recording every opt-in and opt-out event.
    Rows must never be edited or deleted through the application.
    """
    class EventType(models.TextChoices):
        OPT_IN = "opt_in", _("Opt In")
        OPT_OUT = "opt_out", _("Opt Out")

    class Source(models.TextChoices):
        KEYWORD = "keyword", _("Keyword")
        WEB_FORM = "web_form", _("Web Form")
        CSV_ATTESTATION = "csv_attestation", _("CSV Attestation")
        MANUAL = "manual", _("Manual")
        API = "api", _("API")
        PROVIDER = "provider", _("Provider")

    contact = models.ForeignKey(
        "contacts.Contact",
        on_delete=models.CASCADE,
        related_name="consent_events",
    )
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="consent_events",
    )
    event_type = models.CharField(
        max_length=20,
        choices=EventType.choices,
    )
    source = models.CharField(
        max_length=30,
        choices=Source.choices,
    )
    proof_text = models.TextField(
        help_text=_("The exact message, timestamp, IP, or attestation evidence"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Consent Event")
        verbose_name_plural = _("Consent Events")

    def save(self, *args, **kwargs):
        # Enforce append-only ledger constraint at the model layer
        if self.pk:
            raise ValidationError(_("ConsentEvent entries are immutable and cannot be updated."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("ConsentEvent entries are immutable and cannot be deleted."))

    def __str__(self):
        return f"{self.event_type.upper()} ({self.source}) - {self.contact.phone} [{self.created_at}]"
