"""
Telegram handlers for admin commands.

/payroll [YYYY-MM]        — payroll summary per member for a pay month
/lockmonth YYYY-MM        — lock all shifts in a pay month (no more edits)
/setrate <username> <rate> [reason] — change a member's hourly rate
                            (writes a Rate History audit record)
"""

import logging
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from core import airtable_client as at
from core.payroll import (
    PayrollError,
    build_payroll_summary,
    format_payroll_summary,
    has_payroll_access,
    lock_month,
    previous_pay_month,
)
from core.timeutils import now

logger = logging.getLogger(__name__)

# Telegram rejects messages over 4096 chars with BadRequest("Message is
# too long"). Error text from third-party APIs (HTML error pages, big
# JSON bodies) can blow past that, and the send failure then masks the
# very error we were reporting — seen live 2026-08-04 on /snapshot.
TELEGRAM_TEXT_LIMIT = 3500


def _short_error(e: Exception) -> str:
    """One-line, length-capped rendering of an exception for a DM.
    The full traceback always goes to the logs."""
    text = f"{type(e).__name__}: {e}".replace("\n", " ")
    if len(text) > TELEGRAM_TEXT_LIMIT:
        text = text[:TELEGRAM_TEXT_LIMIT] + " …[truncated — see logs]"
    return text


async def _require_admin(update: Update) -> dict:
    """Return the admin's member record, or None (after replying) if not admin."""
    member = at.get_member_by_telegram_id(update.effective_user.id)
    if not at.is_admin(member):
        await update.message.reply_text("⚠️ Only admins can use this command.")
        return None
    return member


async def _require_payroll_access(update: Update) -> dict:
    """
    Return the member record for someone allowed to run payroll — an
    admin, or a member with the 'Payroll handler' checkbox. A handler
    who couldn't run /payroll would get a monthly prompt they can't act
    on, so the prompt and the permission share one rule.
    """
    member = at.get_member_by_telegram_id(update.effective_user.id)
    if not has_payroll_access(member):
        await update.message.reply_text(
            "⚠️ Only admins and payroll handlers can use this command."
        )
        return None
    return member


def _lock_button(pay_month: str) -> InlineKeyboardMarkup:
    """The 'lock this month' button shown under a payroll summary."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"🔒 Lock {pay_month}",
                               callback_data=f"paylock:{pay_month}")]]
    )


def _lock_confirm_markup(pay_month: str) -> InlineKeyboardMarkup:
    """Yes/Cancel buttons guarding the irreversible lock."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔒 Yes, lock it",
                             callback_data=f"paylockyes:{pay_month}"),
        InlineKeyboardButton("Cancel", callback_data="paylockno"),
    ]])


def payroll_prompt_keyboard(pay_month: str) -> InlineKeyboardMarkup:
    """The button on the monthly payroll prompt (see jobs/scheduler.py)."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"💰 Run payroll for {pay_month}",
                               callback_data=f"payrun:{pay_month}")]]
    )


async def chatid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /chatid — reply with the current chat's ID. Run it inside a group to
    get the value for TELEGRAM_GROUP_CHAT_ID. Admin-only, and silently
    ignored for everyone else so it never adds noise to the group.
    """
    member = at.get_member_by_telegram_id(update.effective_user.id)
    if not at.is_admin(member):
        return
    chat = update.effective_chat
    await update.message.reply_text(
        f"Chat ID: {chat.id}\n"
        f"Type: {chat.type}\n"
        f"Title: {chat.title or '—'}\n\n"
        f"Set TELEGRAM_GROUP_CHAT_ID to this value (Railway variables), then redeploy."
    )


async def snapshot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /snapshot — run the design score snapshot right now instead of
    waiting for 06:05. Exists so the whole chain (Airtable → gspread →
    Sheets) can be verified on demand; reports the real error text on
    failure rather than a generic message.
    """
    if not await _require_admin(update):
        return

    from core.snapshots import missing_snapshot_config, take_snapshot

    missing = missing_snapshot_config()
    if missing:
        await update.message.reply_text(
            f"⚠️ Snapshots aren't configured on this deploy.\n"
            f"Not visible to the running process: {', '.join(missing)}\n\n"
            f"Env vars only load at startup — set them, apply/redeploy, "
            f"then try again."
        )
        return

    await update.message.reply_text("Taking snapshot…")
    try:
        written = take_snapshot()
    except Exception as e:
        logger.exception("Manual snapshot failed")
        await update.message.reply_text(f"⚠️ Snapshot failed: {_short_error(e)}")
        return

    if not written:
        await update.message.reply_text(
            "No design candidates right now — nothing written. "
            "(Check Projects for Process = Designing.)"
        )
        return
    await update.message.reply_text(f"✅ Wrote {written} row(s) to the snapshot sheet.")


async def compare_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /compare [YYYY-MM-DD] — write the comparison of a day's frozen
    ranking against the design blocks actually recorded. Defaults to
    yesterday (today is still in progress, so its actuals are partial).
    """
    if not await _require_admin(update):
        return

    from core.snapshots import missing_snapshot_config, take_comparison
    from core.timeutils import now
    from datetime import timedelta

    missing = missing_snapshot_config()
    if missing:
        await update.message.reply_text(
            f"⚠️ Snapshots aren't configured on this deploy: "
            f"{', '.join(missing)}"
        )
        return

    if context.args:
        day = context.args[0]
        try:
            date.fromisoformat(day)
        except ValueError:
            await update.message.reply_text(
                "Usage: /compare [YYYY-MM-DD] — defaults to yesterday"
            )
            return
    else:
        day = (now() - timedelta(days=1)).date().isoformat()

    await update.message.reply_text(f"Comparing {day}…")
    try:
        written = take_comparison(day)
    except Exception as e:
        logger.exception("Manual comparison failed")
        await update.message.reply_text(f"⚠️ Comparison failed: {_short_error(e)}")
        return

    if not written:
        await update.message.reply_text(
            f"Nothing to compare for {day} — no snapshot from that morning "
            f"and no blocks recorded."
        )
        return
    await update.message.reply_text(
        f"✅ Wrote {written} comparison row(s) for {day}."
    )


def _parse_pay_month(arg: str) -> str:
    """Validate a YYYY-MM argument. Raises ValueError on bad input."""
    parts = arg.split("-")
    if len(parts) != 2:
        raise ValueError
    year, month = int(parts[0]), int(parts[1])
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        raise ValueError
    return f"{year:04d}-{month:02d}"


# ──────────────────────────────────────────────
# /payroll
# ──────────────────────────────────────────────

async def payroll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /payroll [YYYY-MM] — defaults to the month that just ENDED.

    Payroll is a month-end act, so the previous month is the figure
    you're paying against; the current month is always partial.
    """
    if not await _require_payroll_access(update):
        return

    if context.args:
        try:
            pay_month = _parse_pay_month(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /payroll [YYYY-MM], e.g. /payroll 2026-06")
            return
    else:
        pay_month = previous_pay_month(now().date())

    summary = build_payroll_summary(pay_month)
    if not summary["totals"]:
        await update.message.reply_text(f"No completed shifts found for {pay_month}.")
        return

    await update.message.reply_text(
        format_payroll_summary(summary),
        reply_markup=_lock_button(pay_month),
    )


# ──────────────────────────────────────────────
# /lockmonth
# ──────────────────────────────────────────────

async def lockmonth_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /lockmonth [YYYY-MM] — set all completed shifts to Locked.
    Defaults to the month that just ended, matching /payroll: the month
    you've just paid is the one you'd freeze.

    Never locks on the command alone. Since the command now takes no
    required argument, a bare /lockmonth would otherwise be one
    keystroke from a terminal, unrepeatable write.
    """
    if not await _require_payroll_access(update):
        return

    if context.args:
        try:
            pay_month = _parse_pay_month(context.args[0])
        except ValueError:
            await update.message.reply_text(
                "Usage: /lockmonth [YYYY-MM], e.g. /lockmonth 2026-06"
            )
            return
    else:
        pay_month = previous_pay_month(now().date())

    await update.message.reply_text(
        f"Lock {pay_month}? This is permanent — members can't request "
        f"edits on those shifts afterwards.",
        reply_markup=_lock_confirm_markup(pay_month),
    )


# ──────────────────────────────────────────────
# Month-end prompt buttons
# ──────────────────────────────────────────────

async def payroll_run_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Run payroll for <month>' on the month-end prompt."""
    query = update.callback_query
    pay_month = query.data.split(":", 1)[1]

    member = at.get_member_by_telegram_id(query.from_user.id)
    if not has_payroll_access(member):
        # Invariant 4: an alert must be the FIRST and ONLY answer on
        # its code path.
        await query.answer("Only admins and payroll handlers can do this.",
                           show_alert=True)
        return

    await query.answer()
    summary = build_payroll_summary(pay_month)
    if not summary["totals"]:
        await query.message.reply_text(
            f"No completed shifts found for {pay_month}."
        )
        return

    await query.message.reply_text(
        format_payroll_summary(summary),
        reply_markup=_lock_button(pay_month),
    )


async def paylock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    'Lock <month>' — asks for confirmation rather than locking on the
    first tap. Locking is terminal (no edits ever again on those
    shifts), so a stray tap must not be able to end the conversation.
    """
    query = update.callback_query
    pay_month = query.data.split(":", 1)[1]

    member = at.get_member_by_telegram_id(query.from_user.id)
    if not has_payroll_access(member):
        await query.answer("Only admins and payroll handlers can do this.",
                           show_alert=True)
        return

    await query.answer()
    await query.message.reply_text(
        f"Lock {pay_month}? This is permanent — members can't request "
        f"edits on those shifts afterwards.",
        reply_markup=_lock_confirm_markup(pay_month),
    )


async def paylock_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Yes, lock it' — the irreversible step."""
    query = update.callback_query
    pay_month = query.data.split(":", 1)[1]

    member = at.get_member_by_telegram_id(query.from_user.id)
    if not has_payroll_access(member):
        await query.answer("Only admins and payroll handlers can do this.",
                           show_alert=True)
        return

    try:
        locked = lock_month(pay_month)
    except PayrollError as e:
        await query.answer(str(e), show_alert=True)
        return
    except Exception as e:
        logger.exception("Month lock failed for %s", pay_month)
        await query.answer(_short_error(e), show_alert=True)
        return

    await query.answer(f"Locked {locked} shift(s)")
    # Drop the buttons so the finished action can't be tapped again.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not clear lock buttons", exc_info=True)
    await query.message.reply_text(
        f"🔒 Locked {locked} shift(s) for {pay_month}. "
        f"Members can no longer request edits on them."
    )


async def paylock_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Cancel' on the lock confirmation."""
    query = update.callback_query
    await query.answer("Not locked")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not clear lock buttons", exc_info=True)


# ──────────────────────────────────────────────
# /setrate
# ──────────────────────────────────────────────

async def setrate_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /setrate <username> <rate> [reason...].
    Updates the member's current rate and writes a Rate History record.
    Open shifts are unaffected (rate is snapshotted at clock-in);
    the new rate applies from the next clock-in.
    """
    admin = await _require_admin(update)
    if not admin:
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /setrate <username> <rate> [reason]\n"
            "Example: /setrate faqih 15.50 Annual review"
        )
        return

    username = context.args[0].lstrip("@")
    try:
        new_rate = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Rate must be a number, e.g. 15.50")
        return

    if not (0 < new_rate <= 1000):
        await update.message.reply_text("That rate looks wrong — double-check it.")
        return

    reason = " ".join(context.args[2:]) if len(context.args) > 2 else ""

    member = at.get_member_by_username(username)
    if not member:
        await update.message.reply_text(
            f"No member found with username '{username}'. "
            f"Check the 'Telegram username' field in Airtable."
        )
        return

    old_rate = member["fields"].get("Current hourly rate (SGD)")
    member_name = member["fields"].get("Name", username)

    at.update_member_rate(member["id"], new_rate)
    at.create_rate_history_entry(
        member_record_id=member["id"],
        rate=new_rate,
        effective_from=now().date().isoformat(),
        changed_by=admin["fields"].get("Name", "Unknown admin"),
        reason=reason,
    )
    logger.info("Rate change: %s %s -> %s by %s",
                member_name, old_rate, new_rate,
                admin["fields"].get("Name"))

    old_str = f"${old_rate:.2f}" if old_rate is not None else "unset"
    await update.message.reply_text(
        f"✅ {member_name}'s rate: {old_str} → ${new_rate:.2f}/hr "
        f"(effective from their next clock-in).\n"
        f"Rate History record created."
    )

    # Notify the member
    member_tg_id = member["fields"].get("Telegram user ID")
    if member_tg_id:
        try:
            await context.bot.send_message(
                chat_id=member_tg_id,
                text=f"💵 Your hourly rate has been updated to ${new_rate:.2f}/hr, "
                     f"effective from your next shift.",
            )
        except Exception:
            logger.exception("Failed to notify %s of rate change", member_name)
