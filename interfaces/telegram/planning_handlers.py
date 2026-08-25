"""
Telegram handlers for the daily planning Mini App.

Thin: the packing rules and Airtable writes live in core/planning.py.

The button MUST be an INLINE one. Telegram's two Mini App launch
styles are mutually exclusive in exactly the way that matters here:

  reply-keyboard button → Telegram.WebApp.sendData() works,
                          but initData is EMPTY
  inline button         → initData is fully populated and signed,
                          but sendData() is unavailable

The page has to prove who is asking before it can be shown the
project list, so signed initData wins and submissions go back through
an authenticated POST instead of sendData. Building it the other way
round (reply keyboard + sendData) was tried first and cannot work: the
launch carries no identity at all (confirmed live 2026-08-25 —
version and platform arrived, tgWebAppData did not).
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


def plan_keyboard() -> InlineKeyboardMarkup:
    """The 'Plan today' Web App button (inline — see the module note)."""
    url = f"{config.WEBAPP_URL.rstrip('/')}/plan"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 Plan today", web_app=WebAppInfo(url=url))]]
    )


async def plan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/plan — open the morning selection Mini App."""
    if not planning_configured():
        await update.message.reply_text(
            "⚠️ Planning isn't configured on this deploy (WEBAPP_URL not set)."
        )
        return

    await update.message.reply_text(
        "Tap below to pick today's projects.",
        reply_markup=plan_keyboard(),
    )
