"""
Telegram handlers for availability management.

Member flow:
- Receives inline keyboard with day buttons (Mon-Sat)
- Taps days they're available → toggles on/off
- Taps "Submit" to confirm

Admin flow:
- /confirmweek → bot sends confirmed-day notifications to all members
  AND posts a weekly schedule summary to the group chat
"""

from datetime import datetime, date
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, CommandHandler

from core.availability import (
    get_next_week_dates,
    submit_availability,
    notify_confirmed_shifts,
    AvailabilityError,
)
from core import airtable_client as at
import config

TZ = ZoneInfo(config.TIMEZONE)

# Day name abbreviations
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _build_day_keyboard(
    dates: list[date], selected: set[str], callback_prefix: str
) -> InlineKeyboardMarkup:
    """
    Build an inline keyboard with day buttons.
    Selected days show a checkmark. Each button toggles that day.
    """
    buttons = []
    row = []
    for d in dates:
        iso = d.isoformat()
        day_name = DAY_NAMES[d.weekday()]
        day_num = d.day
        label = f"{'✅ ' if iso in selected else ''}{day_name} {day_num}"
        row.append(
            InlineKeyboardButton(label, callback_data=f"{callback_prefix}:{iso}")
        )
        # 3 buttons per row
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Action buttons
    buttons.append([
        InlineKeyboardButton("📤 Submit", callback_data=f"{callback_prefix}:submit"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"{callback_prefix}:cancel"),
    ])

    return InlineKeyboardMarkup(buttons)


async def availability_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle inline button presses for availability selection.

    The callback_data format is 'avail:<ISO_DATE>' for toggles,
    'avail:submit' for submission, and 'avail:cancel' for cancellation.
    """
    query = update.callback_query
    await query.answer()

    data = query.data.replace("avail:", "")

    # Initialise tracking if needed
    if "avail_selected" not in context.user_data:
        context.user_data["avail_selected"] = set()
    if "avail_dates" not in context.user_data:
        context.user_data["avail_dates"] = []

    selected = context.user_data["avail_selected"]

    if data == "cancel":
        context.user_data.pop("avail_selected", None)
        context.user_data.pop("avail_dates", None)
        await query.edit_message_text("Availability submission cancelled.")
        return

    if data == "submit":
        if not selected:
            await query.answer("Select at least one day first.", show_alert=True)
            return

        telegram_id = query.from_user.id
        try:
            result = submit_availability(telegram_id, sorted(list(selected)))
            created = result["created"]
            skipped = result["skipped"]

            day_list = ", ".join(
                _format_date_short(d) for d in sorted(created)
            )
            msg = f"✅ Availability submitted for: {day_list}"
            if skipped:
                skip_list = ", ".join(
                    _format_date_short(d) for d in sorted(skipped)
                )
                msg += f"\n(Already submitted: {skip_list})"

        except AvailabilityError as e:
            msg = f"⚠️ {e}"

        context.user_data.pop("avail_selected", None)
        context.user_data.pop("avail_dates", None)
        await query.edit_message_text(msg)
        return

    # Toggle a day
    if data in selected:
        selected.discard(data)
    else:
        selected.add(data)

    # Rebuild the keyboard with updated selection
    dates = [date.fromisoformat(d) for d in context.user_data.get("avail_dates", [])]
    if not dates:
        # Reconstruct from the selected dates' week
        dates = get_next_week_dates()
        context.user_data["avail_dates"] = [d.isoformat() for d in dates]

    keyboard = _build_day_keyboard(dates, selected, "avail")
    await query.edit_message_reply_markup(reply_markup=keyboard)


async def send_availability_prompt(
    bot,
    telegram_id: int,
    dates: list[date],
    is_reminder: bool = False,
):
    """
    Send the availability prompt to a team member.
    Called by the scheduled job, not directly by a command handler.
    """
    monday = dates[0]
    saturday = dates[-1]
    header = (
        f"{'🔔 Reminder: ' if is_reminder else '📅 '}What days are you available "
        f"next week ({monday.strftime('%d %b')} – {saturday.strftime('%d %b')})?\n\n"
        f"Tap the days that work, then hit Submit."
    )

    keyboard = _build_day_keyboard(dates, set(), "avail")

    await bot.send_message(
        chat_id=telegram_id,
        text=header,
        reply_markup=keyboard,
    )


# ──────────────────────────────────────────────
# Admin: /confirmweek
# ──────────────────────────────────────────────

async def confirmweek_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin command to notify members of their confirmed shifts for next week.

    1. Reads the Confirmed checkbox from the Availability table
    2. DMs each member with their confirmed days (private)
    3. Posts a full weekly schedule summary to the group chat (public)
    """
    telegram_id = update.effective_user.id

    # Check admin role
    member = at.get_member_by_telegram_id(telegram_id)
    if not member or member["fields"].get("Role") != "admin":
        await update.message.reply_text("⚠️ Only admins can use this command.")
        return

    # Determine next week's Monday
    dates = get_next_week_dates()
    week_starting = dates[0].isoformat()

    confirmations = notify_confirmed_shifts(week_starting)

    if not confirmations:
        await update.message.reply_text(
            "No confirmed availability found for next week. "
            "Make sure you've ticked the Confirmed checkbox in Airtable."
        )
        return

    # ── 1. Send private DMs to each member ──
    sent_count = 0
    for entry in confirmations:
        tg_id = entry.get("telegram_id")
        if not tg_id:
            continue

        day_list = ", ".join(
            _format_date_short(d) for d in sorted(entry["dates"])
        )
        msg = (
            f"✅ You're confirmed for next week:\n{day_list}\n\n"
            f"See you then!"
        )

        try:
            await context.bot.send_message(chat_id=tg_id, text=msg)
            sent_count += 1
        except Exception:
            pass

    # ── 2. Post schedule summary to group chat ──
    if config.TELEGRAM_GROUP_CHAT_ID:
        group_msg = _build_group_schedule(confirmations, dates)
        try:
            await context.bot.send_message(
                chat_id=config.TELEGRAM_GROUP_CHAT_ID,
                text=group_msg,
            )
        except Exception as e:
            await update.message.reply_text(
                f"⚠️ Couldn't post to group chat: {e}"
            )

    await update.message.reply_text(
        f"Notifications sent to {sent_count} member(s)."
    )


def _build_group_schedule(confirmations: list[dict], dates: list[date]) -> str:
    """
    Build a readable weekly schedule for the group chat.

    Output example:
        📅 Next week's schedule (27 Apr – 02 May):
        Mon 27 — Faqih, Taufiq
        Wed 29 — Faqih
        Thu 30 — Taufiq
    """
    monday = dates[0]
    saturday = dates[-1]

    # Build a dict: date_str → list of names
    schedule = {}
    for entry in confirmations:
        name = entry.get("name", "Unknown")
        for d in entry.get("dates", []):
            if d not in schedule:
                schedule[d] = []
            schedule[d].append(name)

    lines = [
        f"📅 Next week's schedule ({monday.strftime('%d %b')} – {saturday.strftime('%d %b')}):"
    ]

    # Iterate through all 6 days (Mon-Sat), only show days with people
    for d in dates:
        iso = d.isoformat()
        if iso in schedule:
            day_label = f"{DAY_NAMES[d.weekday()]} {d.strftime('%d')}"
            names = ", ".join(sorted(schedule[iso]))
            lines.append(f"{day_label} — {names}")

    if len(lines) == 1:
        lines.append("No confirmed shifts.")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _format_date_short(iso_str: str) -> str:
    """Format '2026-04-27' as 'Mon 27 Apr'."""
    try:
        d = date.fromisoformat(iso_str) if isinstance(iso_str, str) else iso_str
        return f"{DAY_NAMES[d.weekday()]} {d.strftime('%d %b')}"
    except (ValueError, TypeError):
        return str(iso_str)
