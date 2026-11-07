"""
What the central error handler logs and tells admins.

Before Nov 2026 the handler logged the whole Update repr (a screenful per
error) and told nobody; admins learned of failures from members'
screenshots. Now: one searchable summary line in the log, and a short DM
to every admin naming who, what they tapped/typed, and the exception.

The DM is throttled per exception type so an outage (Airtable down,
Telegram flaky) sends one alert, not one per update. The throttle lives
in memory on purpose: losing it on a restart costs at most one extra DM,
and nothing correctness-related depends on it.
"""

import time
from typing import Optional

from telegram import Update

from core.timeutils import now

ALERT_COOLDOWN_SECONDS = 10 * 60
_MAX_DETAIL = 200

_last_alert: dict[str, float] = {}


def describe_update(update: object) -> str:
    """One line: who did what. Never the full repr."""
    if not isinstance(update, Update):
        return "no update (job or polling error)"
    user = update.effective_user
    who = (f"{user.full_name} ({user.id})" if user else "unknown user")
    if update.callback_query:
        what = f"tapped button '{update.callback_query.data}'"
    elif update.effective_message and update.effective_message.text:
        what = f"sent '{update.effective_message.text[:60]}'"
    else:
        what = "sent a non-text update"
    return f"{who} {what}"


def describe_error(error: Optional[BaseException]) -> str:
    if error is None:
        return "unknown error"
    detail = str(error).strip()
    if len(detail) > _MAX_DETAIL:
        detail = detail[:_MAX_DETAIL] + "…"
    name = type(error).__name__
    return f"{name}: {detail}" if detail else name


def format_error_alert(update: object, error: Optional[BaseException]) -> str:
    return (
        f"⚠️ Bot error at {now().strftime('%d %b %H:%M:%S')}\n"
        f"{describe_update(update)}\n"
        f"{describe_error(error)}\n"
        f"They were told to try again. Full traceback is in the host log."
    )


def should_alert(error: Optional[BaseException],
                 clock=time.monotonic) -> bool:
    """True at most once per exception type per cooldown window."""
    key = type(error).__name__ if error is not None else "None"
    t = clock()
    last = _last_alert.get(key)
    if last is not None and t - last < ALERT_COOLDOWN_SECONDS:
        return False
    _last_alert[key] = t
    return True
