from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utc_now():
    return datetime.now(timezone.utc)


def stamp(value=None):
    return (value or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def next_occurrence(spec, after=None):
    """Return a strictly future UTC slot; skip nonexistent local times on DST jumps.

    Ambiguous local times fire once, using the first occurrence (fold=0).
    """
    after = after or utc_now()
    if after.tzinfo is None:
        raise ValueError("Scheduling requires a timezone-aware reference time")
    try:
        tz = ZoneInfo(spec.get("timezone", "Asia/Kolkata"))
    except (ZoneInfoNotFoundError, TypeError):
        raise ValueError("Unknown timezone; use an IANA name such as Asia/Kolkata") from None
    cadence = spec.get("cadence", "daily")
    if cadence not in ("daily", "weekly"):
        raise ValueError("Cadence must be daily or weekly")
    clock = spec.get("time", "08:00")
    if not isinstance(clock, str) or len(clock) != 5 or clock[2] != ":":
        raise ValueError("Time must use HH:MM")
    try:
        hour, minute = map(int, clock.split(":"))
    except ValueError:
        raise ValueError("Time must use HH:MM") from None
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError("Time must use HH:MM with a valid hour and minute")
    weekday = spec.get("weekday", 0)
    if type(weekday) is not int or not 0 <= weekday <= 6:
        raise ValueError("Weekday must be 0 (Monday) through 6 (Sunday)")
    local = after.astimezone(tz)
    for offset in range(15):
        date = local.date() + timedelta(days=offset)
        if cadence == "weekly" and date.weekday() != weekday:
            continue
        candidate = datetime(date.year, date.month, date.day, hour, minute, tzinfo=tz, fold=0)
        as_utc = candidate.astimezone(timezone.utc)
        if as_utc.astimezone(tz).replace(tzinfo=None) != candidate.replace(tzinfo=None):
            continue
        if as_utc > after:
            return stamp(as_utc)
    raise ValueError("Could not find a valid future schedule slot")
