"""
Availability management business logic.

Weekly cycle:
1. Wednesday 22:00 → prompt members for next week's availability, and
   generate next week's days for fixed-schedule members (they never
   submit — see generate_fixed_availability). Members answer with days
   or with an explicit "not available" (declare_unavailable); either
   way admins get a DM (format_admin_notice)
2. Thursday and Friday 22:00 → reminder if not submitted
3. Saturday 09:00 → admin digest of who has/hasn't submitted, plus what
   was auto-confirmed for the fixed-schedule members; then part-time
   fabricators are auto-selected on thin days (auto_select_part_timers)
   and admins get one picker message per day (get_part_timer_days)
4. Admin adjusts the per-day pickers (or ticks Confirmed in Airtable),
   and unticks any auto-confirmed day a member is away for
5. Admin runs /confirmweek → bot notifies members of confirmed days
"""

import logging
from datetime import timedelta, date
from typing import Optional

from core import airtable_client as at
import config
from core.timeutils import fmt_date_short, now

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

    Runs from the Wednesday 22:00 job so the admin has until Saturday to untick
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


# ──────────────────────────────────────────────
# Saturday part-timer selection
# ──────────────────────────────────────────────

def _has_role(member: dict, role: str) -> bool:
    """Role is a single select today and slated to become a multi-select
    (CLAUDE.md), so accept either shape."""
    value = member["fields"].get("Role")
    if isinstance(value, list):
        return role in value
    return value == role


def is_part_time_fabricator(member: dict) -> bool:
    """
    Who the Saturday auto-select and day pickers are about. Role here is
    job function feeding a function-specific feature (fabrication
    rostering), not access control — the same use README sanctions for
    design scheduling.
    """
    return (member["fields"].get("Employment type") == "Part-time"
            and _has_role(member, "Fabricator"))


def get_part_timer_days(week_starting: str) -> list[dict]:
    """
    The week's part-time fabricator availability, one entry per day
    Mon-Sat (all six, including empty days):
    {"date": ISO, "entries": [{"record_id", "name", "confirmed",
    "notified"}, ...]} with entries sorted by name.

    Only Active members who SUBMITTED that day count — no record, no
    entry. Two reads total; the member filter is client-side
    (invariant 1).
    """
    members = {m["id"]: m for m in at.get_active_members()
               if is_part_time_fabricator(m)}
    monday = date.fromisoformat(week_starting)
    days = {(monday + timedelta(days=i)).isoformat(): []
            for i in range(len(FIXED_DAY_NAMES))}

    for record in at.get_availability_for_week(week_starting):
        f = record["fields"]
        if f.get("Date") not in days:
            continue
        for member_id in f.get("Member", []):
            member = members.get(member_id)
            if not member:
                continue
            days[f["Date"]].append({
                "record_id": record["id"],
                "name": member["fields"].get("Name", "Unknown"),
                "confirmed": bool(f.get("Confirmed")),
                "notified": bool(f.get("Notified")),
            })

    return [
        {"date": d, "entries": sorted(entries, key=lambda e: e["name"])}
        for d, entries in days.items()
    ]


def plan_auto_select(days: list[dict],
                     limit: int = config.PART_TIMER_AUTO_SELECT_MAX
                     ) -> list[str]:
    """
    Record IDs to tick Confirmed: everyone on a day with `limit` or
    fewer part-timers available, skipping anyone already ticked. A day
    with more is left entirely to the admin — no partial pick. Pure.
    """
    return [
        e["record_id"]
        for day in days
        if len(day["entries"]) <= limit
        for e in day["entries"]
        if not e["confirmed"]
    ]


def auto_select_part_timers(week_starting: str) -> list[dict]:
    """
    Saturday auto-select: tick Confirmed for every part-time fabricator
    on a thin day (plan_auto_select), then return the week's days as
    get_part_timer_days does, reflecting the writes.

    Ticking Confirmed also locks those members' /availability edits for
    the week (submit_availability) — intended: the roster is now being
    built. A failed write is logged and shows up as unticked in the
    returned days, so the admin sees the truth rather than the plan.
    """
    days = get_part_timer_days(week_starting)
    to_tick = set(plan_auto_select(days))
    for day in days:
        for e in day["entries"]:
            if e["record_id"] not in to_tick:
                continue
            try:
                at.update_availability(e["record_id"], {"Confirmed": True})
            except Exception:
                logger.exception("Auto-select failed for %s on %s",
                                 e["name"], day["date"])
                continue
            e["confirmed"] = True
            e["auto"] = True
    logger.info("Part-timer auto-select for %s: ticked %d record(s)",
                week_starting, sum(1 for d in days for e in d["entries"]
                                   if e.get("auto")))
    return days


def get_part_timer_day(record_date: str) -> dict:
    """One day's entry from get_part_timer_days (for re-rendering a
    picker message after a tap)."""
    d = date.fromisoformat(record_date)
    monday = d - timedelta(days=d.weekday())
    for day in get_part_timer_days(monday.isoformat()):
        if day["date"] == record_date:
            return day
    # Sunday: never offered, but don't crash on a hand-made record.
    return {"date": record_date, "entries": []}


def toggle_part_timer(record_id: str) -> dict:
    """
    Flip Confirmed on one Availability record from a day picker.

    Reads the record first so the flip is against live state, not the
    message's (possibly stale) rendering. Returns {"date", "confirmed"
    (new value), "notified"} — notified means /confirmweek already told
    the member, so an untick needs a word with them in person.
    """
    record = at.get_availability(record_id)
    if not record or not record["fields"].get("Date"):
        raise AvailabilityError(
            "That day's record is gone — tap 🔄 Refresh.")
    f = record["fields"]
    new_value = not f.get("Confirmed")
    at.update_availability(record_id, {"Confirmed": new_value})
    return {"date": f["Date"], "confirmed": new_value,
            "notified": bool(f.get("Notified"))}


def format_part_timer_day(day: dict,
                          limit: int = config.PART_TIMER_AUTO_SELECT_MAX
                          ) -> str:
    """
    The admin's per-day message. Rendered from live state every time,
    so it can't say who was auto-selected (that's history); it states
    the rule that applied instead.
    """
    entries = day["entries"]
    selected = [e["name"] for e in entries if e["confirmed"]]
    lines = [f"🛠 {fmt_date_short(day['date'])} — part-time fabricators"]
    if not entries:
        lines.append("Nobody available.")
        return "\n".join(lines)

    lines.append(f"Available ({len(entries)}): "
                 + ", ".join(e["name"] for e in entries))
    lines.append(f"Selected ({len(selected)}): "
                 + (", ".join(selected) if selected else "—"))
    if len(entries) <= limit:
        # Only true of those who had submitted when the job ran; a late
        # submitter shows up unticked on Refresh.
        lines.append(f"{limit} or fewer available: everyone who had "
                     "submitted by Sat 09:00 was auto-selected. "
                     "Tap a name to change.")
    else:
        lines.append(f"More than {limit} available — tap names to select.")
    return "\n".join(lines)


def get_submission_status(week_starting: str) -> dict:
    """
    Split schedulable members into submitted / unavailable / not-submitted
    for a week. Fetches availability once (no per-member queries).

    'unavailable' = answered "not available" (their 'Unavailable week' is
    this week) and has no days. That is an answer, so they're neither
    reminded nor listed as missing.

    Returns {"submitted": [...], "unavailable": [...], "missing": [...]}
    of member info dicts.
    """
    active_members = get_schedulable_members()
    week_records = at.get_availability_for_week(week_starting)

    submitted_member_ids = set()
    for record in week_records:
        for member_id in record["fields"].get("Member", []):
            submitted_member_ids.add(member_id)

    submitted, unavailable, missing = [], [], []
    for member in active_members:
        info = {
            "member": member,
            "telegram_id": member["fields"].get("Telegram user ID"),
            "name": member["fields"].get("Name", "Unknown"),
        }
        if member["id"] in submitted_member_ids:
            submitted.append(info)
        elif member["fields"].get("Unavailable week") == week_starting:
            unavailable.append(info)
        else:
            missing.append(info)

    return {"submitted": submitted, "unavailable": unavailable,
            "missing": missing}


def get_members_needing_prompt(week_starting: str) -> list[dict]:
    """Active members who haven't submitted availability for the week."""
    return get_submission_status(week_starting)["missing"]


def submit_availability(telegram_id: int, dates: list[str]) -> dict:
    """
    Set a member's availability for a week to exactly `dates` (ISO strings).

    Reconciles against what's already stored: newly ticked days are
    created, deselected days are deleted, unchanged days are left alone.
    Both the initial prompted submission and later /availability edits go
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

    # One week only, and one that hasn't started. A selection is kept per
    # member, not per message, so taps on two prompts (or a prompt left
    # over from an earlier week) could otherwise write days into a past
    # week, outside the week the lock and delete passes look at.
    try:
        mondays = {
            d - timedelta(days=d.weekday())
            for d in (date.fromisoformat(x) for x in dates)
        }
    except (TypeError, ValueError):
        raise AvailabilityError(_STALE_PROMPT)
    if len(mondays) != 1:
        raise AvailabilityError(_STALE_PROMPT)
    monday = mondays.pop()
    _refuse_started_week(monday)
    week_starting = monday.isoformat()

    existing = _unlocked_week_records(member, week_starting)

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

    # Days supersede an earlier "not available" for the same week.
    was_unavailable = member["fields"].get("Unavailable week") == week_starting
    if was_unavailable:
        at.set_member_unavailable_week(member["id"], None)

    return {
        "member": member,
        "member_name": member["fields"].get("Name", "Unknown"),
        "unavailable": False,
        "was_unavailable": was_unavailable,
        "created": created,
        "removed": removed,
        "kept": kept,
        "week_starting": week_starting,
    }


def declare_unavailable(telegram_id: int, week_starting: str) -> dict:
    """
    Record that a member is not available at all in the week starting
    `week_starting` (a Monday, ISO).

    The explicit counterpart to submit_availability, which refuses an
    empty selection: an empty week would look exactly like "hasn't
    answered" (reminded Friday, listed missing Saturday). Stored as
    'Unavailable week' on Team Members; any days already submitted for
    the week are deleted, so the two states never coexist. Same lock as
    submit_availability. Idempotent.
    """
    try:
        monday = date.fromisoformat(week_starting)
    except (TypeError, ValueError):
        monday = None
    if monday is None or monday.weekday() != 0:
        raise AvailabilityError(_STALE_PROMPT)
    # Only the latest declaration is stored, so a 🚫 tapped on last
    # week's prompt would otherwise replace this week's answer.
    _refuse_started_week(monday)

    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise AvailabilityError("You're not registered in the system.")

    existing = _unlocked_week_records(member, week_starting)

    removed = []
    for record in existing:
        at.delete_availability(record["id"])
        if record["fields"].get("Date"):
            removed.append(record["fields"]["Date"])

    was_unavailable = member["fields"].get("Unavailable week") == week_starting
    if not was_unavailable:
        at.set_member_unavailable_week(member["id"], week_starting)

    return {
        "member": member,
        "member_name": member["fields"].get("Name", "Unknown"),
        "unavailable": True,
        "was_unavailable": was_unavailable,
        "created": [],
        "removed": sorted(removed),
        "kept": [],
        "week_starting": week_starting,
    }


_STALE_PROMPT = "That prompt is out of date — use /availability."


def _refuse_started_week(monday: date) -> None:
    """Members answer for next week only (that's all the prompt and
    /availability offer); a week already under way goes through an admin."""
    if monday < _next_monday():
        raise AvailabilityError(_STALE_PROMPT)


def _unlocked_week_records(member: dict, week_starting: str) -> list[dict]:
    """
    A member's Availability records for the week, raising if the week is
    locked (an admin ticked Confirmed on any of their days — the roster is
    being built, so changes go through an admin).
    """
    existing = at.get_member_availability_for_week(member["id"], week_starting)
    if any(r["fields"].get("Confirmed") for r in existing):
        raise AvailabilityError(
            "Your schedule for that week is already being confirmed — "
            "contact an admin if you need to change it."
        )
    return existing


def format_admin_notice(result: dict) -> Optional[str]:
    """
    The admin DM for a submit_availability / declare_unavailable result,
    or None when nothing changed (a re-submit of the same answer).

    Sent on edits too, not just the first answer: a change after the first
    submission is exactly what an admin building the roster would miss.
    """
    days = sorted(result["kept"] + result["created"])
    first = not (result["kept"] or result["removed"] or result["was_unavailable"])
    if result["unavailable"]:
        changed = bool(result["removed"]) or not result["was_unavailable"]
    else:
        changed = bool(result["created"] or result["removed"]
                       or result["was_unavailable"])
    if not changed:
        return None

    week = date.fromisoformat(result["week_starting"]).strftime("%d %b")
    name = result["member_name"]
    verb = "submitted" if first else "updated"

    if result["unavailable"]:
        lines = [f"🚫 {name} {verb} availability for week of {week}: "
                 f"not available"]
    else:
        lines = [f"📥 {name} {verb} availability for week of {week}: "
                 f"{', '.join(fmt_date_short(d) for d in days)}"]

    if not first:
        if result["created"]:
            lines.append("Added: " + ", ".join(
                fmt_date_short(d) for d in sorted(result["created"])))
        if result["removed"]:
            lines.append("Removed: " + ", ".join(
                fmt_date_short(d) for d in sorted(result["removed"])))
        if result["was_unavailable"] and not result["unavailable"]:
            lines.append("(Previously: not available)")
    return "\n".join(lines)


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
        "unavailable": member["fields"].get("Unavailable week") == week_starting,
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
