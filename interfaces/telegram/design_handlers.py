"""
Telegram handlers for design-block scheduling (see DESIGN_SCHEDULING.md).

Thin translation only: the adjustment rules and cascades live in
core/design.py.

The three buttons are meant to be tapped repeatedly ('+15, +15, +15'),
so each tap EDITS the message it came from instead of replying: the
keyboard stays under the designer's thumb, and the chat doesn't fill
with one confirmation per tap.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from core.planning import planning_configured
from interfaces.telegram.day_handlers import day_button

from core.design import (
    ACTIONS,
    ADJUST_MINUTES,
    DesignError,
    adjust_current_block,
    format_adjustment,
    note_switch_ping_sent,
)

logger = logging.getLogger(__name__)


def adjust_keyboard(minutes: int = ADJUST_MINUTES,
                    with_editor: bool = True) -> InlineKeyboardMarkup:
    """
    The buttons attached to every switch reminder — and to the reply of
    every adjustment, so a second tap needs no scrolling.

    Buttons rather than lines of text on purpose: the ping's whole job
    is to fit a lock-screen notification preview, and buttons don't
    consume it. The commands still work typed, at any time.

    'Edit day' rides along on the same keyboard because the reminder
    fires at exactly the moment worth catching — 5 min before a switch
    is when you know the plan was wrong (Marcus, 2026-09-02).

    It is left off in two cases, both of which would make Telegram
    reject the WHOLE message rather than just the button:
    `with_editor=False` for a chat that isn't private (Web App buttons
    are private-chat only), and no WEBAPP_URL. The pings and callbacks
    are always DMs; a typed /extend need not be.
    """
    rows = [
        [
            InlineKeyboardButton(f"⏱ +{minutes} min task",
                                 callback_data=f"extend:{minutes}"),
            InlineKeyboardButton(f"⏪ −{minutes} min task",
                                 callback_data=f"shrink:{minutes}"),
        ],
        [InlineKeyboardButton(f"☕ +{minutes} min space",
                              callback_data=f"space:{minutes}")],
    ]
    if with_editor and planning_configured():
        rows.append([day_button()])
    return InlineKeyboardMarkup(rows)


# Deliberately no module-level ADJUST_KEYBOARD constant any more: the
# keyboard's shape now depends on WEBAPP_URL, and a constant built at
# import time would freeze it before config could be read.


async def _fire_next_reminder(bot, chat_id: int, ping: dict):
    """
    Send the next block's switch reminder now.

    A shrink can pull the next block to within minutes of now, or into
    the past; the polling job only pings blocks whose Start is still in
    the future, so this is the only chance that reminder gets sent
    (core/design.py: plan_shrink).
    """
    try:
        await bot.send_message(chat_id=chat_id, text=ping["text"],
                               reply_markup=adjust_keyboard())
    except Exception:
        logger.exception("Could not fire the pulled-forward switch reminder")
        return
    try:
        note_switch_ping_sent(ping["block_id"])
    except Exception:
        logger.exception("Reminder sent but block %s not marked as pinged",
                         ping["block_id"])


async def _apply(update: Update, context: ContextTypes.DEFAULT_TYPE,
                 action: str, minutes: int):
    """Run one adjustment for a typed command and reply with the result."""
    try:
        plan = adjust_current_block(update.effective_user.id, action, minutes)
    except DesignError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return

    private = update.effective_chat.type == "private"
    await update.message.reply_text(
        format_adjustment(plan), reply_markup=adjust_keyboard(with_editor=private))
    if plan.get("ping_next"):
        await _fire_next_reminder(context.bot, update.effective_user.id,
                                  plan["ping_next"])


def _minutes_arg(args, default: int = ADJUST_MINUTES):
    """Parse '[minutes]' off a command. Returns None if it isn't a number."""
    if not args:
        return default
    try:
        return int(args[0])
    except ValueError:
        return None


async def extend_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/extend [minutes] — add time to the design block you're in.

    A negative argument takes time off it, so the typed command covers
    both directions the buttons do (/extend -15 == the −15 button)."""
    minutes = _minutes_arg(context.args)
    if minutes is None:
        await update.message.reply_text(
            f"Usage: /extend [minutes] — e.g. /extend 60, or /extend -15 to "
            f"end early. Defaults to {ADJUST_MINUTES}."
        )
        return

    action = "shrink" if minutes < 0 else "extend"
    await _apply(update, context, action, abs(minutes))


async def space_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/space [minutes] — open unallocated time for a break or
    non-project work, pushing the rest of the day back."""
    minutes = _minutes_arg(context.args)
    if minutes is None or minutes < 0:
        await update.message.reply_text(
            f"Usage: /space [minutes] — e.g. /space 30. "
            f"Defaults to {ADJUST_MINUTES}."
        )
        return

    await _apply(update, context, "space", minutes)


async def adjust_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The +15 / −15 / +15-space buttons on a switch reminder."""
    query = update.callback_query
    action, _, raw = query.data.partition(":")
    try:
        minutes = int(raw)
    except ValueError:
        minutes = ADJUST_MINUTES
    if action not in ACTIONS:
        action = "extend"

    try:
        plan = adjust_current_block(query.from_user.id, action, minutes)
    except DesignError as e:
        # Invariant 4: Telegram honours only the FIRST answer to a
        # callback query, so an alert must be the only one on its path.
        await query.answer(str(e), show_alert=True)
        return

    summary = format_adjustment(plan)
    await query.answer(summary.split("\n")[0][:200])

    # Edit in place, keyboard intact, so the next tap lands on the same
    # message. Every summary carries a time that just changed, so
    # 'message is not modified' should not happen — but a network hiccup
    # or a deleted message shouldn't lose the confirmation either.
    try:
        await query.edit_message_text(summary, reply_markup=adjust_keyboard())
    except BadRequest:
        logger.warning("Could not edit the adjustment message; replying instead")
        await query.message.reply_text(summary, reply_markup=adjust_keyboard())

    if plan.get("ping_next"):
        await _fire_next_reminder(context.bot, query.from_user.id,
                                  plan["ping_next"])
