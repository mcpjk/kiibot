"""
Kii-bot main entry point.

Wires together:
- Telegram command handlers (shifts, edits, availability)
- Callback query handlers (day selection, edit approval)
- Scheduled jobs (end-of-day, auto-close, availability prompts)

Run with: python main.py
"""

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
)

import config
from interfaces.telegram.shift_handlers import (
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
    confirmweek_handler,
)
from jobs.scheduler import register_jobs


def main():
    """Build and run the bot."""

    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

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

    # ── Availability callbacks (member) ──
    app.add_handler(
        CallbackQueryHandler(availability_callback, pattern=r"^avail:")
    )

    # ── Admin commands ──
    app.add_handler(CommandHandler("confirmweek", confirmweek_handler))

    # ── Scheduled jobs ──
    register_jobs(app.job_queue)

    # ── Start ──
    print("Kii-bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
