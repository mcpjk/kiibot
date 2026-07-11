"""Unit tests for core business logic (no network — fake Airtable layer)."""

from datetime import date, datetime, timedelta

import pytest

from core import shifts, edits
from core.timeutils import TZ, parse_dt, fmt_dt, now
from core.availability import _next_monday, get_next_week_dates
from conftest import make_member, make_shift


# ── timeutils ────────────────────────────────

def test_parse_dt_handles_airtable_z_suffix():
    # Airtable returns UTC with 'Z'; 01:00 UTC == 09:00 SGT
    dt = parse_dt("2026-07-06T01:00:00.000Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.hour == 9
    assert dt.utcoffset() == timedelta(hours=8)


def test_fmt_dt_displays_sgt_not_utc():
    assert fmt_dt("2026-07-06T01:00:00.000Z") == "06 Jul 09:00"


def test_parse_dt_garbage_returns_none():
    assert parse_dt("not-a-date") is None
    assert parse_dt("") is None


# ── clock in / out ───────────────────────────

def test_clock_in_creates_open_shift(fake_at):
    fake_at["members"].append(make_member(telegram_id=111, rate=15.0))
    result = shifts.clock_in(111)
    assert result["rate"] == 15.0
    assert len(fake_at["created"]) == 1
    assert fake_at["created"][0]["fields"]["Status"] == "Open"


def test_clock_in_rejects_unregistered(fake_at):
    with pytest.raises(shifts.ShiftError):
        shifts.clock_in(999)


def test_clock_in_rejects_double_clockin(fake_at):
    m = make_member(telegram_id=111)
    fake_at["members"].append(m)
    fake_at["shifts"].append(make_shift(member_id=m["id"], status="Open"))
    with pytest.raises(shifts.ShiftError, match="already clocked in"):
        shifts.clock_in(111)


def test_clock_in_rejects_inactive(fake_at):
    fake_at["members"].append(make_member(telegram_id=111, status="Inactive"))
    with pytest.raises(shifts.ShiftError, match="not active"):
        shifts.clock_in(111)


def test_clock_in_rejects_missing_rate(fake_at):
    fake_at["members"].append(make_member(telegram_id=111, rate=None))
    with pytest.raises(shifts.ShiftError, match="No hourly rate"):
        shifts.clock_in(111)


def test_clock_out_prefers_airtable_formula_values(fake_at):
    m = make_member(telegram_id=111)
    fake_at["members"].append(m)
    fake_at["shifts"].append(make_shift(
        member_id=m["id"], status="Open",
        **{"Duration (hours)": 8.0, "Gross pay (SGD)": 120.0},
    ))
    result = shifts.clock_out(111)
    assert result["duration_hours"] == 8.0
    assert result["gross_pay"] == 120.0
    # Shift was closed
    assert fake_at["shifts"][0]["fields"]["Status"] == "Closed"


def test_clock_out_falls_back_to_local_calc(fake_at):
    m = make_member(telegram_id=111)
    fake_at["members"].append(m)
    start = (now() - timedelta(hours=2)).isoformat()
    fake_at["shifts"].append(make_shift(member_id=m["id"], start=start,
                                        status="Open", rate=10.0))
    result = shifts.clock_out(111)
    assert result["duration_hours"] == pytest.approx(2.0, abs=0.01)
    assert result["gross_pay"] == pytest.approx(20.0, abs=0.1)


def test_clock_out_without_open_shift(fake_at):
    fake_at["members"].append(make_member(telegram_id=111))
    with pytest.raises(shifts.ShiftError, match="don't have an open shift"):
        shifts.clock_out(111)


# ── confirm shift / auto-close (the stateless cycle) ──

def test_confirm_shift_writes_confirmed_at(fake_at):
    m = make_member(telegram_id=111)
    fake_at["members"].append(m)
    fake_at["shifts"].append(make_shift(member_id=m["id"], status="Open"))
    shifts.confirm_shift(111)
    assert any("Confirmed at" in fields for _, fields in fake_at["updates"])


def test_autoclose_skips_confirmed_shift(fake_at):
    """The original bug: /confirmshift didn't prevent auto-close."""
    m = make_member(telegram_id=111)
    fake_at["members"].append(m)
    prompted = now() - timedelta(hours=1)
    confirmed = now() - timedelta(minutes=30)  # confirmed AFTER prompt
    fake_at["shifts"].append(make_shift(
        member_id=m["id"], status="Open",
        **{"Prompted at": prompted.isoformat(),
           "Confirmed at": confirmed.isoformat()},
    ))
    assert shifts.get_shifts_to_autoclose() == []


def test_autoclose_includes_unconfirmed_prompted_shift(fake_at):
    m = make_member(telegram_id=111)
    fake_at["members"].append(m)
    prompted = now() - timedelta(hours=1)
    fake_at["shifts"].append(make_shift(
        member_id=m["id"], status="Open",
        **{"Prompted at": prompted.isoformat()},
    ))
    to_close = shifts.get_shifts_to_autoclose()
    assert len(to_close) == 1
    # Closes at the prompt time, not now
    assert to_close[0]["prompt_time"] == parse_dt(prompted.isoformat())


def test_autoclose_ignores_stale_confirmation(fake_at):
    """A confirmation from BEFORE tonight's prompt doesn't count."""
    m = make_member(telegram_id=111)
    fake_at["members"].append(m)
    confirmed = now() - timedelta(hours=2)
    prompted = now() - timedelta(hours=1)
    fake_at["shifts"].append(make_shift(
        member_id=m["id"], status="Open",
        **{"Prompted at": prompted.isoformat(),
           "Confirmed at": confirmed.isoformat()},
    ))
    assert len(shifts.get_shifts_to_autoclose()) == 1


def test_autoclose_skips_never_prompted_shift(fake_at):
    """Someone who clocked in after the 20:00 sweep must not be closed."""
    m = make_member(telegram_id=111)
    fake_at["members"].append(m)
    fake_at["shifts"].append(make_shift(member_id=m["id"], status="Open"))
    assert shifts.get_shifts_to_autoclose() == []


# ── edit validation ──────────────────────────

def _iso(dt):
    return dt.isoformat()


def test_edit_rejects_end_before_start():
    start = now() - timedelta(hours=2)
    with pytest.raises(edits.EditError, match="after start"):
        edits.validate_edit_times(_iso(start), _iso(start - timedelta(hours=1)))


def test_edit_rejects_future_start():
    start = now() + timedelta(days=1)
    with pytest.raises(edits.EditError, match="future"):
        edits.validate_edit_times(_iso(start), _iso(start + timedelta(hours=8)))


def test_edit_rejects_absurd_duration():
    start = now() - timedelta(days=3)
    with pytest.raises(edits.EditError, match="hours long"):
        edits.validate_edit_times(_iso(start), _iso(start + timedelta(hours=40)))


def test_edit_accepts_sane_times():
    start = now() - timedelta(hours=9)
    edits.validate_edit_times(_iso(start), _iso(start + timedelta(hours=8)))


# ── availability week math ───────────────────

def test_next_monday_from_thursday():
    thursday = date(2026, 7, 2)
    assert _next_monday(thursday) == date(2026, 7, 6)


def test_next_monday_from_monday_means_next_week():
    monday = date(2026, 7, 6)
    assert _next_monday(monday) == date(2026, 7, 13)


def test_next_week_dates_are_mon_to_sat():
    dates = get_next_week_dates(date(2026, 7, 2))
    assert len(dates) == 6
    assert dates[0].weekday() == 0  # Monday
    assert dates[-1].weekday() == 5  # Saturday


def test_week_date_bounds_are_exclusive_sun_to_mon():
    """Availability is filtered by a date range, not the fragile
    {Week starting} formula. Bounds must straddle Mon..Sun exclusively."""
    from core.airtable_client import _week_date_bounds

    lower, upper = _week_date_bounds("2026-07-13")  # a Monday
    assert lower == "2026-07-12"   # Sunday before < Monday 13th
    assert upper == "2026-07-20"   # Sunday 19th < next Monday 20th
