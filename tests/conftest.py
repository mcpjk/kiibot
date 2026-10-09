"""
Test fixtures. Sets dummy env vars BEFORE config is imported anywhere,
and provides a fake Airtable layer by monkeypatching core.airtable_client
functions on the modules that imported it.
"""

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("AIRTABLE_API_KEY", "test-key")
os.environ.setdefault("AIRTABLE_BASE_ID", "appTESTTESTTESTTE")

import pytest  # noqa: E402

from core.timeutils import parse_dt  # noqa: E402


def make_member(record_id="recMEMBER000000001", name="Alice", telegram_id=111,
                status="Active", role="Fabricator", rate=15.0,
                employment="Part-time", admin=False, weekly=True):
    """Role is job function (Designer/Fabricator/Communicator); access
    control lives in the Admin / Weekly availability / Employment type
    fields, mirroring the Airtable schema."""
    fields = {
        "Name": name,
        "Telegram user ID": telegram_id,
        "Status": status,
        "Role": role,
        "Employment type": employment,
        "Current hourly rate (SGD)": rate,
    }
    if admin:
        fields["Admin"] = True
    if weekly:
        fields["Weekly availability"] = True
    return {"id": record_id, "fields": fields}


def make_shift(record_id="recSHIFT0000000001", member_id="recMEMBER000000001",
               start="2026-07-06T09:00:00.000Z", end=None, status="Open",
               rate=15.0, **extra):
    fields = {
        "Member": [member_id],
        "Start time": start,
        "Hourly rate snapshot (SGD)": rate,
        "Status": status,
    }
    if end:
        fields["End time"] = end
    fields.update(extra)
    return {"id": record_id, "fields": fields}


@pytest.fixture
def member():
    return make_member()


@pytest.fixture
def fake_at(monkeypatch):
    """
    Patch core.airtable_client functions with an in-memory store.
    Returns the store so tests can seed and inspect it.
    """
    from core import airtable_client as at

    store = {
        "members": [],
        "shifts": [],
        "updates": [],   # (record_id, fields) log of update_shift calls
        "created": [],   # created shift field dicts
        "requests": [],  # Shift Edit Requests records
    }

    def get_member_by_telegram_id(tg_id):
        for m in store["members"]:
            if m["fields"].get("Telegram user ID") == tg_id:
                return m
        return None

    def get_open_shift(member_id):
        for s in store["shifts"]:
            if (s["fields"].get("Status") == "Open"
                    and member_id in s["fields"].get("Member", [])):
                return s
        return None

    def get_all_open_shifts():
        return [s for s in store["shifts"] if s["fields"].get("Status") == "Open"]

    def get_all_members_indexed():
        return {m["id"]: m for m in store["members"]}

    def get_shift(shift_id):
        for s in store["shifts"]:
            if s["id"] == shift_id:
                return s
        return None

    def update_shift(shift_id, fields):
        store["updates"].append((shift_id, fields))
        s = get_shift(shift_id)
        if s:
            s["fields"].update(fields)
        return s

    def close_shift(shift_id, end_time, status="Closed"):
        return update_shift(shift_id, {"End time": end_time, "Status": status})

    def create_shift(member_record_id, start_time, hourly_rate, source="Telegram",
                     end_time=None, status="Open"):
        rec = make_shift(
            record_id=f"recNEW{len(store['created']):012d}",
            member_id=member_record_id,
            start=start_time,
            end=end_time,
            status=status,
            rate=hourly_rate,
            Source=source,
        )
        store["shifts"].append(rec)
        store["created"].append(rec)
        return rec

    def get_member(member_id):
        for m in store["members"]:
            if m["id"] == member_id:
                return m
        return None

    def get_admin_members():
        return [m for m in store["members"] if m["fields"].get("Admin")]

    def get_member_shifts(member_id, limit=10, pay_month=None):
        mine = [s for s in store["shifts"]
                if member_id in s["fields"].get("Member", [])]
        mine.sort(key=lambda s: parse_dt(s["fields"]["Start time"]), reverse=True)
        return mine[:limit]

    def get_member_shifts_between(member_id, start_iso, end_iso):
        lo, hi = parse_dt(start_iso), parse_dt(end_iso)
        return [s for s in store["shifts"]
                if member_id in s["fields"].get("Member", [])
                and lo < parse_dt(s["fields"]["Start time"]) < hi]

    def get_shifts_for_payroll(pay_month):
        return [s for s in store["shifts"]
                if s["fields"].get("Pay month") == pay_month]

    def create_edit_request(shift_record_id, member_record_id, original_start,
                            original_end, requested_start, requested_end,
                            reason):
        fields = {
            "Requested by": [member_record_id],
            "Requested start": requested_start,
            "Requested end": requested_end,
            "Reason": reason,
            "Status": "Pending",
        }
        if shift_record_id:
            fields["Shift"] = [shift_record_id]
        if original_start:
            fields["Original start"] = original_start
        if original_end:
            fields["Original end"] = original_end
        rec = {"id": f"recREQ{len(store['requests']):012d}", "fields": fields}
        store["requests"].append(rec)
        return rec

    def get_edit_request(request_id):
        for r in store["requests"]:
            if r["id"] == request_id:
                return r
        return None

    def get_pending_edit_requests():
        return [r for r in store["requests"]
                if r["fields"].get("Status") == "Pending"]

    def update_edit_request(request_record_id, status, reviewed_by_record_id=None,
                            reviewed_at=None, admin_notes="",
                            shift_record_id=None):
        r = get_edit_request(request_record_id)
        r["fields"]["Status"] = status
        if reviewed_by_record_id:
            r["fields"]["Reviewed by"] = [reviewed_by_record_id]
        if reviewed_at:
            r["fields"]["Reviewed at"] = reviewed_at
        if admin_notes:
            r["fields"]["Admin notes"] = admin_notes
        if shift_record_id:
            r["fields"]["Shift"] = [shift_record_id]
        return r

    monkeypatch.setattr(at, "get_member_by_telegram_id", get_member_by_telegram_id)
    monkeypatch.setattr(at, "get_open_shift", get_open_shift)
    monkeypatch.setattr(at, "get_all_open_shifts", get_all_open_shifts)
    monkeypatch.setattr(at, "get_all_members_indexed", get_all_members_indexed)
    monkeypatch.setattr(at, "get_shift", get_shift)
    monkeypatch.setattr(at, "update_shift", update_shift)
    monkeypatch.setattr(at, "close_shift", close_shift)
    monkeypatch.setattr(at, "create_shift", create_shift)
    for fn in (get_member, get_admin_members, get_member_shifts,
               get_member_shifts_between, get_shifts_for_payroll,
               create_edit_request, get_edit_request,
               get_pending_edit_requests, update_edit_request):
        monkeypatch.setattr(at, fn.__name__, fn)

    return store
