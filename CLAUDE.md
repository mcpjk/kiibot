# CLAUDE.md — Kii-bot maintenance guide

Read this fully before changing anything. It encodes hard-won empirical
findings; several "obvious simplifications" of this codebase are actually
bugs that were already found and fixed once.

## What this is

Telegram shift-management bot for a small hourly-paid team in Singapore.
Airtable is the database (Kii master base `appzTLEjQPg1DAe2m`; the five
shift tables were consolidated here from the old "Kii Shift Management"
base `appn3g9814LnoFdKH`, which is now retired).
All times Asia/Singapore (UTC+8), pay in SGD. Runs long-polling via
python-telegram-bot (PTB) v20+; scheduled jobs via PTB JobQueue.
Pre-launch as of Jul 2026 — see README.md for commands, jobs, and setup.

## Owner preferences (Marcus)

- Concise, direct responses; minimal formatting.
- Empirical over anecdotal: verify against the real Airtable base / real
  behavior instead of assuming. Say clearly when you're unsure or assuming.
- Point out flaws or better alternatives in his requests when relevant.
- Metric units, SGD prices.
- Ask for missing information that would narrow uncertainty before
  committing to consequential changes; batch questions.

## Architecture

```
main.py                      wiring: handlers, error handler, jobs, logging
config.py                    env secrets + timing/table constants
core/airtable_client.py      ALL Airtable I/O goes through here
core/shifts.py               clock in/out, confirm, sweeps (business logic)
core/edits.py                edit-request workflow + validation
core/availability.py         weekly availability cycle
core/timeutils.py            ALL datetime parse/format goes through here
interfaces/telegram/*.py     thin handlers: translate Telegram <-> core
jobs/scheduler.py            job functions + register_jobs()
tests/                       pytest, no network (fake Airtable in conftest.py)
setup_airtable.py            one-off schema bootstrap (mostly historical)
```

Keep handlers thin; business logic belongs in `core/`. Handlers and jobs
must not call pyairtable directly — only via `core/airtable_client.py`.

## Data model (field names are a stringly-typed contract)

Tables: Team Members, Rate History, Shifts, Shift Edit Requests,
Availability. Exact field names used in code are listed in README.md.
Renaming a field in Airtable silently breaks the bot — grep the codebase
for the field name before/after any schema change.

Shift Status lifecycle:
`Open` → `Closed` (clock-out) or `Auto-closed` (sweep) → possibly
`Edit-approved` (admin approved an edit) → `Locked` (/lockmonth; terminal,
uneditable).

`Hourly rate snapshot (SGD)` is copied from the member at clock-in.
Never recompute historical pay from the member's *current* rate.

## Critical invariants — do NOT reintroduce these bugs

1. **Linked-record filtering.** Airtable formulas render linked-record
   fields as the linked record's PRIMARY FIELD VALUE (e.g. member Name),
   never its record ID. `FIND('recXXX', ARRAYJOIN({Member}))` can NEVER
   match. Correct approach (already implemented): filter non-linked fields
   server-side (Status, Pay month, Week starting), then filter by record ID
   client-side — the REST API returns linked fields as lists of record IDs.
   Do not "optimise" this back into a formula.

2. **Timezones.** Airtable returns dateTimes as UTC with a `Z` suffix
   (`2026-07-06T01:00:00.000Z`). Raw `datetime.fromisoformat()` fails on
   `Z` below Python 3.11, and naive formatting displays UTC (8 h behind
   SGT). ALL parsing/formatting must go through `core/timeutils.py`
   (`parse_dt`, `fmt_dt`, `fmt_time`, `fmt_date_short`, `now`).

3. **PTB weekday convention.** `job_queue.run_daily(days=...)` uses
   **0=Sunday … 6=Saturday** in PTB v20+ (changed from the old
   Monday-first convention). Thursday is 4, not 3. Verify against the
   installed version's docstring if touching schedules.

4. **`query.answer()` once per callback.** Telegram honours only the first
   answer to a callback query. If a branch needs `show_alert=True`, that
   must be the first and only `answer()` call on that code path.

5. **Sweeps are stateless by design.** The 20:00 sweep writes `Prompted at`
   on the shift; `/confirmshift` writes `Confirmed at`; the 21:00 sweep
   closes Open shifts where `Prompted at` is set and `Confirmed at` is
   absent or earlier than `Prompted at`, with end time = prompt time.
   All state lives in Airtable so restarts lose nothing. Do not store job
   state in `bot_data` / memory — that was a bug (restart between 20:00
   and 21:00 lost the warned list).

6. **Pay figures.** Airtable formula fields (`Duration (hours)`,
   `Gross pay (SGD)`, `Pay month`) are the single source of truth; the bot
   re-reads the record after closing a shift. Local float arithmetic exists
   only as a logged fallback. Don't add a second computation path.

7. **Airtable API limits.** The API cannot delete fields and cannot create
   lookup/rollup/createdTime fields (formula creation works via the MCP
   connector). Rate limit ~5 req/s — avoid per-record lookups in loops;
   use `get_all_members_indexed()`.

## How to verify changes (do this every time)

```bash
source venv/bin/activate   # or use system python3 with deps installed
python -m pytest tests/ -q          # 23+ tests, no network needed
python -c "import main"             # import check (needs dummy env vars:
                                    # TELEGRAM_BOT_TOKEN, AIRTABLE_API_KEY,
                                    # AIRTABLE_BASE_ID=appXXXXXXXXXXXXXX)
```

tests/conftest.py sets dummy env vars and fakes the Airtable layer by
monkeypatching `core.airtable_client` functions. When adding core logic,
add tests there — especially for anything touching money or the
prompt/confirm/auto-close cycle.

Manual smoke test after deploy: `/start`, `/clockin`, `/myshifts`
(check displayed times match SGT wall clock), `/clockout` (check duration/
gross match Airtable), `/editshift` round-trip with an admin account.

Only ONE bot instance may poll at a time — a second instance causes
Telegram `Conflict: terminated by other getUpdates request` errors.
Stop the local run before starting the server one, and vice versa.

## Working conventions for maintenance sessions

- Run the test suite before AND after changes; keep it green.
- Make minimal diffs; don't reformat untouched code.
- Before assuming Airtable behavior, verify empirically: use the Airtable
  MCP connector (if available) to inspect the live schema, or create a
  test record and read it back. Schema drift is the most likely silent
  breakage.
- Ask Marcus before: changing the Airtable schema, changing job times,
  or anything affecting pay calculation. Batch the questions.
- Money code: bias toward underpayment-with-easy-correction over
  overpayment (that's why auto-close backdates to the prompt time and
  /editshift exists). Preserve this bias.
- Update README.md command/schema tables and this file when behavior
  changes. Commit with descriptive messages explaining WHY.
- Deployment is via systemd (`kii-bot.service`, unit in README). After
  pulling changes on the server: `systemctl restart kii-bot`, then
  `journalctl -u kii-bot -f` to confirm a clean start.

## Known deliberate limitations (not bugs)

- Single 20:00 sweep: work past 20:00 happens ~once a year; the edit flow
  covers it. Don't add complexity here without being asked.
- Overnight/multi-day shifts unsupported by design.
- Admin confirms availability by ticking `Confirmed` in Airtable directly,
  then runs `/confirmweek` — the Airtable UI is intentionally part of the
  admin workflow.
- Onboarding is self-service: `/start` creates a `Pending` Team Members
  record (Telegram ID + username) and DMs admins; an admin sets the
  rate/role and flips Status to `Active`. Pending members can't clock in
  (`clock_in` requires Status `Active` + a rate). Admins still activate
  manually — that gate is intentional.
- Roles: `admin` / `part-timer` / `full-timer`. Full-timers are Active
  members who belong in the group chat but are excluded from the weekly
  availability cycle (`get_schedulable_members`).
- Lunch (13:00–14:00 SGT) is unpaid: the Airtable `Lunch (hours)` formula
  computes the shift's overlap with the window and `Duration (hours)`
  subtracts it, so all pay stays formula-derived (invariant 6). Shifts
  starting before `LUNCH_POLICY_START` (2026-08-01) are exempt — earlier
  months were deducted manually and locked history must keep matching
  what was paid. `lunch_overlap_hours()` in `core/timeutils.py` mirrors
  the formula for the logged local fallback only; keep the two in sync.
  The clockout summary shows a soft "(− lunch)" marker, deliberately not
  the deducted amount (Marcus's preference).
- Group membership (`core/membership.py`): invariant is Status `Active`
  ⇔ in group chat. Audit runs from `/confirmweek`; removal trigger is a
  human flipping Status to `Inactive` (the bot executes ban+unban).
  Staleness (no shifts in `STALE_SHIFT_WEEKS`) is flag-only — never
  auto-flip Status, it gates pay/access. Admins are never auto-removed.
  The Bot API cannot enumerate group members: checks are roster-driven
  via `get_chat_member`, strangers detectable only via join events.
  The bot must be a group admin with ban rights.
