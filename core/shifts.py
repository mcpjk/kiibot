"""
Shift management business logic.

All shift operations go through here — the Telegram handlers and
future console interface both call these functions.
"""

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import config
from core import airtable_client as at

TZ = ZoneInfo(config.TIMEZONE)


def _now() -> datetime:
    return datetime.now(TZ)


def _iso(dt: datetime) -> str:
    """Format a datetime as an ISO string for Airtable."""
    return dt.isoformat()


# ──────────────────────────────────────────────
# Clock in / out
# ──────────────────────────────────────────────

class ShiftError(Exception):
    """Raised when a shift operation fails for a known reason."""
    pass


def clock_in(telegram_id: int, source: str = "Telegram") -> dict:
    """
    Clock in a team member.

    Steps:
    1. Look up member by Telegram user ID
    2. Check they don't already have an open shift
    3. Read their current hourly rate
    4. Create a new Shift record with the rate written as a snapshot

    Returns a dict with 'shift' (the Airtable record) and 'member_name'.
    Raises ShiftError if something goes wrong.
    """
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise ShiftError("You're not registered in the system. Ask an admin to add you.")

    if member["fields"].get("Status") != "Active":
        raise ShiftError("Your account is not active. Contact an admin.")

    member_id = member["id"]

    # Enforce one open shift at a time
    existing = at.get_open_shift(member_id)
    if existing:
        start = existing["fields"].get("Start time", "unknown")
        raise ShiftError(f"You're already clocked in since {_format_time(start)}.")

    # Read current rate and snapshot it
    rate = member["fields"].get("Current hourly rate (SGD)")
    if rate is None:
        raise ShiftError("No hourly rate set for your account. Contact an admin.")

    now = _now()
    shift = at.create_shift(
        member_record_id=member_id,
        start_time=_iso(now),
        hourly_rate=rate,
        source=source,
    )

    return {
        "shift": shift,
        "member_name": member["fields"].get("Name", "Unknown"),
        "start_time": now,
        "rate": rate,
    }


def clock_out(telegram_id: int) -> dict:
    """
    Clock out a team member.

    Finds their open shift, sets end time to now, status to Closed.
    Returns shift summary (duration, gross pay, etc.).
    Raises ShiftError if no open shift.
    """
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise ShiftError("You're not registered in the system.")

    member_id = member["id"]
    open_shift = at.get_open_shift(member_id)
    if not open_shift:
        raise ShiftError("You don't have an open shift to close.")

    now = _now()
    at.close_shift(open_shift["id"], _iso(now), status="Closed")

    # Calculate duration and pay locally for the response message
    start_str = open_shift["fields"]["Start time"]
    start = datetime.fromisoformat(start_str)
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)

    duration_seconds = (now - start).total_seconds()
    duration_hours = duration_seconds / 3600
    rate = open_shift["fields"].get("Hourly rate snapshot (SGD)", 0)
    gross = duration_hours * rate

    return {
        "member_name": member["fields"].get("Name", "Unknown"),
        "start_time": start,
        "end_time": now,
        "duration_hours": round(duration_hours, 2),
        "rate": rate,
        "gross_pay": round(gross, 2),
    }


def confirm_shift(telegram_id: int) -> dict:
    """
    Confirm an open shift is still active (response to end-of-day prompt).
    Doesn't change any data — just validates the shift is still open.
    Returns the open shift info for confirmation message.
    """
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise ShiftError("You're not registered in the system.")

    open_shift = at.get_open_shift(member["id"])
    if not open_shift:
        raise ShiftError("You don't have an open shift.")

    start_str = open_shift["fields"]["Start time"]
    return {
        "member_name": member["fields"].get("Name", "Unknown"),
        "start_time": start_str,
        "shift_id": open_shift["id"],
    }


# ──────────────────────────────────────────────
# View shifts
# ──────────────────────────────────────────────

def get_recent_shifts(telegram_id: int, limit: int = 7) -> dict:
    """
    Get a member's recent shifts for display.
    Returns member info + list of formatted shift data.
    """
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise ShiftError("You're not registered in the system.")

    shifts = at.get_member_shifts(member["id"], limit=limit)

    formatted = []
    for s in shifts:
        f = s["fields"]
        formatted.append({
            "record_id": s["id"],
            "start": f.get("Start time"),
            "end": f.get("End time"),
            "duration": f.get("Duration (hours)"),
            "rate": f.get("Hourly rate snapshot (SGD)"),
            "gross": f.get("Gross pay (SGD)"),
            "status": f.get("Status"),
        })

    return {
        "member_name": member["fields"].get("Name", "Unknown"),
        "shifts": formatted,
    }


def get_current_month_summary(telegram_id: int) -> dict:
    """
    Get the current month's total hours and gross pay for a member.
    """
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise ShiftError("You're not registered in the system.")

    pay_month = _now().strftime("%Y-%m")
    shifts = at.get_member_shifts(member["id"], limit=100, pay_month=pay_month)

    total_hours = 0
    total_gross = 0
    count = 0
    for s in shifts:
        f = s["fields"]
        duration = f.get("Duration (hours)")
        gross = f.get("Gross pay (SGD)")
        if duration is not None:
            total_hours += duration
            count += 1
        if gross is not None:
            total_gross += gross

    return {
        "member_name": member["fields"].get("Name", "Unknown"),
        "pay_month": pay_month,
        "shift_count": count,
        "total_hours": round(total_hours, 2),
        "total_gross": round(total_gross, 2),
        "rate": member["fields"].get("Current hourly rate (SGD)"),
    }


def get_rate(telegram_id: int) -> dict:
    """Get a member's current rate."""
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise ShiftError("You're not registered in the system.")

    return {
        "member_name": member["fields"].get("Name", "Unknown"),
        "rate": member["fields"].get("Current hourly rate (SGD)"),
    }


# ──────────────────────────────────────────────
# Auto-close (called by scheduled job)
# ──────────────────────────────────────────────

def get_open_shifts_for_sweep() -> list[dict]:
    """
    Get all currently open shifts with member info.
    Used by the end-of-day sweep job.
    """
    open_shifts = at.get_all_open_shifts()
    results = []
    for shift in open_shifts:
        # We need the member's Telegram ID to message them.
        # The Member field is a linked record — we need to look up the member.
        member_ids = shift["fields"].get("Member", [])
        if not member_ids:
            continue

        # Look up full member record to get Telegram ID
        members_table = at._table(config.TABLE_TEAM_MEMBERS)
        try:
            member = members_table.get(member_ids[0])
        except Exception:
            continue

        results.append({
            "shift": shift,
            "member": member,
            "telegram_id": member["fields"].get("Telegram user ID"),
            "member_name": member["fields"].get("Name", "Unknown"),
            "start_time": shift["fields"].get("Start time"),
        })

    return results


def auto_close_shift(shift_record_id: str, close_time: datetime) -> dict:
    """
    Auto-close a shift. Called by the auto-close sweep job.
    Sets end time to the given close_time and status to Auto-closed.
    """
    return at.close_shift(shift_record_id, _iso(close_time), status="Auto-closed")


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _format_time(iso_str: str) -> str:
    """Format an ISO datetime string for display."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.strftime("%H:%M on %d %b")
    except (ValueError, TypeError):
        return str(iso_str)
