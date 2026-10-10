"""
Telegram handlers for shift edit requests.

Uses python-telegram-bot's ConversationHandler for the multi-step
/editshift flow. Designed to need as little typing as possible (Oct
2026 rework — the old flow wanted two full DD/MM/YYYY HH:MM timestamps):

1. /editshift → recent editable shifts as buttons, plus ➕ Log a missed
   shift. The "✏️ Fix this shift" button on the clock-out and auto-close
   messages jumps straight to step 2 for that shift.
2. Shift tapped → shown with its times; "What needs fixing?"
   [End] [Start] [Both]. (Missed shift: pick a day from the last week.)
3. Times are typed WITHOUT a date — the date is the shift's own —
   leniently parsed (18:00, 1800, 6pm). End also offers an 18:00 button.
4. Reason: three canned buttons or Other (typed).
5. Confirm screen, which runs every check first. Submit → a trim (only
   removes time) applies at once and admins get an FYI; anything else
   goes to admins with Approve / Reject.

Admin approval/rejection uses inline callback buttons.
"""

import logging
from datetime import date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

import config
from core.edits import (
    get_editable_shifts,
    get_editable_shift,
    missed_shift_days,
    preview_edit,
    preview_missed_shift,
    submit_edit_request,
    submit_missed_shift,
    validate_edit_times,
    approve_edit,
    reject_edit,
    EditError,
)
from core.timeutils import at_clock, fmt_date_short, fmt_dt, now, parse_clock, parse_dt
from interfaces.telegram.callback_utils import safe_answer
from interfaces.telegram.notify import notify_admins

logger = logging.getLogger(__name__)

# Conversation states
(SELECT_SHIFT, SELECT_DAY, CHOOSE_FIELD, ENTER_START, ENTER_END,
 CHOOSE_REASON, ENTER_REASON, CONFIRM) = range(8)

# Canned reasons: ~17 of the first 24 free-text reasons were a variant
# of the first two.
REASONS = ("Forgot to clock out", "Forgot to clock in", "Worked late")

_KEYS = ("edit_mode", "edit_shift_id", "edit_day", "edit_orig_start",
         "edit_orig_end", "edit_start", "edit_end", "edit_fields",
         "edit_reason")

CANCEL_ROW = [InlineKeyboardButton("Cancel", callback_data="edit:cancel")]


def _clear_edit_data(context):
    for key in _KEYS:
        context.user_data.pop(key, None)


def fix_shift_keyboard(shift_record_id: str) -> InlineKeyboardMarkup:
    """The button under the clock-out and auto-close messages."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "✏️ Fix this shift", callback_data=f"edit_fix:{shift_record_id}")]])


def _hm(iso: str) -> str:
    dt = parse_dt(iso)
    return dt.strftime("%H:%M") if dt else "?"


def _span(start_iso: str, end_iso: str) -> str:
    return f"{_hm(start_iso)} → {_hm(end_iso) if end_iso else '—'}"


def _shift_label(s: dict) -> str:
    """'✅ Thu 25 Sep 11:10 → 20:00' — weekday included, so a shift from
    another day is hard to tap by mistake."""
    start = parse_dt(s["start"])
    day = fmt_date_short(start.date()) if start else "?"
    icon = "🔶" if s["status"] == "Auto-closed" else "✅"
    return f"{icon} {day} {_span(s['start'], s['end'])}"


async def _say(update: Update, text: str, buttons=None, new_message=False):
    """Edit the flow's message in place after a button tap; reply after
    typed input (or when the tap was on someone else's message)."""
    markup = InlineKeyboardMarkup(buttons) if buttons else None
    query = update.callback_query
    if query and not new_message:
        await query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_chat.send_message(text, reply_markup=markup)


def _day(context) -> date:
    return date.fromisoformat(context.user_data["edit_day"])


# ──────────────────────────────────────────────
# Step 1 — pick a shift (or a missed day)
# ──────────────────────────────────────────────

async def editshift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the edit shift flow. Lists recent editable shifts."""
    _clear_edit_data(context)
    try:
        shifts = get_editable_shifts(update.effective_user.id)
    except EditError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return ConversationHandler.END

    buttons = [[InlineKeyboardButton(
        _shift_label(s), callback_data=f"edit:shift:{s['record_id']}")]
        for s in shifts]
    buttons.append([InlineKeyboardButton(
        "➕ Log a missed shift", callback_data="edit:missed")])
    buttons.append(CANCEL_ROW)

    text = ("Which shift needs fixing?" if shifts else
            "No closed shifts to edit. Did you miss clocking a whole shift?")
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    return SELECT_SHIFT


async def _begin_edit(update, context, shift_record_id, new_message=False):
    try:
        s = get_editable_shift(update.effective_user.id, shift_record_id)
    except EditError as e:
        await _say(update, f"⚠️ {e}", new_message=new_message)
        return ConversationHandler.END

    context.user_data.update({
        "edit_mode": "edit",
        "edit_shift_id": shift_record_id,
        "edit_orig_start": s["start"],
        "edit_orig_end": s["end"],
        "edit_day": parse_dt(s["start"]).date().isoformat(),
    })
    await _say(update, f"{_shift_label(s)}\n\nWhat needs fixing?", [
        [InlineKeyboardButton("End time", callback_data="edit:field:end"),
         InlineKeyboardButton("Start time", callback_data="edit:field:start")],
        [InlineKeyboardButton("Both", callback_data="edit:field:both")],
        CANCEL_ROW,
    ], new_message=new_message)
    return CHOOSE_FIELD


async def shift_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    return await _begin_edit(update, context, query.data.split(":", 2)[2])


async def fix_shift_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'✏️ Fix this shift' under a clock-out / auto-close message. Starts
    a fresh message so the original summary stays readable."""
    query = update.callback_query
    await safe_answer(query)
    _clear_edit_data(context)
    return await _begin_edit(update, context, query.data.split(":", 1)[1],
                             new_message=True)


async def missed_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    days = missed_shift_days()
    labels = ["Today", "Yesterday"] + [fmt_date_short(d) for d in days[2:]]
    day_buttons = [InlineKeyboardButton(label, callback_data=f"edit:day:{d.isoformat()}")
                   for label, d in zip(labels, days)]
    rows = [day_buttons[i:i + 2] for i in range(0, len(day_buttons), 2)]
    await _say(update, "Which day did you work without clocking in?",
               rows + [CANCEL_ROW])
    return SELECT_DAY


async def day_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    context.user_data.update({
        "edit_mode": "missed",
        "edit_day": query.data.split(":", 2)[2],
        "edit_fields": "both",
    })
    return await _ask_start(update, context)


# ──────────────────────────────────────────────
# Steps 2–3 — which times, then the times
# ──────────────────────────────────────────────

async def field_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    fields = query.data.split(":", 2)[2]
    context.user_data["edit_fields"] = fields
    if fields == "end":
        return await _ask_end(update, context)
    return await _ask_start(update, context)


async def _ask_start(update, context, problem: str = ""):
    prefix = f"⚠️ {problem}\n\n" if problem else ""
    await _say(update, f"{prefix}{fmt_date_short(_day(context))}: what time did "
               f"you start? Type it, e.g. 11:00 or 9:30am.", [CANCEL_ROW])
    return ENTER_START


async def _ask_end(update, context, problem: str = ""):
    prefix = f"⚠️ {problem}\n\n" if problem else ""
    std = f"{config.STANDARD_END_HOUR:02d}:{config.STANDARD_END_MINUTE:02d}"
    await _say(update, f"{prefix}{fmt_date_short(_day(context))}: what time did "
               f"you finish? Type it (e.g. 18:45), or tap:", [
                   [InlineKeyboardButton(std, callback_data="edit:end:std")],
                   CANCEL_ROW,
               ])
    return ENTER_END


async def start_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hm = parse_clock(update.message.text)
    if hm is None:
        return await _ask_start(update, context, "Couldn't read that as a time.")
    start = at_clock(_day(context), *hm)
    if start > now():
        return await _ask_start(update, context, "That's in the future.")
    context.user_data["edit_start"] = start.isoformat()

    if context.user_data.get("edit_fields") == "start":
        try:
            validate_edit_times(start.isoformat(), context.user_data["edit_orig_end"])
        except EditError as e:
            return await _ask_start(update, context, str(e))
        return await _ask_reason(update, context)
    return await _ask_end(update, context)


async def _take_end(update, context, hour: int, minute: int):
    end = at_clock(_day(context), hour, minute)
    start_iso = (context.user_data.get("edit_start")
                 or context.user_data.get("edit_orig_start"))
    try:
        validate_edit_times(start_iso, end.isoformat())
    except EditError as e:
        return await _ask_end(update, context, str(e))
    context.user_data["edit_end"] = end.isoformat()
    return await _ask_reason(update, context)


async def end_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hm = parse_clock(update.message.text)
    if hm is None:
        return await _ask_end(update, context, "Couldn't read that as a time.")
    return await _take_end(update, context, *hm)


async def end_standard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_answer(update.callback_query)
    return await _take_end(update, context, config.STANDARD_END_HOUR,
                           config.STANDARD_END_MINUTE)


# ──────────────────────────────────────────────
# Step 4 — reason
# ──────────────────────────────────────────────

async def _ask_reason(update, context):
    rows = [[InlineKeyboardButton(r, callback_data=f"edit:reason:{i}")]
            for i, r in enumerate(REASONS)]
    rows.append([InlineKeyboardButton("Other…", callback_data="edit:reason:other")])
    await _say(update, "What happened?", rows + [CANCEL_ROW])
    return CHOOSE_REASON


async def reason_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    choice = query.data.split(":", 2)[2]
    if choice == "other":
        await _say(update, "Type the reason:", [CANCEL_ROW])
        return ENTER_REASON
    context.user_data["edit_reason"] = REASONS[int(choice)]
    return await _confirm(update, context)


async def reason_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["edit_reason"] = update.message.text.strip()
    return await _confirm(update, context)


# ──────────────────────────────────────────────
# Step 5 — confirm, submit
# ──────────────────────────────────────────────

def _requested(context) -> tuple[str, str]:
    ud = context.user_data
    return (ud.get("edit_start") or ud.get("edit_orig_start"),
            ud.get("edit_end") or ud.get("edit_orig_end"))


async def _confirm(update, context):
    """Runs every check (overlaps, pending requests, locked month) before
    Submit is offered, so a clash surfaces here and not after the tap."""
    ud = context.user_data
    start_iso, end_iso = _requested(context)
    tg_id = update.effective_user.id
    try:
        if ud["edit_mode"] == "edit":
            pv = preview_edit(tg_id, ud["edit_shift_id"], start_iso, end_iso)
        else:
            pv = preview_missed_shift(tg_id, start_iso, end_iso)
    except EditError as e:
        _clear_edit_data(context)
        await _say(update, f"⚠️ {e}\n\nStart again with /editshift.")
        return ConversationHandler.END

    day = fmt_date_short(_day(context))
    if ud["edit_mode"] == "edit":
        lines = [f"Shift on {day}",
                 f"Was: {_span(ud['edit_orig_start'], ud['edit_orig_end'])}",
                 f"Now: {_span(start_iso, end_iso)}"]
    else:
        lines = [f"Missed shift on {day}", _span(start_iso, end_iso)]
    lines.append(f"Reason: {ud['edit_reason']}")
    lines.append("")
    lines.append("This only shortens the shift, so it applies straight away."
                 if pv["auto_approve"] else "An admin will need to approve this.")

    await _say(update, "\n".join(lines), [
        [InlineKeyboardButton("✅ Submit", callback_data="edit:submit")],
        CANCEL_ROW,
    ])
    return CONFIRM


async def submit_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    ud = context.user_data
    start_iso, end_iso = _requested(context)
    tg_id = update.effective_user.id

    try:
        if ud["edit_mode"] == "edit":
            result = submit_edit_request(
                telegram_id=tg_id,
                shift_record_id=ud["edit_shift_id"],
                requested_start=start_iso,
                requested_end=end_iso,
                reason=ud["edit_reason"],
            )
        else:
            result = submit_missed_shift(tg_id, start_iso, end_iso, ud["edit_reason"])
    except EditError as e:
        _clear_edit_data(context)
        await query.edit_message_text(f"⚠️ {e}\n\nStart again with /editshift.")
        return ConversationHandler.END

    _clear_edit_data(context)

    if result["auto_approved"]:
        await query.edit_message_text(
            f"✅ Shift updated: {_span(start_iso, end_iso)}.")
        await notify_admins(context.bot, _format_auto_notice(result))
        return ConversationHandler.END

    await query.edit_message_text("✅ Sent. Waiting for admin approval.")
    request_id = result["request"]["id"]
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"edit_approve:{request_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"edit_reject:{request_id}"),
    ]])
    notified = await notify_admins(context.bot, _format_admin_request(result), buttons)
    if notified == 0:
        logger.error("Edit request %s: no admin could be notified", request_id)
        await update.effective_chat.send_message(
            "⚠️ Heads-up: I couldn't reach any admin on Telegram. "
            "The request is saved — you may want to tell them directly."
        )
    return ConversationHandler.END


def _same_day_line(result: dict) -> str:
    """Context for the approver: the member's other shifts that day.
    Clashes are refused before this point, so anything listed here is a
    separate stretch of work — but seeing it is what lets a human notice
    a day that doesn't add up."""
    others = result.get("same_day") or []
    spans = ", ".join(_span(s, e) for s, e in others) or "none"
    return f"Other shifts that day: {spans}"


def _format_admin_request(result: dict) -> str:
    if "shift_record_id" not in result:
        return (
            f"🕓 Missed-shift request from {result['member_name']}:\n\n"
            f"{fmt_dt(result['requested_start'])} → "
            f"{_hm(result['requested_end'])}\n"
            f"Reason: {result['reason']}\n"
            f"{_same_day_line(result)}"
        )
    return (
        f"📝 Shift edit request from {result['member_name']}:\n\n"
        f"Original: {fmt_dt(result['original_start'])} → "
        f"{fmt_dt(result['original_end']) if result['original_end'] else '—'}\n"
        f"Requested: {fmt_dt(result['requested_start'])} → "
        f"{fmt_dt(result['requested_end'])}\n"
        f"Reason: {result['reason']}\n"
        f"{_same_day_line(result)}"
    )


def _format_auto_notice(result: dict) -> str:
    return (
        f"ℹ️ {result['member_name']} shortened a shift (applied "
        f"automatically, as it only removes time):\n\n"
        f"Was: {fmt_dt(result['original_start'])} → {_hm(result['original_end'])}\n"
        f"Now: {fmt_dt(result['requested_start'])} → {_hm(result['requested_end'])}\n"
        f"Reason: {result['reason']}"
    )


# ──────────────────────────────────────────────
# Cancel / stale buttons
# ──────────────────────────────────────────────

async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the edit flow."""
    _clear_edit_data(context)
    await update.message.reply_text("Edit cancelled.")
    return ConversationHandler.END


async def cancel_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_answer(update.callback_query)
    _clear_edit_data(context)
    await update.callback_query.edit_message_text("Edit cancelled.")
    return ConversationHandler.END


async def stale_step_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A button from an earlier step of a flow still in progress: say so
    and stay where we are (returning None keeps the state)."""
    await safe_answer(update.callback_query,
                      "That button is from an earlier step — use the latest message.")
    return None


async def expired_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """An edit button tapped with no flow running (finished, cancelled, or
    lost to a bot restart). Registered outside the conversation."""
    await safe_answer(update.callback_query,
                      "This edit has expired. Start again with /editshift.",
                      show_alert=True)


# ──────────────────────────────────────────────
# Admin approval/rejection callbacks
# ──────────────────────────────────────────────

async def edit_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin tapped Approve on an edit request."""
    query = update.callback_query
    await safe_answer(query)

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
    await safe_answer(query)

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
    text = filters.TEXT & ~filters.COMMAND
    return ConversationHandler(
        entry_points=[
            CommandHandler("editshift", editshift_start),
            CallbackQueryHandler(fix_shift_entry, pattern=r"^edit_fix:"),
        ],
        states={
            SELECT_SHIFT: [
                CallbackQueryHandler(shift_selected, pattern=r"^edit:shift:"),
                CallbackQueryHandler(missed_selected, pattern=r"^edit:missed$"),
            ],
            SELECT_DAY: [CallbackQueryHandler(day_selected, pattern=r"^edit:day:")],
            CHOOSE_FIELD: [CallbackQueryHandler(field_selected, pattern=r"^edit:field:")],
            ENTER_START: [MessageHandler(text, start_entered)],
            ENTER_END: [
                MessageHandler(text, end_entered),
                CallbackQueryHandler(end_standard, pattern=r"^edit:end:std$"),
            ],
            CHOOSE_REASON: [CallbackQueryHandler(reason_selected, pattern=r"^edit:reason:")],
            ENTER_REASON: [MessageHandler(text, reason_entered)],
            CONFIRM: [CallbackQueryHandler(submit_selected, pattern=r"^edit:submit$")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_edit),
            CallbackQueryHandler(cancel_edit_callback, pattern=r"^edit:cancel$"),
            CallbackQueryHandler(stale_step_callback, pattern=r"^edit:"),
        ],
        # /editshift or a Fix button mid-flow restarts rather than being
        # swallowed by the flow already running.
        allow_reentry=True,
    )
