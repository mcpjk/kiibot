"""
Shared datetime helpers for kii-bot.

Airtable returns dateTime fields as UTC ISO strings with a 'Z' suffix
(e.g. '2026-04-25T01:00:00.000Z'). Two gotchas this module handles:

1. datetime.fromisoformat() on Python < 3.11 can't parse the 'Z' suffix.
2. Naively formatting the parsed value displays UTC — 8 hours behind SGT.

All parsing and display formatting should go through these helpers so
times are always converted to the configured timezone.
"""

import re
from datetime import datetime, date, timedelta
from typing import Optional, Union
from zoneinfo import ZoneInfo

import config

TZ = ZoneInfo(config.TIMEZONE)

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def now() -> datetime:
    """Current time in the configured timezone."""
    return datetime.now(TZ)


def parse_dt(iso_str: str) -> Optional[datetime]:
    """
    Parse an ISO datetime string (Airtable or locally produced) into an
    aware datetime in the configured timezone. Returns None on failure.
    """
    if not iso_str:
        return None
    try:
        # Python <3.11 can't parse a trailing 'Z'
        cleaned = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        # Assume local timezone if naive (shouldn't happen with Airtable)
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def fmt_dt(iso_str: Optional[str]) -> str:
    """Format an ISO datetime string for display: '25 Apr 09:30' (SGT)."""
    dt = parse_dt(iso_str) if iso_str else None
    if dt is None:
        return str(iso_str) if iso_str else "—"
    return dt.strftime("%d %b %H:%M")


def fmt_time(iso_str: Optional[str]) -> str:
    """Format an ISO datetime string for display: '09:30 on 25 Apr' (SGT)."""
    dt = parse_dt(iso_str) if iso_str else None
    if dt is None:
        return str(iso_str) if iso_str else "—"
    return dt.strftime("%H:%M on %d %b")


def round_up_to(dt: datetime, step_minutes: int) -> datetime:
    """
    Round a datetime UP to the next `step_minutes` boundary (SGT).

    Used to pick a planning start time: blocks begin on the grid, so a
    day planned at 09:07 starts at 09:15 rather than carrying a stray
    seven minutes through every block that follows.
    """
    dt = dt.astimezone(TZ).replace(second=0, microsecond=0)
    remainder = dt.minute % step_minutes
    if remainder:
        dt += timedelta(minutes=step_minutes - remainder)
    return dt


def lunch_window(day: datetime) -> tuple[datetime, datetime]:
    """
    The (start, end) of the shared lunch hour on `day`'s date, in SGT.

    One definition, two consumers: the unpaid-lunch deduction fallback
    below, and the design-block extension cascade (core/design.py),
    which treats the hour as an immovable obstacle because the whole
    shop breaks together.
    """
    day = day.astimezone(TZ)
    return (
        day.replace(hour=config.LUNCH_START_HOUR, minute=0,
                    second=0, microsecond=0),
        day.replace(hour=config.LUNCH_END_HOUR, minute=0,
                    second=0, microsecond=0),
    )


def lunch_overlap_hours(start: datetime, end: datetime) -> float:
    """
    Hours of overlap between a shift and the unpaid lunch window
    (13:00–14:00 SGT on the shift's start date), clamped to [0, 1].

    Mirrors the overlap logic of the Airtable 'Lunch (hours)' formula
    (which stores the same overlap in SECONDS as a duration field);
    this helper returns hours for the local fallback in clock_out. Same
    logic, different unit — keep the two in sync.
    """
    start = start.astimezone(TZ)
    end = end.astimezone(TZ)

    lunch_start, lunch_end = lunch_window(start)
    overlap = min(end, lunch_end) - max(start, lunch_start)
    return max(0.0, overlap.total_seconds() / 3600)


_CLOCK_RE = re.compile(
    r"^\s*(\d{1,2})(?:[:.h]?(\d{2}))?\s*([ap])\.?m?\.?\s*$|"
    r"^\s*(\d{1,2})(?:[:.h]?(\d{2}))?\s*$",
    re.IGNORECASE,
)


def parse_clock(text: str) -> Optional[tuple[int, int]]:
    """
    Parse a typed wall-clock time into (hour, minute), or None.

    Lenient on purpose — members type on phones: '18:00', '18.00',
    '1800', '930' (09:30), '9', '6pm', '6:30 pm', '12am' (00:00).
    No date: the caller supplies it.
    """
    m = _CLOCK_RE.match(text or "")
    if not m:
        return None
    if m.group(3):  # 12-hour clock with am/pm
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if m.group(3).lower() == "p" else 0)
    else:
        hour, minute = int(m.group(4)), int(m.group(5) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def at_clock(day: date, hour: int, minute: int) -> datetime:
    """The SGT datetime for `hour:minute` on `day`."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def fmt_date_short(iso_str: Union[str, date]) -> str:
    """Format '2026-04-27' as 'Mon 27 Apr'."""
    try:
        d = date.fromisoformat(iso_str) if isinstance(iso_str, str) else iso_str
        return f"{DAY_NAMES[d.weekday()]} {d.strftime('%d %b')}"
    except (ValueError, TypeError):
        return str(iso_str)
