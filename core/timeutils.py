"""
Shared datetime helpers for kii-bot.

Airtable returns dateTime fields as UTC ISO strings with a 'Z' suffix
(e.g. '2026-04-25T01:00:00.000Z'). Two gotchas this module handles:

1. datetime.fromisoformat() on Python < 3.11 can't parse the 'Z' suffix.
2. Naively formatting the parsed value displays UTC — 8 hours behind SGT.

All parsing and display formatting should go through these helpers so
times are always converted to the configured timezone.
"""

from datetime import datetime, date
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


def lunch_overlap_hours(start: datetime, end: datetime) -> float:
    """
    Hours of overlap between a shift and the unpaid lunch window
    (13:00–14:00 SGT on the shift's start date), clamped to [0, 1].
    Shifts starting before LUNCH_POLICY_START are exempt — history was
    deducted manually and must keep matching what was actually paid.

    This mirrors the Airtable 'Lunch (hours)' formula, which is the pay
    source of truth; this helper exists only for the local fallback in
    clock_out. Keep the two in sync.
    """
    start = start.astimezone(TZ)
    end = end.astimezone(TZ)

    if start.date() < date.fromisoformat(config.LUNCH_POLICY_START):
        return 0.0

    lunch_start = start.replace(hour=config.LUNCH_START_HOUR, minute=0,
                                second=0, microsecond=0)
    lunch_end = start.replace(hour=config.LUNCH_END_HOUR, minute=0,
                              second=0, microsecond=0)
    overlap = min(end, lunch_end) - max(start, lunch_start)
    return max(0.0, overlap.total_seconds() / 3600)


def fmt_date_short(iso_str: Union[str, date]) -> str:
    """Format '2026-04-27' as 'Mon 27 Apr'."""
    try:
        d = date.fromisoformat(iso_str) if isinstance(iso_str, str) else iso_str
        return f"{DAY_NAMES[d.weekday()]} {d.strftime('%d %b')}"
    except (ValueError, TypeError):
        return str(iso_str)
