"""
Telegram handlers for availability management.

Member flow:
- Receives inline keyboard with day buttons (Mon-Sat)
- Taps days they're available → toggles on/off
- Taps "Submit" to confirm, or "Not available next week" to answer
  with no days
- Admins (other than the member themselves) get a DM for every answer
  that changes something

Admin flow:
- /confirmweek → bot sends confirmed-day notifications to all members
  AND posts a weekly schedule summary to the group chat
"""

import logging
from datetime import date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from core.availability import (
    get_next_week_dates,
    get_member_week_status,
    submit_availability,
    declare_unavailable,
    format_admin_notice,
    notify_confirmed_shifts,
    AvailabilityError,
)
from core.membership import run_membership_audit, format_audit_report
from core import airtable_client as at
from core.timeutils import DAY_NAMES, fmt_date_short
from interfaces.telegram.callback_utils import safe_answer
import config

logger = logging.getLogger(__name__)


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
        label = f"{'✅ ' if iso in selected else ''}{day_name} {d.day}"
        row.append(
            InlineKeyboardButton(label, callback_data=f"{callback_prefix}:{iso}")
        )
        if len(row) == 3:  # 3 buttons per row
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # The week rides in the callback data rather than being recomputed at
    # tap time: a Thursday prompt tapped after the weekend would otherwise
    # mark the wrong week unavailable.
    buttons.append([
        InlineKeyboardButton(
            "🚫 Not available next week",
            callback_data=f"{callback_prefix}:none:{dates[0].isoformat()}",
        ),
    ])
    buttons.append([
        InlineKeyboardButton("📤 Submit", callback_data=f"{callback_prefix}:submit"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"{callback_prefix}:cancel"),
    ])

    return InlineKeyboardMarkup(buttons)


async def availability_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle inline button presses for availability selection.

    callback_data: 'avail:<ISO_DATE>' toggles a day,
    'avail:submit' submits, 'avail:none:<MONDAY_ISO>' declares the week
    unavailable, 'avail:cancel' cancels.

    Note: query.answer() may only be called once per callback, so each
    branch answers exactly once (the empty-submit branch uses an alert).
    """
    query = update.callback_query
    data = query.data.replace("avail:", "")

    context.user_data.setdefault("avail_selected", set())
    context.user_data.setdefault("avail_dates", [])

    selected = context.user_data["avail_selected"]

    if data == "cancel":
        await safe_answer(query)
        context.user_data.pop("avail_selected", None)
        context.user_data.pop("avail_dates", None)
        await query.edit_message_text("Availability submission cancelled.")
        return

    if data.startswith("none:"):
        await safe_answer(query)
        try:
            result = declare_unavailable(query.from_user.id, data[len("none:"):])
            msg = "🚫 Got it — you're marked as not available next week."
            if result["removed"]:
                removed_list = ", ".join(fmt_date_short(d) for d in result["removed"])
                msg += f"\n(Removed: {removed_list})"
            msg += (
                "\n\nCan work after all? Use /availability any time before "
                "the schedule is confirmed."
            )
        except AvailabilityError as e:
            result = None
            msg = f"⚠️ {e}"

        context.user_data.pop("avail_selected", None)
        context.user_data.pop("avail_dates", None)
        await query.edit_message_text(msg)
        if result:
            await _notify_admins(context.bot, result)
        return

    if data == "submit":
        if not selected:
            # Alert must be the FIRST (and only) answer to this callback
            await safe_answer(
                query,
                "Select at least one day first — or tap "
                "\"Not available next week\".",
                show_alert=True,
            )
            return
        await safe_answer(query)

        telegram_id = query.from_user.id
        try:
            result = submit_availability(telegram_id, sorted(selected))
            final = sorted(result["kept"] + result["created"])
            removed = result["removed"]

            day_list = ", ".join(fmt_date_short(d) for d in final)
            msg = f"✅ Availability submitted for: {day_list}"
            if removed:
                removed_list = ", ".join(fmt_date_short(d) for d in sorted(removed))
                msg += f"\n(Removed: {removed_list})"
            msg += (
                "\n\nNeed to change it? Use /availability any time before "
                "the schedule is confirmed."
            )

        except AvailabilityError as e:
            result = None
            msg = f"⚠️ {e}"

        context.user_data.pop("avail_selected", None)
        context.user_data.pop("avail_dates", None)
        await query.edit_message_text(msg)
        if result:
            await _notify_admins(context.bot, result)
        return

    # Toggle a day
    await safe_answer(query)
    if data in selected:
        selected.discard(data)
    else:
        selected.add(data)

    # Rebuild the keyboard with updated selection
    dates = [date.fromisoformat(d) for d in context.user_data.get("avail_dates", [])]
    if not dates:
        dates = get_next_week_dates()
        context.user_data["avail_dates"] = [d.isoformat() for d in dates]

    keyboard = _build_day_keyboard(dates, selected, "avail")
    await query.edit_message_reply_markup(reply_markup=keyboard)


async def _notify_admins(bot, result: dict):
    """
    DM every admin about a member's availability answer — except the
    member themselves, so an admin in the cycle isn't told about their
    own submission. Best-effort: the submission already succeeded, so a
    failed DM is logged, never surfaced to the member.
    """
    msg = format_admin_notice(result)
    if not msg:
        return
    try:
        admins = at.get_admin_members()
    except Exception:
        logger.exception("Couldn't load admins for availability notice")
        return
    for admin in admins:
        if admin["id"] == result["member"]["id"]:
            continue
        tg_id = admin["fields"].get("Telegram user ID")
        if not tg_id:
            continue
        try:
            await bot.send_message(chat_id=tg_id, text=msg)
        except Exception:
            logger.exception("Failed to send availability notice to admin %s",
                             admin["fields"].get("Name"))


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
        f"Tap the days that work, then hit Submit — or tap "
        f"\"Not available next week\" if you can't work any of them."
    )

    keyboard = _build_day_keyboard(dates, set(), "avail")

    await bot.send_message(
        chat_id=telegram_id,
        text=header,
        reply_markup=keyboard,
    )


# ──────────────────────────────────────────────
# Member: /availability (view / edit next week)
# ──────────────────────────────────────────────

async def availability_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /availability — show next week's day picker pre-ticked with what the
    member already submitted, so they can add/remove days and re-submit.
    Locked once an admin has ticked Confirmed on any of their days (the
    roster is being built at that point — changes go through an admin).
    """
    telegram_id = update.effective_user.id
    member = at.get_member_by_telegram_id(telegram_id)

    if not member:
        await update.message.reply_text(
            "⚠️ You're not registered in the system. Send /start first."
        )
        return
    f = member["fields"]
    if f.get("Status") != "Active":
        await update.message.reply_text("⚠️ Your account is not active. Contact an admin.")
        return
    if not f.get("Weekly availability"):
        await update.message.reply_text(
            "You're not part of the weekly availability cycle — "
            "ask an admin if that seems wrong."
        )
        return

    status = get_member_week_status(telegram_id)
    if status["locked"]:
        await update.message.reply_text(
            "🔒 Your schedule for next week is already being confirmed — "
            "contact an admin if you need to change it."
        )
        return

    dates = status["dates"]
    selected = set(status["selected"])
    context.user_data["avail_selected"] = selected
    context.user_data["avail_dates"] = [d.isoformat() for d in dates]

    week = f"({dates[0].strftime('%d %b')} – {dates[-1].strftime('%d %b')})"
    if selected:
        header = (
            f"📅 Your availability for next week {week}.\n\n"
            f"Tap days to toggle, then hit Submit to save."
        )
    elif status["unavailable"]:
        header = (
            f"🚫 You're marked as not available next week {week}.\n\n"
            f"Can work after all? Tap the days that work, then hit Submit."
        )
    else:
        header = (
            f"📅 You haven't submitted availability for next week yet "
            f"{week}.\n\n"
            f"Tap the days that work, then hit Submit — or tap "
            f"\"Not available next week\"."
        )
    keyboard = _build_day_keyboard(dates, selected, "avail")
    await update.message.reply_text(header, reply_markup=keyboard)


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

    member = at.get_member_by_telegram_id(telegram_id)
    if not at.is_admin(member):
        await update.message.reply_text("⚠️ Only admins can use this command.")
        return

    dates = get_next_week_dates()
    week_starting = dates[0].isoformat()

    confirmations = notify_confirmed_shifts(week_starting)

    if not confirmations:
        await update.message.reply_text(
            "No confirmed availability found for next week. "
            "Make sure you've ticked the Confirmed checkbox in Airtable."
        )
        await _audit_and_report(update, context)
        return

    # ── 1. Send private DMs to each member ──
    sent_count = 0
    for entry in confirmations:
        tg_id = entry.get("telegram_id")
        if not tg_id:
            logger.warning("Member %s has no Telegram ID; skipping DM", entry["name"])
            continue

        day_list = ", ".join(fmt_date_short(d) for d in sorted(entry["dates"]))
        msg = f"✅ You're confirmed for next week:\n{day_list}\n\nSee you then!"

        try:
            await context.bot.send_message(chat_id=tg_id, text=msg)
            sent_count += 1
        except Exception:
            logger.exception("Failed to DM confirmation to %s", entry["name"])

    # ── 2. Post schedule summary to group chat ──
    if config.TELEGRAM_GROUP_CHAT_ID:
        group_msg = _build_group_schedule(confirmations, dates)
        try:
            await context.bot.send_message(
                chat_id=config.TELEGRAM_GROUP_CHAT_ID,
                text=group_msg,
            )
        except Exception as e:
            logger.exception("Failed to post schedule to group chat")
            await update.message.reply_text(f"⚠️ Couldn't post to group chat: {e}")

    await update.message.reply_text(f"Notifications sent to {sent_count} member(s).")

    # Weekly membership audit rides along with schedule confirmation,
    # while attention is already on the week's organisation.
    await _audit_and_report(update, context)


async def _audit_and_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run the group membership audit and reply with the report."""
    try:
        report = await run_membership_audit(context.bot)
    except Exception:
        logger.exception("Membership audit failed")
        await update.message.reply_text("⚠️ Membership audit failed — see logs.")
        return
    await update.message.reply_text(format_audit_report(report))


def _build_group_schedule(confirmations: list[dict], dates: list[date]) -> str:
    """
    Build a readable weekly schedule for the group chat.

    Output example:
        📅 Next week's schedule (27 Apr – 02 May):
        Mon 27 — Faqih, Taufiq
        Wed 29 — Faqih
    """
    monday = dates[0]
    saturday = dates[-1]

    schedule = {}
    for entry in confirmations:
        name = entry.get("name", "Unknown")
        for d in entry.get("dates", []):
            schedule.setdefault(d, []).append(name)

    lines = [
        f"📅 Next week's schedule ({monday.strftime('%d %b')} – {saturday.strftime('%d %b')}):"
    ]

    for d in dates:
        iso = d.isoformat()
        if iso in schedule:
            day_label = f"{DAY_NAMES[d.weekday()]} {d.strftime('%d')}"
            names = ", ".join(sorted(schedule[iso]))
            lines.append(f"{day_label} — {names}")

    if len(lines) == 1:
        lines.append("No confirmed shifts.")

    return "\n".join(lines)
