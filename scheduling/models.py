from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class CalendarEvent(models.Model):
    """One entry on a team member's calendar. Times are stored in UTC and shown in the viewer's time zone."""

    class Kind(models.TextChoices):
        CALL = "call", "Call"
        FOLLOW_UP = "follow_up", "Follow-up"
        MEETING = "meeting", "Meeting"
        TASK = "task", "Task"
        BUSY = "busy", "Busy / time off"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        COMPLETED = "completed", "Completed"
        MISSED = "missed", "Missed"
        CANCELLED = "cancelled", "Cancelled"

    class Source(models.TextChoices):
        MANUAL = "manual", "Added by a person"
        AI = "ai", "Booked by Gemma"

    # Heads-up options (minutes before the start). None = no reminders at all; 0 = only when it starts.
    REMINDER_CHOICES = [(None, "No reminder"), (0, "When it starts"), (5, "5 minutes before"),
                        (10, "10 minutes before"), (15, "15 minutes before"), (30, "30 minutes before"),
                        (60, "1 hour before")]
    DEFAULT_REMINDER_MINUTES = 10

    organization = models.ForeignKey(
        "core.Organization", on_delete=models.CASCADE, related_name="calendar_events",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_events",
        help_text="Whose calendar this is on",
    )
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.CALL)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.SET_NULL, null=True, blank=True, related_name="calendar_events",
        help_text="The lead this is about, if any",
    )
    conversation = models.ForeignKey(
        "messaging.Conversation", on_delete=models.SET_NULL, null=True, blank=True, related_name="calendar_events",
        help_text="The SMS chat this call came from, if any",
    )
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    notes = models.TextField(blank=True, default="")
    reminder_minutes = models.PositiveSmallIntegerField(
        null=True, blank=True, default=DEFAULT_REMINDER_MINUTES,
        help_text="Send a heads-up this many minutes before the start (0 = only when it starts; empty = no reminders)",
    )
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    start_alert_sent_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_at"]
        indexes = [
            models.Index(fields=["organization", "user", "start_at"]),
            models.Index(fields=["status", "start_at"]),
        ]

    def reset_reminders(self):
        """Call after the time changes so the reminders go out again for the new time."""
        self.reminder_sent_at = None
        self.start_alert_sent_at = None

    def clean(self):
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValidationError({"end_at": "The end time must be after the start time."})

    def __str__(self):
        return f"{self.title} ({self.user} @ {self.start_at:%Y-%m-%d %H:%M} UTC)"


class WorkerHeartbeat(models.Model):
    """Last time a background job checked in (e.g. the call reminder sender), so the app can warn when it isn't running."""
    name = models.CharField(max_length=50, unique=True)
    last_seen = models.DateTimeField()

    def __str__(self):
        return f"{self.name} @ {self.last_seen:%Y-%m-%d %H:%M:%S}"
