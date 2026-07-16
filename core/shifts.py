"""
Shift management business logic.

All shift operations go through here — the Telegram handlers and
scheduled jobs both call these functions.

Design note: the end-of-day prompt / auto-close cycle is STATELESS.
The 20:00 sweep writes 'Prompted at' on each open shift; /confirmshift
writes 'Confirmed at'; the 21:00 sweep closes open shifts whose
'Prompted at' is set and not superseded by a later 'Confirmed at'.
All state lives in Airtable, so a bot restart between 20:00 and 21:00
loses nothing.
"""

import logging
from datetime import datetime

import config
from core import airtable_client as at
from core.timeutils import TZ, now, parse_dt, fmt_time, lunch_overlap_hours

logger = logging.getLogger(__name__)


def _iso(dt: datetime) -> str:
    """Format a datetime as an ISO string for Airtable."""
    return dt.isoformat()


class ShiftError(Exception):
    """Raised when a shift operation fails for a known reason."""
    pass


def _get_registered_member(telegram_id: int) -> dict:
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise ShiftError(
            "You're not registered in the system. "
            "Send /start to get your Telegram ID and ask an admin to add you."
        )
    return member


# ──────────────────────────────────────────────
# Clock in / out
# ──────────────────────────────────────────────

def clock_in(telegram_id: int, source: str = "Telegram") -> dict:
    """
    Clock in a team member.

    1. Look up member by Telegram user ID
    2. Check they don't already have an open shift
    3. Read their current hourly rate
    4. Create a new Shift record with the rate written as a snapshot
    """
    member = _get_registered_member(telegram_id)

    if member["fields"].get("Status") != "Active":
        raise ShiftError("Your account is not active. Contact an admin.")

    member_id = member["id"]

    # Enforce one open shift at a time
    existing = at.get_open_shift(member_id)
    if existing:
        start = existing["fields"].get("Start time", "")
        raise ShiftError(f"You're already clocked in since {fmt_time(start)}.")

    # Read current rate and snapshot it
    rate = member["fields"].get("Current hourly rate (SGD)")
    if rate is None:
        raise ShiftError("No hourly rate set for your account. Contact an admin.")

    started = now()
    shift = at.create_shift(
        member_record_id=member_id,
        start_time=_iso(started),
        hourly_rate=rate,
        source=source,
    )

    return {
        "shift": shift,
        "member_name": member["fields"].get("Name", "Unknown"),
        "start_time": started,
        "rate": rate,
    }


def clock_out(telegram_id: int) -> dict:
    """
    Clock out a team member.

    Closes the open shift, then re-reads the record so that Duration and
    Gross pay come from Airtable's formula fields — a single source of
    truth. Falls back to local arithmetic only if the formulas haven't
    computed (should not normally happen).
    """
    member = _get_registered_member(telegram_id)

    member_id = member["id"]
    open_shift = at.get_open_shift(member_id)
    if not open_shift:
        raise ShiftError("You don't have an open shift to close.")

    ended = now()
    at.close_shift(open_shift["id"], _iso(ended), status="Closed")

    # Re-read to get Airtable-computed Duration / Gross pay
    updated = at.get_shift(open_shift["id"]) or open_shift
    f = updated["fields"]

    start = parse_dt(f.get("Start time")) or parse_dt(
        open_shift["fields"].get("Start time")
    )
    rate = f.get("Hourly rate snapshot (SGD)", 0)

    duration_hours = f.get("Duration (hours)")
    gross = f.get("Gross pay (SGD)")
    # 'Lunch (hours)' is stored in SECONDS (Airtable duration field);
    # convert to hours for the summary. lunch_overlap_hours() (fallback)
    # already returns hours.
    lunch_seconds = f.get("Lunch (hours)")
    lunch_hours = lunch_seconds / 3600 if lunch_seconds else 0.0
    if duration_hours is None and start is not None:
        logger.warning("Duration formula empty for shift %s; computing locally", updated["id"])
        lunch_hours = lunch_overlap_hours(start, ended)
        duration_hours = (ended - start).total_seconds() / 3600 - lunch_hours
    if gross is None and duration_hours is not None:
        gross = duration_hours * rate

    return {
        "member_name": member["fields"].get("Name", "Unknown"),
        "start_time": start,
        "end_time": ended,
        "duration_hours": round(duration_hours or 0, 2),
        "lunch_hours": round(lunch_hours or 0, 2),
        "rate": rate,
        "gross_pay": round(gross or 0, 2),
    }


def confirm_shift(telegram_id: int) -> dict:
    """
    Confirm an open shift is still active (response to end-of-day prompt).
    Writes 'Confirmed at' so the auto-close sweep skips this shift.
    """
    member = _get_registered_member(telegram_id)

    open_shift = at.get_open_shift(member["id"])
    if not open_shift:
        raise ShiftError("You don't have an open shift.")

    confirmed = now()
    at.update_shift(open_shift["id"], {"Confirmed at": _iso(confirmed)})

    return {
        "member_name": member["fields"].get("Name", "Unknown"),
        "start_time": open_shift["fields"].get("Start time"),
        "shift_id": open_shift["id"],
    }


# ──────────────────────────────────────────────
# View shifts
# ──────────────────────────────────────────────

def get_recent_shifts(telegram_id: int, limit: int = 7) -> dict:
    """Get a member's recent shifts for display."""
    member = _get_registered_member(telegram_id)

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
    """Get the current month's total hours and gross pay for a member."""
    member = _get_registered_member(telegram_id)

    pay_month = now().strftime("%Y-%m")
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
    member = _get_registered_member(telegram_id)

    return {
        "member_name": member["fields"].get("Name", "Unknown"),
        "rate": member["fields"].get("Current hourly rate (SGD)"),
    }


# ──────────────────────────────────────────────
# Sweep jobs (stateless — all state in Airtable)
# ──────────────────────────────────────────────

def get_open_shifts_for_sweep() -> list[dict]:
    """
    Get all currently open shifts with member info attached.
    Members are fetched once and indexed (no per-shift lookups).
    """
    open_shifts = at.get_all_open_shifts()
    if not open_shifts:
        return []

    members = at.get_all_members_indexed()

    results = []
    for shift in open_shifts:
        member_ids = shift["fields"].get("Member", [])
        member = members.get(member_ids[0]) if member_ids else None
        if not member:
            logger.warning("Open shift %s has no resolvable member", shift["id"])
            continue

        results.append({
            "shift": shift,
            "member": member,
            "telegram_id": member["fields"].get("Telegram user ID"),
            "member_name": member["fields"].get("Name", "Unknown"),
            "start_time": shift["fields"].get("Start time"),
        })

    return results


def mark_shift_prompted(shift_record_id: str, prompted_at: datetime) -> dict:
    """Record that the end-of-day prompt was sent for this shift."""
    return at.update_shift(shift_record_id, {"Prompted at": _iso(prompted_at)})


def get_shifts_to_autoclose() -> list[dict]:
    """
    Find open shifts that were prompted and not confirmed afterwards.
    Returns entries with shift, member info, and the prompt time to
    close the shift at.
    """
    to_close = []
    for entry in get_open_shifts_for_sweep():
        f = entry["shift"]["fields"]
        prompted = parse_dt(f.get("Prompted at"))
        if prompted is None:
            continue  # never prompted (e.g. clocked in after the sweep)
        confirmed = parse_dt(f.get("Confirmed at"))
        if confirmed is not None and confirmed >= prompted:
            continue  # member confirmed after the prompt — exempt
        entry["prompt_time"] = prompted
        to_close.append(entry)
    return to_close


def auto_close_shift(shift_record_id: str, close_time: datetime) -> dict:
    """
    Auto-close a shift at the given time (the prompt time, so unresponsive
    shifts are closed at 20:00, not 21:00).
    """
    return at.close_shift(shift_record_id, _iso(close_time), status="Auto-closed")
