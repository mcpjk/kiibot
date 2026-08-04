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

from core import airtable_client as at

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
    correction was dropped.
    """
    pending = at.get_pending_edit_requests()
    if pending:
        raise PayrollError(
            f"{len(pending)} edit request(s) still pending. Approve or "
            f"reject them first, then lock the month."
        )

    shifts = at.get_shifts_for_payroll(pay_month)
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
