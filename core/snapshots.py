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
import re
from datetime import datetime

import config
from core import airtable_client as at

logger = logging.getLogger(__name__)

SNAPSHOT_HEADER = [
    "Date", "Rank", "Project", "Score", "Tier",
    "Days since touch", "Design Due", "Status", "Touched yesterday",
]


COMPARISON_HEADER = [
    "Date", "Project", "Rank", "Score",
    "Design hours", "Total hours", "Block types", "Outcome",
]

# Outcome categories — the whole point of the comparison. Both kinds of
# disagreement are tuning signal, and the second is the one that finds
# terms the score doesn't model at all.
OUTCOME_WORKED = "Worked"                    # ranked and actually done
OUTCOME_SKIPPED = "Ranked but skipped"       # score said urgent, human passed
OUTCOME_UNRANKED = "Worked (unranked)"       # human found it important, score didn't

# Block types that count as design effort (mirrors the Confirmed touch
# formula in Airtable — DESIGN_SCHEDULING.md §9.1). Other types are
# real attributable work but not design motion.
DESIGN_BLOCK_TYPES = {"Design", "CAM"}
# Planned = not data yet; Dropped = didn't happen. Neither is an actual.
COUNTED_BLOCK_STATUSES = {"Confirmed", "Adjusted"}


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


def take_comparison(day: str) -> int:
    """
    Write the comparison for `day` (ISO date): that morning's frozen
    ranking joined against the blocks actually recorded. Returns rows
    written; 0 when there's nothing to compare (no snapshot for that
    day and no work recorded).
    """
    try:
        snapshot_rows = read_snapshot_rows_for(day)
        blocks = at.get_design_blocks_for_day(day)
        if not snapshot_rows and not blocks:
            return 0
        project_names = {
            rec_id: rec["fields"].get("Project name", "(unnamed)")
            for rec_id, rec in at.get_all_projects_indexed().items()
        }
        rows = build_comparison_rows(snapshot_rows, blocks, project_names, day)
        if not rows:
            return 0
        append_rows_to_worksheet(
            config.COMPARISON_WORKSHEET, COMPARISON_HEADER, rows
        )
        return len(rows)
    except Exception as e:
        raise _translated(e) from e


def read_snapshot_rows_for(day: str) -> list[list]:
    """Rows from the Snapshots worksheet whose Date column matches `day`."""
    import gspread

    worksheet = _open_worksheet(config.SNAPSHOT_WORKSHEET)
    if worksheet is None:
        return []
    return [r for r in worksheet.get_all_values()[1:] if r and r[0] == day]


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


def summarise_blocks(blocks: list[dict], project_names: dict[str, str]) -> dict:
    """
    Group a day's Design Blocks by project name → {design_hours,
    total_hours, types}. Only Confirmed/Adjusted blocks count: Planned
    is provisional and Dropped didn't happen.
    """
    by_project: dict[str, dict] = {}
    for block in blocks:
        f = block["fields"]
        if f.get("Block status") not in COUNTED_BLOCK_STATUSES:
            continue
        project_ids = f.get("Project") or []
        if not project_ids:
            continue
        name = project_names.get(project_ids[0], "(unknown project)")
        block_type = f.get("Block type") or "(none)"
        # Confirmed designer-hours already applies the headcount multiplier.
        hours = f.get("Confirmed designer-hours")
        if hours is None:
            hours = f.get("Actual hours") or 0

        entry = by_project.setdefault(
            name, {"design_hours": 0.0, "total_hours": 0.0, "types": set()}
        )
        entry["total_hours"] += hours
        if block_type in DESIGN_BLOCK_TYPES:
            entry["design_hours"] += hours
        entry["types"].add(block_type)
    return by_project


def build_comparison_rows(
    snapshot_rows: list[list],
    blocks: list[dict],
    project_names: dict[str, str],
    day: str,
) -> list[list]:
    """
    Join a day's frozen ranking against what actually happened.

    Full outer join on project, deliberately: projects ranked but not
    worked AND projects worked but never ranked both matter. The second
    group is the blind-spot signal — work the score couldn't see at all
    (project not a design candidate, or ranked so low it never surfaced).

    snapshot_rows are the raw sheet rows for `day` (SNAPSHOT_HEADER
    order); reading them back rather than recomputing is the point —
    they're the frozen record of what the score said that morning.
    """
    worked = summarise_blocks(blocks, project_names)

    rows = []
    ranked_projects = set()
    for snap in snapshot_rows:
        # Date, Rank, Project, Score, ...
        project = snap[2] if len(snap) > 2 else ""
        if not project:
            continue
        ranked_projects.add(project)
        actual = worked.get(project)
        rows.append([
            day,
            project,
            snap[1] if len(snap) > 1 else "",
            snap[3] if len(snap) > 3 else "",
            round(actual["design_hours"], 2) if actual else 0,
            round(actual["total_hours"], 2) if actual else 0,
            ", ".join(sorted(actual["types"])) if actual else "",
            OUTCOME_WORKED if (actual and actual["design_hours"] > 0)
            else OUTCOME_SKIPPED,
        ])

    for project, actual in sorted(worked.items()):
        if project in ranked_projects:
            continue
        rows.append([
            day, project, "", "",
            round(actual["design_hours"], 2),
            round(actual["total_hours"], 2),
            ", ".join(sorted(actual["types"])),
            OUTCOME_UNRANKED,
        ])
    return rows


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


def _open_worksheet(name: str):
    """Return the named worksheet, or None if it doesn't exist yet."""
    import gspread

    try:
        return _spreadsheet().worksheet(name)
    except gspread.WorksheetNotFound:
        return None


def append_rows_to_worksheet(name: str, header: list[str], rows: list[list]) -> None:
    """Append rows to a worksheet, creating it and its header on first use."""
    import gspread

    sheet = _spreadsheet()
    try:
        worksheet = sheet.worksheet(name)
    except gspread.WorksheetNotFound:
        worksheet = sheet.add_worksheet(name, rows=2000, cols=len(header))

    if not worksheet.get_values("A1:A1"):
        worksheet.append_row(header)

    worksheet.append_rows(rows, value_input_option="USER_ENTERED")


def append_snapshot_rows(rows: list[list]) -> None:
    """Append rows to the Snapshots worksheet."""
    append_rows_to_worksheet(config.SNAPSHOT_WORKSHEET, SNAPSHOT_HEADER, rows)
