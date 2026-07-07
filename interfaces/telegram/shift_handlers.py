"""
Telegram handlers for shift commands.

Each handler translates between Telegram's message/callback format
and the core business logic in core/shifts.py.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from core import airtable_client as at
from core.shifts import (
    clock_in,
    clock_out,
    confirm_shift,
    get_recent_shifts,
    get_current_month_summary,
    get_rate,
    ShiftError,
)
from core.timeutils import fmt_dt

logger = logging.getLogger(__name__)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /start — onboarding.
    Registered members get a command overview; unregistered users get
    their Telegram ID to pass to an admin (admins otherwise have to dig
    for the numeric ID manually).
    """
    user = update.effective_user
    member = at.get_member_by_telegram_id(user.id)

    if member:
        name = member["fields"].get("Name", user.first_name)
        msg = (
            f"👋 Hi {name}! You're registered.\n\n"
            f"Commands:\n"
            f"/clockin — start a shift\n"
            f"/clockout — end your shift\n"
            f"/myshifts — recent shifts & month total\n"
            f"/myrate — your hourly rate\n"
            f"/editshift — request a shift correction"
        )
    else:
        msg = (
            f"👋 Hi {user.first_name}! You're not registered yet.\n\n"
            f"Send this to your admin so they can add you:\n"
            f"Telegram user ID: {user.id}\n"
            f"Username: @{user.username or '—'}"
        )

    await update.message.reply_text(msg)


async def clockin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /clockin command."""
    telegram_id = update.effective_user.id

    try:
        result = clock_in(telegram_id)
        start = result["start_time"].strftime("%H:%M")
        rate = result["rate"]
        msg = (
            f"✅ Clocked in at {start}\n"
            f"Rate: ${rate:.2f}/hr\n"
            f"Use /clockout when you're done."
        )
    except ShiftError as e:
        msg = f"⚠️ {e}"

    await update.message.reply_text(msg)


async def clockout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /clockout command."""
    telegram_id = update.effective_user.id

    try:
        result = clock_out(telegram_id)
        start = result["start_time"].strftime("%H:%M") if result["start_time"] else "?"
        end = result["end_time"].strftime("%H:%M")
        msg = (
            f"✅ Clocked out at {end}\n"
            f"Shift: {start} → {end}\n"
            f"Duration: {result['duration_hours']:.2f} hrs\n"
            f"Rate: ${result['rate']:.2f}/hr\n"
            f"Gross: ${result['gross_pay']:.2f}"
        )
    except ShiftError as e:
        msg = f"⚠️ {e}"

    await update.message.reply_text(msg)


async def confirmshift_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /confirmshift — response to end-of-day 'still working?' prompt."""
    telegram_id = update.effective_user.id

    try:
        confirm_shift(telegram_id)
        msg = (
            f"✅ Shift confirmed. You're still clocked in and won't be "
            f"auto-closed tonight.\n"
            f"Use /clockout when you're done."
        )
    except ShiftError as e:
        msg = f"⚠️ {e}"

    await update.message.reply_text(msg)


async def myshifts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /myshifts — show recent shifts and current month summary."""
    telegram_id = update.effective_user.id

    try:
        recent = get_recent_shifts(telegram_id, limit=7)
        summary = get_current_month_summary(telegram_id)

        lines = [f"📋 Recent shifts for {recent['member_name']}:\n"]

        for s in recent["shifts"]:
            start_str = fmt_dt(s["start"]) if s["start"] else "?"
            end_str = fmt_dt(s["end"]) if s["end"] else "ongoing"
            duration = f"{s['duration']:.2f}h" if s["duration"] else "—"
            gross = f"${s['gross']:.2f}" if s["gross"] else "—"
            status_icon = _status_icon(s["status"])

            lines.append(f"{status_icon} {start_str} → {end_str}  {duration}  {gross}")

        lines.append("")
        lines.append(
            f"📊 {summary['pay_month']} total: "
            f"{summary['total_hours']:.1f} hrs, ${summary['total_gross']:.2f} "
            f"({summary['shift_count']} shifts)"
        )

        msg = "\n".join(lines)

    except ShiftError as e:
        msg = f"⚠️ {e}"

    await update.message.reply_text(msg)


async def myrate_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /myrate — show current hourly rate."""
    telegram_id = update.effective_user.id

    try:
        result = get_rate(telegram_id)
        rate = result["rate"]
        if rate is not None:
            msg = f"Your current rate: ${rate:.2f}/hr"
        else:
            msg = "⚠️ No rate set. Contact an admin."
    except ShiftError as e:
        msg = f"⚠️ {e}"

    await update.message.reply_text(msg)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _status_icon(status: str) -> str:
    icons = {
        "Open": "🟢",
        "Closed": "✅",
        "Auto-closed": "🔶",
        "Edit-approved": "✏️",
        "Locked": "🔒",
    }
    return icons.get(status, "•")
