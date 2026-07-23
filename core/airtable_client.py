"""
Airtable API client for kii-bot.

Uses pyairtable to interact with the shift management base.
All Airtable reads/writes go through this module.

IMPORTANT — linked-record filtering:
Airtable formulas render linked-record fields as the linked record's
*primary field value* (e.g. the member's Name), NOT its record ID.
So formulas like FIND('recXXX', ARRAYJOIN({Member})) never match.
Instead, we filter on non-linked fields server-side (Status, Pay month,
Week starting) and filter by linked record ID client-side — the REST
API returns linked fields as lists of record IDs.

pyairtable docs: https://pyairtable.readthedocs.io/
"""

import logging
from datetime import date, timedelta
from typing import Optional

from pyairtable import Api
from pyairtable.formulas import match

import config

logger = logging.getLogger(__name__)

_api_instance: Optional[Api] = None


def _api() -> Api:
    """Return a shared pyairtable Api instance."""
    global _api_instance
    if _api_instance is None:
        _api_instance = Api(config.AIRTABLE_API_KEY)
    return _api_instance


def _table(table_name: str):
    """Get a pyairtable Table object for the given table name."""
    return _api().table(config.AIRTABLE_BASE_ID, table_name)


def _member_matches(record: dict, member_record_id: str, field: str = "Member") -> bool:
    """Client-side check: does this record's linked field contain the member?"""
    return member_record_id in record["fields"].get(field, [])


def _escape(value: str) -> str:
    """Escape a string for interpolation into an Airtable formula."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


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


def get_member_by_username(username: str) -> Optional[dict]:
    """Look up a team member by Telegram username (without @)."""
    table = _table(config.TABLE_TEAM_MEMBERS)
    formula = match({"Telegram username": username})
    records = table.all(formula=formula)
    if records:
        return records[0]
    return None


def get_member(member_record_id: str) -> Optional[dict]:
    """Fetch a single member record by record ID. Returns None if not found."""
    table = _table(config.TABLE_TEAM_MEMBERS)
    try:
        return table.get(member_record_id)
    except Exception:
        logger.warning("Member record %s not found", member_record_id)
        return None


def get_all_members_indexed() -> dict[str, dict]:
    """
    Fetch all team members in one request, indexed by record ID.
    Use this instead of per-record lookups inside loops (avoids N+1).
    """
    table = _table(config.TABLE_TEAM_MEMBERS)
    return {r["id"]: r for r in table.all()}


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
    """Update a member's current hourly rate. Returns the updated record."""
    table = _table(config.TABLE_TEAM_MEMBERS)
    return table.update(member_record_id, {"Current hourly rate (SGD)": new_rate})


def create_team_member(
    name: str,
    telegram_id: int,
    username: Optional[str] = None,
    status: str = "Pending",
) -> dict:
    """
    Create a Team Members record for a self-registering user (/start).

    New sign-ups land as 'Pending' with no rate/role, so they can't clock in
    (clock_in requires Status == 'Active' and a rate) until an admin finishes
    setup. typecast=True lets Airtable add the 'Pending' Status option
    automatically if the base predates it.
    """
    table = _table(config.TABLE_TEAM_MEMBERS)
    fields = {
        "Name": name,
        "Telegram user ID": telegram_id,
        "Status": status,
    }
    if username:
        fields["Telegram username"] = username
    return table.create(fields, typecast=True)


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

def get_shift(shift_record_id: str) -> Optional[dict]:
    """Fetch a single shift record by record ID. Returns None if not found."""
    table = _table(config.TABLE_SHIFTS)
    try:
        return table.get(shift_record_id)
    except Exception:
        logger.warning("Shift record %s not found", shift_record_id)
        return None


def get_open_shift(member_record_id: str) -> Optional[dict]:
    """
    Find the currently open shift for a member.
    Server-side filter on Status, client-side filter on Member (see module
    docstring). Returns the Airtable record or None.
    """
    table = _table(config.TABLE_SHIFTS)
    open_shifts = table.all(formula=match({"Status": "Open"}))
    for record in open_shifts:
        if _member_matches(record, member_record_id):
            return record
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
    """Return all shifts with Status = 'Open'. Used by the sweep jobs."""
    table = _table(config.TABLE_SHIFTS)
    return table.all(formula=match({"Status": "Open"}))


def get_member_shifts(
    member_record_id: str,
    limit: int = 10,
    pay_month: Optional[str] = None,
) -> list[dict]:
    """
    Get recent shifts for a member, most recent first.
    If pay_month is given (e.g. '2026-04'), filter to that month server-side.
    Member filtering is client-side (see module docstring); we iterate
    pages sorted by most-recent and stop once we have `limit` matches.
    """
    table = _table(config.TABLE_SHIFTS)
    formula = None
    if pay_month:
        formula = f"{{Pay month}} = '{_escape(pay_month)}'"

    results: list[dict] = []
    for page in table.iterate(formula=formula, sort=["-Start time"], page_size=100):
        for record in page:
            if _member_matches(record, member_record_id):
                results.append(record)
                if len(results) >= limit:
                    return results
    return results


def get_shifts_for_payroll(pay_month: str) -> list[dict]:
    """
    Get all closed/approved/locked shifts for a given pay month.
    Used by /payroll and /lockmonth.
    """
    table = _table(config.TABLE_SHIFTS)
    formula = (
        f"AND("
        f"{{Pay month}} = '{_escape(pay_month)}', "
        f"OR({{Status}} = 'Closed', {{Status}} = 'Auto-closed', "
        f"{{Status}} = 'Edit-approved', {{Status}} = 'Locked')"
        f")"
    )
    return table.all(formula=formula, sort=["Start time"])


def get_shifts_since(iso_cutoff: str) -> list[dict]:
    """
    Get all shifts starting after the given ISO datetime, any status.
    Used by the membership audit to find recently-active members
    (one request instead of per-member lookups).
    """
    table = _table(config.TABLE_SHIFTS)
    formula = f"IS_AFTER({{Start time}}, '{_escape(iso_cutoff)}')"
    return table.all(formula=formula)


def batch_update_shifts(updates: list[dict]) -> list[dict]:
    """
    Batch-update shifts. Each entry: {"id": rec_id, "fields": {...}}.
    Used by /lockmonth.
    """
    table = _table(config.TABLE_SHIFTS)
    return table.batch_update(updates)


# ──────────────────────────────────────────────
# Shift Edit Requests
# ──────────────────────────────────────────────

def get_edit_request(request_record_id: str) -> Optional[dict]:
    """Fetch a single edit request by record ID. Returns None if not found."""
    table = _table(config.TABLE_SHIFT_EDIT_REQUESTS)
    try:
        return table.get(request_record_id)
    except Exception:
        logger.warning("Edit request %s not found", request_record_id)
        return None


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
    return table.all(formula=match({"Status": "Pending"}))


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


def _week_date_bounds(week_starting: str) -> tuple[str, str]:
    """
    Exclusive (lower, upper) ISO date bounds for a week's availability.
    Any record dated Monday..Sunday of that week falls strictly between them
    (lower = the Sunday before, upper = the following Monday).
    """
    monday = date.fromisoformat(week_starting)
    return (
        (monday - timedelta(days=1)).isoformat(),
        (monday + timedelta(days=7)).isoformat(),
    )


def get_availability_for_week(week_starting: str) -> list[dict]:
    """
    Get all availability records for a given week.
    week_starting should be the Monday date string, e.g. '2026-04-27'.

    Filters on the raw Date field (Monday..Sunday), NOT the {Week starting}
    formula field. A broken formula silently returns zero matches (that bug
    made the whole availability read path — digest, /confirmweek, reminders —
    report nothing); filtering the stored Date is robust and needs no formula.
    """
    table = _table(config.TABLE_AVAILABILITY)
    lower, upper = _week_date_bounds(week_starting)
    formula = f"AND(IS_AFTER({{Date}}, '{lower}'), IS_BEFORE({{Date}}, '{upper}'))"
    return table.all(formula=formula, sort=["Date"])


def get_member_availability_for_week(
    member_record_id: str, week_starting: str
) -> list[dict]:
    """Get a member's availability records for a given week (client-side member filter)."""
    records = get_availability_for_week(week_starting)
    return [r for r in records if _member_matches(r, member_record_id)]


def get_confirmed_availability(member_record_id: str, week_starting: str) -> list[dict]:
    """Get confirmed availability for a member in a given week."""
    return [
        r
        for r in get_member_availability_for_week(member_record_id, week_starting)
        if r["fields"].get("Confirmed")
    ]


def update_availability(record_id: str, fields: dict) -> dict:
    """Generic update for an availability record (e.g., set Confirmed or Notified)."""
    table = _table(config.TABLE_AVAILABILITY)
    return table.update(record_id, fields)


def delete_availability(record_id: str) -> None:
    """Delete an availability record (member deselected a day via /availability)."""
    table = _table(config.TABLE_AVAILABILITY)
    table.delete(record_id)
