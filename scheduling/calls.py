"""
Call scheduling: booking calls with leads on a team member's calendar, and the notifications around them.

Used by the "Schedule call" button, by Gemma (when a lead agrees to a call and says when), and by the
reminder sender. Every time is stored in UTC; "9 AM" from a lead always means 9 AM where the LEAD is.
"""
import logging
from datetime import date as date_cls
from datetime import datetime, timedelta
from datetime import time as time_cls
from zoneinfo import ZoneInfo

from django.db import models
from django.utils import timezone

from core.testmode import time_limits_off
from core.timezones import org_timezone, timezone_label, user_timezone

from . import holidays
from .models import CalendarEvent

logger = logging.getLogger(__name__)

DEFAULT_CALL_MINUTES = 15
# Usual US calling window in the lead's local time (TCPA quiet hours are before 8 AM and after 9 PM)
CALLING_START, CALLING_END = time_cls(8, 0), time_cls(21, 0)
# When a lead says a part of the day, or "tomorrow" without a time
PART_OF_DAY_TIMES = {"morning": time_cls(10, 0), "afternoon": time_cls(14, 0), "evening": time_cls(18, 0)}
DEFAULT_DAY_TIME = time_cls(10, 0)
FIRST_SLOT_NEXT_DAY = time_cls(9, 0)
MAX_DAYS_AHEAD = 60

# Primary time zone per US state / territory (states split across zones use the zone most people live in)
STATE_TIMEZONES = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix", "AR": "America/Chicago",
    "CA": "America/Los_Angeles", "CO": "America/Denver", "CT": "America/New_York", "DE": "America/New_York",
    "DC": "America/New_York", "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis", "IA": "America/Chicago",
    "KS": "America/Chicago", "KY": "America/New_York", "LA": "America/Chicago", "ME": "America/New_York",
    "MD": "America/New_York", "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver", "NE": "America/Chicago",
    "NV": "America/Los_Angeles", "NH": "America/New_York", "NJ": "America/New_York", "NM": "America/Denver",
    "NY": "America/New_York", "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York", "RI": "America/New_York",
    "SC": "America/New_York", "SD": "America/Chicago", "TN": "America/Chicago", "TX": "America/Chicago",
    "UT": "America/Denver", "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver", "PR": "America/Puerto_Rico",
    "GU": "Pacific/Guam", "VI": "America/St_Thomas", "AS": "Pacific/Pago_Pago", "MP": "Pacific/Saipan",
}
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "washington dc": "DC", "florida": "FL",
    "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR", "guam": "GU",
}
_STATE_KEYS = {"state", "state_code", "statecode", "st", "province", "region"}


# ---------------------------------------------------------------------------
# Time zones & formatting
# ---------------------------------------------------------------------------

def _find_state(data, depth=0):
    """A US state code from the lead's details (top level first, then nested address blocks)."""
    if not isinstance(data, dict) or depth > 3:
        return ""
    nested = []
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            nested.append(value)
            continue
        if str(key).strip().lower().replace(" ", "_") in _STATE_KEYS:
            raw = str(value or "").strip()
            code = raw.upper() if raw.upper() in STATE_TIMEZONES else STATE_NAMES.get(raw.lower(), "")
            if code:
                return code
    for value in nested:
        items = value if isinstance(value, list) else [value]
        for item in items[:5]:
            code = _find_state(item, depth + 1)
            if code:
                return code
    return ""


def lead_state(contact) -> str:
    cf = getattr(contact, "custom_fields", None)
    return _find_state(cf) if isinstance(cf, dict) else ""


def lead_timezone(contact, org=None) -> str:
    """Where the lead is: their state, else their phone number's area code, else the workspace time zone."""
    state = lead_state(contact)
    if state:
        return STATE_TIMEZONES[state]
    from compliance.quiet_hours import _phone_location
    area_state, zones = _phone_location(getattr(contact, "phone", ""))
    if area_state in STATE_TIMEZONES:
        return STATE_TIMEZONES[area_state]
    if len(zones) == 1:
        return zones[0]
    return org_timezone(org or getattr(contact, "organization", None))


def fmt_time(dt) -> str:
    return f"{dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def fmt_day(dt) -> str:
    return f"{dt:%a, %b} {dt.day}"


def fmt_zone_abbr(dt) -> str:
    abbr = dt.strftime("%Z")
    return "" if abbr.startswith(("+", "-")) else abbr


def describe_when(moment, tz: str, now=None) -> str:
    """e.g. "today at 9:00 AM", "tomorrow at 2:30 PM", "Mon, Nov 23 at 10:00 AM" (in time zone tz)."""
    local = moment.astimezone(ZoneInfo(tz))
    today = (now or timezone.now()).astimezone(ZoneInfo(tz)).date()
    if local.date() == today:
        day = "today"
    elif local.date() == today + timedelta(days=1):
        day = "tomorrow"
    else:
        day = fmt_day(local)
    return f"{day} at {fmt_time(local)}"


def proposal_text(moment, tz: str, now=None) -> str:
    """How we say a call time to the lead, in their own time: "11 AM tomorrow (Sat, Nov 21)", "2:30 PM on Mon, Nov 23"."""
    local = moment.astimezone(ZoneInfo(tz))
    today = (now or timezone.now()).astimezone(ZoneInfo(tz)).date()
    clock = f"{local.hour % 12 or 12}{'' if local.minute == 0 else f':{local.minute:02d}'} {'AM' if local.hour < 12 else 'PM'}"
    if local.date() == today:
        return f"{clock} today ({fmt_day(local)})"
    if local.date() == today + timedelta(days=1):
        return f"{clock} tomorrow ({fmt_day(local)})"
    return f"{clock} on {fmt_day(local)}"


def call_hours(org=None):
    """(start, end) of the team's working hours for calls, in the WORKSPACE time zone (Settings > Company Profile).
    Separately, every call must also fall inside 8 AM-9 PM in the lead's own time (see legal_for_lead)."""
    start = getattr(org, "call_start", None) or CALLING_START
    end = getattr(org, "call_end", None) or CALLING_END
    return (start, end) if start < end else (CALLING_START, CALLING_END)


def _team_zone(org):
    return ZoneInfo(org_timezone(org))


def legal_for_lead(moment, lead_tz: str) -> bool:
    """Inside 8 AM-9 PM where the lead is (TCPA quiet hours are outside it)."""
    if time_limits_off():
        return True
    local = moment.astimezone(ZoneInfo(lead_tz)).time()
    return CALLING_START <= local < CALLING_END


def in_team_hours(moment, org) -> bool:
    """Inside the team's working hours (workspace time zone)."""
    if org is None or time_limits_off():
        return True
    start, end = call_hours(org)
    local = moment.astimezone(_team_zone(org)).time()
    return start <= local < end


def fmt_clock(t) -> str:
    """time(9, 0) -> "9 AM", time(17, 30) -> "5:30 PM"."""
    return f"{t.hour % 12 or 12}{'' if t.minute == 0 else f':{t.minute:02d}'} {'AM' if t.hour < 12 else 'PM'}"


WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def call_days(org) -> set:
    """The days the team takes calls (0 = Monday ... 6 = Sunday), in the workspace time zone. Mon-Fri by default."""
    days = getattr(org, "call_days", None)
    if not isinstance(days, (list, tuple)):
        return {0, 1, 2, 3, 4}
    cleaned = {int(d) for d in days if str(d).strip().isdigit() and 0 <= int(d) <= 6}
    return cleaned or {0, 1, 2, 3, 4}


def is_call_day(moment, tz: str, org) -> bool:
    """A day the team works, judged on the team's own calendar (workspace time zone): one of the call days, and
    not a closed day (a holiday the office observes, or a date the owner closed)."""
    if org is None or time_limits_off():
        return True
    day = moment.astimezone(_team_zone(org)).date()
    return day.weekday() in call_days(org) and not holidays.is_closed(org, day)


def closed_reason_at(moment, org):
    """Why the office is closed then ("Thanksgiving"), or None (the team's own date)."""
    if org is None or time_limits_off():
        return None
    return holidays.closed_reason(org, moment.astimezone(_team_zone(org)).date())


def in_calling_hours(moment, tz: str, org=None) -> bool:
    """Legal for the lead (8 AM-9 PM their time) and, given the org, inside the team's working hours."""
    return legal_for_lead(moment, tz) and in_team_hours(moment, org)


def can_call_at(moment, tz: str, org) -> bool:
    """A time Gemma may book: legal for the lead, inside the team's working hours, on a team work day."""
    return in_calling_hours(moment, tz, org) and is_call_day(moment, tz, org)


def lead_window(org, lead_tz: str, day):
    """When we can call on `day` (a date where the lead is): (start, end) times in the lead's zone, or None.
    The team's working hours, moved into the lead's zone, cut to 8 AM-9 PM their time."""
    if time_limits_off():
        return time_cls(0, 0), time_cls(23, 59)          # local test mode: any time of any day
    zone = ZoneInfo(lead_tz)
    lo = datetime.combine(day, CALLING_START, tzinfo=zone)
    hi = datetime.combine(day, CALLING_END, tzinfo=zone)
    if org is None:
        return lo.time(), hi.time()
    team, (start, end), days = _team_zone(org), call_hours(org), call_days(org)
    best = None
    for d in (day - timedelta(days=1), day, day + timedelta(days=1)):     # the team's day may differ near midnight
        if d.weekday() not in days or holidays.is_closed(org, d):
            continue
        s = max(lo, datetime.combine(d, start, tzinfo=team))
        e = min(hi, datetime.combine(d, end, tzinfo=team))
        if s < e:
            best = (s, e) if best is None else (min(best[0], s), max(best[1], e))
    if not best:
        return None
    return best[0].astimezone(zone).time(), best[1].astimezone(zone).time()


def am_pm_obvious(hour: int, minute: int = 0, org=None, tz: str = None, day=None) -> bool:
    """A bare hour where only AM or only PM is a time we can call: "10" -> 10 AM, "3" -> 3 PM, "12" -> noon.
    "8" isn't (8 AM and 8 PM both are)."""
    window = lead_window(org, tz, day or timezone.now().astimezone(ZoneInfo(tz)).date()) if tz else None
    start, end = window or (CALLING_START, CALLING_END)
    options = {hour % 12, hour % 12 + 12} - {0}
    return sum(start <= time_cls(h, minute) < end for h in options) == 1


def _days_text(org) -> str:
    days = sorted(call_days(org))
    if len(days) == 7:
        return "every day"
    if len(days) > 1 and days == list(range(days[0], days[-1] + 1)):
        return f"{WEEKDAY_NAMES[days[0]]}-{WEEKDAY_NAMES[days[-1]]}"
    return ", ".join(WEEKDAY_NAMES[d] for d in days)


def team_hours_text(org) -> str:
    """For the team: "8 AM-9 PM PST, Monday-Friday" (the agents' working hours, workspace time zone)."""
    start, end = call_hours(org)
    abbr = fmt_zone_abbr(timezone.now().astimezone(_team_zone(org)))
    return f"{fmt_clock(start)}-{fmt_clock(end)}{(' ' + abbr) if abbr else ''}, {_days_text(org)}"


def lead_hours_text(org, lead_tz: str, day=None) -> str:
    """For the lead, in their own time: "11 AM-9 PM, Monday-Friday" (team hours moved to their zone, kept legal)."""
    day = day or timezone.now().astimezone(ZoneInfo(lead_tz)).date()
    window = None
    for i in range(7):                     # the window of the first work day from `day` on
        window = lead_window(org, lead_tz, day + timedelta(days=i))
        if window:
            break
    start, end = window or (CALLING_START, CALLING_END)
    return f"{fmt_clock(start)}-{fmt_clock(end)}, {_days_text(org)}"


def call_window_text(org) -> str:
    return team_hours_text(org)


def contact_name(contact) -> str:
    name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
    return name or contact.phone


# ---------------------------------------------------------------------------
# How many calls fit in one time slot (team call calendar)
# ---------------------------------------------------------------------------

def call_team(org):
    """Active team members who take calls (owners, admins and agents)."""
    from core.models import User
    return User.objects.filter(organization=org, is_active=True,
                               role__in=[User.Role.OWNER, User.Role.ADMIN, User.Role.AGENT])


def slot_limit(org) -> int:
    """How many calls may start at the same time: the owner's number, or else the agents available for leads
    (everyone active when nobody is marked available, e.g. at night). At least 1."""
    if org is None:
        return 1_000
    if getattr(org, "max_calls_per_slot", None):
        return int(org.max_calls_per_slot)
    team = call_team(org)
    return max(1, team.filter(is_available_for_leads=True).count() or team.count())


def calls_at(org, start, end, exclude=None) -> int:
    """Scheduled calls overlapping [start, end) in this workspace (claimed or not)."""
    qs = CalendarEvent.objects.filter(organization=org, kind=CalendarEvent.Kind.CALL,
                                      status=CalendarEvent.Status.SCHEDULED, start_at__lt=end, end_at__gt=start)
    if exclude is not None and getattr(exclude, "pk", None):
        qs = qs.exclude(pk=exclude.pk)
    return qs.count()


def slot_is_full(org, moment, exclude=None, minutes: int = DEFAULT_CALL_MINUTES) -> bool:
    if org is None or moment is None or not getattr(org, "pk", None):
        return False
    return calls_at(org, moment, moment + timedelta(minutes=minutes), exclude) >= slot_limit(org)


def first_free(moment, tz: str, org=None, exclude=None):
    """`moment`, or the next quarter hour after it we can call that isn't fully booked yet."""
    if org is None or moment is None or not getattr(org, "pk", None):
        return moment
    candidate = moment
    for _ in range(4 * 24 * 21):                      # up to three weeks of quarter hours
        if can_call_at(candidate, tz, org) and not slot_is_full(org, candidate, exclude):
            return candidate
        candidate += timedelta(minutes=15)
    return moment


# ---------------------------------------------------------------------------
# Turning what a lead said into a moment
# ---------------------------------------------------------------------------

def next_call_slot(now, tz: str, org=None):
    """The next open slot to call: _next_call_slot, moved later while that time is already fully booked."""
    return first_free(_next_call_slot(now, tz, org), tz, org)


def _next_call_slot(now, tz: str, org=None):
    """The next sensible moment to call: the next quarter hour at least 10 minutes out, inside the time we can call
    the lead (the team's working hours and days, and 8 AM-9 PM their time): Friday night -> Monday morning."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone) + timedelta(minutes=10)
    minute = (local.minute // 15 + 1) * 15
    slot = local.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    day = slot.date()
    for i in range(15):
        window = lead_window(org, tz, day)
        if window:
            start, end = window
            last = (datetime.combine(day, end) - timedelta(minutes=30)).time()   # no new call in the last 30 min
            first = max(start, FIRST_SLOT_NEXT_DAY)                               # a day's first call: 9 AM or later
            if first >= last:
                first = start
            if i == 0 and start <= slot.time() < last:
                return slot
            if (i > 0 or slot.time() < start) and first < last:
                return datetime.combine(day, first, tzinfo=zone)
        day += timedelta(days=1)
    return slot


def first_slot_on(day, tz: str, org=None):
    """The first time we'd call on `day` (a date where the lead is): 9 AM, or later if the window opens later.
    None when we can't call that day (a day the team doesn't work)."""
    window = lead_window(org, tz, day)
    if not window:
        return None
    start, end = window
    last = (datetime.combine(day, end) - timedelta(minutes=30)).time()
    first = max(start, FIRST_SLOT_NEXT_DAY)
    if first >= last:
        first = start
    return datetime.combine(day, first, tzinfo=ZoneInfo(tz))


def callable_instead(moment, tz: str, org=None, now=None):
    """The nearest time we CAN call, for a time we can't: Saturday 11 AM -> Monday 11 AM, 7 AM -> 9 AM that day,
    8 PM -> the last slot that day (5:30 PM when calls end at 6), or the next work day once that's gone.
    Keeps the lead's time of day when it fits; otherwise that day's first slot."""
    now = now or timezone.now()
    zone = ZoneInfo(tz)
    local = moment.astimezone(zone)
    for i in range(15):
        day = local.date() + timedelta(days=i)
        window = lead_window(org, tz, day)
        if not window:
            continue
        start, end = window
        last = (datetime.combine(day, end) - timedelta(minutes=30)).time()
        if i == 0 and local.time() >= last:
            # "8pm" when calls end at 6: the last slot that day is closest to what they want, if it's still ahead
            latest = datetime.combine(day, last, tzinfo=zone)
            if latest > now + timedelta(minutes=10) and can_call_at(latest, tz, org) and not slot_is_full(org, latest):
                return latest
            continue                                   # already gone: the next day we call
        if start <= local.time() < last:
            candidate = datetime.combine(day, local.time(), tzinfo=zone)
        else:
            candidate = first_slot_on(day, tz, org)
        if candidate <= now + timedelta(minutes=10):
            return next_call_slot(now, tz, org)
        if can_call_at(candidate, tz, org):
            return first_free(candidate, tz, org)
    return next_call_slot(now, tz, org)


def _parse_date(value):
    try:
        return date_cls.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _parse_time(value):
    text = str(value or "").strip()
    try:
        hh, mm = text.split(":")[:2]
        return time_cls(int(hh), int(mm))
    except (ValueError, TypeError):
        return None


def resolve_call_time(when: dict, tz: str, now=None, org=None):
    """
    Pin down what the lead asked for. `when` has date (YYYY-MM-DD), time (HH:MM), part_of_day and asap,
    all as the lead meant them in their own time zone. Returns (moment, how) or (None, why_not):
      how: "exact" | "their_day" (a day/part of day, we picked the time) | "asap"
    """
    now = now or timezone.now()
    zone = ZoneInfo(tz)
    local_now = now.astimezone(zone)
    day = _parse_date(when.get("date"))
    at = _parse_time(when.get("time"))
    part = str(when.get("part_of_day") or "").strip().lower()
    part_time = PART_OF_DAY_TIMES.get(part)

    def at_day(d, t):
        return datetime.combine(d, t, tzinfo=zone)

    if when.get("asap") and not day and not at:
        return next_call_slot(now, tz, org), "asap"

    if at and when.get("am_pm_said", True) is False and 1 <= at.hour <= 12:
        # "11" / "at 3" with no am or pm: the next such time that falls in calling hours
        # (11 said at 12:45 PM -> 11 AM tomorrow, not 11 PM tonight; 7 said at 2 PM -> 7 PM today)
        hours = sorted({at.hour % 12, at.hour % 12 + 12} - {0})           # a bare "12" means noon
        days = [day] if day else [local_now.date(), local_now.date() + timedelta(days=1)]
        options = [at_day(d, time_cls(h, at.minute)) for d in days for h in hours]
        ahead = [m for m in options if m > local_now + timedelta(minutes=5)]
        callable_ = [m for m in ahead if can_call_at(m, tz, org)]
        moment = (callable_ or ahead or [None])[0]
        if moment is None:
            return None, "that time has already passed"
        return moment, "exact"

    if day and at:
        moment, how = at_day(day, at), "exact"
    elif at:
        moment, how = at_day(local_now.date(), at), "exact"
        if moment <= local_now + timedelta(minutes=5):
            moment = moment + timedelta(days=1)
    elif day and part_time:
        moment, how = at_day(day, part_time), "their_day"
    elif day:
        if day == local_now.date():
            return next_call_slot(now, tz, org), "asap"
        # "anytime on the 28th": that day's first slot; a plain "the 28th": mid-morning
        first = first_slot_on(day, tz, org) if when.get("asap") else None
        moment, how = (first or at_day(day, DEFAULT_DAY_TIME)), "their_day"
    elif part_time:
        moment, how = at_day(local_now.date(), part_time), "their_day"
        if moment <= local_now + timedelta(minutes=5):
            moment = moment + timedelta(days=1)
    else:
        return next_call_slot(now, tz, org), "asap"     # they want a call but didn't say when

    if moment <= now:
        return None, "that time has already passed"
    if moment > now + timedelta(days=MAX_DAYS_AHEAD):
        return None, "that's more than two months away"
    return moment, how


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

def upcoming_call(conversation=None, contact=None):
    """The next scheduled call for this chat (or lead) that hasn't finished yet."""
    qs = CalendarEvent.objects.filter(
        kind=CalendarEvent.Kind.CALL, status=CalendarEvent.Status.SCHEDULED, end_at__gte=timezone.now(),
    )
    if conversation is not None:
        qs = qs.filter(conversation=conversation)
    elif contact is not None:
        qs = qs.filter(contact=contact)
    else:
        return None
    return qs.select_related("user", "contact").order_by("start_at").first()


def fallback_caller(org):
    """Who gets the call when nobody is assigned: the first owner, then admin, then anyone active."""
    from core.models import User
    team = User.objects.filter(organization=org, is_active=True)
    for role in (User.Role.OWNER, User.Role.ADMIN, User.Role.AGENT):
        user = team.filter(role=role).order_by("date_joined").first()
        if user:
            return user
    return None


def book_call(*, contact, user, start, conversation=None, source=CalendarEvent.Source.MANUAL, created_by=None,
              notes="", duration_minutes=DEFAULT_CALL_MINUTES, reminder_minutes=CalendarEvent.DEFAULT_REMINDER_MINUTES,
              event=None):
    """
    Put a call with this lead on `user`'s calendar (user=None: on the team calendar for an agent to claim),
    or move `event` to the new time. Returns (event, created).
    """
    created = event is None
    if created:
        event = CalendarEvent(
            organization=contact.organization, contact=contact, conversation=conversation,
            kind=CalendarEvent.Kind.CALL, source=source, created_by=created_by,
            title=f"Call {contact_name(contact)}"[:200],
        )
    event.user = user
    if user is None and event.pooled_at is None:
        event.pooled_at = timezone.now()
    event.start_at = start
    event.end_at = start + timedelta(minutes=max(5, int(duration_minutes or DEFAULT_CALL_MINUTES)))
    event.status = CalendarEvent.Status.SCHEDULED
    event.reminder_minutes = reminder_minutes
    if notes:
        event.notes = (f"{event.notes}\n\n{notes}" if event.notes and not created else notes)[:5000]
    event.reset_reminders()
    event.save()
    return event, created


# ---------------------------------------------------------------------------
# Telling people about it
# ---------------------------------------------------------------------------

def event_url(event) -> str:
    if event.conversation_id:
        return f"/inbox/{event.conversation_id}/"
    if event.contact_id:
        return f"/contacts/{event.contact_id}/"
    return "/calendar/"


STAGE_TITLES = {
    "booked": "📅 Call booked",
    "moved": "📅 Call moved",
    "cancelled": "📅 Call cancelled",
    "soon": "📞 Call coming up",
    "now": "📞 Time to call",
    "pool": "📅 New call to claim",
    "assigned": "📅 Call assigned to you",
    "escalated": "🔥 Hot lead still waiting",
}


def call_notification(event, stage: str, now=None, recipient=None) -> dict:
    """The notification payload for the person whose calendar this call is on (or `recipient`)."""
    user = recipient or event.user
    tz = user_timezone(user)
    local = event.start_at.astimezone(ZoneInfo(tz))
    who = contact_name(event.contact) if event.contact_id else event.title
    when = describe_when(event.start_at, tz, now)
    if stage == "soon":
        minutes = round((event.start_at - (now or timezone.now())).total_seconds() / 60)
        body = f"{who} in {minutes} min ({fmt_time(local)})" if minutes >= 1 else f"{who} at {fmt_time(local)}"
    elif stage == "now":
        body = f"{who} · now ({fmt_time(local)})"
    elif stage == "moved":
        body = f"Moved: {who} is now {when}"
    elif stage == "cancelled":
        body = f"Cancelled: {who} no longer wants the call {when}"
    elif stage == "pool":
        body = f"{who} {when} · first to claim it gets the lead"
    elif stage == "assigned":
        body = f"{who} {when} · nobody claimed it, so it's yours"
    elif stage == "escalated":
        waited = max(1, round(((now or timezone.now()) - event.start_at).total_seconds() / 60))
        body = f"{who} asked for a call right now {waited} min ago and nobody has called yet"
    else:
        by = "Gemma booked" if event.source == CalendarEvent.Source.AI else "Booked"
        body = f"{by}: {who} {when}"
    org = event.organization
    phone = event.contact.phone if event.contact_id else ""
    return {
        "id": f"call-{event.id}-{stage}-{int(event.start_at.timestamp())}",
        "type": "call_reminder",
        "stage": stage,
        "title": STAGE_TITLES.get(stage, "📞 Call"),
        "body": body,
        "when_text": when,
        "event_id": event.id,
        "conversation_id": event.conversation_id,
        "contact_name": who,
        "contact_phone": phone,
        "url": f"/calendar/?claim={event.id}" if stage == "pool" else event_url(event),
        "assigned_to_id": user.id,
        "timestamp": int((now or timezone.now()).timestamp() * 1000),
        "notification_sound": getattr(org, "notification_sound", "sales_flourish"),
        "custom_sound_url": org.custom_notification_sound.url if getattr(org, "custom_notification_sound", None) else None,
    }


def notify_call(event, stage: str, now=None) -> None:
    """Live toast + sound + bell entry for whoever has this call (all their open tabs). A team call nobody has
    claimed yet goes to everyone who could claim it."""
    if event.user_id is None:
        # booked / moved: "a call to claim"; cancelled: so nobody claims a call that's gone
        notify_pool(event, now, stage="cancelled" if stage == "cancelled" else "pool")
        return
    if stage in ("booked", "moved") and not event.urgent:
        from core import slack
        slack.notify(event.organization, "call_booked",
                     f"📅 Call {'moved' if stage == 'moved' else 'booked'}: "
                     f"{contact_name(event.contact) if event.contact_id else event.title} with {caller_name(event)}",
                     _slack_call_lines(event), event_url(event))
    try:
        from messaging.ws_broadcast import broadcast_user_event
        broadcast_user_event(event.user_id, {"event": "call_reminder", "reminder": call_notification(event, stage, now)})
    except Exception as exc:
        logger.warning("Couldn't send the %s notification for call %s: %s", stage, event.id, exc)


def post_chat_note(conversation, body: str):
    """A system line in the chat (shows live in open chats), e.g. "📅 Call booked with Aaron ..."."""
    from messaging.models import Message
    msg = Message.objects.create(
        conversation=conversation, organization=conversation.organization,
        sender_type=Message.SenderType.SYSTEM, direction=Message.Direction.OUTBOUND,
        status=Message.Status.DELIVERED, body=body[:1600],
    )
    try:
        from messaging.utils import format_clock
        from messaging.ws_broadcast import broadcast_chat_event
        broadcast_chat_event(conversation.id, {
            "event": "new_message",
            "message": {
                "id": msg.id, "body": msg.body, "direction": msg.direction, "sender_type": msg.sender_type,
                "created_at": format_clock(msg.created_at, org=conversation.organization), "status": msg.status,
            },
        })
    except Exception as exc:
        logger.warning("Couldn't broadcast the call note for conversation %s: %s", conversation.id, exc)
    return msg


def caller_name(event) -> str:
    if event.user_id is None:
        return "the team (not claimed yet)"
    return event.user.first_name or event.user.email


def booking_summary(event, lead_tz: str) -> str:
    """e.g. "Sat, Nov 22 at 9:00 AM PST (lead's time) with Aaron"."""
    local = event.start_at.astimezone(ZoneInfo(lead_tz))
    abbr = fmt_zone_abbr(local)
    caller = caller_name(event)
    return f"{fmt_day(local)} at {fmt_time(local)}{(' ' + abbr) if abbr else ''} (lead's time) with {caller}"


def refresh_handoff_reason(conversation, event, lead_tz: str) -> None:
    """A handed-off chat whose reason is about a call ("Wants a call - best time: tomorrow at 10am") shows the
    booked time instead, so the banner and alerts never show an old time after the call moves."""
    reason = (conversation.handoff_reason or "").strip()
    if not reason or "call" not in reason.lower():
        return
    local = event.start_at.astimezone(ZoneInfo(lead_tz))
    abbr = fmt_zone_abbr(local)
    conversation.handoff_reason = (f"Wants a call - booked for {fmt_day(local)} at {fmt_time(local)}"
                                   f"{(' ' + abbr) if abbr else ''} (lead's time)")
    conversation.save(update_fields=["handoff_reason"])


def upcoming_call_banner(conversation, viewer):
    """What the chat page shows about the next booked call, in the viewer's time zone (None if nothing booked)."""
    event = upcoming_call(conversation=conversation)
    if not event:
        return None
    lead_tz = lead_timezone(event.contact or conversation.contact, conversation.organization)
    return {
        "event_id": event.id,
        "text": f"{describe_when(event.start_at, user_timezone(viewer))} (your time) with {caller_name(event)}",
        "lead_time": lead_time_line(event.start_at, lead_tz),
        "by_ai": event.source == CalendarEvent.Source.AI,
    }


def lead_time_line(moment, lead_tz: str) -> str:
    local = moment.astimezone(ZoneInfo(lead_tz))
    return f"Lead's local time: {fmt_day(local)} at {fmt_time(local)} · {timezone_label(lead_tz, moment)}"


# ---------------------------------------------------------------------------
# Team call calendar: claiming calls, and assigning the ones nobody claims
# ---------------------------------------------------------------------------

CLAIM_GRACE = timedelta(minutes=2)       # a call booked close to its start still gets a moment to be claimed
CLAIM_MINUTE_CHOICES = [10, 15, 30, 45, 60, 120]


def pool_enabled(org) -> bool:
    return bool(getattr(org, "call_pool_enabled", True))


def claim_minutes(org) -> int:
    value = getattr(org, "call_claim_minutes", None)
    return int(value) if value else 30


def assign_by(event):
    """When an unclaimed call gets assigned automatically ("call me now": 3 minutes after it starts ringing)."""
    if event.urgent:
        return (event.pooled_at or event.created_at) + URGENT_CLAIM
    deadline = event.start_at - timedelta(minutes=claim_minutes(event.organization))
    return max(deadline, (event.pooled_at or event.created_at) + CLAIM_GRACE)


def pool_recipients(org):
    """Who hears about a new team call: everyone available for leads (or the whole team when nobody is)."""
    team = list(call_team(org))
    available = [u for u in team if u.is_available_for_leads]
    return available or team


def _slack_call_lines(event):
    """For Slack: the lead's phone and when the call is, in the lead's own time."""
    contact = event.contact
    lead_tz = lead_timezone(contact, event.organization) if contact else org_timezone(event.organization)
    local = event.start_at.astimezone(ZoneInfo(lead_tz))
    abbr = fmt_zone_abbr(local)
    return [contact.phone if contact else "", f"{fmt_day(local)} at {fmt_time(local)}{(' ' + abbr) if abbr else ''} "
                                              "(lead's time)"]


def notify_pool(event, now=None, stage: str = "pool") -> None:
    from core import slack
    from messaging.ws_broadcast import broadcast_user_event
    if stage == "pool" and not event.urgent:
        slack.notify(event.organization, "team_call", f"📅 New call to claim: {contact_name(event.contact) if event.contact_id else event.title}",
                     [*_slack_call_lines(event), "First agent to claim it gets the lead."],
                     f"/calendar/?claim={event.id}", "Claim it in SparkText")
    for user in pool_recipients(event.organization):
        try:
            broadcast_user_event(user.id, {"event": "call_reminder",
                                           "reminder": call_notification(event, stage, now, recipient=user)})
        except Exception as exc:
            logger.warning("Couldn't tell %s about team call %s: %s", user.id, event.id, exc)


def claim_call(event, user, auto=False, now=None) -> bool:
    """Give this team call to `user` (they claimed it, or it was assigned automatically). The lead becomes theirs
    if nobody has it yet. False when someone else got there first."""
    from messaging.models import Conversation
    now = now or timezone.now()
    taken = CalendarEvent.objects.filter(pk=event.pk, user__isnull=True).update(
        user=user, claimed_at=now, auto_assigned=auto, updated_at=now)
    if not taken:
        return False
    event.refresh_from_db()
    who = user.first_name or user.email
    conversation = event.conversation
    if conversation is not None:
        Conversation.objects.filter(pk=conversation.pk, assigned_to__isnull=True).update(assigned_to=user)
        conversation.refresh_from_db(fields=["assigned_to"])
        lead_tz = lead_timezone(event.contact or conversation.contact, event.organization)
        how = ("Nobody claimed it, so it was assigned automatically to " if auto else "Claimed by ") + who
        post_chat_note(conversation, f"📅 {how}: {booking_summary(event, lead_tz)}.")
    if auto:
        notify_call(event, "assigned", now)
    if event.urgent:
        refresh_urgent(event.organization)            # the new person's screen starts ringing
    logger.info("Team call %s %s %s", event.id, "auto-assigned to" if auto else "claimed by", user.email)
    return True


def _auto_assignee(event):
    """Who gets an unclaimed call: the lead's agent if they have one by now, else the lead routing
    (round-robin / least busy), else the owner."""
    conversation = event.conversation
    if conversation is not None:
        conversation.refresh_from_db(fields=["assigned_to"])
        if conversation.assigned_to_id and conversation.assigned_to.is_active:
            return conversation.assigned_to
        try:
            from messaging.routing import assign_qualified_lead
            routed = assign_qualified_lead(conversation, trigger_reason="Unclaimed team call")
            if routed:
                return routed
        except Exception as exc:
            logger.warning("Routing an unclaimed call (%s) failed: %s", event.id, exc)
    return fallback_caller(event.organization)


def assign_unclaimed_calls(now=None) -> int:
    """Assign every team call nobody claimed in time. Returns how many were assigned."""
    now = now or timezone.now()
    longest = timedelta(minutes=max(CLAIM_MINUTE_CHOICES))
    due = (CalendarEvent.objects.filter(user__isnull=True, status=CalendarEvent.Status.SCHEDULED,
                                        start_at__lte=now + longest)
           .select_related("organization", "conversation", "contact"))
    assigned = 0
    for event in due:
        if assign_by(event) > now:
            continue
        user = _auto_assignee(event)
        if user and claim_call(event, user, auto=True, now=now):
            assigned += 1
    return assigned


def unclaimed_calls(org, now=None):
    """Team calls waiting for an agent, soonest first."""
    now = now or timezone.now()
    return (CalendarEvent.objects.filter(organization=org, user__isnull=True, status=CalendarEvent.Status.SCHEDULED,
                                         end_at__gte=now - timedelta(hours=1))
            .select_related("contact", "conversation", "organization").order_by("start_at"))


# ---------------------------------------------------------------------------
# "Call me now" (hot lead): ring the agents until someone clicks Call
# ---------------------------------------------------------------------------

URGENT_CLAIM = timedelta(minutes=3)          # nobody clicked Call: assigned automatically after this
URGENT_ESCALATE = timedelta(minutes=10)      # still nobody called: owners/admins are told
URGENT_SHOW_FOR = timedelta(minutes=60)      # a hot lead older than this stops showing in the banner


def can_call_now(org, lead_tz: str, now=None):
    """(True, "") when an agent can call this lead right now, else (False, why) in words for the lead."""
    now = now or timezone.now()
    closed = closed_reason_at(now, org)
    if closed:
        return False, f"our office is closed today ({closed})"
    if not can_call_at(now + timedelta(minutes=1), lead_tz, org):
        return False, "our agents aren't available to call right now"
    if not call_team(org).filter(is_available_for_leads=True).exists():
        return False, "our agents aren't available to call right now"
    return True, ""


def _bosses(org):
    from core.models import User
    return list(User.objects.filter(organization=org, is_active=True, role__in=[User.Role.OWNER, User.Role.ADMIN]))


def _is_boss(user) -> bool:
    from core.models import User
    return getattr(user, "role", None) in (User.Role.OWNER, User.Role.ADMIN)


def ring_targets(event) -> list:
    """Whose screen rings: the call's agent, or (a team call) everyone available who hasn't clicked Ignore. Once
    nobody has called for 10 minutes, owners and admins ring too (until they click Call or Ignore)."""
    ignored = set(event.ignored_by or [])
    if event.user_id:
        targets = [event.user] if event.user_id not in ignored else []
    else:
        targets = [u for u in pool_recipients(event.organization) if u.id not in ignored]
    if event.escalated_at:
        have = {u.id for u in targets}
        targets += [b for b in _bosses(event.organization) if b.id not in have and b.id not in ignored]
    return targets


def refresh_urgent(org) -> None:
    """Tell every open tab in the workspace to re-check hot leads (start / stop ringing, show / hide the banner)."""
    try:
        from messaging.ws_broadcast import broadcast_inbox_event
        broadcast_inbox_event(org.id, {"event": "urgent_refresh"})
    except Exception as exc:
        logger.warning("Couldn't refresh hot leads for org %s: %s", getattr(org, "id", None), exc)


def waiting_hot_leads(org, now=None):
    """Hot leads nobody has called yet (newest last)."""
    now = now or timezone.now()
    return (CalendarEvent.objects.filter(organization=org, urgent=True, answered_at__isnull=True,
                                         status=CalendarEvent.Status.SCHEDULED,
                                         start_at__gte=now - URGENT_SHOW_FOR)
            .select_related("user", "contact", "conversation", "organization").order_by("start_at"))


def hot_lead_for(event, viewer, now=None) -> dict:
    """What one person's screen shows for a hot lead: ring or not, and whether their Call button works."""
    now = now or timezone.now()
    targets = {u.id for u in ring_targets(event)}
    escalated = event.escalated_at is not None
    mine = event.user_id is None or event.user_id == viewer.id or (escalated and _is_boss(viewer))
    return {
        "id": event.id,
        "name": contact_name(event.contact) if event.contact_id else event.title,
        "phone": event.contact.phone if event.contact_id else "",
        "chat_url": f"/inbox/{event.conversation_id}/" if event.conversation_id else "",
        "waiting_seconds": max(0, int((now - event.start_at).total_seconds())),
        "asked_at_ms": int(event.start_at.timestamp() * 1000),       # a hot lead's call starts when they asked
        "ring_key": int((event.last_asked_at or event.start_at).timestamp() * 1000),   # a new ask rings again
        "asks": max(1, event.asks or 0),
        "ring": viewer.id in targets,
        "can_call": mine,
        "escalated": escalated,
        "waiting_for": "" if event.user_id is None else (event.user.first_name or event.user.email),
    }


def answer_call(event, user, now=None):
    """`user` clicked Call: a team call becomes theirs (with the lead), the ringing stops everywhere.
    Returns (True, "") or (False, why)."""
    now = now or timezone.now()
    if event.status != CalendarEvent.Status.SCHEDULED:
        return False, "This call isn't waiting anymore."
    if event.user_id is None and not claim_call(event, user, now=now):
        event.refresh_from_db()
    if event.user_id not in (None, user.id) and event.escalated_at and _is_boss(user) and event.answered_at is None:
        # nobody called for 10 minutes: an owner/admin takes it over
        CalendarEvent.objects.filter(pk=event.pk, answered_at__isnull=True).update(user=user, claimed_at=now)
        event.refresh_from_db()
    if event.user_id != user.id:
        return False, f"{event.user.first_name or event.user.email} is already calling this lead."
    CalendarEvent.objects.filter(pk=event.pk, answered_at__isnull=True).update(answered_at=now, updated_at=now)
    event.refresh_from_db()
    if event.conversation_id:
        post_chat_note(event.conversation, f"📞 {user.first_name or user.email} is calling the lead now.")
    refresh_urgent(event.organization)
    return True, ""


def ignore_call(event, user, now=None) -> None:
    """`user` clicked Ignore: their screen stops ringing. When everyone it was ringing for has ignored a team call,
    it goes straight to the owner instead of waiting out the 3 minutes."""
    from django.db import transaction
    now = now or timezone.now()
    with transaction.atomic():
        locked = CalendarEvent.objects.select_for_update().get(pk=event.pk)
        ignored = list(locked.ignored_by or [])
        if user.id not in ignored:
            ignored.append(user.id)
            locked.ignored_by = ignored
            locked.save(update_fields=["ignored_by", "updated_at"])
    event.refresh_from_db()
    if event.user_id is None and event.answered_at is None and not ring_targets(event):
        owner = fallback_caller(event.organization)
        if owner and claim_call(event, owner, auto=True, now=now) and event.conversation_id:
            post_chat_note(event.conversation, "🔥 Everyone who was asked ignored this hot lead, so it went to "
                                               f"{owner.first_name or owner.email}.")
    refresh_urgent(event.organization)


ESCALATE_ON_ASK = 3          # the third "call me now" alerts owners/admins at once


def register_repeat_ask(event, message_id, now=None) -> None:
    """They asked for a call right now AGAIN: whoever clicked Call (or the whole team) rings again, the banner
    returns, and the third ask alerts owners and admins straight away."""
    now = now or timezone.now()
    updated = CalendarEvent.objects.filter(pk=event.pk).exclude(last_ask_message_id=message_id).update(
        asks=models.F("asks") + 1, last_asked_at=now, last_ask_message_id=message_id,
        answered_at=None, ignored_by=[], updated_at=now)
    if not updated:
        return                                          # this text was already counted
    event.refresh_from_db()
    who = "the team" if event.user_id is None else (event.user.first_name or event.user.email)
    nth = {2: "2nd", 3: "3rd"}.get(event.asks, f"{event.asks}th")
    if event.conversation_id:
        post_chat_note(event.conversation, f"🔥 They asked again for a call right now ({nth} time). "
                                           f"Ringing {who} again.")
    from core import slack
    slack.notify(event.organization, "hot_lead",
                 f"🔥 {contact_name(event.contact) if event.contact_id else event.title} asked AGAIN for a call "
                 f"right now ({nth} time)", [event.contact.phone if event.contact_id else ""],
                 event_url(event), "Open the chat")
    if event.asks >= ESCALATE_ON_ASK and event.escalated_at is None:
        _escalate(event, now)
    refresh_urgent(event.organization)


def escalate_hot_leads(now=None) -> int:
    """Owners/admins hear about a hot lead nobody has called 10 minutes after it asked. Returns how many."""
    now = now or timezone.now()
    due = (CalendarEvent.objects.filter(urgent=True, answered_at__isnull=True, escalated_at__isnull=True,
                                        status=CalendarEvent.Status.SCHEDULED, start_at__lte=now - URGENT_ESCALATE,
                                        start_at__gte=now - URGENT_SHOW_FOR)
           .select_related("organization", "contact", "user"))
    told = 0
    for event in due:
        if _escalate(event, now):
            told += 1
    return told


def _escalate(event, now) -> bool:
    """Tell owners/admins a hot lead is still waiting (once per call): 10 minutes after they asked, or on their third
    ask. They ring again even if they clicked Ignore earlier. False if it was already done."""
    from core import slack
    from messaging.ws_broadcast import broadcast_user_event
    if not CalendarEvent.objects.filter(pk=event.pk, escalated_at__isnull=True).update(escalated_at=now):
        return False
    event.escalated_at = now
    bosses = _bosses(event.organization)
    for boss in bosses:
        try:
            broadcast_user_event(boss.id, {"event": "call_reminder",
                                           "reminder": call_notification(event, "escalated", now, recipient=boss)})
        except Exception as exc:
            logger.warning("Couldn't escalate hot lead %s to %s: %s", event.id, boss.id, exc)
    boss_ids = {b.id for b in bosses}
    CalendarEvent.objects.filter(pk=event.pk).update(ignored_by=[i for i in (event.ignored_by or []) if i not in boss_ids])
    why = (f"after asking {event.asks} times" if (event.asks or 0) >= ESCALATE_ON_ASK
           else "10 minutes after they asked")
    who = contact_name(event.contact) if event.contact_id else event.title
    if event.conversation_id:
        post_chat_note(event.conversation, f"🔥 Nobody has reached this hot lead {why}. Owners and admins were alerted.")
    slack.notify(event.organization, "hot_lead_waiting", f"🔥 Still waiting: nobody has reached {who} {why}",
                 [event.contact.phone if event.contact_id else "",
                  f"Waiting for {caller_name(event)}" if event.user_id else "Nobody has taken it yet"],
                 event_url(event), "Open the chat")
    refresh_urgent(event.organization)
    return True
