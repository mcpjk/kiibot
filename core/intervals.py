"""
Immovable time, as a set of spans (DESIGN_SCHEDULING.md §14).

Until 2026-09-14 the only thing the scheduling engine could not move
was the shared lunch hour, and it was hard-coded as a single window in
a dozen places across core/day.py and core/design.py. Holds (§14) make
the obstacle list *variable*: lunch, plus whatever the designer has
blocked out for meetings today. This module is the one implementation
of "what does a span run into, and where does it fit instead".

Deliberately unit-agnostic. core/day.py works in SGT minutes past
midnight and core/design.py in tz-aware datetimes; both only need
`<`, `-` and `+`, so the same four functions serve both and there is
no second copy of the jump rule to keep in step. Nothing here touches
Airtable, the clock, or a time zone.

A span is a half-open `(start, end)` pair: touching ends don't overlap,
so a block ending exactly at 13:00 is clear of a 13:00–14:00 lunch.
"""


def overlaps(a_start, a_end, b_start, b_end) -> bool:
    """True if two half-open spans share any time."""
    return a_start < b_end and b_start < a_end


def merge(spans: list) -> list:
    """
    Sorted, non-overlapping spans. Two holds that touch or overlap
    become one, so `clear` can't bounce out of one and into the next
    without noticing.
    """
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def first_hit(start, end, spans: list):
    """The first span this one runs into, or None. `spans` must be
    merged (see `merge`)."""
    for span in spans:
        if overlaps(start, end, span[0], span[1]):
            return span
    return None


def newly_hit(old_start, old_end, new_start, new_end, spans: list):
    """
    The span a MOVE puts a block into that it wasn't already in.

    Pre-existing overlaps are left alone everywhere in this codebase —
    a block that was already planned through lunch stays that way, and
    a guard that refused to touch it would strand it. So only new
    violations count.
    """
    hit = first_hit(new_start, new_end, spans)
    if hit is None:
        return None
    return None if overlaps(old_start, old_end, hit[0], hit[1]) else hit


def clear(start, duration, spans: list):
    """
    The earliest start at or after `start` where a span of `duration`
    fits between the obstacles.

    Loops rather than jumping once: two holds with a 10-minute crack
    between them would otherwise let a 30-minute block land in the
    crack. `merge` collapses the adjacent ones; this handles the rest.
    """
    cursor = start
    while True:
        hit = first_hit(cursor, cursor + duration, spans)
        if hit is None:
            return cursor
        cursor = hit[1]
