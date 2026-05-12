"""
Telegram handlers for shift commands.

Each handler translates between Telegram's message/callback format
and the core business logic in core/shifts.py.
"""

from telegram import Update
from telegram.ext import ContextTypes

from core.shifts import clock_in, clock_out, confirm_shift, get_recent_shifts, get_current_month_summary, get_rate, ShiftError


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
        start = result["start_time"].strftime("%H:%M")
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
        result = confirm_shift(telegram_id)
        msg = (
            f"✅ Shift confirmed. You're still clocked in.\n"
            f"Use /clockout when you're done."
        )
    except ShiftError as e:
        msg = f"⚠️ {e}"

    await update.message.reply_text(msg)


async def myshifts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /myshifts — show recent shifts and current month summary."""
    telegram_id = update.effective_user.id

    try:
        # Recent shifts
        recent = get_recent_shifts(telegram_id, limit=7)
        # Monthly summary
        summary = get_current_month_summary(telegram_id)

        lines = [f"📋 Recent shifts for {recent['member_name']}:\n"]

        for s in recent["shifts"]:
            start_str = _format_dt(s["start"]) if s["start"] else "?"
            end_str = _format_dt(s["end"]) if s["end"] else "ongoing"
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

def _format_dt(iso_str: str) -> str:
    """Format ISO datetime for display: '25 Apr 09:30'."""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d %b %H:%M")
    except (ValueError, TypeError):
        return str(iso_str)


def _status_icon(status: str) -> str:
    icons = {
        "Open": "🟢",
        "Closed": "✅",
        "Auto-closed": "🔶",
        "Edit-approved": "✏️",
        "Locked": "🔒",
    }
    return icons.get(status, "•")
