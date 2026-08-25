"""
Design-block scheduling logic (see DESIGN_SCHEDULING.md).

Mid-day corrections to the frozen plan: **+15 min on the current task,
−15 min off it, and +15 min of space** (a break, or non-project work).
Every switch reminder carries all three as buttons, and each may be
tapped repeatedly.

Why this needs a cascade at all: observed blocks are almost always
back-to-back (3 Aug: 10:30→11:30→12:30→13:00 SGT contiguous), so an
adjustment nearly always collides with the next planned block rather
than landing in free time.

The rule (decided with Marcus 2026-08-04) is **gap-first**:

- extend the running block by N minutes;
- push the blocks that now collide, in order, and **stop the ripple at
  the first gap that can swallow it** — later blocks keep their planned
  times;
- the 13:00–14:00 lunch hour is immovable (the whole shop breaks
  together): a pushed block that would land in it jumps to *after*
  lunch instead, and an extension that would itself run into lunch is
  refused rather than silently truncated;
- end-of-day counts as a gap, so the last block of the day simply runs
  later and the ripple always terminates. No day cutoff needed.

**Shrinking mirrors it** (2026-08-25): the running block ends N minutes
early and the contiguous chain that follows is pulled earlier by
whatever the intervening gaps don't already absorb. Since that can move
the next block to within minutes of *now* — or into the past — a shrink
reports the next block back to the caller so its switch reminder can be
fired immediately: the polling job only ever pings blocks whose Start
is still in the future, so without this the reminder would die silently
(Marcus's requirement, 2026-08-25).

**Space is not an extension.** '+15 min space' pushes everything after
the current block later without touching the current block's End, so
the break (or the non-project task) never lands on a project's
attributable hours. The current block keeps its planned length; the
gap opens after it.

`Planned hours` is never touched — the plan stays frozen, so any of
these registers as deviation exactly as the spec intends (§1). What
they really buy is actuals captured *as they happen*, which is the
evening pass's observed failure point (§9a).
"""

import logging
from datetime import datetime, timedelta

from core import airtable_client as at
from core.timeutils import TZ, lunch_window, now, parse_dt, round_up_to
import config

logger = logging.getLogger(__name__)

# The step every button uses. 15 min since 2026-08-25: it is the
# planning grid (§12), and the buttons are meant to be tapped several
# times in a row rather than sized for the worst case.
ADJUST_MINUTES = 15
MIN_ADJUST_MINUTES = 5
MAX_ADJUST_MINUTES = 240

# What the three buttons do. Kept as strings because they travel
# through Telegram callback data ('extend:15', 'shrink:15', 'space:15').
ACTIONS = ("extend", "shrink", "space")


class DesignError(Exception):
    """Raised when a design-block operation fails for a known reason.

    The message is shown to the designer verbatim, so keep it short
    enough for a Telegram callback alert (~200 chars).
    """
    pass


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    """True if the two half-open intervals share any time."""
    return a_start < b_end and b_start < a_end


def _timed_blocks(blocks: list[dict]) -> list[tuple[dict, datetime, datetime]]:
    """
    Parse blocks into (record, start, end) sorted by start.

    Dropped blocks are planned-but-didn't-happen and must never
    participate — they're not obstacles and they're not extendable.
    Blocks missing Start or End are skipped rather than guessed at.
    """
    parsed = []
    for block in blocks:
        fields = block["fields"]
        if fields.get("Block status") == "Dropped":
            continue
        start = parse_dt(fields.get("Start"))
        end = parse_dt(fields.get("End"))
        if not start or not end or end <= start:
            continue
        parsed.append((block, start, end))
    return sorted(parsed, key=lambda item: item[1])


def _block_name(block: dict, cache: dict) -> str:
    """
    Display name for a block: its project, falling back to block type.

    Resolved one project at a time (like the switch ping) rather than
    by fetching every project: this runs for a handful of blocks in an
    interactive command, and per-ID reads go through the rename-proof
    field-ID path.
    """
    project_ids = block["fields"].get("Project") or []
    if not project_ids:
        return block["fields"].get("Block type") or "block"
    project_id = project_ids[0]
    if project_id not in cache:
        cache[project_id] = at.get_project_name(project_id)
    return cache[project_id]


def _check_minutes(minutes: int) -> None:
    if not (MIN_ADJUST_MINUTES <= minutes <= MAX_ADJUST_MINUTES):
        raise DesignError(
            f"Adjustments must be between {MIN_ADJUST_MINUTES} and "
            f"{MAX_ADJUST_MINUTES} minutes."
        )


def _running_index(timed, at_time: datetime):
    """
    Index of the block the designer is *in*, or None.

    The same rule whether a button is tapped or the command is typed.
    Note the switch reminder announces the *next* block while its
    buttons adjust the *running* one; that asymmetry is the point (you
    are overrunning, or finishing early on, the current task), and this
    rule still does the right thing when a button is tapped late, after
    the announced block has already started.
    """
    return next(
        (i for i, (_, start, end) in enumerate(timed) if start <= at_time < end),
        None,
    )


def _cascade_forward(timed, from_index: int, cursor: datetime):
    """
    Push every block from `from_index` that collides with `cursor`,
    stopping at the first gap wide enough to absorb what's left of the
    delay. Shared by the extension and the spacer.

    Returns (updates, moved).
    """
    updates, moved = [], []
    for block, start, end in timed[from_index:]:
        if start >= cursor:
            break  # a gap (or lunch, or the end of the day) swallows it

        delta = cursor - start
        new_start, new_end = start + delta, end + delta

        lunch_start, lunch_end = lunch_window(new_start)
        if (_overlaps(new_start, new_end, lunch_start, lunch_end)
                and not _overlaps(start, end, lunch_start, lunch_end)):
            jump = lunch_end - new_start
            new_start, new_end = new_start + jump, new_end + jump

        updates.append((block["id"], {
            "Start": _iso(new_start),
            "End": _iso(new_end),
            # Its switch reminder was for the old time. Without this the
            # dedupe stamp silently suppresses the ping at the new one.
            "Switch ping sent": None,
        }))
        moved.append((block, new_start, new_end))
        cursor = new_end

    return updates, moved


def plan_extension(blocks: list[dict], at_time: datetime,
                   minutes: int = ADJUST_MINUTES) -> dict:
    """
    Work out the writes an extension implies. Pure — no Airtable I/O,
    so the cascade is testable without network.

    `blocks` must already be narrowed to ONE designer's blocks for the
    day. Returns {"action", "target", "new_end", "updates", "moved",
    "ping_next"} where `updates` is a list of (record_id, fields) ready
    to write.
    """
    _check_minutes(minutes)

    at_time = at_time.astimezone(TZ)
    timed = _timed_blocks(blocks)

    target_index = _running_index(timed, at_time)
    if target_index is None:
        raise DesignError(
            "No design block is running right now — this adds time to "
            "the block you're currently in."
        )

    target, target_start, target_end = timed[target_index]
    new_end = target_end + timedelta(minutes=minutes)

    # Lunch is immovable. Refuse rather than truncate: a silently
    # shorter extension than asked for is worse than a clear no.
    # A block already planned through lunch is left alone — this guard
    # blocks *new* violations only, never pre-existing data.
    lunch_start, lunch_end = lunch_window(target_start)
    if (_overlaps(target_start, new_end, lunch_start, lunch_end)
            and not _overlaps(target_start, target_end, lunch_start, lunch_end)):
        raise DesignError(
            f"That would run into the {lunch_start:%H:%M}–{lunch_end:%H:%M} "
            f"lunch hour. Pick it up after lunch instead."
        )

    pushed, moved = _cascade_forward(timed, target_index + 1, new_end)

    return {
        "action": "extend",
        "target": target,
        "new_end": new_end,
        "minutes": minutes,
        "updates": [(target["id"], {"End": _iso(new_end)})] + pushed,
        "moved": moved,
        "ping_next": None,
    }


def plan_shrink(blocks: list[dict], at_time: datetime,
                minutes: int = ADJUST_MINUTES) -> dict:
    """
    The mirror of `plan_extension`: end the running block `minutes`
    early and pull the contiguous chain that follows it earlier by
    whatever the gaps in between don't already absorb. Pure.

    A gap absorbs the pull-back exactly as it absorbs a delay, so a
    block that already had breathing room in front of it keeps its
    planned time and simply gains more.

    `ping_next` is the (block, new_start, new_end) of the next block
    when the pull-back brings it to within the switch-reminder lead of
    *now* — including into the past. The polling job only pings blocks whose
    Start is still in the future, so that reminder has to be fired by
    the caller or it never happens at all.
    """
    _check_minutes(minutes)

    at_time = at_time.astimezone(TZ)
    timed = _timed_blocks(blocks)

    target_index = _running_index(timed, at_time)
    if target_index is None:
        raise DesignError(
            "No design block is running right now — this takes time off "
            "the block you're currently in."
        )

    target, target_start, target_end = timed[target_index]
    new_end = target_end - timedelta(minutes=minutes)

    # Two floors, both about not writing nonsense: a block can't end
    # before the moment you press the button, and can't be shorter than
    # the planning grid. A block that didn't happen at all is `Dropped`
    # in the evening pass — that's a different act, and not this one.
    if new_end < at_time:
        raise DesignError(
            f"That would end the block before now ({at_time:%H:%M}). "
            f"It's due to end at {target_end:%H:%M}."
        )
    floor = config.PLAN_MIN_BLOCK_MINUTES
    if new_end - target_start < timedelta(minutes=floor):
        raise DesignError(
            f"A block can't be shorter than {floor} min. If it didn't "
            f"happen, mark it Dropped in Airtable."
        )

    updates = [(target["id"], {"End": _iso(new_end)})]
    moved = []

    pull = timedelta(minutes=minutes)
    previous_end = target_end  # gaps are measured against the plan as it stands
    for block, start, end in timed[target_index + 1:]:
        pull -= max(timedelta(0), start - previous_end)
        previous_end = end
        if pull <= timedelta(0):
            break  # a gap swallowed it; later blocks keep their planned times

        new_start, block_new_end = start - pull, end - pull

        # Never pull a block into the shared lunch hour, and stop there
        # rather than skipping it: the blocks after it are contiguous
        # with it, not with the pull.
        lunch_start, lunch_end = lunch_window(new_start)
        if (_overlaps(new_start, block_new_end, lunch_start, lunch_end)
                and not _overlaps(start, end, lunch_start, lunch_end)):
            break

        updates.append((block["id"], {
            "Start": _iso(new_start),
            "End": _iso(block_new_end),
            "Switch ping sent": None,
        }))
        moved.append((block, new_start, block_new_end))

    ping_next = None
    if moved:
        next_block, next_start, next_end = moved[0]
        if next_start <= at_time + timedelta(minutes=config.SWITCH_PING_LEAD_MINUTES):
            ping_next = (next_block, next_start, next_end)

    return {
        "action": "shrink",
        "target": target,
        "new_end": new_end,
        "minutes": minutes,
        "updates": updates,
        "moved": moved,
        "ping_next": ping_next,
    }


def plan_spacer(blocks: list[dict], at_time: datetime,
                minutes: int = ADJUST_MINUTES) -> dict:
    """
    Open `minutes` of unallocated time — a break, or non-project work —
    and push the rest of the day back gap-first. Pure.

    The running block is NOT extended: its End stays where it is and
    the space opens after it. That's the whole distinction from
    '+15 min on the current task' — this time belongs to no project, so
    it must not land on one's attributable hours.

    With nothing running the space starts now (snapped up to the grid),
    and only blocks that would collide with it move.
    """
    _check_minutes(minutes)

    at_time = at_time.astimezone(TZ)
    timed = _timed_blocks(blocks)

    target_index = _running_index(timed, at_time)
    if target_index is None:
        target = None
        space_from = round_up_to(at_time, config.PLAN_GRID_MINUTES)
        follow_from = next(
            (i for i, (_, start, _) in enumerate(timed) if start >= at_time),
            len(timed),
        )
    else:
        target, _, target_end = timed[target_index]
        space_from = target_end
        follow_from = target_index + 1

    updates, moved = _cascade_forward(
        timed, follow_from, space_from + timedelta(minutes=minutes)
    )

    return {
        "action": "space",
        "target": target,
        "new_end": space_from + timedelta(minutes=minutes),
        "space_from": space_from,
        "minutes": minutes,
        "updates": updates,
        "moved": moved,
        "ping_next": None,
    }


PLANNERS = {
    "extend": plan_extension,
    "shrink": plan_shrink,
    "space": plan_spacer,
}


def adjust_current_block(telegram_id: int, action: str = "extend",
                         minutes: int = ADJUST_MINUTES) -> dict:
    """
    Apply one of the three mid-day adjustments to the caller's day, and
    write the result. Returns the plan (see the planners above) with
    project names resolved for display, plus `ping_next` enriched into
    the reminder the caller should send.
    """
    planner = PLANNERS.get(action)
    if planner is None:
        raise DesignError(f"Unknown adjustment: {action}")

    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise DesignError(
            "You're not registered in the system. Send /start first."
        )

    today = now()
    blocks = at.get_design_blocks_for_day(today.date().isoformat())

    # Invariant 1: linked-record fields can't be filtered server-side by
    # record ID (formulas see the primary field value), so the designer
    # filter happens here, on the record IDs the REST API returns.
    mine = [b for b in blocks if member["id"] in (b["fields"].get("Designers") or [])]

    plan = planner(mine, today, minutes)

    for record_id, fields in plan["updates"]:
        at.update_design_block(record_id, fields)
    logger.info("%s: %d min for %s (%d block(s) moved)",
                action, minutes, member["fields"].get("Name"), len(plan["moved"]))

    names: dict[str, str] = {}
    plan["target_name"] = (
        _block_name(plan["target"], names) if plan["target"] else None
    )
    plan["moved_names"] = [
        (_block_name(block, names), start, end)
        for block, start, end in plan["moved"]
    ]

    if plan["ping_next"]:
        block, new_start, new_end = plan["ping_next"]
        # BOTH ends, or the reminder announces the block's new start
        # against its old end and overstates the duration.
        fields = {**block["fields"], "Start": _iso(new_start),
                  "End": _iso(new_end)}
        plan["ping_next"] = {
            "block_id": block["id"],
            "text": format_switch_ping(fields, _block_name(block, names)),
        }
    return plan


def note_switch_ping_sent(block_id: str) -> None:
    """Stamp a block as pinged after an out-of-band reminder (the one a
    shrink fires). Same dedupe marker the polling job writes, so the
    job never repeats it."""
    at.mark_design_block_pinged(block_id, now().isoformat())


def format_switch_ping(block_fields: dict, project_name: str) -> str:
    """
    Build the switch-reminder DM text from a Design Block's fields.

    Deliberately ONE short line: the whole point is that the phone's
    notification preview carries the full message, so no line should be
    spent on words the designer can infer ('wrap up and switch over').
    Order is time → project → duration → type, most-specific first.

    Lives here rather than in jobs/scheduler.py because a shrink fires
    this same reminder off-schedule (see `plan_shrink`), and the job
    module imports the handlers — the other direction would be a cycle.
    """
    start = parse_dt(block_fields.get("Start"))
    end = parse_dt(block_fields.get("End"))
    when = start.strftime("%H:%M") if start else "soon"
    block_type = block_fields.get("Block type") or "Work"
    if start and end:
        hours = (end - start).total_seconds() / 3600
        span = f" ({hours:g} h)"
    else:
        span = ""
    return f"📐 {when}: {project_name}{span}, {block_type}"


def format_adjustment(plan: dict) -> str:
    """The confirmation shown after an adjustment."""
    name = plan.get("target_name") or "Current block"
    minutes = plan["minutes"]

    if plan["action"] == "extend":
        lines = [f"✅ {name} extended to {plan['new_end']:%H:%M} "
                 f"(+{minutes} min)."]
        shifted = "Pushed back"
    elif plan["action"] == "shrink":
        lines = [f"✅ {name} now ends {plan['new_end']:%H:%M} "
                 f"(−{minutes} min)."]
        shifted = "Pulled earlier"
    else:
        lines = [f"☕ {minutes} min of space from "
                 f"{plan['space_from']:%H:%M}."]
        shifted = "Pushed back"

    if plan["moved_names"]:
        blocks = ", ".join(
            f"{moved_name} {start:%H:%M}–{end:%H:%M}"
            for moved_name, start, end in plan["moved_names"]
        )
        lines.append(f"{shifted}: {blocks}")
    else:
        lines.append("Nothing else moved.")
    return "\n".join(lines)
