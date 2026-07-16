"""
Airtable base setup script for kii-bot shift management.

Creates all 5 tables with their fields. Run this once against
a fresh Airtable base to set up the schema.

Usage:
    python setup_airtable.py

Requires AIRTABLE_API_KEY and AIRTABLE_BASE_ID in .env

HOW THIS WORKS:
- The Airtable Metadata API lets us create tables and add fields
  via HTTP requests, so you don't have to build them by hand.
- Each table is created with its initial fields, then additional
  fields (especially formula fields) are added afterward because
  the Metadata API requires certain field types to be added after
  table creation.
- Linked fields (Link to another record) need the target table to
  exist first, so tables are created in dependency order.

WHAT IT DOESN'T DO:
- Create views (Airtable's API doesn't support view creation yet).
  You'll create the Monthly Payroll Summary view manually.
- Set up automations (e.g., Rate History auto-creation on rate change).
  You'll do this in Airtable's automation tab.
"""

import os
import sys
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("AIRTABLE_API_KEY")
BASE_ID = os.getenv("AIRTABLE_BASE_ID")

if not API_KEY or not BASE_ID:
    print("Error: Set AIRTABLE_API_KEY and AIRTABLE_BASE_ID in .env")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

META_URL = f"https://api.airtable.com/v0/meta/bases/{BASE_ID}/tables"


def api_call(method, url, data=None):
    """Make an API call with rate-limit handling."""
    resp = getattr(requests, method)(url, headers=HEADERS, json=data)

    if resp.status_code == 429:
        # Rate limited — wait and retry
        wait = int(resp.headers.get("Retry-After", 30))
        print(f"  Rate limited, waiting {wait}s...")
        time.sleep(wait)
        resp = getattr(requests, method)(url, headers=HEADERS, json=data)

    if resp.status_code not in (200, 201):
        print(f"  ERROR {resp.status_code}: {resp.text}")
        return None

    return resp.json()


def create_table(name, fields, description=""):
    """
    Create a table with initial fields.
    Returns the table ID, or None on failure.

    'fields' is a list of dicts, each with at least:
      - name: field name
      - type: Airtable field type string
    And optionally:
      - options: field-type-specific options
      - description: field description
    """
    print(f"\nCreating table: {name}")
    payload = {"name": name, "fields": fields}
    if description:
        payload["description"] = description

    result = api_call("post", META_URL, payload)
    if result:
        table_id = result["id"]
        print(f"  ✅ Created: {table_id}")
        return table_id
    return None


def add_field(table_id, field):
    """
    Add a single field to an existing table.
    'field' is a dict with name, type, and optionally options/description.
    """
    url = f"{META_URL}/{table_id}/fields"
    print(f"  Adding field: {field['name']} ({field['type']})")
    result = api_call("post", url, field)
    if result:
        print(f"    ✅ Added")
    return result


# ──────────────────────────────────────────────
# Table definitions
# ──────────────────────────────────────────────

def setup():
    """Create all tables in dependency order."""

    print("=" * 50)
    print("Kii-bot Airtable Setup")
    print(f"Base: {BASE_ID}")
    print("=" * 50)

    # ── 1. Team Members ──
    team_id = create_table(
        "Team Members",
        description="Roster of all team members with current rates and roles.",
        fields=[
            {"name": "Name", "type": "singleLineText"},
            {"name": "Telegram user ID", "type": "number",
             "options": {"precision": 0}},
            {"name": "Telegram username", "type": "singleLineText"},
            {"name": "Status", "type": "singleSelect",
             "options": {"choices": [
                 {"name": "Active", "color": "greenBright"},
                 {"name": "Pending", "color": "yellowBright"},
                 {"name": "Inactive", "color": "grayBright"},
             ]}},
            {"name": "Role", "type": "singleSelect",
             "options": {"choices": [
                 {"name": "admin", "color": "purpleBright"},
                 {"name": "member", "color": "blueBright"},
             ]}},
            {"name": "Current hourly rate (SGD)", "type": "currency",
             "options": {"precision": 2, "symbol": "$"}},
            {"name": "Start date", "type": "date",
             "options": {"dateFormat": {"name": "iso"}}},
            {"name": "Notes", "type": "multilineText"},
        ],
    )

    if not team_id:
        print("Failed to create Team Members. Aborting.")
        sys.exit(1)

    time.sleep(0.5)  # Small pause between table creations

    # ── 2. Rate History ──
    # Primary field must be a text type — linked records can't be primary.
    # "Entry" is a placeholder label; each row represents one rate change.
    rate_id = create_table(
        "Rate History",
        description="Append-only audit trail of rate changes.",
        fields=[
            {"name": "Entry", "type": "singleLineText"},
            {"name": "Rate (SGD)", "type": "currency",
             "options": {"precision": 2, "symbol": "$"}},
            {"name": "Effective from", "type": "date",
             "options": {"dateFormat": {"name": "iso"}}},
            {"name": "Changed by", "type": "singleLineText"},
            {"name": "Reason", "type": "multilineText"},
        ],
    )

    # Add linked field after table creation
    if rate_id:
        time.sleep(0.3)
        add_field(rate_id, {
            "name": "Member",
            "type": "multipleRecordLinks",
            "options": {"linkedTableId": team_id},
        })

    time.sleep(0.5)

    # ── 3. Shifts ──
    # autoNumber can't be created via API. Use a text primary field instead.
    # You can manually convert or add an autonumber column in Airtable later.
    shifts_id = create_table(
        "Shifts",
        description="Core shift records with immutable rate snapshots.",
        fields=[
            {"name": "Shift label", "type": "singleLineText"},
            {"name": "Start time", "type": "dateTime",
             "options": {
                 "dateFormat": {"name": "iso"},
                 "timeFormat": {"name": "24hour"},
                 "timeZone": "Asia/Singapore",
             }},
            {"name": "End time", "type": "dateTime",
             "options": {
                 "dateFormat": {"name": "iso"},
                 "timeFormat": {"name": "24hour"},
                 "timeZone": "Asia/Singapore",
             }},
            {"name": "Hourly rate snapshot (SGD)", "type": "currency",
             "options": {"precision": 2, "symbol": "$"}},
            {"name": "Status", "type": "singleSelect",
             "options": {"choices": [
                 {"name": "Open", "color": "greenBright"},
                 {"name": "Closed", "color": "blueBright"},
                 {"name": "Auto-closed", "color": "yellowBright"},
                 {"name": "Edit-approved", "color": "cyanBright"},
                 {"name": "Locked", "color": "grayBright"},
             ]}},
            {"name": "Source", "type": "singleSelect",
             "options": {"choices": [
                 {"name": "Telegram"},
                 {"name": "Console"},
                 {"name": "Manual"},
                 {"name": "Edit-approved"},
             ]}},
            {"name": "Notes", "type": "multilineText"},
        ],
    )

    if not shifts_id:
        print("Failed to create Shifts. Aborting.")
        sys.exit(1)

    # Add linked + formula fields to Shifts (must be done after creation)
    time.sleep(0.5)

    add_field(shifts_id, {
        "name": "Member",
        "type": "multipleRecordLinks",
        "options": {"linkedTableId": team_id},
    })

    time.sleep(0.3)

    # Unpaid lunch: overlap with 13:00-14:00 SGT on the shift's date,
    # clamped to [0, 3600] SECONDS. SGT is UTC+8 with no DST, so the window
    # is always 05:00-06:00 UTC on the shift's SGT date. The result is in
    # seconds so Airtable formats the field as a Duration (h:mm) — a full
    # hour reads '1:00'. Duration (hours) below subtracts these seconds.
    # Overlap logic must stay in sync with lunch_overlap_hours() in
    # core/timeutils.py (that helper returns the same overlap in hours).
    lunch_start_utc = (
        "DATETIME_PARSE("
        "DATETIME_FORMAT(DATEADD({Start time},8,'hours'),'YYYY-MM-DD')"
        "&' 05:00','YYYY-MM-DD HH:mm')"
    )
    add_field(shifts_id, {
        "name": "Lunch (hours)",
        "type": "formula",
        "options": {
            "formula": (
                "IF(AND({Start time},{End time}),"
                "MAX(0,"
                f"MIN(DATETIME_DIFF({{End time}},{lunch_start_utc},'seconds'),3600)"
                f"-MAX(DATETIME_DIFF({{Start time}},{lunch_start_utc},'seconds'),0)"
                "),"
                "0)"
            ),
        },
    })

    time.sleep(0.3)

    # Raw shift seconds minus lunch seconds, then to decimal hours.
    add_field(shifts_id, {
        "name": "Duration (hours)",
        "type": "formula",
        "options": {
            "formula": (
                "IF(AND({Start time},{End time}),"
                "(DATETIME_DIFF({End time},{Start time},'seconds')"
                "-{Lunch (hours)})/3600,"
                "BLANK())"
            ),
        },
    })

    time.sleep(0.3)

    add_field(shifts_id, {
        "name": "Gross pay (SGD)",
        "type": "formula",
        "options": {
            "formula": (
                "IF({Duration (hours)},"
                "{Duration (hours)}*{Hourly rate snapshot (SGD)},"
                "BLANK())"
            ),
        },
    })

    time.sleep(0.3)

    add_field(shifts_id, {
        "name": "Pay month",
        "type": "formula",
        "options": {
            "formula": "IF({Start time},DATETIME_FORMAT({Start time},'YYYY-MM'),BLANK())",
        },
    })

    time.sleep(0.5)

    # ── 4. Shift Edit Requests ──
    edits_id = create_table(
        "Shift Edit Requests",
        description="Edit requests from members, reviewed by admins.",
        fields=[
            {"name": "Request", "type": "singleLineText"},
            {"name": "Original start", "type": "dateTime",
             "options": {
                 "dateFormat": {"name": "iso"},
                 "timeFormat": {"name": "24hour"},
                 "timeZone": "Asia/Singapore",
             }},
            {"name": "Original end", "type": "dateTime",
             "options": {
                 "dateFormat": {"name": "iso"},
                 "timeFormat": {"name": "24hour"},
                 "timeZone": "Asia/Singapore",
             }},
            {"name": "Requested start", "type": "dateTime",
             "options": {
                 "dateFormat": {"name": "iso"},
                 "timeFormat": {"name": "24hour"},
                 "timeZone": "Asia/Singapore",
             }},
            {"name": "Requested end", "type": "dateTime",
             "options": {
                 "dateFormat": {"name": "iso"},
                 "timeFormat": {"name": "24hour"},
                 "timeZone": "Asia/Singapore",
             }},
            {"name": "Reason", "type": "multilineText"},
            {"name": "Status", "type": "singleSelect",
             "options": {"choices": [
                 {"name": "Pending", "color": "yellowBright"},
                 {"name": "Approved", "color": "greenBright"},
                 {"name": "Rejected", "color": "redBright"},
             ]}},
            {"name": "Submitted at", "type": "createdTime",
             "options": {"result": {
                 "type": "dateTime",
                 "options": {
                     "dateFormat": {"name": "iso"},
                     "timeFormat": {"name": "24hour"},
                     "timeZone": "Asia/Singapore",
                 },
             }}},
            {"name": "Reviewed at", "type": "dateTime",
             "options": {
                 "dateFormat": {"name": "iso"},
                 "timeFormat": {"name": "24hour"},
                 "timeZone": "Asia/Singapore",
             }},
            {"name": "Admin notes", "type": "multilineText"},
        ],
    )

    # Add linked fields after creation
    if edits_id:
        time.sleep(0.3)
        add_field(edits_id, {
            "name": "Shift",
            "type": "multipleRecordLinks",
            "options": {"linkedTableId": shifts_id},
        })
        time.sleep(0.3)
        add_field(edits_id, {
            "name": "Requested by",
            "type": "multipleRecordLinks",
            "options": {"linkedTableId": team_id},
        })
        time.sleep(0.3)
        add_field(edits_id, {
            "name": "Reviewed by",
            "type": "multipleRecordLinks",
            "options": {"linkedTableId": team_id},
        })

    time.sleep(0.5)

    # ── 5. Availability ──
    avail_id = create_table(
        "Availability",
        description="Weekly availability submissions from members.",
        fields=[
            {"name": "Entry", "type": "singleLineText"},
            {"name": "Date", "type": "date",
             "options": {"dateFormat": {"name": "iso"}}},
            {"name": "Confirmed", "type": "checkbox",
             "options": {"icon": "check", "color": "greenBright"}},
            {"name": "Notified", "type": "checkbox",
             "options": {"icon": "check", "color": "grayBright"}},
            {"name": "Submitted at", "type": "createdTime",
             "options": {"result": {
                 "type": "dateTime",
                 "options": {
                     "dateFormat": {"name": "iso"},
                     "timeFormat": {"name": "24hour"},
                     "timeZone": "Asia/Singapore",
                 },
             }}},
        ],
    )

    # Add linked field after creation
    if avail_id:
        time.sleep(0.3)
        add_field(avail_id, {
            "name": "Member",
            "type": "multipleRecordLinks",
            "options": {"linkedTableId": team_id},
        })

    # Add formula field for Week starting
    time.sleep(0.3)
    add_field(avail_id, {
        "name": "Week starting",
        "type": "formula",
        "options": {
            # Monday of the Date's week, as a 'YYYY-MM-DD' string.
            # WEEKDAY's 2nd arg is a day-name string ('Monday'), NOT a number;
            # the old numeric form errored (#ERROR!). The code no longer
            # depends on this field, but keep it correct for UI grouping.
            "formula": (
                "IF({Date},"
                "DATETIME_FORMAT(DATEADD({Date},-WEEKDAY({Date},'Monday'),'days'),"
                "'YYYY-MM-DD'),"
                "BLANK())"
            ),
        },
    })

    print("\n" + "=" * 50)
    print("Setup complete!")
    print("=" * 50)
    print(f"\nTable IDs:")
    print(f"  Team Members:        {team_id}")
    print(f"  Rate History:        {rate_id}")
    print(f"  Shifts:              {shifts_id}")
    print(f"  Shift Edit Requests: {edits_id}")
    print(f"  Availability:        {avail_id}")
    print(f"\nNext steps:")
    print(f"  1. Add yourself to Team Members (Role: admin)")
    print(f"  2. Add your part-timers (Role: member)")
    print(f"  3. Set up the Rate History automation in Airtable:")
    print(f"     Trigger: 'Current hourly rate (SGD)' changes on Team Members")
    print(f"     Action: Create record in Rate History with the new rate")
    print(f"  4. Create the Monthly Payroll Summary view on the Shifts table:")
    print(f"     Filter: Pay month = current month")
    print(f"     Group by: Member")
    print(f"  5. Copy .env.template to .env and fill in your tokens")
    print(f"  6. Run: python main.py")


if __name__ == "__main__":
    setup()
