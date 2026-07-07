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


def make_member(record_id="recMEMBER000000001", name="Alice", telegram_id=111,
                status="Active", role="member", rate=15.0):
    return {
        "id": record_id,
        "fields": {
            "Name": name,
            "Telegram user ID": telegram_id,
            "Status": status,
            "Role": role,
            "Current hourly rate (SGD)": rate,
        },
    }


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

    def create_shift(member_record_id, start_time, hourly_rate, source="Telegram"):
        rec = make_shift(
            record_id=f"recNEW{len(store['created']):012d}",
            member_id=member_record_id,
            start=start_time,
            rate=hourly_rate,
        )
        store["shifts"].append(rec)
        store["created"].append(rec)
        return rec

    monkeypatch.setattr(at, "get_member_by_telegram_id", get_member_by_telegram_id)
    monkeypatch.setattr(at, "get_open_shift", get_open_shift)
    monkeypatch.setattr(at, "get_all_open_shifts", get_all_open_shifts)
    monkeypatch.setattr(at, "get_all_members_indexed", get_all_members_indexed)
    monkeypatch.setattr(at, "get_shift", get_shift)
    monkeypatch.setattr(at, "update_shift", update_shift)
    monkeypatch.setattr(at, "close_shift", close_shift)
    monkeypatch.setattr(at, "create_shift", create_shift)

    return store
