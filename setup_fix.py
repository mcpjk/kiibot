"""
Fix script: creates the two tables that failed (Shift Edit Requests, Availability)
and adds the Shifts Member linked field if missing.

Run AFTER setup_airtable.py. Uses the table IDs from the first run.

Fields that can't be created via API (formula, createdTime) are listed
at the end — you add those manually in Airtable's UI.

Usage:
    python3 setup_fix.py
"""

import os
import sys
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

# ── Table IDs from the first run ──
# Update these if yours are different (check setup_airtable.py output)
TEAM_MEMBERS_ID = "tblfRUA1asrRINZFz"
SHIFTS_ID = "tblujYBdOPJP6qcuY"


def api_call(method, url, data=None):
    resp = getattr(requests, method)(url, headers=HEADERS, json=data)
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", 30))
        print(f"  Rate limited, waiting {wait}s...")
        time.sleep(wait)
        resp = getattr(requests, method)(url, headers=HEADERS, json=data)
    if resp.status_code not in (200, 201):
        print(f"  ERROR {resp.status_code}: {resp.text}")
        return None
    return resp.json()


def create_table(name, fields, description=""):
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
    url = f"{META_URL}/{table_id}/fields"
    print(f"  Adding field: {field['name']} ({field['type']})")
    result = api_call("post", url, field)
    if result:
        print(f"    ✅ Added")
    return result


def setup():
    print("=" * 50)
    print("Kii-bot Airtable Fix Script")
    print(f"Base: {BASE_ID}")
    print("=" * 50)

    dt_options = {
        "dateFormat": {"name": "iso"},
        "timeFormat": {"name": "24hour"},
        "timeZone": "Asia/Singapore",
    }

    # ── Shift Edit Requests ──
    # Created WITHOUT: createdTime (Submitted at), linked records
    # Those are added after or manually.
    edits_id = create_table(
        "Shift Edit Requests",
        description="Edit requests from members, reviewed by admins.",
        fields=[
            {"name": "Request", "type": "singleLineText"},
            {"name": "Original start", "type": "dateTime", "options": dt_options},
            {"name": "Original end", "type": "dateTime", "options": dt_options},
            {"name": "Requested start", "type": "dateTime", "options": dt_options},
            {"name": "Requested end", "type": "dateTime", "options": dt_options},
            {"name": "Reason", "type": "multilineText"},
            {"name": "Status", "type": "singleSelect",
             "options": {"choices": [
                 {"name": "Pending", "color": "yellowBright"},
                 {"name": "Approved", "color": "greenBright"},
                 {"name": "Rejected", "color": "redBright"},
             ]}},
            {"name": "Reviewed at", "type": "dateTime", "options": dt_options},
            {"name": "Admin notes", "type": "multilineText"},
        ],
    )

    if edits_id:
        time.sleep(0.3)
        add_field(edits_id, {
            "name": "Shift",
            "type": "multipleRecordLinks",
            "options": {"linkedTableId": SHIFTS_ID},
        })
        time.sleep(0.3)
        add_field(edits_id, {
            "name": "Requested by",
            "type": "multipleRecordLinks",
            "options": {"linkedTableId": TEAM_MEMBERS_ID},
        })
        time.sleep(0.3)
        add_field(edits_id, {
            "name": "Reviewed by",
            "type": "multipleRecordLinks",
            "options": {"linkedTableId": TEAM_MEMBERS_ID},
        })

    time.sleep(0.5)

    # ── Availability ──
    # Created WITHOUT: createdTime (Submitted at)
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
        ],
    )

    if avail_id:
        time.sleep(0.3)
        add_field(avail_id, {
            "name": "Member",
            "type": "multipleRecordLinks",
            "options": {"linkedTableId": TEAM_MEMBERS_ID},
        })

    # ── Summary ──
    print("\n" + "=" * 50)
    print("Fix script complete!")
    print("=" * 50)
    print(f"\nTable IDs:")
    print(f"  Team Members (existing):        {TEAM_MEMBERS_ID}")
    print(f"  Shifts (existing):              {SHIFTS_ID}")
    print(f"  Shift Edit Requests:            {edits_id}")
    print(f"  Availability:                   {avail_id}")

    print(f"\n{'=' * 50}")
    print("MANUAL STEPS IN AIRTABLE")
    print(f"{'=' * 50}")
    print("""
Open your Airtable base and add these fields manually.
In each table, click the '+' at the end of the field headers
to add a new field.

── Shifts table ──

1. "Duration (hours)" — Field type: Formula
   Formula:
   IF(AND({Start time},{End time}),DATETIME_DIFF({End time},{Start time},'seconds')/3600,BLANK())

2. "Gross pay (SGD)" — Field type: Formula
   Formula:
   IF({Duration (hours)},{Duration (hours)}*{Hourly rate snapshot (SGD)},BLANK())

3. "Pay month" — Field type: Formula
   Formula:
   IF({Start time},DATETIME_FORMAT({Start time},'YYYY-MM'),BLANK())

── Shift Edit Requests table ──

4. "Submitted at" — Field type: Created time
   Format: ISO, 24-hour, Asia/Singapore

── Availability table ──

5. "Submitted at" — Field type: Created time
   Format: ISO, 24-hour, Asia/Singapore

6. "Week starting" — Field type: Formula
   Formula:
   IF({Date},DATEADD({Date},-(WEEKDAY({Date},1)-2),'days'),BLANK())

── Views ──

7. On the Shifts table, create a view called "Monthly Payroll Summary":
   - Filter: Pay month = (current month, e.g. 2026-04)
   - Group by: Member
   - Fields shown: Start time, End time, Duration (hours), Gross pay (SGD)
   - Enable summary row: SUM for Duration and Gross pay

── Automation ──

8. Create an automation for Rate History:
   - Trigger: When a record is updated in Team Members
   - Condition: "Current hourly rate (SGD)" has changed
   - Action: Create a record in Rate History
     - Member: the updated record
     - Rate (SGD): the new rate value
     - Effective from: today
     - Changed by: (your name or leave blank)

── Data ──

9. Add yourself to Team Members:
   - Name: Marcus
   - Telegram user ID: (your numeric Telegram user ID)
   - Status: Active
   - Role: admin
   - Current hourly rate: (whatever, not used for admin)

10. Add Faqih and Taufiq to Team Members:
    - Status: Active
    - Role: member
    - Set their hourly rates
    - Set their Telegram user IDs
      (They can find this by messaging @userinfobot on Telegram)
""")


if __name__ == "__main__":
    setup()
