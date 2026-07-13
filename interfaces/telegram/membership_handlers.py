"""
Telegram handlers for group-chat membership events.

Complements the weekly audit (core/membership.py): the audit reconciles
Airtable → group on /confirmweek; this handler reacts to join/leave
events in between, so admins hear about strangers joining or members
leaving without waiting for the next audit. Alerts only — removals stay
with the audit / admins.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from core import airtable_client as at
from core.membership import IN_GROUP_STATUSES
import config

logger = logging.getLogger(__name__)


async def group_membership_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """React to someone joining or leaving the team group chat."""
    cmu = update.chat_member
    if not cmu or not config.TELEGRAM_GROUP_CHAT_ID:
        return
    if cmu.chat.id != config.TELEGRAM_GROUP_CHAT_ID:
        return

    user = cmu.new_chat_member.user
    if user.is_bot:
        return
    # Skip membership changes the bot itself performed (audit removals).
    if cmu.from_user and cmu.from_user.id == context.bot.id:
        return

    was_in = cmu.old_chat_member.status in IN_GROUP_STATUSES
    now_in = cmu.new_chat_member.status in IN_GROUP_STATUSES
    if was_in == now_in:
        return  # promotion/restriction change, not a join/leave

    member = at.get_member_by_telegram_id(user.id)
    name = member["fields"].get("Name") if member else None
    label = f"{name or user.full_name} (@{user.username or '—'}, ID {user.id})"

    if now_in:
        status = member["fields"].get("Status") if member else None
        if status == "Active":
            return  # expected join, nothing to report
        detail = f"roster status: {status}" if member else "NOT in the roster"
        text = f"👥 {label} joined the group chat — {detail}."
    else:
        if not member or member["fields"].get("Status") != "Active":
            return  # inactive/unknown person leaving is the desired state
        text = (
            f"👥 {label} left the group chat but is still Active in the "
            f"roster. If they've left the team, flip their Status to Inactive."
        )

    for admin in at.get_admin_members():
        tg_id = admin["fields"].get("Telegram user ID")
        if not tg_id or tg_id == user.id:
            continue
        try:
            await context.bot.send_message(chat_id=tg_id, text=text)
        except Exception:
            logger.exception("Failed to alert admin %s about membership change",
                             admin["fields"].get("Name"))
