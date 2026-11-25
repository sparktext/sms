"""
Closed days for the team call calendar: federal holidays the office observes (Settings > Company Profile), plus
single dates the owner closes or reopens on the calendar ("open this Thanksgiving", "closed for the office party").
Gemma never offers or books a call on a closed day. Dates are the team's own (workspace time zone).
"""
from datetime import date, timedelta
from functools import lru_cache

from compliance.quiet_hours import _last_weekday, _nth_weekday

# (key, name, closed by default) in calendar order
HOLIDAYS = [
    ("new_year", "New Year's Day", True),
    ("mlk", "Martin Luther King Jr. Day", False),
    ("presidents", "Presidents' Day", False),
    ("memorial", "Memorial Day", True),
    ("juneteenth", "Juneteenth", False),
    ("independence", "Independence Day", True),
    ("labor", "Labor Day", True),
    ("columbus", "Columbus Day", False),
    ("veterans", "Veterans Day", False),
    ("thanksgiving", "Thanksgiving", True),
    ("christmas", "Christmas Day", True),
]
HOLIDAY_NAMES = {key: name for key, name, _ in HOLIDAYS}
DEFAULT_CLOSED = [key for key, _, closed in HOLIDAYS if closed]


def _observed(day: date) -> date:
    """A fixed-date holiday on a Saturday is observed on Friday, on a Sunday on Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=32)
def holiday_dates(year: int) -> dict:
    """{key: the date it's observed} for that year."""
    return {
        "new_year": _observed(date(year, 1, 1)),
        "mlk": _nth_weekday(year, 1, 0, 3),
        "presidents": _nth_weekday(year, 2, 0, 3),
        "memorial": _last_weekday(year, 5, 0),
        "juneteenth": _observed(date(year, 6, 19)),
        "independence": _observed(date(year, 7, 4)),
        "labor": _nth_weekday(year, 9, 0, 1),
        "columbus": _nth_weekday(year, 10, 0, 2),
        "veterans": _observed(date(year, 11, 11)),
        "thanksgiving": _nth_weekday(year, 11, 3, 4),
        "christmas": _observed(date(year, 12, 25)),
    }


def holiday_on(day: date):
    """The key of the federal holiday observed on this date, or None (New Year's observed on Dec 31 counts too)."""
    for year in (day.year, day.year + 1):
        for key, when in holiday_dates(year).items():
            if when == day:
                return key
    return None


def closed_holidays(org) -> set:
    keys = getattr(org, "closed_holidays", None)
    if not isinstance(keys, (list, tuple)):
        return set(DEFAULT_CLOSED)
    return {k for k in keys if k in HOLIDAY_NAMES}


CACHE_SECONDS = 30


def _overrides(org, start: date = None, end: date = None) -> dict:
    """{date: ClosedDay} for this workspace. Kept on the org object for a few seconds (booking checks many days
    in a row), never longer: a long-lived org object must still see a day the owner just closed."""
    import time

    from .models import ClosedDay
    if org is None or not getattr(org, "pk", None):
        return {}
    cached = getattr(org, "_closed_day_cache", None)
    if cached is None or time.monotonic() - cached[0] > CACHE_SECONDS:
        cached = (time.monotonic(), {d.date: d for d in ClosedDay.objects.filter(organization=org)})
        try:
            org._closed_day_cache = cached
        except AttributeError:
            pass
    return cached[1]


def forget_closed_days(org) -> None:
    """Call after changing this workspace's closed days or holidays."""
    if org is not None and hasattr(org, "_closed_day_cache"):
        del org._closed_day_cache


def closed_reason(org, day: date, overrides=None):
    """Why the office is closed on this date ("Thanksgiving", "Office party"), or None when it's open."""
    if org is None:
        return None
    override = (overrides if overrides is not None else _overrides(org)).get(day)
    if override is not None:
        return (override.name or "Closed") if override.closed else None
    key = holiday_on(day)
    if key and key in closed_holidays(org):
        return HOLIDAY_NAMES[key]
    return None


def is_closed(org, day: date) -> bool:
    return closed_reason(org, day) is not None


def closed_days_between(org, start: date, end: date) -> list:
    """Every date in [start, end] with its state, for the calendar and Gemma:
    [{"date", "name", "closed", "kind": "holiday"|"custom"|"reopened"}] (only days that are special)."""
    overrides = _overrides(org)
    out = []
    day = start
    while day <= end:
        key = holiday_on(day)
        override = overrides.get(day)
        if override is not None:
            if override.closed:
                out.append({"date": day, "name": override.name or (HOLIDAY_NAMES.get(key) if key else "Closed"),
                            "closed": True, "kind": "custom", "holiday": key})
            elif key:
                out.append({"date": day, "name": HOLIDAY_NAMES[key], "closed": False, "kind": "reopened",
                            "holiday": key})
        elif key and key in closed_holidays(org):
            out.append({"date": day, "name": HOLIDAY_NAMES[key], "closed": True, "kind": "holiday", "holiday": key})
        day += timedelta(days=1)
    return out


def upcoming_holidays(org, today: date) -> list:
    """Each federal holiday's next date, and whether the office is closed then (for the settings page)."""
    rows = []
    for key, name, _ in HOLIDAYS:
        when = holiday_dates(today.year)[key]
        if when < today:
            when = holiday_dates(today.year + 1)[key]
        rows.append({"key": key, "name": name, "next": when, "closed": key in closed_holidays(org)})
    return rows
