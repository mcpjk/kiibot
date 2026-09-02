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
core/planning.py             daily planning: plannable list + block packing
core/day.py                  day editor: pure ops over a day's blocks
interfaces/telegram/*.py     thin handlers: translate Telegram <-> core
web/                         Mini Apps: aiohttp server + initData auth + pages
jobs/scheduler.py            job functions + register_jobs()
tests/                       pytest, no network (fake Airtable in conftest.py)
setup_airtable.py            one-off schema bootstrap (mostly historical)
```

Keep handlers thin; business logic belongs in `core/`. Handlers and jobs
must not call pyairtable directly — only via `core/airtable_client.py`.

## Design scheduling

The design scheduling system (Projects / Design Blocks / Design Days in
the same base) has its own spec: **DESIGN_SCHEDULING.md**. Read it fully
before touching design-block code or schema — it encodes the scoring
engine, decided conventions, and hard-won Airtable quirks. Bot code uses
table/field IDs from config.py (rename-proof), but the filter formulas
reference field NAMES (`Start`, `Block status`, `Switch ping sent`) —
renaming those breaks the switch-ping job silently. `Switch ping sent`
(fld7AQsYX5YjGhDqx) is bot-written dedupe state; never hand-edit.

Daily planning (`core/planning.py` + `web/`, §12) replaced the
score-driven ration on 2026-08-24: designers pick from the FULL
plannable list in a Telegram Mini App; `Priority score` is now one sort
key of five. Things to keep straight:

- **The button must be INLINE, and the write path is an authenticated
  POST.** Telegram's two launch styles are mutually exclusive:
  reply-keyboard launches get `sendData()` but **no initData at all**;
  inline launches get signed initData but no `sendData()`. The page
  can't be authorised to load without initData, so inline wins and
  submissions go to `POST /api/plan`. This was built the wrong way
  round first and could not work (confirmed live 2026-08-25) — don't
  "restore" `sendData()`.
- **`initData` HMAC is the only identity check** on both POST routes
  (`web/auth.py`). Never read a Telegram user ID from a request body.
- **Airtable calls in `web/` must run via `asyncio.to_thread`** — the
  server shares its event loop with the bot's polling, so a blocking
  call stalls the bot.
- **The picker's order mirrors Marcus's Airtable view**, not the
  score: Status → Delivered → Process → Priority score → Lead date
  (`sort_projects`). Two Airtable facts are baked in and can drift —
  the option ORDER of the Status/Process selects (mirrored in
  `STATUS_ORDER` / `PROCESS_ORDER`, because the REST API doesn't return
  it with records), and blank-cell ranking (blank is the lowest value:
  first ascending, last descending — verified live). Reordering those
  options in the UI silently desyncs the two lists.
- **Step 2 previews each block's start/stop and allows reordering.**
  The preview re-implements `pack_blocks`' grid + lunch rules in JS —
  it has to, since it must update on every tap — and is checked against
  the Python packer by hand; if you change one, change both. The page
  never parses time zones: the server sends `nowMinutes` (SGT minutes
  past midnight) plus the lunch bounds and the page does integer
  arithmetic. The submitted block ORDER is the running order.
- **`Planned hours` / `Capacity (hours)` are written in HOURS** (both
  renamed from `... slots` on 2026-08-25), because `Deviation (hours)`
  subtracts them from `Actual hours`. Any other unit is silently wrong
  by 2×. Design Blocks' primary field is now `Start` — the old text
  `Name` field is gone; never write it.

Planning grid is 15 min (was 30), and since 2026-08-25 so is every
mid-day adjustment: the switch reminder carries **+15 / −15 / +15
space** buttons, all in `core/design.py`, all pure planners
(`plan_extension`, `plan_shrink`, `plan_spacer`) with the Airtable
writes in `adjust_current_block`. Keep them pure — they're the only
part testable without network.

- **+15** cascades the rest of the day **gap-first**: push the
  colliding blocks, stop the ripple at the first gap that absorbs it.
- **−15** is its mirror: pull the contiguous chain earlier by what the
  gaps don't absorb. It returns `ping_next` when the pull brings the
  next block within the reminder lead (or into the past) — the handler
  must fire that reminder, because the polling job only pings blocks
  whose Start is still in the FUTURE. Dropping that is a silent miss.
- **+15 space** must never move the running block's End: the break is
  non-project time and extending the block would bill it to a project.
  The gap opens after the current block instead.

Lunch 13:00–14:00 is an immovable obstacle throughout (shared shop
break) — pushed blocks jump past it, blocks are never pulled into it,
and an extension that would enter it is refused, not truncated;
end-of-day counts as a gap, which is why no day cutoff is needed. Never
write `Planned hours` or `Block status` from here (frozen plan; evening
pass owns status), and always clear `Switch ping sent` on a block whose
Start moves or its reminder dies silently.

The day editor (`core/day.py` + `web/static/day.html`, §13) took the
evening pass out of Airtable on 2026-09-02, and with it two rules that
used to hold everywhere: **the bot now writes `Block status` and `Day
status`** — but only from this path. `core/design.py`'s ±15 buttons
still must not touch either. Things to keep straight:

- **Every rule in `core/day.py` is pure**, and that is the point: the
  page POSTs `{day, op}` to `/api/day/apply` and re-renders the
  response. Do NOT "optimise" the arithmetic into the page's
  JavaScript — that is the §12e duplication, and this is several times
  more logic than a preview. There is no server-side draft state
  either, so a restart mid-edit costs nothing.
- **Ops are serialised client-side.** Each is computed from the day it
  is handed, so two in flight race. The page chains them.
- **`Planned hours` is never written on an existing block** (frozen
  plan) and is 0 on every block the editor creates.
- **Any block whose `Start` moves loses `Switch ping sent`** — done in
  the write layer, not per call site.
- **Save refuses when a block moved in Airtable since the editor
  loaded it.** The ±15 buttons write to the same records; a stale
  editor silently clobbering a live adjustment is a real path, and the
  guard is why it can't happen.
- **Reorder** preserves each block's duration and each *position's*
  gap, anchored at the earlier block's start — so a contiguous pair
  keeps its span and nothing after it moves. **Dropping** never closes
  the hole it leaves; the hole is evidence.
- **Confirmed vs Adjusted is derived**, never asked: Adjusted iff the
  duration differs from `Planned hours`. The two count identically
  toward `Confirmed designer-hours`, so there is nothing else in it.
- **Lunch is an obstacle everywhere except `switch_now`**, which
  records what is happening right now. Don't "fix" that inconsistency
  — refusing to record the truth is worse.

`format_switch_ping` lives in `core/design.py`, not `jobs/scheduler.py`:
a shrink fires the same reminder off-schedule, and the job module
imports the handlers, so the other direction is an import cycle.

Score snapshots (`core/snapshots.py`, 06:05 SGT) append the day's
ranking to a Google Sheet — deliberately NOT Airtable (Marcus analyses
in Sheets) and NOT a local CSV (Railway's disk is ephemeral). Must run
after the 06:00 recalc automation or scores are stale. Log the score's
INPUTS alongside the total; that's what makes counterfactual weight
tuning possible. `gspread` is imported lazily so the bot runs without
Google config.

The ranking-vs-actuals comparison layer and `/compare` were **retired
2026-08-24**. Once the Mini App offers the whole plannable list,
"ranked but skipped" carries no signal and "worked (unranked)" is
near-impossible, so the join had nothing left to say. Don't rebuild it
against the new flow without a reason the full list doesn't already
answer. Actuals live in Airtable regardless.

**"Worked" means ANY block type** (Marcus, 2026-08-04): client comms,
site meetings and admin all consume design capacity and constitute
progress — convincing a client of a choice or closing out an invoice is
as real as modelling. That decision still governs the Airtable
`Confirmed designer-hours` formula (no type filter) and `Hours
consumed`; `Modelling hours` (Design + CAM) is only a breakdown for
costing analysis. Do NOT reintroduce it as the test for whether a
project was worked on.

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

Month-end payroll (`core/payroll.py`): `/payroll` defaults to the month
that just **ended** (the current month is always partial). The prompt
job runs daily at 09:00 and returns early unless the date is the
month's first Mon–Fri — PTB can't express "first weekday of the month",
and deriving it from the date keeps the job stateless. `/lockmonth`
deliberately has NO default (month is a required argument) — unlike
/payroll, since `Locked` is terminal. Every path to it (button and
command alike) also goes through a Yes/Cancel confirmation; don't
"streamline" that into a single tap.

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
- The day editor shows ONE designer's day: shared work is still
  ignored (DESIGN_SCHEDULING.md §12c), so the app won't tell you the
  other designer already picked a project. Mirrored single-designer
  blocks (§7) still work, they're just not surfaced.
- Overnight/multi-day shifts unsupported by design.
- Admin confirms availability by ticking `Confirmed` in Airtable directly,
  then runs `/confirmweek` — the Airtable UI is intentionally part of the
  admin workflow.
- Members can edit next week's availability via `/availability` (day
  picker pre-ticked with their submission). `submit_availability` has
  SET semantics: it reconciles the final selection (creates new days,
  deletes deselected ones) and is idempotent. The week LOCKS for a
  member as soon as an admin ticks `Confirmed` on any of their days —
  after that, edits raise and go through an admin instead (protects the
  roster mid-build). Deselecting every day isn't possible via the bot
  (empty submit is blocked); full withdrawal goes through an admin.
- Onboarding is self-service: `/start` creates a `Pending` Team Members
  record (Telegram ID + username) and DMs admins; an admin sets the
  rate/role and flips Status to `Active`. Pending members can't clock in
  (`clock_in` requires Status `Active` + a rate). Admins still activate
  manually — that gate is intentional.
- Member granularity (since Aug 2026): access control is explicit, not
  inferred. `Admin` checkbox → admin commands/alerts (`is_admin`);
  `Weekly availability` checkbox → the availability cycle
  (`get_schedulable_members`); `Payroll handler` checkbox
  (fldYoqk14cihnda5h) → the month-end payroll prompt plus `/payroll`
  and `/lockmonth` (`has_payroll_access`, admins keep access too);
  `Employment type` Part-time → staleness flag. `Role` is job function ONLY (Designer/Fabricator/Communicator,
  single-select pending Marcus's multi-select conversion in the UI) —
  never gate anything on it. The old admin/part-timer/full-timer Role
  options are dead; code must not reference them.
- Lunch (13:00–14:00 SGT) is unpaid: the Airtable `Lunch (hours)` formula
  computes the shift's overlap with the window and `Duration (hours)`
  subtracts it, so all pay stays formula-derived (invariant 6). `Lunch
  (hours)` stores the overlap in SECONDS (an Airtable duration field, so
  it displays h:mm — e.g. `1:00`); `Duration (hours)` does
  `(raw_seconds − lunch_seconds)/3600`, and `clock_out` divides the field
  by 3600 to report hours. No date cutover: pre-launch, no shifts in this
  base were ever paid under the old manual-deduction process (that lived
  in another app), so the formula applies to all records.
  `lunch_overlap_hours()` in `core/timeutils.py` mirrors the overlap
  logic for the logged local fallback but returns HOURS — same logic,
  different unit; keep the two in sync. The clockout summary shows a soft
  "(− lunch)" marker, deliberately not the deducted amount (Marcus's
  preference).
- Group membership (`core/membership.py`): invariant is Status `Active`
  ⇔ in group chat. Audit runs from `/confirmweek`; removal trigger is a
  human flipping Status to `Inactive` (the bot executes ban+unban).
  Staleness (no shifts in `STALE_SHIFT_WEEKS`) is flag-only — never
  auto-flip Status, it gates pay/access. Admins are never auto-removed.
  The Bot API cannot enumerate group members: checks are roster-driven
  via `get_chat_member`, strangers detectable only via join events.
  The bot must be a group admin with ban rights.
