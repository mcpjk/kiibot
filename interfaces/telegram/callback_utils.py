"""
Shared helpers for Telegram callback-query handlers.

Telegram invalidates a callback query id seconds after the button tap.
A late answer — duplicate tap, tap on an old keyboard message, or an
update delivered across a restart — raises
BadRequest("Query is too old and response timeout expired or query id
is invalid"). That failure is cosmetic: the tap was real and any work
the handler did (e.g. Airtable writes) succeeded. Letting it bubble up
sends users a scary "Something went wrong" for an operation that
worked (seen live 2026-07-17; users then re-submit).

Invariant 4 (CLAUDE.md) still applies: answer each query at most once,
and an alert must be the first and only answer on its path —
safe_answer changes error handling, not answer semantics.
"""

import logging

from telegram.error import BadRequest

logger = logging.getLogger(__name__)


async def safe_answer(query, *args, **kwargs) -> bool:
    """
    Answer a callback query, treating an expired/invalid query as a
    no-op. Returns True if the answer went through, False if Telegram
    rejected it as stale. Other errors still raise.
    """
    try:
        await query.answer(*args, **kwargs)
        return True
    except BadRequest as e:
        logger.info("Callback answer skipped (stale query): %s", e)
        return False
