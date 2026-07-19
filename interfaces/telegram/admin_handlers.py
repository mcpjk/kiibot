"""
Telegram handlers for admin commands.

/payroll [YYYY-MM]        — payroll summary per member for a pay month
/lockmonth YYYY-MM        — lock all shifts in a pay month (no more edits)
/setrate <username> <rate> [reason] — change a member's hourly rate
                            (writes a Rate History audit record)
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from core import airtable_client as at
from core.timeutils import now

logger = logging.getLogger(__name__)

LOCKABLE_STATUSES = ("Closed", "Auto-closed", "Edit-approved")


async def _require_admin(update: Update) -> dict:
    """Return the admin's member record, or None (after replying) if not admin."""
    member = at.get_member_by_telegram_id(update.effective_user.id)
    if not member or member["fields"].get("Role") != "admin":
        await update.message.reply_text("⚠️ Only admins can use this command.")
        return None
    return member


async def chatid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /chatid — reply with the current chat's ID. Run it inside a group to
    get the value for TELEGRAM_GROUP_CHAT_ID. Admin-only, and silently
    ignored for everyone else so it never adds noise to the group.
    """
    member = at.get_member_by_telegram_id(update.effective_user.id)
    if not member or member["fields"].get("Role") != "admin":
        return
    chat = update.effective_chat
    await update.message.reply_text(
        f"Chat ID: {chat.id}\n"
        f"Type: {chat.type}\n"
        f"Title: {chat.title or '—'}\n\n"
        f"Set TELEGRAM_GROUP_CHAT_ID to this value (Railway variables), then redeploy."
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
    """Handle /payroll [YYYY-MM] — defaults to the current month."""
    if not await _require_admin(update):
        return

    if context.args:
        try:
            pay_month = _parse_pay_month(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /payroll [YYYY-MM], e.g. /payroll 2026-06")
            return
    else:
        pay_month = now().strftime("%Y-%m")

    shifts = at.get_shifts_for_payroll(pay_month)
    if not shifts:
        await update.message.reply_text(f"No completed shifts found for {pay_month}.")
        return

    members = at.get_all_members_indexed()

    # Aggregate per member
    totals = {}
    any_open_edit_flag = False
    for s in shifts:
        f = s["fields"]
        member_ids = f.get("Member", [])
        member = members.get(member_ids[0]) if member_ids else None
        name = member["fields"].get("Name", "Unknown") if member else "Unknown"

        t = totals.setdefault(name, {"hours": 0.0, "gross": 0.0, "shifts": 0,
                                     "auto_closed": 0})
        t["hours"] += f.get("Duration (hours)") or 0
        t["gross"] += f.get("Gross pay (SGD)") or 0
        t["shifts"] += 1
        if f.get("Status") == "Auto-closed":
            t["auto_closed"] += 1
            any_open_edit_flag = True

    lines = [f"💰 Payroll summary — {pay_month}:\n"]
    grand_total = 0.0
    for name in sorted(totals):
        t = totals[name]
        flag = f" ({t['auto_closed']} auto-closed ⚠️)" if t["auto_closed"] else ""
        lines.append(
            f"{name}: {t['hours']:.2f} hrs, ${t['gross']:.2f} "
            f"({t['shifts']} shifts){flag}"
        )
        grand_total += t["gross"]

    lines.append(f"\nTotal: ${grand_total:.2f}")
    if any_open_edit_flag:
        lines.append(
            "\n⚠️ Auto-closed shifts may have wrong end times — "
            "check them before paying, then /lockmonth to freeze."
        )

    pending = at.get_pending_edit_requests()
    if pending:
        lines.append(f"⚠️ {len(pending)} edit request(s) still pending review.")

    await update.message.reply_text("\n".join(lines))


# ──────────────────────────────────────────────
# /lockmonth
# ──────────────────────────────────────────────

async def lockmonth_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /lockmonth YYYY-MM — set all completed shifts to Locked."""
    if not await _require_admin(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /lockmonth YYYY-MM, e.g. /lockmonth 2026-06")
        return

    try:
        pay_month = _parse_pay_month(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /lockmonth YYYY-MM, e.g. /lockmonth 2026-06")
        return

    # Refuse to lock a month with pending edit requests for its shifts
    pending = at.get_pending_edit_requests()
    if pending:
        await update.message.reply_text(
            f"⚠️ There are {len(pending)} pending edit request(s). "
            f"Approve or reject them first, then lock the month."
        )
        return

    shifts = at.get_shifts_for_payroll(pay_month)
    to_lock = [s for s in shifts if s["fields"].get("Status") in LOCKABLE_STATUSES]

    if not to_lock:
        await update.message.reply_text(
            f"No unlocked completed shifts found for {pay_month}."
        )
        return

    at.batch_update_shifts(
        [{"id": s["id"], "fields": {"Status": "Locked"}} for s in to_lock]
    )
    logger.info("Locked %d shift(s) for %s", len(to_lock), pay_month)

    await update.message.reply_text(
        f"🔒 Locked {len(to_lock)} shift(s) for {pay_month}. "
        f"Members can no longer request edits on them."
    )


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
