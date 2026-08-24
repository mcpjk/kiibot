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
    if not init_data or not bot_token:
        return None

    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    # compare_digest, not ==: signature comparison must not leak timing.
    if not hmac.compare_digest(expected, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    seconds_old = (clock if clock is not None else time.time()) - auth_date
    if seconds_old > max_age_seconds:
        logger.info("Mini App initData rejected: %.0f s old", seconds_old)
        return None

    try:
        user = json.loads(pairs.get("user", "null"))
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or "id" not in user:
        return None
    return user
