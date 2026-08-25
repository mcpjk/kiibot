# Kii Design Scheduling & Time-Tracking System

**Base:** Kii master base (`appzTLEjQPg1DAe2m`) · **Users:** Marcus, Nauf · **Units:** hours (**0.25 h grid** since 2026-08-24, §12) · **TZ:** Asia/Singapore
**Status as of 2026-08-24:** kii-bot integration (repo `mcpjk/kiibot`, deployed on Railway). Live: **switch reminders** (DM ~5 min before each block starts, one notification-sized line, with a **+30 min** button), **`/extend`** (gap-first mid-day overrun cascade), **morning score snapshots** to Google Sheets (§11). **New: the daily planning Mini App (§12) replaces the score-driven ration** — designers pick from the full plannable list each morning and the bot lays out the blocks. The **ranking-vs-actuals comparison layer and `/compare` are retired** (§11); its three weeks of data are extracted into §11c. `Confirmed touch` type filter restored 2026-08-03 (§9.1 resolved) — but see §11a: the decision that **all block types are design work** may argue for reverting it. Prior status (2026-07-20): 47 blocks captured as a retrospective journal; planning layer unused (§9a); estimation removed (§9.2).

---

## 1. Purpose & design principles

Measure actual design effort per project (the missing half of Kii's costing — fabrication is already accounted for) while *reducing* the designers' daily cognitive load, not adding a timesheet burden. Secondary goals: retrospective reference data to calibrate design-time intuition; reduction of the high-load "which project now?" state; protection against the availability-bias pathology where low-priority projects drift for months unseen.

Principles the system is built on — future changes should preserve these:

- **Pre-filled timesheet, not blank timesheet.** The system drafts the day (the ration); designers correct deviations in the evening. Correction is cheaper than composition (default effect). A lazily-corrected day still yields approximately-right data.
- **Silence ≠ data.** Nothing becomes a recorded actual without an explicit confirmation act. Plans and actuals are separate objects; unconfirmed days are visibly hollow.
- **The plan is frozen at creation** (`Planned` value written once, never edited). Plan-vs-actual deviation is itself a first-class metric: it measures how interrupt-driven the practice is and whether rationing survives reality.
- **Scoring is decision support, not a decider.** Deterministic, auditable formula — every ranking disagreement can be traced to a term and either the weight or the gut is corrected. No AI in the ration decision.
- **Human judgment enters at three fixed frequencies only:** intake (owner, design due), weekly (priority tier), morning (capacity + tap-to-select). Everything else is computed.
- **The morning view must force neglect signals into view** (days untouched, hours remaining, due date) — this is the bias-correction mechanism. Don't bury them.
- **Never delete blocks; use `Dropped`.** Deletion erases deviation evidence. (Exception: test fixtures.)
- **Measured data never mixes with estimated data in the same field.** (Backfilling was considered and rejected; `Partial design record` checkbox marks lower-bound projects instead.)

## 2. Architecture & flow

```
Projects (scoring fields + gate) ──ranked by──▶ Morning view (candidates by Priority score)
        ▲                                              │ human selects, declares capacity
        │ rollups (Hours consumed,                     ▼
        │          Last design touch)          Design Blocks created (Planned, frozen plan)
        │                                              │ day happens; drag/adjust in Timeline
        └────────── Design Blocks ◀── evening pass: statuses → Confirmed/Adjusted/Dropped,
                    (linked to Design Days)            Day status → Confirmed
```

Scores feed selection → selection creates blocks → confirmed blocks feed rollups → rollups move tomorrow's scores. The measurement loop and scheduling loop close on each other with no separate data entry.

## 3. Schema reference

### Projects (`tbl4dydfirupmpYtz`) — scheduling-relevant fields

| Field | Type | Role |
|---|---|---|
| `Status` | select: Lead / Pending client / Confirmed / Cancelled | Commercial/activity state. Pending client = short-term, awaiting client input, design cannot progress |
| `Process` | select: Designing / Fabricating / Pending delivery / Delivered (Pause being removed, see §9) | Lifecycle stage. Only `Designing` is design-schedulable |
| `Design owner` (`fldpwWSh5gj5BRbJU`) | link → Team Members | Routes candidates to designer |
| `Design estimate (hours)` (`fldkGUQNIeezHKeVE`) | number | **Deleted by decision 2026-07-20** (still in schema at last fetch — delete `Remaining hours` first, then this). Rationale: current estimates too unreliable to be a useful input; the calibration loop is deferred, not abandoned — estimation returns as reference-class figures derived from recorded actuals (§10.4) |
| `Design Due` (`fldNr650JHwijPee1`) | date | When design must *finish* (≠ project Deadline) |
| `Priority tier` (`fldZmSMrMX4FiV8eC`) | select: High / Mid / Low | The batched weekly judgment |
| `Design Blocks` (`fld1ZIWHGslktxdmE`) | link | Pipe for the rollups |
| `Last design touch` (`fldu5OSfFr1SIlmPM`) | rollup | MAX of `Confirmed touch` — latest confirmed block. ⚠ Currently polluted: `Confirmed touch` exposes all types (§9.1), so comms/site blocks reset the neglect clock |
| `Hours consumed` (`fldwc37f4r5vxiXPV`) | rollup | SUM of `Confirmed designer-hours` — **since 2026-07-20 counts ALL block types** (Design, CAM, comms, site, admin): total attributable designer effort per project, the cost-to-serve metric. The CAD/CAM/comms split stays recoverable at any time by filtering blocks by `Block type`. Consider renaming to `Total attributable hours` so the name states the scope |
| `Days since touch` (`flditVeMa4Uk9yDoz`) | formula | Falls back to Confirmed date → Lead date → 0 for never-touched projects |
| `Touched yesterday` (`fldrX5emvnVuflc1O`) | formula | Feeds continuation bonus |
| `Remaining hours` (`fldk8NWZA7WXR90Qq`) | formula | **Deleted by decision 2026-07-20** (delete before `Design estimate`, which it references) |
| `Design candidate?` (`fldYcYYB0c10TFfBm`) | formula | The gate; see §4 |
| `Priority score` (`fldSRqYiVJpGwLnM0`) | formula | The ranking; see §4 |
| `Partial design record` (`fldDw3e2K5LXDgQg1`) | checkbox | Design predates tracking → Hours consumed is a lower bound. Exclude from calibration analysis; valid for everything else |
| `Recalc ping` (`fldfM6BN5MFVM2XOS`) | dateTime | Written by 6am automation. Hidden, never hand-edited |

#### 3a. Value-capture fields (added 2026-07, Projects table)

`Est. value/quote` (number, SGD, entered at quoting/intake) · `Billed` (number, SGD, entered when charging is settled) · `project value` (coalesce: Billed if present, else Est. value/quote — the single reference field for all downstream value analysis). Purpose: capture project value from intake onward so design hours on small/cancelled/never-invoiced projects aren't excluded from the SGD-per-design-hour analysis. Deliberately **not** wired into `Priority score` — the sequencing/pricing distinction (§7) was tested against live morning-view discomfort and resolved to a pricing problem; the fix is upstream intake policy once the data justifies it, not a score term. Quote-vs-billed deviation on completed projects is itself a scope-creep metric. Conventions still to pin down: GST basis (use one basis in both fields), staged-billing meaning (recommend: total contracted value).

### Design Blocks (`tblMq9wqXC7cWl7Qx`) — one record per contiguous chunk of attention

| Field | Type | Notes |
|---|---|---|
| `Start` (`fldk2brYc333Fccyg`) / `End` (`fldKNG6quDk5i03k3`) | dateTime | Both real editable fields so Timeline drag-to-move *and* drag-to-resize work. `Start` is load-bearing: `Confirmed touch` (and thus the neglect signal) dies silently without it. Keep to the 0.5 h grid |
| `Project` / `Designers` / `Day` | links | `Designers` multi = shared block (effort counts × headcount). Link shared blocks to *both* designers' Day records |
| `Block type` | select: Design / CAM / Client comms / Site–meeting / Admin | `CAM` added 2026-07-13: CAM time is comparatively predictable from part size/complexity, whereas CAD time is driven by client requirements, revisions, and in-process discovery — separating them gives the future costing analysis a stable vs. volatile component. **Design and CAM blocks count** toward Hours consumed and Last design touch (⚠ formula update pending, §9.2); comms/site/admin are recorded and project-attributable but are not design effort |
| `Block status` | select: Planned / Confirmed / Adjusted / Dropped | Planned = provisional, not data. Dropped = planned-but-didn't-happen (never delete) |
| `Planned hours` (`fldvmUaahASQLPITS`) | number | Frozen plan, written once at creation; unplanned evening additions carry 0. Renamed from `Planned slots` 2026-08-25 (§9.9 resolved); unit is hours |
| `Actual hours` (`fldEp0OaN7lxFqO4h`) | formula | `(End − Start) in minutes / 60`. Off-grid blocks self-flag as odd decimals (0.3 h) — deliberately not rounded |
| `Deviation (hours)` (`fldIBtBlccWWEwD67`) | formula | Actual − planned; Dropped → −planned. Day-level Σ\|deviation\| = adherence metric |
| `Confirmed designer-hours` (`fldX0GVbIi0eMuzkd`) | formula | Actual × designer count if Confirmed/Adjusted, else 0 — **no type filter since 2026-07-20** (all block types count; feeds the total-attributable `Hours consumed` rollup). Verified deployed 2026-07-20 |
| `Confirmed touch` (`fldEhR5hivqQVB73W`) | formula | Start, exposed if Confirmed/Adjusted. ⚠ **Deployed formula has no type condition** — should expose only type ∈ {Design, CAM}, else comms/site/admin blocks reset the neglect clock (§9.1) |
| `Switch ping sent` (`fld7AQsYX5YjGhDqx`) | dateTime | **Bot-written** dedupe marker for the switch-reminder DM (§11). Never hand-edit |
| `Count (Designers)` (`fldttDjmwLr2buUTZ`) | count | Exists because `COUNTA()` on a linked field returns 1 (string coercion) — see §8 |

### Design Days (`tblkv78uCOg8f2oTv`) — one record per designer per working day

`Date` · `Designer` (link) · `Capacity (hours)` (renamed 2026-08-25) · `Day status` (Draft/Confirmed — **the** confirmation act, one flip per person per day) · `Design Blocks` (link). Purpose: day-level confirmation semantics + variable daily capacity declaration (design capacity varies with fabrication/site days; the ration can't exceed declared capacity).

## 4. The scoring engine

**Gate** — `Design candidate?` should read (verify deployed text, §9):
```
AND({Process} = "Designing", {Status} != "Cancelled", {Status} != "Pending client")
```
Pending client excluded per decided semantics: design cannot progress without client input. Note `Days since touch` keeps counting through exclusion, so released projects re-enter the ranking with accumulated urgency — dormancy converts to re-entry priority automatically. The "chase silent clients" prompt has no home in the morning view; if needed, a separate view (Status = Pending client, sorted oldest `Status/Process last modified` first).

**Score** — `Priority score` (user edited tier labels in UI; verify strings match High/Mid/Low exactly):
```
IF({Design candidate?},
  2 * MIN({Days since touch}, 21)
  + IF({Design Due}, POWER(MIN(40, MAX(0, 25 - DATETIME_DIFF({Design Due}, TODAY(), 'days'))), 1.4), 0)
  + 8 * SWITCH({Priority tier}, "High", 3, "Mid", 2, "Low", 1, 0)
  + SWITCH({Status}, "Confirmed", 10, "Lead", 4, 0)
  + IF({Touched yesterday}, 6, 0))
```
Term semantics: **neglect** rises 2/day, capped at 42 so abandonment can't drown a real deadline; **deadline pressure** zero until 25 days out, nonlinear (^1.4) approach, capped ~just past due (blank Design Due contributes 0 — deliberate; the neglect term carries deadline-less projects, which was the original $600-project pathology); **tier** 8–24 pts, the weekly human weight; **status** Confirmed 10 / Lead 4; **continuation** +6 favours finishing pushes over rotation (a touched-yesterday *penalty* was considered and rejected — it forces fragmentation).

**Tuning protocol:** weights (2, 8, 6, 10/4, cap 21, window 25, exponent 1.4) are seeds. Each time the ranking argues with gut instinct, note which term caused it and who was right on reflection. That log is the entire tuning input. First 1–2 weeks: all legacy projects have saturated neglect (fallback dates are old), so ranking is effectively tier + deadline + status until real touches differentiate — expected, not broken; tier carries extra weight during this window.

## 5. Operating protocol

- **Morning (per designer):** open morning view (`Design candidate? = 1`, sort score desc, showing score / days-since-touch / remaining hours / Design Due / tier — signals visible, everything else hidden). Create today's Design Day, declare capacity in hours. Select projects, create blocks on the Timeline with Start/End; set `Planned` value; status `Planned`; link project, designer(s), day.
- **During the day:** blocks are switch cues and re-entry rules — a client ping mid-block goes to the comms block unless genuinely urgent. (External notification layer — calendar sync — deliberately deferred.)
- **Evening (~1 min):** drag/resize blocks to what actually happened; add unplanned blocks (Planned = 0); mark abandoned ones `Dropped`; flip surviving blocks Confirmed/Adjusted; flip `Day status` → Confirmed. Unconfirmed = hollow, visible as such (Timeline colored by Block status: Draft grey vs Confirmed green).
- **Weekly review (~10 min, both designers):** re-tier priorities, sanity-check Design Due dates, sweep Pending client for stale silences. This ritual is a first-class system component — the score's tier term is only as good as this meeting.
- **Intake (per new project):** set Design owner, Design Due (design finish, from Fabrication start or Deadline minus fab lead-time). Leave `Partial design record` unchecked. (Design estimate removed from intake 2026-07-20.)
- **Project close (future):** inspect total attributable hours against `project value` (§3a) and the block-type *distribution* (fragmentation, design-vs-comms split), not just totals; heavily fragmented projects carry switching-cost overhead beyond their summed hours.

## 6. Automations

**6am recalc (live):** scheduled 06:00 SGT → Find records (`Design candidate? = 1`) → Repeating group → Update record: `Recalc ping` ← run time. Exists because Airtable recalculates `TODAY()` formulas lazily — an unopened base serves stale scores at 8am, mis-ranking at the exact decision moment (nonlinear terms and the Touched-yesterday boolean distort ordering, not just magnitude). Runs server-side; local machines/sleep irrelevant. **If morning scores ever look stale, check this automation's run history first.**

**Block-creation automation (deferred, deliberately):** to be designed *after* 1–2 weeks of the manual protocol, encoding the ritual as actually lived. When built, it writes `Planned` and Start/End, closing the "blocks can exist without Start" softness.

## 7. Conventions & data rules

- Delete nothing; `Dropped` preserves deviation evidence. (Test fixtures are the one deletion exception.)
- **Shared-work convention resolved by practice (2026-07-15, Woofer box):** two mirrored single-designer blocks, not one multi-designer block. Adopt as *the* convention; the bot inherits it (simpler — no multi-designer packing logic needed). The `Count (Designers)` multiplier becomes a safety net rather than the norm.
- Half-hour grid is convention, not structure (Start/End are free datetimes). Observed adherence in the manual period: 100% on-grid.
- Client's budget is deliberately **not** in the score — budget-correlates-with-attention is the bias the system corrects.
- New Status options default to *excluded* from candidacy until deliberately added to the gate. If Status options multiply, invert the gate to an allowlist (`OR(Status="Confirmed", Status="Lead")`) so the failure direction is safe.

## 8. Platform quirks & API limitations (hard-won)

1. **`TODAY()`/`NOW()` recalc is lazy** — hence the 6am automation (§6).
2. **`COUNTA({linked field})` returns 1** — linked-record fields coerce to a single comma-joined string in formulas (lookups behave as arrays; links don't). Use a native Count field.
3. **No `^` operator** in Airtable formulas — `POWER(base, exp)`.
4. **MCP/API cannot create:** tables, rollup fields, count fields (manual, via UI). **Can create:** most field types incl. formula and linked-record. Formula text can be written with field *names*; stored as IDs → rename-proof afterward.
5. **This connection's approval pattern:** `create_*` calls approved; `update_field`/`update_table` calls declined → formula edits and renames are done by Marcus in the UI.
6. **Timeline view** needs a real editable End field — a formula End kills drag-to-resize, which the evening pass depends on.
7. **Renaming a select option relabels every record on it; it never moves data between fields.** Migrate records deliberately, then delete empty options.
8. **`DATETIME_FORMAT()` returns text** — breaks MAX rollups; roll up raw datetimes.
9. Rollup filtering strategy: bake conditions into block-level formulas (`Confirmed designer-hours`, `Confirmed touch`) so Projects rollups stay condition-free — no silent misconfiguration surface.

## 9. Outstanding items (in priority order)

1. **✅ RESOLVED 2026-08-03 — `Confirmed touch` type filter restored** (but revisit under §11a: if all block types are design work, the neglect clock arguably should reflect that too). Original entry: **Restore `Confirmed touch` type filter (UI-side):** the 2026-07-20 edit that removed the type condition from `Confirmed designer-hours` (correct) also left `Confirmed touch` with *no* type condition (verified in deployed schema 2026-07-20). Result: comms/site/admin blocks reset the neglect clock — observed live (13 Jul site meeting, 14 Jul comms blocks all carry touch dates). Fix: wrap with `AND(..., OR({Block type} = "Design", {Block type} = "CAM"))`. The two formulas are deliberately asymmetric: hours = all types (cost), touch = design motion only (neglect signal).
2. **Delete `Remaining hours`, then `Design estimate (hours)`** (decision 2026-07-20; both still in schema at last fetch — that order, since Remaining references the estimate). Update morning view columns accordingly.
3. **Data hygiene:** zero-duration block `rec5vfmNJZYhBJw2T` (2026-07-16, no type, no project, 0 h, Confirmed) — fill in or delete (test-fixture exception applies). Three empty Design Days fixture records from 2026-07-07 (`recGhRkk7UyVdJ6CI`, `recJLTUJWFCCWHCBg`, `recdIUSLiqEZG6T0M`) — delete or repurpose when the Day layer comes into use.
4. **Decide the fate of the Day-confirmation layer:** Design Days is unused in practice (§9a) — blocks are confirmed individually at creation, no capacity declaration, no Day-status flip. Either (a) drop Day-level semantics and accept per-block confirmation as the act (document it), or (b) restore the Day layer through the bot, which needs it anyway as the container for capacity + calendar-informed packing. Recommendation: (b) — don't hand-build a habit the bot will automate; but then the deviation/adherence metrics stay empty until the bot ships (accepted cost, see §9a).
5. **Pause removal, half-done at handoff:** set project `recLwzT6BDuEeeGNC` (Status=Pending client, Process=Pause) to its correct Process — likely `Designing` — *then* delete the Pause option from Process. The other 14 Pause records are all Cancelled; their Process blanking is harmless. Skipping step one leaves that project silently unschedulable forever once its client responds.
6. **Verify the deployed gate formula** contains both `!= "Cancelled"` and `!= "Pending client"`, and that the score's SWITCH strings exactly match the renamed tier options (a SWITCH mismatch silently returns 0, indistinguishable from "no tier set" — check one High project shows 24 tier points).
7. **Launch pass** (one sitting): tick `Partial design record` for all mid-design projects; fill Design Due, Design owner, tier for the same set. (Estimate no longer part of this pass.)
8. **Value-field conventions to pin down** (§3a): GST basis consistent across `Est. value/quote` and `Billed`; staged-billing meaning of `Billed` (recommend: total contracted value, not latest invoice).
9. **✅ RESOLVED 2026-08-25 — unit rename done:** `Planned slots` → `Planned hours`, `Capacity (slots)` → `Capacity (hours)`. Note the rename broke plan submission until the bot's write code caught up (it addresses these fields by NAME): both live in `core/planning.py` / `core/airtable_client.py` and are pinned by a test. Stale field *descriptions* still say "slots".

## 9a. Manual-period findings (8–17 Jul, 47 blocks, analysed 2026-07-20)

**The protocol as lived is a retrospective time journal, not a morning ration.** Zero blocks carry a `Planned` value; every block is status `Confirmed` (no Planned/Adjusted/Dropped ever used); creation timestamps cluster in the evening or the *next day* (10 Jul CAM block created 11 Jul 16:07; 16 Jul blocks created 17 Jul); Design Days untouched. Consequence: plan-vs-actual deviation and adherence — the metrics that were to answer "does rationing survive reality" and drive weight tuning — have **no data**. The measurement half works (attribution is complete: every real block has project, type, designer, on-grid times); the decision-support half never engaged. Reading: composing the day's record retrospectively is *more* effortful than the correct-the-plan flow the system intended (default effect inverted), which is the strongest evidence yet that the morning layer needs the bot to exist rather than more discipline.

**Volume & shape (8 working days, both designers):** 53 h recorded — Design 26 h (49%), CAM 16.5 h (31%), Client comms 9 h (17%), Site 1 h, Admin 0.5 h. Marcus 20 h (60% design, 35% comms, no CAM); Nauf 33 h (design + all CAM). Recorded design-side time per designer-day ≈ 2–5.5 h, median ~3 h — the realistic capacity envelope for the bot's default prompt. 16 Jul nearly empty (1 h) — recording lapse vs light day is indistinguishable in a journal; a daily bot ping resolves this ambiguity class.

**Fragmentation is the operating reality:** 46 real blocks, median 1.0 h; 70% ≤ 1 h; only 4 blocks ≥ 2.5 h (the longest, 4 h, is CAM). Each designer touches 2–4 projects/day; 14 distinct projects touched in 8 days; one project (Dog perch) absorbed 32% of all effort. Comms is already 17% of attributable time after one week — early signal for the small-project cost hypothesis (§3a), too little data to conclude.

**Bot design parameters this fixes:** default chunk 1–1.5 h for Design (CAM chunks may run longer); candidate list ~4–6 deep; capacity prompt default ~3 h; mirrored single-designer blocks as the shared-work convention (§7) — no multi-designer packing logic needed; evening flow should be *lighter* than the morning flow, since evening discipline is the observed failure point.

## 10. Roadmap

1. **Now:** finish the manual recording period with one adjustment — keep journaling (attribution data is good and accumulating), stop pretending the planning layer will start by itself (§9a). Keep the ranking-vs-gut disagreement log alive: with adherence data empty, that log is now the *only* weight-tuning input.
2. **Next: score tuning** from the disagreement log. Also decide Design Due derivation (needs fab lead-time by project type — never supplied).
3. **Then: daily scheduling automation — kii-bot (decided 2026-07-13).** Telegram conversation flow on the existing bot (`mcpjk/kiibot`), folded into the shift-management VPS deployment rather than run as a separate project. Morning: bot pings each designer → capacity declared by reply (default suggestion ~3 h, per §9a) → bot fetches candidates by score (neglect signals must survive into the Telegram rendering; list ~4–6 deep) → inline-keyboard accept/swap → bot writes Design Day + packed blocks (Planned + Start/End; default chunks 1–1.5 h Design, longer allowed for CAM; mirrored single-designer blocks for shared work). Evening: per-block Confirm/Adjust/Drop taps → Day confirmed — deliberately the *lightest* interaction of the day, since evening discipline is the observed failure point (§9a). Human inputs remain exactly two: capacity number + selection taps. Airtable stays source of truth; bot failure degrades to the journaling protocol.
   **Google Calendar integration (read direction, planned):** appointments already live in Google Calendar; the bot reads the day's busy intervals (free/busy or events list) and packs design blocks around them — existing appointments become fixed obstacles in the packing step, and the bot can show "calendar says you have X h of appointments" alongside the capacity question (capacity remains human-declared, calendar-informed — never calendar-derived, since design capacity depends on fab/site load the calendar doesn't know). Distinct from the *write* direction (pushing blocks to calendar as switch cues), which remains deferred as before; read is lower-risk and higher-value first.
4. **Later:** adherence dashboard from Day rollups (data starts only when the bot's planning layer goes live); SGD per attributable-hour by project class (`Hours consumed` × rates joins the existing fabrication costing; `Est. value/quote`/`Billed`/`project value` (§3a) are the value side; block-type filtering gives the design/CAM/comms split — comms share per project is the small-project cost probe). **Reference-class estimation** replaces the deleted intake estimates: once a few months of actuals exist, derive expected-hours ranges per project class from recorded data (CAM converges first — predictable from part size/complexity; CAD stays volatile, client-driven).

## 11. kii-bot integration (live as of 2026-08-04)

Repo `mcpjk/kiibot` (Railway deployment; Airtable stays source of truth).
Table/field IDs live in `config.py` so Airtable renames can't break the
bot — but three field NAMES are referenced in filter formulas and must
not be renamed without a code change: `Start`, `Block status`,
`Switch ping sent`.

**Switch reminders (live).** Every 2 min the bot finds Design Blocks
starting in the next ~5–8 min and DMs the linked designers
("📐 14:00: Espira Spring 1 (1.5 h), CAM"). The message is deliberately
a single short line (changed 2026-08-04): a switch cue is read off the
lock screen, so everything must fit the notification preview and
nothing may be spent on words the designer can infer ("wrap up and
switch over"). Order is time → project → duration → type. Dedupe is
stateless — the bot stamps `Switch ping sent` on the block, so restarts
never double-ping and a downed bot simply misses pings. `Dropped` blocks
never ping. Requires blocks to exist with *future* Start times, i.e. the
morning-planning protocol; a retrospective journal produces no reminders.

**`/extend` — mid-day overrun (live 2026-08-04).** Every switch
reminder carries an inline **⏱ +30 min on current task** button
(`/extend [minutes]` typed does the same). It adds time to the block
the designer is *currently in* — the reminder announces the *next*
block while the button extends the *running* one, which is the intended
asymmetry: the motivating case is being mid-machining when the cue
fires. Tapping it late, after the announced block has started, still
does the right thing, because the rule is uniformly "the block you're
in". Refuses when nothing is running.

The cascade rule is **gap-first** (decided with Marcus 2026-08-04).
Blocks are almost always back-to-back in practice — 3 Aug ran
10:30→11:30→12:30→13:00 SGT contiguous — so an extension nearly always
collides, and "only extend into free time" would refuse most of the
time. Therefore: extend the running block, push the blocks that now
collide in order, and **stop the ripple at the first gap that can
swallow it**; everything after that gap keeps its planned times.
Three properties make this terminate without a day cutoff:

- **Lunch (13:00–14:00) is immovable** — the whole shop breaks
  together. A pushed block that would land inside it jumps to *after*
  lunch; an extension that would itself run into lunch is refused
  rather than silently truncated (a shorter extension than asked for is
  worse than a clear no). Blocks already planned through lunch are left
  alone — the guard blocks new violations only.
- **End-of-day is a gap** — the last block of the day simply runs later.
- **`Dropped` blocks are neither obstacles nor targets** (§7).

`Planned hours` and `Block status` are never written: the plan stays
frozen, so an extension shows up as deviation exactly as §1 intends,
and the evening pass keeps sole ownership of Confirmed/Adjusted. What
/extend actually buys is **actuals captured as they happen**, which is
the precise failure point §9a identified — retrospective composition is
more effortful than in-the-moment correction.

⚠ Any block whose `Start` moves must have `Switch ping sent` **cleared**
(the bot does this), or the dedupe stamp silently suppresses its
reminder at the new time. Logic lives in `core/design.py`; the cascade
planner is pure, so it's tested without network.

**Score snapshots (live).** 06:05 SGT, deliberately after the 06:00
recalc automation (§6) — `TODAY()`-dependent scores are stale before it
fires. Appends one row per design candidate to a Google Sheet:
`Date · Rank · Project · Score · Tier · Days since touch · Design Due ·
Status · Touched yesterday`. Logging the score's **inputs** alongside the
total is the point: alternative weights can be tested counterfactually
against the whole history with sheet formulas, rather than only noting
disagreements as they occur. Storage is Sheets, not Airtable (analysis
happens in Sheets) and not a local CSV (Railway's disk is ephemeral).

**Comparison layer — RETIRED 2026-08-24** (code and `/compare` removed;
sheet deleted). It joined each morning's frozen ranking against the
day's recorded blocks into `Worked` / `Ranked but skipped` /
`Worked (unranked)`. With §12's full-list selection, the first two
outcomes stop meaning anything (skipping is now normal, not a
disagreement) and the third is near-impossible. See §11c for what the
three weeks of data actually showed before it was switched off.

`/snapshot` remains as the on-demand snapshot trigger.

### 11a. Decision 2026-08-04: all block types are design work

`Worked` counts **any** block type. Client comms, site meetings and
admin all consume design capacity and constitute progress — convincing a
client of a choice, or closing out an invoice, moves a project forward
as much as modelling does. Restricting the measure to Design+CAM was
judged not to translate into workflow insight.

`Modelling hours` (Design + CAM) survives as a *breakdown* column for the
costing analysis (CAM is predictable from part size, CAD is
client-driven and volatile) — not as a definition of real work.

**Open tension this creates:** `Confirmed touch` was just filtered back
to Design+CAM (§9.1), so the neglect clock still ignores comms/site/admin
— a project can be actively worked in a way this document now calls
design work while `Days since touch` keeps climbing. Either (a) revert
the filter so any block type counts as a touch, matching this decision,
or (b) keep it, on the argument that a chasing email shouldn't buy the
same neglect reset as substantive work. Not yet decided; the comparison
data (`Worked` rows whose hours are all comms) should settle it
empirically within a couple of weeks.

### 11b. Schema drift noted 2026-08-04

The live `Block type` select contains **`Assembly`**, which this document
doesn't list (§3 Design Blocks). Blocks using it are being recorded.
Decide whether it's a design-process type (and so belongs in the
documented set) or a fabrication type that shouldn't be on Design Blocks
at all.

### 11c. What the comparison data showed (4–21 Aug, read before deletion)

The sheet was deleted 2026-08-24; this is the extraction. Treat it as
the tuning log §4 asks for, not as a settled result — the usable sample
is **two days**.

**Why only two.** Of ten dates with rows, only 4 and 6 Aug had a
working ranking join. The rest were corrupted by the append
column-drift bug (§12d): drifted rows had an empty first cell, so
`read_snapshot_rows_for` matched nothing and every project came out
`Worked (unranked)`. A second cause: 13 Aug's blocks were created at
11:03 the next day, five hours *after* the 06:05 comparison ran —
**retrospective entry is permanently invisible to a morning job.** The
Mini App writing blocks before the work removes that failure mode.

**The finding: the score's top was never worked.** On both valid days
ranks 1 and 2 were skipped; work landed at ranks 3 and 6–11 (4 Aug) and
ranks 5 and 18 (6 Aug). Sharpest case: **CDJ hook-on stands held rank 1
and went untouched from 14 July to at least 24 August — 41 days.**

**The mechanism.** Projects worked on 4 Aug had days-since-touch of
0, 0, 0, 1, 4, 11 — the designers were finishing pushes. The
continuation bonus is **+6**; the neglect term saturates at **42**. So
a parked project outranks a project in flight by design, and the humans
override it every time. §4 predicted this for the first weeks; three
weeks in it hadn't resolved. If the neglect/continuation balance is
ever retuned, this is the evidence for it. Note Marcus had already
corrected CDJ by hand — `High` tier on 4 Aug, `Low` now — which is the
tuning protocol working, just through the tier rather than the weights.

**Pending client was checked and cleared.** 13 h of confirmed work sits
against projects that are Pending client *today*, which looked like an
argument against excluding them from §12's picker. It isn't: every
instance is "worked while active, flipped afterwards" — Reinvention,
OTP Loft storage, Study table and Half-wall ledge were all *ranked* on
4/6 Aug (which the gate only permits for non-Pending projects), and
Reception desk was created 11 Aug, got 3 h of design over 12–14 Aug,
then went silent for 10 days: the signature of "designed it, sent it,
waiting". No evidence of work while genuinely parked.

**Two numbers that back §12's choices.** Four **0.25 h** blocks already
exist (three Client comms, one Admin), plus one off-grid 1.23 h block —
sub-half-hour work is real, which is why the grid moved to 15 min.
Busy days ran 4.5–7.75 h *combined across both designers*, ≈2.5–3.75 h
each, so the 3 h capacity default is well-placed.

**Housekeeping this surfaced:** roughly a quarter of the 21-project
plannable list is long-dead — Serving tray Tall (98 days untouched),
Chafing dish cladding (56), CDJ hook-on stands (41), Wall panel repair
(35), Volume building workshop (31), four of them still `Lead`. With
the score no longer rationing, nothing removes these from view; they
will be scrolled past every morning forever. Moving stale Leads out of
`Process = Designing` (or adding a dormant status) is the cheapest
improvement available to the new flow.

## 12. Daily planning Mini App (decided 2026-08-24)

**The ration model is retired.** §9a showed the planning layer never
engaged: a top-N ration composed from a score was never how the day
actually got chosen. Marcus's reading — "there's far more context that
goes into choosing what we work on than is sensible to record in
Airtable" — is accepted. Note this is a smaller change than it sounds:
§1 already said scoring is *decision support, not a decider*. What
goes is the **ration**, not the score.

**What replaces it.** Each morning (Mon–Fri 10:00) the bot DMs each
designer a `📋 Plan today` button opening a **Telegram Mini App**:

1. **Step 1 — select.** The full plannable list, one tap each. Shows
   Status, Process, and **hours consumed to date**. Deliberately *not*
   neglect signals: the designers carry recency context themselves,
   while cumulative effort is the number they can't hold in their heads
   and which feeds future costing (Marcus, 2026-08-24). Capacity
   ("hours available today") is declared at the top.
2. **Step 2 — dial in.** Only the selected projects expand, each with a
   block type and a duration stepper. Duration starts at
   `capacity ÷ projects`, snapped to the grid, and recomputes live as
   projects are added or removed — but stops auto-adjusting any block
   the designer has touched.
3. **Confirm** → blocks are laid end to end from **now, rounded up to
   the next 15 min**, jumping the 13:00–14:00 lunch hour, and written
   as `Planned`.

**Grid is now 15 minutes**, not 30 (§7's half-hour convention is
superseded): comms blocks are often shorter than half an hour.
`/extend` still adds 30 min at a time.

**Gate:** `Process` ∈ {Designing, Fabricating} and `Status` ∉
{Cancelled, Pending client} — 21 of 33 projects as at 2026-08-24.
Pending client stays out (design can't progress without the client;
including it would put 12 unusable rows on the list). This is
`get_plannable_projects()` in code, deliberately NOT the
`Design candidate?` formula — that field still gates the score and
snapshot, and widening it would mean a UI formula edit.

**Who gets prompted:** Active members who are `Design owner` on ≥1
plannable project — derived from live data, so it needs no new
checkbox and never gates on `Role` (which CLAUDE.md forbids).

### 12a. Architecture

The bot gains a small `aiohttp` server (`web/`) started from PTB's
`post_init` hook — same process, same event loop, so there is still
exactly one poller. It serves two routes and nothing more:
`GET /plan` (the page) and `POST /api/projects` (the list).

**The launch button must be INLINE.** Telegram offers two mutually
exclusive Mini App launch styles, and the trade is exactly the one
that matters here:

| launch | `initData` | `sendData()` |
|---|---|---|
| reply-keyboard button | **empty** | available |
| inline button | signed, full | unavailable |

The page must prove who is asking before it may be shown the project
list, so signed `initData` wins. Submissions therefore go to an
authenticated `POST /api/plan` rather than `sendData()`.

This was built the other way round first — reply keyboard plus
`sendData()`, on the reasoning that it avoided a write API — and it
cannot work: such a launch carries no identity whatsoever, so the read
route could never authorise the page. Confirmed live 2026-08-25 (the
launch delivered `tgWebAppVersion` and `tgWebAppPlatform` but no
`tgWebAppData`). Do not "restore" `sendData()`.

Both POST routes authenticate by verifying Telegram's `initData` HMAC
against the bot token (`web/auth.py`). That signature is the only
thing establishing which designer is calling; never trust a user ID
from a request body.

⚠ Every Airtable call in `web/` runs through `asyncio.to_thread`. The
server shares its event loop with the bot's long polling, so a
blocking HTTP call there stalls the bot — and `submit_plan` makes one
API call per block.

`WEBAPP_URL` (public HTTPS origin) enables the feature; unset, the
whole planning layer disables cleanly and the bot runs as before.

### 12b. Units and field names — read before touching

`Planned hours` and `Capacity (hours)` are written in **HOURS**. This
is forced, not cosmetic: `Deviation (hours)` computes `Actual hours −
Planned hours`, so anything else makes deviation silently wrong by 2×.
Both were renamed from `... slots` on 2026-08-25 (§9.9 resolved).

⚠ **Field renames are NOT free, and an earlier version of this note
said they were.** The bot addresses *tables* by ID but writes *fields*
by NAME, so renaming a field the bot writes breaks it instantly with a
422 UNKNOWN_FIELD_NAME. That rename shipped without the code change
and plan submission failed on every attempt until it caught up. Three
names are load-bearing on writes here — `Planned hours`,
`Capacity (hours)`, and the absence of a text primary field on Design
Blocks (its primary is now `Start`; the old `Name` field was deleted,
so writing `Name` is a 422 too).

**Before renaming any field the bot writes:** grep the codebase for
the old name, change both together, ship them in one deploy. A test
(`test_submitted_blocks_use_the_live_airtable_field_names`) pins the
exact key set written to Design Blocks so the next rename fails in CI
rather than on someone's phone.

### 12c. Consequences to decide

- **The comparison layer is retired** (done 2026-08-24, §11c):
  "Ranked but skipped" means nothing once the list is everything and
  selection is free, and "Worked (unranked)" is near-impossible.
  Snapshots stay as a cheap daily log of the signals.
- **Design Days is revived** (§9.4 resolved in favour of (b)): the bot
  now writes one per designer per day with declared capacity.
- **Calendar-derived capacity** (§10.3) stays out for now by decision,
  but capacity enters through exactly one field in the payload, so a
  future "calendar says you have X h of appointments" default has one
  place to land.
- **Evening pass stays in Airtable** for now.
- **Shared work is ignored** for now: the app doesn't show that the
  other designer already picked a project. Mirrored single-designer
  blocks (§7) still work, they're just not surfaced.

### 12d. Sheet append drift (fixed 2026-08-24)

`gspread.append_rows` without `table_range` lets the Sheets API
auto-detect which columns "the table" occupies, including its left
edge. Once anything makes it detect a block not starting at column A,
the append lands there and the next day re-anchors further right: rows
marched across the sheet, a few columns per day (observed 5–6 Aug).
Every append now passes `table_range="A1"`. Anything that reads a sheet
back by column position depends on this.
