"""
Telegram handlers for shift edit requests.

Uses python-telegram-bot's ConversationHandler for the multi-step
/editshift flow:

1. /editshift → bot lists recent editable shifts as inline buttons
2. User taps a shift → bot asks for new start time
3. User types start time → bot asks for new end time
4. User types end time → validated, bot asks for reason
5. User types reason → bot creates request, notifies admins

Admin approval/rejection uses inline callback buttons.
"""

import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from core.edits import (
    get_editable_shifts,
    submit_edit_request,
    validate_edit_times,
    approve_edit,
    reject_edit,
    EditError,
)
from core import airtable_client as at
from core.timeutils import TZ, fmt_dt

logger = logging.getLogger(__name__)

# Conversation states
SELECT_SHIFT, ENTER_START, ENTER_END, ENTER_REASON = range(4)

TIME_FORMAT = "%d/%m/%Y %H:%M"


def _clear_edit_data(context):
    for key in ("edit_shift_id", "edit_start", "edit_end"):
        context.user_data.pop(key, None)


async def editshift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the edit shift flow. Lists recent editable shifts."""
    telegram_id = update.effective_user.id

    try:
        shifts = get_editable_shifts(telegram_id)
    except EditError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return ConversationHandler.END

    if not shifts:
        await update.message.reply_text(
            "No editable shifts found. Only closed or auto-closed shifts can be edited."
        )
        return ConversationHandler.END

    # Build inline buttons — one per shift
    buttons = []
    for s in shifts:
        start = fmt_dt(s["start"]) if s["start"] else "?"
        end = fmt_dt(s["end"]) if s["end"] else "?"
        icon = "🔶" if s["status"] == "Auto-closed" else "✅"
        label = f"{icon} {start} → {end}"
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"edit_select:{s['record_id']}")]
        )

    buttons.append([InlineKeyboardButton("Cancel", callback_data="edit_select:cancel")])

    await update.message.reply_text(
        "Which shift do you want to edit?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return SELECT_SHIFT


async def shift_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User selected a shift to edit."""
    query = update.callback_query
    await query.answer()

    data = query.data.replace("edit_select:", "")

    if data == "cancel":
        await query.edit_message_text("Edit cancelled.")
        return ConversationHandler.END

    context.user_data["edit_shift_id"] = data

    await query.edit_message_text(
        "Enter the corrected *start time* for this shift.\n"
        "Format: `DD/MM/YYYY HH:MM` (e.g. `25/04/2026 09:00`)\n\n"
        "Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return ENTER_START


async def start_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User entered the corrected start time."""
    text = update.message.text.strip()

    try:
        dt = datetime.strptime(text, TIME_FORMAT).replace(tzinfo=TZ)
        context.user_data["edit_start"] = dt.isoformat()
    except ValueError:
        await update.message.reply_text(
            "Couldn't parse that. Use format: `DD/MM/YYYY HH:MM`\n"
            "Example: `25/04/2026 09:00`",
            parse_mode="Markdown",
        )
        return ENTER_START

    await update.message.reply_text(
        "Now enter the corrected *end time*.\n"
        "Format: `DD/MM/YYYY HH:MM`",
        parse_mode="Markdown",
    )
    return ENTER_END


async def end_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User entered the corrected end time. Validate the pair immediately."""
    text = update.message.text.strip()

    try:
        dt = datetime.strptime(text, TIME_FORMAT).replace(tzinfo=TZ)
    except ValueError:
        await update.message.reply_text(
            "Couldn't parse that. Use format: `DD/MM/YYYY HH:MM`\n"
            "Example: `25/04/2026 18:00`",
            parse_mode="Markdown",
        )
        return ENTER_END

    # Validate now so the user can correct immediately, not after typing a reason
    try:
        validate_edit_times(context.user_data.get("edit_start"), dt.isoformat())
    except EditError as e:
        await update.message.reply_text(
            f"⚠️ {e}\nEnter the end time again, or /cancel."
        )
        return ENTER_END

    context.user_data["edit_end"] = dt.isoformat()
    await update.message.reply_text("What's the reason for this edit?")
    return ENTER_REASON


async def reason_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User entered the reason. Submit the edit request and notify admins."""
    telegram_id = update.effective_user.id
    reason = update.message.text.strip()

    try:
        result = submit_edit_request(
            telegram_id=telegram_id,
            shift_record_id=context.user_data.get("edit_shift_id"),
            requested_start=context.user_data.get("edit_start"),
            requested_end=context.user_data.get("edit_end"),
            reason=reason,
        )
    except EditError as e:
        await update.message.reply_text(f"⚠️ {e}")
        _clear_edit_data(context)
        return ConversationHandler.END

    await update.message.reply_text("✅ Edit request submitted. Waiting for admin approval.")

    # Notify all admins
    request_id = result["request"]["id"]
    admin_msg = (
        f"📝 Shift edit request from {result['member_name']}:\n\n"
        f"Original: {fmt_dt(result['original_start'])} → "
        f"{fmt_dt(result['original_end']) if result['original_end'] else '—'}\n"
        f"Requested: {fmt_dt(result['requested_start'])} → "
        f"{fmt_dt(result['requested_end'])}\n"
        f"Reason: {result['reason']}"
    )

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"edit_approve:{request_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"edit_reject:{request_id}"),
        ]
    ])

    notified = 0
    for admin in at.get_admin_members():
        admin_tg_id = admin["fields"].get("Telegram user ID")
        if admin_tg_id:
            try:
                await context.bot.send_message(
                    chat_id=admin_tg_id,
                    text=admin_msg,
                    reply_markup=buttons,
                )
                notified += 1
            except Exception:
                # Admin may not have started the bot yet
                logger.exception("Failed to notify admin %s of edit request",
                                 admin["fields"].get("Name"))

    if notified == 0:
        logger.error("Edit request %s: no admin could be notified", request_id)
        await update.message.reply_text(
            "⚠️ Heads-up: I couldn't reach any admin on Telegram. "
            "The request is saved — you may want to tell them directly."
        )

    _clear_edit_data(context)
    return ConversationHandler.END


async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the edit flow."""
    _clear_edit_data(context)
    await update.message.reply_text("Edit cancelled.")
    return ConversationHandler.END


# ──────────────────────────────────────────────
# Admin approval/rejection callbacks
# ──────────────────────────────────────────────

async def edit_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin tapped Approve on an edit request."""
    query = update.callback_query
    await query.answer()

    request_id = query.data.replace("edit_approve:", "")
    admin_telegram_id = query.from_user.id

    try:
        result = approve_edit(request_id, admin_telegram_id)
        await query.edit_message_text(
            query.message.text + f"\n\n✅ Approved by {result['admin_name']}"
        )

        requester_tg_id = result.get("requester_telegram_id")
        if requester_tg_id:
            try:
                await context.bot.send_message(
                    chat_id=requester_tg_id,
                    text="✅ Your shift edit request has been approved.",
                )
            except Exception:
                logger.exception("Failed to notify requester of approval")

    except EditError as e:
        await query.edit_message_text(query.message.text + f"\n\n⚠️ {e}")


async def edit_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin tapped Reject on an edit request."""
    query = update.callback_query
    await query.answer()

    request_id = query.data.replace("edit_reject:", "")
    admin_telegram_id = query.from_user.id

    try:
        result = reject_edit(request_id, admin_telegram_id)
        await query.edit_message_text(
            query.message.text + f"\n\n❌ Rejected by {result['admin_name']}"
        )

        requester_tg_id = result.get("requester_telegram_id")
        if requester_tg_id:
            notes = result.get("admin_notes", "")
            msg = "❌ Your shift edit request was rejected."
            if notes:
                msg += f"\nNote: {notes}"
            try:
                await context.bot.send_message(chat_id=requester_tg_id, text=msg)
            except Exception:
                logger.exception("Failed to notify requester of rejection")

    except EditError as e:
        await query.edit_message_text(query.message.text + f"\n\n⚠️ {e}")


# ──────────────────────────────────────────────
# Build the ConversationHandler
# ──────────────────────────────────────────────

def build_edit_conversation_handler() -> ConversationHandler:
    """Build and return the ConversationHandler for /editshift."""
    return ConversationHandler(
        entry_points=[CommandHandler("editshift", editshift_start)],
        states={
            SELECT_SHIFT: [
                CallbackQueryHandler(shift_selected, pattern=r"^edit_select:")
            ],
            ENTER_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, start_entered),
                CommandHandler("cancel", cancel_edit),
            ],
            ENTER_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, end_entered),
                CommandHandler("cancel", cancel_edit),
            ],
            ENTER_REASON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, reason_entered),
                CommandHandler("cancel", cancel_edit),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_edit)],
    )
