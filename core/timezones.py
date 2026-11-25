"""
Time zone helpers shared by Settings and the Calendar.

The workspace time zone (Organization.timezone) is the default for everyone. A person can override it
with their own (UserProfile.timezone); blank means "follow the workspace".
"""
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

DEFAULT_TIMEZONE = "America/Los_Angeles"

# Shown first in pickers; everything else follows under "All time zones"
COMMON_TIMEZONES = [
    ("America/New_York", "Eastern Time (US & Canada)"),
    ("America/Chicago", "Central Time (US & Canada)"),
    ("America/Denver", "Mountain Time (US & Canada)"),
    ("America/Phoenix", "Arizona (no daylight saving)"),
    ("America/Los_Angeles", "Pacific Time (US & Canada)"),
    ("America/Anchorage", "Alaska"),
    ("Pacific/Honolulu", "Hawaii"),
    ("Asia/Kolkata", "India Standard Time"),
    ("Europe/London", "London"),
    ("UTC", "UTC"),
]
_FRIENDLY = dict(COMMON_TIMEZONES)


@lru_cache(maxsize=1)
def all_timezones() -> list:
    return sorted(z for z in available_timezones() if "/" in z or z == "UTC")


def is_valid_timezone(name: str) -> bool:
    return bool(name) and name in set(all_timezones())


def org_timezone(org) -> str:
    tz = getattr(org, "timezone", "") or ""
    return tz if is_valid_timezone(tz) else DEFAULT_TIMEZONE


def personal_timezone(user) -> str:
    """The person's own override, or "" when they follow the workspace."""
    try:
        tz = user.profile.timezone or ""
    except Exception:
        return ""
    return tz if is_valid_timezone(tz) else ""


def user_timezone(user) -> str:
    """The time zone this person's calendar is shown in."""
    return personal_timezone(user) or org_timezone(getattr(user, "organization", None))


def timezone_label(name: str, at: datetime = None) -> str:
    """e.g. "Pacific Time (US & Canada) · PST (UTC−08:00)"."""
    if name == "UTC":
        return "UTC"
    abbr, utc = _abbr_and_offset(name, at)
    friendly = _FRIENDLY.get(name, name.replace("_", " "))
    return f"{friendly} · {f'{abbr} ({utc})' if abbr else utc}"


def timezone_short(name: str, at: datetime = None) -> str:
    """Compact form for buttons, e.g. "New York · EST" or "Kolkata · IST"."""
    if name == "UTC":
        return "UTC"
    abbr, utc = _abbr_and_offset(name, at)
    return f"{name.split('/')[-1].replace('_', ' ')} · {abbr or utc}"


def _abbr_and_offset(name: str, at: datetime = None):
    """("PST", "UTC−08:00") for this zone right now; abbr is "" when the zone only has a numeric one."""
    now = (at or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(name))
    offset = now.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    hh, mm = divmod(abs(minutes), 60)
    utc = f"UTC{'+' if minutes >= 0 else '−'}{hh:02d}:{mm:02d}"
    abbr = now.strftime("%Z")
    return ("" if not abbr or abbr.startswith(("+", "-")) else abbr), utc


def timezone_choices() -> dict:
    """Grouped options for a <select>: common zones first, then every zone."""
    common = [(z, timezone_label(z)) for z, _ in COMMON_TIMEZONES]
    common_names = {z for z, _ in COMMON_TIMEZONES}
    rest = [(z, z.replace("_", " ")) for z in all_timezones() if z not in common_names]
    return {"common": common, "all": rest}
