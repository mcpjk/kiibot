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

**Admins** (Role = `admin` in Team Members)

| Command | What it does |
|---|---|
| `/confirmweek` | DM members their confirmed days; post schedule to group chat; run the group membership audit |
| `/payroll [YYYY-MM]` | Payroll summary per member (defaults to current month) |
| `/lockmonth YYYY-MM` | Lock all completed shifts in a pay month (blocks edits) |
| `/setrate <username> <rate> [reason]` | Change a rate; writes Rate History |
| `/chatid` | Reply with the current chat's ID (run it in a group to get `TELEGRAM_GROUP_CHAT_ID`) |

## Scheduled jobs (all SGT)

| When | Job |
|---|---|
| Daily 20:00 | Prompt open shifts ("still working?"), stamp `Prompted at` |
| Daily 21:00 | Auto-close prompted shifts not confirmed since the prompt; end time = prompt time |
| Thu 22:00 | Ask members for next week's (Mon–Sat) availability |
| Fri 22:00 | Remind non-submitters |
| Sat 09:00 | Digest to admins: who has/hasn't submitted |

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

## Airtable schema contract

Table and field names are referenced by exact name in the code
(`config.py` + `core/airtable_client.py`). If you rename anything in
Airtable, update the code. Required tables/fields:

- **Team Members**: Name (primary), Telegram user ID (number), Telegram
  username, Status (Active/Pending/Inactive), Role (admin/part-timer/
  full-timer), Current hourly rate (SGD), links to other tables
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

Roles: `part-timer`s and `admin`s take part in the weekly availability
cycle; `full-timer`s are Active members (and belong in the group chat)
but are skipped by scheduling prompts/digests.

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
