"""
The day editor (DESIGN_SCHEDULING.md §13).

Correcting a day that didn't go to plan — mid-day and at the end of it
— without opening Airtable. Decided with Marcus 2026-09-02: editing
`Start`/`End`/`Block status` field by field was the last clunky part of
the design system, and reordering was the worst of it (Airtable has no
reorder: `Start`/`End` are absolute datetimes, so 'B actually came
before A' means retyping four timestamps in the right order, and a slip
silently overlaps two blocks with nothing flagging it).

**Every rule in here is pure.** The page holds the day as JSON and
POSTs an op; `apply_op` returns the recomputed day. No Airtable, no
server-side session state — a restart mid-edit costs nothing, and the
whole rule set is testable without network.

Why server-side rather than in the page's JavaScript, unlike the
planner's step-2 preview (§12e): that preview cost us a *hand-checked*
second implementation of `pack_blocks`, which CLAUDE.md flags as a
standing hazard. The rules here (gap-first cascade, reorder repack,
switch-now, lunch, grid) are far more than a preview's worth of
arithmetic, and a second copy of them would be a liability rather than
a latency win. The cost is one round trip per tap on a tool used a
handful of times a day; the planner's fast path is untouched.

Times travel as **SGT minutes past midnight**, the same scalar the
planner already sends as `nowMinutes` — so the page does integer
arithmetic and never touches a time zone (its own clock may be set to
anywhere). ISO conversion happens once, here, on save.

The rules, in one place:

- **±15 on a boundary resizes that boundary only.** Pushing a block's
  end into the next one cascades gap-first (core/design.py's rule:
  push the collisions, stop the ripple at the first gap that absorbs
  it). Pulling never cascades — gaps are legal, overlaps are not.
- **Reorder swaps two rows and repacks from the earlier one's start**,
  preserving each block's duration and each *position's* gap. When the
  swapped pair was contiguous this is exactly a swap and nothing else
  in the day moves; only a lunch jump can ripple further.
- **Lunch 13:00–14:00 is immovable**, as everywhere else — with one
  deliberate exception, `switch_now`, which records what is happening
  right now rather than planning something. Refusing to record the
  truth would be worse than a block that overlaps the break.
- **`Planned hours` is never written on an existing block.** The plan
  stays frozen, so every edit here registers as deviation exactly as
  the spec intends. Blocks created here are unplanned by definition and
  carry 0 (§3).
- **Nothing is deleted** (§7): a block that didn't happen is `Dropped`.
  Only rows that have never reached Airtable can be removed outright.
"""

import logging
from datetime import date as date_cls, datetime, time as time_cls, timedelta

import config
from core import airtable_client as at
from core.planning import BLOCK_TYPES
from core.timeutils import TZ, now, parse_dt

logger = logging.getLogger(__name__)

MINUTES_PER_DAY = 24 * 60

# Airtable's Block status options (§3). 'Planned' is provisional — not
# data — which is why the day-confirmation pass resolves every
# surviving Planned block into Confirmed or Adjusted.
BLOCK_STATUSES = ("Planned", "Confirmed", "Adjusted", "Dropped")

# How long an interruption is assumed to run before you say otherwise.
# Median observed block is 1.0 h (§9a), but an interruption is not a
# work block — 30 min, nudged from there.
SWITCH_DEFAULT_MINUTES = 30

# A guard on the posted payload, not a business rule: the observed
# ceiling is 2-4 blocks per designer-day (§9a).
MAX_ROWS = 60


class DayError(Exception):
    """
    A day-editing operation failed for a reason worth showing.

    The message reaches the designer verbatim in the editor, so keep it
    short and say what to do instead.
    """
    pass


# ──────────────────────────────────────────────
# Minutes <-> datetimes
# ──────────────────────────────────────────────

def _lunch_minutes() -> tuple[int, int]:
    return config.LUNCH_START_HOUR * 60, config.LUNCH_END_HOUR * 60


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """True if the two half-open intervals share any time."""
    return a_start < b_end and b_start < a_end


def _enters_lunch(old_start: int, old_end: int,
                  new_start: int, new_end: int) -> bool:
    """
    True if a move puts a block into the lunch hour that wasn't in it
    before. Pre-existing overlaps are left alone everywhere — the guard
    blocks NEW violations only, never data that already exists.
    """
    lunch_start, lunch_end = _lunch_minutes()
    return (_overlaps(new_start, new_end, lunch_start, lunch_end)
            and not _overlaps(old_start, old_end, lunch_start, lunch_end))


def _snap(minutes: int) -> int:
    """Round to the NEAREST grid point.

    Nearest rather than up (`round_up_to`, used when the planner picks
    a start) because this snaps 'now' to record something that is
    already happening: rounding up would put the cut as much as a full
    grid step into the future.
    """
    grid = config.PLAN_GRID_MINUTES
    return int(round(minutes / grid) * grid)


def _fmt(minutes: int) -> str:
    """'10:45', for error messages the designer reads."""
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def _iso_at(day: date_cls, minutes: int) -> str:
    """SGT minutes past midnight on `day` → an ISO datetime for Airtable."""
    midnight = datetime.combine(day, time_cls(0, 0), tzinfo=TZ)
    return (midnight + timedelta(minutes=minutes)).isoformat()


def _row_minutes(block: dict) -> tuple[int, int]:
    """
    A Design Block's (start, end) as SGT minutes past midnight.

    End is derived as start + duration rather than read off the wall
    clock, so a block running past midnight yields end > 1440 instead
    of a smaller number than its own start.
    """
    start = parse_dt(block["fields"].get("Start"))
    end = parse_dt(block["fields"].get("End"))
    if not start or not end:
        raise DayError("A block on this day is missing its start or end time. "
                       "Fix it in Airtable, then reopen.")
    start_minutes = start.hour * 60 + start.minute
    duration = int(round((end - start).total_seconds() / 60))
    return start_minutes, start_minutes + duration


# ──────────────────────────────────────────────
# The day, as the page holds it
# ──────────────────────────────────────────────

def _live(day: list[dict]) -> list[dict]:
    """
    The rows that occupy time, in time order.

    Dropped blocks are planned-but-didn't-happen: they are not
    obstacles, they don't move, and they take no part in any repack —
    the same exclusion `_timed_blocks` makes in core/design.py.
    """
    return sorted((row for row in day if row["status"] != "Dropped"),
                  key=lambda row: row["start"])


def sort_day(day: list[dict]) -> list[dict]:
    """Rows in time order — Dropped ones included, so they stay visible
    where they were planned rather than collecting at one end."""
    return sorted(day, key=lambda row: (row["start"], row["end"]))


def _new_row(project_id, project_name, block_type, start, end,
             status="Planned") -> dict:
    """
    A row that isn't in Airtable yet.

    `id` None is what marks it for creation on save, and `planned_hours`
    0 is the unplanned-block convention (§3): the whole of its duration
    shows up as positive deviation, which is the signal that the day
    went off-plan.
    """
    return {
        "id": None,
        "project_id": project_id,
        "project_name": project_name,
        "block_type": block_type,
        "start": start,
        "end": end,
        "status": status,
        "planned_hours": 0.0,
        "orig_start": None,
        "orig_end": None,
    }


def build_day(blocks: list[dict], project_names: dict) -> list[dict]:
    """
    Design Block records → the editor's rows.

    `orig_start`/`orig_end` are the times as loaded; save compares them
    against Airtable again and refuses if anything moved underneath the
    edit (the +15 buttons write to these same records).
    """
    rows = []
    for block in blocks:
        fields = block["fields"]
        start, end = _row_minutes(block)
        project_ids = fields.get("Project") or []
        project_id = project_ids[0] if project_ids else None
        rows.append({
            "id": block["id"],
            "project_id": project_id,
            "project_name": project_names.get(project_id, "(no project)"),
            "block_type": fields.get("Block type") or "Design",
            "start": start,
            "end": end,
            "status": fields.get("Block status") or "Planned",
            "planned_hours": float(fields.get("Planned hours") or 0),
            "orig_start": start,
            "orig_end": end,
        })
    return sort_day(rows)


def parse_day(raw) -> list[dict]:
    """
    Validate and coerce a day posted by the page.

    Everything here arrives from a browser, so nothing is trusted: the
    shape is rebuilt field by field rather than passed through. Note
    what this deliberately does NOT trust — `planned_hours` on an
    existing row is never written back (the plan is frozen), and `id`
    is checked against the caller's own blocks in `save_day`, so a
    posted record ID can't reach someone else's day.
    """
    if not isinstance(raw, list):
        raise DayError("That day didn't come through cleanly. Reopen the editor.")
    if len(raw) > MAX_ROWS:
        raise DayError(f"A day can hold at most {MAX_ROWS} blocks.")

    rows = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise DayError("That day didn't come through cleanly. Reopen the editor.")
        try:
            start = int(entry["start"])
            end = int(entry["end"])
        except (KeyError, TypeError, ValueError):
            raise DayError("A block is missing its times. Reopen the editor.")
        status = entry.get("status") or "Planned"
        if status not in BLOCK_STATUSES:
            raise DayError(f"Unknown block status: {status!r}")
        block_type = entry.get("block_type") or "Design"
        if block_type not in BLOCK_TYPES:
            raise DayError(f"Unknown block type: {block_type!r}")
        if not (0 <= start < end <= MINUTES_PER_DAY):
            raise DayError("A block has impossible times. Reopen the editor.")

        record_id = entry.get("id") or None
        rows.append({
            "id": record_id,
            "project_id": entry.get("project_id") or None,
            "project_name": str(entry.get("project_name") or "")[:120],
            "block_type": block_type,
            "start": start,
            "end": end,
            "status": status,
            "planned_hours": float(entry.get("planned_hours") or 0),
            "orig_start": (int(entry["orig_start"])
                           if entry.get("orig_start") is not None else None),
            "orig_end": (int(entry["orig_end"])
                         if entry.get("orig_end") is not None else None),
        })
    return sort_day(rows)


def check_no_overlaps(day: list[dict]) -> None:
    """
    Defence in depth before writing: two blocks claiming the same
    minutes is the silent corruption this editor exists to prevent, and
    it must never reach Airtable however the payload got here.
    """
    live = _live(day)
    for earlier, later in zip(live, live[1:]):
        if later["start"] < earlier["end"]:
            raise DayError(
                f"{earlier['project_name']} and {later['project_name']} "
                f"overlap at {_fmt(later['start'])}. Adjust one of them."
            )


# ──────────────────────────────────────────────
# The rules (pure)
# ──────────────────────────────────────────────

def _cascade(rows: list[dict], cursor: int) -> None:
    """
    Push every row that collides with `cursor`, stopping at the first
    gap wide enough to absorb what's left — core/design.py's gap-first
    rule, in minutes. Mutates the rows in place.

    End of day counts as a gap, so the ripple always terminates.
    """
    lunch_start, lunch_end = _lunch_minutes()
    for row in rows:
        if row["start"] >= cursor:
            break
        delta = cursor - row["start"]
        new_start, new_end = row["start"] + delta, row["end"] + delta
        if _enters_lunch(row["start"], row["end"], new_start, new_end):
            jump = lunch_end - new_start
            new_start, new_end = new_start + jump, new_end + jump
        if new_end > MINUTES_PER_DAY:
            raise DayError(
                f"That pushes {row['project_name']} past midnight. "
                f"Shorten or drop something first."
            )
        row["start"], row["end"] = new_start, new_end
        cursor = new_end


def nudge(day: list[dict], index: int, edge: str, delta: int) -> list[dict]:
    """
    Move one boundary of one block by `delta` minutes.

    Pushing an end into the block behind it cascades (a block ran long
    — the rest of the day gives way, gap-first). Pulling a start back
    into the block in front of it is refused instead: overlaps are
    never silently created, and there is no honest backward cascade
    (that a block starts later says nothing about when the previous one
    finished).
    """
    day = sort_day(day)
    row = _row_at(day, index)
    if row["status"] == "Dropped":
        raise DayError("That block is dropped. Un-drop it before editing its times.")

    floor = config.PLAN_MIN_BLOCK_MINUTES
    live = _live(day)
    position = _position_of(live, row)

    if edge == "end":
        new_end = row["end"] + delta
        if new_end - row["start"] < floor:
            raise DayError(
                f"A block can't be shorter than {floor} min. If it didn't "
                f"happen, drop it instead."
            )
        if new_end > MINUTES_PER_DAY:
            raise DayError("That runs past midnight.")
        if _enters_lunch(row["start"], row["end"], row["start"], new_end):
            lunch_start, lunch_end = _lunch_minutes()
            raise DayError(
                f"That would run into the {_fmt(lunch_start)}–{_fmt(lunch_end)} "
                f"lunch hour. Pick it up after lunch instead."
            )
        row["end"] = new_end
        _cascade(live[position + 1:], new_end)

    elif edge == "start":
        new_start = row["start"] + delta
        if row["end"] - new_start < floor:
            raise DayError(
                f"A block can't be shorter than {floor} min. If it didn't "
                f"happen, drop it instead."
            )
        if new_start < 0:
            raise DayError("That starts before midnight.")
        if _enters_lunch(row["start"], row["end"], new_start, row["end"]):
            lunch_start, lunch_end = _lunch_minutes()
            raise DayError(
                f"That would reach back into the {_fmt(lunch_start)}–"
                f"{_fmt(lunch_end)} lunch hour."
            )
        if position > 0 and new_start < live[position - 1]["end"]:
            previous = live[position - 1]
            raise DayError(
                f"That would overlap {previous['project_name']}, which runs "
                f"to {_fmt(previous['end'])}. Move that one first."
            )
        row["start"] = new_start

    else:
        raise DayError(f"Unknown edge: {edge!r}")

    return sort_day(day)


def move(day: list[dict], index: int, by: int) -> list[dict]:
    """
    Swap a block with its neighbour in the running order, then repack.

    The repack preserves each block's DURATION and each *position's*
    gap, anchored at the earlier of the two blocks' start (Marcus's
    call, 2026-09-02: what actually happened is that a 2 h task ran
    before a 1 h one, so the 1 h one really did finish later).

    Because the gaps belong to positions rather than to blocks, a
    contiguous pair stays contiguous and its total span is unchanged —
    so in the ordinary case *nothing after the pair moves at all*. Only
    a lunch jump can ripple past them, which is why the repack runs to
    the end of the day rather than stopping at the pair.
    """
    day = sort_day(day)
    row = _row_at(day, index)
    if row["status"] == "Dropped":
        raise DayError("Dropped blocks stay where they were planned.")

    live = _live(day)
    position = _position_of(live, row)
    target = position + by
    if not (0 <= target < len(live)):
        return day

    first = min(position, target)
    # Gaps belong to positions, not to blocks: gap[k] is the space
    # BEFORE position k as the day stands now.
    gaps = [0] + [live[k]["start"] - live[k - 1]["end"]
                  for k in range(1, len(live))]
    anchor = live[first]["start"]
    live[position], live[target] = live[target], live[position]

    lunch_start, lunch_end = _lunch_minutes()
    cursor = anchor
    for k in range(first, len(live)):
        current = live[k]
        duration = current["end"] - current["start"]
        new_start = cursor if k == first else cursor + gaps[k]
        new_end = new_start + duration
        if _overlaps(new_start, new_end, lunch_start, lunch_end):
            new_start, new_end = lunch_end, lunch_end + duration
        if new_end > MINUTES_PER_DAY:
            raise DayError(
                f"That reordering pushes {current['project_name']} past "
                f"midnight."
            )
        current["start"], current["end"] = new_start, new_end
        cursor = new_end

    return sort_day(day)


def set_status(day: list[dict], index: int, status: str) -> list[dict]:
    """
    Set one block's status. Dropping never deletes (§7) and never
    shifts the rest of the day: the hole a dropped block leaves is
    evidence, and closing it up would erase when the others actually
    ran.
    """
    if status not in BLOCK_STATUSES:
        raise DayError(f"Unknown block status: {status!r}")
    day = sort_day(day)
    row = _row_at(day, index)
    if status == "Dropped" and row["id"] is None:
        raise DayError("This block was added here and hasn't been saved yet — "
                       "remove it instead of dropping it.")
    previous, row["status"] = row["status"], status
    if status != "Dropped":
        try:
            check_no_overlaps(day)
        except DayError:
            row["status"] = previous
            raise
    return day


def remove(day: list[dict], index: int) -> list[dict]:
    """
    Remove a row that has never reached Airtable.

    Everything else is Dropped instead — deviation evidence is never
    deleted (§7).
    """
    day = sort_day(day)
    row = _row_at(day, index)
    if row["id"] is not None:
        raise DayError("Saved blocks are never deleted — drop it instead.")
    return [other for other in day if other is not row]


def add_block(day: list[dict], project_id: str, project_name: str,
              block_type: str, minutes: int, now_minutes: int) -> list[dict]:
    """
    Add an unplanned block at the end of the day, to be moved into
    place with the reorder arrows.

    One insertion rule, then reorder — rather than an insert-at-position
    control that would need its own repack semantics.
    """
    _check_block(block_type, minutes)
    day = sort_day(day)
    live = _live(day)
    start = max(live[-1]["end"], _snap(now_minutes)) if live else _snap(now_minutes)

    lunch_start, lunch_end = _lunch_minutes()
    if _overlaps(start, start + minutes, lunch_start, lunch_end):
        start = lunch_end
    if start + minutes > MINUTES_PER_DAY:
        raise DayError("That won't fit before midnight.")

    day.append(_new_row(project_id, project_name, block_type,
                        start, start + minutes))
    return sort_day(day)


def switch_now(day: list[dict], project_id: str, project_name: str,
               block_type: str, minutes: int, now_minutes: int,
               resume: bool = True) -> list[dict]:
    """
    'I'm doing this instead, starting now.'

    The delivery-arrives / lead-calls case (Marcus, 2026-09-02), and the
    one op that fixes the mid-day problem rather than easing it: it
    records the interruption *as it happens*, which is precisely what
    the evening reconstruction was failing to do (§9a).

    In order: cut the running block at the nearest grid point to now;
    insert the interruption there; optionally re-queue what was left of
    the interrupted block after it; push the rest of the day gap-first.

    A block cut back to nothing is Dropped, not left at zero length —
    'it didn't happen' is exactly what Dropped means (§3).

    **This is the one place lunch is not an obstacle.** The block is
    happening now; refusing to record it, or moving it to after the
    break, would make the tool lie. Everything it pushes still respects
    lunch.
    """
    _check_block(block_type, minutes)
    day = sort_day(day)
    live = _live(day)
    lunch_start, lunch_end = _lunch_minutes()

    running = next(
        (row for row in live if row["start"] <= now_minutes < row["end"]),
        None,
    )

    remainder = 0
    resumed_type = block_type
    resumed_project = (project_id, project_name)
    if running is not None:
        cut = _snap(now_minutes)
        resumed_type = running["block_type"]
        resumed_project = (running["project_id"], running["project_name"])
        cut = min(cut, running["end"])
        remainder = running["end"] - max(cut, running["start"])
        if cut <= running["start"]:
            # It barely started: it didn't happen, and its slot is free.
            cut = running["start"]
            remainder = running["end"] - running["start"]
            running["status"] = "Dropped"
        else:
            running["end"] = cut
    else:
        # Snapping to the NEAREST grid point can land inside a block
        # that has already finished (a block ending at 10:50, with now
        # at 10:52, snaps back to 10:45). Never start behind something
        # that already happened.
        cut = _snap(now_minutes)
        cut = max(cut, max((row["end"] for row in live if row["start"] < cut),
                           default=cut))

    inserted = _new_row(project_id, project_name, block_type,
                        cut, cut + minutes)
    if inserted["end"] > MINUTES_PER_DAY:
        raise DayError("That won't fit before midnight.")
    day.append(inserted)
    cursor = inserted["end"]
    resumed = None

    # Re-queue what was left of the interrupted block, so switching away
    # doesn't quietly delete the rest of the task. Its planned hours stay
    # on the original record, so the pair nets out to no deviation if the
    # work does get finished.
    if resume and remainder >= config.PLAN_MIN_BLOCK_MINUTES:
        start = cursor
        if _overlaps(start, start + remainder, lunch_start, lunch_end):
            start = lunch_end
        if start + remainder <= MINUTES_PER_DAY:
            resumed = _new_row(resumed_project[0], resumed_project[1],
                               resumed_type, start, start + remainder)
            day.append(resumed)
            cursor = start + remainder

    fresh = {id(inserted), id(resumed)} if resumed else {id(inserted)}
    following = [row for row in _live(day)
                 if id(row) not in fresh and row["start"] >= cut]
    _cascade(following, cursor)
    return sort_day(day)


def _check_block(block_type: str, minutes: int) -> None:
    if block_type not in BLOCK_TYPES:
        raise DayError(f"Unknown block type: {block_type!r}")
    if not (config.PLAN_MIN_BLOCK_MINUTES <= minutes
            <= config.PLAN_MAX_BLOCK_MINUTES):
        raise DayError(
            f"Block length must be between {config.PLAN_MIN_BLOCK_MINUTES} "
            f"and {config.PLAN_MAX_BLOCK_MINUTES} minutes."
        )
    if minutes % config.PLAN_GRID_MINUTES:
        raise DayError(f"Blocks sit on a {config.PLAN_GRID_MINUTES} min grid.")


def _position_of(rows: list[dict], row: dict) -> int:
    """Index of `row` in `rows` by IDENTITY.

    list.index() would compare by value, and two rows of a day can be
    value-identical (same project, type and length, different times are
    the only difference — and a swap makes even those coincide).
    """
    for index, candidate in enumerate(rows):
        if candidate is row:
            return index
    raise DayError("That block is no longer there. Reopen the editor.")


def _row_at(day: list[dict], index: int) -> dict:
    try:
        return day[int(index)]
    except (IndexError, TypeError, ValueError):
        raise DayError("That block is no longer there. Reopen the editor.")


OPS = {
    "nudge": lambda day, op: nudge(day, op["index"], op["edge"],
                                   int(op["delta"])),
    "move": lambda day, op: move(day, op["index"], int(op["by"])),
    "status": lambda day, op: set_status(day, op["index"], op["status"]),
    "remove": lambda day, op: remove(day, op["index"]),
    "add": lambda day, op: add_block(
        day, op["project_id"], op.get("project_name", ""), op["block_type"],
        int(op["minutes"]), int(op["now_minutes"])),
    "switch": lambda day, op: switch_now(
        day, op["project_id"], op.get("project_name", ""), op["block_type"],
        int(op["minutes"]), int(op["now_minutes"]), bool(op.get("resume", True))),
}


def apply_op(day: list[dict], op: dict) -> list[dict]:
    """Run one editor op over a posted day. Pure: no Airtable, no state."""
    if not isinstance(op, dict):
        raise DayError("That didn't come through cleanly. Try again.")
    handler = OPS.get(op.get("kind"))
    if handler is None:
        raise DayError(f"Unknown action: {op.get('kind')!r}")
    try:
        return handler(day, op)
    except (KeyError, TypeError, ValueError):
        raise DayError("That didn't come through cleanly. Try again.")


# ──────────────────────────────────────────────
# Confirming the day
# ──────────────────────────────────────────────

def resolve_statuses(day: list[dict]) -> list[dict]:
    """
    Turn every surviving `Planned` block into Confirmed or Adjusted.

    Derived rather than asked (Marcus, 2026-09-02): the two differ only
    in whether the block ran to its planned length, which is already
    `Deviation (hours)` — so the confirmation tap that carried no
    information is simply removed. A block added here had no plan to
    deviate from, so it confirms as-is.

    Both statuses count identically toward `Confirmed designer-hours`;
    the distinction is for reading the day back, not for the maths.
    """
    day = sort_day(day)
    for row in day:
        if row["status"] != "Planned":
            continue
        if row["id"] is None:
            row["status"] = "Confirmed"
            continue
        actual = (row["end"] - row["start"]) / 60
        planned = row["planned_hours"]
        # Half a minute of tolerance: these are grid multiples, so any
        # real difference is at least 15 min.
        row["status"] = "Adjusted" if abs(actual - planned) > 1 / 120 else "Confirmed"
    return day


# ──────────────────────────────────────────────
# Loading and saving (the only Airtable I/O)
# ──────────────────────────────────────────────

def _my_blocks(member_id: str, day_iso: str) -> list[dict]:
    """
    One designer's Design Blocks for a day.

    Invariant 1: `Designers` is a linked field, so a formula can't
    filter it by record ID — the day is filtered server-side and the
    designer client-side, on the record IDs the REST API returns.
    """
    blocks = at.get_design_blocks_for_day(day_iso)
    return [b for b in blocks
            if member_id in (b["fields"].get("Designers") or [])]


def load_day(telegram_id: int, day_iso: str = None) -> dict:
    """Everything the editor needs to open: the day, and who owns it."""
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise DayError("You're not registered in the system. Send /start first.")

    today = now()
    day_iso = day_iso or today.date().isoformat()
    blocks = _my_blocks(member["id"], day_iso)
    project_names = {
        record_id: (record["fields"].get("Project name") or "(unnamed)")
        for record_id, record in at.get_all_projects_indexed().items()
    }
    day_record = at.get_design_day(member["id"], day_iso)

    return {
        "member": member,
        "date": day_iso,
        "rows": build_day(blocks, project_names),
        "day_status": ((day_record["fields"].get("Day status") or "Draft")
                       if day_record else "Draft"),
    }


def save_day(telegram_id: int, day_iso: str, rows: list[dict],
             confirm: bool = False) -> dict:
    """
    Write an edited day: one batched update, one batched create.

    Refuses if any block moved in Airtable since the editor loaded it.
    The +15/−15 buttons write to these same records, so a stale editor
    silently clobbering a live adjustment is a real path, and times are
    exactly the thing that must not be lost quietly.
    """
    member = at.get_member_by_telegram_id(telegram_id)
    if not member:
        raise DayError("You're not registered in the system. Send /start first.")

    if confirm:
        rows = resolve_statuses(rows)
    check_no_overlaps(rows)

    current = {block["id"]: block for block in _my_blocks(member["id"], day_iso)}
    day = date_cls.fromisoformat(day_iso)

    updates, creates = [], []
    for row in rows:
        if row["id"] is None:
            if not row["project_id"]:
                raise DayError("Every added block needs a project.")
            creates.append({
                # No primary-field write: Design Blocks' primary field
                # is 'Start', which this sets (§12b).
                "Project": [row["project_id"]],
                "Designers": [member["id"]],
                "Block type": row["block_type"],
                "Block status": row["status"],
                "Start": _iso_at(day, row["start"]),
                "End": _iso_at(day, row["end"]),
                # Unplanned by definition (§3) — the whole duration
                # reads as deviation, which is the point.
                "Planned hours": 0,
            })
            continue

        block = current.get(row["id"])
        if block is None:
            raise DayError("A block was removed in Airtable while you were "
                           "editing. Reopen the editor.")
        live_start, live_end = _row_minutes(block)
        if (row["orig_start"], row["orig_end"]) != (live_start, live_end):
            raise DayError(
                f"{row['project_name']} moved to {_fmt(live_start)}–"
                f"{_fmt(live_end)} while you were editing. Reopen the editor "
                f"so you don't overwrite that."
            )

        fields = {}
        if row["start"] != live_start:
            fields["Start"] = _iso_at(day, row["start"])
            # Its switch reminder was for the old time; without this the
            # dedupe stamp silently suppresses the ping at the new one.
            fields["Switch ping sent"] = None
        if row["end"] != live_end:
            fields["End"] = _iso_at(day, row["end"])
        if row["status"] != (block["fields"].get("Block status") or "Planned"):
            fields["Block status"] = row["status"]
        # 'Planned hours' is never written here — the plan is frozen.
        if fields:
            updates.append({"id": row["id"], "fields": fields})

    if updates:
        at.batch_update_design_blocks(updates)

    # Resolved at most once per save: creating blocks and confirming the
    # day both need it, and Airtable's rate limit is ~5 req/s
    # (invariant 7).
    day_id = (_day_record_id(member["id"], day_iso, rows)
              if (creates or confirm) else None)

    if creates:
        if day_id:
            for fields in creates:
                fields["Day"] = [day_id]
        at.batch_create_design_blocks(creates)

    confirmed = False
    if confirm:
        if day_id:
            at.update_design_day(day_id, {"Day status": "Confirmed"})
            confirmed = True
        else:
            logger.warning("Day editor: no Design Day record for %s on %s; "
                           "blocks saved, day not confirmed",
                           member["fields"].get("Name"), day_iso)

    logger.info("Day editor: %s saved %s — %d update(s), %d new block(s)%s",
                member["fields"].get("Name"), day_iso, len(updates),
                len(creates), ", day confirmed" if confirmed else "")
    return {
        "updated": len(updates),
        "created": len(creates),
        "confirmed": confirmed,
        "rows": rows,
    }


def _day_record_id(member_id: str, day_iso: str, rows: list[dict]):
    """
    The designer's Design Days record, created if the day was never
    planned through the Mini App.

    Capacity has to come from somewhere in that case; the planned hours
    already on the day are the only honest answer available, and a
    declared capacity is never overwritten (`get_design_day` reads,
    `get_or_create_design_day` writes only on creation of a new one).
    """
    existing = at.get_design_day(member_id, day_iso)
    if existing:
        return existing["id"]
    planned = sum(row["planned_hours"] for row in rows) or None
    return at.get_or_create_design_day(
        member_id, day_iso, planned or config.PLAN_DEFAULT_CAPACITY_HOURS
    )


def format_day(rows: list[dict], confirmed: bool = False) -> str:
    """The confirmation DM after a saved day."""
    live = _live(rows)
    lines = ["📐 Day saved:" if not confirmed else "✅ Day confirmed:"]
    for row in sort_day(rows):
        mark = "  ✗ " if row["status"] == "Dropped" else "  "
        lines.append(
            f"{mark}{_fmt(row['start'])}–{_fmt(row['end'])}  "
            f"{row['project_name']} ({row['block_type']})"
        )
    total = sum(row["end"] - row["start"] for row in live) / 60
    lines.append(f"\n{len(live)} block(s), {total:g} h.")
    return "\n".join(lines)
