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
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_events", null=True, blank=True,
        help_text="Whose calendar this is on. Empty = a team call nobody has claimed yet",
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
    # Team call calendar: when the call went to the team to claim, and how it got its agent
    pooled_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    auto_assigned = models.BooleanField(default=False, help_text="Nobody claimed it, so it was assigned automatically")
    # "Call me now" (hot lead): rings the agents until someone clicks Call
    urgent = models.BooleanField(default=False, help_text="The lead asked for a call right now")
    answered_at = models.DateTimeField(null=True, blank=True, help_text="When someone clicked Call")
    ignored_by = models.JSONField(default=list, blank=True, help_text="Ids of team members who clicked Ignore")
    escalated_at = models.DateTimeField(null=True, blank=True, help_text="When owners/admins were told it's waiting")
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

    @property
    def is_unclaimed(self) -> bool:
        return self.user_id is None

    def reset_reminders(self):
        """Call after the time changes so the reminders go out again for the new time."""
        self.reminder_sent_at = None
        self.start_alert_sent_at = None

    def clean(self):
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValidationError({"end_at": "The end time must be after the start time."})

    def __str__(self):
        return f"{self.title} ({self.user or 'unclaimed'} @ {self.start_at:%Y-%m-%d %H:%M} UTC)"


class ClosedDay(models.Model):
    """One date on the team calendar that differs from the usual: closed (a holiday the owner added, an office
    event) or, with closed=False, a federal holiday the office is open after all."""
    organization = models.ForeignKey("core.Organization", on_delete=models.CASCADE, related_name="closed_days")
    date = models.DateField()
    closed = models.BooleanField(default=True)
    name = models.CharField(max_length=80, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["organization", "date"], name="one_closed_day_per_date")]

    def __str__(self):
        return f"{self.date} {'closed' if self.closed else 'open'} {self.name}".strip()


class WorkerHeartbeat(models.Model):
    """Last time a background job checked in (e.g. the call reminder sender), so the app can warn when it isn't running."""
    name = models.CharField(max_length=50, unique=True)
    last_seen = models.DateTimeField()

    def __str__(self):
        return f"{self.name} @ {self.last_seen:%Y-%m-%d %H:%M:%S}"
