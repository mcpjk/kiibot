"""
Kii-bot main entry point.

Wires together:
- Telegram command handlers (shifts, edits, availability, admin)
- Callback query handlers (day selection, edit approval)
- Scheduled jobs (end-of-day, auto-close, availability cycle)

Run with: python main.py
"""

import logging

from telegram import BotCommand, BotCommandScopeAllPrivateChats, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from interfaces.telegram.shift_handlers import (
    start_handler,
    clockin_handler,
    clockout_handler,
    lunch_callback,
    confirmshift_handler,
    myshifts_handler,
    myrate_handler,
)
from interfaces.telegram.edit_handlers import (
    build_edit_conversation_handler,
    edit_approve_callback,
    edit_reject_callback,
    expired_edit_callback,
)
from interfaces.telegram.availability_handlers import (
    availability_callback,
    availability_command_handler,
    part_timer_callback,
    confirmweek_handler,
)
from interfaces.telegram.admin_handlers import (
    payroll_handler,
    lockmonth_handler,
    setrate_handler,
    chatid_handler,
    snapshot_handler,
    payroll_run_callback,
    paylock_callback,
    paylock_confirm_callback,
    paylock_cancel_callback,
)
from interfaces.telegram.design_handlers import (
    adjust_callback,
    extend_handler,
    space_handler,
)
from interfaces.telegram.planning_handlers import plan_handler
from interfaces.telegram.day_handlers import day_handler
from interfaces.telegram.membership_handlers import group_membership_handler
from interfaces.telegram.involvement_handlers import (
    LEAVE_LABEL,
    RESUME_LABEL,
    hide_removal_notice,
    involvement_callback,
    paused_keyboard_handler,
    me_handler,
)
from interfaces.telegram.errors import (
    describe_error,
    describe_update,
    format_error_alert,
    should_alert,
)
from interfaces.telegram.notify import post_notification, sample_callback
from jobs.scheduler import register_jobs

logger = logging.getLogger(__name__)


def setup_logging():
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    )
    # httpx logs every Telegram poll at INFO — too noisy
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """
    Central error handler: log the exception, tell the user something
    broke, and DM admins a one-line summary (throttled per exception type).
    """
    logger.error("Unhandled exception: %s — %s",
                 describe_update(update), describe_error(context.error),
                 exc_info=context.error)
    if not (isinstance(update, Update) and update.effective_message):
        # Jobs and polling errors: no member saw anything; the log is enough.
        return
    try:
        await update.effective_message.reply_text(
            "⚠️ Something went wrong on my end. Please try again — "
            "if it keeps happening, tell an admin."
        )
    except Exception:
        logger.exception("Failed to send error message to user")
    if should_alert(context.error):
        try:
            await post_notification(context.bot,
                                    format_error_alert(update, context.error))
        except Exception:
            logger.exception("Failed to alert admins about an error")


# The "/" menu in private chats. Telegram shows nothing unless the bot
# registers it, so self-service features like /me were undiscoverable.
# Members' commands only; admin commands stay out of everyone's menu.
MEMBER_COMMANDS = [
    ("clockin", "Start a shift"),
    ("clockout", "End your shift"),
    ("myshifts", "Recent shifts & month total"),
    ("availability", "Next week's availability"),
    ("editshift", "Fix a shift or log a missed one"),
    ("me", "Your status: pause weekly messages, or leave"),
    ("myrate", "Your hourly rate"),
]


async def _post_init(application):
    try:
        await application.bot.set_my_commands(
            [BotCommand(c, d) for c, d in MEMBER_COMMANDS],
            scope=BotCommandScopeAllPrivateChats())
    except Exception:
        logger.exception("Couldn't register the command menu")
    await _start_web(application)


async def _start_web(application):
    """
    PTB post_init hook: bring the Mini App's server up inside the same
    event loop as the poller, so there is still exactly one process and
    one getUpdates loop (a second poller breaks Telegram).
    """
    from core.planning import planning_configured

    if not planning_configured():
        logger.warning(
            "Planning Mini App disabled — WEBAPP_URL not set. "
            "(Env vars only load at startup; set it, then redeploy.)"
        )
        return

    from web.server import start_web_server

    application.bot_data["web_runner"] = await start_web_server(application)


async def _stop_web(application):
    """Release the port on shutdown so a restart can rebind it."""
    runner = application.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()


def main():
    """Build and run the bot."""
    setup_logging()

    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_stop_web)
        .build()
    )

    # ── Onboarding ──
    app.add_handler(CommandHandler("start", start_handler))

    # ── Shift commands (member) ──
    app.add_handler(CommandHandler("clockin", clockin_handler))
    app.add_handler(CommandHandler("clockout", clockout_handler))
    app.add_handler(CommandHandler("confirmshift", confirmshift_handler))
    app.add_handler(CommandHandler("myshifts", myshifts_handler))
    app.add_handler(CommandHandler("myrate", myrate_handler))

    # ── Edit shift conversation ──
    # ConversationHandler must be added before generic CallbackQueryHandlers
    # so its state-specific callbacks take priority.
    app.add_handler(build_edit_conversation_handler())

    # ── Edit approval callbacks (admin) ──
    app.add_handler(
        CallbackQueryHandler(edit_approve_callback, pattern=r"^edit_approve:")
    )
    app.add_handler(
        CallbackQueryHandler(edit_reject_callback, pattern=r"^edit_reject:")
    )
    # Edit-flow buttons tapped after the flow ended (or a restart lost it).
    app.add_handler(
        CallbackQueryHandler(expired_edit_callback, pattern=r"^edit(:|_select:)")
    )

    # ── Availability (member) ──
    app.add_handler(CommandHandler("availability", availability_command_handler))
    app.add_handler(
        CallbackQueryHandler(availability_callback, pattern=r"^avail:")
    )
    # Saturday per-day part-timer pickers (admin).
    app.add_handler(
        CallbackQueryHandler(part_timer_callback, pattern=r"^ptpick:")
    )

    # Clock-out message: part-timer "worked through lunch" toggle.
    app.add_handler(
        CallbackQueryHandler(lunch_callback, pattern=r"^lunch:(on|off):")
    )

    # ── Design scheduling (designers) ──
    app.add_handler(CommandHandler("extend", extend_handler))
    app.add_handler(CommandHandler("space", space_handler))
    # One handler for all three switch-reminder buttons. 'extend:' also
    # covers the +30 min buttons on reminders sent before 2026-08-25 —
    # they carry their own minutes, so old messages keep working.
    app.add_handler(
        CallbackQueryHandler(adjust_callback, pattern=r"^(extend|shrink|space):")
    )
    # Submitted plans come back through the Mini App's own authenticated
    # POST (web/server.py), not as a Telegram update — an inline-launched
    # Web App has no sendData().
    app.add_handler(CommandHandler("plan", plan_handler))
    app.add_handler(CommandHandler("day", day_handler))

    # ── Admin commands ──
    app.add_handler(CommandHandler("confirmweek", confirmweek_handler))
    app.add_handler(CommandHandler("payroll", payroll_handler))
    app.add_handler(CommandHandler("lockmonth", lockmonth_handler))
    app.add_handler(CommandHandler("setrate", setrate_handler))
    app.add_handler(CommandHandler("chatid", chatid_handler))
    app.add_handler(CommandHandler("snapshot", snapshot_handler))

    # ── Month-end payroll buttons ──
    # Patterns are disjoint (the trailing ':' keeps 'paylock:' from
    # matching 'paylockyes:'), so registration order doesn't matter.
    app.add_handler(
        CallbackQueryHandler(payroll_run_callback, pattern=r"^payrun:")
    )
    app.add_handler(
        CallbackQueryHandler(paylock_confirm_callback, pattern=r"^paylockyes:")
    )
    app.add_handler(
        CallbackQueryHandler(paylock_cancel_callback, pattern=r"^paylockno$")
    )
    app.add_handler(
        CallbackQueryHandler(paylock_callback, pattern=r"^paylock:")
    )

    # ── Group membership events (join/leave alerts to admins) ──
    # Requires allowed_updates to include CHAT_MEMBER (Update.ALL_TYPES does).
    app.add_handler(
        ChatMemberHandler(group_membership_handler, ChatMemberHandler.CHAT_MEMBER)
    )

    # ── Inert buttons on notifications-group samples ──
    app.add_handler(CallbackQueryHandler(sample_callback, pattern=r"^sample$"))

    # ── Member self-service: /me, pause/resume, leave, rejoin ──
    app.add_handler(CommandHandler("me", me_handler))
    app.add_handler(
        CallbackQueryHandler(involvement_callback, pattern=r"^invl:")
    )
    # The paused part-timer's reply keyboard (leave / restart) sends text.
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Text([LEAVE_LABEL, RESUME_LABEL]),
        paused_keyboard_handler))
    # Delete the group's "Kii-bot removed X" notice (silent leave).
    # Service messages reach bots regardless of privacy mode.
    app.add_handler(
        MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, hide_removal_notice)
    )

    # ── Error handler ──
    app.add_error_handler(error_handler)

    # ── Scheduled jobs ──
    register_jobs(app.job_queue)

    # ── Start ──
    logger.info("Kii-bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
