"""
Scheduled jobs for kii-bot.

These run on a timer via python-telegram-bot's JobQueue.
All times are in Asia/Singapore (UTC+8).

Jobs:
- end_of_day_sweep: daily 20:00 — prompts open shifts
- auto_close_sweep: daily 21:00 — closes unresponded shifts
- availability_prompt: Thursday 22:00 — asks for next week's availability
- availability_reminder: Friday 22:00 — reminds those who haven't responded
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo
from telegram.ext import ContextTypes

from core.shifts import get_open_shifts_for_sweep, auto_close_shift
from core.availability import (
    get_next_week_dates,
    get_members_needing_prompt,
)
from interfaces.telegram.availability_handlers import send_availability_prompt
import config

TZ = ZoneInfo(config.TIMEZONE)


# ──────────────────────────────────────────────
# End-of-day sweep (daily 20:00)
# ──────────────────────────────────────────────

async def end_of_day_sweep(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs at 20:00 daily.
    Finds all open shifts and messages each member:
    'Still working? /confirmshift or /clockout'

    Stores the list of warned shift IDs in bot_data so the
    auto_close_sweep (21:00) knows which shifts to close.
    """
    open_shifts = get_open_shifts_for_sweep()

    if not open_shifts:
        return

    # Track which shifts were warned, so auto_close knows what to close
    warned = {}

    for entry in open_shifts:
        tg_id = entry["telegram_id"]
        start = entry["start_time"]
        shift_id = entry["shift"]["id"]

        if not tg_id:
            continue

        msg = (
            f"🕗 You're still clocked in since {_format_time(start)}.\n\n"
            f"Still working? Reply /confirmshift to stay clocked in, "
            f"or /clockout to end your shift.\n\n"
            f"If no response in 1 hour, your shift will be auto-closed."
        )

        try:
            await context.bot.send_message(chat_id=tg_id, text=msg)
            warned[shift_id] = {
                "telegram_id": tg_id,
                "member_name": entry["member_name"],
                "prompt_time": datetime.now(TZ),
            }
        except Exception:
            pass

    # Store warned shifts for the auto-close job
    context.bot_data["warned_shifts"] = warned


# ──────────────────────────────────────────────
# Auto-close sweep (daily 21:00)
# ──────────────────────────────────────────────

async def auto_close_sweep(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs at 21:00 daily (1 hour after the end-of-day prompt).

    Checks all shifts that were warned at 20:00.
    If they're still open (member didn't /clockout or /confirmshift),
    auto-close them with end time set to the 20:00 prompt time.
    """
    warned = context.bot_data.get("warned_shifts", {})

    if not warned:
        return

    from core import airtable_client as at

    for shift_id, info in warned.items():
        # Check if the shift is still open
        shifts_table = at._table(config.TABLE_SHIFTS)
        try:
            shift = shifts_table.get(shift_id)
        except Exception:
            continue

        if shift["fields"].get("Status") != "Open":
            # Member already clocked out or confirmed — skip
            continue

        # Auto-close at the prompt time (20:00), not now (21:00)
        prompt_time = info["prompt_time"]
        auto_close_shift(shift_id, prompt_time)

        # Notify the member
        tg_id = info["telegram_id"]
        if tg_id:
            msg = (
                f"🔶 Your shift was auto-closed at {prompt_time.strftime('%H:%M')}.\n"
                f"If your actual end time was different, use /editshift to correct it."
            )
            try:
                await context.bot.send_message(chat_id=tg_id, text=msg)
            except Exception:
                pass

    # Clear the warned list
    context.bot_data["warned_shifts"] = {}


# ──────────────────────────────────────────────
# Availability prompt (Thursday 22:00)
# ──────────────────────────────────────────────

async def availability_prompt_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs Thursday 22:00.
    Prompts all active members who haven't submitted availability
    for the following week.
    """
    dates = get_next_week_dates()
    week_starting = dates[0].isoformat()

    members = get_members_needing_prompt(week_starting)

    for member_info in members:
        tg_id = member_info["telegram_id"]
        if not tg_id:
            continue

        try:
            await send_availability_prompt(
                bot=context.bot,
                telegram_id=tg_id,
                dates=dates,
                is_reminder=False,
            )
        except Exception:
            pass


# ──────────────────────────────────────────────
# Availability reminder (Friday 22:00)
# ──────────────────────────────────────────────

async def availability_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs Friday 22:00.
    Reminds members who still haven't submitted availability.
    """
    dates = get_next_week_dates()
    week_starting = dates[0].isoformat()

    members = get_members_needing_prompt(week_starting)

    for member_info in members:
        tg_id = member_info["telegram_id"]
        if not tg_id:
            continue

        try:
            await send_availability_prompt(
                bot=context.bot,
                telegram_id=tg_id,
                dates=dates,
                is_reminder=True,
            )
        except Exception:
            pass


# ──────────────────────────────────────────────
# Register all jobs
# ──────────────────────────────────────────────

def register_jobs(job_queue):
    """
    Register all scheduled jobs with python-telegram-bot's JobQueue.
    Call this from main.py after building the Application.

    python-telegram-bot's job_queue.run_daily() takes a time object
    and an optional day_of_week parameter.
    """
    # End-of-day sweep — every day at 20:00 SGT
    job_queue.run_daily(
        end_of_day_sweep,
        time=time(
            hour=config.END_OF_DAY_HOUR,
            minute=config.END_OF_DAY_MINUTE,
            tzinfo=TZ,
        ),
        name="end_of_day_sweep",
    )

    # Auto-close sweep — every day at 21:00 SGT (1h after prompt)
    auto_close_hour = config.END_OF_DAY_HOUR
    auto_close_minute = config.END_OF_DAY_MINUTE + config.AUTO_CLOSE_DELAY_MINUTES
    # Handle minute overflow
    auto_close_hour += auto_close_minute // 60
    auto_close_minute = auto_close_minute % 60

    job_queue.run_daily(
        auto_close_sweep,
        time=time(
            hour=auto_close_hour,
            minute=auto_close_minute,
            tzinfo=TZ,
        ),
        name="auto_close_sweep",
    )

    # Availability prompt — Thursday 22:00
    job_queue.run_daily(
        availability_prompt_job,
        time=time(
            hour=config.AVAILABILITY_PROMPT_HOUR,
            minute=config.AVAILABILITY_PROMPT_MINUTE,
            tzinfo=TZ,
        ),
        days=(config.AVAILABILITY_PROMPT_DAY,),
        name="availability_prompt",
    )

    # Availability reminder — Friday 22:00
    job_queue.run_daily(
        availability_reminder_job,
        time=time(
            hour=config.AVAILABILITY_REMINDER_HOUR,
            minute=config.AVAILABILITY_REMINDER_MINUTE,
            tzinfo=TZ,
        ),
        days=(config.AVAILABILITY_REMINDER_DAY,),
        name="availability_reminder",
    )


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _format_time(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return str(iso_str)
