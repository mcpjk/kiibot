"""Unit tests for core business logic (no network — fake Airtable layer)."""

import json
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


def test_edit_rejects_future_end():
    start = now() - timedelta(hours=2)
    with pytest.raises(edits.EditError, match="End time can't be in the future"):
        edits.validate_edit_times(_iso(start), _iso(start + timedelta(hours=3)))


# ── edit guardrails ──────────────────────────

def _day(days_ago, hour, minute=0):
    """An SGT datetime `days_ago` days back at hour:minute."""
    d = (now() - timedelta(days=days_ago)).date()
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ)


def _closed_shift(record_id, member_id, start, end, status="Closed"):
    return make_shift(record_id=record_id, member_id=member_id,
                      start=_iso(start), end=_iso(end), status=status)


@pytest.fixture
def edit_world(fake_at):
    """Alice with two closed shifts (3 and 2 days ago, 11:00–20:00) and
    an admin, Bob."""
    alice = make_member()
    bob = make_member("recADMIN0000000001", name="Bob", telegram_id=999,
                      admin=True)
    fake_at["members"] += [alice, bob]
    fake_at["shifts"] += [
        _closed_shift("recSHIFTA", alice["id"], _day(3, 11), _day(3, 20)),
        _closed_shift("recSHIFTB", alice["id"], _day(2, 11), _day(2, 20)),
    ]
    return fake_at


def test_edit_cannot_move_a_shift_to_another_day(edit_world):
    """Editing an old shift into a missed day deleted the old day's
    shift and, if the missed day already had one, double-paid it."""
    with pytest.raises(edits.EditError, match="different day"):
        edits.submit_edit_request(111, "recSHIFTA", _iso(_day(4, 11)),
                                  _iso(_day(4, 18)), "forgot")
    assert edit_world["requests"] == []


def test_edit_refuses_to_overlap_another_shift(edit_world):
    """Same day, but over a second shift already recorded that day."""
    edit_world["shifts"].append(_closed_shift(
        "recSHIFTC", "recMEMBER000000001", _day(1, 15), _day(1, 18)))
    edit_world["shifts"].append(_closed_shift(
        "recSHIFTD", "recMEMBER000000001", _day(1, 10), _day(1, 12)))
    with pytest.raises(edits.EditError, match="overlaps your other shift"):
        edits.submit_edit_request(111, "recSHIFTD", _iso(_day(1, 10)),
                                  _iso(_day(1, 16)), "late")


def test_edit_ignores_the_shift_being_edited_and_other_members(edit_world):
    edit_world["shifts"].append(_closed_shift(
        "recOTHER", "recSOMEONEELSE0001", _day(3, 11), _day(3, 20)))
    result = edits.submit_edit_request(111, "recSHIFTA", _iso(_day(3, 10)),
                                       _iso(_day(3, 18)), "forgot")
    assert result["request"]["fields"]["Status"] == "Pending"


def test_edit_refuses_a_second_pending_request_for_the_same_shift(edit_world):
    edits.submit_edit_request(111, "recSHIFTA", _iso(_day(3, 10)),
                              _iso(_day(3, 18)), "forgot")
    with pytest.raises(edits.EditError, match="already has an edit request"):
        edits.submit_edit_request(111, "recSHIFTA", _iso(_day(3, 10)),
                                  _iso(_day(3, 17)), "forgot again")


def test_approve_refuses_a_shift_locked_since_the_request(edit_world):
    """Approving must not overwrite a Locked shift (and flip it back to
    Edit-approved); the request stays Pending for the admin to reject."""
    req = edits.submit_edit_request(111, "recSHIFTA", _iso(_day(3, 10)),
                                    _iso(_day(3, 18)), "forgot")["request"]
    shift = next(s for s in edit_world["shifts"] if s["id"] == "recSHIFTA")
    shift["fields"]["Status"] = "Locked"

    with pytest.raises(edits.EditError, match="now Locked"):
        edits.approve_edit(req["id"], 999)
    assert req["fields"]["Status"] == "Pending"
    assert shift["fields"]["Status"] == "Locked"
    assert edit_world["updates"] == []


def test_approve_rechecks_overlaps(edit_world):
    req = edits.submit_edit_request(111, "recSHIFTA", _iso(_day(3, 9)),
                                    _iso(_day(3, 20)), "early start")["request"]
    edit_world["shifts"].append(_closed_shift(
        "recLATE", "recMEMBER000000001", _day(3, 8), _day(3, 10)))
    with pytest.raises(edits.EditError, match="overlaps"):
        edits.approve_edit(req["id"], 999)
    assert req["fields"]["Status"] == "Pending"


def test_approve_applies_times_then_marks_approved(edit_world):
    req = edits.submit_edit_request(111, "recSHIFTA", _iso(_day(3, 10)),
                                    _iso(_day(3, 18)), "forgot")["request"]
    edits.approve_edit(req["id"], 999)
    shift = next(s for s in edit_world["shifts"] if s["id"] == "recSHIFTA")
    assert parse_dt(shift["fields"]["End time"]) == _day(3, 18)
    assert shift["fields"]["Status"] == "Edit-approved"
    assert req["fields"]["Status"] == "Approved"


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


def test_audit_does_not_flag_members_newer_than_the_window():
    # A part-timer who joined after the window opened has had no chance
    # to work 5 weeks — she isn't stale, she's new (Tiffany, Oct 2026).
    from datetime import datetime, timezone
    cutoff = datetime(2026, 9, 6, tzinfo=timezone.utc)
    roster = _roster()
    roster[1]["createdTime"] = "2026-10-08T03:00:00.000Z"   # ActiveOut: new
    new = classify_members(roster, ALL_IN_GROUP, {"recA", "recF"},
                           stale_cutoff=cutoff)
    assert new["stale"] == []

    roster[1]["createdTime"] = "2026-08-01T03:00:00.000Z"   # long-standing
    old = classify_members(roster, ALL_IN_GROUP, {"recA", "recF"},
                           stale_cutoff=cutoff)
    assert [i["name"] for i in old["stale"]] == ["ActiveOut"]


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


# ── daily planning (core/planning.py) ────────

def _sel(project_id, minutes, block_type="Design"):
    return {"project_id": project_id, "block_type": block_type, "minutes": minutes}


def test_planning_packs_blocks_back_to_back():
    from core.planning import pack_blocks

    blocks = pack_blocks(
        [_sel("recA", 90), _sel("recB", 45), _sel("recC", 15)],
        datetime(2026, 8, 24, 9, 0, tzinfo=TZ),
    )
    assert [(b["start"].strftime("%H:%M"), b["end"].strftime("%H:%M")) for b in blocks] == [
        ("09:00", "10:30"), ("10:30", "11:15"), ("11:15", "11:30")
    ]


def test_planning_jumps_the_lunch_hour():
    from core.planning import pack_blocks

    # 12:30 + 60 min would run to 13:30, inside the shared break.
    blocks = pack_blocks(
        [_sel("recA", 30), _sel("recB", 60)],
        datetime(2026, 8, 24, 12, 0, tzinfo=TZ),
    )
    assert blocks[0]["start"].strftime("%H:%M") == "12:00"
    assert blocks[1]["start"].strftime("%H:%M") == "14:00"
    assert blocks[1]["end"].strftime("%H:%M") == "15:00"


def test_planning_start_inside_lunch_waits_for_it_to_end():
    from core.planning import pack_blocks

    blocks = pack_blocks([_sel("recA", 30)],
                         datetime(2026, 8, 24, 13, 15, tzinfo=TZ))
    assert blocks[0]["start"].strftime("%H:%M") == "14:00"


def test_planning_block_ending_exactly_at_lunch_is_fine():
    from core.planning import pack_blocks

    blocks = pack_blocks([_sel("recA", 30)],
                         datetime(2026, 8, 24, 12, 30, tzinfo=TZ))
    assert blocks[0]["end"].strftime("%H:%M") == "13:00"


def test_planning_rejects_unknown_block_type_and_bad_length():
    from core.planning import PlanningError, pack_blocks

    start = datetime(2026, 8, 24, 9, 0, tzinfo=TZ)
    with pytest.raises(PlanningError, match="block type"):
        pack_blocks([_sel("recA", 30, block_type="Nonsense")], start)
    with pytest.raises(PlanningError, match="between"):
        pack_blocks([_sel("recA", 5)], start)


def test_planning_default_duration_splits_capacity_on_the_grid():
    from core.planning import default_minutes

    assert default_minutes(6, 4) == 90       # 6 h over 4 → 1.5 h each
    assert default_minutes(3, 2) == 90
    assert default_minutes(1, 3) == 15       # snaps to the 15-min grid
    assert default_minutes(6, 0) == 15       # nothing picked yet


def test_planning_options_sort_by_score_and_expose_hours():
    from core.planning import build_project_options

    options = build_project_options([
        {"id": "rec1", "fields": {"Project name": "Low", "Priority score": 10,
                                  "Hours consumed": 4.25}},
        {"id": "rec2", "fields": {"Project name": "High", "Priority score": 61}},
    ])
    # Score still orders the list even though it no longer rations it.
    assert [o["name"] for o in options] == ["High", "Low"]
    assert options[0]["hours"] == 0
    assert options[1]["hours"] == 4.2


def _project(name, status="Lead", process="Designing", score=None,
             lead=None, delivered=None):
    fields = {"Project name": name, "Status": status, "Process": process}
    if score is not None:
        fields["Priority score"] = score
    if lead:
        fields["Lead date"] = lead
    if delivered:
        fields["Delivered"] = delivered
    return {"id": "rec" + name, "fields": fields}


def test_planning_orders_projects_like_the_airtable_view():
    """
    Status first→last, Delivered latest→earliest, Process last→first,
    Priority score 9→1, Lead date latest→earliest — Marcus's view, so
    the picker and the base read the same way round.

    The expected order below is the live base's own answer to that sort
    (checked against Airtable 2026-08-25), reduced to the rows that
    exercise each key.
    """
    from core.planning import sort_projects

    ordered = sort_projects([
        _project("Mid century stools", "Lead", "Designing", 5, "2026-08-24"),
        _project("Photo studio reno", "Confirmed", "Fabricating", None, "2026-07-28"),
        _project("Boat speaker mount", "Lead", "Designing", 46, "2026-08-24"),
        _project("Logo canape trays", "Confirmed", "Designing", 42, "2026-07-08"),
        _project("Live station cart 2", "Confirmed", "Fabricating", None, "2026-08-16"),
        _project("Wall panel repair", "Confirmed", "Designing", 28, "2026-06-08"),
        _project("Chafing dish cladding", "Lead", "Designing", 37, "2026-06-29"),
        _project("Mini hog scoops", "Lead", "Designing", 37, "2026-08-04"),
    ])

    assert [r["fields"]["Project name"] for r in ordered] == [
        # Confirmed before Lead; within Confirmed, Fabricating (later in
        # the Process options) before Designing; Fabricating carries no
        # Priority score, so Lead date breaks the tie.
        "Live station cart 2",
        "Photo studio reno",
        "Logo canape trays",
        "Wall panel repair",
        # Then the Leads, by score, then by lead date.
        "Boat speaker mount",
        "Mini hog scoops",
        "Chafing dish cladding",
        "Mid century stools",
    ]


def test_planning_order_puts_empty_cells_where_airtable_does():
    """Airtable treats an empty cell as the lowest value: last in every
    descending pass (verified against the live base 2026-08-25). A
    blank Priority score must not outrank a real one."""
    from core.planning import sort_projects

    ordered = sort_projects([
        _project("no score"),
        _project("scored", score=1),
    ])
    assert [r["fields"]["Project name"] for r in ordered] == ["scored", "no score"]

    # A delivered project sorts above a not-yet-delivered one, since
    # Delivered runs latest→earliest and blank is lowest.
    ordered = sort_projects([
        _project("open"),
        _project("delivered", delivered="2026-08-21"),
    ])
    assert [r["fields"]["Project name"] for r in ordered] == ["delivered", "open"]


def test_planning_order_survives_an_unknown_select_option():
    """A new Status option in Airtable must mis-place a row at worst,
    never raise: this list is the whole morning flow."""
    from core.planning import sort_projects

    ordered = sort_projects([
        _project("brand new status", status="Quoting"),
        _project("confirmed", status="Confirmed"),
    ])
    assert [r["fields"]["Project name"] for r in ordered] == [
        "confirmed", "brand new status"]


def test_planning_designers_are_derived_from_project_ownership(monkeypatch):
    from core import airtable_client as at
    from core.planning import get_planning_designers

    alice = make_member("recA", name="Alice", telegram_id=1)
    bob = make_member("recB", name="Bob", telegram_id=2)
    monkeypatch.setattr(at, "get_plannable_projects", lambda: [
        {"id": "recP", "fields": {"Design owner": ["recA"]}},
        {"id": "recQ", "fields": {}},
    ])
    monkeypatch.setattr(at, "get_active_members", lambda: [alice, bob])
    assert get_planning_designers() == [alice]


# ── Mini App initData signatures ─────────────

def _signed_init_data(token, user_id=111, auth_date=1756000000):
    import hashlib, hmac
    from urllib.parse import urlencode

    fields = {"auth_date": str(auth_date),
              "user": json.dumps({"id": user_id, "first_name": "Alice"})}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def test_init_data_accepts_a_correct_signature():
    from web.auth import validate_init_data

    data = _signed_init_data("test-token")
    user = validate_init_data(data, "test-token", clock=1756000100)
    assert user["id"] == 111


def test_init_data_rejects_tampering_and_wrong_token():
    """The signature is the ONLY thing establishing which designer is
    submitting — a forged user id must never get through."""
    from web.auth import validate_init_data

    data = _signed_init_data("test-token", user_id=111)
    assert validate_init_data(data, "different-token", clock=1756000100) is None

    forged = data.replace("111", "222")
    assert validate_init_data(forged, "test-token", clock=1756000100) is None
    assert validate_init_data("", "test-token") is None


def test_init_data_rejects_a_stale_launch():
    from web.auth import validate_init_data

    data = _signed_init_data("test-token", auth_date=1756000000)
    stale = 1756000000 + (48 * 60 * 60)
    assert validate_init_data(data, "test-token", clock=stale) is None


def test_submitted_blocks_use_the_live_airtable_field_names(monkeypatch):
    """
    These names are a stringly-typed contract with Airtable, and getting
    one wrong is a 422 at write time, not an import error. Renaming
    'Planned slots' -> 'Planned hours' and dropping the text primary
    field broke plan submission live on 2026-08-25; pin the exact keys
    so the next rename fails here instead of on someone's phone.
    """
    from core import airtable_client as at
    from core import planning

    written = []
    monkeypatch.setattr(at, "get_member_by_telegram_id",
                        lambda t: {"id": "recM", "fields": {"Name": "Marcus"}})
    monkeypatch.setattr(at, "get_all_projects_indexed",
                        lambda: {"recP": {"fields": {"Project name": "Order kiosk"}}})
    monkeypatch.setattr(at, "get_or_create_design_day", lambda m, d, c: "recDAY")
    monkeypatch.setattr(at, "create_design_block",
                        lambda f: written.append(f) or {"id": "recB"})

    planning.submit_plan(111, 2.0, [
        {"project_id": "recP", "block_type": "Design", "minutes": 60}])

    assert set(written[0]) == {
        "Project", "Designers", "Day", "Block type", "Block status",
        "Start", "End", "Planned hours",
    }
    # 'Start' is the primary field now — never write a text 'Name'.
    assert "Name" not in written[0]
    assert written[0]["Planned hours"] == 1.0     # hours, not slots
    assert written[0]["Block status"] == "Planned"


def test_projects_payload_carries_what_the_preview_needs(monkeypatch):
    """
    Step 2 previews each block's start/end in the page, which it can
    only do with the server's clock and the lunch window — the phone's
    own time zone is unknown. Losing a key here silently blanks every
    time bubble, so pin the payload.
    """
    import asyncio
    import time
    from aiohttp.test_utils import TestClient, TestServer

    from core import airtable_client as at
    import web.server as srv

    monkeypatch.setattr(at, "get_member_by_telegram_id",
                        lambda t: {"id": "recM", "fields": {"Name": "Marcus"}})
    monkeypatch.setattr(at, "get_plannable_projects", lambda: [])

    async def go():
        async with TestClient(TestServer(srv.build_app())) as client:
            # A launch from *now*: initData older than the auth window
            # is rejected, exactly as a real stale launch would be.
            signed = _signed_init_data("test-token", auth_date=int(time.time()))
            r = await client.post("/api/projects", json={"initData": signed})
            assert r.status == 200
            return await r.json()

    payload = asyncio.run(go())
    for key in ("nowMinutes", "gridMinutes", "minMinutes",
                "lunchStartMinutes", "lunchEndMinutes"):
        assert key in payload, key
    assert 0 <= payload["nowMinutes"] < 24 * 60
    assert (payload["lunchStartMinutes"], payload["lunchEndMinutes"]) == (780, 840)


def test_plan_submission_requires_a_valid_signature(monkeypatch):
    """
    POST /api/plan writes to Airtable, so an unsigned or forged caller
    must never reach submit_plan. Inline-launched Mini Apps carry real
    initData; this is what makes the write path safe without sendData.
    """
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer

    import core.planning as planning
    import web.server as srv

    called = []
    monkeypatch.setattr(
        planning, "submit_plan",
        lambda *a, **k: called.append(a) or {"blocks": [], "capacity_hours": 1,
                                            "member": {}, "day_id": None},
    )
    monkeypatch.setattr(srv, "submit_plan", planning.submit_plan)

    async def go():
        async with TestClient(TestServer(srv.build_app())) as client:
            body = {"capacity_hours": 3, "blocks": [
                {"project_id": "recP", "block_type": "Design", "minutes": 60}]}
            r = await client.post("/api/plan", json={**body, "initData": ""})
            assert r.status == 401
            forged = _signed_init_data("test-token").replace("111", "222")
            r = await client.post("/api/plan", json={**body, "initData": forged})
            assert r.status == 401

    asyncio.run(go())
    assert called == [], "submit_plan must not run for an unauthenticated caller"


def test_init_data_logs_a_distinct_reason_per_rejection(caplog):
    """
    A bare None told nothing apart in the logs — 'opened outside
    Telegram' and 'wrong token' looked identical from the outside
    (discovered live, 2026-08-25). Each rejection path must log its own
    reason so a future 401 diagnoses itself from Railway logs alone.
    """
    from web.auth import validate_init_data

    with caplog.at_level("INFO"):
        validate_init_data("", "test-token")
    assert "empty" in caplog.text
    caplog.clear()

    with caplog.at_level("INFO"):
        validate_init_data("not_a_key_value_pair", "test-token")
    assert "unparseable" in caplog.text
    caplog.clear()

    with caplog.at_level("INFO"):
        forged = _signed_init_data("test-token", user_id=111).replace("111", "222")
        validate_init_data(forged, "test-token", clock=1756000100)
    assert "signature mismatch" in caplog.text


# ── snapshot sheet writes ────────────────────

class _FakeWorksheet:
    def __init__(self, calls, has_header=True):
        self.calls = calls
        self._has_header = has_header

    def get_values(self, rng):
        return [["Date"]] if self._has_header else []

    def append_row(self, values, **kwargs):
        self.calls["append_row"] = kwargs

    def append_rows(self, values, **kwargs):
        self.calls["append_rows"] = kwargs


def _patch_sheet(monkeypatch, worksheet):
    from core import snapshots

    class _FakeSpreadsheet:
        def worksheet(self, name):
            return worksheet

    monkeypatch.setattr(snapshots, "_spreadsheet", lambda: _FakeSpreadsheet())


def test_sheet_appends_are_anchored_to_column_a(monkeypatch):
    """
    Regression (observed live 5-6 Aug 2026): without an explicit
    table_range the Sheets API re-detects which columns 'the table'
    occupies on every append, and each day's rows marched a few columns
    further right. Anchoring at A1 pins the left edge.
    """
    from core.snapshots import TABLE_ANCHOR, append_rows_to_worksheet

    calls = {}
    _patch_sheet(monkeypatch, _FakeWorksheet(calls))
    append_rows_to_worksheet("Snapshots", ["Date"], [["2026-08-06", 1]])

    assert calls["append_rows"]["table_range"] == TABLE_ANCHOR == "A1"
    assert calls["append_rows"]["value_input_option"] == "USER_ENTERED"


def test_sheet_header_write_is_anchored_too(monkeypatch):
    """The header is the row every later append anchors against, so it
    must land in column A as well."""
    from core.snapshots import TABLE_ANCHOR, append_rows_to_worksheet

    calls = {}
    _patch_sheet(monkeypatch, _FakeWorksheet(calls, has_header=False))
    append_rows_to_worksheet("Snapshots", ["Date"], [["2026-08-06", 1]])

    assert calls["append_row"]["table_range"] == TABLE_ANCHOR


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
        assert "Planned hours" not in fields   # the plan stays frozen
        assert "Block status" not in fields    # the evening pass owns it


def test_extend_rejects_absurd_durations():
    from core.design import DesignError, plan_extension

    blocks = [_dblock("recA", 14, 15)]
    with pytest.raises(DesignError):
        plan_extension(blocks, _at_sgt(14.5), 999)


# ── −15 min (core/design.py: plan_shrink) ────

def test_shrink_pulls_the_contiguous_chain_earlier():
    from core.design import plan_shrink

    blocks = [_dblock("recA", 14, 15), _dblock("recB", 15, 16),
              _dblock("recC", 16, 16.5)]
    plan = plan_shrink(blocks, _at_sgt(14.5), 15)

    assert _ends(plan["updates"], "recA")[1] == _at_sgt(14.75)
    assert _ends(plan["updates"], "recB") == (_at_sgt(14.75), _at_sgt(15.75))
    assert _ends(plan["updates"], "recC") == (_at_sgt(15.75), _at_sgt(16.25))


def test_shrink_stops_where_a_gap_absorbs_it():
    from core.design import plan_shrink

    # 30 min of slack after recA already; ending 15 min early just makes
    # the gap bigger — the mirror of gap-first.
    blocks = [_dblock("recA", 14, 15), _dblock("recB", 15.5, 16.5)]
    plan = plan_shrink(blocks, _at_sgt(14.5), 15)

    assert plan["moved"] == []
    assert [rec for rec, _ in plan["updates"]] == ["recA"]


def test_shrink_clears_the_ping_stamp_on_blocks_it_moves():
    from core.design import plan_shrink

    blocks = [_dblock("recA", 14, 15),
              _dblock("recB", 15, 16, pinged="2026-08-04T06:55:00.000Z")]
    plan = plan_shrink(blocks, _at_sgt(14.5), 15)
    assert dict(plan["updates"])["recB"]["Switch ping sent"] is None


def test_shrink_flags_the_next_block_for_an_immediate_reminder():
    """The polling job only pings blocks whose Start is still in the
    future, so a block pulled to now would never be announced."""
    from core.design import plan_shrink

    blocks = [_dblock("recA", 14, 15), _dblock("recB", 15, 16)]

    # Pressed at 14:44: recB is pulled to 14:45, a minute from now.
    plan = plan_shrink(blocks, _at_sgt(14 + 44 / 60), 15)
    assert plan["ping_next"][0]["id"] == "recB"
    assert plan["ping_next"][1:] == (_at_sgt(14.75), _at_sgt(15.75))

    # Pressing at 14:15 leaves recB at 14:45, half an hour out: the job
    # will ping it on schedule, so this must NOT ping twice.
    assert plan_shrink(blocks, _at_sgt(14.25), 15)["ping_next"] is None


def test_shrink_refuses_to_end_a_block_in_the_past():
    from core.design import DesignError, plan_shrink

    blocks = [_dblock("recA", 14, 15)]
    with pytest.raises(DesignError, match="before now"):
        plan_shrink(blocks, _at_sgt(14.9), 15)


def test_shrink_refuses_to_shrink_below_the_grid():
    from core.design import DesignError, plan_shrink

    # A 20-minute block, just started: 15 min off it leaves 5.
    blocks = [_dblock("recA", 14, 14 + 20 / 60)]
    with pytest.raises(DesignError, match="shorter than"):
        plan_shrink(blocks, _at_sgt(14), 15)


def test_shrink_never_pulls_a_block_into_lunch():
    from core.design import plan_shrink

    # recA is pre-existing data planned straight through lunch (the
    # guard blocks new violations only); recB starts as lunch ends, so
    # pulling it 15 min earlier would put it inside the shared break.
    blocks = [_dblock("recA", 13, 14), _dblock("recB", 14, 15)]
    plan = plan_shrink(blocks, _at_sgt(13.75), 15)
    assert plan["moved"] == []


def test_shrink_never_writes_planned_hours_or_status():
    from core.design import plan_shrink

    blocks = [_dblock("recA", 14, 15), _dblock("recB", 15, 16)]
    plan = plan_shrink(blocks, _at_sgt(14.5), 15)
    for _, fields in plan["updates"]:
        assert "Planned hours" not in fields
        assert "Block status" not in fields


# ── the reminder's buttons (interfaces/telegram) ──

class _FakeQuery:
    """Just enough of a CallbackQuery to watch what the handler does."""

    def __init__(self, data, user_id=111):
        self.data = data
        self.from_user = type("U", (), {"id": user_id})()
        self.answers = []
        self.edits = []
        self.replies = []
        self.message = type("M", (), {"reply_text": self._reply})()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def _reply(self, text, reply_markup=None):
        self.replies.append(text)


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


def _button_context(monkeypatch, blocks, at_hour):
    """Wire core.design to an in-memory day and a fixed clock."""
    from core import airtable_client as at
    from core import design

    written = []
    monkeypatch.setattr(at, "get_member_by_telegram_id",
                        lambda t: {"id": "recMEMBER000000001",
                                   "fields": {"Name": "Marcus"}})
    monkeypatch.setattr(at, "get_design_blocks_for_day", lambda d: blocks)
    monkeypatch.setattr(at, "update_design_block",
                        lambda rec, fields: written.append((rec, fields)))
    monkeypatch.setattr(at, "get_project_name", lambda rec: "Order kiosk")
    monkeypatch.setattr(at, "mark_design_block_pinged",
                        lambda rec, when: written.append((rec, {"pinged": when})))
    monkeypatch.setattr(design, "now", lambda: _at_sgt(at_hour))
    return written


def test_button_taps_edit_the_message_and_keep_the_keyboard(monkeypatch):
    """Repeated taps are the point (+15, +15, +15), so the confirmation
    replaces the message it came from and re-attaches the buttons —
    a reply would leave the keyboard scrolled off the screen."""
    import asyncio

    from interfaces.telegram.design_handlers import adjust_callback, adjust_keyboard

    blocks = [_dblock("recA", 14, 15, project="recP"), _dblock("recB", 15, 16)]
    written = _button_context(monkeypatch, blocks, 14.5)

    query = _FakeQuery("extend:15")
    update = type("Upd", (), {"callback_query": query})()
    context = type("Ctx", (), {"bot": _FakeBot()})()
    asyncio.run(adjust_callback(update, context))

    assert len(query.answers) == 1            # invariant 4
    assert query.answers[0][1] is False       # a toast, not an alert
    assert query.edits and query.edits[0][1].to_dict() == adjust_keyboard().to_dict()
    assert "Order kiosk" in query.edits[0][0]
    assert dict(written)["recA"]["End"].startswith("2026-08-04T15:15")


def test_shrink_button_fires_the_pulled_forward_reminder(monkeypatch):
    """The polling job can't send this one — the block's Start is no
    longer in the future — so the handler must, and must stamp it so the
    job doesn't repeat it."""
    import asyncio

    from interfaces.telegram.design_handlers import adjust_callback

    blocks = [_dblock("recA", 14, 15, project="recP"),
              _dblock("recB", 15, 16, project="recQ")]
    written = _button_context(monkeypatch, blocks, 14 + 44 / 60)

    query = _FakeQuery("shrink:15")
    bot = _FakeBot()
    update = type("Upd", (), {"callback_query": query})()
    context = type("Ctx", (), {"bot": bot})()
    asyncio.run(adjust_callback(update, context))

    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == 111
    # The block's NEW start AND its new end — announcing 14:45 against
    # the old 16:00 end would overstate the block by the shrink.
    assert text.startswith("📐 14:45: Order kiosk (1 h)")
    assert any(rec == "recB" and "pinged" in fields for rec, fields in written), \
        "the fired reminder must stamp 'Switch ping sent' or the job repeats it"


def test_a_failed_adjustment_answers_once_with_an_alert(monkeypatch):
    import asyncio

    from interfaces.telegram.design_handlers import adjust_callback

    written = _button_context(monkeypatch, [_dblock("recA", 9, 10)], 14.5)

    query = _FakeQuery("extend:15")
    update = type("Upd", (), {"callback_query": query})()
    context = type("Ctx", (), {"bot": _FakeBot()})()
    asyncio.run(adjust_callback(update, context))

    assert query.answers == [("No design block is running right now — this "
                              "adds time to the block you're currently in.",
                              True)]
    assert written == []


# ── +15 min space (core/design.py: plan_spacer) ──

def test_spacer_pushes_the_day_without_touching_the_current_block():
    """The break belongs to no project, so the running block's End must
    not move — only what comes after it."""
    from core.design import plan_spacer

    blocks = [_dblock("recA", 14, 15), _dblock("recB", 15, 16)]
    plan = plan_spacer(blocks, _at_sgt(14.5), 15)

    assert "recA" not in dict(plan["updates"])
    assert _ends(plan["updates"], "recB") == (_at_sgt(15.25), _at_sgt(16.25))


def test_spacer_stops_at_a_gap_that_already_has_room():
    from core.design import plan_spacer

    blocks = [_dblock("recA", 14, 15), _dblock("recB", 15.5, 16.5)]
    plan = plan_spacer(blocks, _at_sgt(14.5), 15)
    assert plan["updates"] == []


def test_spacer_with_nothing_running_pushes_the_blocks_ahead():
    from core.design import plan_spacer

    # 14:20 now, nothing running: space runs 14:30–14:45, so the 14:30
    # block moves.
    blocks = [_dblock("recB", 14.5, 15.5)]
    plan = plan_spacer(blocks, _at_sgt(14.33), 15)
    assert _ends(plan["updates"], "recB") == (_at_sgt(14.75), _at_sgt(15.75))


def test_spacer_jumps_lunch_when_it_pushes_a_block_into_it():
    from core.design import plan_spacer

    blocks = [_dblock("recA", 11.75, 12.75), _dblock("recB", 12.75, 13)]
    plan = plan_spacer(blocks, _at_sgt(12.5), 15)
    assert _ends(plan["updates"], "recB") == (_at_sgt(14), _at_sgt(14.25))


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
                        lambda mid, d, confirmed=False: created.append(d))
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


# ── "not available" answer + admin notice ──

def _patch_unavailable(monkeypatch, existing, unavailable_week=None):
    """Like _patch_availability, plus the Team Members 'Unavailable week'
    write. Returns (created, deleted, week_writes)."""
    from core import airtable_client as at

    member = make_member()
    if unavailable_week:
        member["fields"]["Unavailable week"] = unavailable_week
    created, deleted = _patch_availability(monkeypatch, existing)
    monkeypatch.setattr(at, "get_member_by_telegram_id", lambda tid: member)
    writes = []
    monkeypatch.setattr(at, "set_member_unavailable_week",
                        lambda mid, ws: writes.append(ws))
    return created, deleted, writes


def test_declare_unavailable_records_week_and_clears_days(monkeypatch):
    """Answering "not available" after submitting days withdraws the days
    — the two states must never coexist for one week."""
    from core.availability import declare_unavailable

    existing = [_avail_record("recMON", "2026-07-20"),
                _avail_record("recTUE", "2026-07-21")]
    created, deleted, writes = _patch_unavailable(monkeypatch, existing)

    result = declare_unavailable(111, "2026-07-20")
    assert writes == ["2026-07-20"]
    assert sorted(deleted) == ["recMON", "recTUE"]
    assert result["removed"] == ["2026-07-20", "2026-07-21"]
    assert result["unavailable"] is True


def test_declare_unavailable_idempotent(monkeypatch):
    from core.availability import declare_unavailable, format_admin_notice

    _, deleted, writes = _patch_unavailable(
        monkeypatch, [], unavailable_week="2026-07-20")
    result = declare_unavailable(111, "2026-07-20")
    assert writes == [] and deleted == []
    assert format_admin_notice(result) is None   # nothing changed → no DM


def test_declare_unavailable_respects_lock(monkeypatch):
    from core.availability import declare_unavailable, AvailabilityError

    existing = [_avail_record("recMON", "2026-07-20", confirmed=True)]
    _, deleted, writes = _patch_unavailable(monkeypatch, existing)
    with pytest.raises(AvailabilityError, match="already being confirmed"):
        declare_unavailable(111, "2026-07-20")
    assert deleted == [] and writes == []


def test_submitting_days_clears_unavailable_for_that_week(monkeypatch):
    from core.availability import submit_availability, format_admin_notice

    created, _, writes = _patch_unavailable(
        monkeypatch, [], unavailable_week="2026-07-20")
    result = submit_availability(111, ["2026-07-22"])
    assert created == ["2026-07-22"]
    assert writes == [None]
    assert result["was_unavailable"] is True
    notice = format_admin_notice(result)
    assert notice.startswith("📥 Alice updated")
    assert "Previously: not available" in notice


def test_submitting_days_leaves_other_weeks_unavailable_alone(monkeypatch):
    from core.availability import submit_availability

    _, _, writes = _patch_unavailable(
        monkeypatch, [], unavailable_week="2026-07-13")
    submit_availability(111, ["2026-07-22"])
    assert writes == []


def test_admin_notice_first_submission_and_edit():
    from core.availability import format_admin_notice

    base = {"member_name": "Alice", "week_starting": "2026-07-20",
            "unavailable": False, "was_unavailable": False}
    first = format_admin_notice({**base, "created": ["2026-07-20"],
                                 "kept": [], "removed": []})
    assert first == ("📥 Alice submitted availability for week of 20 Jul: "
                     "Mon 20 Jul")

    edit = format_admin_notice({**base, "created": ["2026-07-22"],
                                "kept": ["2026-07-20"],
                                "removed": ["2026-07-21"]})
    assert edit.splitlines() == [
        "📥 Alice updated availability for week of 20 Jul: Mon 20 Jul, Wed 22 Jul",
        "Added: Wed 22 Jul",
        "Removed: Tue 21 Jul",
    ]

    same = format_admin_notice({**base, "created": [], "kept": ["2026-07-20"],
                                "removed": []})
    assert same is None

    none_first = format_admin_notice({**base, "unavailable": True,
                                      "created": [], "kept": [], "removed": []})
    assert none_first == ("🚫 Alice submitted availability for week of 20 Jul: "
                          "not available")


def test_submission_status_counts_unavailable_as_answered(monkeypatch):
    """A "not available" answer must not be reminded or listed missing —
    that is the whole point of making it explicit."""
    from core import availability
    from core import airtable_client as at

    away = make_member("recB", name="Away")
    away["fields"]["Unavailable week"] = "2026-07-20"
    stale = make_member("recC", name="Stale")
    stale["fields"]["Unavailable week"] = "2026-07-13"   # an older week
    monkeypatch.setattr(at, "get_active_members", lambda: [
        make_member("recA", name="Days"), away, stale])
    monkeypatch.setattr(at, "get_availability_for_week", lambda ws: [
        {"id": "r1", "fields": {"Member": ["recA"], "Date": "2026-07-20"}}])

    status = availability.get_submission_status("2026-07-20")
    names = {k: [m["name"] for m in v] for k, v in status.items()}
    assert names == {"submitted": ["Days"], "unavailable": ["Away"],
                     "missing": ["Stale"]}


def test_day_keyboard_none_button_carries_the_week():
    from datetime import date
    from interfaces.telegram.availability_handlers import _build_day_keyboard
    from core.availability import get_next_week_dates

    dates = get_next_week_dates(date(2026, 7, 16))
    kb = _build_day_keyboard(dates, set(), "avail")
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "avail:none:2026-07-20" in data
    assert all(len(d.encode()) <= 64 for d in data)   # Telegram's limit


def test_notify_admins_skips_the_submitter(monkeypatch):
    """An admin in the cycle (Marcus) isn't DM'd about his own answer;
    other admins are."""
    import asyncio
    from core import airtable_client as at
    from interfaces.telegram import availability_handlers as h

    me = make_member("recME", name="Marcus", telegram_id=1, admin=True)
    other = make_member("recOT", name="Other", telegram_id=2, admin=True)
    monkeypatch.setattr(at, "get_admin_members", lambda: [me, other])

    sent = []

    class Bot:
        async def send_message(self, chat_id, text):
            sent.append(chat_id)

    result = {"member": me, "member_name": "Marcus",
              "week_starting": "2026-07-20", "unavailable": True,
              "was_unavailable": False, "created": [], "kept": [],
              "removed": []}
    asyncio.run(h._notify_admins(Bot(), result))
    assert sent == [2]


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


# ── fixed-schedule availability (contract / intern) ──

def _patch_fixed(monkeypatch, members, week_records):
    from core import airtable_client as at

    created = []
    monkeypatch.setattr(at, "get_active_members", lambda: members)
    monkeypatch.setattr(at, "get_availability_for_week",
                        lambda ws: week_records)
    monkeypatch.setattr(
        at, "create_availability",
        lambda mid, d, confirmed=False: created.append((mid, d, confirmed)))
    return created


def _fixed_member(rec_id="recFIXED", name="Nauf", days=None, **kw):
    m = make_member(rec_id, name=name, employment="Full-time",
                    weekly=False, **kw)
    if days is not None:
        m["fields"]["Fixed days"] = days
    return m


def test_fixed_schedule_members_excludes_the_cycle_and_part_timers(monkeypatch):
    """Full-time + NOT in the availability cycle. The checkbox is what
    keeps a submitting full-timer (the boss) out of the generator."""
    from core import availability
    from core import airtable_client as at

    monkeypatch.setattr(at, "get_active_members", lambda: [
        _fixed_member("recN", name="Nauf"),
        _fixed_member("recA", name="Alma"),
        make_member("recB", name="Boss", employment="Full-time",
                    weekly=True, admin=True),
        make_member("recP", name="PartTimer"),          # Part-time, in cycle
        make_member("recX", name="PartOptedOut", weekly=False),
    ])
    names = [m["fields"]["Name"]
             for m in availability.get_fixed_schedule_members()]
    assert names == ["Nauf", "Alma"]


def test_fixed_days_blank_means_the_whole_week():
    from core.availability import fixed_days_for_member

    assert fixed_days_for_member(_fixed_member()) == [0, 1, 2, 3, 4, 5]
    assert fixed_days_for_member(_fixed_member(days=[])) == [0, 1, 2, 3, 4, 5]


def test_fixed_days_map_to_offsets_from_monday():
    from core.availability import fixed_days_for_member

    assert fixed_days_for_member(_fixed_member(days=["Fri", "Mon", "Wed"])) \
        == [0, 2, 4]
    # An option that isn't a Mon-Sat day is ignored, not crashed on.
    assert fixed_days_for_member(_fixed_member(days=["Tue", "Sun"])) == [1]


def test_generate_fixed_availability_creates_confirmed_days(monkeypatch):
    from core.availability import generate_fixed_availability

    created = _patch_fixed(
        monkeypatch, [_fixed_member("recN", days=["Mon", "Sat"])], [])

    result = generate_fixed_availability("2026-09-14")
    assert created == [("recN", "2026-09-14", True),
                       ("recN", "2026-09-19", True)]
    assert result[0]["created"] == ["2026-09-14", "2026-09-19"]
    assert result[0]["existing"] == []


def test_generate_fixed_availability_never_revives_an_unticked_day(monkeypatch):
    """The admin marks someone away by unticking Confirmed. A re-run must
    leave that record alone — re-creating it would silently put them back
    on the roster."""
    from core.availability import generate_fixed_availability

    existing = [{"id": "recTUE",
                 "fields": {"Date": "2026-09-15", "Confirmed": False,
                            "Member": ["recN"]}}]
    created = _patch_fixed(
        monkeypatch, [_fixed_member("recN", days=["Mon", "Tue"])], existing)

    result = generate_fixed_availability("2026-09-14")
    assert created == [("recN", "2026-09-14", True)]
    assert result[0]["existing"] == ["2026-09-15"]


def test_generate_fixed_availability_ignores_other_members_records(monkeypatch):
    """Availability is fetched once for the whole week; the member filter
    is client-side (invariant 1) and must not match across members."""
    from core.availability import generate_fixed_availability

    existing = [{"id": "recOTHER",
                 "fields": {"Date": "2026-09-14", "Confirmed": True,
                            "Member": ["recSOMEONEELSE"]}}]
    created = _patch_fixed(
        monkeypatch, [_fixed_member("recN", days=["Mon"])], existing)

    generate_fixed_availability("2026-09-14")
    assert created == [("recN", "2026-09-14", True)]


def test_fixed_schedule_status_reports_only_confirmed_days(monkeypatch):
    from core.availability import get_fixed_schedule_status

    week = [
        {"id": "r1", "fields": {"Date": "2026-09-14", "Confirmed": True,
                                "Member": ["recN"]}},
        {"id": "r2", "fields": {"Date": "2026-09-15", "Confirmed": False,
                                "Member": ["recN"]}},
        {"id": "r3", "fields": {"Date": "2026-09-14", "Confirmed": True,
                                "Member": ["recOTHER"]}},
    ]
    _patch_fixed(monkeypatch, [_fixed_member("recN", name="Nauf")], week)

    assert get_fixed_schedule_status("2026-09-14") == [
        {"name": "Nauf", "dates": ["2026-09-14"]}]


def test_is_admin_reads_checkbox_not_role():
    from core import airtable_client as at

    assert at.is_admin(make_member(admin=True, role="Designer"))
    assert not at.is_admin(make_member(role="admin"))  # old convention dead
    assert not at.is_admin(None)


# ──────────────────────────────────────────────
# The day editor (DESIGN_SCHEDULING.md §13)
# ──────────────────────────────────────────────

def _row(name, start_h, end_h, rec_id="rec1", status="Planned",
         block_type="Design", planned=None, project="recP"):
    """An editor row. Times as SGT hours; minutes past midnight inside."""
    start, end = int(start_h * 60), int(end_h * 60)
    return {
        "id": rec_id,
        "project_id": project,
        "project_name": name,
        "block_type": block_type,
        "start": start,
        "end": end,
        "status": status,
        "planned_hours": (end - start) / 60 if planned is None else planned,
        "orig_start": start,
        "orig_end": end,
    }


def _times(day):
    """[(name, 'HH:MM-HH:MM'), ...] in time order — what a test asserts on."""
    from core.day import _fmt, sort_day

    return [(row["project_name"], f"{_fmt(row['start'])}-{_fmt(row['end'])}")
            for row in sort_day(day)]


def test_nudging_an_end_pushes_the_day_gap_first():
    """The editor inherits core/design.py's cascade: pushing into the
    next block moves it, and the ripple stops at the first gap that can
    absorb what's left."""
    from core.day import nudge

    day = [_row("A", 10, 11, "recA"), _row("B", 11, 11.5, "recB"),
           _row("C", 12, 13, "recC")]
    out = nudge(day, 0, "end", 15)

    assert _times(out) == [("A", "10:00-11:15"), ("B", "11:15-11:45"),
                           ("C", "12:00-13:00")]


def test_nudging_an_end_earlier_leaves_a_gap_rather_than_pulling():
    """Overlaps are illegal, gaps are not — so shrinking never drags
    the rest of the day earlier behind your back."""
    from core.day import nudge

    day = [_row("A", 10, 11, "recA"), _row("B", 11, 12, "recB")]
    out = nudge(day, 0, "end", -15)

    assert _times(out) == [("A", "10:00-10:45"), ("B", "11:00-12:00")]


def test_nudging_an_end_into_lunch_is_refused():
    from core.day import DayError, nudge

    day = [_row("A", 12.25, 13, "recA")]
    with pytest.raises(DayError, match="lunch"):
        nudge(day, 0, "end", 15)


def test_a_block_cannot_be_nudged_below_the_grid():
    from core.day import DayError, nudge

    day = [_row("A", 10, 10.25, "recA")]
    with pytest.raises(DayError, match="15 min"):
        nudge(day, 0, "end", -15)


def test_pulling_a_start_into_the_previous_block_is_refused():
    """No backward cascade: that a block started later says nothing
    about when the one before it finished."""
    from core.day import DayError, nudge

    day = [_row("A", 10, 11, "recA"), _row("B", 11, 12, "recB")]
    with pytest.raises(DayError, match="overlap"):
        nudge(day, 1, "start", -15)


def test_reordering_a_contiguous_pair_moves_nothing_else():
    """The whole point of the reorder: a 2 h task really did run before
    the 1 h one, and the day after them is untouched."""
    from core.day import move

    day = [_row("A", 10, 11, "recA"), _row("B", 11, 13, "recB"),
           _row("C", 14, 15, "recC")]
    out = move(day, 0, 1)

    assert _times(out) == [("B", "10:00-12:00"), ("A", "12:00-13:00"),
                           ("C", "14:00-15:00")]


def test_reordering_preserves_the_gap_that_sat_between_them():
    """Gaps belong to POSITIONS in the day, not to blocks — so the
    shape of the day survives a swap."""
    from core.day import move

    day = [_row("A", 10, 10.5, "recA"), _row("B", 11, 12, "recB")]
    out = move(day, 0, 1)

    assert _times(out) == [("B", "10:00-11:00"), ("A", "11:30-12:00")]


def test_reordering_off_either_end_is_a_no_op():
    from core.day import move

    day = [_row("A", 10, 11, "recA"), _row("B", 11, 12, "recB")]
    assert _times(move(day, 0, -1)) == _times(day)
    assert _times(move(day, 1, 1)) == _times(day)


def test_dropped_blocks_take_no_part_in_the_day():
    """Dropped is planned-but-didn't-happen: not an obstacle, never
    moved, and the hole it leaves is evidence (§7)."""
    from core.day import nudge

    # C sits inside the dropped B's old slot: if B counted, the cascade
    # would have to push it, and B itself would move.
    day = [_row("A", 10, 11, "recA"),
           _row("B", 11, 11.5, "recB", status="Dropped"),
           _row("C", 11, 12, "recC")]
    out = nudge(day, 0, "end", 15)

    assert _times(out) == [("A", "10:00-11:15"), ("B", "11:00-11:30"),
                           ("C", "11:15-12:15")]


def test_switch_now_cuts_the_running_block_and_requeues_the_rest():
    """The delivery-arrives case: what was running is cut at the grid
    point nearest now, the interruption goes in, and the remainder of
    the interrupted task is re-queued rather than silently lost."""
    from core.day import switch_now

    day = [_row("A", 9, 11, "recA"), _row("B", 11, 12, "recB")]
    out = switch_now(day, "recQ", "Delivery", "Admin", 30,
                     now_minutes=9 * 60 + 40, resume=True)

    assert _times(out) == [("A", "09:00-09:45"), ("Delivery", "09:45-10:15"),
                           ("A", "10:15-11:30"), ("B", "11:30-12:30")]
    # The re-queued remainder is unplanned: the frozen plan stays on the
    # original record, so the pair nets out to no deviation.
    requeued = [r for r in out if r["project_name"] == "A" and r["id"] is None]
    assert requeued and requeued[0]["planned_hours"] == 0


def test_switch_now_can_decline_to_requeue():
    from core.day import switch_now

    day = [_row("A", 9, 11, "recA")]
    out = switch_now(day, "recQ", "Lead call", "Client comms", 30,
                     now_minutes=9 * 60 + 40, resume=False)

    assert _times(out) == [("A", "09:00-09:45"), ("Lead call", "09:45-10:15")]


def test_switch_now_drops_a_block_it_cuts_back_to_nothing():
    """'It didn't happen' is exactly what Dropped means (§3) — better
    than leaving a zero-length block behind."""
    from core.day import switch_now

    day = [_row("A", 10, 11, "recA")]
    out = switch_now(day, "recQ", "Delivery", "Admin", 30,
                     now_minutes=10 * 60 + 5, resume=False)

    assert dict((r["project_name"], r["status"]) for r in out)["A"] == "Dropped"
    assert ("A", "10:00-11:00") in _times(out)      # its slot is evidence
    assert ("Delivery", "10:00-10:30") in _times(out)


def test_switch_now_is_the_one_place_lunch_is_not_an_obstacle():
    """It records what is happening right now. Refusing it, or moving it
    to after the break, would make the tool lie."""
    from core.day import switch_now

    day = [_row("A", 12, 12.75, "recA")]
    out = switch_now(day, "recQ", "Delivery", "Admin", 30,
                     now_minutes=12 * 60 + 40, resume=False)

    assert ("Delivery", "12:45-13:15") in _times(out)


def test_confirming_derives_adjusted_from_the_frozen_plan():
    """Confirmed vs Adjusted is just 'did it run to its planned length',
    which is already Deviation (hours) — so it isn't asked."""
    from core.day import resolve_statuses

    day = [
        _row("Ran as planned", 10, 11, "recA", planned=1.0),
        _row("Ran long", 11, 12, "recB", planned=0.5),
        _row("Didn't happen", 12, 12.5, "recC", status="Dropped", planned=0.5),
    ]
    day.append({**_row("Unplanned", 14, 15, "recD"), "id": None,
                "planned_hours": 0, "orig_start": None, "orig_end": None})
    out = {row["project_name"]: row["status"] for row in resolve_statuses(day)}

    assert out == {"Ran as planned": "Confirmed", "Ran long": "Adjusted",
                   "Didn't happen": "Dropped", "Unplanned": "Confirmed"}


def test_overlapping_blocks_never_reach_airtable():
    """Two blocks claiming the same minutes is the silent corruption
    this editor exists to prevent, whatever the payload claims."""
    from core.day import DayError, check_no_overlaps

    with pytest.raises(DayError, match="overlap"):
        check_no_overlaps([_row("A", 10, 11.5, "recA"), _row("B", 11, 12, "recB")])


def test_posted_days_are_rebuilt_field_by_field():
    from core.day import DayError, parse_day

    with pytest.raises(DayError):
        parse_day([{"start": 600, "end": 500}])          # end before start
    with pytest.raises(DayError):
        parse_day([{"start": 600, "end": 660, "block_type": "Nap"}])
    with pytest.raises(DayError):
        parse_day("not a day")

    rows = parse_day([{"id": "recA", "start": 600, "end": 660,
                       "block_type": "Design", "status": "Planned",
                       "project_id": "recP", "planned_hours": 1}])
    assert rows[0]["start"] == 600 and rows[0]["id"] == "recA"


def _day_store(monkeypatch, blocks, day_record=None):
    """Fake the Airtable layer for the day editor's save path."""
    from core import day as day_module

    store = {"updates": [], "creates": [], "day_updates": []}
    member = make_member()

    monkeypatch.setattr(day_module.at, "get_member_by_telegram_id",
                        lambda tg_id: member)
    monkeypatch.setattr(day_module.at, "get_design_blocks_for_day",
                        lambda day_iso: blocks)
    monkeypatch.setattr(day_module.at, "batch_update_design_blocks",
                        lambda updates: store["updates"].extend(updates))
    monkeypatch.setattr(day_module.at, "batch_create_design_blocks",
                        lambda records: store["creates"].extend(records))
    monkeypatch.setattr(day_module.at, "get_design_day",
                        lambda member_id, day_iso: day_record)
    monkeypatch.setattr(day_module.at, "update_design_day",
                        lambda rec, fields: store["day_updates"].append((rec, fields)))
    monkeypatch.setattr(day_module.at, "get_or_create_design_day",
                        lambda member_id, day_iso, capacity: "recDAY")
    return store


def test_saving_never_writes_planned_hours_and_clears_the_ping_stamp():
    """Invariants that outlive this feature: the plan is frozen, and a
    block whose Start moves must lose its dedupe stamp or its switch
    reminder dies silently."""
    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    blocks = [_dblock("recA", 10, 11, project="recP")]
    store = _day_store(monkeypatch, blocks)

    from core.day import save_day

    rows = [_row("A", 10.25, 11.25, "recA")]
    rows[0]["orig_start"], rows[0]["orig_end"] = 600, 660
    save_day(111, "2026-08-04", rows)
    monkeypatch.undo()

    written = store["updates"][0]["fields"]
    assert "Planned hours" not in written
    assert written["Switch ping sent"] is None
    assert written["Start"].startswith("2026-08-04T10:15")
    assert written["End"].startswith("2026-08-04T11:15")


def test_saving_refuses_a_day_that_moved_underneath_the_editor():
    """The +15 buttons write to these same records, so a stale editor
    silently clobbering a live adjustment is a real path."""
    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    blocks = [_dblock("recA", 10, 11.5, project="recP")]   # already extended
    _day_store(monkeypatch, blocks)

    from core.day import DayError, save_day

    rows = [_row("A", 10, 11, "recA")]                     # loaded before that
    with pytest.raises(DayError, match="while you were editing"):
        save_day(111, "2026-08-04", rows)
    monkeypatch.undo()


def test_saving_creates_unplanned_blocks_with_zero_planned_hours():
    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    store = _day_store(monkeypatch, [])

    from core.day import save_day

    row = _row("Delivery", 10, 10.5, rec_id=None, project="recQ")
    row["id"], row["orig_start"], row["orig_end"] = None, None, None
    result = save_day(111, "2026-08-04", row and [row], confirm=True)
    monkeypatch.undo()

    created = store["creates"][0]
    assert created["Planned hours"] == 0
    assert created["Block status"] == "Confirmed"      # nothing to deviate from
    assert created["Day"] == ["recDAY"]
    assert store["day_updates"] == [("recDAY", {"Day status": "Confirmed"})]
    assert result["confirmed"] is True


def test_day_editor_routes_require_a_valid_signature(monkeypatch):
    """
    Both day routes reach Airtable — one reads a designer's day, one
    rewrites it — so the initData HMAC gates them exactly as it gates
    the planner. A record ID in the body is never a licence to touch it.
    """
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer

    import web.server as srv

    called = []
    monkeypatch.setattr(srv, "save_day",
                        lambda *a, **k: called.append(a))
    monkeypatch.setattr(srv, "load_day",
                        lambda *a, **k: called.append(a))

    async def go():
        async with TestClient(TestServer(srv.build_app())) as client:
            forged = _signed_init_data("test-token").replace("111", "222")
            for path in ("/api/day", "/api/day/apply", "/api/day/save"):
                r = await client.post(path, json={"initData": ""})
                assert r.status == 401, path
                r = await client.post(path, json={"initData": forged})
                assert r.status == 401, path

    asyncio.run(go())
    assert called == [], "no day route may run for an unauthenticated caller"


def test_applying_an_op_touches_no_airtable_at_all(monkeypatch):
    """
    /api/day/apply is a pure function of the posted day — that is what
    lets the rules live in Python without a second copy in the page's
    JavaScript, and what makes a restart mid-edit free.
    """
    import asyncio
    import time
    from aiohttp.test_utils import TestClient, TestServer

    from core import airtable_client as at_module
    import web.server as srv

    def explode(*a, **k):
        raise AssertionError("apply must not reach Airtable")

    for name in ("get_design_blocks_for_day", "batch_update_design_blocks",
                 "get_member_by_telegram_id", "get_all_projects_indexed"):
        monkeypatch.setattr(at_module, name, explode)

    async def go():
        async with TestClient(TestServer(srv.build_app())) as client:
            day = [{"id": "recA", "project_id": "recP", "project_name": "A",
                    "block_type": "Design", "start": 600, "end": 660,
                    "status": "Planned", "planned_hours": 1,
                    "orig_start": 600, "orig_end": 660}]
            # A fresh auth_date: initData older than a day is rejected
            # as stale, which would mask what this test is checking.
            fresh = _signed_init_data("test-token", auth_date=int(time.time()))
            r = await client.post("/api/day/apply", json={
                "initData": fresh,
                "day": day,
                "op": {"kind": "nudge", "index": 0, "edge": "end", "delta": 15},
            })
            assert r.status == 200
            assert (await r.json())["rows"][0]["end"] == 675

    asyncio.run(go())


def test_the_edit_day_button_stays_out_of_group_chats(monkeypatch):
    """
    Web App buttons are private-chat only — Telegram rejects the WHOLE
    message, not just the button, so a /extend typed in the group would
    lose its confirmation entirely.
    """
    import config as cfg
    from interfaces.telegram.design_handlers import adjust_keyboard

    # planning_configured() reads this at call time; without it the
    # button is absent for a different reason than the one under test.
    monkeypatch.setattr(cfg, "WEBAPP_URL", "https://plan.example.com")

    def labels(markup):
        return [b.text for row in markup.inline_keyboard for b in row]

    assert any("Edit day" in text for text in labels(adjust_keyboard()))
    assert not any("Edit day" in text
                   for text in labels(adjust_keyboard(with_editor=False)))
    # The adjustment buttons themselves are unaffected either way.
    assert len(labels(adjust_keyboard(with_editor=False))) == 3


# ──────────────────────────────────────────────
# Holds: blocked-out time (DESIGN_SCHEDULING.md §14)
# ──────────────────────────────────────────────

def test_intervals_merge_touching_obstacles_into_one_wall():
    """Two holds with no room between them must not leave a crack a
    block could be dropped into."""
    from core.intervals import clear, merge

    spans = merge([(600, 660), (660, 720), (780, 840)])
    assert spans == [(600, 720), (780, 840)]
    # 90 min doesn't fit in the 720-780 gap, so it lands after the
    # second wall rather than in the crack between them.
    assert clear(600, 90, spans) == 840
    # An hour fits exactly: 12:00-13:00 against a 13:00 wall is clear,
    # because the spans are half-open.
    assert clear(600, 60, spans) == 720


def _hold_row(start_h, end_h, rec_id="recHOLD", status="Planned"):
    """An editor row for blocked-out time: no project, no plan."""
    return _row("", start_h, end_h, rec_id=rec_id, status=status,
                block_type="Hold", planned=0, project=None)


def test_a_hold_is_jumped_by_the_cascade_not_pushed_by_it():
    """The whole point of a hold: a meeting doesn't move because the
    morning ran late — the work goes round it."""
    from core.day import nudge

    day = [_row("A", 10, 11, "recA"), _row("B", 11, 12, "recB"),
           _hold_row(12, 13), _row("C", 14, 15, "recC")]
    out = nudge(day, 0, "end", 15)

    # A ran over, so B is pushed — into the hold, so it jumps to after
    # it; C is then pushed on in turn. The hold itself never moves.
    assert _times(out) == [("A", "10:00-11:15"), ("", "12:00-13:00"),
                           ("B", "14:00-15:00"), ("C", "15:00-16:00")]


def test_a_block_pushed_into_a_hold_lands_after_it():
    from core.day import nudge

    day = [_row("A", 10, 11, "recA"), _row("B", 11, 11.5, "recB"),
           _hold_row(11.5, 12.5)]
    out = nudge(day, 0, "end", 15)

    assert _times(out) == [("A", "10:00-11:15"), ("", "11:30-12:30"),
                           ("B", "12:30-13:00")]


def test_growing_a_block_into_a_hold_is_refused():
    """Marcus, 2026-09-14: refuse, don't truncate — the same rule lunch
    already had."""
    import pytest

    from core.day import DayError, nudge

    day = [_row("A", 10, 11, "recA"), _hold_row(11, 12)]
    with pytest.raises(DayError) as e:
        nudge(day, 0, "end", 15)
    assert "hold" in str(e.value)


def test_a_hold_can_still_be_resized_on_its_own_edges():
    """A meeting that ran long is still a meeting — a hold is not an
    obstacle to itself."""
    from core.day import nudge

    day = [_hold_row(10, 11), _row("B", 11, 12, "recB")]
    out = nudge(day, 0, "end", 15)

    assert _times(out) == [("", "10:00-11:15"), ("B", "11:15-12:15")]


def test_a_hold_takes_no_part_in_the_running_order():
    import pytest

    from core.day import DayError, move

    day = [_row("A", 10, 11, "recA"), _hold_row(11, 12)]
    with pytest.raises(DayError):
        move(day, 1, -1)


def test_reordering_repacks_around_a_hold():
    """Swapping two blocks either side of a hold keeps the hold where
    it is and lays the pair around it."""
    from core.day import move

    day = [_row("A", 10, 11, "recA"), _hold_row(11, 12),
           _row("B", 12, 13, "recB")]
    out = move(day, 0, 1)

    # B ran first after all. It takes A's old start, A takes B's slot
    # after the hold, and the hold doesn't budge.
    assert _times(out) == [("B", "10:00-11:00"), ("", "11:00-12:00"),
                           ("A", "12:00-13:00")]


def test_a_dropped_hold_frees_its_time():
    """Dropping a hold is how you say the meeting didn't happen."""
    from core.day import nudge

    day = [_row("A", 10, 11, "recA"), _hold_row(11, 12, status="Dropped"),
           _row("B", 12, 13, "recB")]
    out = nudge(day, 0, "end", 15)

    assert ("A", "10:00-11:15") in _times(out)
    assert ("B", "12:00-13:00") in _times(out)


def test_confirming_the_day_leaves_holds_planned():
    """A hold never reaches Confirmed or Adjusted, which is exactly
    what keeps it out of 'Confirmed designer-hours' (§14)."""
    from core.day import resolve_statuses

    day = resolve_statuses([_row("A", 10, 11, "recA"), _hold_row(11, 12)])

    assert [(row["block_type"], row["status"]) for row in day] == [
        ("Design", "Confirmed"), ("Hold", "Planned")]


def test_work_recorded_over_a_hold_is_allowed_to_be_saved():
    """switch_now records what is happening NOW, so it ignores holds —
    and the overlap guard must not then refuse to save the truth."""
    from core.day import check_no_overlaps, switch_now

    day = [_hold_row(14, 15)]
    out = switch_now(day, "recP", "Order kiosk", "Design", 30,
                     now_minutes=14 * 60 + 30)

    assert ("Order kiosk", "14:30-15:00") in _times(out)
    check_no_overlaps(out)      # must not raise


def test_a_block_added_without_a_project_is_refused_unless_it_is_a_hold():
    import pytest

    from core.day import DayError, add_block

    with pytest.raises(DayError):
        add_block([], None, "", "Design", 60, now_minutes=600)

    day = add_block([], None, "", "Hold", 60, now_minutes=600)
    assert _times(day) == [("", "10:00-11:00")]


def test_saved_holds_carry_no_project_and_no_planned_hours(monkeypatch):
    """Field-name contract, as pinned for the planner: a hold is
    written WITHOUT 'Project', because that link is the one every
    rollup travels along."""
    from core import airtable_client as at
    from core import day as day_mod

    created = []
    monkeypatch.setattr(at, "get_member_by_telegram_id",
                        lambda t: {"id": "recM", "fields": {"Name": "Marcus"}})
    monkeypatch.setattr(at, "get_design_blocks_for_day", lambda d: [])
    monkeypatch.setattr(at, "get_design_day", lambda m, d: {"id": "recDAY",
                                                            "fields": {}})
    monkeypatch.setattr(at, "batch_create_design_blocks",
                        lambda records: created.extend(records))
    monkeypatch.setattr(at, "batch_update_design_blocks", lambda u: None)

    rows = day_mod.parse_day([{**_hold_row(15, 16), "id": None,
                               "orig_start": None, "orig_end": None}])
    day_mod.save_day(111, "2026-09-14", rows)

    assert "Project" not in created[0]
    assert created[0]["Block type"] == "Hold"
    assert created[0]["Planned hours"] == 0


def test_planner_lays_a_hold_out_in_the_running_order():
    from core.planning import pack_blocks

    blocks = pack_blocks(
        [_sel("recA", 60), {"project_id": None, "block_type": "Hold",
                            "minutes": 30}, _sel("recB", 60)],
        datetime(2026, 9, 14, 9, 0, tzinfo=TZ),
    )
    assert [(b["block_type"], b["start"].strftime("%H:%M")) for b in blocks] == [
        ("Design", "09:00"), ("Hold", "10:00"), ("Design", "10:30")]
    assert blocks[1]["project_id"] is None


def test_planner_requires_a_project_on_everything_but_a_hold():
    import pytest

    from core.planning import PlanningError, pack_blocks

    with pytest.raises(PlanningError):
        pack_blocks([{"project_id": None, "block_type": "Design",
                      "minutes": 60}],
                    datetime(2026, 9, 14, 9, 0, tzinfo=TZ))


def test_submitted_holds_omit_the_project_link(monkeypatch):
    """The write-side twin of the planner's field-name contract test."""
    from core import airtable_client as at
    from core import planning

    written = []
    monkeypatch.setattr(at, "get_member_by_telegram_id",
                        lambda t: {"id": "recM", "fields": {"Name": "Marcus"}})
    monkeypatch.setattr(at, "get_all_projects_indexed", lambda: {})
    monkeypatch.setattr(at, "get_or_create_design_day", lambda m, d, c: "recDAY")
    monkeypatch.setattr(at, "create_design_block",
                        lambda f: written.append(f) or {"id": "recB"})

    planning.submit_plan(111, 2.0, [
        {"project_id": None, "block_type": "Hold", "minutes": 60}])

    assert "Project" not in written[0]
    assert written[0]["Planned hours"] == 0
    assert written[0]["Block status"] == "Planned"


def test_extend_refuses_to_run_into_a_hold():
    import pytest

    from core.design import DesignError, plan_extension

    blocks = [_dblock("recA", 10, 11, project="recP"),
              _dblock("recH", 11, 12, block_type="Hold")]
    with pytest.raises(DesignError) as e:
        plan_extension(blocks, _at_sgt(10.5), 15)
    assert "hold" in str(e.value)


def test_extend_pushes_a_colliding_block_past_a_hold():
    from core.design import plan_extension

    blocks = [_dblock("recA", 10, 11, project="recP"),
              _dblock("recB", 11, 11.5, project="recQ"),
              _dblock("recH", 11.5, 12.5, block_type="Hold")]
    plan = plan_extension(blocks, _at_sgt(10.5), 15)

    # B jumps the hold; the hold itself is never written to.
    assert _ends(plan["updates"], "recB") == (_at_sgt(12.5), _at_sgt(13))
    assert "recH" not in dict(plan["updates"])


def test_shrink_stops_pulling_at_a_hold():
    """Blocks behind a hold are anchored to it, not to the pull — the
    same rule lunch already had."""
    from core.design import plan_shrink

    blocks = [_dblock("recA", 10, 11, project="recP"),
              _dblock("recH", 11, 12, block_type="Hold"),
              _dblock("recB", 12, 13, project="recQ")]
    plan = plan_shrink(blocks, _at_sgt(10.5), 15)

    assert dict(plan["updates"]).keys() == {"recA"}
    assert plan["moved"] == []


def test_a_holds_reminder_names_the_hold_not_a_project():
    from core.design import format_switch_ping

    fields = {"Start": "2026-09-14T07:00:00.000Z",
              "End": "2026-09-14T08:00:00.000Z", "Block type": "Hold"}
    assert format_switch_ping(fields, "(no project)") == "⏸ 15:00: Hold (1 h)"


def test_a_hold_is_not_its_own_obstacle_but_its_neighbour_is():
    """
    The exclusion has to happen BEFORE the obstacle spans are merged.
    Two adjacent holds merge into one wall, and filtering the target
    out afterwards would never match that merged span — so extending
    one hold would silently swallow the next.
    """
    import pytest

    from core.design import DesignError, plan_extension

    blocks = [_dblock("recH1", 11, 12, block_type="Hold"),
              _dblock("recH2", 12, 13, block_type="Hold")]
    with pytest.raises(DesignError) as e:
        plan_extension(blocks, _at_sgt(11.5), 15)
    assert "hold" in str(e.value)


def test_growing_a_hold_into_the_hold_next_to_it_is_refused():
    """The day editor's twin of the same rule."""
    import pytest

    from core.day import DayError, nudge

    day = [_hold_row(11, 12, rec_id="recH1"),
           _hold_row(12, 13, rec_id="recH2")]
    with pytest.raises(DayError):
        nudge(day, 0, "end", 15)


def test_space_pushes_a_block_past_a_hold_rather_than_into_it():
    from core.design import plan_spacer

    blocks = [_dblock("recA", 10, 11, project="recP"),
              _dblock("recB", 11, 11.5, project="recQ"),
              _dblock("recH", 11.5, 12.5, block_type="Hold")]
    plan = plan_spacer(blocks, _at_sgt(10.5), 15)

    # 15 min of space after A pushes B into the hold, so B jumps it.
    assert _ends(plan["updates"], "recB") == (_at_sgt(12.5), _at_sgt(13))
    assert "recH" not in dict(plan["updates"])


# ── /editshift rework: typed times, trims, missed shifts ──

@pytest.mark.parametrize("text, expected", [
    ("18:00", (18, 0)), ("18.00", (18, 0)), ("1800", (18, 0)),
    ("930", (9, 30)), ("9", (9, 0)), ("6pm", (18, 0)), ("6:30 pm", (18, 30)),
    ("12am", (0, 0)), ("12pm", (12, 0)),
    ("24:00", None), ("13pm", None), ("18:60", None), ("abc", None),
    ("25/09/2026 18:00", None),
])
def test_parse_clock(text, expected):
    from core.timeutils import parse_clock
    assert parse_clock(text) == expected


def test_trim_means_inside_the_recorded_times():
    rec_start = _iso(_day(3, 11, 10).replace(second=14))   # clock-in seconds
    rec_end = _iso(_day(3, 20))
    assert edits.is_trim(rec_start, rec_end, rec_start, _iso(_day(3, 18)))
    assert edits.is_trim(rec_start, rec_end, _iso(_day(3, 12)), rec_end)
    # Retyping the start minute without its seconds is still a trim...
    assert edits.is_trim(rec_start, rec_end, _iso(_day(3, 11, 10)), _iso(_day(3, 18)))
    # ...but rounding the start down (seen 7 times live) is not.
    assert not edits.is_trim(rec_start, rec_end, _iso(_day(3, 11)), _iso(_day(3, 18)))
    assert not edits.is_trim(rec_start, rec_end, rec_start, _iso(_day(3, 21)))
    assert not edits.is_trim(rec_start, None, rec_start, _iso(_day(3, 18)))


def test_a_trim_applies_at_once_with_no_reviewer(edit_world):
    result = edits.submit_edit_request(111, "recSHIFTA", _iso(_day(3, 11)),
                                       _iso(_day(3, 18)), "Forgot to clock out")
    assert result["auto_approved"]
    req = result["request"]["fields"]
    assert req["Status"] == "Approved"
    assert "Reviewed by" not in req
    assert req["Admin notes"] == edits.AUTO_APPROVE_NOTE
    shift = next(s for s in edit_world["shifts"] if s["id"] == "recSHIFTA")
    assert parse_dt(shift["fields"]["End time"]) == _day(3, 18)
    assert shift["fields"]["Status"] == "Edit-approved"


def test_an_edit_that_adds_time_waits_for_an_admin(edit_world):
    result = edits.submit_edit_request(111, "recSHIFTA", _iso(_day(3, 10)),
                                       _iso(_day(3, 18)), "Forgot to clock in")
    assert not result["auto_approved"]
    assert result["request"]["fields"]["Status"] == "Pending"
    assert edit_world["updates"] == []


def test_preview_checks_everything_and_writes_nothing(edit_world):
    assert edits.preview_edit(111, "recSHIFTA", _iso(_day(3, 11)),
                              _iso(_day(3, 18)))["auto_approve"]
    assert edit_world["requests"] == [] and edit_world["updates"] == []
    edit_world["shifts"].append(_closed_shift(
        "recEARLY", "recMEMBER000000001", _day(3, 8), _day(3, 10, 30)))
    with pytest.raises(edits.EditError, match="overlaps"):
        edits.preview_edit(111, "recSHIFTA", _iso(_day(3, 10)), _iso(_day(3, 18)))
    assert edit_world["requests"] == []


def test_missed_shift_days_cover_the_last_week_newest_first():
    days = edits.missed_shift_days(date(2026, 10, 9))
    assert len(days) == 7
    assert days[0] == date(2026, 10, 9) and days[-1] == date(2026, 10, 3)


def test_missed_shift_is_a_request_with_no_shift_until_approved(edit_world):
    result = edits.submit_missed_shift(111, _iso(_day(1, 11)), _iso(_day(1, 18)),
                                       "Forgot to clock in")
    req = result["request"]
    assert "Shift" not in req["fields"] and req["fields"]["Status"] == "Pending"
    assert edit_world["created"] == []

    edits.approve_edit(req["id"], 999)
    [shift] = edit_world["created"]
    f = shift["fields"]
    assert f["Member"] == ["recMEMBER000000001"]
    assert parse_dt(f["Start time"]) == _day(1, 11)
    assert parse_dt(f["End time"]) == _day(1, 18)
    assert f["Status"] == "Edit-approved" and f["Source"] == "Edit-approved"
    assert f["Hourly rate snapshot (SGD)"] == 15.0
    assert req["fields"]["Status"] == "Approved"
    assert req["fields"]["Shift"] == [shift["id"]]


def test_missed_shift_refuses_days_outside_the_week(edit_world):
    with pytest.raises(edits.EditError, match="last 7 days"):
        edits.submit_missed_shift(111, _iso(_day(7, 11)), _iso(_day(7, 18)), "x")


def test_missed_shift_refuses_a_day_that_already_has_a_shift(edit_world):
    """The exact double-pay path found live, now refused at the door."""
    with pytest.raises(edits.EditError, match="overlaps your other shift"):
        edits.submit_missed_shift(111, _iso(_day(2, 12)), _iso(_day(2, 18)), "x")


def test_missed_shift_refuses_the_same_request_twice(edit_world):
    edits.submit_missed_shift(111, _iso(_day(1, 11)), _iso(_day(1, 18)), "x")
    with pytest.raises(edits.EditError, match="request you already sent"):
        edits.submit_missed_shift(111, _iso(_day(1, 12)), _iso(_day(1, 17)), "x")


def test_missed_shift_refuses_a_locked_pay_month(edit_world):
    start = _day(1, 11)
    pay_month = start.astimezone(timezone.utc).strftime("%Y-%m")
    edit_world["shifts"].append(make_shift(
        record_id="recLOCKED", member_id="recSOMEONEELSE0001",
        start=_iso(_day(30, 11)), end=_iso(_day(30, 18)), status="Locked",
        **{"Pay month": pay_month}))
    with pytest.raises(edits.EditError, match="already locked"):
        edits.submit_missed_shift(111, _iso(start), _iso(_day(1, 18)), "x")


def test_approving_a_missed_shift_rechecks_overlaps(edit_world):
    req = edits.submit_missed_shift(111, _iso(_day(1, 11)), _iso(_day(1, 18)),
                                    "x")["request"]
    edit_world["shifts"].append(_closed_shift(
        "recLATER", "recMEMBER000000001", _day(1, 12), _day(1, 14)))
    with pytest.raises(edits.EditError, match="overlaps"):
        edits.approve_edit(req["id"], 999)
    assert edit_world["created"] == [] and req["fields"]["Status"] == "Pending"


class _EditChat:
    def __init__(self):
        self.sent = []

    async def send_message(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))


class _EditMsg:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


def _edit_step(handler, context, data=None, text=None):
    """Run one step of the /editshift conversation; return (state, update)."""
    import asyncio
    from types import SimpleNamespace

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=_EditChat(),
        callback_query=_FakeQuery(data) if data else None,
        message=_EditMsg(text) if text is not None else None,
    )
    return asyncio.run(handler(update, context)), update


def _edit_context():
    from types import SimpleNamespace
    return SimpleNamespace(user_data={}, bot=_FakeBot())


def test_fix_button_to_standard_end_is_taps_only(edit_world):
    """The common case — forgot to clock out, left at 18:00 — needs no
    typing at all, applies at once, and still tells the admins."""
    from interfaces.telegram import edit_handlers as h

    ctx = _edit_context()
    state, _ = _edit_step(h.fix_shift_entry, ctx, data="edit_fix:recSHIFTA")
    assert state == h.CHOOSE_FIELD
    state, _ = _edit_step(h.field_selected, ctx, data="edit:field:end")
    assert state == h.ENTER_END
    state, _ = _edit_step(h.end_standard, ctx, data="edit:end:std")
    assert state == h.CHOOSE_REASON
    state, upd = _edit_step(h.reason_selected, ctx, data="edit:reason:0")
    assert state == h.CONFIRM
    assert "applies straight away" in upd.callback_query.edits[-1][0]
    state, upd = _edit_step(h.submit_selected, ctx, data="edit:submit")
    assert state == h.ConversationHandler.END

    shift = next(s for s in edit_world["shifts"] if s["id"] == "recSHIFTA")
    assert parse_dt(shift["fields"]["End time"]) == _day(3, 18)
    [(admin_id, notice)] = ctx.bot.sent
    assert admin_id == 999 and "applied automatically" in notice
    assert ctx.user_data == {}


def test_missed_shift_flow_with_typed_times(edit_world):
    from interfaces.telegram import edit_handlers as h

    ctx = _edit_context()
    _edit_step(h.missed_selected, ctx, data="edit:missed")
    day = _day(1, 0).date().isoformat()
    state, _ = _edit_step(h.day_selected, ctx, data=f"edit:day:{day}")
    assert state == h.ENTER_START

    state, upd = _edit_step(h.start_entered, ctx, text="sometime")
    assert state == h.ENTER_START and "Couldn't read" in upd.effective_chat.sent[0][0]
    state, _ = _edit_step(h.start_entered, ctx, text="11am")
    assert state == h.ENTER_END
    state, upd = _edit_step(h.end_entered, ctx, text="1030")
    assert state == h.ENTER_END and "after start" in upd.effective_chat.sent[0][0]
    state, _ = _edit_step(h.end_entered, ctx, text="6pm")
    assert state == h.CHOOSE_REASON
    state, _ = _edit_step(h.reason_selected, ctx, data="edit:reason:other")
    assert state == h.ENTER_REASON
    state, upd = _edit_step(h.reason_entered, ctx, text="First day")
    assert state == h.CONFIRM and "admin will need to approve" in upd.effective_chat.sent[0][0]
    _edit_step(h.submit_selected, ctx, data="edit:submit")

    [req] = edit_world["requests"]
    assert req["fields"]["Status"] == "Pending" and "Shift" not in req["fields"]
    assert parse_dt(req["fields"]["Requested start"]) == _day(1, 11)
    assert parse_dt(req["fields"]["Requested end"]) == _day(1, 18)
    [(admin_id, text)] = ctx.bot.sent
    assert admin_id == 999 and "Missed-shift request" in text


def test_a_clash_surfaces_at_confirm_not_after_submit(edit_world):
    from interfaces.telegram import edit_handlers as h

    ctx = _edit_context()
    _edit_step(h.missed_selected, ctx, data="edit:missed")
    day = _day(2, 0).date().isoformat()    # Alice already has a shift then
    _edit_step(h.day_selected, ctx, data=f"edit:day:{day}")
    _edit_step(h.start_entered, ctx, text="12:00")
    _edit_step(h.end_entered, ctx, text="18:00")
    state, upd = _edit_step(h.reason_selected, ctx, data="edit:reason:1")
    assert state == h.ConversationHandler.END
    assert "overlaps your other shift" in upd.callback_query.edits[-1][0]
    assert edit_world["requests"] == []


# ── pre-payroll shift checks, clock notices (Oct 2026) ──

def _pshift(record_id, member_id, start, end, status="Closed"):
    return {"id": record_id, "fields": {
        "Member": [member_id], "Status": status,
        "Start time": _iso(start), "End time": _iso(end),
        "Duration (hours)": 1.0, "Gross pay (SGD)": 10.0}}


def _members_ab():
    return {"recA": make_member("recA", name="Alice", telegram_id=1),
            "recB": make_member("recB", name="Bob", telegram_id=2)}


def test_anomalies_find_the_live_double_pay_shape():
    """Wayne 25 Aug: an edited 11:00–18:00 on top of a real 12:19–19:02."""
    from core.payroll import find_shift_anomalies

    shifts = [
        _pshift("rec1", "recA", _day(5, 11), _day(5, 18), status="Edit-approved"),
        _pshift("rec2", "recA", _day(5, 12, 19), _day(5, 19, 2)),
        _pshift("rec3", "recB", _day(5, 12), _day(5, 18)),   # other member
        _pshift("rec4", "recA", _day(4, 11), _day(4, 18)),   # other day
    ]
    found = find_shift_anomalies(shifts, _members_ab())
    assert len(found["overlaps"]) == 1
    assert found["overlaps"][0].startswith("Alice:")
    assert found["overlaps_block_lock"]
    assert found["short"] == [] and found["long"] == []


def test_back_to_back_shifts_are_not_an_overlap():
    from core.payroll import find_shift_anomalies

    shifts = [_pshift("rec1", "recA", _day(5, 9), _day(5, 13)),
              _pshift("rec2", "recA", _day(5, 13), _day(5, 18))]
    assert find_shift_anomalies(shifts, _members_ab())["overlaps"] == []


def test_anomalies_flag_short_and_long_shifts():
    from core.payroll import find_shift_anomalies

    shifts = [
        _pshift("rec1", "recA", _day(5, 18, 36), _day(5, 18, 36) + timedelta(seconds=2)),
        _pshift("rec2", "recB", _day(5, 8), _day(5, 21)),
        _pshift("rec3", "recA", _day(4, 11), _day(4, 11, 5)),   # exactly 5 min: fine
        {"id": "rec4", "fields": {"Member": ["recA"], "Status": "Closed"}},  # no times
    ]
    found = find_shift_anomalies(shifts, _members_ab())
    assert len(found["short"]) == 1 and "Alice" in found["short"][0]
    assert len(found["long"]) == 1 and "Bob" in found["long"][0]
    assert found["overlaps"] == []


def test_lock_month_refuses_while_shifts_overlap(monkeypatch):
    from core import airtable_client as at
    from core.payroll import PayrollError, lock_month

    shifts = [_pshift("rec1", "recA", _day(5, 11), _day(5, 18)),
              _pshift("rec2", "recA", _day(5, 12), _day(5, 19))]
    _patch_payroll(monkeypatch, shifts, list(_members_ab().values()))
    written = []
    monkeypatch.setattr(at, "batch_update_shifts", lambda recs: written.extend(recs))

    with pytest.raises(PayrollError, match="overlapping") as e:
        lock_month("2026-10")
    assert len(str(e.value)) <= 200   # shown as a Telegram alert
    assert written == []


def test_an_already_locked_overlap_does_not_block_a_lock(monkeypatch):
    """Locked is terminal, so a Locked pair can't be fixed any more —
    it must not hold the rest of the month hostage."""
    from core import airtable_client as at
    from core.payroll import lock_month

    shifts = [_pshift("rec1", "recA", _day(5, 11), _day(5, 18), status="Locked"),
              _pshift("rec2", "recA", _day(5, 12), _day(5, 19), status="Locked"),
              _pshift("rec3", "recB", _day(5, 12), _day(5, 19))]
    _patch_payroll(monkeypatch, shifts, list(_members_ab().values()))
    monkeypatch.setattr(at, "batch_update_shifts", lambda recs: None)
    assert lock_month("2026-10") == 1


def test_payroll_summary_lists_overlaps_and_odd_lengths(monkeypatch):
    from core.payroll import build_payroll_summary, format_payroll_summary

    shifts = [_pshift("rec1", "recA", _day(5, 11), _day(5, 18)),
              _pshift("rec2", "recA", _day(5, 12), _day(5, 19)),
              _pshift("rec3", "recB", _day(5, 9), _day(5, 9, 1))]
    _patch_payroll(monkeypatch, shifts, list(_members_ab().values()))
    text = format_payroll_summary(build_payroll_summary("2026-10"))
    assert "Overlapping shifts" in text and "Alice:" in text
    assert "Under 5 min" in text and "Bob:" in text


def test_clock_out_flags_a_seconds_long_shift(fake_at):
    m = make_member(telegram_id=111)
    fake_at["members"].append(m)
    fake_at["shifts"].append(make_shift(member_id=m["id"], status="Open",
                                        start=_iso(now() - timedelta(seconds=3))))
    assert shifts.clock_out(111)["short"]


def test_clock_out_does_not_flag_a_normal_shift(fake_at):
    m = make_member(telegram_id=111)
    fake_at["members"].append(m)
    fake_at["shifts"].append(make_shift(member_id=m["id"], status="Open",
                                        start=_iso(now() - timedelta(hours=7))))
    assert not shifts.clock_out(111)["short"]


def _clock_update(user_id=111):
    from types import SimpleNamespace
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id),
                           message=_EditMsg("/clockin"))


def test_clock_in_and_out_notify_admins_but_not_the_actor(fake_at):
    import asyncio
    from interfaces.telegram.shift_handlers import clockin_handler, clockout_handler

    fake_at["members"] += [
        make_member(),                                                 # Alice, 111
        make_member("recADMIN0000000001", name="Bob", telegram_id=999, admin=True),
        make_member("recADMIN0000000002", name="Cat", telegram_id=888, admin=True),
    ]
    ctx = _edit_context()
    asyncio.run(clockin_handler(_clock_update(), ctx))
    assert sorted(chat for chat, _ in ctx.bot.sent) == [888, 999]
    assert all("Alice clocked in" in text for _, text in ctx.bot.sent)

    # An admin clocking themselves out isn't told about it.
    ctx = _edit_context()
    fake_at["shifts"].append(make_shift(member_id="recADMIN0000000001", status="Open",
                                        start=_iso(now() - timedelta(hours=6))))
    asyncio.run(clockout_handler(_clock_update(999), ctx))
    assert [chat for chat, _ in ctx.bot.sent] == [888]
    assert "Bob clocked out" in ctx.bot.sent[0][1]


def test_a_failed_clock_in_notifies_nobody(fake_at):
    import asyncio
    from interfaces.telegram.shift_handlers import clockin_handler

    fake_at["members"] += [
        make_member(rate=None),
        make_member("recADMIN0000000001", name="Bob", telegram_id=999, admin=True),
    ]
    ctx = _edit_context()
    update = _clock_update()
    asyncio.run(clockin_handler(update, ctx))
    assert ctx.bot.sent == []
    assert "No hourly rate" in update.message.replies[0][0]


def test_admin_request_shows_the_members_other_shifts_that_day(edit_world):
    from interfaces.telegram.edit_handlers import _format_admin_request

    edit_world["shifts"].append(_closed_shift(
        "recMORNING", "recMEMBER000000001", _day(1, 8), _day(1, 10)))
    result = edits.submit_missed_shift(111, _iso(_day(1, 14)), _iso(_day(1, 18)), "x")
    assert "Other shifts that day: 08:00 → 10:00" in _format_admin_request(result)

    result = edits.submit_edit_request(111, "recSHIFTA", _iso(_day(3, 10)),
                                       _iso(_day(3, 18)), "x")
    assert "Other shifts that day: none" in _format_admin_request(result)
