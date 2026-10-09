"""
Shift edit request business logic.

Flow:
1. Member requests an edit (via bot)
2. Request is stored in Airtable as Pending
3. Admin receives notification with Approve/Reject buttons
4. On approval, the original shift is updated
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import config
from core import airtable_client as at
from core.timeutils import fmt_date_short, fmt_dt, now, parse_dt

logger = logging.getLogger(__name__)


# Statuses a member may ask to correct. Open has no end yet; Locked is
# terminal (pay period closed).
EDITABLE_STATUSES = ("Closed", "Auto-closed", "Edit-approved")


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


def get_editable_shifts(telegram_id: int, limit: int = 7) -> list[dict]:
    """
    Get a member's recent shifts that can be edited
    (Closed, Auto-closed, or Edit-approved — not Open or Locked).
    """
    member = _get_registered_member(telegram_id)

    shifts = at.get_member_shifts(member["id"], limit=limit)

    editable = []
    for s in shifts:
        status = s["fields"].get("Status")
        if status in EDITABLE_STATUSES:
            editable.append({
                "record_id": s["id"],
                "start": s["fields"].get("Start time"),
                "end": s["fields"].get("End time"),
                "duration": s["fields"].get("Duration (hours)"),
                "status": status,
            })

    return editable


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
    window_start = start - timedelta(hours=config.MAX_SHIFT_HOURS)
    shifts = at.get_member_shifts_between(
        member_record_id, window_start.isoformat(), end.isoformat()
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


def _pending_request_for_shift(shift_record_id: str) -> Optional[dict]:
    for r in at.get_pending_edit_requests():
        if shift_record_id in r["fields"].get("Shift", []):
            return r
    return None


def submit_edit_request(
    telegram_id: int,
    shift_record_id: str,
    requested_start: str,
    requested_end: str,
    reason: str,
) -> dict:
    """
    Submit a shift edit request. Validates times, ownership, and shift
    status, stores the request as Pending.
    """
    member = _get_registered_member(telegram_id)

    validate_edit_times(requested_start, requested_end)

    shift = at.get_shift(shift_record_id)
    if not shift:
        raise EditError("Shift not found.")

    # Verify this shift belongs to the requesting member
    if member["id"] not in shift["fields"].get("Member", []):
        raise EditError("This shift doesn't belong to you.")

    # Check shift is editable
    status = shift["fields"].get("Status")
    if status == "Open":
        raise EditError("This shift is still open. Clock out first.")
    if status == "Locked":
        raise EditError("This shift is locked (pay period closed). Contact an admin.")
    if status not in EDITABLE_STATUSES:
        raise EditError(f"This shift can't be edited ({status}).")

    original_start = shift["fields"].get("Start time", "")
    original_end = shift["fields"].get("End time")

    # An edit corrects a shift's times; it never moves it to another day.
    # Moving one was the workaround for a missed clock-in, and it silently
    # deleted the original day's shift.
    orig = parse_dt(original_start)
    req = parse_dt(requested_start)
    if orig and req and orig.date() != req.date():
        raise EditError(
            f"That's a different day from this shift "
            f"({fmt_date_short(orig.date())}). An edit can only change "
            f"the times on the same day."
        )

    if _pending_request_for_shift(shift_record_id):
        raise EditError(
            "This shift already has an edit request waiting for an admin."
        )

    conflict = find_shift_conflict(
        member["id"], parse_dt(requested_start), parse_dt(requested_end),
        exclude_shift_id=shift_record_id,
    )
    if conflict:
        raise EditError(conflict)

    request = at.create_edit_request(
        shift_record_id=shift_record_id,
        member_record_id=member["id"],
        original_start=original_start,
        original_end=original_end,
        requested_start=requested_start,
        requested_end=requested_end,
        reason=reason,
    )

    return {
        "request": request,
        "member_name": member["fields"].get("Name", "Unknown"),
        "original_start": original_start,
        "original_end": original_end,
        "requested_start": requested_start,
        "requested_end": requested_end,
        "reason": reason,
        "shift_record_id": shift_record_id,
    }


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
    original shift, then mark the request Approved.

    The shift is re-checked first — it may have been locked, or another
    shift may have appeared over the requested times, since the request
    was made. Applying before marking means a failed write leaves the
    request Pending (retryable) rather than Approved-but-not-applied.
    """
    admin = _require_admin(admin_telegram_id)
    request = _get_pending_request(request_record_id)

    shift_ids = request["fields"].get("Shift", [])
    if shift_ids:
        shift = at.get_shift(shift_ids[0])
        if not shift:
            raise EditError("The shift for this request no longer exists.")
        status = shift["fields"].get("Status")
        if status not in EDITABLE_STATUSES:
            raise EditError(
                f"Can't apply: the shift is now {status}. Reject this request instead."
            )
        requested_start = request["fields"].get("Requested start")
        requested_end = request["fields"].get("Requested end")
        member_ids = shift["fields"].get("Member", [])
        if member_ids and requested_start and requested_end:
            conflict = find_shift_conflict(
                member_ids[0], parse_dt(requested_start), parse_dt(requested_end),
                exclude_shift_id=shift_ids[0],
            )
            if conflict:
                raise EditError(f"Can't apply. {conflict}")

        fields_to_update = {
            "Status": "Edit-approved",
            "Source": "Edit-approved",
        }
        if requested_start:
            fields_to_update["Start time"] = requested_start
        if requested_end:
            fields_to_update["End time"] = requested_end

        at.update_shift(shift_ids[0], fields_to_update)
    else:
        logger.error("Edit request %s has no linked shift", request_record_id)

    at.update_edit_request(
        request_record_id=request_record_id,
        status="Approved",
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
    }


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
