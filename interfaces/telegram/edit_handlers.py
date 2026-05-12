"""
Telegram handlers for shift edit requests.

Uses python-telegram-bot's ConversationHandler for the multi-step
/editshift flow:

1. /editshift → bot lists recent editable shifts as inline buttons
2. User taps a shift → bot asks for new start time
3. User types start time → bot asks for new end time
4. User types end time → bot asks for reason
5. User types reason → bot creates request, notifies admins

Admin approval/rejection uses inline callback buttons.
"""

from datetime import datetime
from zoneinfo import ZoneInfo
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
    approve_edit,
    reject_edit,
    EditError,
)
from core import airtable_client as at
import config

TZ = ZoneInfo(config.TIMEZONE)

# Conversation states
SELECT_SHIFT, ENTER_START, ENTER_END, ENTER_REASON = range(4)


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
        start = _format_dt(s["start"]) if s["start"] else "?"
        end = _format_dt(s["end"]) if s["end"] else "?"
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

    # Store the selected shift ID in conversation context
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
        dt = datetime.strptime(text, "%d/%m/%Y %H:%M")
        dt = dt.replace(tzinfo=TZ)
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
    """User entered the corrected end time."""
    text = update.message.text.strip()

    try:
        dt = datetime.strptime(text, "%d/%m/%Y %H:%M")
        dt = dt.replace(tzinfo=TZ)
        context.user_data["edit_end"] = dt.isoformat()
    except ValueError:
        await update.message.reply_text(
            "Couldn't parse that. Use format: `DD/MM/YYYY HH:MM`\n"
            "Example: `25/04/2026 18:00`",
            parse_mode="Markdown",
        )
        return ENTER_END

    await update.message.reply_text("What's the reason for this edit?")
    return ENTER_REASON


async def reason_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User entered the reason. Submit the edit request and notify admins."""
    telegram_id = update.effective_user.id
    reason = update.message.text.strip()

    shift_id = context.user_data.get("edit_shift_id")
    requested_start = context.user_data.get("edit_start")
    requested_end = context.user_data.get("edit_end")

    try:
        result = submit_edit_request(
            telegram_id=telegram_id,
            shift_record_id=shift_id,
            requested_start=requested_start,
            requested_end=requested_end,
            reason=reason,
        )
    except EditError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return ConversationHandler.END

    await update.message.reply_text("✅ Edit request submitted. Waiting for admin approval.")

    # Notify all admins
    request_id = result["request"]["id"]
    admin_msg = (
        f"📝 Shift edit request from {result['member_name']}:\n\n"
        f"Original: {_format_dt(result['original_start'])} → "
        f"{_format_dt(result['original_end']) if result['original_end'] else '—'}\n"
        f"Requested: {_format_dt(result['requested_start'])} → "
        f"{_format_dt(result['requested_end'])}\n"
        f"Reason: {result['reason']}"
    )

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"edit_approve:{request_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"edit_reject:{request_id}"),
        ]
    ])

    admins = at.get_admin_members()
    for admin in admins:
        admin_tg_id = admin["fields"].get("Telegram user ID")
        if admin_tg_id:
            try:
                await context.bot.send_message(
                    chat_id=admin_tg_id,
                    text=admin_msg,
                    reply_markup=buttons,
                )
            except Exception:
                pass  # Admin may not have started the bot yet

    # Clean up conversation data
    context.user_data.pop("edit_shift_id", None)
    context.user_data.pop("edit_start", None)
    context.user_data.pop("edit_end", None)

    return ConversationHandler.END


async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the edit flow."""
    context.user_data.pop("edit_shift_id", None)
    context.user_data.pop("edit_start", None)
    context.user_data.pop("edit_end", None)
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

        # Notify the requester
        requester_tg_id = result.get("requester_telegram_id")
        if requester_tg_id:
            await context.bot.send_message(
                chat_id=requester_tg_id,
                text="✅ Your shift edit request has been approved.",
            )

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

        # Notify the requester
        requester_tg_id = result.get("requester_telegram_id")
        if requester_tg_id:
            notes = result.get("admin_notes", "")
            msg = "❌ Your shift edit request was rejected."
            if notes:
                msg += f"\nNote: {notes}"
            await context.bot.send_message(chat_id=requester_tg_id, text=msg)

    except EditError as e:
        await query.edit_message_text(query.message.text + f"\n\n⚠️ {e}")


# ──────────────────────────────────────────────
# Build the ConversationHandler
# ──────────────────────────────────────────────

def build_edit_conversation_handler() -> ConversationHandler:
    """
    Build and return the ConversationHandler for /editshift.
    This should be added to the Application in main.py.
    """
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


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _format_dt(iso_str: str) -> str:
    """Format ISO datetime for display."""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d %b %H:%M")
    except (ValueError, TypeError):
        return str(iso_str) if iso_str else "—"
