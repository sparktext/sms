from django.contrib import admin
from .models import Campaign, CampaignRecipient


class CampaignRecipientInline(admin.TabularInline):
    model = CampaignRecipient
    extra = 0
    readonly_fields = ("contact", "status", "skip_reason", "sent_at", "error_message")
    can_delete = False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "organization",
        "status",
        "total_recipients",
        "sent_count",
        "delivered_count",
        "skipped_count",
        "scheduled_at",
        "created_at",
    )
    list_filter = ("status", "organization")
    search_fields = ("name", "organization__name")
    readonly_fields = (
        "total_recipients",
        "sent_count",
        "delivered_count",
        "failed_count",
        "skipped_count",
        "opt_out_count",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    )
    inlines = [CampaignRecipientInline]


@admin.register(CampaignRecipient)
class CampaignRecipientAdmin(admin.ModelAdmin):
    list_display = ("campaign", "contact", "status", "skip_reason", "sent_at")
    list_filter = ("status", "campaign__organization")
    search_fields = ("contact__phone", "contact__first_name", "contact__last_name", "campaign__name")
    readonly_fields = ("campaign", "contact", "message", "status", "skip_reason", "sent_at", "created_at")

