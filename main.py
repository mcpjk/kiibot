"""
Kii-bot main entry point.

Wires together:
- Telegram command handlers (shifts, edits, availability, admin)
- Callback query handlers (day selection, edit approval)
- Scheduled jobs (end-of-day, auto-close, availability cycle)

Run with: python main.py
"""

import logging

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
)

import config
from interfaces.telegram.shift_handlers import (
    start_handler,
    clockin_handler,
    clockout_handler,
    confirmshift_handler,
    myshifts_handler,
    myrate_handler,
)
from interfaces.telegram.edit_handlers import (
    build_edit_conversation_handler,
    edit_approve_callback,
    edit_reject_callback,
)
from interfaces.telegram.availability_handlers import (
    availability_callback,
    availability_command_handler,
    confirmweek_handler,
)
from interfaces.telegram.admin_handlers import (
    payroll_handler,
    lockmonth_handler,
    setrate_handler,
    chatid_handler,
)
from interfaces.telegram.membership_handlers import group_membership_handler
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
    """Central error handler: log the exception, tell the user something broke."""
    logger.exception("Unhandled exception while processing update: %s",
                     update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong on my end. Please try again — "
                "if it keeps happening, tell an admin."
            )
        except Exception:
            logger.exception("Failed to send error message to user")


def main():
    """Build and run the bot."""
    setup_logging()

    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

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

    # ── Availability (member) ──
    app.add_handler(CommandHandler("availability", availability_command_handler))
    app.add_handler(
        CallbackQueryHandler(availability_callback, pattern=r"^avail:")
    )

    # ── Admin commands ──
    app.add_handler(CommandHandler("confirmweek", confirmweek_handler))
    app.add_handler(CommandHandler("payroll", payroll_handler))
    app.add_handler(CommandHandler("lockmonth", lockmonth_handler))
    app.add_handler(CommandHandler("setrate", setrate_handler))
    app.add_handler(CommandHandler("chatid", chatid_handler))

    # ── Group membership events (join/leave alerts to admins) ──
    # Requires allowed_updates to include CHAT_MEMBER (Update.ALL_TYPES does).
    app.add_handler(
        ChatMemberHandler(group_membership_handler, ChatMemberHandler.CHAT_MEMBER)
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
