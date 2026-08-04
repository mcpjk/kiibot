"""Unit tests for core business logic (no network — fake Airtable layer)."""

from datetime import date, datetime, timedelta, timezone

import pytest

from core import shifts, edits
from core.timeutils import TZ, parse_dt, fmt_dt, now, lunch_overlap_hours
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


# ── lunch deduction (13:00–14:00 SGT) ────────

def _sgt(day, hour, minute=0):
    return datetime(2026, 8, day, hour, minute, tzinfo=TZ)


def test_lunch_full_overlap_deducts_one_hour():
    assert lunch_overlap_hours(_sgt(3, 9), _sgt(3, 18)) == pytest.approx(1.0)


def test_lunch_partial_overlaps():
    assert lunch_overlap_hours(_sgt(3, 9), _sgt(3, 13, 30)) == pytest.approx(0.5)
    assert lunch_overlap_hours(_sgt(3, 13, 30), _sgt(3, 18)) == pytest.approx(0.5)
    assert lunch_overlap_hours(_sgt(3, 13, 15), _sgt(3, 13, 45)) == pytest.approx(0.5)


def test_lunch_no_overlap_deducts_nothing():
    assert lunch_overlap_hours(_sgt(3, 9), _sgt(3, 12)) == 0.0
    assert lunch_overlap_hours(_sgt(3, 14), _sgt(3, 18)) == 0.0


def test_lunch_utc_inputs_use_sgt_wall_clock():
    # 05:00–06:00 UTC == 13:00–14:00 SGT: a UTC-expressed shift spanning
    # it must still be deducted
    start = datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc)   # 09:00 SGT
    end = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)    # 18:00 SGT
    assert lunch_overlap_hours(start, end) == pytest.approx(1.0)


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
    start_dt = now() - timedelta(hours=2)
    fake_at["shifts"].append(make_shift(member_id=m["id"],
                                        start=start_dt.isoformat(),
                                        status="Open", rate=10.0))
    result = shifts.clock_out(111)
    # The fallback deducts lunch too; when this test runs across the
    # 13:00-14:00 SGT window, expect the same deduction.
    expected = 2.0 - lunch_overlap_hours(start_dt, now())
    assert result["duration_hours"] == pytest.approx(expected, abs=0.02)
    assert result["gross_pay"] == pytest.approx(expected * 10.0, abs=0.2)


def test_clock_out_reports_lunch_from_airtable_formula(fake_at):
    m = make_member(telegram_id=111)
    fake_at["members"].append(m)
    # 'Lunch (hours)' is stored in seconds (Airtable duration field).
    fake_at["shifts"].append(make_shift(
        member_id=m["id"], status="Open",
        **{"Duration (hours)": 8.0, "Gross pay (SGD)": 120.0,
           "Lunch (hours)": 3600},
    ))
    result = shifts.clock_out(111)
    assert result["lunch_hours"] == 1.0  # converted seconds -> hours
    assert result["duration_hours"] == 8.0  # already net of lunch


def test_clock_out_no_lunch_field_reports_zero(fake_at):
    """Until the Airtable field exists, lunch_hours must default to 0
    (no marker shown), not crash."""
    m = make_member(telegram_id=111)
    fake_at["members"].append(m)
    fake_at["shifts"].append(make_shift(
        member_id=m["id"], status="Open",
        **{"Duration (hours)": 3.0, "Gross pay (SGD)": 45.0},
    ))
    result = shifts.clock_out(111)
    assert result["lunch_hours"] == 0


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


# ── group membership audit ───────────────────

from core.membership import classify_members  # noqa: E402


def _roster():
    return [
        make_member("recA", name="ActiveIn", telegram_id=1),
        make_member("recB", name="ActiveOut", telegram_id=2),
        make_member("recC", name="GoneButLingering", telegram_id=3,
                    status="Inactive"),
        make_member("recD", name="FullTimer", telegram_id=4,
                    employment="Full-time", weekly=False),
        make_member("recE", name="Boss", telegram_id=5,
                    employment="Full-time", admin=True),
        make_member("recF", name="NoTelegram", telegram_id=None),
        make_member("recG", name="ExBoss", telegram_id=7, status="Inactive",
                    employment="Full-time", admin=True),
        make_member("recH", name="NewPending", telegram_id=8, status="Pending"),
    ]


def _classify(in_group, recent):
    return classify_members(_roster(), in_group, recent)


ALL_IN_GROUP = {1, 2, 3, 4, 5, 7, 8}


def test_audit_removes_inactive_member_in_group():
    result = _classify(ALL_IN_GROUP, {"recA", "recB"})
    assert [i["name"] for i in result["to_remove"]] == ["GoneButLingering"]


def test_audit_never_removes_admins():
    result = _classify(ALL_IN_GROUP, set())
    assert [i["name"] for i in result["inactive_admins"]] == ["ExBoss"]
    assert all(i["name"] != "ExBoss" for i in result["to_remove"])


def test_audit_reports_active_member_missing_from_group():
    result = _classify(ALL_IN_GROUP - {2}, {"recA", "recB"})
    assert [i["name"] for i in result["missing"]] == ["ActiveOut"]


def test_audit_flags_stale_part_time_members_only():
    # recB has no recent shift; Full-time members must never be flagged
    result = _classify(ALL_IN_GROUP, {"recA", "recF"})
    assert [i["name"] for i in result["stale"]] == ["ActiveOut"]


def test_audit_ignores_pending_and_reports_missing_telegram_id():
    result = _classify(ALL_IN_GROUP, {"recA", "recB", "recF"})
    assert [i["name"] for i in result["no_telegram"]] == ["NoTelegram"]
    names = [i["name"] for group in result.values() for i in group]
    assert "NewPending" not in names


# ── morning score snapshots ──────────────────

def _candidate(name, score, tier="P2", days=3, due="2026-08-10",
               status="Confirmed", touched=False):
    return {"id": "recX", "fields": {
        "Project name": name, "Priority score": score, "Priority tier": tier,
        "Days since touch": days, "Design Due": due, "Status": status,
        "Touched yesterday": 1 if touched else 0,
    }}


def test_snapshot_rows_ranked_by_score_desc():
    from core.snapshots import build_snapshot_rows

    rows = build_snapshot_rows(
        [_candidate("Low", 10), _candidate("High", 46), _candidate("Mid", 30)],
        datetime(2026, 8, 4, 6, 5, tzinfo=TZ),
    )
    assert [(r[1], r[2]) for r in rows] == [(1, "High"), (2, "Mid"), (3, "Low")]
    assert all(r[0] == "2026-08-04" for r in rows)


def test_snapshot_rows_log_score_inputs():
    """Inputs (not just the total) must be logged so alternative weights
    can be tested counterfactually in the sheet."""
    from core.snapshots import build_snapshot_rows, SNAPSHOT_HEADER

    row = build_snapshot_rows(
        [_candidate("P", 46, tier="P1", days=7, due="2026-08-15",
                    status="Confirmed", touched=True)],
        datetime(2026, 8, 4, 6, 5, tzinfo=TZ),
    )[0]
    assert dict(zip(SNAPSHOT_HEADER, row)) == {
        "Date": "2026-08-04", "Rank": 1, "Project": "P", "Score": 46,
        "Tier": "P1", "Days since touch": 7, "Design Due": "2026-08-15",
        "Status": "Confirmed", "Touched yesterday": 1,
    }


def test_short_error_caps_length_for_telegram():
    """A 4096+ char error text made the reply itself fail with
    BadRequest('Message is too long'), masking the error being
    reported — seen live on /snapshot 2026-08-04."""
    from interfaces.telegram.admin_handlers import _short_error, TELEGRAM_TEXT_LIMIT

    short = _short_error(ValueError("boom"))
    assert short == "ValueError: boom"

    long = _short_error(ValueError("x" * 9000))
    assert len(long) < 4096
    assert long.endswith("…[truncated — see logs]")


def test_short_error_flattens_newlines():
    from interfaces.telegram.admin_handlers import _short_error

    assert "\n" not in _short_error(ValueError("line1\nline2"))


# ── snapshot vs actual comparison ────────────

def _block(project_id, hours, btype="Design", status="Confirmed"):
    return {"id": "recB", "fields": {
        "Project": [project_id], "Block type": btype,
        "Block status": status, "Confirmed designer-hours": hours,
    }}


_NAMES = {"p1": "Dog perch", "p2": "Espira Spring 1", "p3": "Woofer box"}


def _snap(rank, project, score):
    # SNAPSHOT_HEADER order: Date, Rank, Project, Score, ...
    return ["2026-08-03", rank, project, score, "P1", 5, "", "Confirmed", 0]


def test_comparison_marks_ranked_work_as_worked():
    from core.snapshots import build_comparison_rows, OUTCOME_WORKED

    rows = build_comparison_rows(
        [_snap(1, "Dog perch", 46)], [_block("p1", 2.5)], _NAMES, "2026-08-03")
    assert rows[0][1] == "Dog perch"
    assert rows[0][4] == 2.5              # hours (all types)
    assert rows[0][7] == OUTCOME_WORKED


def test_comparison_marks_ranked_but_untouched_as_skipped():
    from core.snapshots import build_comparison_rows, OUTCOME_SKIPPED

    rows = build_comparison_rows(
        [_snap(1, "Dog perch", 46)], [], _NAMES, "2026-08-03")
    assert rows[0][4] == 0
    assert rows[0][7] == OUTCOME_SKIPPED


def test_comparison_surfaces_work_the_score_never_ranked():
    """The blind-spot signal: work done on a project the ranking didn't
    contain at all. Without the outer join this is invisible."""
    from core.snapshots import build_comparison_rows, OUTCOME_UNRANKED

    rows = build_comparison_rows(
        [_snap(1, "Dog perch", 46)], [_block("p3", 3.0)], _NAMES, "2026-08-03")
    unranked = [r for r in rows if r[7] == OUTCOME_UNRANKED]
    assert len(unranked) == 1
    assert unranked[0][1] == "Woofer box"
    assert unranked[0][2] == ""           # no rank
    assert unranked[0][4] == 3.0          # hours


def test_comms_only_day_counts_as_worked():
    """All block types consume design capacity and move a project
    forward (Marcus, 2026-08-04) — a comms-only day IS work, and the
    Design+CAM subset is only a breakdown column."""
    from core.snapshots import build_comparison_rows, OUTCOME_WORKED

    rows = build_comparison_rows(
        [_snap(1, "Dog perch", 46)],
        [_block("p1", 1.0, btype="Client comms")],
        _NAMES, "2026-08-03")
    assert rows[0][4] == 1.0              # hours (all types)
    assert rows[0][5] == 0                # modelling hours (Design+CAM)
    assert rows[0][7] == OUTCOME_WORKED


def test_admin_and_site_blocks_also_count_as_worked():
    from core.snapshots import build_comparison_rows, OUTCOME_WORKED

    for btype in ("Admin", "Site–meeting", "Assembly"):
        rows = build_comparison_rows(
            [_snap(1, "Dog perch", 46)], [_block("p1", 0.5, btype=btype)],
            _NAMES, "2026-08-03")
        assert rows[0][7] == OUTCOME_WORKED, btype


def test_comparison_ignores_planned_and_dropped_blocks():
    from core.snapshots import build_comparison_rows, OUTCOME_SKIPPED

    rows = build_comparison_rows(
        [_snap(1, "Dog perch", 46)],
        [_block("p1", 2.0, status="Planned"), _block("p1", 1.0, status="Dropped")],
        _NAMES, "2026-08-03")
    assert rows[0][4] == 0
    assert rows[0][7] == OUTCOME_SKIPPED


def test_comparison_sums_multiple_blocks_and_lists_types():
    from core.snapshots import build_comparison_rows

    rows = build_comparison_rows(
        [_snap(1, "Dog perch", 46)],
        [_block("p1", 1.5), _block("p1", 2.0, btype="CAM"),
         _block("p1", 0.5, btype="Client comms")],
        _NAMES, "2026-08-03")
    assert rows[0][4] == 4.0              # hours: all types
    assert rows[0][5] == 3.5              # modelling subset: Design + CAM
    assert rows[0][6] == "CAM, Client comms, Design"


def test_sheet_key_accepts_bare_id_or_full_url():
    """Pasting the whole URL corrupts the API path and yields a wall of
    HTML from Google's frontend — normalise it away (seen live)."""
    from core.snapshots import sheet_key

    bare = "1a2B3c-D4e_F5g6H7i8J9k"
    assert sheet_key(bare) == bare
    assert sheet_key(
        f"https://docs.google.com/spreadsheets/d/{bare}/edit#gid=0"
    ) == bare
    assert sheet_key(f'  "{bare}"  ') == bare


def test_html_error_translated_to_readable_cause():
    from core.snapshots import _translated

    html = RuntimeError("<!DOCTYPE html><html>...pages of markup...")
    assert "HTML page instead of API data" in str(_translated(html))

    other = ValueError("plain failure")
    assert _translated(other) is other  # untouched


def test_snapshot_rows_survive_missing_fields():
    from core.snapshots import build_snapshot_rows

    rows = build_snapshot_rows([{"id": "recX", "fields": {}}],
                               datetime(2026, 8, 4, 6, 5, tzinfo=TZ))
    assert rows[0][2] == "(unnamed)" and rows[0][3] == 0


# ── design-block switch reminders ────────────

def test_format_switch_ping_renders_sgt_time_and_span():
    from jobs.scheduler import format_switch_ping

    # 06:00 UTC == 14:00 SGT
    fields = {"Start": "2026-08-03T06:00:00.000Z",
              "End": "2026-08-03T07:30:00.000Z",
              "Block type": "CAM"}
    msg = format_switch_ping(fields, "Espira Spring 1")
    assert msg == "📐 14:00: Espira Spring 1 (1.5 h), CAM"
    # One line: the notification preview must carry the whole message.
    assert "\n" not in msg


def test_format_switch_ping_survives_missing_fields():
    from jobs.scheduler import format_switch_ping

    msg = format_switch_ping({}, "(no project)")
    assert "soon" in msg and "(no project)" in msg


# ── payroll month-end (core/payroll.py) ──────

def test_previous_pay_month_is_the_month_just_ended():
    from core.payroll import previous_pay_month

    assert previous_pay_month(date(2026, 8, 4)) == "2026-07"
    assert previous_pay_month(date(2026, 8, 1)) == "2026-07"
    assert previous_pay_month(date(2026, 8, 31)) == "2026-07"


def test_previous_pay_month_rolls_back_across_new_year():
    from core.payroll import previous_pay_month

    assert previous_pay_month(date(2027, 1, 4)) == "2026-12"
    assert previous_pay_month(date(2027, 3, 1)) == "2027-02"


def test_first_weekday_of_month_when_the_first_is_a_weekday():
    from core.payroll import is_first_weekday_of_month

    # 1 Sep 2026 is a Tuesday.
    assert is_first_weekday_of_month(date(2026, 9, 1))
    assert not is_first_weekday_of_month(date(2026, 9, 2))


def test_first_weekday_of_month_skips_a_weekend_start():
    from core.payroll import is_first_weekday_of_month

    # 1 Aug 2026 is a Saturday: the prompt waits for Monday the 3rd.
    assert not is_first_weekday_of_month(date(2026, 8, 1))
    assert not is_first_weekday_of_month(date(2026, 8, 2))
    assert is_first_weekday_of_month(date(2026, 8, 3))


def test_first_weekday_fires_exactly_once_a_month():
    from core.payroll import is_first_weekday_of_month

    for year, month, days in [(2026, 8, 31), (2026, 9, 30), (2027, 1, 31)]:
        hits = [d for d in range(1, days + 1)
                if is_first_weekday_of_month(date(year, month, d))]
        assert len(hits) == 1, (year, month, hits)


def _payroll_shift(member_id, hours, gross, status="Closed"):
    return {"id": f"recS{hours}{gross}{status}", "fields": {
        "Member": [member_id], "Status": status,
        "Duration (hours)": hours, "Gross pay (SGD)": gross}}


def _patch_payroll(monkeypatch, shifts, members, pending=0, open_shifts=0):
    from core import airtable_client as at

    monkeypatch.setattr(at, "get_shifts_for_payroll", lambda m: shifts)
    monkeypatch.setattr(at, "get_all_members_indexed",
                        lambda: {m["id"]: m for m in members})
    monkeypatch.setattr(at, "get_pending_edit_requests",
                        lambda: [{}] * pending)
    monkeypatch.setattr(at, "get_all_open_shifts", lambda: [{}] * open_shifts)


def test_payroll_summary_totals_come_from_airtable_formula_fields(monkeypatch):
    from core.payroll import build_payroll_summary

    alice = make_member("recA", name="Alice", telegram_id=1)
    bob = make_member("recB", name="Bob", telegram_id=2)
    _patch_payroll(monkeypatch, [
        _payroll_shift("recA", 8.0, 120.0),
        _payroll_shift("recA", 4.0, 60.0),
        _payroll_shift("recB", 2.0, 30.0),
    ], [alice, bob])

    summary = build_payroll_summary("2026-07")
    assert summary["totals"]["Alice"]["hours"] == 12.0
    assert summary["totals"]["Alice"]["gross"] == 180.0
    assert summary["totals"]["Alice"]["shifts"] == 2
    assert summary["grand_total"] == 210.0


def test_payroll_summary_flags_auto_closed_shifts(monkeypatch):
    from core.payroll import build_payroll_summary, format_payroll_summary

    alice = make_member("recA", name="Alice", telegram_id=1)
    _patch_payroll(monkeypatch,
                   [_payroll_shift("recA", 8.0, 120.0, status="Auto-closed")],
                   [alice])

    summary = build_payroll_summary("2026-07")
    assert summary["any_auto_closed"]
    assert "auto-closed" in format_payroll_summary(summary)


def test_payroll_summary_warns_about_still_open_shifts(monkeypatch):
    """An open shift has no end time, so it's absent from the totals —
    paying without noticing would underpay someone."""
    from core.payroll import build_payroll_summary, format_payroll_summary

    alice = make_member("recA", name="Alice", telegram_id=1)
    _patch_payroll(monkeypatch, [_payroll_shift("recA", 8.0, 120.0)],
                   [alice], open_shifts=1)

    text = format_payroll_summary(build_payroll_summary("2026-07"))
    assert "still open" in text


def test_payroll_summary_empty_month_reports_nothing_found(monkeypatch):
    from core.payroll import build_payroll_summary, format_payroll_summary

    _patch_payroll(monkeypatch, [], [])
    summary = build_payroll_summary("2026-07")
    assert summary["totals"] == {}
    assert "No completed shifts" in format_payroll_summary(summary)


def test_lock_month_refuses_while_edits_are_pending(monkeypatch):
    from core.payroll import PayrollError, lock_month

    alice = make_member("recA", name="Alice", telegram_id=1)
    _patch_payroll(monkeypatch, [_payroll_shift("recA", 8.0, 120.0)],
                   [alice], pending=2)

    with pytest.raises(PayrollError, match="pending"):
        lock_month("2026-07")


def test_lock_month_locks_only_completed_shifts(monkeypatch):
    from core import airtable_client as at
    from core.payroll import lock_month

    alice = make_member("recA", name="Alice", telegram_id=1)
    shifts = [
        _payroll_shift("recA", 8.0, 120.0, status="Closed"),
        _payroll_shift("recA", 4.0, 60.0, status="Auto-closed"),
        _payroll_shift("recA", 2.0, 30.0, status="Locked"),   # already locked
        _payroll_shift("recA", 1.0, 15.0, status="Open"),     # no end time
    ]
    _patch_payroll(monkeypatch, shifts, [alice])
    written = []
    monkeypatch.setattr(at, "batch_update_shifts", lambda recs: written.extend(recs))

    assert lock_month("2026-07") == 2
    assert all(r["fields"]["Status"] == "Locked" for r in written)


def test_lock_month_with_nothing_to_lock_raises(monkeypatch):
    from core.payroll import PayrollError, lock_month

    _patch_payroll(monkeypatch, [], [])
    with pytest.raises(PayrollError, match="No unlocked"):
        lock_month("2026-07")


def test_payroll_access_is_explicit(monkeypatch):
    from core.payroll import has_payroll_access

    assert has_payroll_access(make_member(admin=True))
    assert not has_payroll_access(make_member())
    assert not has_payroll_access(None)

    handler = make_member()
    handler["fields"]["Payroll handler"] = True
    assert has_payroll_access(handler)

    # Job function must never grant it (Role is job function only).
    designer = make_member(role="Designer")
    assert not has_payroll_access(designer)


def test_payroll_handlers_fall_back_to_admins(monkeypatch):
    """Before anyone is ticked, the prompt still reaches someone rather
    than silently going nowhere."""
    from core import airtable_client as at
    from core.payroll import get_payroll_handlers

    admin = make_member("recAdmin", name="Marcus", telegram_id=9, admin=True)
    monkeypatch.setattr(at, "get_payroll_handler_members", lambda: [])
    monkeypatch.setattr(at, "get_admin_members", lambda: [admin])
    assert get_payroll_handlers() == [admin]


def test_payroll_handlers_ignores_inactive_members(monkeypatch):
    from core import airtable_client as at
    from core.payroll import get_payroll_handlers

    active = make_member("recA", name="Alice", telegram_id=1)
    active["fields"]["Payroll handler"] = True
    gone = make_member("recB", name="Bob", telegram_id=2, status="Inactive")
    gone["fields"]["Payroll handler"] = True

    monkeypatch.setattr(at, "get_payroll_handler_members", lambda: [active, gone])
    monkeypatch.setattr(at, "get_admin_members", lambda: [])
    assert get_payroll_handlers() == [active]


# ── /extend cascade (core/design.py) ─────────

def _dblock(rec_id, start_h, end_h, status="Planned", project=None,
           block_type="Design", pinged=None):
    """A Design Block on 2026-08-04, times given as SGT hours (floats)."""
    def sgt(h):
        return datetime(2026, 8, 4, int(h), int(round((h % 1) * 60)),
                        tzinfo=TZ).isoformat()

    fields = {"Start": sgt(start_h), "End": sgt(end_h),
              "Block status": status, "Block type": block_type,
              "Designers": ["recMEMBER000000001"]}
    if project:
        fields["Project"] = [project]
    if pinged:
        fields["Switch ping sent"] = pinged
    return {"id": rec_id, "fields": fields}


def _at_sgt(h):
    return datetime(2026, 8, 4, int(h), int(round((h % 1) * 60)), tzinfo=TZ)


def _ends(updates, rec_id):
    """The (start, end) an update writes for a record, as SGT datetimes."""
    fields = dict(updates)[rec_id]
    return (parse_dt(fields.get("Start")), parse_dt(fields.get("End")))


def test_extend_into_free_time_moves_nothing_else():
    from core.design import plan_extension

    # Next block starts at 15:30, so the extra 30 min lands in the gap.
    blocks = [_dblock("recA", 14, 15), _dblock("recB", 15.5, 16.5)]
    plan = plan_extension(blocks, _at_sgt(14.75), 30)

    assert plan["target"]["id"] == "recA"
    assert plan["new_end"] == _at_sgt(15.5)
    assert [rec_id for rec_id, _ in plan["updates"]] == ["recA"]
    assert plan["moved"] == []


def test_extend_ripples_through_contiguous_blocks_then_stops_at_gap():
    from core.design import plan_extension

    # 14:00–15:00 running, 15:00–16:00 back-to-back, 16:30–17:00 after a gap.
    blocks = [_dblock("recA", 14, 15), _dblock("recB", 15, 16),
              _dblock("recC", 16.5, 17)]
    plan = plan_extension(blocks, _at_sgt(14.9), 30)

    assert _ends(plan["updates"], "recB") == (_at_sgt(15.5), _at_sgt(16.5))
    # recC's 30-min gap absorbs the delay: it keeps its planned times.
    assert "recC" not in dict(plan["updates"])
    assert len(plan["moved"]) == 1


def test_extend_clears_switch_ping_stamp_on_moved_blocks():
    from core.design import plan_extension

    # Without clearing, the dedupe stamp suppresses the reminder at the
    # block's NEW start time — the ping would be lost silently.
    blocks = [_dblock("recA", 14, 15),
              _dblock("recB", 15, 16, pinged="2026-08-04T06:55:00.000Z")]
    plan = plan_extension(blocks, _at_sgt(14.9), 30)

    assert dict(plan["updates"])["recB"]["Switch ping sent"] is None
    # The running block doesn't move, so its own stamp stays put.
    assert "Switch ping sent" not in dict(plan["updates"])["recA"]


def test_extend_pushes_a_colliding_block_past_the_lunch_hour():
    from core.design import plan_extension

    # 12:30–13:00 pushed by 30 min would sit inside 13:00–14:00 lunch;
    # the shop breaks together, so it jumps to after lunch instead.
    blocks = [_dblock("recA", 12, 12.5), _dblock("recB", 12.5, 13)]
    plan = plan_extension(blocks, _at_sgt(12.4), 30)

    assert _ends(plan["updates"], "recB") == (_at_sgt(14), _at_sgt(14.5))


def test_extend_refuses_to_run_into_lunch():
    from core.design import DesignError, plan_extension

    blocks = [_dblock("recA", 12, 12.75)]
    with pytest.raises(DesignError, match="lunch"):
        plan_extension(blocks, _at_sgt(12.5), 30)


def test_extend_allows_a_block_already_planned_through_lunch():
    from core.design import plan_extension

    # Pre-existing lunch overlap isn't this guard's business — it only
    # blocks NEW violations, so odd historical data stays extendable.
    blocks = [_dblock("recA", 12.5, 13.5)]
    plan = plan_extension(blocks, _at_sgt(13), 30)
    assert plan["new_end"] == _at_sgt(14)


def test_extend_end_of_day_block_just_runs_later():
    from core.design import plan_extension

    blocks = [_dblock("recA", 17, 18)]
    plan = plan_extension(blocks, _at_sgt(17.5), 30)
    assert plan["new_end"] == _at_sgt(18.5)
    assert plan["moved"] == []


def test_extend_requires_a_running_dblock():
    from core.design import DesignError, plan_extension

    blocks = [_dblock("recA", 14, 15)]
    with pytest.raises(DesignError, match="running"):
        plan_extension(blocks, _at_sgt(15.25), 30)


def test_extend_ignores_dropped_blocks():
    from core.design import DesignError, plan_extension

    # Dropped = planned-but-didn't-happen: never an obstacle, never a
    # target (§7 — they're kept as deviation evidence, not schedule).
    blocks = [_dblock("recA", 14, 15), _dblock("recB", 15, 16, status="Dropped"),
              _dblock("recC", 16, 17)]
    plan = plan_extension(blocks, _at_sgt(14.5), 30)
    assert "recB" not in dict(plan["updates"])
    assert "recC" not in dict(plan["updates"])  # 15:30 end clears 16:00 start

    with pytest.raises(DesignError, match="running"):
        plan_extension([_dblock("recD", 14, 15, status="Dropped")], _at_sgt(14.5))


def test_extend_never_writes_planned_slots():
    from core.design import plan_extension

    blocks = [_dblock("recA", 14, 15), _dblock("recB", 15, 16)]
    plan = plan_extension(blocks, _at_sgt(14.5), 30)
    for _, fields in plan["updates"]:
        assert "Planned slots" not in fields   # the plan stays frozen
        assert "Block status" not in fields    # the evening pass owns it


def test_extend_rejects_absurd_durations():
    from core.design import DesignError, plan_extension

    blocks = [_dblock("recA", 14, 15)]
    with pytest.raises(DesignError):
        plan_extension(blocks, _at_sgt(14.5), 999)


# ── availability reconcile (/availability edits) ──

def _avail_record(rec_id, d, confirmed=False):
    return {"id": rec_id, "fields": {"Date": d, "Confirmed": confirmed,
                                     "Member": ["recMEMBER000000001"]}}


def _patch_availability(monkeypatch, existing):
    from core import airtable_client as at

    created, deleted = [], []
    monkeypatch.setattr(at, "get_member_by_telegram_id",
                        lambda tid: make_member(telegram_id=tid))
    monkeypatch.setattr(at, "get_member_availability_for_week",
                        lambda mid, ws: existing)
    monkeypatch.setattr(at, "create_availability",
                        lambda mid, d: created.append(d))
    monkeypatch.setattr(at, "delete_availability",
                        lambda rid: deleted.append(rid))
    return created, deleted


def test_submit_availability_reconciles_adds_and_removes(monkeypatch):
    from core.availability import submit_availability

    existing = [_avail_record("recMON", "2026-07-20"),
                _avail_record("recTUE", "2026-07-21")]
    created, deleted = _patch_availability(monkeypatch, existing)

    result = submit_availability(111, ["2026-07-21", "2026-07-22"])
    assert created == ["2026-07-22"]      # newly ticked
    assert deleted == ["recMON"]          # deselected
    assert result["kept"] == ["2026-07-21"]
    assert sorted(result["kept"] + result["created"]) == [
        "2026-07-21", "2026-07-22"]


def test_submit_availability_initial_creates_all(monkeypatch):
    from core.availability import submit_availability

    created, deleted = _patch_availability(monkeypatch, [])
    result = submit_availability(111, ["2026-07-20", "2026-07-24"])
    assert created == ["2026-07-20", "2026-07-24"]
    assert deleted == []
    assert result["removed"] == []


def test_submit_availability_locked_once_any_day_confirmed(monkeypatch):
    """Once an admin has ticked Confirmed on any day, the roster is being
    built — member edits must be rejected, nothing created or deleted."""
    from core.availability import submit_availability, AvailabilityError

    existing = [_avail_record("recMON", "2026-07-20", confirmed=True),
                _avail_record("recTUE", "2026-07-21")]
    created, deleted = _patch_availability(monkeypatch, existing)

    with pytest.raises(AvailabilityError, match="already being confirmed"):
        submit_availability(111, ["2026-07-22"])
    assert created == []
    assert deleted == []


def test_schedulable_members_is_the_weekly_availability_checkbox(monkeypatch):
    from core import availability
    from core import airtable_client as at

    monkeypatch.setattr(at, "get_active_members", lambda: [
        make_member("recA", name="InCycle"),
        make_member("recD", name="OptedOut", employment="Full-time", weekly=False),
        make_member("recE", name="Boss", employment="Full-time", admin=True),
    ])
    names = [m["fields"]["Name"] for m in availability.get_schedulable_members()]
    assert names == ["InCycle", "Boss"]


def test_is_admin_reads_checkbox_not_role():
    from core import airtable_client as at

    assert at.is_admin(make_member(admin=True, role="Designer"))
    assert not at.is_admin(make_member(role="admin"))  # old convention dead
    assert not at.is_admin(None)
