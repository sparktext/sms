import re
from django.db import models
from django.utils.translation import gettext_lazy as _


class MessageTemplate(models.Model):
    """
    Reusable SMS templates for marketing broadcasts, quick replies, lead intake, and follow-ups.
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
        related_name="message_templates",
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
        help_text=_("Whether this is a system default template"),
    )
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_templates",
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
        matches = re.findall(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", self.body)
        return sorted(list(set(matches)))
