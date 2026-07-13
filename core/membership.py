"""
Group-chat membership audit.

Invariant: everyone with Status = Active belongs in the group chat;
nobody with Status = Inactive does. Roles don't change that — full-timers
are Active members too, they're just excluded from the shift-scheduling
cycle.

The Telegram Bot API cannot list a group's members, so the audit is
Airtable-driven: for every Team Members row with a Telegram ID we ask
get_chat_member() whether that user is currently in the group, then:

- Inactive + in group  → removed by the bot (ban + immediate unban, so
  they can be re-invited later). The human decision stays in Airtable —
  flipping Status to Inactive is what triggers removal at the next audit.
- Active + not in group → reported to admins (send them the invite link).
- Active part-timer with no shift in STALE_SHIFT_WEEKS → flagged for
  review only. Never auto-flipped: Status gates pay/access, so a human
  decides.
- Inactive admins in the group are reported, never auto-removed.

Runs from /confirmweek so the check happens while attention is already
on the week's organisation. Stateless: derives everything fresh from
Airtable + Telegram each run, so re-running is harmless (removals are
naturally idempotent — a removed member is no longer in the group).
"""

import logging
from datetime import timedelta

import config
from core import airtable_client as at
from core.timeutils import now

logger = logging.getLogger(__name__)

# get_chat_member statuses that count as "currently in the group".
IN_GROUP_STATUSES = {"creator", "administrator", "member", "restricted"}


def classify_members(
    members: list[dict],
    in_group_tg_ids: set[int],
    recent_shift_member_ids: set[str],
) -> dict:
    """
    Pure classification (no I/O) — the testable heart of the audit.

    members: Team Members records; in_group_tg_ids: Telegram IDs currently
    in the group; recent_shift_member_ids: Airtable record IDs of members
    with a shift in the staleness window.
    """
    result = {
        "to_remove": [],        # Inactive non-admins in the group
        "inactive_admins": [],  # Inactive admins in the group (report only)
        "missing": [],          # Active members not in the group
        "stale": [],            # Active part-timers with no recent shift
        "no_telegram": [],      # Active members we can't check (no ID)
    }

    for member in members:
        f = member["fields"]
        status = f.get("Status")
        role = f.get("Role")
        tg_id = f.get("Telegram user ID")
        info = {
            "member": member,
            "name": f.get("Name", "Unknown"),
            "telegram_id": tg_id,
        }

        if status == "Active":
            if not tg_id:
                result["no_telegram"].append(info)
            elif tg_id not in in_group_tg_ids:
                result["missing"].append(info)

            if role == "part-timer" and member["id"] not in recent_shift_member_ids:
                result["stale"].append(info)

        elif status == "Inactive" and tg_id and tg_id in in_group_tg_ids:
            if role == "admin":
                result["inactive_admins"].append(info)
            else:
                result["to_remove"].append(info)

        # Pending members are transitional: an admin is already looking at
        # them, so the audit leaves them alone either way.

    return result


async def _is_in_group(bot, tg_id: int) -> bool:
    """
    Check whether a Telegram user is currently in the group chat.
    Telegram raises for users it has never seen in the chat — that
    means "not in group", not an error.
    """
    try:
        chat_member = await bot.get_chat_member(config.TELEGRAM_GROUP_CHAT_ID, tg_id)
        return chat_member.status in IN_GROUP_STATUSES
    except Exception:
        logger.debug("get_chat_member(%s): treating as not in group", tg_id)
        return False


async def remove_from_group(bot, tg_id: int) -> None:
    """
    Remove a user from the group without a permanent ban:
    ban then immediately unban, so they can rejoin by invite later.
    """
    await bot.ban_chat_member(config.TELEGRAM_GROUP_CHAT_ID, tg_id)
    await bot.unban_chat_member(
        config.TELEGRAM_GROUP_CHAT_ID, tg_id, only_if_banned=True
    )


async def run_membership_audit(bot) -> dict:
    """
    Full audit: classify everyone, remove Inactive members from the group,
    and return a report dict (see format_audit_report).
    """
    if not config.TELEGRAM_GROUP_CHAT_ID:
        return {"disabled": True}

    members = list(at.get_all_members_indexed().values())

    cutoff = now() - timedelta(weeks=config.STALE_SHIFT_WEEKS)
    recent_shifts = at.get_shifts_since(cutoff.isoformat())
    recent_member_ids = {
        member_id
        for shift in recent_shifts
        for member_id in shift["fields"].get("Member", [])
    }

    in_group_tg_ids = set()
    for member in members:
        tg_id = member["fields"].get("Telegram user ID")
        if tg_id and await _is_in_group(bot, tg_id):
            in_group_tg_ids.add(tg_id)

    report = classify_members(members, in_group_tg_ids, recent_member_ids)
    report["disabled"] = False
    report["removed"] = []
    report["remove_failed"] = []

    for info in report.pop("to_remove"):
        try:
            await remove_from_group(bot, info["telegram_id"])
            report["removed"].append(info)
            logger.info("Removed inactive member %s from group", info["name"])
        except Exception:
            logger.exception("Failed to remove %s from group", info["name"])
            report["remove_failed"].append(info)

    return report


def format_audit_report(report: dict) -> str:
    """Render the audit result as a short admin-facing message."""
    if report.get("disabled"):
        return "👥 Membership audit skipped: TELEGRAM_GROUP_CHAT_ID not set."

    def names(key):
        return ", ".join(sorted(i["name"] for i in report[key]))

    lines = ["👥 Group membership audit:"]
    if report["removed"]:
        lines.append(f"Removed (inactive): {names('removed')}")
    if report["remove_failed"]:
        lines.append(
            f"⚠️ Couldn't remove: {names('remove_failed')} — check the bot "
            f"is a group admin with ban rights."
        )
    if report["missing"]:
        lines.append(f"⚠️ Active but NOT in group: {names('missing')} — invite them.")
    if report["inactive_admins"]:
        lines.append(f"Inactive admins still in group: {names('inactive_admins')}")
    if report["stale"]:
        lines.append(
            f"💤 No shifts in {config.STALE_SHIFT_WEEKS} weeks: {names('stale')} "
            f"— still on the team? Flip to Inactive if not."
        )
    if report["no_telegram"]:
        lines.append(f"Unverifiable (no Telegram ID): {names('no_telegram')}")

    if len(lines) == 1:
        lines.append("All clear — group matches the roster. ✅")

    return "\n".join(lines)
