"""
Team calendar: every team member (owner, admin or agent) has a calendar.

- Agents see and edit only their own calendar.
- Owners and admins can open anyone's calendar in the workspace, or everyone's at once, and add events for them.
- Times are stored in UTC and always shown in the viewer's time zone (their own, or the workspace's).
"""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from contacts.models import Contact
from core.models import User, UserProfile
from core.timezones import (
    is_valid_timezone,
    org_timezone,
    personal_timezone,
    timezone_choices,
    timezone_label,
    timezone_short,
    user_timezone,
)

from . import holidays
from .models import CalendarEvent, ClosedDay
from .reminders import reminders_running

MAX_EVENT_DAYS = 7          # one event can't run longer than this
MAX_FEED_DAYS = 62          # widest window the calendar may ask for (month view shows 6 weeks)
MAX_NOTES = 5000

KIND_COLORS = {
    CalendarEvent.Kind.CALL: "#4f46e5",
    CalendarEvent.Kind.FOLLOW_UP: "#d97706",
    CalendarEvent.Kind.MEETING: "#059669",
    CalendarEvent.Kind.TASK: "#0284c7",
    CalendarEvent.Kind.BUSY: "#64748b",
}
UNCLAIMED_COLOR = "#f59e0b"
MEMBER_COLORS = ["#4f46e5", "#059669", "#d97706", "#db2777", "#0284c7", "#7c3aed", "#0d9488", "#dc2626", "#65a30d", "#475569"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_manager(user) -> bool:
    return user.role in (User.Role.OWNER, User.Role.ADMIN)


def _display_name(user) -> str:
    name = (user.get_full_name() or "").strip()
    if not name:
        try:
            name = (user.profile.full_name or "").strip()
        except Exception:
            name = ""
    return name or user.email


def _team(org):
    return list(User.objects.filter(organization=org, is_active=True).order_by("first_name", "last_name", "email"))


def _member_colors(team) -> dict:
    return {u.id: MEMBER_COLORS[i % len(MEMBER_COLORS)] for i, u in enumerate(team)}


def _resolve_member(request, key):
    """Whose calendar to show: "all" (owners/admins only), a team member, or yourself."""
    user = request.user
    if not _is_manager(user) or not key:
        return user
    if key == "all":
        return "all"
    try:
        return User.objects.get(public_id=key, organization=user.organization, is_active=True)
    except Exception:  # unknown person or not a valid id
        return user


def _parse_dt(value, tz):
    """ISO date/time from the calendar. Times without an offset are read in the viewer's time zone."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt


def _can_edit(viewer, event) -> bool:
    return event.organization_id == viewer.organization_id and (_is_manager(viewer) or event.user_id == viewer.id)


def _serialize(event, viewer, tz, color_by_member=False, member_colors=None):
    zone = ZoneInfo(tz)
    editable = _can_edit(viewer, event)
    unclaimed = event.user_id is None
    if unclaimed:
        color = UNCLAIMED_COLOR
    elif color_by_member:
        color = (member_colors or {}).get(event.user_id, MEMBER_COLORS[0])
    else:
        color = KIND_COLORS.get(event.kind, KIND_COLORS[CalendarEvent.Kind.CALL])
    contact = event.contact
    contact_name = ""
    if contact:
        contact_name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or contact.phone
    return {
        "id": event.id,
        "title": event.title,
        "start": event.start_at.astimezone(zone).isoformat(),
        "end": event.end_at.astimezone(zone).isoformat(),
        "backgroundColor": color,
        "borderColor": color,
        "editable": editable,
        "classNames": [f"ev-kind-{event.kind}", f"ev-status-{event.status}"] + (["ev-unclaimed"] if unclaimed else []),
        "extendedProps": {
            "kind": event.kind,
            "kind_label": event.get_kind_display(),
            "status": event.status,
            "status_label": event.get_status_display(),
            "notes": event.notes,
            "owner": str(event.user.public_id) if event.user_id else "",
            "owner_name": _display_name(event.user) if event.user_id else "Unclaimed",
            "unclaimed": unclaimed,
            "can_claim": unclaimed and event.status == CalendarEvent.Status.SCHEDULED,
            "auto_assigned": event.auto_assigned,
            "contact_name": contact_name,
            "contact_url": reverse("contacts:detail", args=[contact.id]) if contact else "",
            "chat_url": f"/inbox/{event.conversation_id}/" if event.conversation_id else "",
            "source": event.source,
            "reminder_minutes": event.reminder_minutes,
            "can_edit": editable,
        },
    }


def _payload(request) -> dict:
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}
    return request.POST.dict()


def _error(message, status=400):
    return JsonResponse({"status": "error", "message": message}, status=status)


def _apply_changes(event, data, tz, org, partial=False):
    """Copy submitted fields onto the event. Returns an error message, or None when everything is valid."""
    if "kind" in data or not partial:
        kind = str(data.get("kind") or CalendarEvent.Kind.CALL)
        if kind not in CalendarEvent.Kind.values:
            return "Unknown event type."
        event.kind = kind

    if "status" in data:
        status = str(data.get("status") or "")
        if status not in CalendarEvent.Status.values:
            return "Unknown status."
        event.status = status

    if "title" in data or not partial:
        title = str(data.get("title") or "").strip()
        event.title = (title or CalendarEvent.Kind(event.kind).label)[:200]

    if "notes" in data:
        notes = str(data.get("notes") or "").strip()
        if len(notes) > MAX_NOTES:
            return f"Notes can be at most {MAX_NOTES} characters."
        event.notes = notes

    if "reminder_minutes" in data:
        reminder = _parse_reminder(data.get("reminder_minutes"))
        if reminder is False:
            return "Unknown reminder option."
        if reminder != event.reminder_minutes:
            event.reminder_minutes = reminder
            event.reset_reminders()

    if "contact_id" in data:
        contact_id = data.get("contact_id")
        if contact_id in (None, "", 0, "0"):
            event.contact = None
        else:
            contact = Contact.objects.filter(organization=org, id=contact_id).first()
            if not contact:
                return "That lead wasn't found."
            event.contact = contact

    if "start" in data or not partial:
        start = _parse_dt(data.get("start"), tz)
        if not start:
            return "Pick a valid start date and time."
        event.start_at = start
    if "end" in data or not partial:
        end = _parse_dt(data.get("end"), tz)
        if not end:
            return "Pick a valid end date and time."
        event.end_at = end

    if event.end_at <= event.start_at:
        return "The end time must be after the start time."
    if event.end_at - event.start_at > timedelta(days=MAX_EVENT_DAYS):
        return f"An event can't be longer than {MAX_EVENT_DAYS} days."
    return None


REMINDER_VALUES = {value for value, _ in CalendarEvent.REMINDER_CHOICES}


def _parse_reminder(value):
    """None (no reminder), a valid number of minutes, or False when it isn't one of the options."""
    if value in (None, "", "none", "null"):
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return False
    return minutes if minutes in REMINDER_VALUES else False


def _owner_for_write(request, key):
    """Whose calendar a new/changed event goes on. Agents can only use their own."""
    if not key or not _is_manager(request.user):
        return request.user
    return User.objects.filter(public_id=key, organization=request.user.organization, is_active=True).first()


# ---------------------------------------------------------------------------
# Pages & endpoints
# ---------------------------------------------------------------------------

@login_required
def calendar_page(request):
    from . import calls
    org = request.user.organization
    if not org:
        messages.error(request, "No organization associated with this account.")
        return redirect("messaging:dashboard")

    tz = user_timezone(request.user)
    is_manager = _is_manager(request.user)
    team = _team(org) if is_manager else [request.user]
    colors = _member_colors(team)
    selected = _resolve_member(request, request.GET.get("member"))
    now_local = timezone.now().astimezone(ZoneInfo(tz))

    team_rows = [
        {
            "key": str(u.public_id),
            "name": _display_name(u),
            "role": u.get_role_display(),
            "color": colors[u.id],
            "is_me": u.id == request.user.id,
        }
        for u in team
    ]
    config = {
        "timeZone": tz,
        "member": "all" if selected == "all" else str(selected.public_id),
        "me": str(request.user.public_id),
        "isManager": is_manager,
        # Open a little before "now" during the working day; otherwise at 8 AM
        "scrollTime": f"{(now_local.hour - 2) if 9 <= now_local.hour <= 20 else 8:02d}:00:00",
        "kinds": [{"value": k.value, "label": k.label, "color": KIND_COLORS[k]} for k in CalendarEvent.Kind],
        "defaultReminder": CalendarEvent.DEFAULT_REMINDER_MINUTES,
        "team": team_rows,
        "urls": {
            "feed": reverse("scheduling:event_feed"),
            "create": reverse("scheduling:event_create"),
            "update": reverse("scheduling:event_update", args=[0]),
            "delete": reverse("scheduling:event_delete", args=[0]),
            "timezone": reverse("scheduling:set_timezone"),
            "claim": reverse("scheduling:claim_call", args=[0]),
            "unclaimed": reverse("scheduling:unclaimed_calls"),
            "closedDays": reverse("scheduling:closed_days"),
        },
        "claimId": request.GET.get("claim") or "",
    }
    context = {
        "tz": tz,
        "tz_label": timezone_label(tz),
        "tz_short": timezone_short(tz),
        # the toolbar shows "Los Angeles · PST" on big screens and just "PST" on laptops
        "tz_city": timezone_short(tz).rpartition(" · ")[0],
        "tz_code": timezone_short(tz).rpartition(" · ")[2],
        "workspace_tz": org_timezone(org),
        "workspace_tz_label": timezone_label(org_timezone(org)),
        "personal_tz": personal_timezone(request.user),
        "tz_choices": timezone_choices(),
        "is_manager": is_manager,
        "team": team_rows,
        "selected_member": config["member"],
        "kinds": config["kinds"],
        "statuses": CalendarEvent.Status.choices,
        "reminders": [("" if v is None else v, label) for v, label in CalendarEvent.REMINDER_CHOICES],
        "default_reminder": CalendarEvent.DEFAULT_REMINDER_MINUTES,
        "reminders_running": reminders_running(),
        "calendar_config": config,
        "claim_minutes": calls.claim_minutes(org),
        "slot_limit": calls.slot_limit(org),
        "slot_limit_custom": bool(org.max_calls_per_slot),
    }
    return render(request, "scheduling/calendar.html", context)


@login_required
@require_GET
def event_feed(request):
    org = request.user.organization
    if not org:
        return JsonResponse({"events": []})
    tz = user_timezone(request.user)
    start = _parse_dt(request.GET.get("start"), tz)
    end = _parse_dt(request.GET.get("end"), tz)
    if not start or not end or end <= start:
        return _error("A valid start and end are required.")
    end = min(end, start + timedelta(days=MAX_FEED_DAYS))

    member = _resolve_member(request, request.GET.get("member"))
    qs = (
        CalendarEvent.objects.filter(organization=org, start_at__lt=end, end_at__gt=start)
        .select_related("user", "user__profile", "contact")
    )
    unclaimed = Q(user__isnull=True, status=CalendarEvent.Status.SCHEDULED)     # team calls: on everyone's calendar
    if member == "all":
        team = _team(org)
        qs = qs.filter(Q(user__in=team) | unclaimed)
        colors = _member_colors(team)
        events = [_serialize(e, request.user, tz, color_by_member=True, member_colors=colors) for e in qs]
    else:
        qs = qs.filter(Q(user=member) | unclaimed)
        events = [_serialize(e, request.user, tz) for e in qs]
    events += _closed_day_events(org, start, end, tz)
    return JsonResponse({"events": events, "timeZone": tz})


def _closed_day_events(org, start, end, tz):
    """Closed days (and holidays the office is open after all) as shaded days on the calendar."""
    zone = ZoneInfo(tz)
    out = []
    for day in holidays.closed_days_between(org, start.astimezone(zone).date(), end.astimezone(zone).date()):
        # All-day: month view only shades all-day background events (week/day views shade the whole column)
        out.append({
            "id": f"closed-{day['date'].isoformat()}",
            "title": f"Closed · {day['name']}" if day["closed"] else f"Open · {day['name']}",
            "start": day["date"].isoformat(),
            "end": (day["date"] + timedelta(days=1)).isoformat(),
            "allDay": True,
            "display": "background",
            "backgroundColor": "#fecaca" if day["closed"] else "#bbf7d0",
            "classNames": ["closed-day" if day["closed"] else "reopened-day"],
            "editable": False,
            "extendedProps": {"closed_day": True, "closed": day["closed"], "name": day["name"]},
        })
    return out


@login_required
@require_POST
def event_create(request):
    org = request.user.organization
    if not org:
        return _error("No organization associated with this account.", status=403)
    data = _payload(request)
    to_team = data.get("member") == "pool" and _is_manager(request.user)     # a team call for agents to claim
    owner = None if to_team else _owner_for_write(request, data.get("member"))
    if owner is None and not to_team:
        return _error("That team member wasn't found.")
    tz = user_timezone(request.user)
    event = CalendarEvent(organization=org, user=owner, created_by=request.user,
                          pooled_at=timezone.now() if to_team else None)
    err = _apply_changes(event, data, tz, org)
    if err:
        return _error(err)
    event.save()
    return JsonResponse({"status": "success", "event": _serialize(event, request.user, tz)})


# ---------------------------------------------------------------------------
# Team calls: claiming the calls Gemma put on the team calendar
# ---------------------------------------------------------------------------

def _unclaimed_row(event, viewer_tz):
    from . import calls
    lead_tz = calls.lead_timezone(event.contact, event.organization) if event.contact_id else viewer_tz
    return {
        "id": event.id,
        "title": event.title,
        "contact_name": calls.contact_name(event.contact) if event.contact_id else "",
        "contact_phone": event.contact.phone if event.contact_id else "",
        "when_text": calls.describe_when(event.start_at, viewer_tz),
        "lead_time": calls.lead_time_line(event.start_at, lead_tz),
        "start": event.start_at.astimezone(ZoneInfo(viewer_tz)).isoformat(),
        "assign_at_text": calls.describe_when(calls.assign_by(event), viewer_tz),
        "chat_url": f"/inbox/{event.conversation_id}/" if event.conversation_id else "",
        "notes": event.notes,
    }


@login_required
@require_GET
def unclaimed_calls(request):
    """The team calls waiting for someone to claim them (the panel on the calendar polls this)."""
    from . import calls
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "success", "calls": []})
    tz = user_timezone(request.user)
    rows = [_unclaimed_row(e, tz) for e in calls.unclaimed_calls(org)]
    return JsonResponse({"status": "success", "calls": rows,
                         "claim_minutes": calls.claim_minutes(org)})


@login_required
@require_POST
def claim_call(request, event_id):
    """Take a team call (any team member), or give it to someone (owners/admins, with "member")."""
    from . import calls
    org = request.user.organization
    event = CalendarEvent.objects.filter(id=event_id, organization=org).select_related(
        "organization", "conversation", "contact").first() if org else None
    if not event:
        return _error("That call wasn't found.", status=404)
    if event.status != CalendarEvent.Status.SCHEDULED:
        return _error("That call isn't scheduled anymore.")
    data = _payload(request)
    who = request.user
    if data.get("member") and _is_manager(request.user):
        who = _owner_for_write(request, data.get("member"))
        if who is None:
            return _error("That team member wasn't found.")
    if event.user_id is not None:
        taker = _display_name(event.user)
        return _error(f"{'You' if event.user_id == request.user.id else taker} already "
                      f"{'have' if event.user_id == request.user.id else 'has'} this call.", status=409)
    if not calls.claim_call(event, who):
        return _error("Someone else just claimed this call.", status=409)
    if who.id != request.user.id:
        event.refresh_from_db()
        calls.notify_call(event, "booked")
    tz = user_timezone(request.user)
    return JsonResponse({"status": "success", "event": _serialize(event, request.user, tz),
                         "message": "The call is yours" if who.id == request.user.id
                         else f"Given to {_display_name(who)}"})


# ---------------------------------------------------------------------------
# "Call me now" (hot leads): what rings on each screen, and the Call / Ignore buttons
# ---------------------------------------------------------------------------

@login_required
@require_GET
def hot_leads(request):
    """Hot leads nobody has called yet, as this person's screen should show them (every page polls this)."""
    from . import calls
    org = request.user.organization
    if not org:
        return JsonResponse({"status": "success", "leads": []})
    now = timezone.now()
    leads = [calls.hot_lead_for(e, request.user, now) for e in calls.waiting_hot_leads(org, now)]
    return JsonResponse({"status": "success", "leads": leads, "server_now_ms": int(now.timestamp() * 1000)})


def _hot_event(request, event_id):
    org = request.user.organization
    return CalendarEvent.objects.filter(id=event_id, organization=org, urgent=True).select_related(
        "user", "contact", "conversation", "organization").first() if org else None


@login_required
@require_POST
def answer_hot_lead(request, event_id):
    """Call: the hot lead is yours (if nobody has it yet) and the ringing stops for everyone."""
    from . import calls
    event = _hot_event(request, event_id)
    if not event:
        return _error("That call wasn't found.", status=404)
    ok, why = calls.answer_call(event, request.user)
    if not ok:
        return _error(why, status=409)
    return JsonResponse({"status": "success", "phone": event.contact.phone if event.contact_id else "",
                         "chat_url": f"/inbox/{event.conversation_id}/" if event.conversation_id else ""})


@login_required
@require_POST
def ignore_hot_lead(request, event_id):
    """Ignore: stop ringing on my screen."""
    from . import calls
    event = _hot_event(request, event_id)
    if not event:
        return _error("That call wasn't found.", status=404)
    calls.ignore_call(event, request.user)
    return JsonResponse({"status": "success"})


# ---------------------------------------------------------------------------
# Closed days (owners/admins): close a date, or open the office on a holiday
# ---------------------------------------------------------------------------

def _closed_day_rows(org, today):
    rows = []
    for day in holidays.closed_days_between(org, today, today + timedelta(days=366)):
        rows.append({"date": day["date"].isoformat(), "label": f"{day['date']:%a, %b} {day['date'].day}, "
                                                                f"{day['date'].year}",
                     "name": day["name"], "closed": day["closed"], "kind": day["kind"],
                     "holiday": bool(day.get("holiday"))})
    return rows


@login_required
def closed_days(request):
    """GET: closed days for the next year. POST (owners/admins): {date, action: close|open|reset, name}."""
    org = request.user.organization
    if not org:
        return _error("No organization associated with this account.", status=403)
    today = timezone.now().astimezone(ZoneInfo(org_timezone(org))).date()
    if request.method == "POST":
        if not _is_manager(request.user):
            return _error("Only owners and admins can change closed days.", status=403)
        data = _payload(request)
        try:
            day = date.fromisoformat(str(data.get("date") or "").strip())
        except ValueError:
            return _error("Pick a valid date.")
        if day < today:
            return _error("That date has already passed.")
        action = str(data.get("action") or "").strip()
        name = " ".join(str(data.get("name") or "").split())[:80]
        holiday = holidays.holiday_on(day)
        if action == "close":
            ClosedDay.objects.update_or_create(organization=org, date=day, defaults={
                "closed": True, "name": name or (holidays.HOLIDAY_NAMES[holiday] if holiday else "Closed"),
                "created_by": request.user})
        elif action == "open":
            if holiday and holiday in holidays.closed_holidays(org):
                ClosedDay.objects.update_or_create(organization=org, date=day, defaults={
                    "closed": False, "name": holidays.HOLIDAY_NAMES[holiday], "created_by": request.user})
            else:
                ClosedDay.objects.filter(organization=org, date=day).delete()
        elif action == "reset":
            ClosedDay.objects.filter(organization=org, date=day).delete()
        else:
            return _error("Unknown action.")
        holidays.forget_closed_days(org)
    return JsonResponse({"status": "success", "days": _closed_day_rows(org, today)})


def _editable_event(request, event_id):
    event = (
        CalendarEvent.objects.filter(id=event_id, organization=request.user.organization)
        .select_related("user", "contact").first()
    )
    return event if event and _can_edit(request.user, event) else None


@login_required
@require_POST
def event_update(request, event_id):
    event = _editable_event(request, event_id)
    if not event:
        return _error("Event not found.", status=404)
    data = _payload(request)
    if "member" in data:
        if data.get("member") == "pool" and _is_manager(request.user):
            if event.user_id is not None:                 # back to the team: anyone can claim it again
                event.user, event.claimed_at, event.auto_assigned = None, None, False
                event.pooled_at = timezone.now()
        else:
            owner = _owner_for_write(request, data.get("member"))
            if owner is None:
                return _error("That team member wasn't found.")
            if event.user_id is None and owner is not None:
                from . import calls
                calls.claim_call(event, owner)            # an owner/admin gave the team call to someone: lead too
            event.user = owner
    tz = user_timezone(request.user)
    old_start = event.start_at
    err = _apply_changes(event, data, tz, request.user.organization, partial=True)
    if err:
        return _error(err)
    if event.start_at != old_start:
        event.reset_reminders()          # reminders go out again for the new time
    event.save()
    return JsonResponse({"status": "success", "event": _serialize(event, request.user, tz)})


@login_required
@require_POST
def event_delete(request, event_id):
    event = _editable_event(request, event_id)
    if not event:
        return _error("Event not found.", status=404)
    event.delete()
    return JsonResponse({"status": "success"})


@login_required
@require_POST
def set_my_timezone(request):
    """Save the person's own time zone ("" = follow the workspace)."""
    tz = (request.POST.get("timezone") or "").strip()
    if tz and not is_valid_timezone(tz):
        return _error("Unknown time zone.")
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    profile.timezone = tz
    profile.save(update_fields=["timezone"])
    effective = user_timezone(request.user)
    return JsonResponse({"status": "success", "timezone": effective, "label": timezone_label(effective)})


# ---------------------------------------------------------------------------
# "Schedule call" (from a chat or a contact)
# ---------------------------------------------------------------------------

def _wall(moment, tz) -> str:
    """A moment as the value of a datetime-local input in time zone tz."""
    return moment.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")


def _call_target(request, data):
    """The lead (and chat) a call is being scheduled for, from conversation_id or contact_id; in this workspace only."""
    from messaging.models import Conversation
    org = request.user.organization
    conversation = None
    conv_id = data.get("conversation_id") or data.get("conversation")
    if conv_id:
        conversation = Conversation.objects.filter(id=conv_id, organization=org).select_related(
            "contact", "assigned_to").first()
        if not conversation:
            return None, None
        return conversation.contact, conversation
    contact_id = data.get("contact_id") or data.get("contact")
    contact = Contact.objects.filter(id=contact_id, organization=org).first() if contact_id else None
    if contact:
        conversation = contact.conversations.order_by("-last_message_at").first()
    return contact, conversation


@login_required
@require_GET
def call_options(request):
    """Everything the Schedule-call window needs: lead's time zone, who can take it, and any call already booked."""
    from . import calls
    org = request.user.organization
    if not org:
        return _error("No organization associated with this account.", status=403)
    contact, conversation = _call_target(request, request.GET)
    if not contact:
        return _error("Lead not found.", status=404)

    tz = user_timezone(request.user)
    lead_tz = calls.lead_timezone(contact, org)
    is_manager = _is_manager(request.user)
    team = _team(org) if is_manager else [request.user]
    assigned = conversation.assigned_to if conversation and conversation.assigned_to_id else None
    default_member = assigned if (assigned and assigned.is_active and (is_manager or assigned.id == request.user.id)) \
        else request.user

    existing = calls.upcoming_call(conversation=conversation) if conversation else calls.upcoming_call(contact=contact)
    existing_data = None
    if existing and (_can_edit(request.user, existing) or existing.user_id is None):   # scheduling it claims it
        existing_data = {
            "event_id": existing.id,
            "start": _wall(existing.start_at, tz),
            "duration": max(5, int((existing.end_at - existing.start_at).total_seconds() // 60)),
            "member": str(existing.user.public_id) if existing.user_id else "",
            "member_name": _display_name(existing.user) if existing.user_id else "Unclaimed (team call)",
            "reminder_minutes": existing.reminder_minutes,
            "notes": existing.notes,
            "when_text": calls.describe_when(existing.start_at, tz),
            "source": existing.source,
        }

    return JsonResponse({
        "status": "success",
        "lead": {
            "name": calls.contact_name(contact),
            "phone": contact.phone,
            "tz": lead_tz,
            "tz_label": timezone_label(lead_tz),
            "state": calls.lead_state(contact),
        },
        "viewer": {"tz": tz, "tz_label": timezone_label(tz)},
        "members": [{"key": str(u.public_id), "name": _display_name(u), "role": u.get_role_display(),
                     "is_me": u.id == request.user.id} for u in team],
        "default_member": str(default_member.public_id),
        "existing": existing_data,
        "suggested_start": _wall(calls.next_call_slot(timezone.now(), lead_tz, request.user.organization), tz),
        "reminder_choices": [["" if v is None else v, label] for v, label in CalendarEvent.REMINDER_CHOICES],
        "default_reminder": CalendarEvent.DEFAULT_REMINDER_MINUTES,
        "reminders_running": reminders_running(),
        "is_manager": is_manager,
        "calling_hours": {"start": calls.call_hours(request.user.organization)[0].strftime("%H:%M"),
                          "end": calls.call_hours(request.user.organization)[1].strftime("%H:%M"),
                          "team_tz": org_timezone(request.user.organization),
                          "text": calls.team_hours_text(request.user.organization),
                          "days": sorted(calls.call_days(request.user.organization))},
    })


@login_required
@require_POST
def schedule_call(request):
    """Book (or move) a call with a lead on a team member's calendar."""
    from . import calls
    org = request.user.organization
    if not org:
        return _error("No organization associated with this account.", status=403)
    data = _payload(request)
    contact, conversation = _call_target(request, data)
    if not contact:
        return _error("Lead not found.", status=404)

    existing = None
    if data.get("event_id"):
        existing = _editable_event(request, data.get("event_id")) or CalendarEvent.objects.filter(
            id=data.get("event_id"), organization=org, user__isnull=True).first()     # a team call: anyone may take it
        if not existing:
            return _error("That call wasn't found.", status=404)

    caller = _owner_for_write(request, data.get("member"))
    if caller is None:
        return _error("That team member wasn't found.")

    tz = user_timezone(request.user)
    start = _parse_dt(data.get("start"), tz)
    if not start:
        return _error("Pick a valid date and time for the call.")
    if start < timezone.now() - timedelta(minutes=1):
        return _error("That time has already passed.")
    try:
        duration = int(data.get("duration") or calls.DEFAULT_CALL_MINUTES)
    except (TypeError, ValueError):
        return _error("Pick a valid call length.")
    if not 5 <= duration <= 240:
        return _error("A call can be 5 minutes to 4 hours long.")
    reminder = _parse_reminder(data.get("reminder_minutes", CalendarEvent.DEFAULT_REMINDER_MINUTES))
    if reminder is False:
        return _error("Unknown reminder option.")
    notes = str(data.get("notes") or "").strip()
    if len(notes) > MAX_NOTES:
        return _error(f"Notes can be at most {MAX_NOTES} characters.")

    moved = existing is not None
    previous_user_id = existing.user_id if existing else None
    if existing and existing.user_id is None:
        existing.claimed_at = timezone.now()          # giving a team call a caller claims it for them
    if existing:
        existing.notes = notes          # the window shows the current notes, so what comes back replaces them
        notes = ""
    event, _ = calls.book_call(
        contact=contact, user=caller, start=start, conversation=conversation or (existing.conversation if existing else None),
        source=CalendarEvent.Source.MANUAL, created_by=request.user, notes=notes,
        duration_minutes=duration, reminder_minutes=reminder, event=existing,
    )

    lead_tz = calls.lead_timezone(contact, org)
    if moved and previous_user_id is None and event.conversation_id:
        # a team call nobody had claimed: whoever it's now booked with gets the lead too
        from messaging.models import Conversation
        Conversation.objects.filter(pk=event.conversation_id, assigned_to__isnull=True).update(assigned_to=event.user)
    if event.conversation_id:
        calls.refresh_handoff_reason(event.conversation, event, lead_tz)
        calls.post_chat_note(
            event.conversation,
            f"📅 {'Call moved' if moved else 'Call booked'} by {_display_name(request.user)}: "
            f"{calls.booking_summary(event, lead_tz)}.",
        )
    # Tell the person whose calendar it's on (unless they did it themselves)
    if event.user_id != request.user.id:
        calls.notify_call(event, "moved" if moved and previous_user_id == event.user_id else "booked")
    return JsonResponse({
        "status": "success",
        "event": _serialize(event, request.user, tz),
        "summary": calls.booking_summary(event, lead_tz),
        "when_text": calls.describe_when(event.start_at, tz),
        "caller_name": event.user.first_name or event.user.email,
        "lead_time": calls.lead_time_line(event.start_at, lead_tz),
    })
