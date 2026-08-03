"""
Scheduled jobs for kii-bot.

These run on a timer via python-telegram-bot's JobQueue.
All times are in Asia/Singapore (UTC+8).

Jobs:
- end_of_day_sweep: daily 20:00 — prompts open shifts, writes 'Prompted at'
- auto_close_sweep: daily 21:00 — closes prompted-but-unconfirmed shifts
- availability_prompt: Thursday 22:00 — asks for next week's availability
- availability_reminder: Friday 22:00 — reminds those who haven't responded
- availability_digest: Saturday 09:00 — tells admins who has/hasn't submitted

All jobs are STATELESS — they derive everything from Airtable, so a bot
restart at any point loses nothing.
"""

import logging
from datetime import time

from telegram.ext import ContextTypes

from core.shifts import (
    get_open_shifts_for_sweep,
    get_shifts_to_autoclose,
    mark_shift_prompted,
    auto_close_shift,
)
from core.availability import (
    get_next_week_dates,
    get_members_needing_prompt,
    get_submission_status,
)
from core import airtable_client as at
from core.timeutils import TZ, now, fmt_time, parse_dt
from interfaces.telegram.availability_handlers import send_availability_prompt
import config

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# End-of-day sweep (daily 20:00)
# ──────────────────────────────────────────────

async def end_of_day_sweep(context: ContextTypes.DEFAULT_TYPE):
    """
    Finds all open shifts, messages each member ('still working?'),
    and stamps 'Prompted at' on the shift so the auto-close sweep
    knows which shifts were warned — even across a restart.
    """
    open_shifts = get_open_shifts_for_sweep()
    logger.info("End-of-day sweep: %d open shift(s)", len(open_shifts))

    for entry in open_shifts:
        tg_id = entry["telegram_id"]
        shift_id = entry["shift"]["id"]

        if not tg_id:
            logger.warning("Open shift %s: member %s has no Telegram ID",
                           shift_id, entry["member_name"])
            continue

        msg = (
            f"🕗 You're still clocked in since {fmt_time(entry['start_time'])}.\n\n"
            f"Still working? Reply /confirmshift to stay clocked in, "
            f"or /clockout to end your shift.\n\n"
            f"If no response in 1 hour, your shift will be auto-closed."
        )

        try:
            await context.bot.send_message(chat_id=tg_id, text=msg)
        except Exception:
            logger.exception("Failed to send end-of-day prompt to %s (%s)",
                             entry["member_name"], tg_id)
            continue  # don't mark prompted if they never got the message

        try:
            mark_shift_prompted(shift_id, now())
        except Exception:
            logger.exception("Failed to mark shift %s as prompted", shift_id)


# ──────────────────────────────────────────────
# Auto-close sweep (daily 21:00)
# ──────────────────────────────────────────────

async def auto_close_sweep(context: ContextTypes.DEFAULT_TYPE):
    """
    Closes open shifts that were prompted and not confirmed afterwards.
    End time is set to the prompt time (20:00), not the sweep time, so
    unresponsive members aren't credited the extra hour. Members can
    correct genuine cases via /editshift.
    """
    to_close = get_shifts_to_autoclose()
    logger.info("Auto-close sweep: %d shift(s) to close", len(to_close))

    for entry in to_close:
        shift_id = entry["shift"]["id"]
        prompt_time = entry["prompt_time"]

        try:
            auto_close_shift(shift_id, prompt_time)
        except Exception:
            logger.exception("Failed to auto-close shift %s", shift_id)
            continue

        tg_id = entry["telegram_id"]
        if tg_id:
            msg = (
                f"🔶 Your shift was auto-closed at {prompt_time.strftime('%H:%M')}.\n"
                f"If your actual end time was different, use /editshift to correct it."
            )
            try:
                await context.bot.send_message(chat_id=tg_id, text=msg)
            except Exception:
                logger.exception("Failed to notify %s of auto-close",
                                 entry["member_name"])


# ──────────────────────────────────────────────
# Availability prompt / reminder (Thu / Fri 22:00)
# ──────────────────────────────────────────────

async def _send_availability_prompts(context, is_reminder: bool):
    dates = get_next_week_dates()
    week_starting = dates[0].isoformat()

    members = get_members_needing_prompt(week_starting)
    logger.info("Availability %s: %d member(s) to prompt",
                "reminder" if is_reminder else "prompt", len(members))

    for member_info in members:
        tg_id = member_info["telegram_id"]
        if not tg_id:
            logger.warning("Member %s has no Telegram ID", member_info["name"])
            continue

        try:
            await send_availability_prompt(
                bot=context.bot,
                telegram_id=tg_id,
                dates=dates,
                is_reminder=is_reminder,
            )
        except Exception:
            logger.exception("Failed to send availability prompt to %s",
                             member_info["name"])


async def availability_prompt_job(context: ContextTypes.DEFAULT_TYPE):
    """Thursday 22:00 — first ask."""
    await _send_availability_prompts(context, is_reminder=False)


async def availability_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Friday 22:00 — reminder for non-submitters."""
    await _send_availability_prompts(context, is_reminder=True)


# ──────────────────────────────────────────────
# Availability digest to admins (Saturday 09:00)
# ──────────────────────────────────────────────

async def availability_digest_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Saturday morning: tell admins who has and hasn't submitted
    availability for next week, so they can chase or plan around gaps
    before confirming the schedule.
    """
    dates = get_next_week_dates()
    week_starting = dates[0].isoformat()

    status = get_submission_status(week_starting)
    submitted = sorted(m["name"] for m in status["submitted"])
    missing = sorted(m["name"] for m in status["missing"])

    lines = [f"📋 Availability for week of {dates[0].strftime('%d %b')}:"]
    lines.append(
        f"Submitted ({len(submitted)}): {', '.join(submitted) if submitted else '—'}"
    )
    lines.append(
        f"Missing ({len(missing)}): {', '.join(missing) if missing else '— everyone responded 🎉'}"
    )
    lines.append(
        "\nReview and tick Confirmed in Airtable, then run /confirmweek."
    )
    msg = "\n".join(lines)

    for admin in at.get_admin_members():
        tg_id = admin["fields"].get("Telegram user ID")
        if not tg_id:
            continue
        try:
            await context.bot.send_message(chat_id=tg_id, text=msg)
        except Exception:
            logger.exception("Failed to send availability digest to admin %s",
                             admin["fields"].get("Name"))


# ──────────────────────────────────────────────
# Design-block switch reminders (poll every 2 min)
# ──────────────────────────────────────────────

def format_switch_ping(block_fields: dict, project_name: str) -> str:
    """Build the switch-reminder DM text from a Design Block's fields."""
    start = parse_dt(block_fields.get("Start"))
    end = parse_dt(block_fields.get("End"))
    when = start.strftime("%H:%M") if start else "soon"
    block_type = block_fields.get("Block type") or "Work"
    if start and end:
        hours = (end - start).total_seconds() / 3600
        span = f" ({hours:g} h)"
    else:
        span = ""
    return (
        f"🔔 Up next at {when}: {block_type} — {project_name}{span}\n"
        f"Wrap up your current task and switch over."
    )


async def switch_ping_job(context: ContextTypes.DEFAULT_TYPE):
    """
    DM designers ~SWITCH_PING_LEAD_MINUTES before each planned Design
    Block starts. Stateless: dedupe lives in the block's 'Switch ping
    sent' field, so restarts never double-ping and a downed bot simply
    misses pings (the block itself remains the plan of record).
    """
    window = config.SWITCH_PING_LEAD_MINUTES + (config.SWITCH_PING_POLL_SECONDS // 60) + 1
    try:
        blocks = at.get_design_blocks_needing_ping(window)
    except Exception:
        logger.exception("Switch ping: failed to query design blocks")
        return
    if not blocks:
        return

    members = at.get_all_members_indexed()
    for block in blocks:
        f = block["fields"]
        project_ids = f.get("Project", [])
        project_name = at.get_project_name(project_ids[0]) if project_ids else "(no project)"
        msg = format_switch_ping(f, project_name)

        sent_to_someone = False
        for designer_id in f.get("Designers", []):
            member = members.get(designer_id)
            tg_id = member["fields"].get("Telegram user ID") if member else None
            if not tg_id:
                logger.warning("Switch ping: designer %s on block %s has no Telegram ID",
                               designer_id, block["id"])
                continue
            try:
                await context.bot.send_message(chat_id=tg_id, text=msg)
                sent_to_someone = True
            except Exception:
                logger.exception("Switch ping: failed to DM %s for block %s",
                                 tg_id, block["id"])

        # Only mark pinged if someone actually got it; otherwise retry
        # next cycle until the window closes.
        if sent_to_someone:
            try:
                at.mark_design_block_pinged(block["id"], now().isoformat())
            except Exception:
                logger.exception("Switch ping: failed to mark block %s", block["id"])


# ──────────────────────────────────────────────
# Morning score snapshot (06:05 SGT → Google Sheet)
# ──────────────────────────────────────────────

async def score_snapshot_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Freeze the design-priority ranking for today into the snapshot
    sheet. Runs after the 06:00 Airtable recalc automation so scores
    are fresh. On failure: log + one DM to admins; the day shows as a
    visible gap in the sheet rather than silently wrong data.
    """
    from core.snapshots import build_snapshot_rows, append_snapshot_rows

    try:
        candidates = at.get_design_candidates()
        rows = build_snapshot_rows(candidates, now())
        if not rows:
            logger.info("Score snapshot: no design candidates today")
            return
        append_snapshot_rows(rows)
        logger.info("Score snapshot: wrote %d row(s)", len(rows))
    except Exception:
        logger.exception("Score snapshot failed")
        for admin in at.get_admin_members():
            tg_id = admin["fields"].get("Telegram user ID")
            if not tg_id:
                continue
            try:
                await context.bot.send_message(
                    chat_id=tg_id,
                    text="⚠️ This morning's score snapshot failed — "
                         "today will be a gap in the sheet. Check the logs.",
                )
            except Exception:
                logger.exception("Failed to notify admin of snapshot failure")


# ──────────────────────────────────────────────
# Register all jobs
# ──────────────────────────────────────────────

def register_jobs(job_queue):
    """Register all scheduled jobs. Call from main.py after building the app."""
    job_queue.run_daily(
        end_of_day_sweep,
        time=time(config.END_OF_DAY_HOUR, config.END_OF_DAY_MINUTE, tzinfo=TZ),
        name="end_of_day_sweep",
    )

    # Auto-close sweep — AUTO_CLOSE_DELAY_MINUTES after the prompt
    total_minutes = (
        config.END_OF_DAY_HOUR * 60
        + config.END_OF_DAY_MINUTE
        + config.AUTO_CLOSE_DELAY_MINUTES
    )
    job_queue.run_daily(
        auto_close_sweep,
        time=time((total_minutes // 60) % 24, total_minutes % 60, tzinfo=TZ),
        name="auto_close_sweep",
    )

    job_queue.run_daily(
        availability_prompt_job,
        time=time(config.AVAILABILITY_PROMPT_HOUR,
                  config.AVAILABILITY_PROMPT_MINUTE, tzinfo=TZ),
        days=(config.AVAILABILITY_PROMPT_DAY,),
        name="availability_prompt",
    )

    job_queue.run_daily(
        availability_reminder_job,
        time=time(config.AVAILABILITY_REMINDER_HOUR,
                  config.AVAILABILITY_REMINDER_MINUTE, tzinfo=TZ),
        days=(config.AVAILABILITY_REMINDER_DAY,),
        name="availability_reminder",
    )

    job_queue.run_daily(
        availability_digest_job,
        time=time(config.AVAILABILITY_DIGEST_HOUR,
                  config.AVAILABILITY_DIGEST_MINUTE, tzinfo=TZ),
        days=(config.AVAILABILITY_DIGEST_DAY,),
        name="availability_digest",
    )

    job_queue.run_repeating(
        switch_ping_job,
        interval=config.SWITCH_PING_POLL_SECONDS,
        first=15,
        name="switch_ping",
    )

    from core.snapshots import snapshots_configured
    if snapshots_configured():
        job_queue.run_daily(
            score_snapshot_job,
            time=time(config.SNAPSHOT_HOUR, config.SNAPSHOT_MINUTE, tzinfo=TZ),
            name="score_snapshot",
        )
    else:
        logger.info(
            "Score snapshot disabled: set GOOGLE_SERVICE_ACCOUNT_JSON and "
            "SCORE_SNAPSHOT_SHEET_ID to enable"
        )
