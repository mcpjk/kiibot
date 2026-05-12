"""
Availability management business logic.

Weekly cycle:
1. Thursday 22:00 → prompt members for next week's availability
2. Friday 22:00 → reminder if not submitted
3. Friday 23:59 → deadline
4. Saturday → admin reviews in Airtable, ticks Confirmed
5. Admin runs /confirmweek → bot notifies members of confirmed days
"""

from datetime import datetime, timedelta, date
from typing import Optional
from zoneinfo import ZoneInfo

import config
from core import airtable_client as at

TZ = ZoneInfo(config.TIMEZONE)


class AvailabilityError(Exception):
    pass


def _next_monday(from_date: Optional[date] = None) -> date:
    """
    Get the Monday of the following week.
    If from_date is a Thursday, 'next week' means the Monday 4 days later.
    """
    if from_date is None:
        from_date = datetime.now(TZ).date()

    # Days until next Monday: (7 - weekday) where Monday=0
    days_ahead = (7 - from_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # If today is Monday, we mean next Monday
    return from_date + timedelta(days=days_ahead)


def get_next_week_dates(from_date: Optional[date] = None) -> list[date]:
    """
    Get Monday through Saturday of the following week.
    Returns 6 date objects.
    """
    monday = _next_monday(from_date)
    return [monday + timedelta(days=i) for i in range(6)]  # Mon-Sat


def get_members_needing_prompt(week_starting: str) -> list[dict]:
    """
    Get active members who haven't submitted availability for the given week.
    Used by both the Thursday prompt and Friday reminder.
    """
    active_members = at.get_active_members()
    members_needing_prompt = []

    for member in active_members:
        existing = at.get_member_availability_for_week(member["id"], week_starting)
        if not existing:
            members_needing_prompt.append({
                "member": member,
                "telegram_id": member["fields"].get("Telegram user ID"),
                "name": member["fields"].get("Name", "Unknown"),
            })

    return members_needing_prompt


def submit_availability(telegram_id: int, dates: list[str]) -> dict:
    """
    Submit availability for a member.
    dates is a list of ISO date strings, e.g. ['2026-04-27', '2026-04-29'].

    Creates one Availability record per date.
    If the member already has records for any of these dates in the same week,
    those are skipped (no duplicates).
    """
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise AvailabilityError("You're not registered in the system.")

    if not dates:
        raise AvailabilityError("No dates selected.")

    # Determine the week from the first date
    first_date = date.fromisoformat(dates[0])
    # Calculate Monday of that week
    monday = first_date - timedelta(days=first_date.weekday())
    week_starting = monday.isoformat()

    # Check for existing submissions this week
    existing = at.get_member_availability_for_week(member["id"], week_starting)
    existing_dates = {r["fields"].get("Date") for r in existing}

    created = []
    skipped = []
    for d in dates:
        if d in existing_dates:
            skipped.append(d)
            continue
        record = at.create_availability(member["id"], d)
        created.append(d)

    return {
        "member_name": member["fields"].get("Name", "Unknown"),
        "created": created,
        "skipped": skipped,
        "week_starting": week_starting,
    }


def get_confirmed_days(telegram_id: int, week_starting: str) -> list[str]:
    """
    Get the confirmed days for a member in a given week.
    Returns list of date strings.
    """
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise AvailabilityError("You're not registered in the system.")

    records = at.get_confirmed_availability(member["id"], week_starting)
    return [r["fields"].get("Date") for r in records if r["fields"].get("Date")]


def notify_confirmed_shifts(week_starting: str) -> list[dict]:
    """
    Get all confirmed availability for the week, grouped by member.
    Used by /confirmweek to send notifications.

    Returns a list of dicts, one per member, with their confirmed dates
    and Telegram ID.
    """
    all_availability = at.get_availability_for_week(week_starting)

    # Group confirmed records by member
    by_member = {}
    for record in all_availability:
        if not record["fields"].get("Confirmed"):
            continue

        member_ids = record["fields"].get("Member", [])
        if not member_ids:
            continue

        member_id = member_ids[0]
        if member_id not in by_member:
            # Look up member for Telegram ID
            members_table = at._table(config.TABLE_TEAM_MEMBERS)
            try:
                member = members_table.get(member_id)
            except Exception:
                continue
            by_member[member_id] = {
                "member": member,
                "telegram_id": member["fields"].get("Telegram user ID"),
                "name": member["fields"].get("Name", "Unknown"),
                "dates": [],
            }

        by_member[member_id]["dates"].append(record["fields"].get("Date"))

    # Mark all confirmed records as notified
    for record in all_availability:
        if record["fields"].get("Confirmed") and not record["fields"].get("Notified"):
            at.update_availability(record["id"], {"Notified": True})

    return list(by_member.values())
