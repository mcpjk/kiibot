"""
Telegram side of member self-service (core/involvement.py): /me, the
⏸ button on the availability prompt, leaving and
rejoining, and hiding the group's "removed" notices.

Callback data: 'invl:<action>' — pause, resume, leave (asks),
leaveyes, leaveno, rejoin. Every action re-reads the member from
Airtable, so an old message's buttons can't do anything the member's
current state doesn't allow.

A paused part-timer's persistent reply keyboard swaps /clockin
/clockout for "leave the group chat" and "restart weekly availability"
(Marcus, 2026-11-08); /clockin still works typed. Reply-keyboard taps
arrive as plain text, hence `paused_keyboard_handler`. Telegram sets a
reply keyboard only by SENDING a message (an edit can't), and a message
can't carry both an inline and a reply keyboard — so a state change
from an inline button sends one extra short message to swap them.
"""

import logging

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      KeyboardButton, ReplyKeyboardMarkup,
                      Update)
from telegram.ext import ContextTypes

import config
from core.involvement import (
    InvolvementError,
    describe,
    format_admin_notice,
    leave_group,
    pause_availability,
    rejoin_team,
    resume_availability,
)
from core import airtable_client as at
from core.membership import _is_in_group, kick_from_group
from interfaces.telegram.callback_utils import safe_answer
from interfaces.telegram.notify import notify_admins, post_notification
# shift_handlers imports this module only inside start_handler, so no cycle.
from interfaces.telegram.shift_handlers import SHIFT_KEYBOARD

logger = logging.getLogger(__name__)

PAUSE_BUTTON = InlineKeyboardButton(
    "⏸ Stop these weekly messages", callback_data="invl:pause")

LEAVE_LABEL = "👋 Leave Kii group chat silently"
RESUME_LABEL = "▶️ Restart weekly availability"

# Replaces SHIFT_KEYBOARD while a part-timer is paused. Button text is
# what the member sends, matched exactly by paused_keyboard_handler.
PAUSED_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(LEAVE_LABEL)], [KeyboardButton(RESUME_LABEL)]],
    resize_keyboard=True,
    is_persistent=True,
)

LEAVE_CONFIRM_TEXT = (
    "👋 Leave the team group chat? You'll be removed quietly (no "
    "announcement). You stay part of Kii: weekly availability messages "
    "stay off and you can still /clockin. To come back, tap ▶️ Restart "
    "weekly availability and an admin will add you back.")


def _btn(text, action):
    return InlineKeyboardButton(text, callback_data=f"invl:{action}")


def me_text(d: dict) -> str:
    """The /me summary for a member's current state. Pure."""
    if d["status"] == "Pending":
        return (f"👋 {d['name']}, you're registered and waiting for an admin "
                f"to finish setting you up.")
    if d["status"] == "Inactive":
        if d["can_rejoin"]:
            return (f"👋 {d['name']}, your account is inactive. Coming back? "
                    f"Tap below and you'll be active again.")
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
        rows.append([_btn(LEAVE_LABEL, "leave")])
    if d["can_rejoin"]:
        rows.append([_btn("🔁 I'm back", "rejoin")])
    return InlineKeyboardMarkup(rows) if rows else None


def leave_confirm_keyboard():
    return InlineKeyboardMarkup([[
        _btn("Yes, leave", "leaveyes"), _btn("Cancel", "leaveno")]])


def reply_keyboard_for(d: dict):
    """The persistent bottom keyboard for an Active member: leave /
    restart while a part-timer is paused, clock in/out otherwise. Pure."""
    if d["part_time"] and not d["weekly"]:
        return PAUSED_KEYBOARD
    return SHIFT_KEYBOARD


async def _swap_keyboard(context, chat_id, text, keyboard):
    """Send `text` carrying a new reply keyboard. Best-effort: the state
    change already happened; /start re-sends the right keyboard."""
    try:
        await context.bot.send_message(chat_id, text, reply_markup=keyboard)
    except Exception:
        logger.warning("Couldn't swap the reply keyboard", exc_info=True)


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
            LEAVE_CONFIRM_TEXT, reply_markup=leave_confirm_keyboard())
        return
    if action == "leaveno":
        await query.edit_message_text("👍 Nothing changed. /me shows your options.")
        return

    try:
        if action == "pause":
            r = pause_availability(tg_id)
            text = PAUSED_TEXT
            markup = InlineKeyboardMarkup([[_btn(LEAVE_LABEL, "leave")]])
            follow = (PAUSED_KEYBOARD_TEXT, PAUSED_KEYBOARD)
            notice = "pause" if r["changed"] else None
        elif action == "resume":
            r = resume_availability(tg_id)
            text = RESUMED_TEXT
            markup = None
            follow = ("🕒 Clock in/out buttons are back below.",
                      SHIFT_KEYBOARD)
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

    await query.edit_message_text(text, reply_markup=markup)
    await _swap_keyboard(context, query.message.chat_id, *follow)
    if notice:
        await post_notification(context.bot, format_admin_notice(notice, r["name"]),
                            exclude_telegram_id=tg_id)


PAUSED_TEXT = (
    "⏸ Done — no more weekly availability messages. You're still in the "
    "group chat and can still /clockin. Turn them back on any time with "
    "the button below or /me.\n\nWant to leave the group chat too?")
PAUSED_KEYBOARD_TEXT = (
    "Your buttons below are now leave / restart. Type /clockin and "
    "/clockout as usual when you're working.")
RESUMED_TEXT = (
    "▶️ You're back in the weekly availability cycle — the next prompt "
    "comes Wednesday 22:00. To answer for next week right now, use "
    "/availability.")


async def paused_keyboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The two PAUSED_KEYBOARD buttons (private chat, exact text)."""
    msg = update.message
    tg_id = update.effective_user.id
    if msg.text == LEAVE_LABEL:
        # Same Yes/Cancel confirmation as the inline path.
        await msg.reply_text(LEAVE_CONFIRM_TEXT,
                             reply_markup=leave_confirm_keyboard())
        return
    try:
        r = resume_availability(tg_id)
    except InvolvementError as e:
        await msg.reply_text(f"⚠️ {e}")
        return
    await msg.reply_text(
        RESUMED_TEXT, reply_markup=SHIFT_KEYBOARD)
    if r["changed"]:
        await post_notification(context.bot, format_admin_notice("resume", r["name"]),
                                exclude_telegram_id=tg_id)


async def _leave(query, context):
    """Pause (core), then remove from the group chat. Stays Active."""
    tg_id = query.from_user.id
    try:
        r = leave_group(tg_id)
    except InvolvementError as e:
        await query.edit_message_text(f"⚠️ {e}")
        return

    removed, note = False, ""
    if config.TELEGRAM_GROUP_CHAT_ID:
        try:
            if await _is_in_group(context.bot, tg_id):
                await kick_from_group(context.bot, tg_id)
                removed = True
        except Exception:
            # Unlike Inactive, a paused member outside the group is not
            # something the audit sweeps, so admins must hear about it.
            logger.exception("Couldn't remove %s from the group", r["name"])
            note = " (⚠️ couldn't remove them — please remove them by hand)"

    if removed or not note:
        text = ("👋 Done — you're out of the team group chat. Weekly "
                "availability messages are off and you can still /clockin. "
                "Tap ▶️ Restart weekly availability whenever you want back in.")
    else:
        text = ("⚠️ I couldn't remove you from the group chat — an admin "
                "will. Weekly availability messages are off.")
    await query.edit_message_text(text)
    # Leaving also pauses, so make sure the bottom keyboard is the paused one.
    if r["changed"]:
        await _swap_keyboard(context, query.message.chat_id,
                             PAUSED_KEYBOARD_TEXT, PAUSED_KEYBOARD)
    if note:   # someone has to act: a DM, not an FYI
        await notify_admins(
            context.bot, format_admin_notice("leave", r["name"], note),
            exclude_telegram_id=tg_id)
    elif removed or r["changed"]:
        await post_notification(
            context.bot, format_admin_notice("leave", r["name"]),
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
    await _swap_keyboard(context, query.message.chat_id,
                         "🕒 Clock in/out buttons are back below.",
                         SHIFT_KEYBOARD)
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
