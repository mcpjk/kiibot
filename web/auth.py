"""
Telegram Mini App initData validation.

The page runs in the user's browser, so nothing it claims about who is
using it can be trusted. Telegram signs the launch parameters with the
bot token; verifying that signature is the ONLY thing that establishes
which designer is submitting a plan. Never read the Telegram user ID
from a request body.

Algorithm (Telegram's spec):
    secret  = HMAC_SHA256(key="WebAppData", msg=<bot token>)
    check   = "\\n".join(sorted "key=value" pairs, excluding hash)
    valid   = HMAC_SHA256(key=secret, msg=check) == hash
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)

# Reject launches older than this. Telegram's initData is long-lived,
# so without a freshness check a leaked URL stays usable indefinitely.
MAX_AUTH_AGE_SECONDS = 24 * 60 * 60


def validate_init_data(init_data: str, bot_token: str,
                       max_age_seconds: int = MAX_AUTH_AGE_SECONDS,
                       clock: Optional[float] = None) -> Optional[dict]:
    """
    Verify a Mini App's initData and return the Telegram user dict, or
    None if the signature is absent, wrong, or stale.
    """
    # Every rejection below logs its specific reason. A bare "unauthorized"
    # with nothing in the logs is undiagnosable from the outside — this
    # was discovered the hard way (2026-08-25): none of empty/missing/
    # wrong-signature/stale look any different from the client's error
    # message, so a real failure and a browser-not-Telegram open were
    # indistinguishable without this. Never log the raw initData, hash,
    # or token — length/presence is enough to diagnose from.
    if not init_data:
        logger.info("Mini App initData rejected: empty (opened outside "
                    "Telegram, or the launch button isn't a Web App button)")
        return None
    if not bot_token:
        logger.warning("Mini App initData rejected: no bot token configured")
        return None

    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        logger.info("Mini App initData rejected: unparseable (%d chars)",
                    len(init_data))
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        logger.info("Mini App initData rejected: no hash field present")
        return None

    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    # compare_digest, not ==: signature comparison must not leak timing.
    if not hmac.compare_digest(expected, received_hash):
        logger.info(
            "Mini App initData rejected: signature mismatch (%d fields). "
            "Usually a stale WEBAPP_URL/bot-token mismatch, or the launch "
            "URL was opened directly rather than via the Web App button.",
            len(pairs),
        )
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        logger.info("Mini App initData rejected: auth_date not an integer")
        return None
    seconds_old = (clock if clock is not None else time.time()) - auth_date
    if seconds_old > max_age_seconds:
        logger.info("Mini App initData rejected: %.0f s old", seconds_old)
        return None

    try:
        user = json.loads(pairs.get("user", "null"))
    except json.JSONDecodeError:
        logger.info("Mini App initData rejected: user field not valid JSON")
        return None
    if not isinstance(user, dict) or "id" not in user:
        logger.info("Mini App initData rejected: user field missing id")
        return None
    return user
