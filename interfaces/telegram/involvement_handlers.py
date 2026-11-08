"""
Telegram side of member self-service (core/involvement.py): /me, the
⏸ button on the availability prompt, leaving and
rejoining, and hiding the group's "removed" notices.

Callback data: 'invl:<action>' — pause, resume, leave (asks),
leaveyes, leaveno, rejoin. Every action re-reads the member from
Airtable, so an old message's buttons can't do anything the member's
current state doesn't allow.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
from core.involvement import (
    InvolvementError,
    describe,
    format_admin_notice,
    leave_team,
    pause_availability,
    rejoin_team,
    resume_availability,
)
from core import airtable_client as at
from core.membership import kick_from_group
from interfaces.telegram.callback_utils import safe_answer
from interfaces.telegram.notify import notify_admins, post_notification

logger = logging.getLogger(__name__)

PAUSE_BUTTON = InlineKeyboardButton(
    "⏸ Stop these weekly messages", callback_data="invl:pause")


def _btn(text, action):
    return InlineKeyboardButton(text, callback_data=f"invl:{action}")


def me_text(d: dict) -> str:
    """The /me summary for a member's current state. Pure."""
    if d["status"] == "Pending":
        return (f"👋 {d['name']}, you're registered and waiting for an admin "
                f"to finish setting you up.")
    if d["status"] == "Inactive":
        if d["can_rejoin"]:
            return (f"👋 {d['name']}, you've left Kii. Coming back? Tap below "
                    f"and you'll be active again.")
        return f"👋 {d['name']}, your account isn't active. Ask an admin."
    if not d["part_time"]:
        return (f"👋 {d['name']}, you're Active. Your schedule is set by an "
                f"admin, so there's nothing to change here.")
    if d["weekly"]:
        cycle = ("📅 You get the weekly availability messages "
                 "(Wed, with reminders Thu and Fri).")
    else:
        cycle = ("⏸ Weekly availability messages are off. You're "
                 "still in the group and can still /clockin.")
    return f"👋 {d['name']}, you're Active.\n{cycle}"


def me_keyboard(d: dict):
    rows = []
    if d["can_pause"]:
        rows.append([_btn("⏸ Stop weekly availability messages", "pause")])
    if d["can_resume"]:
        rows.append([_btn("▶️ Turn weekly availability back on", "resume")])
    if d["can_leave"]:
        rows.append([_btn("👋 Leave Kii and the group chat", "leave")])
    if d["can_rejoin"]:
        rows.append([_btn("🔁 I'm back", "rejoin")])
    return InlineKeyboardMarkup(rows) if rows else None


async def me_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/me — your status and what you can change."""
    if update.effective_chat.type != "private":
        await update.message.reply_text("Send /me to me in a private chat.")
        return
    member = at.get_member_by_telegram_id(update.effective_user.id)
    if not member:
        await update.message.reply_text("You're not registered yet — send /start.")
        return
    d = describe(member)
    await update.message.reply_text(me_text(d), reply_markup=me_keyboard(d))


async def involvement_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.split(":", 1)[1]
    tg_id = query.from_user.id
    await safe_answer(query)

    if action == "leave":
        await query.edit_message_text(
            "👋 Leave Kii? You'll be set inactive and removed from the team "
            "group chat (no announcement). You can come back any time via "
            "/start.",
            reply_markup=InlineKeyboardMarkup([[
                _btn("Yes, leave", "leaveyes"), _btn("Cancel", "leaveno")]]))
        return
    if action == "leaveno":
        await query.edit_message_text("👍 Nothing changed. /me shows your options.")
        return

    try:
        if action == "pause":
            r = pause_availability(tg_id)
            text = ("⏸ Done — no more weekly availability messages. You're "
                    "still in the group and can still /clockin. Turn them "
                    "back on any time with /me.")
            notice = "pause" if r["changed"] else None
        elif action == "resume":
            r = resume_availability(tg_id)
            text = ("▶️ You're back in the weekly availability cycle — the "
                    "next prompt comes Wednesday 22:00. To answer for next "
                    "week right now, use /availability.")
            notice = "resume" if r["changed"] else None
        elif action == "leaveyes":
            await _leave(query, context)
            return
        elif action == "rejoin":
            await _rejoin(query, context)
            return
        else:
            return
    except InvolvementError as e:
        await query.edit_message_text(f"⚠️ {e}")
        return

    await query.edit_message_text(text)
    if notice:
        await post_notification(context.bot, format_admin_notice(notice, r["name"]),
                            exclude_telegram_id=tg_id)


async def _leave(query, context):
    tg_id = query.from_user.id
    try:
        r = leave_team(tg_id)
    except InvolvementError as e:
        await query.edit_message_text(f"⚠️ {e}")
        return

    group_note = ""
    if config.TELEGRAM_GROUP_CHAT_ID:
        try:
            await kick_from_group(context.bot, tg_id)
            group_note = " and out of the group"
        except Exception:
            # Inactive-in-group is what the audit sweeps, so this heals
            # at the next /confirmweek.
            logger.exception("Couldn't remove %s from the group", r["name"])
            group_note = " (couldn't remove them from the group — the next audit will)"

    await query.edit_message_text(
        "👋 Done — you've left Kii. Thanks for everything! If you ever want "
        "back in, send /start.")
    if r["changed"]:
        await post_notification(
            context.bot, format_admin_notice("leave", r["name"], group_note),
            exclude_telegram_id=tg_id)


async def _rejoin(query, context):
    tg_id = query.from_user.id
    try:
        r = rejoin_team(tg_id)
    except InvolvementError as e:
        await query.edit_message_text(f"⚠️ {e}")
        return

    await query.edit_message_text(
        "🔁 Welcome back! You're active again and back in the weekly "
        "availability cycle (next prompt Wednesday 22:00). An admin will "
        "add you back to the team group chat.")
    if r["changed"]:
        # An action, not an FYI: group membership stays manual (Marcus,
        # 2026-11-08), so someone has to add them back.
        await notify_admins(
            context.bot, format_admin_notice("rejoin", r["name"]),
            exclude_telegram_id=tg_id)


async def hide_removal_notice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Delete the "Kii-bot removed X" service message the group gets when
    the bot removes someone (self-serve leave or audit). Telegram has no
    way to remove a member without posting it; deleting it right after
    is the closest thing to a silent exit. Needs the bot's Delete
    messages right. A member who leaves on their own keeps their notice
    — we only tidy up after ourselves.
    """
    msg = update.effective_message
    if (not msg or not config.TELEGRAM_GROUP_CHAT_ID
            or msg.chat_id != config.TELEGRAM_GROUP_CHAT_ID
            or not msg.from_user or msg.from_user.id != context.bot.id):
        return
    try:
        await msg.delete()
    except Exception:
        logger.warning("Couldn't delete the group's removal notice "
                       "(bot lacks Delete messages?)", exc_info=True)
