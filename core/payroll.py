"""
Payroll business logic: the month-end summary and the month lock.

Lives in core/ (not the handler) because three callers need the same
code path: the /payroll and /lockmonth commands, the inline buttons on
the month-end prompt, and the monthly prompt job itself.

Money rule (CLAUDE.md invariant 6): every figure here comes from the
Airtable formula fields 'Duration (hours)' and 'Gross pay (SGD)'. This
module aggregates them and never recomputes pay from rates.
"""

import logging
from datetime import date, timedelta

import config
from core import airtable_client as at
from core.timeutils import fmt_date_short, parse_dt

logger = logging.getLogger(__name__)

# Shift statuses that a month-end lock applies to. 'Open' is absent on
# purpose: an unclosed shift has no end time, so it has no pay to lock.
LOCKABLE_STATUSES = ("Closed", "Auto-closed", "Edit-approved")


class PayrollError(Exception):
    """Raised when a payroll operation fails for a known reason."""
    pass


# ──────────────────────────────────────────────
# Pay-month arithmetic (pure)
# ──────────────────────────────────────────────

def previous_pay_month(today: date) -> str:
    """
    The pay month that just ended, as 'YYYY-MM'.

    This is /payroll's default because payroll is a month-end act: the
    current month is always incomplete, so summing it invites paying
    against a partial figure.
    """
    return (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def is_first_weekday_of_month(day: date) -> bool:
    """
    True if `day` is the month's first Mon–Fri.

    Payroll is an office task, so the prompt skips a 1st that lands on a
    weekend and fires on the Monday instead. Derived from the date
    alone — nothing is stored, so a restart can't double-prompt or lose
    the prompt.
    """
    if day.weekday() > 4:   # Sat/Sun
        return False
    first = day.replace(day=1)
    while first.weekday() > 4:
        first += timedelta(days=1)
    return day == first


# ──────────────────────────────────────────────
# Who handles payroll
# ──────────────────────────────────────────────

def get_payroll_handlers() -> list[dict]:
    """
    Active members with the 'Payroll handler' checkbox ticked — the
    people the month-end prompt goes to.

    Falls back to admins (with a warning) when nobody is ticked, so the
    feature degrades to "someone gets told" rather than silently doing
    nothing before the checkbox is first used.
    """
    handlers = [
        m for m in at.get_payroll_handler_members()
        if m["fields"].get("Status") == "Active"
    ]
    if handlers:
        return handlers
    logger.warning(
        "No Active member has 'Payroll handler' ticked — sending the "
        "month-end payroll prompt to admins instead."
    )
    return at.get_admin_members()


def has_payroll_access(member: dict | None) -> bool:
    """
    Who may run /payroll and /lockmonth: payroll handlers, plus admins
    (who keep every command they already had). Explicit checkboxes,
    never inferred from Role or Employment type.
    """
    return bool(at.is_admin(member) or at.is_payroll_handler(member))


# ──────────────────────────────────────────────
# Shift checks (pure) — run before money moves
# ──────────────────────────────────────────────

def _span(start, end) -> str:
    return (f"{fmt_date_short(start.date())} "
            f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}")


def find_shift_anomalies(shifts: list[dict], members: dict[str, dict]) -> dict:
    """
    Flag shifts that are probably wrong, from any source — bot edits are
    guarded, but times edited directly in Airtable bypass every guard.

    - overlaps: two shifts of one member covering the same time. Each is
      paid in full, so the overlap is paid twice (three such pairs were
      found and paid in Aug–Sep 2026). Blocks /lockmonth.
    - short / long: under SHORT_SHIFT_MINUTES or over LONG_SHIFT_HOURS of
      clock time. Warning only — a long day can be real.

    Shifts without both times are skipped (open shifts are reported
    separately). Returns lists of human-readable lines, plus whether any
    overlap still involves an unlocked shift (a fully Locked pair can no
    longer be fixed, so it must not block a later month's lock).
    """
    def name_of(member_id):
        m = members.get(member_id)
        return m["fields"].get("Name", "Unknown") if m else "Unknown"

    by_member: dict[str, list] = {}
    short, long_ = [], []
    for s in shifts:
        f = s["fields"]
        start, end = parse_dt(f.get("Start time")), parse_dt(f.get("End time"))
        member_ids = f.get("Member", [])
        if not (start and end and member_ids):
            continue
        by_member.setdefault(member_ids[0], []).append((start, end, s))
        minutes = (end - start).total_seconds() / 60
        if minutes < config.SHORT_SHIFT_MINUTES:
            short.append(f"{name_of(member_ids[0])}: {_span(start, end)} "
                         f"({minutes:.0f} min)")
        elif minutes > config.LONG_SHIFT_HOURS * 60:
            long_.append(f"{name_of(member_ids[0])}: {_span(start, end)} "
                         f"({minutes / 60:.1f} h)")

    overlaps, blocking = [], False
    for member_id, rows in by_member.items():
        rows.sort(key=lambda r: r[0])
        for i, (a_start, a_end, a) in enumerate(rows):
            for b_start, b_end, b in rows[i + 1:]:
                if b_start >= a_end:
                    break
                overlaps.append(f"{name_of(member_id)}: {_span(a_start, a_end)} "
                                f"and {b_start.strftime('%H:%M')}–"
                                f"{b_end.strftime('%H:%M')}")
                if (a["fields"].get("Status") != "Locked"
                        or b["fields"].get("Status") != "Locked"):
                    blocking = True

    return {"overlaps": overlaps, "short": short, "long": long_,
            "overlaps_block_lock": blocking}


# ──────────────────────────────────────────────
# The summary
# ──────────────────────────────────────────────

def build_payroll_summary(pay_month: str) -> dict:
    """
    Aggregate a pay month into per-member totals.

    Returns None-free data only; an empty 'totals' means no completed
    shifts were found, which the caller reports rather than treating as
    a zero-dollar month.
    """
    shifts = at.get_shifts_for_payroll(pay_month)
    members = at.get_all_members_indexed()

    totals: dict[str, dict] = {}
    for shift in shifts:
        fields = shift["fields"]
        member_ids = fields.get("Member", [])
        member = members.get(member_ids[0]) if member_ids else None
        name = member["fields"].get("Name", "Unknown") if member else "Unknown"

        entry = totals.setdefault(
            name, {"hours": 0.0, "gross": 0.0, "shifts": 0, "auto_closed": 0}
        )
        entry["hours"] += fields.get("Duration (hours)") or 0
        entry["gross"] += fields.get("Gross pay (SGD)") or 0
        entry["shifts"] += 1
        if fields.get("Status") == "Auto-closed":
            entry["auto_closed"] += 1

    return {
        "pay_month": pay_month,
        "totals": totals,
        "anomalies": find_shift_anomalies(shifts, members),
        "grand_total": sum(t["gross"] for t in totals.values()),
        "any_auto_closed": any(t["auto_closed"] for t in totals.values()),
        "pending_edits": len(at.get_pending_edit_requests()),
        # Open shifts don't reach the summary at all (no end time, no
        # pay), so say so explicitly rather than letting someone pay a
        # month that's still missing a shift.
        "open_shifts": len(at.get_all_open_shifts()),
    }


def format_payroll_summary(summary: dict) -> str:
    """Render a summary for Telegram."""
    if not summary["totals"]:
        return f"No completed shifts found for {summary['pay_month']}."

    lines = [f"💰 Payroll summary — {summary['pay_month']}:\n"]
    for name in sorted(summary["totals"]):
        entry = summary["totals"][name]
        flag = (f" ({entry['auto_closed']} auto-closed ⚠️)"
                if entry["auto_closed"] else "")
        lines.append(
            f"{name}: {entry['hours']:.2f} hrs, ${entry['gross']:.2f} "
            f"({entry['shifts']} shifts){flag}"
        )

    lines.append(f"\nTotal: ${summary['grand_total']:.2f}")

    if summary["any_auto_closed"]:
        lines.append(
            "\n⚠️ Auto-closed shifts may have wrong end times — "
            "check them before paying, then lock the month."
        )
    if summary["pending_edits"]:
        lines.append(
            f"⚠️ {summary['pending_edits']} edit request(s) still pending review."
        )
    if summary["open_shifts"]:
        lines.append(
            f"⚠️ {summary['open_shifts']} shift(s) still open — they're not "
            f"in these totals."
        )

    anomalies = summary.get("anomalies") or {}
    if anomalies.get("overlaps"):
        lines.append("\n🚫 Overlapping shifts — the overlap is paid twice. "
                     "Fix the times in Airtable before locking:")
        lines += [f"• {line}" for line in anomalies["overlaps"]]
    if anomalies.get("short"):
        lines.append(f"\n⏱ Under {config.SHORT_SHIFT_MINUTES} min — "
                     f"often a missed clock-in:")
        lines += [f"• {line}" for line in anomalies["short"]]
    if anomalies.get("long"):
        lines.append(f"\n⏱ Over {config.LONG_SHIFT_HOURS} h — check the times:")
        lines += [f"• {line}" for line in anomalies["long"]]
    return "\n".join(lines)


# ──────────────────────────────────────────────
# The lock (terminal — see the Shift Status lifecycle)
# ──────────────────────────────────────────────

def lock_month(pay_month: str) -> int:
    """
    Set every completed shift in the pay month to 'Locked'. Terminal:
    locked shifts can't be edited afterwards, which is why the callers
    confirm first.

    Refuses while edit requests are pending — approving one after the
    lock would silently fail, and the member would never learn their
    correction was dropped — and while any unlocked shift overlaps
    another of the same member's (see find_shift_anomalies).
    """
    pending = at.get_pending_edit_requests()
    if pending:
        raise PayrollError(
            f"{len(pending)} edit request(s) still pending. Approve or "
            f"reject them first, then lock the month."
        )

    shifts = at.get_shifts_for_payroll(pay_month)

    # Locking would make an overlap permanent. The message is shown as a
    # Telegram alert (200-char limit), so it points at /payroll for the list.
    anomalies = find_shift_anomalies(shifts, at.get_all_members_indexed())
    if anomalies["overlaps_block_lock"]:
        raise PayrollError(
            f"{len(anomalies['overlaps'])} pair(s) of overlapping shifts in "
            f"{pay_month} would be paid twice. See /payroll {pay_month}, fix "
            f"the times in Airtable, then lock."
        )

    to_lock = [s for s in shifts
               if s["fields"].get("Status") in LOCKABLE_STATUSES]
    if not to_lock:
        raise PayrollError(
            f"No unlocked completed shifts found for {pay_month}."
        )

    at.batch_update_shifts(
        [{"id": s["id"], "fields": {"Status": "Locked"}} for s in to_lock]
    )
    logger.info("Locked %d shift(s) for %s", len(to_lock), pay_month)
    return len(to_lock)
