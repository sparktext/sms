"""
When SparkText may send a MARKETING text (campaigns) to a lead: the same fixed rules for every customer.

1. Federal TCPA: 8 AM-9 PM in the lead's own local time.
2. Stricter state rules, by the lead's state (state "mini-TCPA" and telephone-solicitation laws). Many of these are
   written for calls; SparkText applies them to texts too, and uses the stricter "automated" window where a state
   has one, because campaign texts are sent automatically. When in doubt, the stricter reading wins.
3. Where the lead is:
     a. the state in their lead details;
     b. otherwise their phone number's area code (state + time zone);
     c. otherwise the agency's own state (Company Profile): carriers only let agencies sell in their own state,
        so that's where their leads are;
     d. still unknown (no agency state either): the "safe everywhere" window, which is allowed in every mainland
        US time zone under the strictest common state hours.

Replies to a lead's own message, and calls the lead asked for, are not telephone solicitations and aren't held
back by this module.

SOURCES (checked Nov 25, 2026; secondary summaries, not legal advice; have counsel review before relying on them):
  https://minyona.com/blog/call-center-calling-and-texting-hours-by-state
  https://www.fransis.ai/articles/texting-quiet-hours-tcpa-state-rules
To change a rule, edit STATE_RULES below; everything else follows.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from django.utils import timezone

Window = tuple[time, time] | None         # (start, end) local time; None = no sending that day

FEDERAL: tuple[time, time] = (time(8, 0), time(21, 0))


def _w(start_h, end_h, start_m=0, end_m=0):
    return (time(start_h, start_m), time(end_h, end_m))


@dataclass(frozen=True)
class StateRule:
    weekday: tuple[time, time]                      # Monday-Friday
    saturday: Window = None                         # defaults to the weekday window (see SAME)
    sunday: Window = None
    holidays: Window = None                         # federal holidays; SAME = like the weekday
    effective_from: date | None = None           # rule applies from this date (federal before it)
    note: str = ""
    saturday_set: bool = field(default=False, repr=False)
    sunday_set: bool = field(default=False, repr=False)
    holidays_set: bool = field(default=False, repr=False)


def rule(weekday, *, saturday="same", sunday="same", holidays="same", effective_from=None, note=""):
    def pick(value):
        return (weekday, False) if value == "same" else (value, True)
    sat, sat_set = pick(saturday)
    sun, sun_set = pick(sunday)
    hol, hol_set = pick(holidays)
    return StateRule(weekday=weekday, saturday=sat, sunday=sun, holidays=hol, effective_from=effective_from,
                     note=note, saturday_set=sat_set, sunday_set=sun_set, holidays_set=hol_set)


# States stricter than the federal 8 AM-9 PM. Everything else: federal.
STATE_RULES: dict[str, StateRule] = {
    "AL": rule(_w(8, 20), sunday=None, holidays=None, note="Ala. Admin. Code r. 770-X-5-.17"),
    "CA": rule(_w(9, 21), note="Automated dialing 9-9 (Cal. Pub. Util. Code 2872)"),
    "CT": rule(_w(9, 20), note="Conn. Gen. Stat. 42-288a(c); texts can be telephonic sales calls"),
    "FL": rule(_w(8, 20), note="Fla. Stat. 501.616(6)"),
    "IL": rule(_w(9, 21), note="Automated 9-9 (815 ILCS 305/15)"),
    "IN": rule(_w(9, 20), note="Automated 9-8 (IC 24-5-14-8)"),
    "KY": rule(_w(10, 21), note="KRS 367.46955(16)"),
    "LA": rule(_w(8, 20), sunday=None, holidays=None, note="La. R.S. 45:811"),
    "MA": rule(_w(8, 20), note="M.G.L. c. 159C, 3"),
    "MD": rule(_w(8, 20), note="Md. Code, Com. Law 14-4502(c)(1)"),
    "ME": rule(_w(9, 17), saturday=None, sunday=None, holidays=None,
               note="Autodialed: 9-5 weekdays (10 M.R.S. 1498)"),
    "MI": rule(_w(9, 21), note="MCL 750.540e"),
    "MN": rule(_w(9, 21), note="Minn. Stat. 325E.30"),
    "MS": rule(_w(8, 20), sunday=None, note="Miss. Code 77-3-723 (Central time)"),
    "NE": rule(FEDERAL, sunday=_w(13, 21), holidays=_w(13, 21), note="Automated: Sun/holidays from 1 PM"),
    "NV": rule(_w(9, 20), note="NRS 598.0918"),
    "NM": rule(_w(9, 21), note="NMSA 57-12-22(B)(5)"),
    "OK": rule(_w(8, 20), note="15 O.S. 775C.4"),
    "OR": rule(_w(8, 20), note="ORS 646.563"),
    "PA": rule(_w(9, 19), sunday=None, holidays=None, effective_from=date(2026, 10, 18),
               note="Act 47 of 2026 (SB 992): 9-7 Mon-Sat, texts included"),
    "PR": rule(_w(9, 21), note="Ley 210-2003"),
    "RI": rule(_w(9, 18), saturday=_w(10, 17), sunday=None, holidays=None, note="R.I. Gen. Laws 5-61-3.6"),
    "SD": rule(_w(9, 21), sunday=None, note="SDCL 37-30A-3"),
    "TX": rule(_w(9, 21), sunday=_w(12, 21), note="Tex. Bus. & Com. Code 301.051"),
    "UT": rule(FEDERAL, sunday=None, holidays=None, note="Utah Code 13-25a-103"),
    "WA": rule(_w(8, 20), note="RCW 80.36.390"),
    "WY": rule(_w(8, 20), note="Wyo. Stat. 40-12-302"),
}

# Unknown location: only when it's allowed in every mainland US time zone under the strictest common hours
# (9 AM-8 PM, Monday-Saturday, no holidays) -> noon-8 PM Eastern = 9 AM-5 PM Pacific.
UNKNOWN_ZONES = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles")
UNKNOWN_RULE = rule(_w(9, 20), sunday=None, holidays=None, note="Location unknown: safe everywhere")


# ---------------------------------------------------------------------------
# Federal holidays (the days observed), used for states that bar holiday messages
# ---------------------------------------------------------------------------

def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


@lru_cache(maxsize=16)
def federal_holidays(year: int) -> frozenset:
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4), date(year, 11, 11), date(year, 12, 25)]
    days = set(fixed)
    for d in fixed:                                 # observed on Friday / Monday when it falls on a weekend
        if d.weekday() == 5:
            days.add(d - timedelta(days=1))
        elif d.weekday() == 6:
            days.add(d + timedelta(days=1))
    days |= {
        _nth_weekday(year, 1, 0, 3),                # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),                # Presidents' Day
        _last_weekday(year, 5, 0),                  # Memorial Day
        _nth_weekday(year, 9, 0, 1),                # Labor Day
        _nth_weekday(year, 10, 0, 2),               # Columbus Day
        _nth_weekday(year, 11, 3, 4),               # Thanksgiving
    }
    return frozenset(days)


# ---------------------------------------------------------------------------
# Where the lead is
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LeadLocation:
    state: str                    # "CA", or "" when unknown
    zones: tuple[str, ...]        # one time zone normally; several (or the mainland set) when unsure
    source: str                   # "lead details" | "phone area code" | "unknown"


def _phone_location(phone: str):
    """(state code, time zones) from a US/Canada number's area code; ("", ()) when it can't tell."""
    try:
        import phonenumbers
        from phonenumbers import geocoder
        from phonenumbers import timezone as pn_tz

        from scheduling.calls import STATE_NAMES
        parsed = phonenumbers.parse(phone or "", "US")
        zones = tuple(z for z in pn_tz.time_zones_for_number(parsed) if z != "Etc/Unknown")
        place = (geocoder.description_for_number(parsed, "en") or "").strip().lower()
        state = STATE_NAMES.get(place, "")
        if not state and "," in place:                                  # "Sacramento, CA"
            tail = place.rsplit(",", 1)[1].strip().upper()
            from scheduling.calls import STATE_TIMEZONES
            state = tail if tail in STATE_TIMEZONES else ""
        return state, zones
    except Exception:
        return "", ()


def us_states():
    """[("AL", "Alabama"), ...] for US states, DC and Puerto Rico."""
    from scheduling.calls import STATE_NAMES
    names = {}
    for name, code in STATE_NAMES.items():
        if name in ("washington dc",):
            continue
        names.setdefault(code, name.title().replace("Of ", "of "))
    return sorted(names.items(), key=lambda kv: kv[1])


def state_name(code: str) -> str:
    return dict(us_states()).get(code or "", code or "")


def agency_state(org) -> str:
    """The agency's state from Company Profile, as a code ("CA"), or "" when not set."""
    from scheduling.calls import STATE_NAMES, STATE_TIMEZONES
    raw = str(getattr(org, "address_state", "") or "").strip()
    if raw.upper() in STATE_TIMEZONES:
        return raw.upper()
    return STATE_NAMES.get(raw.lower(), "")


def lead_location(contact, org=None) -> LeadLocation:
    from scheduling.calls import STATE_TIMEZONES, lead_state
    state = lead_state(contact)
    if state:
        return LeadLocation(state, (STATE_TIMEZONES[state],), "lead details")
    area_state, zones = _phone_location(getattr(contact, "phone", ""))
    if area_state:
        return LeadLocation(area_state, (STATE_TIMEZONES.get(area_state, zones[0] if zones else UNKNOWN_ZONES[0]),),
                            "phone area code")
    if len(zones) == 1:
        return LeadLocation("", zones, "phone area code")
    home = agency_state(org if org is not None else getattr(contact, "organization", None))
    if home:
        return LeadLocation(home, (STATE_TIMEZONES[home],), "agency state")
    return LeadLocation("", UNKNOWN_ZONES, "unknown")


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def _window_for(rule_: StateRule, day: date) -> Window:
    if day in federal_holidays(day.year):
        return rule_.holidays
    if day.weekday() == 5:
        return rule_.saturday
    if day.weekday() == 6:
        return rule_.sunday
    return rule_.weekday


def rule_for(state: str, on: date) -> StateRule:
    r = STATE_RULES.get(state or "")
    if r and (r.effective_from is None or on >= r.effective_from):
        return r
    return rule(FEDERAL)


def allowed_in_zone(moment: datetime, zone: str, rule_: StateRule) -> bool:
    local = moment.astimezone(ZoneInfo(zone))
    window = _window_for(rule_, local.date())
    if window is None:
        return False
    start, end = window
    # Never outside the federal window, whatever a state rule says
    start, end = max(start, FEDERAL[0]), min(end, FEDERAL[1])
    return start <= local.time() < end


def allowed_for(where: LeadLocation, moment: datetime) -> bool:
    if where.source == "unknown":
        return all(allowed_in_zone(moment, z, UNKNOWN_RULE) for z in where.zones)
    local_day = moment.astimezone(ZoneInfo(where.zones[0])).date()
    r = rule_for(where.state, local_day)
    return all(allowed_in_zone(moment, z, r) for z in where.zones)


def can_text_at(contact, moment: datetime | None = None, org=None) -> bool:
    """May a marketing text go to this lead right now? Federal + their state's rules, in their own time."""
    return allowed_for(lead_location(contact, org), moment or timezone.now())


def next_allowed_at(where: LeadLocation, moment: datetime | None = None) -> datetime | None:
    """The first moment (to the minute) a campaign text may go out at this location, from now; None if not this week."""
    moment = (moment or timezone.now()).replace(second=0, microsecond=0)
    if allowed_for(where, moment):
        return moment
    probe = moment
    for _ in range(8 * 24 * 4):                     # quarter-hours over the next 8 days, then the exact minute
        nxt = probe + timedelta(minutes=15)
        if allowed_for(where, nxt):
            for minute in range(15):
                exact = probe + timedelta(minutes=minute + 1)
                if allowed_for(where, exact):
                    return exact
            return nxt
        probe = nxt
    return None


def _clock(t: time) -> str:
    return f"{t.hour % 12 or 12}{'' if t.minute == 0 else f':{t.minute:02d}'} {'AM' if t.hour < 12 else 'PM'}"


def _join_and(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def plain_hours(where: LeadLocation) -> tuple[str, str]:
    """In plain words, for people: ("between 9 AM and 7 PM, and never on Sundays or holidays", "Pennsylvania law")."""
    if where.source == "unknown":
        return "between noon and 8 PM Eastern time, Monday to Saturday", "the rules for an unknown location"
    r = rule_for(where.state, timezone.now().date())
    start, end = max(r.weekday[0], FEDERAL[0]), min(r.weekday[1], FEDERAL[1])
    text = f"between {_clock(start)} and {_clock(end)}"
    other, never = [], []
    for label, window in (("Saturdays", r.saturday), ("Sundays", r.sunday), ("holidays", r.holidays)):
        if window is None:
            never.append(label)
        elif window != r.weekday:
            other.append(f"{label} {_clock(window[0])} to {_clock(window[1])}")
    if other:
        text += " (" + ", ".join(other) + ")"
    if never:
        text += ", and never on " + _join_and(never)
    law = f"{state_name(where.state)} law" if where.state in STATE_RULES else "federal law"
    return text, law


def describe_hours(where: LeadLocation) -> str:
    """e.g. "9 AM-9 PM (California rule)", "9 AM-7 PM, Mon-Sat, no holidays (Pennsylvania rule)"."""
    if where.source == "unknown":
        return "noon-8 PM Eastern, Mon-Sat (location unknown, so only hours that are allowed everywhere)"
    r = rule_for(where.state, timezone.now().date())
    start, end = max(r.weekday[0], FEDERAL[0]), min(r.weekday[1], FEDERAL[1])
    parts = [f"{_clock(start)}-{_clock(end)}"]
    if r.saturday != r.weekday:
        parts.append("Sat " + (f"{_clock(r.saturday[0])}-{_clock(r.saturday[1])}" if r.saturday else "none"))
    if r.sunday != r.weekday:
        parts.append(f"Sun {_clock(r.sunday[0])}-{_clock(r.sunday[1])}" if r.sunday else "no Sundays")
    if r.holidays is None:
        parts.append("no holidays")
    elif r.holidays != r.weekday:
        parts.append(f"holidays {_clock(r.holidays[0])}-{_clock(r.holidays[1])}")
    whose = f"{state_name(where.state)} rule" if where.state in STATE_RULES else "federal rule"
    return f"{', '.join(parts)} ({whose})"
