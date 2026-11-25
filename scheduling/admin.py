from django.contrib import admin

from .models import CalendarEvent


@admin.register(CalendarEvent)
class CalendarEventAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "kind", "status", "start_at", "end_at", "organization")
    list_filter = ("kind", "status", "organization")
    search_fields = ("title", "notes", "user__email")
    raw_id_fields = ("user", "contact", "created_by")
