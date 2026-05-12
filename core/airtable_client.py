"""
Airtable API client for kii-bot.

Uses pyairtable to interact with the shift management base.
All Airtable reads/writes go through this module.

pyairtable docs: https://pyairtable.readthedocs.io/
"""

from pyairtable import Api
from pyairtable.formulas import match, FIELD, STR_VALUE
from datetime import datetime, date
from typing import Optional
import config


def _api() -> Api:
    """Create a fresh pyairtable Api instance."""
    return Api(config.AIRTABLE_API_KEY)


def _table(table_name: str):
    """Get a pyairtable Table object for the given table name."""
    return _api().table(config.AIRTABLE_BASE_ID, table_name)


# ──────────────────────────────────────────────
# Team Members
# ──────────────────────────────────────────────

def get_member_by_telegram_id(telegram_id: int) -> Optional[dict]:
    """
    Look up a team member by their Telegram user ID.
    Returns the full Airtable record (with 'id' and 'fields'), or None.
    """
    table = _table(config.TABLE_TEAM_MEMBERS)
    formula = match({"Telegram user ID": telegram_id})
    records = table.all(formula=formula)
    if records:
        return records[0]
    return None


def get_active_members() -> list[dict]:
    """Return all team members with Status = 'Active'."""
    table = _table(config.TABLE_TEAM_MEMBERS)
    formula = match({"Status": "Active"})
    return table.all(formula=formula)


def get_admin_members() -> list[dict]:
    """Return all team members with Role = 'admin'."""
    table = _table(config.TABLE_TEAM_MEMBERS)
    formula = match({"Role": "admin"})
    return table.all(formula=formula)


def update_member_rate(member_record_id: str, new_rate: float) -> dict:
    """
    Update a member's current hourly rate.
    Returns the updated record.
    Note: The Rate History record should be created separately
    (via Airtable automation or create_rate_history_entry).
    """
    table = _table(config.TABLE_TEAM_MEMBERS)
    return table.update(member_record_id, {"Current hourly rate (SGD)": new_rate})


# ──────────────────────────────────────────────
# Rate History
# ──────────────────────────────────────────────

def create_rate_history_entry(
    member_record_id: str,
    rate: float,
    effective_from: str,
    changed_by: str,
    reason: str = "",
) -> dict:
    """
    Create a new Rate History record.
    effective_from should be an ISO date string, e.g. '2026-04-25'.
    """
    table = _table(config.TABLE_RATE_HISTORY)
    return table.create(
        {
            "Member": [member_record_id],
            "Rate (SGD)": rate,
            "Effective from": effective_from,
            "Changed by": changed_by,
            "Reason": reason,
        }
    )


# ──────────────────────────────────────────────
# Shifts
# ──────────────────────────────────────────────

def get_open_shift(member_record_id: str) -> Optional[dict]:
    """
    Find the currently open shift for a member.
    Returns the Airtable record or None.
    """
    table = _table(config.TABLE_SHIFTS)
    # Formula: Member contains the record ID AND Status = 'Open'
    # pyairtable's match() doesn't handle linked record filtering cleanly,
    # so we use a raw formula string.
    formula = (
        f"AND("
        f"FIND('{member_record_id}', ARRAYJOIN({{Member}})),  "
        f"{{Status}} = 'Open'"
        f")"
    )
    records = table.all(formula=formula)
    if records:
        return records[0]
    return None


def create_shift(
    member_record_id: str,
    start_time: str,
    hourly_rate: float,
    source: str = "Telegram",
) -> dict:
    """
    Create a new shift record (clock in).
    start_time should be an ISO datetime string.
    hourly_rate is written as a snapshot — not a lookup.
    """
    table = _table(config.TABLE_SHIFTS)
    return table.create(
        {
            "Member": [member_record_id],
            "Start time": start_time,
            "Hourly rate snapshot (SGD)": hourly_rate,
            "Status": "Open",
            "Source": source,
        }
    )


def close_shift(shift_record_id: str, end_time: str, status: str = "Closed") -> dict:
    """
    Close a shift (clock out or auto-close).
    status should be 'Closed' for normal clock-out, 'Auto-closed' for runaway shifts.
    """
    table = _table(config.TABLE_SHIFTS)
    return table.update(
        shift_record_id,
        {
            "End time": end_time,
            "Status": status,
        },
    )


def update_shift(shift_record_id: str, fields: dict) -> dict:
    """Generic update for a shift record."""
    table = _table(config.TABLE_SHIFTS)
    return table.update(shift_record_id, fields)


def get_all_open_shifts() -> list[dict]:
    """Return all shifts with Status = 'Open'. Used by end-of-day sweep."""
    table = _table(config.TABLE_SHIFTS)
    formula = match({"Status": "Open"})
    return table.all(formula=formula)


def get_member_shifts(
    member_record_id: str,
    limit: int = 10,
    pay_month: Optional[str] = None,
) -> list[dict]:
    """
    Get recent shifts for a member.
    If pay_month is given (e.g. '2026-04'), filter to that month.
    Returns most recent first.
    """
    table = _table(config.TABLE_SHIFTS)

    if pay_month:
        formula = (
            f"AND("
            f"FIND('{member_record_id}', ARRAYJOIN({{Member}})), "
            f"{{Pay month}} = '{pay_month}'"
            f")"
        )
    else:
        formula = f"FIND('{member_record_id}', ARRAYJOIN({{Member}}))"

    records = table.all(formula=formula, sort=["-Start time"])
    return records[:limit]


def get_shifts_for_payroll(pay_month: str) -> list[dict]:
    """
    Get all closed/approved shifts for a given pay month.
    Used for the monthly payroll summary.
    """
    table = _table(config.TABLE_SHIFTS)
    formula = (
        f"AND("
        f"{{Pay month}} = '{pay_month}', "
        f"OR({{Status}} = 'Closed', {{Status}} = 'Auto-closed', {{Status}} = 'Edit-approved')"
        f")"
    )
    return table.all(formula=formula, sort=["Member", "Start time"])


# ──────────────────────────────────────────────
# Shift Edit Requests
# ──────────────────────────────────────────────

def create_edit_request(
    shift_record_id: str,
    member_record_id: str,
    original_start: str,
    original_end: Optional[str],
    requested_start: str,
    requested_end: str,
    reason: str,
) -> dict:
    """Create a new shift edit request."""
    table = _table(config.TABLE_SHIFT_EDIT_REQUESTS)
    fields = {
        "Shift": [shift_record_id],
        "Requested by": [member_record_id],
        "Original start": original_start,
        "Requested start": requested_start,
        "Requested end": requested_end,
        "Reason": reason,
        "Status": "Pending",
    }
    if original_end:
        fields["Original end"] = original_end
    return table.create(fields)


def get_pending_edit_requests() -> list[dict]:
    """Get all pending edit requests."""
    table = _table(config.TABLE_SHIFT_EDIT_REQUESTS)
    formula = match({"Status": "Pending"})
    return table.all(formula=formula)


def update_edit_request(
    request_record_id: str,
    status: str,
    reviewed_by_record_id: str,
    reviewed_at: str,
    admin_notes: str = "",
) -> dict:
    """Approve or reject an edit request."""
    table = _table(config.TABLE_SHIFT_EDIT_REQUESTS)
    fields = {
        "Status": status,
        "Reviewed by": [reviewed_by_record_id],
        "Reviewed at": reviewed_at,
    }
    if admin_notes:
        fields["Admin notes"] = admin_notes
    return table.update(request_record_id, fields)


# ──────────────────────────────────────────────
# Availability
# ──────────────────────────────────────────────

def create_availability(member_record_id: str, available_date: str) -> dict:
    """
    Create an availability record for a member on a specific date.
    available_date should be ISO date string, e.g. '2026-04-27'.
    """
    table = _table(config.TABLE_AVAILABILITY)
    return table.create(
        {
            "Member": [member_record_id],
            "Date": available_date,
            "Confirmed": False,
            "Notified": False,
        }
    )


def get_availability_for_week(week_starting: str) -> list[dict]:
    """
    Get all availability records for a given week.
    week_starting should be the Monday date string, e.g. '2026-04-27'.
    """
    table = _table(config.TABLE_AVAILABILITY)
    formula = f"{{Week starting}} = '{week_starting}'"
    return table.all(formula=formula, sort=["Date", "Member"])


def get_member_availability_for_week(
    member_record_id: str, week_starting: str
) -> list[dict]:
    """Check if a member has already submitted availability for a given week."""
    table = _table(config.TABLE_AVAILABILITY)
    formula = (
        f"AND("
        f"FIND('{member_record_id}', ARRAYJOIN({{Member}})), "
        f"{{Week starting}} = '{week_starting}'"
        f")"
    )
    return table.all(formula=formula)


def get_confirmed_availability(member_record_id: str, week_starting: str) -> list[dict]:
    """Get confirmed availability for a member in a given week."""
    table = _table(config.TABLE_AVAILABILITY)
    formula = (
        f"AND("
        f"FIND('{member_record_id}', ARRAYJOIN({{Member}})), "
        f"{{Week starting}} = '{week_starting}', "
        f"{{Confirmed}} = TRUE()"
        f")"
    )
    return table.all(formula=formula, sort=["Date"])


def update_availability(record_id: str, fields: dict) -> dict:
    """Generic update for an availability record (e.g., set Confirmed or Notified)."""
    table = _table(config.TABLE_AVAILABILITY)
    return table.update(record_id, fields)
