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
| `/extend [minutes]` | Designers: add 15 min (or the minutes given) to the design block you're currently in, pushing the rest of the day as needed. A negative value ends it early — see below |
| `/space [minutes]` | Designers: open 15 min (or the minutes given) of unallocated time for a break or non-project work, pushing the rest of the day back |
| `/plan` | Designers: open the daily planning Mini App (pick today's projects, set type and duration) — see below |
| `/day` | Designers: open the day editor — retime, reorder, confirm or drop today's blocks, add unplanned ones, or switch to something else right now — see below |

**Admins** (`Admin` checkbox ticked in Team Members)

| Command | What it does |
|---|---|
| `/confirmweek` | DM members their confirmed days; post schedule to group chat; run the group membership audit |
| `/payroll [YYYY-MM]` | Payroll summary per member (defaults to the month that just **ended**); offers a 🔒 Lock button. Also available to `Payroll handler` members |
| `/lockmonth YYYY-MM` | Lock all completed shifts in a pay month (blocks edits); no default — month is required, and it always asks for confirmation first. Also available to `Payroll handler` members |
| `/setrate <username> <rate> [reason]` | Change a rate; writes Rate History |
| `/chatid` | Reply with the current chat's ID (run it in a group to get `TELEGRAM_GROUP_CHAT_ID`) |
| `/snapshot` | Run the design score snapshot now instead of waiting for 06:05 (verifies the Sheets chain) |

## Scheduled jobs (all SGT)

| When | Job |
|---|---|
| Daily 20:00 | Prompt open shifts ("still working?"), stamp `Prompted at` |
| Daily 21:00 | Auto-close prompted shifts not confirmed since the prompt; end time = prompt time |
| Thu 22:00 | Ask members for next week's (Mon–Sat) availability |
| Fri 22:00 | Remind non-submitters |
| Sat 09:00 | Digest to admins: who has/hasn't submitted |
| First weekday of the month, 09:00 | Payroll prompt to `Payroll handler` members: button runs `/payroll` for the month just ended, then a 🔒 Lock button (with confirmation) |
| Mon–Fri 10:00 | Planning prompt: DM designers the "Plan today" Mini App button |
| Every 2 min | Switch reminder: DM designers ~5 min before their next Design Block starts (carries the ±15 / space buttons and an "Edit day" button) |
| Mon–Fri 18:30 | Day-confirmation prompt: DM designers with an unconfirmed day the "Edit day" button ("How did today actually go?") |
| Daily 06:05 | Score snapshot: append today's design-priority ranking to the Google Sheet (if configured) |

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
  reminders to fire. Each reminder carries three inline buttons —
  **⏱ +15 min task**, **⏪ −15 min task**, **☕ +15 min space** (buttons,
  not extra lines, so the preview stays one line). They're meant to be
  tapped repeatedly; each tap edits the message in place so the
  keyboard stays under your thumb.
- **Daily planning Mini App** (`/plan`, or the Mon–Fri 10:00 prompt):
  a Telegram Web App where designers pick today's projects from the
  full plannable list (Process Designing/Fabricating, excluding
  Pending client), then set a block type and duration for each. The
  duration starts at *hours available ÷ projects picked* on a 15-min
  grid and recalculates live. Blocks are laid end to end from now
  (rounded up to the next 15 min), skipping the 13:00–14:00 lunch
  hour, and written as `Planned`. Step 2 shows each block's
  **start–stop preview** and lets you **reorder** the projects with
  ▲▼ — the order on that screen is the order the day is laid out in.
  The list is ordered exactly like Marcus's Airtable view (Status,
  Delivered, Process, Priority score, Lead date); the score no longer
  rations the list, and is now only one of five sort keys. Needs
  `WEBAPP_URL`; unset, the whole planning layer disables and the rest
  of the bot runs unchanged.
- **`/extend` (gap-first cascade)**: adds time to the block you're
  *currently in* — note the reminder announces the *next* block while
  the button adjusts the running one, which is the point: you're
  overrunning (or finishing early on) the current task. Because blocks are nearly always
  back-to-back, an extension usually collides, so the bot pushes the
  colliding blocks forward and **stops the ripple at the first gap that
  can absorb it**; later blocks keep their planned times. The
  13:00–14:00 lunch hour is immovable (shared shop break): a pushed
  block that would land in it jumps to *after* lunch, and an extension
  that would itself run into lunch is refused rather than truncated.
  End-of-day counts as a gap, so the last block just runs later and the
  ripple always terminates. `Planned hours` and `Block status` are
  never touched — the plan stays frozen and the evening pass still owns
  Confirmed/Adjusted. Moved blocks get their `Switch ping sent` cleared
  so they re-ping at the new time.
- **−15 min (`/extend -15`)**: the mirror. The running block ends 15
  min early and the contiguous chain behind it is pulled earlier by
  whatever the gaps in between don't already absorb; a block is never
  pulled into lunch, never ends before *now*, and never drops below
  15 min (a block that didn't happen is `Dropped` in the evening pass
  instead). If the pull brings the next block to within the reminder
  lead — or into the past — the bot fires that switch reminder
  immediately, since the polling job only ever pings blocks whose
  Start is still in the future.
- **+15 min space (`/space`)**: opens unallocated time for a break or
  non-project work. Deliberately **not** an extension: the running
  block's End does not move, so the time never lands on a project's
  attributable hours; the gap opens after it and the rest of the day
  is pushed back with the same gap-first cascade. With nothing
  running, the space starts now (snapped up to the grid).
- **`/day` (the day editor)**: today's blocks as a tappable timeline —
  the alternative to editing `Start`/`End`/`Block status` field by
  field in Airtable, which is clunky on a phone and impossible to
  reorder (Airtable has no reorder; `Start`/`End` are absolute
  datetimes). Per block: **±15 on either boundary**, **▲▼ to reorder**,
  and **Confirm / Adjust / Drop**. Plus **＋ Add block** for something
  unplanned and **⚡ Switch now** for the delivery-arrives case — it
  cuts the running block at the nearest 15 min, drops in what you're
  actually doing, optionally re-queues the rest of the interrupted
  task, and pushes the day back gap-first. Nothing writes until you
  save; the save is one batched update and one batched create, and it
  **refuses if a block moved in Airtable while you were editing**
  (the ±15 buttons write to the same records). Ticking *Confirm the
  day* resolves every remaining `Planned` block into **Confirmed** or
  **Adjusted** — derived from whether it ran to its planned length,
  since that is all the two statuses mean — and flips `Day status`.
  `Planned hours` is never rewritten (the plan stays frozen, so edits
  read as deviation); added blocks carry 0. Reach it from `/day`, from
  the **✏️ Edit day** button on every switch reminder, or from the
  **18:30 Mon–Fri** prompt. Needs `WEBAPP_URL`.
- **Score snapshots** (06:05 SGT, after the 06:00 Airtable recalc
  automation): one row per design candidate appended to a Google Sheet
  — date, rank, project, score, and the score's *inputs* (tier,
  days-since-touch, due, status, touched-yesterday), so alternative
  weights can be tested against history with sheet formulas. A daily
  log of the sort order, not of a decision — since the Mini App offers
  the whole list, the score doesn't choose anything. Enabled by
  `GOOGLE_SERVICE_ACCOUNT_JSON` + `SCORE_SNAPSHOT_SHEET_ID` (see
  `.env.example`); disabled cleanly when unset. Failures DM the admins
  and leave a visible gap — never silent wrong data. **Env vars only
  take effect on process start — restart the service after setting
  them.** Startup logs say either "Score snapshot enabled…" or
  "Score snapshot disabled…"; `/snapshot` runs it on demand to verify
  the chain end-to-end.

The **ranking-vs-actuals comparison layer and `/compare` were retired
on 2026-08-24**: with selection free from a full list, "ranked but
skipped" carries no signal and "worked (unranked)" is near-impossible.
Actuals live in Airtable regardless.

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
