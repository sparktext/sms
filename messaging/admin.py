from django.contrib import admin
from .models import PhoneNumber, Conversation, Message


@admin.register(PhoneNumber)
class PhoneNumberAdmin(admin.ModelAdmin):
    list_display = ["e164", "organization", "label", "is_active", "twilio_sid", "created_at"]
    list_filter = ["is_active", "organization"]
    search_fields = ["e164", "label", "twilio_sid", "organization__name"]
    readonly_fields = ["created_at"]


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    can_delete = False
    readonly_fields = [
        "direction",
        "sender_type",
        "sent_by",
        "body",
        "status",
        "provider_message_sid",
        "error_code",
        "error_message",
        "num_segments",
        "created_at",
    ]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ["id", "organization", "contact", "phone_number", "status", "unread_count", "last_message_at"]
    list_filter = ["status", "organization"]
    search_fields = ["contact__phone", "contact__first_name", "contact__last_name", "phone_number__e164"]
    readonly_fields = [
        "organization",
        "contact",
        "phone_number",
        "last_message_at",
        "last_inbound_at",
        "unread_count",
        "created_at",
    ]
    inlines = [MessageInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "conversation",
        "organization",
        "direction",
        "sender_type",
        "status",
        "provider_message_sid",
        "created_at",
    ]
    list_filter = ["direction", "sender_type", "status", "organization"]
    search_fields = [
        "body",
        "provider_message_sid",
        "conversation__contact__phone",
        "conversation__phone_number__e164",
    ]
    readonly_fields = [
        "conversation",
        "organization",
        "direction",
        "sender_type",
        "sent_by",
        "body",
        "status",
        "provider",
        "provider_message_sid",
        "error_code",
        "error_message",
        "num_segments",
        "created_at",
        "sent_at",
        "delivered_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
