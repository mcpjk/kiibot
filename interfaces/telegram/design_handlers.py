"""
Telegram handlers for design-block scheduling (see DESIGN_SCHEDULING.md).

Thin translation only: the extension rules and cascade live in
core/design.py.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from core.design import (
    EXTEND_MINUTES,
    DesignError,
    extend_current_block,
    format_extension,
)

logger = logging.getLogger(__name__)

# Attached to every switch reminder. A button rather than a line of
# text on purpose: the ping's whole job is to fit a lock-screen
# notification preview, and buttons don't consume it. /extend still
# works typed, at any time.
EXTEND_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton(f"⏱ +{EXTEND_MINUTES} min on current task",
                           callback_data=f"extend:{EXTEND_MINUTES}")]]
)


async def extend_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/extend [minutes] — add time to the design block you're in."""
    minutes = EXTEND_MINUTES
    if context.args:
        try:
            minutes = int(context.args[0])
        except ValueError:
            await update.message.reply_text(
                f"Usage: /extend [minutes] — e.g. /extend 60. "
                f"Defaults to {EXTEND_MINUTES}."
            )
            return

    try:
        plan = extend_current_block(update.effective_user.id, minutes)
    except DesignError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return

    await update.message.reply_text(format_extension(plan))


async def extend_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The '+30 min' button on a switch reminder."""
    query = update.callback_query
    try:
        minutes = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        minutes = EXTEND_MINUTES

    try:
        plan = extend_current_block(query.from_user.id, minutes)
    except DesignError as e:
        # Invariant 4: Telegram honours only the FIRST answer to a
        # callback query, so an alert must be the only one on its path.
        await query.answer(str(e), show_alert=True)
        return

    await query.answer(f"+{minutes} min")
    await query.message.reply_text(format_extension(plan))
