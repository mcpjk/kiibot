"""
Telegram handlers for the day editor (DESIGN_SCHEDULING.md §13).

Thin: every rule lives in core/day.py, every write in web/server.py's
save route. This module only builds the button that opens the page.

Like the planner, the button MUST be an INLINE Web App button — a
reply-keyboard launch carries no initData at all, and the page can't be
shown a day it can't attribute to anyone (see planning_handlers.py for
the full account of that dead end).
"""

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import ContextTypes

import config
from core.planning import planning_configured

logger = logging.getLogger(__name__)


def day_button() -> InlineKeyboardButton:
    """The 'Edit day' Web App button, for keyboards built elsewhere."""
    url = f"{config.WEBAPP_URL.rstrip('/')}/day"
    return InlineKeyboardButton("✏️ Edit day", web_app=WebAppInfo(url=url))


def day_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[day_button()]])


async def day_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/day — open today's blocks for editing."""
    if not planning_configured():
        await update.message.reply_text(
            "⚠️ The day editor isn't configured on this deploy "
            "(WEBAPP_URL not set)."
        )
        return

    # Web App buttons are private-chat only; sending one to a group
    # fails the whole message rather than just dropping the button.
    if update.effective_chat.type != "private":
        await update.message.reply_text("Message me directly to open the day "
                                        "editor — /day only works in a DM.")
        return

    await update.message.reply_text(
        "Tap below to fix up today's blocks.",
        reply_markup=day_keyboard(),
    )
