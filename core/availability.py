"""
Availability management business logic.

Weekly cycle:
1. Thursday 22:00 → prompt members for next week's availability
2. Friday 22:00 → reminder if not submitted
3. Saturday 09:00 → admin digest of who has/hasn't submitted
4. Admin reviews in Airtable, ticks Confirmed
5. Admin runs /confirmweek → bot notifies members of confirmed days
"""

import logging
from datetime import timedelta, date
from typing import Optional

from core import airtable_client as at
from core.timeutils import now

logger = logging.getLogger(__name__)


class AvailabilityError(Exception):
    pass


def _next_monday(from_date: Optional[date] = None) -> date:
    """Get the Monday of the following week."""
    if from_date is None:
        from_date = now().date()

    days_ahead = (7 - from_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # If today is Monday, we mean next Monday
    return from_date + timedelta(days=days_ahead)


def get_next_week_dates(from_date: Optional[date] = None) -> list[date]:
    """Get Monday through Saturday of the following week (6 dates)."""
    monday = _next_monday(from_date)
    return [monday + timedelta(days=i) for i in range(6)]  # Mon-Sat


def get_schedulable_members() -> list[dict]:
    """
    Active members who take part in the weekly scheduling cycle.
    Full-timers are Active team members (and belong in the group chat)
    but work fixed hours — they don't submit availability.
    """
    return [
        m for m in at.get_active_members()
        if m["fields"].get("Role") != "full-timer"
    ]


def get_submission_status(week_starting: str) -> dict:
    """
    Split schedulable members into submitted / not-submitted for a week.
    Fetches availability once (no per-member queries).
    Returns {"submitted": [...], "missing": [...]} of member info dicts.
    """
    active_members = get_schedulable_members()
    week_records = at.get_availability_for_week(week_starting)

    submitted_member_ids = set()
    for record in week_records:
        for member_id in record["fields"].get("Member", []):
            submitted_member_ids.add(member_id)

    submitted, missing = [], []
    for member in active_members:
        info = {
            "member": member,
            "telegram_id": member["fields"].get("Telegram user ID"),
            "name": member["fields"].get("Name", "Unknown"),
        }
        if member["id"] in submitted_member_ids:
            submitted.append(info)
        else:
            missing.append(info)

    return {"submitted": submitted, "missing": missing}


def get_members_needing_prompt(week_starting: str) -> list[dict]:
    """Active members who haven't submitted availability for the week."""
    return get_submission_status(week_starting)["missing"]


def submit_availability(telegram_id: int, dates: list[str]) -> dict:
    """
    Set a member's availability for a week to exactly `dates` (ISO strings).

    Reconciles against what's already stored: newly ticked days are
    created, deselected days are deleted, unchanged days are left alone.
    Both the initial Thursday submission and later /availability edits go
    through this, so re-submitting is idempotent.

    Locked once an admin has ticked Confirmed on ANY of the member's days
    for the week — at that point the roster is being built and edits
    could silently break it; changes go through an admin instead.
    """
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise AvailabilityError("You're not registered in the system.")

    if not dates:
        raise AvailabilityError("No dates selected.")

    # Determine the week from the first date
    first_date = date.fromisoformat(dates[0])
    monday = first_date - timedelta(days=first_date.weekday())
    week_starting = monday.isoformat()

    existing = at.get_member_availability_for_week(member["id"], week_starting)
    if any(r["fields"].get("Confirmed") for r in existing):
        raise AvailabilityError(
            "Your schedule for that week is already being confirmed — "
            "contact an admin if you need to change it."
        )

    existing_by_date = {
        r["fields"].get("Date"): r
        for r in existing
        if r["fields"].get("Date")
    }
    wanted = set(dates)

    created = []
    removed = []
    kept = []
    for d in sorted(wanted - set(existing_by_date)):
        at.create_availability(member["id"], d)
        created.append(d)
    for d, record in existing_by_date.items():
        if d in wanted:
            kept.append(d)
        else:
            at.delete_availability(record["id"])
            removed.append(d)

    return {
        "member_name": member["fields"].get("Name", "Unknown"),
        "created": created,
        "removed": removed,
        "kept": kept,
        "week_starting": week_starting,
    }


def get_member_week_status(telegram_id: int) -> dict:
    """
    A member's current selection + lock state for next week's availability.
    Used by /availability to pre-tick the day keyboard.
    """
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise AvailabilityError("You're not registered in the system.")

    dates = get_next_week_dates()
    week_starting = dates[0].isoformat()
    records = at.get_member_availability_for_week(member["id"], week_starting)

    return {
        "member": member,
        "dates": dates,
        "selected": {r["fields"]["Date"] for r in records if r["fields"].get("Date")},
        "locked": any(r["fields"].get("Confirmed") for r in records),
    }


def get_confirmed_days(telegram_id: int, week_starting: str) -> list[str]:
    """Get the confirmed days for a member in a given week."""
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise AvailabilityError("You're not registered in the system.")

    records = at.get_confirmed_availability(member["id"], week_starting)
    return [r["fields"].get("Date") for r in records if r["fields"].get("Date")]


def notify_confirmed_shifts(week_starting: str) -> list[dict]:
    """
    Get all confirmed availability for the week, grouped by member.
    Used by /confirmweek to send notifications. Marks confirmed records
    as Notified. Members are fetched once and indexed (no N+1 lookups).
    """
    all_availability = at.get_availability_for_week(week_starting)
    members = at.get_all_members_indexed()

    by_member = {}
    for record in all_availability:
        if not record["fields"].get("Confirmed"):
            continue

        member_ids = record["fields"].get("Member", [])
        if not member_ids:
            continue

        member_id = member_ids[0]
        member = members.get(member_id)
        if not member:
            logger.warning("Availability record %s links unknown member %s",
                           record["id"], member_id)
            continue

        if member_id not in by_member:
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
