from django.contrib import admin

from .models import ConsentEvent


@admin.register(ConsentEvent)
class ConsentEventAdmin(admin.ModelAdmin):
    list_display = ["contact", "organization", "event_type", "source", "created_at"]
    list_filter = ["organization", "event_type", "source", "created_at"]
    search_fields = ["contact__phone", "contact__first_name", "contact__last_name", "proof_text"]
    readonly_fields = ["contact", "organization", "event_type", "source", "proof_text", "created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
