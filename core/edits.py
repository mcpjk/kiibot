"""
Shift edit request business logic.

Flow:
1. Member requests an edit (via bot)
2. Request is stored in Airtable as Pending
3. Admin receives notification with Approve/Reject buttons
4. On approval, the original shift is updated
"""

import logging

import config
from core import airtable_client as at
from core.timeutils import now, parse_dt

logger = logging.getLogger(__name__)


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
    if admin["fields"].get("Role") != "admin":
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
        if status in ("Closed", "Auto-closed", "Edit-approved"):
            editable.append({
                "record_id": s["id"],
                "start": s["fields"].get("Start time"),
                "end": s["fields"].get("End time"),
                "duration": s["fields"].get("Duration (hours)"),
                "status": status,
            })

    return editable


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

    original_start = shift["fields"].get("Start time", "")
    original_end = shift["fields"].get("End time")

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
    Approve a shift edit request: mark the request Approved, then apply
    the requested times to the original shift.
    """
    admin = _require_admin(admin_telegram_id)
    request = _get_pending_request(request_record_id)

    at.update_edit_request(
        request_record_id=request_record_id,
        status="Approved",
        reviewed_by_record_id=admin["id"],
        reviewed_at=now().isoformat(),
        admin_notes=admin_notes,
    )

    # Apply the changes to the original shift
    shift_ids = request["fields"].get("Shift", [])
    if shift_ids:
        fields_to_update = {
            "Status": "Edit-approved",
            "Source": "Edit-approved",
        }
        requested_start = request["fields"].get("Requested start")
        requested_end = request["fields"].get("Requested end")
        if requested_start:
            fields_to_update["Start time"] = requested_start
        if requested_end:
            fields_to_update["End time"] = requested_end

        at.update_shift(shift_ids[0], fields_to_update)
    else:
        logger.error("Edit request %s has no linked shift", request_record_id)

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
