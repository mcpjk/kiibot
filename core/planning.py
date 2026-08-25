"""
Daily design planning (DESIGN_SCHEDULING.md §12).

The morning selection flow, replacing the never-adopted ration model
(§9a). The designer opens a Telegram Mini App, picks the projects
they'll work on today from the full plannable list, then sets a block
type and duration for each. The bot lays those out as Design Blocks.

Two decisions shape everything here (Marcus, 2026-08-24):

- **The score no longer rations.** All plannable projects are offered,
  not a top-N; the human picks. `Priority score` survives only as the
  list's default sort order, so the neglect ordering still reaches the
  eye without deciding anything.
- **15-minute grid**, not the old half-hour — comms blocks are often
  shorter than 30 min.

The packer is pure (`pack_blocks`) so the layout rules are testable
without network, exactly like `plan_extension` in core/design.py — and
it shares that module's lunch rule: 13:00-14:00 is an immovable shared
break, so a block that would land in it starts after it instead.
"""

import logging
from datetime import datetime, timedelta

import config
from core import airtable_client as at
from core.timeutils import TZ, lunch_window, now, round_up_to

logger = logging.getLogger(__name__)

# Block types offered in the Mini App. Mirrors the Airtable single
# select on Design Blocks — 'Site / meeting' is spelled exactly as the
# option is, or Airtable rejects the write.
BLOCK_TYPES = [
    "Design",
    "CAM",
    "Client comms",
    "Site / meeting",
    "Admin",
    "Assembly",
]

# Projects offered for planning: actively being worked, and not blocked
# on the client. 'Pending client' stays excluded (Marcus, 2026-08-24) —
# design can't progress without client input, and including it would
# put 12 of 33 projects on the list that you can't act on.
PLANNABLE_PROCESSES = ("Designing", "Fabricating")
EXCLUDED_STATUSES = ("Cancelled", "Pending client")


class PlanningError(Exception):
    """Raised when a planning operation fails for a known reason."""
    pass


def planning_configured() -> bool:
    """The Mini App needs a public HTTPS origin; Telegram won't open
    anything else. Without it the feature disables cleanly."""
    return bool((config.WEBAPP_URL or "").strip())


# ──────────────────────────────────────────────
# The plannable list
# ──────────────────────────────────────────────

def build_project_options(projects: list[dict]) -> list[dict]:
    """
    Pure transform: project records → the Mini App's list payload,
    sorted by Priority score descending.

    `hours` is Hours consumed to date — shown instead of neglect
    signals at Marcus's request: the designers carry the recency
    context themselves, while cumulative effort is the number they
    can't hold in their heads and which feeds future costing.
    """
    def score(record):
        return record["fields"].get("Priority score") or 0

    options = []
    for record in sorted(projects, key=score, reverse=True):
        fields = record["fields"]
        options.append({
            "id": record["id"],
            "name": fields.get("Project name") or "(unnamed)",
            "status": fields.get("Status") or "",
            "process": fields.get("Process") or "",
            "hours": round(fields.get("Hours consumed") or 0, 1),
        })
    return options


def get_plannable_projects() -> list[dict]:
    """Projects a designer may pick from this morning."""
    return build_project_options(at.get_plannable_projects())


def get_planning_designers() -> list[dict]:
    """
    Who gets the morning 'Plan today' prompt: Active members who own
    the design on at least one plannable project.

    Derived from live data rather than a new checkbox or the Role field
    (which CLAUDE.md forbids gating on). It stays correct by itself as
    ownership moves, and a designer with nothing to plan isn't pinged.
    """
    owner_ids = set()
    for project in at.get_plannable_projects():
        for member_id in project["fields"].get("Design owner") or []:
            owner_ids.add(member_id)

    return [
        member for member in at.get_active_members()
        if member["id"] in owner_ids
    ]


# ──────────────────────────────────────────────
# Laying the day out (pure)
# ──────────────────────────────────────────────

def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def pack_blocks(selections: list[dict], start: datetime) -> list[dict]:
    """
    Lay the selected blocks end to end from `start`, jumping the lunch
    hour. Pure — no Airtable I/O.

    `selections` is [{project_id, block_type, minutes}, ...] in the
    order the designer chose them; that order is the running order (no
    reordering step, by decision — the evening drag and /extend fix
    reality anyway).
    """
    blocks = []
    cursor = start.astimezone(TZ)

    for selection in selections:
        minutes = int(selection["minutes"])
        if not (config.PLAN_MIN_BLOCK_MINUTES <= minutes
                <= config.PLAN_MAX_BLOCK_MINUTES):
            raise PlanningError(
                f"Block length must be between {config.PLAN_MIN_BLOCK_MINUTES} "
                f"and {config.PLAN_MAX_BLOCK_MINUTES} minutes."
            )
        if selection.get("block_type") not in BLOCK_TYPES:
            raise PlanningError(f"Unknown block type: {selection.get('block_type')!r}")

        lunch_start, lunch_end = lunch_window(cursor)
        # Starting inside lunch, or running into it, both mean the same
        # thing: this block belongs after the break.
        if lunch_start <= cursor < lunch_end:
            cursor = lunch_end
        end = cursor + timedelta(minutes=minutes)
        if _overlaps(cursor, end, lunch_start, lunch_end):
            cursor = lunch_end
            end = cursor + timedelta(minutes=minutes)

        blocks.append({
            "project_id": selection["project_id"],
            "block_type": selection["block_type"],
            "start": cursor,
            "end": end,
            "hours": minutes / 60,
        })
        cursor = end

    return blocks


def default_minutes(capacity_hours: float, project_count: int) -> int:
    """
    The starting duration offered per project: capacity split evenly,
    snapped to the grid. The Mini App recomputes this live as projects
    are added or removed; it lives here too so the rule has one home.
    """
    if project_count <= 0:
        return config.PLAN_MIN_BLOCK_MINUTES
    raw = (capacity_hours * 60) / project_count
    grid = config.PLAN_GRID_MINUTES
    snapped = int(round(raw / grid) * grid)
    return max(config.PLAN_MIN_BLOCK_MINUTES, snapped)


# ──────────────────────────────────────────────
# Writing the plan
# ──────────────────────────────────────────────

def submit_plan(telegram_id: int, capacity_hours: float,
                selections: list[dict]) -> dict:
    """
    Turn a Mini App submission into Design Blocks for today.

    Writes the frozen plan (`Planned hours`) and leaves `Block status` at
    Planned; the evening pass in Airtable still owns Confirmed/Adjusted.
    """
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise PlanningError("You're not registered in the system. Send /start first.")
    if not selections:
        raise PlanningError("No projects selected.")
    if not (0 < capacity_hours <= config.PLAN_MAX_CAPACITY_HOURS):
        raise PlanningError(
            f"Hours available must be between 0 and {config.PLAN_MAX_CAPACITY_HOURS:g}."
        )

    start = round_up_to(now(), config.PLAN_GRID_MINUTES)
    blocks = pack_blocks(selections, start)

    day_id = at.get_or_create_design_day(
        member["id"], start.date().isoformat(), capacity_hours
    )

    project_names = at.get_all_projects_indexed()
    created = []
    for block in blocks:
        project = project_names.get(block["project_id"])
        name = (project["fields"].get("Project name") if project else None) or "Block"
        record = at.create_design_block({
            # No primary-field write: Design Blocks' primary field is
            # 'Start' (changed 2026-08-25), which Start below already
            # sets. The old text 'Name' field no longer exists.
            "Project": [block["project_id"]],
            "Designers": [member["id"]],
            "Day": [day_id] if day_id else [],
            "Block type": block["block_type"],
            "Block status": "Planned",
            "Start": block["start"].isoformat(),
            "End": block["end"].isoformat(),
            # Frozen plan, written once. Hours, matching the
            # 'Actual hours' / 'Deviation (hours)' formulas.
            "Planned hours": block["hours"],
        })
        created.append({**block, "id": record["id"], "name": name})

    logger.info("Planned %d block(s) for %s (%.2f h declared)",
                len(created), member["fields"].get("Name"), capacity_hours)
    return {
        "member": member,
        "capacity_hours": capacity_hours,
        "blocks": created,
        "day_id": day_id,
    }


def format_plan(plan: dict) -> str:
    """The confirmation DM after a submitted plan."""
    lines = [f"📐 Today's plan ({plan['capacity_hours']:g} h declared):"]
    for block in plan["blocks"]:
        lines.append(
            f"{block['start']:%H:%M}–{block['end']:%H:%M}  {block['name']} "
            f"({block['block_type']})"
        )
    total = sum(b["hours"] for b in plan["blocks"])
    lines.append(f"\n{len(plan['blocks'])} block(s), {total:g} h planned.")
    lines.append("Switch reminders will fire 5 min before each one.")
    return "\n".join(lines)
