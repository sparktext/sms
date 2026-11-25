"""
Assigns team calls nobody claimed in time, and sends call reminders: a heads-up N minutes before (the event's reminder_minutes) and a "time to call"
alert when it starts. Runs inside the AI worker every few seconds, or on its own with
`python manage.py send_call_reminders --loop`.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from .calls import assign_unclaimed_calls, escalate_hot_leads, notify_call
from .models import CalendarEvent, WorkerHeartbeat

logger = logging.getLogger(__name__)

LONGEST_HEADS_UP = timedelta(minutes=60)   # the largest reminder option
LATE_GRACE = timedelta(minutes=30)         # if the sender was off, don't announce calls that started long ago
HEARTBEAT = "call_reminders"
HEARTBEAT_STALE = timedelta(minutes=2)     # no check-in for this long = reminders aren't running


def reminders_running() -> bool:
    """Has the reminder sender checked in recently? (False = no worker is sending reminders.)"""
    beat = WorkerHeartbeat.objects.filter(name=HEARTBEAT).values_list("last_seen", flat=True).first()
    return bool(beat and timezone.now() - beat < HEARTBEAT_STALE)


def _claim(event, field, now) -> bool:
    """Mark this reminder as sent; False if another process already did (or the call was moved meanwhile)."""
    return bool(
        CalendarEvent.objects.filter(pk=event.pk, start_at=event.start_at, status=CalendarEvent.Status.SCHEDULED,
                                     **{f"{field}__isnull": True}).update(**{field: now})
    )


def send_due_reminders(now=None) -> int:
    """Send every reminder that is due. Returns how many went out."""
    now = now or timezone.now()
    WorkerHeartbeat.objects.update_or_create(name=HEARTBEAT, defaults={"last_seen": timezone.now()})
    # Team calls nobody claimed in time go to an agent first, so their reminders below go out as usual
    try:
        assign_unclaimed_calls(now)
    except Exception as exc:
        logger.exception("Assigning unclaimed team calls failed: %s", exc)
    try:
        escalate_hot_leads(now)                      # "call me now" leads nobody called within 10 minutes
    except Exception as exc:
        logger.exception("Escalating hot leads failed: %s", exc)
    sent = 0
    pending = CalendarEvent.objects.filter(
        status=CalendarEvent.Status.SCHEDULED, reminder_minutes__isnull=False, user__is_active=True,
    ).select_related("user", "contact", "organization")

    # "Time to call" for calls starting now
    for event in pending.filter(start_alert_sent_at__isnull=True, start_at__lte=now, start_at__gt=now - LATE_GRACE):
        if _claim(event, "start_alert_sent_at", now):
            _claim(event, "reminder_sent_at", now)   # no heads-up for a call that has already started
            notify_call(event, "now", now)
            sent += 1

    # Heads-up a few minutes before
    for event in pending.filter(reminder_minutes__gt=0, reminder_sent_at__isnull=True,
                                start_at__gt=now, start_at__lte=now + LONGEST_HEADS_UP):
        if event.start_at - timedelta(minutes=event.reminder_minutes) <= now and _claim(event, "reminder_sent_at", now):
            notify_call(event, "soon", now)
            sent += 1

    if sent:
        logger.info("Sent %d call reminder(s)", sent)
    return sent
