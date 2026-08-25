"""
The Mini App's web server.

Runs in the same process and event loop as the polling bot, started
from PTB's post_init hook, so there is still exactly one process and
one poller (CLAUDE.md: a second poller breaks Telegram).

It serves three things and deliberately nothing more:
  GET  /plan          the page itself
  POST /api/projects  the plannable list, for a verified designer
  POST /api/plan      write the submitted plan, for a verified designer

Both POST routes authenticate the SAME way: by verifying Telegram's
initData HMAC against the bot token. That signature is the only thing
establishing which designer is calling; never read a Telegram user ID
from a request body.

Submissions come through here rather than Telegram.WebApp.sendData()
because sendData is only available to Mini Apps launched from a
reply-keyboard button, and those launches carry no initData at all —
so the page could never have been authorised to load in the first
place. See interfaces/telegram/planning_handlers.py.

Every Airtable call below runs in a worker thread. This server shares
its event loop with the bot's long polling, so a blocking HTTP call
here would stall the bot itself — submit_plan in particular makes one
API call per block.
"""

import asyncio
import json
import logging
from pathlib import Path

import config
from core import airtable_client as at
from core.planning import (
    BLOCK_TYPES,
    PlanningError,
    build_project_options,
    default_minutes,
    format_plan,
    submit_plan,
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
        member = await asyncio.to_thread(at.get_member_by_telegram_id, user["id"])
        if not member:
            return web.json_response(
                {"error": "You're not registered. Send /start to the bot first."},
                status=403,
            )
        projects = build_project_options(
            await asyncio.to_thread(at.get_plannable_projects)
        )
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


async def _health(_request):
    from aiohttp import web

    return web.json_response({"ok": True})


async def _submit(request):
    """Write a submitted plan for whoever Telegram says is submitting."""
    from aiohttp import web

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "bad request"}, status=400)

    user = validate_init_data(body.get("initData", ""), config.TELEGRAM_BOT_TOKEN)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        capacity = float(body["capacity_hours"])
        selections = body["blocks"]
    except (KeyError, TypeError, ValueError):
        return web.json_response({"error": "That plan didn't come through "
                                           "cleanly. Try again."}, status=400)

    try:
        plan = await asyncio.to_thread(submit_plan, user["id"], capacity, selections)
    except PlanningError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        logger.exception("Mini App: failed to write planned blocks")
        # Show the real reason on the phone, not just in the logs. A
        # generic message here cost a full debugging round trip on
        # 2026-08-25, when the actual error named the exact renamed
        # Airtable field. Truncated: Airtable errors can be long.
        detail = f"{type(e).__name__}: {e}".replace("\n", " ")[:300]
        return web.json_response(
            {"error": "Couldn't save that plan — some blocks may not have "
                      "been written. Check Airtable before retrying.\n\n"
                      + detail},
            status=502,
        )

    summary = format_plan(plan)
    # Also drop the plan into the chat: it's the day's reference, and the
    # Mini App closes straight after submitting.
    bot = request.app.get("bot")
    if bot:
        try:
            await bot.send_message(chat_id=user["id"], text=summary)
        except Exception:
            logger.exception("Mini App: plan saved but the DM failed")

    return web.json_response({"summary": summary, "blocks": len(plan["blocks"])})


def build_app(bot=None):
    from aiohttp import web

    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/plan", _serve_page)
    app.router.add_post("/api/projects", _projects)
    app.router.add_post("/api/plan", _submit)
    app.router.add_get("/health", _health)
    return app


async def start_web_server(application=None):
    """PTB post_init hook: bring the web server up beside the poller."""
    from aiohttp import web

    bot = getattr(application, "bot", None)
    runner = web.AppRunner(build_app(bot))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.WEB_PORT)
    await site.start()
    logger.info("Mini App server listening on port %d (public URL: %s)",
                config.WEB_PORT, config.WEBAPP_URL or "NOT SET")
    return runner
