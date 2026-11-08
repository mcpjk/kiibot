"""
Member self-service: how involved a part-timer is (Marcus, 2026-11-08).

Three things a part-time member can change about themselves, all from
the bot (/me, and ⏸ on the availability prompt once they've submitted
nothing for PAUSE_OFFER_AFTER_WEEKS weeks):

- **Pause / resume weekly availability.** Flips their own 'Weekly
  availability' checkbox — the same one admins use, deliberately (no
  separate pause flag; Marcus accepted that a member can re-tick a box
  an admin unticked). Pausing stops the Wed/Thu/Fri prompts and nothing
  else: they stay Active, stay in the group, and can still clock in.
  Part-time ONLY: for a Full-time member an unticked box means "fixed
  schedule", and the Wednesday job would auto-confirm their whole week.
- **Leave.** Status → Inactive; the handler then removes them from the
  group. Leaving the group IS leaving Kii, so the audit's invariant
  (Active ⇔ in group) still holds.
- **Rejoin.** Inactive → Active and back in the cycle, self-serve (no
  approval step); admins are DM'd to add them back to the group, which
  stays manual (Marcus, 2026-11-08). Pending is not Inactive:
  first activation stays an admin decision.

No new schema (Marcus, 2026-11-08): no pause date and no check-ins.
A paused member stays quiet until they resume via /me; admins see them
on the Saturday digest's "Paused" line.
"""

import logging
from datetime import date, timedelta
from typing import Optional

import config
from core import airtable_client as at
from core.timeutils import parse_dt, TZ

logger = logging.getLogger(__name__)

WEEKLY_FIELD = "Weekly availability"


class InvolvementError(Exception):
    """A self-service change refused for a reason the member should see."""


NOT_PART_TIME_MSG = (
    "This is for part-timers only — ask an admin to change your setup."
)


# ──────────────────────────────────────────────
# Reading state
# ──────────────────────────────────────────────

def _is_part_time(member: dict) -> bool:
    return member["fields"].get("Employment type") == "Part-time"


def describe(member: dict) -> dict:
    """What /me shows and which buttons it offers. Pure."""
    f = member["fields"]
    status = f.get("Status")
    part_time = _is_part_time(member)
    return {
        "name": f.get("Name", "there"),
        "status": status,
        "part_time": part_time,
        "weekly": bool(f.get(WEEKLY_FIELD)),
        "admin": bool(f.get("Admin")),
        # Which actions apply. Admins never leave via the bot: the audit
        # never removes them either, and a stray tap shouldn't.
        "can_pause": status == "Active" and part_time and bool(f.get(WEEKLY_FIELD)),
        "can_resume": status == "Active" and part_time and not f.get(WEEKLY_FIELD),
        "can_leave": status == "Active" and part_time and not f.get("Admin"),
        "can_rejoin": status == "Inactive" and part_time,
    }


def _member(telegram_id: int) -> dict:
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise InvolvementError("You're not registered yet — send /start.")
    return member


def _active_part_timer(telegram_id: int) -> dict:
    member = _member(telegram_id)
    if member["fields"].get("Status") != "Active":
        raise InvolvementError("Your account isn't active — see /me.")
    if not _is_part_time(member):
        raise InvolvementError(NOT_PART_TIME_MSG)
    return member


def _result(member: dict, changed: bool, **extra) -> dict:
    return {"member": member,
            "name": member["fields"].get("Name", "Unknown"),
            "changed": changed, **extra}


# ──────────────────────────────────────────────
# Changes (each idempotent; `changed` says whether anything was written)
# ──────────────────────────────────────────────

def pause_availability(telegram_id: int) -> dict:
    """Leave the weekly availability cycle. Idempotent."""
    member = _active_part_timer(telegram_id)
    if not member["fields"].get(WEEKLY_FIELD):
        return _result(member, False)
    at.update_member(member["id"], {WEEKLY_FIELD: False})
    return _result(member, True)


def resume_availability(telegram_id: int) -> dict:
    """Back into the weekly cycle (next Wednesday's prompt). Idempotent."""
    member = _active_part_timer(telegram_id)
    if member["fields"].get(WEEKLY_FIELD):
        return _result(member, False)
    at.update_member(member["id"], {WEEKLY_FIELD: True})
    return _result(member, True)


def leave_team(telegram_id: int) -> dict:
    """
    Active → Inactive. The caller removes them from the group (Telegram
    I/O lives in the handler); if that fails, the next audit removes
    them anyway, because Inactive-in-group is exactly what it sweeps.
    Refused with a shift still open — it would close later into the
    pay month of someone who has left.
    """
    member = _member(telegram_id)
    f = member["fields"]
    if f.get("Status") == "Inactive":
        return _result(member, False)
    if f.get("Status") != "Active":
        raise InvolvementError("Your account isn't active — see /me.")
    if not _is_part_time(member):
        raise InvolvementError(NOT_PART_TIME_MSG)
    if f.get("Admin"):
        raise InvolvementError("Admins can't leave from here — change it in Airtable.")
    if at.get_open_shift(member["id"]):
        raise InvolvementError("You're still clocked in — /clockout first.")
    at.update_member(member["id"], {"Status": "Inactive"})
    return _result(member, True)


def rejoin_team(telegram_id: int) -> dict:
    """Inactive → Active and back in the weekly cycle (self-serve; the
    handler tells admins and sends a single-use invite link)."""
    member = _member(telegram_id)
    f = member["fields"]
    if f.get("Status") == "Active":
        return _result(member, False)
    if f.get("Status") != "Inactive":
        # Pending: first activation is the admins' call.
        raise InvolvementError(
            "You're still waiting for an admin to set up your account.")
    if not _is_part_time(member):
        raise InvolvementError(NOT_PART_TIME_MSG)
    at.update_member(member["id"], {"Status": "Active", WEEKLY_FIELD: True})
    return _result(member, True)


# ──────────────────────────────────────────────
# Who has paused (Saturday digest)
# ──────────────────────────────────────────────

def opted_out_members(members: list[dict]) -> list[dict]:
    """Active part-timers out of the weekly cycle. Pure."""
    return [m for m in members
            if m["fields"].get("Status") == "Active"
            and _is_part_time(m)
            and not m["fields"].get(WEEKLY_FIELD)]


# ──────────────────────────────────────────────
# Who is offered ⏸ on the prompt
# ──────────────────────────────────────────────

def quiet_window(next_monday: date) -> tuple[date, date]:
    """The PAUSE_OFFER_AFTER_WEEKS weeks asked about before `next_monday`
    (the week the prompt is for): (first Monday, last Sunday). Pure."""
    weeks = config.PAUSE_OFFER_AFTER_WEEKS
    return next_monday - timedelta(weeks=weeks), next_monday - timedelta(days=1)


def quiet_member_ids(members: list[dict], records: list[dict],
                     window_start: date) -> set[str]:
    """
    Members with no Availability record dated in the window — i.e. no
    days submitted in any of those weeks, whether they ignored the
    prompt or answered 🚫 (which stores no days). Members created after
    the window opened are left out: they haven't been asked four times
    yet. Pure.
    """
    active_ids = {mid for r in records for mid in r["fields"].get("Member", [])}
    quiet = set()
    for m in members:
        if m["id"] in active_ids:
            continue
        created = parse_dt(m.get("createdTime"))
        if created is not None and created.astimezone(TZ).date() > window_start:
            continue
        quiet.add(m["id"])
    return quiet


def get_pause_offer_ids(next_monday: date, members: list[dict]) -> set[str]:
    """Which of `members` (part-timers being prompted) get ⏸ this week.
    One Availability read for the whole window."""
    if not members:
        return set()
    start, end = quiet_window(next_monday)
    records = at.get_availability_between(
        (start - timedelta(days=1)).isoformat(),
        (end + timedelta(days=1)).isoformat())
    return quiet_member_ids(members, records, start)


def get_opted_out_names() -> list[str]:
    """For the Saturday digest: opted-out part-timers, by name."""
    return sorted(m["fields"].get("Name", "Unknown")
                  for m in opted_out_members(at.get_active_members()))


# ──────────────────────────────────────────────
# Admin FYI text
# ──────────────────────────────────────────────

_NOTICES = {
    "pause": "⏸ {name} stopped the weekly availability messages (still Active, still in the group).",
    "resume": "▶️ {name} is back in the weekly availability cycle.",
    "leave": "👋 {name} left Kii via the bot — now Inactive{group}.",
    "rejoin": "🔁 {name} rejoined Kii via the bot — Active again and back in the availability cycle. Add them back to the team group when ready, and check their rate is still right.",
}


def format_admin_notice(action: str, name: str, group_note: str = "") -> str:
    return _NOTICES[action].format(name=name, group=group_note)
