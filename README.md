# Kii-bot

Telegram shift-management bot backed by Airtable. Team members clock in/out
via Telegram; shifts, rates, and weekly availability live in the Kii master
Airtable base. All times are Asia/Singapore; pay is in SGD.

## Commands

**Members**

| Command | What it does |
|---|---|
| `/start` | Self-registers you as a *Pending* member (captures your Telegram ID + username, DMs admins); active members get a command overview and a persistent Clock in / Clock out button keyboard |
| `/clockin` | Start a shift (rate is snapshotted at clock-in) |
| `/clockout` | End your shift; shows duration and gross pay |
| `/confirmshift` | Reply to the 20:00 "still working?" prompt to avoid auto-close |
| `/myshifts` | Recent shifts + current month totals |
| `/myrate` | Your current hourly rate |
| `/editshift` | Request a correction to a closed shift (admin approves) |
| `/availability` | View/edit next week's submitted availability (locked once an admin starts confirming your days) |
| `/extend [minutes]` | Designers: add 30 min (or the minutes given) to the design block you're currently in, pushing the rest of the day as needed — see below |

**Admins** (`Admin` checkbox ticked in Team Members)

| Command | What it does |
|---|---|
| `/confirmweek` | DM members their confirmed days; post schedule to group chat; run the group membership audit |
| `/payroll [YYYY-MM]` | Payroll summary per member (defaults to the month that just **ended**); offers a 🔒 Lock button. Also available to `Payroll handler` members |
| `/lockmonth YYYY-MM` | Lock all completed shifts in a pay month (blocks edits). Also available to `Payroll handler` members |
| `/setrate <username> <rate> [reason]` | Change a rate; writes Rate History |
| `/chatid` | Reply with the current chat's ID (run it in a group to get `TELEGRAM_GROUP_CHAT_ID`) |
| `/snapshot` | Run the design score snapshot now instead of waiting for 06:05 (verifies the Sheets chain) |
| `/compare [YYYY-MM-DD]` | Write the ranking-vs-actuals comparison for a day (defaults to yesterday) |

## Scheduled jobs (all SGT)

| When | Job |
|---|---|
| Daily 20:00 | Prompt open shifts ("still working?"), stamp `Prompted at` |
| Daily 21:00 | Auto-close prompted shifts not confirmed since the prompt; end time = prompt time |
| Thu 22:00 | Ask members for next week's (Mon–Sat) availability |
| Fri 22:00 | Remind non-submitters |
| Sat 09:00 | Digest to admins: who has/hasn't submitted |
| First weekday of the month, 09:00 | Payroll prompt to `Payroll handler` members: button runs `/payroll` for the month just ended, then a 🔒 Lock button (with confirmation) |
| Every 2 min | Switch reminder: DM designers ~5 min before their next Design Block starts |
| Daily 06:05 | Score snapshot: append today's design-priority ranking to the Google Sheet, and yesterday's ranking-vs-actuals comparison (if configured) |

Jobs are **stateless** — all state (Prompted at / Confirmed at) lives in
Airtable, so restarting the bot at any time loses nothing.

## Group membership audit

Invariant: **Status `Active` ⇔ in the group chat** (all roles). The audit
runs with `/confirmweek` and:

- removes `Inactive` members from the group (ban + immediate unban, so
  they can be re-invited later) — flipping Status to Inactive in Airtable
  is the removal trigger; the bot does the kicking so nobody has to
- reports Active members missing from the group
- flags Active part-timers with no shift in 5 weeks (review only — the
  bot never flips Status itself)
- never auto-removes admins

Between audits, join/leave events alert admins (stranger joined, Active
member left). Requires the bot to be a **group admin with ban rights**
and `TELEGRAM_GROUP_CHAT_ID` set; without them the audit degrades to
report-only. The Bot API can't list group members, so all checks go
roster → Telegram, member by member.

## Design scheduling (in progress)

The design scheduling & time-tracking system (Projects / Design Blocks /
Design Days in the same base) is specified in **DESIGN_SCHEDULING.md** —
read it before touching anything design-related; it encodes the scoring
engine, schema, platform quirks, and roadmap. Bot involvement so far:

- **Switch reminders**: every 2 min the bot looks for Design Blocks
  starting in the next ~5–8 min and DMs the linked designers
  ("📐 14:00: Espira Spring 1 (1.5 h), CAM"). Deliberately one short
  line — the phone's notification preview should carry the whole
  message, so no words are spent on instructions the designer can
  infer. Dedupe lives in the block's `Switch ping sent` field
  (stateless, restart-safe); Dropped blocks never ping. Blocks must be
  created with future Start times (the morning-planning protocol) for
  reminders to fire. Each reminder carries an inline **⏱ +30 min on
  current task** button (a button, not a second line, so the preview
  stays one line).
- **`/extend` (gap-first cascade)**: adds time to the block you're
  *currently in* — note the reminder announces the *next* block while
  the button extends the running one, which is the point: you're
  overrunning the current task. Because blocks are nearly always
  back-to-back, an extension usually collides, so the bot pushes the
  colliding blocks forward and **stops the ripple at the first gap that
  can absorb it**; later blocks keep their planned times. The
  13:00–14:00 lunch hour is immovable (shared shop break): a pushed
  block that would land in it jumps to *after* lunch, and an extension
  that would itself run into lunch is refused rather than truncated.
  End-of-day counts as a gap, so the last block just runs later and the
  ripple always terminates. `Planned slots` and `Block status` are
  never touched — the plan stays frozen and the evening pass still owns
  Confirmed/Adjusted. Moved blocks get their `Switch ping sent` cleared
  so they re-ping at the new time.
- **Score snapshots** (06:05 SGT, after the 06:00 Airtable recalc
  automation): one row per design candidate appended to a Google Sheet
  — date, rank, project, score, and the score's *inputs* (tier,
  days-since-touch, due, status, touched-yesterday), so alternative
  weights can be tested against history with sheet formulas. Compare
  against the Design Blocks actually created that day to tune the
  score. Enabled by `GOOGLE_SERVICE_ACCOUNT_JSON` +
  `SCORE_SNAPSHOT_SHEET_ID` (see `.env.example`); disabled cleanly
  when unset. Failures DM the admins and leave a visible gap — never
  silent wrong data. **Env vars only take effect on process start —
  restart the service after setting them.** Startup logs say either
  "Score snapshot enabled…" or "Score snapshot disabled…"; `/snapshot`
  runs it on demand to verify the chain end-to-end.
- **Comparison** (same 06:05 run, for *yesterday* — by then the evening
  pass is done): joins that morning's frozen ranking against the design
  blocks actually recorded, into a `Comparison` worksheet. Each row is
  one project-day with rank, score, hours, modelling hours, block
  types, and an outcome:
  - `Worked` — ranked and actually done (agreement)
  - `Ranked but skipped` — score said urgent, the day said otherwise
  - `Worked (unranked)` — **the blind-spot signal**: real work on a
    project the ranking never contained, i.e. something the score
    doesn't model

  `Worked` counts **any** block type: client comms, site meetings and
  admin all consume design capacity and move a project forward.
  `Modelling hours` (Design + CAM) is a breakdown column for costing
  analysis, not a definition of real work. `/compare [YYYY-MM-DD]`
  backfills or re-runs any day.

## Airtable schema contract

Table and field names are referenced by exact name in the code
(`config.py` + `core/airtable_client.py`). If you rename anything in
Airtable, update the code. Required tables/fields:

- **Team Members**: Name (primary), Telegram user ID (number), Telegram
  username, Status (Active/Pending/Inactive), Employment type
  (Part-time/Full-time), Role (job function: Designer/Fabricator/
  Communicator/…), Admin (checkbox), Weekly availability (checkbox),
  Payroll handler (checkbox), Current hourly rate (SGD), links to other
  tables
- **Shifts**: Member (link), Start time, End time, Hourly rate snapshot (SGD),
  Status (Open/Closed/Auto-closed/Edit-approved/Locked),
  Source (how the shift was created: Telegram/Console/Manual/Edit-approved),
  Lunch (hours) *(formula, seconds → shown as h:mm)*,
  Duration (hours) *(formula, net of lunch)*,
  Gross pay (SGD) *(formula)*, Pay month *(formula, 'YYYY-MM')*,
  Prompted at, Confirmed at
- **Shift Edit Requests**: Shift (link), Requested by (link), Original/
  Requested start/end, Reason, Status (Pending/Approved/Rejected),
  Reviewed by (link), Reviewed at, Admin notes
- **Availability**: Member (link), Date, Confirmed (checkbox),
  Notified (checkbox), Week starting *(formula, Monday ISO date)*
- **Rate History**: Member (link), Rate (SGD), Effective from, Changed by, Reason

Member granularity: **`Admin`** (checkbox) gates admin commands and
alerts; **`Weekly availability`** (checkbox) is the explicit, per-member
switch for the whole availability cycle (prompts, reminders, digest,
`/availability`) — the bot never infers it; **`Payroll handler`**
(checkbox) receives the month-end payroll prompt and may run `/payroll`
and `/lockmonth`; **`Employment type`** drives
the staleness flag (Part-time only); **`Role`** is job function only
(Designer/Fabricator/Communicator) and feeds function-specific features
like design scheduling — it no longer carries access control.

**Duration and Gross pay are computed by Airtable formulas** — the bot reads
them back rather than recomputing, so Airtable is the single source of truth
for pay figures.

Lunch (13:00–14:00 SGT) is unpaid: `Lunch (hours)` is the shift's overlap
with that window and `Duration (hours)` subtracts it. Clockout
summaries mark the deduction as "(− lunch)".

Note: Airtable formulas render linked-record fields as the linked record's
primary field (its *name*), so formulas can't filter by linked record ID.
The client filters linked records client-side instead — don't "simplify"
queries back to `FIND('rec…', ARRAYJOIN({Member}))`; that never matches.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in tokens
python setup_airtable.py   # once, against a fresh base (see its docstring)
python main.py
```

Requires Python 3.9+ (uses `zoneinfo`); 3.11+ recommended.

## Running in production

Long polling — no inbound ports or webhook needed, just outbound HTTPS.
Run under a supervisor that restarts on failure, e.g. systemd:

```ini
# /etc/systemd/system/kii-bot.service
[Unit]
Description=Kii shift bot
After=network-online.target

[Service]
WorkingDirectory=/opt/kii-bot
ExecStart=/opt/kii-bot/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Tests

```bash
pip install pytest
pytest
```
