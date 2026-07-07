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


def fmt_date_short(iso_str: Union[str, date]) -> str:
    """Format '2026-04-27' as 'Mon 27 Apr'."""
    try:
        d = date.fromisoformat(iso_str) if isinstance(iso_str, str) else iso_str
        return f"{DAY_NAMES[d.weekday()]} {d.strftime('%d %b')}"
    except (ValueError, TypeError):
        return str(iso_str)
