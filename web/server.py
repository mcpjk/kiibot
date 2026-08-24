"""
The Mini App's web server.

Runs in the same process and event loop as the polling bot, started
from PTB's post_init hook, so there is still exactly one process and
one poller (CLAUDE.md: a second poller breaks Telegram).

It serves TWO things and deliberately nothing more:
  GET  /plan          the page itself
  POST /api/projects  the plannable list, for a verified designer

Writes do NOT go through here. The page submits with
Telegram.WebApp.sendData(), which arrives as an ordinary bot update
through long polling — so the write path keeps Telegram's own
authentication and needs no API surface of its own.
"""

import json
import logging
from pathlib import Path

import config
from core import airtable_client as at
from core.planning import (
    BLOCK_TYPES,
    build_project_options,
    default_minutes,
)
from web.auth import validate_init_data

logger = logging.getLogger(__name__)

PAGE_PATH = Path(__file__).parent / "static" / "plan.html"


async def _serve_page(request):
    from aiohttp import web

    try:
        html = PAGE_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.exception("Mini App page missing at %s", PAGE_PATH)
        return web.Response(status=500, text="Planning page unavailable")
    return web.Response(
        text=html, content_type="text/html",
        # The page is public; the DATA behind it is not. Don't let a
        # proxy keep a stale copy of the shell.
        headers={"Cache-Control": "no-store"},
    )


async def _projects(request):
    """Return the plannable list for whoever Telegram says is asking."""
    from aiohttp import web

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "bad request"}, status=400)

    user = validate_init_data(body.get("initData", ""), config.TELEGRAM_BOT_TOKEN)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)

    # One guard over every Airtable call: an unhandled exception here
    # would return an HTML 500, which the page can't parse into an
    # error message — the designer would just see a JSON parse failure.
    try:
        member = at.get_member_by_telegram_id(user["id"])
        if not member:
            return web.json_response(
                {"error": "You're not registered. Send /start to the bot first."},
                status=403,
            )
        projects = build_project_options(at.get_plannable_projects())
    except Exception:
        logger.exception("Mini App: failed to load plannable projects")
        return web.json_response(
            {"error": "Couldn't reach Airtable. Try again in a moment."},
            status=502,
        )

    return web.json_response({
        "projects": projects,
        "blockTypes": BLOCK_TYPES,
        "defaultCapacityHours": config.PLAN_DEFAULT_CAPACITY_HOURS,
        "maxCapacityHours": config.PLAN_MAX_CAPACITY_HOURS,
        "gridMinutes": config.PLAN_GRID_MINUTES,
        "minMinutes": config.PLAN_MIN_BLOCK_MINUTES,
        # Sent so the page and the bot agree on the split rule without
        # the arithmetic living in two places conceptually.
        "defaultMinutesFor": {
            str(n): default_minutes(config.PLAN_DEFAULT_CAPACITY_HOURS, n)
            for n in range(1, 13)
        },
        "name": member["fields"].get("Name", ""),
    }, headers={"Cache-Control": "no-store"})


def build_app():
    from aiohttp import web

    app = web.Application()
    app.router.add_get("/plan", _serve_page)
    app.router.add_post("/api/projects", _projects)
    app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
    return app


async def start_web_server(_application=None):
    """PTB post_init hook: bring the web server up beside the poller."""
    from aiohttp import web

    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.WEB_PORT)
    await site.start()
    logger.info("Mini App server listening on port %d (public URL: %s)",
                config.WEB_PORT, config.WEBAPP_URL or "NOT SET")
    return runner
