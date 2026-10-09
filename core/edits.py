"""
Shift edit request business logic.

Flow:
1. Member requests an edit (via bot)
2. Request is stored in Airtable as Pending
3. Admin receives notification with Approve/Reject buttons
4. On approval, the original shift is updated
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import config
from core import airtable_client as at
from core.timeutils import fmt_date_short, fmt_dt, now, parse_dt

logger = logging.getLogger(__name__)


# Statuses a member may ask to correct. Open has no end yet; Locked is
# terminal (pay period closed).
EDITABLE_STATUSES = ("Closed", "Auto-closed", "Edit-approved")

# Admin notes on a request the bot applied itself (see is_trim).
AUTO_APPROVE_NOTE = "Auto-approved: only removes recorded time."


class EditError(Exception):
    """Raised when an edit operation fails for a known reason."""
    pass


def _get_registered_member(telegram_id: int) -> dict:
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise EditError("You're not registered in the system.")
    return member


def _require_admin(telegram_id: int) -> dict:
    admin = at.get_member_by_telegram_id(telegram_id)
    if not admin:
        raise EditError("Admin not found.")
    if not at.is_admin(admin):
        raise EditError("Only admins can review edit requests.")
    return admin


def validate_edit_times(requested_start: str, requested_end: str) -> None:
    """
    Sanity-check requested times. Raises EditError with a
    member-friendly message on failure.
    """
    start = parse_dt(requested_start)
    end = parse_dt(requested_end)
    if start is None or end is None:
        raise EditError("Couldn't parse the requested times.")
    if end <= start:
        raise EditError("End time must be after start time.")
    if start > now():
        raise EditError("Start time can't be in the future.")
    if end > now():
        raise EditError("End time can't be in the future.")
    duration_hours = (end - start).total_seconds() / 3600
    if duration_hours > config.MAX_SHIFT_HOURS:
        raise EditError(
            f"That shift would be {duration_hours:.1f} hours long "
            f"(max {config.MAX_SHIFT_HOURS}). Double-check the dates."
        )


def _shift_summary(s: dict) -> dict:
    return {
        "record_id": s["id"],
        "start": s["fields"].get("Start time"),
        "end": s["fields"].get("End time"),
        "duration": s["fields"].get("Duration (hours)"),
        "status": s["fields"].get("Status"),
    }


def get_editable_shifts(telegram_id: int, limit: int = 7) -> list[dict]:
    """
    Get a member's recent shifts that can be edited
    (Closed, Auto-closed, or Edit-approved — not Open or Locked).
    """
    member = _get_registered_member(telegram_id)

    shifts = at.get_member_shifts(member["id"], limit=limit)
    return [_shift_summary(s) for s in shifts
            if s["fields"].get("Status") in EDITABLE_STATUSES]


def _get_own_editable_shift(member: dict, shift_record_id: str) -> dict:
    shift = at.get_shift(shift_record_id)
    if not shift:
        raise EditError("Shift not found.")

    # Verify this shift belongs to the requesting member
    if member["id"] not in shift["fields"].get("Member", []):
        raise EditError("This shift doesn't belong to you.")

    status = shift["fields"].get("Status")
    if status == "Open":
        raise EditError("This shift is still open. Clock out first.")
    if status == "Locked":
        raise EditError("This shift is locked (pay period closed). Contact an admin.")
    if status not in EDITABLE_STATUSES:
        raise EditError(f"This shift can't be edited ({status}).")
    return shift


def get_editable_shift(telegram_id: int, shift_record_id: str) -> dict:
    """One of the member's own editable shifts (the "Fix this shift"
    button lands here), or EditError saying why not."""
    member = _get_registered_member(telegram_id)
    return _shift_summary(_get_own_editable_shift(member, shift_record_id))


def is_trim(original_start: str, original_end: Optional[str],
            requested_start: str, requested_end: str) -> bool:
    """
    True when the requested times sit inside the recorded ones — the edit
    can only remove time, so it is applied without an admin (Marcus,
    Oct 2026). Lunch can't break this: shrinking a shift by d shrinks
    its lunch overlap by at most d, so paid hours never rise.

    The recorded start carries clock-in seconds (11:10:14) and typed
    times don't, so the start compares at minute resolution: retyping
    the same minute still counts as a trim (< 1 min of slack).
    """
    o_start, o_end = parse_dt(original_start), parse_dt(original_end)
    r_start, r_end = parse_dt(requested_start), parse_dt(requested_end)
    if not (o_start and o_end and r_start and r_end):
        return False
    return (r_start >= o_start.replace(second=0, microsecond=0)
            and r_end <= o_end)


def _overlaps(a_start: datetime, a_end: datetime,
              b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def find_shift_conflict(
    member_record_id: str,
    start: datetime,
    end: datetime,
    exclude_shift_id: Optional[str] = None,
) -> Optional[str]:
    """
    Describe the first of the member's other shifts that [start, end)
    would overlap, or None. An open shift counts as running until now.

    Overlaps are how editing one day's shift into another day double-paid
    people (three cases found live, Oct 2026): nothing else stops it.
    """
    # Any shift that could overlap must start within MAX_SHIFT_HOURS
    # before `start` — that bounds the server-side window.
    # Sent as UTC, the zone Airtable stores and compares in.
    window_start = start - timedelta(hours=config.MAX_SHIFT_HOURS)
    shifts = at.get_member_shifts_between(
        member_record_id,
        window_start.astimezone(timezone.utc).isoformat(),
        end.astimezone(timezone.utc).isoformat(),
    )
    for s in shifts:
        if s["id"] == exclude_shift_id:
            continue
        s_start = parse_dt(s["fields"].get("Start time"))
        s_end = parse_dt(s["fields"].get("End time")) or now()
        if s_start and _overlaps(start, end, s_start, s_end):
            return (f"That overlaps your other shift "
                    f"{fmt_dt(s['fields'].get('Start time'))} → "
                    f"{s_end.strftime('%H:%M')}.")
    return None


def _find_pending_conflict(
    member_record_id: str,
    start: datetime,
    end: datetime,
    shift_record_id: Optional[str] = None,
) -> Optional[str]:
    """
    Refuse a second pending request on the same shift, and one whose
    times overlap another of the member's pending requests (e.g. the same
    missed shift sent twice). Approval re-checks real shifts regardless.
    """
    for r in at.get_pending_edit_requests():
        f = r["fields"]
        if shift_record_id and shift_record_id in f.get("Shift", []):
            return "This shift already has an edit request waiting for an admin."
        if member_record_id not in f.get("Requested by", []):
            continue
        r_start = parse_dt(f.get("Requested start"))
        r_end = parse_dt(f.get("Requested end"))
        if r_start and r_end and _overlaps(start, end, r_start, r_end):
            return (f"That overlaps a request you already sent "
                    f"({fmt_dt(f.get('Requested start'))} → "
                    f"{r_end.strftime('%H:%M')}), still waiting for an admin.")
    return None


def _check_edit(member: dict, shift_record_id: str,
                requested_start: str, requested_end: str) -> dict:
    """Every rule an edit must pass before it is stored. Returns the shift."""
    validate_edit_times(requested_start, requested_end)
    shift = _get_own_editable_shift(member, shift_record_id)

    # An edit corrects a shift's times; it never moves it to another day.
    # Moving one was the workaround for a missed clock-in, and it silently
    # deleted the original day's shift.
    orig = parse_dt(shift["fields"].get("Start time"))
    req_start, req_end = parse_dt(requested_start), parse_dt(requested_end)
    if orig and orig.date() != req_start.date():
        raise EditError(
            f"That's a different day from this shift "
            f"({fmt_date_short(orig.date())}). For a day you didn't clock "
            f"in at all, use ➕ Log a missed shift in /editshift."
        )

    conflict = (
        _find_pending_conflict(member["id"], req_start, req_end,
                               shift_record_id=shift_record_id)
        or find_shift_conflict(member["id"], req_start, req_end,
                               exclude_shift_id=shift_record_id)
    )
    if conflict:
        raise EditError(conflict)
    return shift


def preview_edit(telegram_id: int, shift_record_id: str,
                 requested_start: str, requested_end: str) -> dict:
    """Run every check without writing; says whether it would auto-apply.
    Lets the confirm step catch a clash before the member taps Submit."""
    member = _get_registered_member(telegram_id)
    shift = _check_edit(member, shift_record_id, requested_start, requested_end)
    return {"auto_approve": is_trim(shift["fields"].get("Start time"),
                                    shift["fields"].get("End time"),
                                    requested_start, requested_end)}


def submit_edit_request(
    telegram_id: int,
    shift_record_id: str,
    requested_start: str,
    requested_end: str,
    reason: str,
) -> dict:
    """
    Submit a shift edit request. Validates times, ownership, shift status
    and clashes, then stores the request. A trim (see is_trim) is applied
    at once and stored Approved with no reviewer; anything else waits as
    Pending for an admin.
    """
    member = _get_registered_member(telegram_id)
    shift = _check_edit(member, shift_record_id, requested_start, requested_end)

    original_start = shift["fields"].get("Start time", "")
    original_end = shift["fields"].get("End time")
    auto = is_trim(original_start, original_end, requested_start, requested_end)

    request = at.create_edit_request(
        shift_record_id=shift_record_id,
        member_record_id=member["id"],
        original_start=original_start,
        original_end=original_end,
        requested_start=requested_start,
        requested_end=requested_end,
        reason=reason,
    )

    if auto:
        # Shift first, request second: a failed write leaves it Pending,
        # where an admin sees it, rather than Approved-but-not-applied.
        _apply_to_shift(shift_record_id, requested_start, requested_end)
        at.update_edit_request(
            request_record_id=request["id"],
            status="Approved",
            reviewed_at=now().isoformat(),
            admin_notes=AUTO_APPROVE_NOTE,
        )

    return {
        "request": request,
        "auto_approved": auto,
        "member_name": member["fields"].get("Name", "Unknown"),
        "original_start": original_start,
        "original_end": original_end,
        "requested_start": requested_start,
        "requested_end": requested_end,
        "reason": reason,
        "shift_record_id": shift_record_id,
    }


# ──────────────────────────────────────────────
# Missed shifts (no clock-in at all)
# ──────────────────────────────────────────────

def missed_shift_days(today: Optional[date] = None) -> list[date]:
    """Days a missed shift may be logged for, most recent first."""
    today = today or now().date()
    return [today - timedelta(days=i)
            for i in range(config.MISSED_SHIFT_LOOKBACK_DAYS)]


def _pay_month_is_locked(start: datetime) -> Optional[str]:
    """
    The pay month `start` falls in, if /lockmonth has already run on it.

    Mirrors the Airtable `Pay month` formula, which formats Start time
    WITHOUT a time zone — i.e. in UTC. A shift created after the month is
    locked would sit unlocked and unpaid in a month already paid out.
    """
    pay_month = start.astimezone(timezone.utc).strftime("%Y-%m")
    if any(s["fields"].get("Status") == "Locked"
           for s in at.get_shifts_for_payroll(pay_month)):
        return pay_month
    return None


def _check_missed_shift(member: dict, requested_start: str,
                        requested_end: str) -> None:
    if member["fields"].get("Status") != "Active":
        raise EditError("Your account is not active. Contact an admin.")
    if member["fields"].get("Current hourly rate (SGD)") is None:
        raise EditError("No hourly rate set for your account. Contact an admin.")

    validate_edit_times(requested_start, requested_end)
    start, end = parse_dt(requested_start), parse_dt(requested_end)
    days = missed_shift_days()
    if start.date() not in days:
        raise EditError(
            f"A missed shift can only be logged for the last "
            f"{config.MISSED_SHIFT_LOOKBACK_DAYS} days. Contact an admin."
        )
    locked = _pay_month_is_locked(start)
    if locked:
        raise EditError(f"Pay for {locked} is already locked. Contact an admin.")

    conflict = (_find_pending_conflict(member["id"], start, end)
                or find_shift_conflict(member["id"], start, end))
    if conflict:
        raise EditError(conflict)


def preview_missed_shift(telegram_id: int, requested_start: str,
                         requested_end: str) -> dict:
    member = _get_registered_member(telegram_id)
    _check_missed_shift(member, requested_start, requested_end)
    return {"auto_approve": False}


def submit_missed_shift(
    telegram_id: int,
    requested_start: str,
    requested_end: str,
    reason: str,
) -> dict:
    """
    Ask for a shift that was never clocked. Stored as an edit request
    with NO linked Shift — that is what marks it as a missed shift;
    approve_edit creates the shift. Always needs an admin: it adds time.
    """
    member = _get_registered_member(telegram_id)
    _check_missed_shift(member, requested_start, requested_end)

    request = at.create_edit_request(
        shift_record_id=None,
        member_record_id=member["id"],
        original_start=None,
        original_end=None,
        requested_start=requested_start,
        requested_end=requested_end,
        reason=reason,
    )
    return {
        "request": request,
        "auto_approved": False,
        "member_name": member["fields"].get("Name", "Unknown"),
        "requested_start": requested_start,
        "requested_end": requested_end,
        "reason": reason,
    }


def _apply_to_shift(shift_record_id: str, requested_start: Optional[str],
                    requested_end: Optional[str]) -> None:
    fields_to_update = {
        "Status": "Edit-approved",
        "Source": "Edit-approved",
    }
    if requested_start:
        fields_to_update["Start time"] = requested_start
    if requested_end:
        fields_to_update["End time"] = requested_end
    at.update_shift(shift_record_id, fields_to_update)


def _get_pending_request(request_record_id: str) -> dict:
    request = at.get_edit_request(request_record_id)
    if not request:
        raise EditError("Edit request not found.")
    if request["fields"].get("Status") != "Pending":
        raise EditError(
            f"This request is already {request['fields'].get('Status', 'processed')}."
        )
    return request


def _get_requester(request: dict) -> dict:
    requester_ids = request["fields"].get("Requested by", [])
    return at.get_member(requester_ids[0]) if requester_ids else None


def approve_edit(
    request_record_id: str,
    admin_telegram_id: int,
    admin_notes: str = "",
) -> dict:
    """
    Approve a shift edit request: apply the requested times to the
    original shift — or, for a missed-shift request (no linked Shift),
    create the shift — then mark the request Approved.

    Everything is re-checked first: the shift may have been locked, or
    another shift may have appeared over the requested times, since the
    request was made. Applying before marking means a failed write leaves
    the request Pending (retryable) rather than Approved-but-not-applied.
    """
    admin = _require_admin(admin_telegram_id)
    request = _get_pending_request(request_record_id)
    requested_start = request["fields"].get("Requested start")
    requested_end = request["fields"].get("Requested end")
    start, end = parse_dt(requested_start), parse_dt(requested_end)

    shift_ids = request["fields"].get("Shift", [])
    created_shift_id = None
    if shift_ids:
        shift = at.get_shift(shift_ids[0])
        if not shift:
            raise EditError("The shift for this request no longer exists.")
        status = shift["fields"].get("Status")
        if status not in EDITABLE_STATUSES:
            raise EditError(
                f"Can't apply: the shift is now {status}. Reject this request instead."
            )
        member_ids = shift["fields"].get("Member", [])
        if member_ids and start and end:
            conflict = find_shift_conflict(member_ids[0], start, end,
                                           exclude_shift_id=shift_ids[0])
            if conflict:
                raise EditError(f"Can't apply. {conflict}")
        _apply_to_shift(shift_ids[0], requested_start, requested_end)
    else:
        created_shift_id = _create_missed_shift(request, start, end)

    at.update_edit_request(
        request_record_id=request_record_id,
        status="Approved",
        reviewed_by_record_id=admin["id"],
        reviewed_at=now().isoformat(),
        admin_notes=admin_notes,
        shift_record_id=created_shift_id,
    )

    requester = _get_requester(request)

    return {
        "request": request,
        "admin_name": admin["fields"].get("Name", "Unknown"),
        "requester": requester,
        "requester_telegram_id": (
            requester["fields"].get("Telegram user ID") if requester else None
        ),
    }


def _create_missed_shift(request: dict, start: Optional[datetime],
                         end: Optional[datetime]) -> str:
    """Create the shift a missed-shift request asked for; return its ID.
    The rate is snapshotted now, from the member's current rate, exactly
    as clock-in would have done."""
    requester = _get_requester(request)
    if not requester or not start or not end:
        raise EditError("This request is missing its member or times.")
    conflict = find_shift_conflict(requester["id"], start, end)
    if conflict:
        raise EditError(f"Can't apply. {conflict}")
    locked = _pay_month_is_locked(start)
    if locked:
        raise EditError(
            f"Can't apply: pay for {locked} is already locked. "
            f"Reject this request instead."
        )
    rate = requester["fields"].get("Current hourly rate (SGD)")
    if rate is None:
        raise EditError("Can't apply: the member has no hourly rate set.")

    shift = at.create_shift(
        member_record_id=requester["id"],
        start_time=request["fields"]["Requested start"],
        hourly_rate=rate,
        source="Edit-approved",
        end_time=request["fields"]["Requested end"],
        status="Edit-approved",
    )
    return shift["id"]


def reject_edit(
    request_record_id: str,
    admin_telegram_id: int,
    admin_notes: str = "",
) -> dict:
    """Reject a shift edit request. The original shift is left unchanged."""
    admin = _require_admin(admin_telegram_id)
    request = _get_pending_request(request_record_id)

    at.update_edit_request(
        request_record_id=request_record_id,
        status="Rejected",
        reviewed_by_record_id=admin["id"],
        reviewed_at=now().isoformat(),
        admin_notes=admin_notes,
    )

    requester = _get_requester(request)

    return {
        "request": request,
        "admin_name": admin["fields"].get("Name", "Unknown"),
        "requester": requester,
        "requester_telegram_id": (
            requester["fields"].get("Telegram user ID") if requester else None
        ),
        "admin_notes": admin_notes,
    }
