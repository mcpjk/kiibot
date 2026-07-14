"""
Telegram handlers for shift commands.

Each handler translates between Telegram's message/callback format
and the core business logic in core/shifts.py.
"""

import logging

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
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

# Persistent reply keyboard shown to registered members.
# Button text IS the command: Telegram parses the tapped text as a
# bot command, so the existing CommandHandlers fire unchanged — no
# separate MessageHandler/callback path to maintain.
SHIFT_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("/clockin"), KeyboardButton("/clockout")]],
    resize_keyboard=True,   # shrink buttons to fit content
    is_persistent=True,     # stays visible instead of collapsing after use
)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /start — onboarding.

    Active members get a command overview. Unregistered users are
    auto-registered as a 'Pending' Team Members record (capturing their
    Telegram ID + username) and told to wait for admin setup; admins are
    DM'd so they can set a rate/role and activate the account. Pending users
    who /start again just get the waiting message. Pending members can't
    clock in until an admin flips them to Active (enforced in clock_in).
    """
    user = update.effective_user
    member = at.get_member_by_telegram_id(user.id)

    if member:
        name = member["fields"].get("Name", user.first_name)
        if member["fields"].get("Status") == "Pending":
            msg = (
                f"👋 Hi {name}! You're registered and waiting for an admin to "
                f"finish setting up your account (rate & access). You'll be "
                f"able to /clockin once that's done."
            )
        else:
            msg = (
                f"👋 Hi {name}! You're registered.\n\n"
                f"Commands:\n"
                f"/clockin — start a shift\n"
                f"/clockout — end your shift\n"
                f"/myshifts — recent shifts & month total\n"
                f"/myrate — your hourly rate\n"
                f"/editshift — request a shift correction"
            )
            # Active members get the persistent clock in/out keyboard;
            # Pending members don't (they can't clock in yet).
            await update.message.reply_text(msg, reply_markup=SHIFT_KEYBOARD)
            return
        await update.message.reply_text(msg)
        return

    # Unregistered → auto-register as Pending and alert admins.
    display_name = user.full_name or user.first_name
    try:
        at.create_team_member(
            name=display_name,
            telegram_id=user.id,
            username=user.username,
        )
    except Exception:
        logger.exception("Failed to auto-register user %s (@%s)",
                         user.id, user.username)
        await update.message.reply_text(
            f"👋 Hi {user.first_name}! I couldn't register you automatically. "
            f"Please send this to your admin:\n"
            f"Telegram user ID: {user.id}\n"
            f"Username: @{user.username or '—'}"
        )
        return

    await update.message.reply_text(
        f"👋 Hi {user.first_name}! You're now registered. An admin will set "
        f"your rate and activate your account shortly — you'll be able to "
        f"/clockin after that."
    )
    await _notify_admins_new_member(context, display_name, user)


async def _notify_admins_new_member(context, display_name, user):
    """DM every admin that a new user self-registered and needs setup."""
    text = (
        f"🆕 New sign-up pending setup:\n"
        f"Name: {display_name}\n"
        f"Username: @{user.username or '—'}\n"
        f"Telegram ID: {user.id}\n\n"
        f"Set their rate & Role and flip Status to Active in Airtable."
    )
    for admin in at.get_admin_members():
        tg_id = admin["fields"].get("Telegram user ID")
        if not tg_id:
            continue
        try:
            await context.bot.send_message(chat_id=tg_id, text=text)
        except Exception:
            logger.exception("Failed to notify admin %s of new sign-up",
                             admin["fields"].get("Name"))


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
        # Duration is already net of lunch (Airtable formula); the marker
        # just signals the deduction without dwelling on the number.
        lunch_note = " (− lunch)" if result.get("lunch_hours") else ""
        msg = (
            f"✅ Clocked out at {end}\n"
            f"Shift: {start} → {end}\n"
            f"Duration: {result['duration_hours']:.2f} hrs{lunch_note}\n"
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
