"""
Morning priority-score snapshots → Google Sheets.

Purpose (DESIGN_SCHEDULING.md): freeze the ranking at the start of each
day so it can be compared with what was actually selected (the Design
Blocks created that day). The comparison drives score-weight tuning and
surfaces terms the score isn't accounting for.

Deliberately logs the score's INPUTS (days since touch, due date, tier,
status, touched-yesterday), not just the total — with inputs in the
sheet, alternative weights can be tested counterfactually against the
whole history using spreadsheet formulas alone.

Storage is a Google Sheet, NOT Airtable (Marcus reviews/calculates
there) and NOT a local CSV (Railway's filesystem is ephemeral — files
die on every deploy). Auth is a Google service account; the sheet is
shared with the service account's email. Missing config disables the
job cleanly at registration.
"""

import json
import logging
from datetime import datetime

import config
from core import airtable_client as at

logger = logging.getLogger(__name__)

SNAPSHOT_HEADER = [
    "Date", "Rank", "Project", "Score", "Tier",
    "Days since touch", "Design Due", "Status", "Touched yesterday",
]


def snapshots_configured() -> bool:
    return bool(config.GOOGLE_SERVICE_ACCOUNT_JSON and config.SCORE_SNAPSHOT_SHEET_ID)


def build_snapshot_rows(candidates: list[dict], snapshot_dt: datetime) -> list[list]:
    """
    Pure transform: candidate records → sheet rows, ranked by Priority
    score descending. One row per candidate per day.
    """
    day = snapshot_dt.date().isoformat()

    def score(record):
        return record["fields"].get("Priority score") or 0

    rows = []
    for rank, record in enumerate(sorted(candidates, key=score, reverse=True), start=1):
        f = record["fields"]
        rows.append([
            day,
            rank,
            f.get("Project name", "(unnamed)"),
            f.get("Priority score") or 0,
            f.get("Priority tier") or "",
            f.get("Days since touch") if f.get("Days since touch") is not None else "",
            f.get("Design Due") or "",
            f.get("Status") or "",
            1 if f.get("Touched yesterday") else 0,
        ])
    return rows


def append_snapshot_rows(rows: list[list]) -> None:
    """
    Append rows to the configured sheet's Snapshots worksheet, creating
    the worksheet and header on first use. Raises on failure — the
    caller decides how loudly to complain.
    """
    import gspread  # deferred: only needed when snapshots are configured

    creds = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
    client = gspread.service_account_from_dict(creds)
    sheet = client.open_by_key(config.SCORE_SNAPSHOT_SHEET_ID)

    try:
        worksheet = sheet.worksheet(config.SNAPSHOT_WORKSHEET)
    except gspread.WorksheetNotFound:
        worksheet = sheet.add_worksheet(
            config.SNAPSHOT_WORKSHEET, rows=2000, cols=len(SNAPSHOT_HEADER)
        )

    if not worksheet.get_values("A1:A1"):
        worksheet.append_row(SNAPSHOT_HEADER)

    worksheet.append_rows(rows, value_input_option="USER_ENTERED")
