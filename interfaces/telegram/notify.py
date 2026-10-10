"""Best-effort DMs to every admin."""

import logging
from typing import Optional

from core import airtable_client as at

logger = logging.getLogger(__name__)


async def notify_admins(bot, text: str, reply_markup=None,
                        exclude_telegram_id: Optional[int] = None) -> int:
    """
    DM every admin (except `exclude_telegram_id`, usually whoever caused
    the event); returns how many were reached. A failed DM is logged and
    skipped — it must never fail the action being reported.
    """
    try:
        admins = at.get_admin_members()
    except Exception:
        logger.exception("Couldn't load admins to notify: %s", text)
        return 0

    notified = 0
    for admin in admins:
        tg_id = admin["fields"].get("Telegram user ID")
        if not tg_id or int(tg_id) == exclude_telegram_id:
            continue
        try:
            await bot.send_message(chat_id=tg_id, text=text,
                                   reply_markup=reply_markup)
            notified += 1
        except Exception:
            # Admin may not have started the bot yet
            logger.exception("Failed to notify admin %s", admin["fields"].get("Name"))
    return notified
