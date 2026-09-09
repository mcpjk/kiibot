"""
Availability management business logic.

Weekly cycle:
1. Thursday 22:00 → prompt members for next week's availability, and
   generate next week's days for fixed-schedule members (they never
   submit — see generate_fixed_availability)
2. Friday 22:00 → reminder if not submitted
3. Saturday 09:00 → admin digest of who has/hasn't submitted, plus what
   was auto-confirmed for the fixed-schedule members
4. Admin reviews in Airtable, ticks Confirmed (and unticks any
   auto-confirmed day the member is away for)
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
    Explicitly controlled by the 'Weekly availability' checkbox on Team
    Members — the bot never infers cycle membership from employment type
    or job role.
    """
    return [
        m for m in at.get_active_members()
        if m["fields"].get("Weekly availability")
    ]


# Fixed-schedule members' working days, as stored in the Team Members
# 'Fixed days' multi-select. Index = offset from the week's Monday, which
# is why the order matters and why Sunday isn't here: the availability
# week is Mon-Sat (get_next_week_dates).
FIXED_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def get_fixed_schedule_members() -> list[dict]:
    """
    Active members whose week is fixed rather than submitted: contract
    and salaried staff (Employment type 'Full-time') who are NOT in the
    weekly availability cycle.

    The 'Weekly availability' checkbox is what separates them from a
    full-timer who does submit (Marcus): the two paths are mutually
    exclusive, so anyone ticked for the cycle is excluded here — without
    that clause the bot would auto-confirm the boss's week too.
    """
    return [
        m for m in at.get_active_members()
        if m["fields"].get("Employment type") == "Full-time"
        and not m["fields"].get("Weekly availability")
    ]


def fixed_days_for_member(member: dict) -> list[int]:
    """
    A fixed-schedule member's working days as offsets from Monday (0-5).

    Blank 'Fixed days' means the whole Mon-Sat week — the admin unticks
    Confirmed on the days they're away. Pure; no Airtable access.
    """
    chosen = member["fields"].get("Fixed days")
    if not chosen:
        return list(range(len(FIXED_DAY_NAMES)))
    return sorted(
        FIXED_DAY_NAMES.index(name)
        for name in chosen
        if name in FIXED_DAY_NAMES
    )


def generate_fixed_availability(week_starting: str) -> list[dict]:
    """
    Create next week's CONFIRMED availability for fixed-schedule members.

    Runs from the Thursday 22:00 job so the admin has Fri/Sat to untick
    days before /confirmweek. Idempotent and non-destructive: a day that
    already has a record is left exactly as it is, so re-running (or a
    restart, or a manual re-run) never re-ticks a day the admin unticked.
    Mark someone away by unticking Confirmed, NOT by deleting the record
    — a deleted record gets recreated confirmed on the next run.

    Returns one entry per member: {"member", "name", "created", "existing"}.
    """
    members = get_fixed_schedule_members()
    if not members:
        return []

    # One read for the whole week, then index by member record ID
    # client-side (invariant 1: linked fields can't be filtered in a
    # formula, and per-member reads would burn the rate limit).
    existing_by_member: dict[str, set[str]] = {}
    for record in at.get_availability_for_week(week_starting):
        record_date = record["fields"].get("Date")
        if not record_date:
            continue
        for member_id in record["fields"].get("Member", []):
            existing_by_member.setdefault(member_id, set()).add(record_date)

    monday = date.fromisoformat(week_starting)
    results = []
    for member in members:
        name = member["fields"].get("Name", "Unknown")
        existing = existing_by_member.get(member["id"], set())
        wanted = [
            (monday + timedelta(days=offset)).isoformat()
            for offset in fixed_days_for_member(member)
        ]
        created, kept = [], []
        for day in wanted:
            if day in existing:
                kept.append(day)
                continue
            try:
                at.create_availability(member["id"], day, confirmed=True)
            except Exception:
                logger.exception("Failed to create fixed availability for "
                                 "%s on %s", name, day)
                continue
            created.append(day)

        logger.info("Fixed schedule %s: created %d day(s), %d already there",
                    name, len(created), len(kept))
        results.append({
            "member": member,
            "name": name,
            "created": created,
            "existing": kept,
        })

    return results


def get_fixed_schedule_status(week_starting: str) -> list[dict]:
    """
    What the fixed-schedule members are currently CONFIRMED for in a week.

    Reads live Airtable state rather than the generator's return value, so
    the Saturday digest shows the admin's unticking — and a generation
    that silently failed shows up as a short list.
    """
    members = get_fixed_schedule_members()
    if not members:
        return []

    confirmed_by_member: dict[str, list[str]] = {}
    for record in at.get_availability_for_week(week_starting):
        if not record["fields"].get("Confirmed"):
            continue
        record_date = record["fields"].get("Date")
        if not record_date:
            continue
        for member_id in record["fields"].get("Member", []):
            confirmed_by_member.setdefault(member_id, []).append(record_date)

    return [
        {
            "name": m["fields"].get("Name", "Unknown"),
            "dates": sorted(confirmed_by_member.get(m["id"], [])),
        }
        for m in members
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
