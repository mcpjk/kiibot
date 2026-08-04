"""
Design-block scheduling logic (see DESIGN_SCHEDULING.md).

Currently: /extend — "give the task I'm on 30 more minutes".

Why this needs a cascade at all: observed blocks are almost always
back-to-back (3 Aug: 10:30→11:30→12:30→13:00 SGT contiguous), so an
extension nearly always collides with the next planned block rather
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

`Planned slots` is never touched — the plan stays frozen, so extending
registers as deviation exactly as the spec intends (§1). What /extend
really buys is actuals captured *as they happen*, which is the evening
pass's observed failure point (§9a).
"""

import logging
from datetime import datetime, timedelta

from core import airtable_client as at
from core.timeutils import TZ, now, parse_dt, lunch_window
import config

logger = logging.getLogger(__name__)

# Default extension. A machining overrun is the motivating case, and
# half an hour is the planning grid (§7).
EXTEND_MINUTES = 30
MIN_EXTEND_MINUTES = 5
MAX_EXTEND_MINUTES = 240


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


def plan_extension(blocks: list[dict], at_time: datetime,
                   minutes: int = EXTEND_MINUTES) -> dict:
    """
    Work out the writes an extension implies. Pure — no Airtable I/O,
    so the cascade is testable without network.

    `blocks` must already be narrowed to ONE designer's blocks for the
    day. Returns {"target", "new_end", "updates", "moved"} where
    `updates` is a list of (record_id, fields) ready to write.
    """
    if not (MIN_EXTEND_MINUTES <= minutes <= MAX_EXTEND_MINUTES):
        raise DesignError(
            f"Extension must be between {MIN_EXTEND_MINUTES} and "
            f"{MAX_EXTEND_MINUTES} minutes."
        )

    at_time = at_time.astimezone(TZ)
    timed = _timed_blocks(blocks)

    # "The block you're in" — the same rule whether /extend is typed or
    # the ping's button is tapped. Note the ping announces the *next*
    # block while the button extends the *running* one; that asymmetry
    # is the point (you're overrunning the current task), and this rule
    # still does the right thing if the button is tapped late, after
    # the announced block has already started.
    target_index = next(
        (i for i, (_, start, end) in enumerate(timed) if start <= at_time < end),
        None,
    )
    if target_index is None:
        raise DesignError(
            "No design block is running right now — /extend adds time to "
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

    updates = [(target["id"], {"End": _iso(new_end)})]
    moved = []

    # Ripple forward, stopping at the first gap wide enough to absorb
    # what's left of the delay.
    cursor = new_end
    for block, start, end in timed[target_index + 1:]:
        if start >= cursor:
            break  # a gap (or lunch, or the end of the day) swallows it

        delta = cursor - start
        new_start, block_new_end = start + delta, end + delta

        b_lunch_start, b_lunch_end = lunch_window(new_start)
        if (_overlaps(new_start, block_new_end, b_lunch_start, b_lunch_end)
                and not _overlaps(start, end, b_lunch_start, b_lunch_end)):
            jump = b_lunch_end - new_start
            new_start, block_new_end = new_start + jump, block_new_end + jump

        updates.append((block["id"], {
            "Start": _iso(new_start),
            "End": _iso(block_new_end),
            # Its switch reminder was for the old time. Without this the
            # dedupe stamp silently suppresses the ping at the new one.
            "Switch ping sent": None,
        }))
        moved.append((block, new_start, block_new_end))
        cursor = block_new_end

    return {
        "target": target,
        "new_end": new_end,
        "minutes": minutes,
        "updates": updates,
        "moved": moved,
    }


def extend_current_block(telegram_id: int, minutes: int = EXTEND_MINUTES) -> dict:
    """
    Extend the design block the caller is currently in, cascading the
    rest of their day. Returns the plan (see plan_extension) with
    project names resolved for display.
    """
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

    plan = plan_extension(mine, today, minutes)

    for record_id, fields in plan["updates"]:
        at.update_design_block(record_id, fields)
    logger.info("Extended block %s by %d min for %s (%d block(s) moved)",
                plan["target"]["id"], minutes,
                member["fields"].get("Name"), len(plan["moved"]))

    names: dict[str, str] = {}
    plan["target_name"] = _block_name(plan["target"], names)
    plan["moved_names"] = [
        (_block_name(block, names), start, end)
        for block, start, end in plan["moved"]
    ]
    return plan


def format_extension(plan: dict) -> str:
    """The confirmation DM after a successful extension."""
    lines = [
        f"✅ {plan['target_name']} extended to {plan['new_end']:%H:%M} "
        f"(+{plan['minutes']} min)."
    ]
    if plan["moved_names"]:
        pushed = ", ".join(
            f"{name} {start:%H:%M}–{end:%H:%M}"
            for name, start, end in plan["moved_names"]
        )
        lines.append(f"Pushed back: {pushed}")
    else:
        lines.append("Nothing else moved.")
    return "\n".join(lines)
