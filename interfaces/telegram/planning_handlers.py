"""
Telegram handlers for the daily planning Mini App.

Thin: the packing rules and Airtable writes live in core/planning.py.

Note the button lives on a REPLY keyboard, not an inline one. That's
load-bearing: Telegram.WebApp.sendData() — which lets the Mini App
submit through ordinary long polling instead of a write API — is only
available to Web Apps opened from a reply-keyboard button.
"""

import json
import logging

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

import config
from core.planning import (
    PlanningError,
    format_plan,
    planning_configured,
    submit_plan,
)

logger = logging.getLogger(__name__)


def plan_keyboard() -> ReplyKeyboardMarkup:
    """The 'Plan today' Web App button."""
    url = f"{config.WEBAPP_URL.rstrip('/')}/plan"
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📋 Plan today", web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
        is_persistent=True,
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


async def plan_submission_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Receive a submitted plan from the Mini App.

    The payload arrives as a normal Telegram update, so the sender is
    already authenticated by Telegram — no signature check needed on
    this path (unlike the read API, which the browser calls directly).
    """
    raw = update.effective_message.web_app_data.data
    try:
        payload = json.loads(raw)
        capacity = float(payload["capacity_hours"])
        selections = payload["blocks"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("Unparseable plan submission: %.200s", raw)
        await update.message.reply_text(
            "⚠️ That plan didn't come through cleanly. Try again."
        )
        return

    try:
        plan = submit_plan(update.effective_user.id, capacity, selections)
    except PlanningError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    except Exception:
        logger.exception("Failed to write planned blocks")
        await update.message.reply_text(
            "⚠️ Couldn't save that plan — nothing may have been written. "
            "Check Airtable before retrying."
        )
        return

    await update.message.reply_text(format_plan(plan))
