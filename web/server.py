"""
The Mini App's web server.

Runs in the same process and event loop as the polling bot, started
from PTB's post_init hook, so there is still exactly one process and
one poller (CLAUDE.md: a second poller breaks Telegram).

It serves the morning planner and the day editor, and nothing else:
  GET  /plan            the planner page
  POST /api/projects    the plannable list, for a verified designer
  POST /api/plan        write the submitted plan, for a verified designer
  GET  /day             the day-editor page (DESIGN_SCHEDULING.md §13)
  POST /api/day         today's blocks, for a verified designer
  POST /api/day/apply   run one editor op — PURE, no Airtable at all
  POST /api/day/save    write an edited day

Every POST route authenticates the SAME way: by verifying Telegram's
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
from core.day import (
    SWITCH_DEFAULT_MINUTES,
    DayError,
    apply_op,
    format_day,
    load_day,
    parse_day,
    save_day,
)
from core.timeutils import now
from web.auth import validate_init_data

logger = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
PAGE_PATH = STATIC / "plan.html"
DAY_PAGE_PATH = STATIC / "day.html"


def _sgt_minutes(moment) -> int:
    """Minutes past midnight, Singapore time."""
    return moment.hour * 60 + moment.minute


def _page(path: Path, label: str):
    """Serve one static page. The shell is public; the DATA behind it
    is not — but don't let a proxy keep a stale copy of the shell."""
    async def handler(_request):
        from aiohttp import web

        try:
            html = path.read_text(encoding="utf-8")
        except OSError:
            logger.exception("Mini App page missing at %s", path)
            return web.Response(status=500, text=f"{label} unavailable")
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-store"})
    return handler


async def _authed(request):
    """
    Parse a POST body and verify who is asking.

    Returns (body, user) or (error_response, None). The initData HMAC is
    the ONLY identity check — never read a Telegram user ID from a body.
    """
    from aiohttp import web

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "bad request"}, status=400), None

    user = validate_init_data(body.get("initData", ""), config.TELEGRAM_BOT_TOKEN)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401), None
    return body, user


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
        # Everything the page needs to preview each block's start/end
        # live, as minutes past SGT midnight — a scalar the phone can do
        # arithmetic on without knowing anything about time zones (its
        # own clock may be set to anywhere). The page advances
        # nowMinutes with its own elapsed time and re-rounds to the
        # grid, which is what submit_plan will do again server-side.
        "nowMinutes": _sgt_minutes(now()),
        "lunchStartMinutes": config.LUNCH_START_HOUR * 60,
        "lunchEndMinutes": config.LUNCH_END_HOUR * 60,
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


# ──────────────────────────────────────────────
# The day editor (DESIGN_SCHEDULING.md §13)
# ──────────────────────────────────────────────

async def _day(request):
    """Load today's blocks for whoever Telegram says is asking."""
    from aiohttp import web

    body, user = await _authed(request)
    if user is None:
        return body

    try:
        state = await asyncio.to_thread(load_day, user["id"])
    except DayError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception:
        logger.exception("Day editor: failed to load the day")
        return web.json_response(
            {"error": "Couldn't reach Airtable. Try again in a moment."},
            status=502,
        )

    return web.json_response({
        "rows": state["rows"],
        "date": state["date"],
        "dayStatus": state["day_status"],
        "name": state["member"]["fields"].get("Name", ""),
        "projects": await asyncio.to_thread(_project_options),
        "blockTypes": BLOCK_TYPES,
        # Minutes past SGT midnight, as the planner sends: the page does
        # integer arithmetic and never touches a time zone.
        "nowMinutes": _sgt_minutes(now()),
        "lunchStartMinutes": config.LUNCH_START_HOUR * 60,
        "lunchEndMinutes": config.LUNCH_END_HOUR * 60,
        "gridMinutes": config.PLAN_GRID_MINUTES,
        "minMinutes": config.PLAN_MIN_BLOCK_MINUTES,
        "switchMinutes": SWITCH_DEFAULT_MINUTES,
    }, headers={"Cache-Control": "no-store"})


def _project_options():
    return build_project_options(at.get_plannable_projects())


async def _day_apply(request):
    """
    Run ONE editor op over the day the page is holding.

    Pure: no Airtable, no server-side session state (see core/day.py for
    why the rules live here rather than in the page's JavaScript). A
    restart mid-edit costs nothing, because there is nothing to lose.
    """
    from aiohttp import web

    body, user = await _authed(request)
    if user is None:
        return body

    try:
        day = apply_op(parse_day(body.get("day")), body.get("op") or {})
    except DayError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception:
        logger.exception("Day editor: op failed")
        return web.json_response({"error": "That didn't work. Reopen the "
                                           "editor."}, status=500)
    return web.json_response({"rows": day})


async def _day_save(request):
    """Write an edited day, optionally confirming it."""
    from aiohttp import web

    body, user = await _authed(request)
    if user is None:
        return body

    confirm = bool(body.get("confirm"))
    try:
        rows = parse_day(body.get("day"))
        date = str(body.get("date") or "")
        result = await asyncio.to_thread(save_day, user["id"], date, rows, confirm)
    except DayError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        logger.exception("Day editor: failed to save the day")
        # Show the real reason on the phone, not just in the logs — the
        # same lesson as the planner's submit path (2026-08-25).
        detail = f"{type(e).__name__}: {e}".replace("\n", " ")[:300]
        return web.json_response(
            {"error": "Couldn't save that day — some blocks may not have "
                      "been written. Check Airtable before retrying.\n\n"
                      + detail},
            status=502,
        )

    summary = format_day(result["rows"], result["confirmed"])
    bot = request.app.get("bot")
    if bot:
        try:
            await bot.send_message(chat_id=user["id"], text=summary)
        except Exception:
            logger.exception("Day editor: day saved but the DM failed")

    return web.json_response({"summary": summary, "updated": result["updated"],
                              "created": result["created"],
                              "confirmed": result["confirmed"]})


def build_app(bot=None):
    from aiohttp import web

    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/plan", _page(PAGE_PATH, "Planning page"))
    app.router.add_post("/api/projects", _projects)
    app.router.add_post("/api/plan", _submit)
    app.router.add_get("/day", _page(DAY_PAGE_PATH, "Day editor"))
    app.router.add_post("/api/day", _day)
    app.router.add_post("/api/day/apply", _day_apply)
    app.router.add_post("/api/day/save", _day_save)
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
