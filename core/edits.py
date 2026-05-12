"""
Shift edit request business logic.

Handles the flow:
1. Member requests an edit (via bot)
2. Request is stored in Airtable as Pending
3. Admin receives notification with Approve/Reject buttons
4. On approval, the original shift is updated
"""

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import config
from core import airtable_client as at

TZ = ZoneInfo(config.TIMEZONE)


class EditError(Exception):
    """Raised when an edit operation fails for a known reason."""
    pass


def get_editable_shifts(telegram_id: int, limit: int = 7) -> list[dict]:
    """
    Get a member's recent shifts that can be edited.
    Returns shifts with Closed, Auto-closed, or Edit-approved status.
    Open shifts should be clocked out first, not edited.
    Locked shifts cannot be edited.
    """
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise EditError("You're not registered in the system.")

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
    Submit a shift edit request.

    The member picks a shift, provides corrected start/end times and a reason.
    The request is stored as Pending and admins are notified.

    Returns the created edit request record and shift details for the
    notification message.
    """
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise EditError("You're not registered in the system.")

    # Get the original shift
    shifts_table = at._table(config.TABLE_SHIFTS)
    try:
        shift = shifts_table.get(shift_record_id)
    except Exception:
        raise EditError("Shift not found.")

    # Verify this shift belongs to the requesting member
    shift_member_ids = shift["fields"].get("Member", [])
    if member["id"] not in shift_member_ids:
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


def approve_edit(
    request_record_id: str,
    admin_telegram_id: int,
    admin_notes: str = "",
) -> dict:
    """
    Approve a shift edit request.

    Updates the edit request status to Approved, then applies
    the requested times to the original shift.
    """
    admin = at.get_member_by_telegram_id(admin_telegram_id)
    if not admin:
        raise EditError("Admin not found.")
    if admin["fields"].get("Role") != "admin":
        raise EditError("Only admins can approve edit requests.")

    # Get the edit request
    edit_table = at._table(config.TABLE_SHIFT_EDIT_REQUESTS)
    try:
        request = edit_table.get(request_record_id)
    except Exception:
        raise EditError("Edit request not found.")

    if request["fields"].get("Status") != "Pending":
        raise EditError(
            f"This request is already {request['fields'].get('Status', 'processed')}."
        )

    now = datetime.now(TZ)

    # Update the edit request
    at.update_edit_request(
        request_record_id=request_record_id,
        status="Approved",
        reviewed_by_record_id=admin["id"],
        reviewed_at=now.isoformat(),
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

    # Get the requesting member's info for notification
    requester_ids = request["fields"].get("Requested by", [])
    requester = None
    if requester_ids:
        members_table = at._table(config.TABLE_TEAM_MEMBERS)
        try:
            requester = members_table.get(requester_ids[0])
        except Exception:
            pass

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
    """
    Reject a shift edit request.
    The original shift is left unchanged.
    """
    admin = at.get_member_by_telegram_id(admin_telegram_id)
    if not admin:
        raise EditError("Admin not found.")
    if admin["fields"].get("Role") != "admin":
        raise EditError("Only admins can reject edit requests.")

    edit_table = at._table(config.TABLE_SHIFT_EDIT_REQUESTS)
    try:
        request = edit_table.get(request_record_id)
    except Exception:
        raise EditError("Edit request not found.")

    if request["fields"].get("Status") != "Pending":
        raise EditError(
            f"This request is already {request['fields'].get('Status', 'processed')}."
        )

    now = datetime.now(TZ)

    at.update_edit_request(
        request_record_id=request_record_id,
        status="Rejected",
        reviewed_by_record_id=admin["id"],
        reviewed_at=now.isoformat(),
        admin_notes=admin_notes,
    )

    requester_ids = request["fields"].get("Requested by", [])
    requester = None
    if requester_ids:
        members_table = at._table(config.TABLE_TEAM_MEMBERS)
        try:
            requester = members_table.get(requester_ids[0])
        except Exception:
            pass

    return {
        "request": request,
        "admin_name": admin["fields"].get("Name", "Unknown"),
        "requester": requester,
        "requester_telegram_id": (
            requester["fields"].get("Telegram user ID") if requester else None
        ),
        "admin_notes": admin_notes,
    }
