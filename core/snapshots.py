"""
Morning priority-score snapshots → Google Sheets.

Purpose (DESIGN_SCHEDULING.md §11): a daily log of what the priority
score said each morning. Since 2026-08-24 the score no longer rations
the day — the planning Mini App offers the full plannable list and the
score only sorts it (§12) — so this is a record of the sort order, not
of a decision.

Deliberately logs the score's INPUTS (days since touch, due date, tier,
status, touched-yesterday), not just the total — with inputs in the
sheet, alternative weights can be tested counterfactually against the
whole history using spreadsheet formulas alone.

The ranking-vs-actuals comparison layer that used to live here was
retired on 2026-08-24 along with `/compare`: once selection is free
from a full list, "ranked but skipped" means nothing and "worked
(unranked)" is near-impossible, so the join had no signal left to
carry. Actuals live in Airtable regardless.

Storage is a Google Sheet, NOT Airtable (Marcus reviews/calculates
there) and NOT a local CSV (Railway's filesystem is ephemeral — files
die on every deploy). Auth is a Google service account; the sheet is
shared with the service account's email. Missing config disables the
job cleanly at registration.
"""

import json
import logging
import re
from datetime import datetime

import config
from core import airtable_client as at

logger = logging.getLogger(__name__)

SNAPSHOT_HEADER = [
    "Date", "Rank", "Project", "Score", "Tier",
    "Days since touch", "Design Due", "Status", "Touched yesterday",
]


def snapshots_configured() -> bool:
    return not missing_snapshot_config()


def missing_snapshot_config() -> list[str]:
    """
    Names of the env vars needed for snapshots that are absent or empty.
    Reported individually at startup: 'both missing' (variables never
    reached the process — e.g. Railway changes staged but not applied)
    and 'one missing' (typo, or an oversized value silently dropped) have
    different fixes, and a combined message can't tell them apart.
    """
    missing = []
    if not (config.GOOGLE_SERVICE_ACCOUNT_JSON or "").strip():
        missing.append("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not (config.SCORE_SNAPSHOT_SHEET_ID or "").strip():
        missing.append("SCORE_SNAPSHOT_SHEET_ID")
    return missing


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


def take_snapshot() -> int:
    """
    Fetch today's design candidates, build the rows, append them to the
    sheet. Returns the number of rows written (0 = no candidates).
    Raises on any failure — callers decide how to report it.

    Shared by the 06:05 job and the on-demand /snapshot command so both
    paths exercise exactly the same code.
    """
    from core.timeutils import now

    candidates = at.get_design_candidates()
    rows = build_snapshot_rows(candidates, now())
    if not rows:
        return 0
    try:
        append_snapshot_rows(rows)
    except Exception as e:
        raise _translated(e) from e
    return len(rows)


def _translated(e: Exception) -> Exception:
    """
    Turn Google's least helpful failure into a readable one.

    When the request doesn't reach the Sheets API handler, Google's
    frontend returns an HTML page and gspread surfaces it verbatim as
    APIError [-1] — kilobytes of markup that say nothing. Detect that
    shape and state the likely causes instead. Other errors pass
    through untouched; the original is always chained for the logs.
    """
    text = str(e)
    if "<!DOCTYPE html" in text or "<html" in text:
        return RuntimeError(
            "Google returned an HTML page instead of API data. Usually: "
            "(1) SCORE_SNAPSHOT_SHEET_ID isn't a valid spreadsheet ID, or "
            "(2) the Google Sheets API isn't enabled on the service "
            "account's project."
        )
    return e


_SHEET_URL_KEY = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def sheet_key(value: str) -> str:
    """
    Accept either a bare spreadsheet ID or a full Google Sheets URL.

    Pasting the whole URL is the natural mistake, and it fails in a
    baffling way: the slashes and scheme corrupt the API path, so
    Google's frontend serves an HTML error page and gspread reports
    APIError [-1] with a wall of HTML instead of a 404 (seen live
    2026-08-04). Normalising here removes that whole failure class.
    """
    value = (value or "").strip().strip('"').strip("'")
    match = _SHEET_URL_KEY.search(value)
    return match.group(1) if match else value


def _spreadsheet():
    """Open the configured spreadsheet. gspread is imported lazily so
    the bot runs without Google config."""
    import gspread

    creds = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
    client = gspread.service_account_from_dict(creds)
    return client.open_by_key(sheet_key(config.SCORE_SNAPSHOT_SHEET_ID))


# Anchor every append to the table that starts at A1.
#
# Without this, gspread sends the whole worksheet as the range and the
# Sheets API auto-detects "the table" to append after — including which
# column it starts in. Once anything on the sheet makes it pick a block
# whose left edge isn't column A, the append lands in that column, and
# the next day's detection re-anchors further right again: each day's
# rows marched a few columns rightward (observed live, 5-6 Aug 2026).
# Pinning the search to A1 makes the left edge always column A.
TABLE_ANCHOR = "A1"


def append_rows_to_worksheet(name: str, header: list[str], rows: list[list]) -> None:
    """Append rows to a worksheet, creating it and its header on first use."""
    import gspread

    sheet = _spreadsheet()
    try:
        worksheet = sheet.worksheet(name)
    except gspread.WorksheetNotFound:
        worksheet = sheet.add_worksheet(name, rows=2000, cols=len(header))

    if not worksheet.get_values("A1:A1"):
        worksheet.append_row(header, table_range=TABLE_ANCHOR)

    worksheet.append_rows(rows, value_input_option="USER_ENTERED",
                          table_range=TABLE_ANCHOR)


def append_snapshot_rows(rows: list[list]) -> None:
    """Append rows to the Snapshots worksheet."""
    append_rows_to_worksheet(config.SNAPSHOT_WORKSHEET, SNAPSHOT_HEADER, rows)
