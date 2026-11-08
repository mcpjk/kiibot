"""
Group-chat membership audit.

Invariant: everyone with Status = Active belongs in the group chat;
nobody with Status = Inactive does. Employment type and job role don't
change that — they only shape which cycles/features a member is in.

The Telegram Bot API cannot list a group's members, so the audit is
Airtable-driven: for every Team Members row with a Telegram ID we ask
get_chat_member() whether that user is currently in the group, then:

- Inactive + in group  → removed by the bot (ban + immediate unban, so
  they can be re-invited later). The human decision stays in Airtable —
  flipping Status to Inactive is what triggers removal at the next audit.
- Active + not in group → the bot DMs them a single-use invite link
  (needs the bot's Invite users right) and tells admins it did; if
  that fails, admins are asked to invite them as before.
- Active Part-time member with no shift in STALE_SHIFT_WEEKS → flagged
  for review only. Part-timers who opted out of weekly availability are
  not flagged: they've said they're stepping back, and the monthly
  check-in (core/involvement.py) asks them instead. Never
  auto-flipped: Status gates pay/access, so a human decides. Members whose record is younger than the window are
  skipped: "no shifts in 5 weeks" is meaningless for someone who joined
  last week (record createdTime ≈ their /start, since onboarding is
  self-service).
- Inactive admins in the group are reported, never auto-removed.

Runs from /confirmweek so the check happens while attention is already
on the week's organisation. Stateless: derives everything fresh from
Airtable + Telegram each run, so re-running is harmless (removals are
naturally idempotent — a removed member is no longer in the group).
"""

import logging
from datetime import timedelta

from telegram.error import BadRequest

import config
from core import airtable_client as at
from core.timeutils import now, parse_dt

logger = logging.getLogger(__name__)

# get_chat_member statuses that count as "currently in the group".
IN_GROUP_STATUSES = {"creator", "administrator", "member", "restricted"}


def in_group(chat_member) -> bool:
    """Whether a ChatMember is in the group. A restricted user who LEAVES
    keeps status 'restricted' with is_member False, so status alone
    would count them as still here (and miss the leave event)."""
    if chat_member.status == "restricted":
        return bool(getattr(chat_member, "is_member", True))
    return chat_member.status in IN_GROUP_STATUSES


def classify_members(
    members: list[dict],
    in_group_tg_ids: set[int],
    recent_shift_member_ids: set[str],
    stale_cutoff=None,
) -> dict:
    """
    Pure classification (no I/O) — the testable heart of the audit.

    members: Team Members records; in_group_tg_ids: Telegram IDs currently
    in the group; recent_shift_member_ids: Airtable record IDs of members
    with a shift in the staleness window; stale_cutoff: start of that
    window — members created after it haven't been around long enough
    to be stale. None (or a record without createdTime) flags as before.
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

            if (f.get("Employment type") == "Part-time"
                    and f.get("Weekly availability")
                    and member["id"] not in recent_shift_member_ids
                    and not _joined_after(member, stale_cutoff)):
                result["stale"].append(info)

        elif status == "Inactive" and tg_id and tg_id in in_group_tg_ids:
            if f.get("Admin"):
                result["inactive_admins"].append(info)
            else:
                result["to_remove"].append(info)

        # Pending members are transitional: an admin is already looking at
        # them, so the audit leaves them alone either way.

    return result


def _joined_after(member: dict, cutoff) -> bool:
    """Whether the member's record was created after `cutoff`. Uses the
    record-level createdTime the REST API returns on every record, so no
    schema field is needed. Unknown → False (keep flagging)."""
    if cutoff is None:
        return False
    created = parse_dt(member.get("createdTime"))
    return created is not None and created > cutoff


async def _is_in_group(bot, tg_id: int) -> bool:
    """
    Check whether a Telegram user is currently in the group chat.
    Telegram answers BadRequest for users it has never seen in the chat —
    that means "not in group", not an error. Anything else (network,
    flood control) is raised: guessing "not in group" there would tell
    the admins to re-invite the whole team.
    """
    try:
        chat_member = await bot.get_chat_member(config.TELEGRAM_GROUP_CHAT_ID, tg_id)
    except BadRequest:
        logger.debug("get_chat_member(%s): treating as not in group", tg_id)
        return False
    return in_group(chat_member)


async def kick_from_group(bot, tg_id: int) -> None:
    """
    Remove a current member in one call: unbanChatMember WITHOUT
    only_if_banned "guarantees that after the call the user is not a
    member of the chat, but will be able to join it" (Bot API) — no ban
    is left behind. Harmless if they've already gone.
    """
    await bot.unban_chat_member(config.TELEGRAM_GROUP_CHAT_ID, tg_id)


async def create_invite_link(bot, name: str) -> str:
    """
    A single-use group invite link (member_limit=1) that expires after
    INVITE_LINK_EXPIRE_DAYS, so a forwarded link can't let anyone else
    in. Needs the bot's Invite users admin right.
    """
    link = await bot.create_chat_invite_link(
        config.TELEGRAM_GROUP_CHAT_ID,
        name=name[:32],
        member_limit=1,
        expire_date=now() + timedelta(days=config.INVITE_LINK_EXPIRE_DAYS),
    )
    return link.invite_link


async def send_invite(bot, tg_id: int, name: str, intro: str) -> bool:
    """DM a member a fresh invite link. False if either step failed."""
    try:
        link = await create_invite_link(bot, name)
        await bot.send_message(chat_id=tg_id, text=f"{intro}\n{link}")
        return True
    except Exception:
        logger.exception("Couldn't send a group invite to %s", name)
        return False


# Admin rights the membership features rely on, with what breaks without.
BOT_RIGHTS = {
    "can_restrict_members": "Ban users (removing leavers)",
    "can_invite_users": "Invite users (invite links)",
    "can_delete_messages": "Delete messages (hiding 'removed' notices)",
}


async def missing_bot_rights(bot) -> list[str]:
    """Which of BOT_RIGHTS the bot lacks in the group (all, if it isn't
    an admin there). Empty when everything is in place."""
    me = await bot.get_chat_member(config.TELEGRAM_GROUP_CHAT_ID, bot.id)
    if me.status == "creator":
        return []
    if me.status != "administrator":
        return list(BOT_RIGHTS.values())
    return [label for attr, label in BOT_RIGHTS.items()
            if not getattr(me, attr, False)]


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

    # Fail loudly if the group itself is unreachable (wrong
    # TELEGRAM_GROUP_CHAT_ID, bot removed): every per-user lookup would
    # then fail as BadRequest and read as "nobody is in the group".
    await bot.get_chat(config.TELEGRAM_GROUP_CHAT_ID)

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

    report = classify_members(members, in_group_tg_ids, recent_member_ids,
                              stale_cutoff=cutoff)
    report["disabled"] = False
    report["removed"] = []
    report["remove_failed"] = []

    try:
        report["missing_rights"] = await missing_bot_rights(bot)
    except Exception:
        logger.exception("Couldn't read the bot's own group rights")
        report["missing_rights"] = []

    # Active but not in the group: send them the way in directly rather
    # than asking an admin to (this is also how a newly activated member
    # first hears they're set up).
    report["invited"] = []
    still_missing = []
    for info in report["missing"]:
        sent = await send_invite(
            bot, info["telegram_id"], info["name"],
            "👥 You're set up on Kii — here's your link to the team group "
            f"chat (single use, valid {config.INVITE_LINK_EXPIRE_DAYS} days):")
        (report["invited"] if sent else still_missing).append(info)
    report["missing"] = still_missing

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
    if report.get("invited"):
        lines.append(f"Invite link sent (Active, not in group): {names('invited')}")
    if report["missing"]:
        lines.append(f"⚠️ Active but NOT in group: {names('missing')} — invite them.")
    if report["inactive_admins"]:
        lines.append(f"Inactive admins still in group: {names('inactive_admins')}")
    if report["stale"]:
        lines.append(
            f"💤 No shifts in {config.STALE_SHIFT_WEEKS} weeks: {names('stale')} "
            f"— still around? They can pause weekly availability or leave "
            f"via /me; Inactive in Airtable removes them from the group."
        )
    if report.get("missing_rights"):
        lines.append(
            "⚠️ The bot is missing group admin rights: "
            f"{'; '.join(report['missing_rights'])}.")
    if report["no_telegram"]:
        lines.append(f"Unverifiable (no Telegram ID): {names('no_telegram')}")

    if len(lines) == 1:
        lines.append("All clear — group matches the roster. ✅")

    return "\n".join(lines)
