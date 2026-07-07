"""
Kii-bot configuration.

All secrets come from environment variables (via .env file).
Timing and table constants are defined here.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Secrets (from .env) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")  # Personal access token
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")  # e.g. "appXXXXXXXXXXXX"
TELEGRAM_GROUP_CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID")
if TELEGRAM_GROUP_CHAT_ID:
    TELEGRAM_GROUP_CHAT_ID = int(TELEGRAM_GROUP_CHAT_ID)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# --- Airtable table names ---
# These must match exactly what's in your Airtable base.
# If you rename tables in Airtable, update them here.
TABLE_TEAM_MEMBERS = "Team Members"
TABLE_RATE_HISTORY = "Rate History"
TABLE_SHIFTS = "Shifts"
TABLE_SHIFT_EDIT_REQUESTS = "Shift Edit Requests"
TABLE_AVAILABILITY = "Availability"

# --- Timing configuration ---
# All times are in Asia/Singapore timezone (UTC+8).
TIMEZONE = "Asia/Singapore"

# End-of-day prompt: bot asks "still working?" at this hour
END_OF_DAY_HOUR = 20  # 2000hrs
END_OF_DAY_MINUTE = 0

# Auto-close: shifts auto-close this many minutes after the prompt if no response
AUTO_CLOSE_DELAY_MINUTES = 60  # 1 hour after the 2000hrs prompt

# Sanity cap on shift length for edit requests (hours)
MAX_SHIFT_HOURS = 16

# Availability prompt schedule.
# NOTE: python-telegram-bot v20+ run_daily days use 0=Sunday ... 6=Saturday.
AVAILABILITY_PROMPT_DAY = 4      # Thursday
AVAILABILITY_PROMPT_HOUR = 22    # 2200hrs Thursday
AVAILABILITY_PROMPT_MINUTE = 0

AVAILABILITY_REMINDER_DAY = 5    # Friday
AVAILABILITY_REMINDER_HOUR = 22  # 2200hrs Friday
AVAILABILITY_REMINDER_MINUTE = 0

# Saturday-morning digest to admins: who has / hasn't submitted
AVAILABILITY_DIGEST_DAY = 6      # Saturday
AVAILABILITY_DIGEST_HOUR = 9     # 0900hrs Saturday
AVAILABILITY_DIGEST_MINUTE = 0

# --- Validation ---
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not set in environment")
if not AIRTABLE_API_KEY:
    raise ValueError("AIRTABLE_API_KEY not set in environment")
if not AIRTABLE_BASE_ID:
    raise ValueError("AIRTABLE_BASE_ID not set in environment")
