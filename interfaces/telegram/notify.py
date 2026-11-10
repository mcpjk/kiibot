"""
Best-effort messages to admins, split by whether they need action.

- `notify_admins`: a DM to every admin. For anything an admin must act
  on (Approve/Reject, set up a new member, Saturday pickers) — buttons
  belong in a DM, where only that admin can tap them.
- `post_notification`: one post to the "Kii notifications" group
  (TELEGRAM_NOTIFY_CHAT_ID) for FYIs (clock events, availability
  answers, error alerts). Falls back to `notify_admins` when the group
  isn't configured or the post fails, so an FYI is never silently lost.

- `post_sample`: one copy of a routine member message (availability
  prompt, planning prompt…) for monitoring, buttons made inert.

New admin-facing messages: pick one of the two by that rule. Never put
LIVE buttons in the group — any member of it could tap them.
"""

import logging
from typing import Optional

import config
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


async def post_notification(bot, text: str,
                            exclude_telegram_id: Optional[int] = None) -> None:
    """
    Post an FYI to the notifications group. A group can't hide a post
    from one reader, so `exclude_telegram_id` only applies to the DM
    fallback. No reply_markup on purpose (see module docstring).
    """
    chat_id = config.TELEGRAM_NOTIFY_CHAT_ID
    if chat_id:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return
        except Exception:
            logger.exception("Couldn't post to the notifications group; "
                             "falling back to admin DMs")
    await notify_admins(bot, text, exclude_telegram_id=exclude_telegram_id)


# ──────────────────────────────────────────────
# Samples of routine member messages (monitoring)
# ──────────────────────────────────────────────

# Every mirrored button carries this instead of its real callback, so a
# tap in the group can never act. It matters: availability, lunch and
# ±15 callbacks act on whoever TAPS (from_user), so a live copy tapped
# by an admin would submit the admin's own availability.
SAMPLE_CALLBACK = "sample"


def inert_copy(markup):
    """Same button labels and layout, every button inert. Also needed
    because Telegram refuses Mini App (web_app) buttons outside private
    chats, so a live copy of the planning prompt can't be posted at all."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if markup is None:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b.text, callback_data=SAMPLE_CALLBACK) for b in row]
        for row in markup.inline_keyboard
    ])


def format_sample(what: str, recipients: list[str], text: str,
                  note: Optional[str] = None) -> str:
    names = ", ".join(recipients)
    header = f"👀 {what} → sent to {len(recipients)}: {names}"
    if note:
        header += f"\n{note}"
    return f"{header}\n─────\n{text}"


async def post_sample(bot, what: str, recipients: list[str], text: str,
                      reply_markup=None, note: Optional[str] = None) -> None:
    """
    Post ONE copy of a routine message that just went to `recipients`,
    with its buttons made inert. Group only: with no notifications group
    there's no DM fallback, since samples in DMs are exactly the clutter
    the group exists to remove. Best-effort: never fails the job.
    """
    chat_id = config.TELEGRAM_NOTIFY_CHAT_ID
    if not chat_id or not recipients:
        return
    try:
        await bot.send_message(chat_id=chat_id,
                               text=format_sample(what, recipients, text, note),
                               reply_markup=inert_copy(reply_markup))
    except Exception:
        logger.exception("Couldn't post a %s sample to the notifications group",
                         what)


async def sample_callback(update, context):
    """A tap on a sample's button: say so, do nothing."""
    from interfaces.telegram.callback_utils import safe_answer

    await safe_answer(update.callback_query,
                      "Sample only: this copy's buttons do nothing.")
