# FTLguard / Air Eagle — Handover Snapshot

Stable commit: (set after first push)
Branch: main
Client: Air Eagle (B737 cargo — ad-hoc + scheduled, confirmed 2026-07-19)

## Recently completed
- Repo scaffolded from scratch (see "Why a restart" note below)
- Directory structure implements the Ownership Table from Project
  Instructions literally: core/, services/, configs/, migrations/
  — this didn't exist in the old repo despite being specified
- Migration tracking system built and tested against real Postgres:
  schema_migrations table, scripts/run_migrations.py (idempotent,
  detects post-apply edits to already-applied migrations via checksum)
- scripts/check_reachability.py built and tested — flags any file
  under core/services/db with zero importers anywhere in the repo.
  Refined during Phase 2 to exclude tests/ from counting as
  "reachable" — a file imported only by its own test is not
  connected to the app, and the original version would have masked
  that (tested and confirmed with a planted dummy file both ways).
- Test harness built: pytest + conftest.py with an isolated,
  disposable Postgres fixture (db_engine) for DB-dependent tests.
- Phase 2: core/legality/pcaa_ano012_core.py and core/duty_summary.py
  ported from the old K2 repo, reviewed in full (not blind-ported —
  read all 1292 + 129 lines), one cosmetic indentation fix in
  duty_summary.py. Tests written as part of the port, not after:
  26/26 passing against real Postgres + pure logic, covering:
    - D8.2.1 FDP table (incl. the overnight-band midnight wrap —
      the exact area of the historical "2.2h instead of 13h" bug)
    - D21.1 charter rest, max(12h, 2xFDP) — confirmed as the actual
      applicable rest rule for Air Eagle's cargo-charter
      classification (2026-07-19), both the floor case and the
      scales-above-floor case, plus an end-to-end illegal/legal
      rest gap through validate_schedule()
    - D9 cumulative limits (28-day, the highest-risk area for a
      small crew pool) — confirmed the engine actually flags a
      breach, not just defines the number
    - ValidationResult status aggregation (LEGAL/WARNING/ILLEGAL/
      NEEDS_MANUAL_REVIEW)
  Reviewed an external algorithm/architecture reference document
  (2026-07-19) for the upcoming roster generator: adopted the
  choice to use Google OR-Tools CP-SAT for the 28-day assignment
  optimization (genuinely useful, fits our existing Python stack).
  Explicitly rejected that document's infra recommendations
  (Go/Rust backend, TimescaleDB+Redis, GraphQL) as scaled for a
  large carrier, not Air Eagle's actual crew pool size — noted here
  so a future session doesn't reintroduce them without re-deciding.
- Phase 3: schema. Three numbered migrations (001_crew_table.sql,
  002_flights_table.sql, 003_roster_table.sql), applied and verified
  against real Postgres — actual resulting columns/constraints
  inspected via psql \d, not assumed from the SQL source. 11 new
  tests (37/37 total), including one that matters more than the
  others: inserted a genuine 2-sector duty into the real, constrained
  roster table, read it back, and ran it through the already-tested
  Phase 2 duty_summary logic — confirming the schema and the dedup
  logic actually fit together, not just that each is independently
  correct. Also tested: FK rejection of orphan crew_id/flight_id,
  CHECK rejection of bad status values and backwards time ranges,
  UNIQUE constraint correctly blocking duplicate (crew, flight, role)
  while correctly allowing two different crew in the same role on
  one flight. crew.base confirmed to have no hardcoded default (a
  bug fixed twice in the old repo) — now an actual regression test,
  not just a one-off fix.
- Phase 4: Crew Data page — the first real service-layer writes and
  the first real page in this repo. Built in this order:
  migrations/004_audit_log.sql (single unified audit table per
  Section 16 — deliberately not built in Phase 3, since there were
  no writes yet to audit; building it ahead of need would have been
  the exact speculative-schema mistake Section 3's FUTURE
  classification warns against), services/audit_service.py,
  services/crew_service.py (add/update/deactivate_crew, get_crew,
  get_all_crew — crew_id always system-generated, never taken from
  caller input, matching what the crew data template already
  promised the operator), then app.py and pages/2_Crew_Data.py as
  thin wrappers calling only these services, no direct SQL.
  24 new tests (61/61 total): audit_service (append-only, minimal
  calls leave other fields NULL), crew_service (required-field
  validation, sequential per-role ID generation, caller-supplied
  crew_id correctly ignored, soft-delete not hard-delete, audit
  record written on every operation with before/after state), and —
  this is the one worth calling out — genuine page-level tests using
  Streamlit's own AppTest framework: the add-crew form actually
  gets filled in and submitted against real Postgres, not just
  syntax-checked. AppTest caught a real, already-relevant
  deprecation (`use_container_width`, deprecation window closed
  2025-12-31) that a plain compile check would have missed entirely.
  crew.base still correctly has no default (regression test from
  Phase 3 still passing).
- Phase 5: Duty builder v2 + Flight Log. core/duty_builder.py
  replaces the old repo's XYZ-hardcoded DUTY_TEMPLATES entirely —
  takes whatever flight legs are given, computes report/debrief/FDP
  from them, no route lookup table. Split into two deliberately
  separate functions after tracing through the exact historical bug
  scenario (Section 8) carefully: build_duty() for planning a NEW
  duty (report_time derived from departure - buffer), and
  recompute_fdp_after_delay() for an EXISTING duty whose crew already
  reported (report_time stays fixed, only debrief_time/fdp_hours
  change). Conflating these two would have reintroduced a version of
  the exact bug this file exists to prevent — a delayed departure
  would have incorrectly shifted report_time along with it. One test
  replicates Section 8's exact numbers (report 05:00, delayed
  debrief 18:00 -> 13.0h) and explicitly demonstrates the wrong
  block-time-only answer (2.25h) it must not produce.
  services/flight_service.py: add/update/cancel_flight, get_flight,
  get_all_flights. cancel_flight() never deletes — sets
  status='CANCELLED' — per the explicit "permanent log of all
  flights" requirement; get_all_flights() shows cancelled flights by
  default rather than hiding them, verified directly by a test.
  app.py updated with nav; pages/3_Flight_Log.py added, thin wrapper
  matching pages/2_Crew_Data.py's pattern.
  28 new tests (89/89 total): 10 for duty_builder (pure logic), 13
  for flight_service, 5 AppTest page-level tests for Flight Log
  including the permanent-log requirement verified through the
  actual UI (add a flight, cancel it, confirm it's still visible
  with status=CANCELLED, not removed from the table).
- Phase 6: Assignment + legality gate. The biggest phase yet, and the
  one where the actual workflow architecture got clarified properly
  (see "Architecture: Roster vs Control Room" below — read that
  before touching either page). Two new migrations first:
  005_roster_partial_unique_index.sql (Phase 3's UNIQUE constraint on
  roster would have permanently blocked ever re-assigning the same
  crew/flight/role after an unassignment — replaced with a partial
  unique index that only applies to non-cancelled rows) and
  006_flights_domestic_column.sql (domestic is a property of the
  route, decided once at flight creation — NOT NULL, no default,
  moved off being asked per-assignment).
  services/assignment_service.py: the core write path for roster,
  shared by both Roster and Control Room. Two real bugs found and
  fixed during testing, not just written and assumed correct:
    1. flight_service.py's and crew_service.py's required-field
       checks used truthiness (`if not value`), which incorrectly
       treats a legitimate `domestic=False` as "missing" (`not False`
       is `True`). Fixed to distinguish None/empty from a real False.
       Both directions now have permanent regression tests.
    2. The downstream-impact check's before/after duty comparison
       never actually included the future duty being assessed in
       either list — it would have silently returned "no conflicts"
       on every single call, which is worse than not having the
       feature at all, since it would have looked like it worked.
       Rewritten to independently load full context (lookback window
       through each future duty's own start time) per future duty
       checked. Caught immediately because the downstream-conflict
       tests were written to assert a SPECIFIC conflict using real
       D21 rest-math numbers, not just "does this not crash."
  assign_crew_to_duty() (Roster — assign to a flight that already
  exists) and assign_crew_to_new_flights() (Control Room — atomic
  flight-creation + assignment, gated by legality BEFORE either is
  written) share one validation core (_validate_new_duty) rather than
  duplicating the legality orchestration — SSOT, not two copies that
  could drift. find_legal_candidates_for_duty() searches active crew
  by role and returns who would actually be legal, not just who has
  the right role.
  pages/4_Roster.py and pages/1_Control_Room.py both built as thin
  wrappers over assignment_service.py.
  33 new tests (122/122 total): 20 for assignment_service (including
  the atomic no-orphan-flight guarantee, verified directly — an
  illegal ad-hoc assignment leaves flight count unchanged), 4 AppTest
  tests each for Control Room and Roster pages.

## Architecture: Roster vs Control Room (clarified 2026-07-20)

Two distinct entry points for crew assignment, both writing through
the same services/assignment_service.py — read this before touching
either page, it's not obvious from the code alone:

| | Roster page (pages/4_Roster.py) | Control Room (pages/1_Control_Room.py) |
|---|---|---|
| For | Scheduled flights | Ad-hoc / unscheduled / charter |
| Flight source | Already exists (Flight Log / future auto-generator) | Created in the same action |
| Function called | assign_crew_to_duty() | assign_crew_to_new_flights() |
| On ILLEGAL | Assignment rejected, flight untouched (it already existed) | BOTH flight and assignment rejected — no orphan flight saved |

Both funnel through `_validate_new_duty()` — one validation core, not
two copies. The only real difference is atomicity: Control Room's
save is gated as one unit because the flight doesn't exist
independently of the crew decision the way a Flight-Log-created
flight does.

**The downstream "swap alert" catch** (also from this clarification):
assigning a crew member to ANY new duty — ad-hoc or scheduled — can
make an ALREADY-scheduled future duty of theirs illegal, even though
the new assignment is perfectly legal on its own (it just consumes
enough rest/cumulative hours). Both assignment functions call
`_check_downstream_impact()` after a successful save. Scope was
explicitly decided as "alert + suggest legal candidates, human
confirms" — not full auto-reassignment. That's Phase 7/reoptimization
territory if it ever gets built; don't quietly expand this function's
scope to auto-apply a swap without that being a deliberate decision.

## Why this repo exists (context for future sessions)
The previous repo (K2 / "K2_for_Claude_Clean") accumulated real
structural damage over ~4-5 months: three incompatible definitions
of the crew table simultaneously, a dead flat-rules legality dict
still sitting in the canonical validator file, a confirmed
production bug (validate_single_assignment missing 5 of 11 params)
live in the actual assignment flow, and 17 files with zero callers
anywhere in the app. Root cause wasn't any single bug — it was no
regression tests despite documented lessons, no verification that
new code was ever wired in, no migration state tracking, and no
file deprecation discipline. This repo is a deliberate restart
specifically to fix the *workflow* gap, not just the code. Full
assessment is in the FTLguard project chat history, 2026-07-19.

## What carried over from the old repo (reviewed, not blindly ported)
- Project Instructions (SSOT table, ownership table, hard-lessons
  catalogue) — the thinking was already good, it just wasn't
  enforced. Same doc, now with actual enforcement mechanisms.
- Crew data collection template (already correct, given to operator)
- pcaa_ano012_core.py and duty_summary.py — ported in Phase 2, with
  tests written as part of the port. See Recently Completed above.

## What did NOT carry over (deliberate)
- utils/ftl_validator.py's dead CAA_RULES flat-limits dict — left
  behind entirely, not ported "just in case"
- duty_builder.py's hardcoded XYZ-specific DUTY_TEMPLATES — rebuilt
  schedule-agnostic from scratch in Phase 5, see Recently Completed.
- crew_position.py / replacement_options.py (location tracking) —
  deliberately not included. All Air Eagle crew are KHI-based;
  nothing currently indicates away-from-base overnight layovers.
  If the route network (pending, see below) shows real layovers,
  build this properly against that actual pattern — don't
  speculatively rebuild it now.

## Current active task
Steps 2–5 of the agreed remediation order are done. NEEDS_MANUAL_REVIEW
gate, domestic/geographic-continuity fix, and the crew qualification
gate (AE-CREW-QUAL-001) are merged into `main`. `type_rating_expiry`/
`contract_expiry` removal (branch `remove-type-rating-contract-fields`
— removed entirely from the qualification gate AND the crew schema,
migrations/008) verified by the user against real Postgres 16
(178/178) and merged into `main` first. Step 5's three "stale data"
fixes (branch `step5-stale-data-fixes` — LOOKBACK_DAYS widened,
delay-recompute wired up, cancel cascade wired up) verified by the
user against real Postgres 16 (190/190, D9.2.3 confirmed empirically
firing with ~300 seeded duties); its roster-status migration,
originally also numbered 008 in parallel, renumbered to 009 and
rebased onto `main` post-field-removal-merge before merging. See the
2026-08-01 log entries for full detail on both.

On top of that: the user's own real-Postgres verification of Step 5
surfaced a new, real problem — a single assignment against ~300
seeded duties returned 2,215 alerts across 11 rule codes in 1.26s
(one alert per breached rule PER historical duty, not one per breach
— `_check_cumulative_limits()`'s per-duty loop, now with 370 days of
history to iterate instead of 35), rendered in uncapped loops on all
three pages, and joined unfiltered into one audit_log row (~150KB
measured). Fixed on branch `alert-summarization`, **verified by the
user against real Postgres 16 and merged into `main`** — 207/207
passing; the same 299-duty scenario re-measured on both `main` and
the branch showed display lines 2,216 -> 29 (-98.7%) and the audit
row 128,685 -> 2,253 chars (-98.3%), with the raw alert count (2,216)
and the REJECTED/ILLEGAL result identical on both — confirming
summarization is display-only. See the dedicated log entry below for
full detail. See "Next safest step" for what's queued next
(item 6: Control Room transactional atomicity, item 7: the
age-pairing rule, or find_legal_candidates_for_duty()'s separate
per-candidate performance problem, explicitly deferred out of this
branch's scope).

**New, separate piece started 2026-08-01**: the OCC assistant's query
parser — `services/assistant/query_parser.py`, a deterministic
(no-LLM) natural-language-to-`ReportRequest` parser, on branch
`query-parser`. This is a standalone building block: it produces
parsed parameters (template + crew/dates/route) but nothing executes
them yet — the seven report functions that would actually run a
`ReportRequest` against the schema are the next piece, not built here.
**Deliberately not merged.**

Placing this file also surfaced two real, independent, previously-
silent bugs in `scripts/check_reachability.py` (both now fixed, on
the same branch), plus the same encoding class of bug in
`scripts/run_migrations.py` (also fixed). See the dedicated log entry
below — this ended up being the larger part of this piece of work.

**New, separate piece started 2026-08-01**: the seven report
functions that actually execute a `query_parser.ReportRequest` against
the real schema — `services/assistant/reports.py`, on branch
`assistant-report-functions`, off `main` at commit `28b5da3`. This is
what makes the parser's output do something; the parser itself never
touches a database. Also added: `services/reporting.py` (general-
purpose `Dataset`/CSV/XLSX/Markdown export, reused by every future
data-bearing page's export button, not assistant-only) and
`services/assistant/regulation_reference.py` (curated ANO-012 section
summaries backing the `regulation` template). See the dedicated log
entry below for full detail. **Verified by the user against real
Postgres 16 (301/301) and merged into `main`.**

**New, separate piece started 2026-08-02**: five operator decisions
implemented together on branch `operator-crew-scope-and-coverage-
reshape` — Air Eagle's crew records narrowed to CPT/FO only (LM/AME
are the operator's own responsibility, tracked nowhere in this system
except as free text per flight), `roster_coverage()` reshaped to
Date/Flight/Route/CPT/FO/occupants/POB/Remarks accordingly, "VAI"
resolved (it's AME), the age-65 rule's wording confirmed (still not
built — still blocked on the pair-level architecture question), and
auth confirmed deliberately parked pending the operator's answer on
two open questions. See the dedicated 2026-08-02 log entries below for
full detail on this and everything since.

**Merge status as of this snapshot (2026-08-08)** — this paragraph,
not the individual dated log entries below, is the single place to
check what's actually landed. A dated log entry describes what was
built and why, and that doesn't go stale; a "MERGED"/"NOT MERGED" note
buried inside one does, once several branches are in flight from
different points in history. Keep merge status here only, going
forward.

- **⚠ DEPLOYMENT ORDERING — READ BEFORE DEPLOYING ANYTHING (2026-08-19).**
  Two migrations are pending on the real Supabase database, and the
  second one is a HARDER prerequisite than any migration before it:

  - `migrations/018_users.sql` — every page now requires a login.
    Deploying without also seeding the three OCC accounts via
    `scripts/seed_users.py` locks the app for everyone.
  - `migrations/019_allow_delete_unused_templates.sql` — needed for the
    delete control to work. **SOFTENED 2026-08-19:** this previously
    said a pre-019 database made `pages/7_Schedule_Templates.py` fail to
    render at all. That was accurate when written and is no longer true:
    the deletability lookup is now wrapped, so a pre-019 database
    renders the page normally with delete shown disabled and the reason
    given. Still apply 019 — without it the feature is unavailable — but
    a missing migration no longer takes the page off the air.

  Apply 018 and 019, then seed the accounts, then deploy. Production was
  at 017 as of the flight-deck merge.

- **`rescue-swap-alerts-and-flight-no` merged into `main` (2026-09-05).**
  Three things rescued from the cancelled
  `flt-schedule-readonly-actuals-to-control-room` branch, all
  independent of the move that was cancelled. **764/764 verified against
  real Postgres 16**, reachability clean. **No reboot, no migration** —
  from the check, not from memory.

  **A CONTROLLER HAD NEVER SEEN A SWAP ALERT.** Live defect on `main`,
  not something the cancelled branch introduced: Control Room's ALLOWED
  branch wrote its success line, pair alerts and swap alerts — "this
  assignment breaks the legality of N already-scheduled future
  duty(ies)", including "no legal candidates found" — then called
  `st.rerun()`, which abandons the run. Queued now.

  **Scoped to the two paths that actually rerun.** REJECTED and
  NEEDS_REVIEW never call `st.rerun()`, so their messages were always
  visible and were left alone — fixing them would have been a change
  with no defect behind it. `tests/test_control_room_notices.py` pins
  BOTH directions, so a later tidy-up that adds a rerun to a visible
  branch fails too.

  **Flight No. required in the UI, nullable in the schema**, and not a
  contradiction: rotation expansion already refused a numberless leg
  and the import script creates crew, so this form was the only path
  that ever produced a NULL. `flight_label()`'s `#123` fallback stays
  for the case the schema still allows.

  Two notes rescued with it: `cargo_dg` recorded and checked against
  nothing while `dg_expiry` is checked on every duty regardless
  (recorded, NOT fixed — operator question), and the positional-access
  rule extended to `at.dataframe[`.

- **`crew-change-revalidation` merged into `main` (2026-09-05).**
  Correcting a legality-relevant crew field now re-checks every future
  PLANNED duty that crew member holds and flags whichever no longer
  pass. Reported from live use: an OCC member set CPT-03's SIM expiry
  to a past date and CPT-03 stayed on already-written PLANNED rosters
  with nothing flagged. **758/758 verified against real Postgres 16**,
  reachability clean, with the operator's exact scenario reproduced end
  to end (`PLANNED` -> `NEEDS_REVIEW`, renewing forward leaves the flag
  standing, `deactivate_crew()` routing through the same door).

  **⚠ NEEDS A REBOOT — TWO LIMBS. No migration.**
  `pages/2_Crew_Data.py` imports `utc_stamp` from a module it already
  imported (limb 3), and `pages/4_Roster.py` calls
  `assignment_service.duties_needing_review()` and
  `clear_duty_review_flag()` — new attributes on a module it already
  imports (limb 4, `AttributeError` at page load against a stale
  `sys.modules`).

  **THE ONE DOOR IS THE POINT.** `update_crew()` AND
  `deactivate_crew()` both call `revalidate_crew_duties()`, because
  `is_active` is not in `UPDATABLE_FIELDS` at all — a fix wired only
  into `update_crew()` would have left taking a pilot out of service
  while they hold future duties bypassing revalidation entirely.
  `test_both_crew_writers_go_through_the_one_door` fails if a third
  writer appears. The reverse import is a genuine cycle, so the call
  imports inside the function body: the only place in `services/` that
  does, and deliberate — wiring into the service rather than the page
  is what stops a caller bypassing it.

  **NEVER AUTO-CLEARS, and that is why the clear control shipped with
  it.** A field corrected back in the safe direction leaves the flag
  standing: the flag records that nobody has LOOKED since the data
  changed, not that the data is currently bad. Until this branch
  nothing anywhere could clear `NEEDS_REVIEW` and no page listed
  flagged duties, so a second flagger without an exit would have made
  the correction path something people route around.

  **Before changing `changed_fields`:** it diffs old against new rather
  than trusting the caller's dict, and that is load-bearing. The Crew
  Data form submits every field on every save, so the naive version
  would revalidate the whole roster on a no-op save — the feature would
  have looked correct while producing pure noise, and the flags would
  have stopped being believed.

- **`roster-table-by-flight` merged into `main` (2026-09-03).** The
  Roster page's "Current assignments" is now one row per flight —
  Flight, Route, Commander, Second Pilot — with the serial column,
  `role`, `duty_id` and `flight_id` gone and crew shown as
  `CPT M Waqar`. **735/735 verified against real Postgres 16**,
  reachability clean.

  **⚠ NEEDS A REBOOT — limb three.** `pages/4_Roster.py` imports
  `crew_seat_name` and `flight_label` from `services/display_labels.py`,
  a module it already imported: a new NAME in an existing import, which
  is an `ImportError` at page load against a stale `sys.modules`. **No
  migration.**

  I wrote "no reboot" in this branch's own entry and caught it only by
  running the check mechanically before pushing — two days after
  documenting that limb. **The rule is easy to remember and hard to
  apply, because the diff looks unremarkable. Run the grep, do not
  recall the conclusion.**

  **`operating_position` NULL means OPPOSITE things depending on grade,
  and the two must stay apart in code.** On a CPT or FO it is an
  anomaly — a real assignment the data failed to place — and is
  surfaced. On an LM or ENGR it is normal, and such a flight does not
  appear at all. Conflating them fills the column with cargo flights
  until it is ignored, which is how the anomaly then gets swallowed.
  Both directions mutation-tested. `COCKPIT_GRADES` is derived from
  `SEAT_ELIGIBLE_GRADES`, never retyped, because that set decides which
  meaning applies.

  **`nan or ""` does not fall back — nan is TRUTHY.** An all-NULL seat
  column comes back float64 and crashed the unassign selectbox. Found
  by a DB-free fixture constructing a state production does not have,
  which is the argument for fixtures that build impossible states and
  not only realistic ones.

- **`sector-coherence-on-actuals` merged into `main` (2026-09-03).** A
  delay that made a duty physically impossible produced no warning —
  flight 53 recorded 2200-2345z while its second sector still read
  2200-2345z. **725/725 verified against real Postgres 16**,
  reachability clean, with the live scenario reproduced: both crew
  flagged NEEDS_MANUAL_REVIEW, the rule named, the legs and times
  given. **No reboot, no migration.**

  **⚠ FDP VALUES CHANGE ON EXISTING DELAYED DUTIES.** `debrief_time`
  now comes from `max(arrival)` across sectors rather than
  `sectors[-1]`, which was the last sector by PLANNED departure and
  never re-sorted once actuals landed. A delay on a non-final sector
  was ending duties earlier on paper than the crew finished, so the
  recorded FDP UNDERSTATED them. Only duties where a sector overtook
  another change; ones still in order are bit-identical, and that is
  pinned.

  **⚠ SEE ALSO the standalone `st.rerun()` entry**, found beside this
  and larger than it: every delay warning on that page had been
  discarded before reaching the browser since the day it was written,
  swap alerts included, and no AppTest assertion can tell the
  difference.

  **Before touching `sector_continuity_problems()`:** it has two
  callers that need OPPOSITE behaviour from one copy of the rule —
  planning must refuse an impossible duty, recording must not, because
  it already happened. That is why it returns sentences instead of
  raising.

- **`uncovered-reason-summary` merged into `main` (2026-09-02).**
  "Why each one could not be crewed" was every attempted pair
  concatenated into a paragraph, the same commander rejection repeating
  in every line. It now leads with the root cause — `No eligible
  Commander — CPT-01's MEDICAL expired 2026-08-31, not valid for duty
  date 2026-09-08. Tried 3 combination(s). Detail: ...` — with every
  trial kept verbatim after `Detail:`. **713/713 verified against real
  Postgres 16**, reachability clean.

  **⚠ NEEDS A REBOOT — AND THE RULE IS NOT ABOUT IMPORTS.** This branch
  changes **no import anywhere**, so both previously-known checks pass
  it clean, and it would still have taken the page down against a stale
  `sys.modules`: `pages/6_Roster_Generation.py` reads
  `rotation.outcome_summary`, a NEW ATTRIBUTE on a `PreviewRotation`
  built by `roster_generator_service`. The old dataclass has no such
  field.

  **The version worth keeping: reboot whenever a page depends on
  anything the running process's copy of a service module does not
  have** — a new module, a new import edge, a new NAME in an existing
  import, or a new ATTRIBUTE on an object that module builds. That
  subsumes all four occurrences. No migration.

  **Two things not to "simplify" here.** The commander test is `all(...)`,
  not `any(...)`: reporting "no eligible Commander" when one is merely
  busy would send a controller to renew a medical when the blocker was
  rest. And the summary is built from STRUCTURED trial fields, never by
  re-parsing the joined sentence — the display must not depend on the
  punctuation of prose written for humans. Both mutation-tested.

  **`uncovered_seats.reason` still holds every trial character for
  character.** The summary is PREPENDED, never substituted; that column
  remains the only surviving explanation of an unfilled seat.

- **`preview-and-accept-provisional-rows` merged into `main` (2026-09-01).**
  Generation no longer writes speculatively. `generate_preview()`
  computes and writes NOTHING; the controller accepts or discards;
  `accept_preview()` writes **PLANNED** directly, per rotation,
  re-validating each against fresh data in the same call that commits.
  PROPOSED is retired as a workflow. **701/701 verified against real
  Postgres 16**, reachability clean.

  **⚠ NEEDS A REBOOT — FIFTH OCCURRENCE, AND A THIRD LIMB OF THE RULE.**
  `pages/6_Roster_Generation.py` imports NEW NAMES from
  `roster_generator_service`, a module it already imported. No new file,
  no new import edge — just a longer import line. It fails harder than
  either known limb: `ImportError` at page load against a stale
  `sys.modules`. **The reliable check is not "did a file get added" but
  "does any page's import statement differ from what the running process
  executed" — names included.**

  **⚠ MIGRATION 021 DOES NOT EXIST YET, and must not be applied as part
  of this deploy.** Dropping PROPOSED from `chk_roster_status` cannot
  happen while the 24 legacy PROPOSED rows and the `publish_window()`
  path that promotes them are still live. Sequence: deploy + reboot →
  resolve the 24 rows (publish or cancel) → THEN write and apply 021.
  See the dated entry.

  **The cross-rotation legality guard is the whole change**
  (`tests/test_cross_rotation_legality.py`). With the provisional union
  disabled, all 36 rotations fill and 30 double-bookings appear; with it
  on, 14 fill and none do. **A healthy pool would have passed either
  way** — the defect only bites where a pool is tight against the
  schedule, which is where operations bite.

- **`schedule-change-path-and-display` merged into `main` (2026-08-31).**
  **⚠ NEEDS A REBOOT FROM MANAGE APP AFTER DEPLOYING — FOURTH
  OCCURRENCE.** `services/display_labels.py` is now imported by
  `pages/5_Assistant.py`, `pages/6_Roster_Generation.py` and
  `pages/7_Schedule_Templates.py`, none of which imported it before.
  That is the rule's second limb — "a page importing a service module it
  did not import before" — and it is the one that keeps catching people,
  because no new file appears in the diff. **No migration.**

  Six live-trial findings. Three were one question: **a template already
  used is superseded by a new version, and there is no other way**
  (legs immutable by trigger, template undeletable, existing
  rotation_code rejected). Answered once so the three UI decisions could
  follow from it rather than diverging. "Create a new version" was
  RELOCATED into each template's expander as **Change this schedule**,
  not removed — removing it would have left both live templates
  permanently unchangeable. The dead Delete button is gone; the reason
  stays and points at the change path. Expand and Review merged, with
  drafts outside the window COUNTED. Timestamps read `25 Aug 2003z`
  (CSV keeps ISO, deliberately). Day offset explained and kept.

  **691/691 verified against real Postgres 16**, reachability clean,
  including the used-template refusal against migrations/019's own
  trigger — the one path the DB-free tests cannot prove, since they stub
  the deletability lookup and would pass against a database with the
  trigger dropped.

  **Before touching `format_timestamps()`, read why `datetime` is
  checked before `date`.** `datetime` subclasses `date`, so the naive
  order converts every crew expiry column and a medical expiring
  2026-07-01 renders as "01 Jul" — losing the digit that decides whether
  a pilot may fly. A silent safety regression inside a formatting
  change; mutation-tested.

- **`roster-coverage-and-seat-fairness` merged into `main` (2026-08-28).**
  The third and fourth instances of seat-versus-grade:
  `reports.roster_coverage()` reported a fully-crewed CPT/CPT flight as
  two Commanders and an UNCOVERED Second Pilot, and
  `_seed_duty_counts()` measured total workload while its docstring
  claimed seat workload. **669/669 verified against real Postgres 16**,
  reachability clean.

  **No reboot, no migration**, confirmed structurally: the only added
  file is a test, no added `import`/`from` line anywhere outside
  `tests/`, no page changed.

  **`_seed_duty_counts()` CHANGES GENERATED ROSTERS.** Operator decision
  about what fair means, not a bug fix — a CPT who has flown many Second
  Pilot duties now sorts as under-used for Commander. If generated
  rosters look different from the trial's, this is why. Fatigue is not
  what it balances; the FTL gate is untouched.

  **`tests/test_seat_vs_grade.py` is the one place to look** before
  writing anything that derives seat occupancy — named after the
  distinction rather than scattered through three modules' suites,
  and DB-free so it runs where the DB-gated checks skip. It caught an
  `UnboundLocalError` on a branch production data cannot reach, which
  every DB-gated test would have skipped.

- **`stop-auditing-generator-trials` merged into `main` (2026-08-26).**
  Operator decision by Arif: the audit trail records decisions, not
  options considered. The generator's internal candidate search no
  longer writes a row per pair it tries and discards; every real
  operational decision stays fully audited, `ADHOC_PAIR_REJECTED`
  included. **658/658 verified against real Postgres 16**, reachability
  clean, **measured in both directions independently by the operator**:
  10 seats all fillable gave 10 audit writes for 10 assignments and zero
  trial writes; 6 seats uncovered gave 6 audit writes (the uncovered
  records themselves), zero trial writes, and the reason text intact at
  full detail —
  `CPT-01+CPT-02 (REJECTED): commander: CPT-01's MEDICAL expired 2026-07-01, ...`

  That last check had been unobtainable across four rounds and matters
  more than the count: **`uncovered_seats.reason` is now the ONLY
  surviving explanation of an unfilled seat.** Treat it as such — it is
  load-bearing regulatory evidence, not a convenience string.

  **No reboot, no migration**, confirmed structurally: no new service
  module, and no added `import`/`from` line anywhere outside `tests/`.

  **Before changing anything about `audit_trials`, read
  `tests/test_audit_scope.py`.** Four static guards, all
  mutation-tested, and the two that carry the weight are: the default is
  `True`, so silence never yields silence; and it gates ONLY trial
  outcomes, so `ASSIGNMENT_CREATED` sits outside any branch the flag can
  reach. The second is checked by walking the AST ancestor chain rather
  than grepping, which is what makes it hold under a reindent. Widening
  who may pass this flag is an operator decision about the regulatory
  record, not a test to edit.

- **`generator-round-trips` merged into `main` (2026-08-26).** Roster
  generation was unusable in production: **4,822 database round-trips to
  fill 10 seats**, 7+ minutes against a 23-second estimate. Four stacked
  multipliers — per-candidate age queries, the second-pilot list rebuilt
  inside the commander loop, crew/flight rows re-fetched per trial, and
  duty history reloaded per trial. **651/651 verified against real
  Postgres 16**, reachability clean, **133 round-trips for 5 rotations
  measured independently** (from 4,822), and the guards now run with no
  database at all in 0.54s.

  **No reboot needed.** Verified structurally rather than from memory:
  the branch adds exactly one file, `tests/test_generation_round_trips.py`,
  which is a test; every added `import`/`from` line in the whole diff is
  inside that test file; no page changed. No new service module, no new
  import edge, so the stale-`sys.modules` rule does not apply. No
  migration either — this deploy has no ordering requirement of any kind.

  **⚠ THIS BRANCH CAUSED A PRODUCTION INCIDENT — see the dated entry.**
  Its own tests wrote **2,954 rows into the live `audit_log`**, a table
  that held 165 before that day. Nothing was deleted; the rows remain,
  identifiable by `PAIR_ASSIGNMENT_REJECTED` + 2026-08-26 + `app_user=occ1`.
  **Whether a rejected speculative trial should leave a permanent audit
  row at all is an OPERATOR decision** about what the regulatory record
  should contain — it is deliberately NOT being made by whoever next
  reads this. Left as-is under `test_audit_writes_do_not_grow_quadratically`,
  `xfail(strict=True)`. Being put to Arif (2026-08-26).

  **`audit_write` is still exactly `C x S`.** Reads are linear and
  fixed; the quadratic term moved into the audit trail rather than
  disappearing. Anyone quoting a round-trip figure for this generator
  should say which of the two they mean.

- **⚠ A PRACTICE THAT WAS RECORDED HERE AND WAS WRONG — `env -u` does
  NOT isolate a test from the database (2026-08-26).** The advice given
  on 2026-08-22, to run new DB-free tests with `DATABASE_URL` unset, is
  worthless as an isolation check: **`db/db.py` calls `load_dotenv()`,
  which reads `.env` off disk regardless of the process environment.**
  A test verified that way still wrote thousands of rows to production.

  Two non-obvious causes compound it, and both are easy to repeat:

  - **`from X import y` binds a COPY.** Patching
    `services.audit_service.log_audit` does nothing to the name
    `assignment_service` bound at import time. Same for
    `from db.db import get_engine`. The escaping call was found by
    tracing it, not by adding patches until the failure went quiet —
    which is how you get a test that passes for the wrong reason.
  - **Patch by ENUMERATION, not by name.** Walk every service module and
    replace the attribute wherever `hasattr` finds it, so a service
    added later is covered automatically instead of being silently
    missed.

  **The check that actually works:** make every `get_engine` binding
  raise on any attribute access, then confirm the tests still pass. If
  the runtime drops sharply when you do, the earlier run was making real
  network calls — here it went from 75s to 2.0s, and that 73-second gap
  was live traffic to production Supabase.

- **`flight-status-transitions` merged into `main` (2026-08-21).**
  `flights.status` could previously only ever become `CANCELLED`, so a
  flight that flew stayed `PLANNED` forever and `DISRUPTED` was
  unreachable. OPERATED is now automatic (both actual times recorded),
  DISRUPTED is a manual label with a required reason in both
  directions. **647/647 verified against real Postgres 16**,
  reachability clean — every transition run end to end, including the
  split-shift case where the two actuals arrive on separate calls, which
  is the one a page-level rule would have got wrong. See the dated log
  entry at the end of this file.

  **⚠ TWO SEPARATE DEPLOYMENT REQUIREMENTS — do not conflate them:**

  - **`migrations/020` must be applied BEFORE the deploy** that ships
    the rule. Otherwise freshly-recorded actuals set `OPERATED` while
    older rows with identical data still read `PLANNED`, and the record
    goes inconsistent in a way that looks like the bug rather than the
    fix.
  - **No reboot is needed for this one.** It adds no module and no new
    import edge, so the stale-`sys.modules` rule does not apply. The two
    requirements are independent and only the first applies here.

  **Before writing any report on `status`:** it does NOT mean "flew" —
  see the dated entry. The honest test is
  `dep_time_actual IS NOT NULL AND arr_time_actual IS NOT NULL`.

- **`control-room-restructure` merged into `main` (2026-08-21).**
  Control Room and Flight Log both carried an identical add-flight form;
  the split now follows what a controller is doing — Control Room is
  where you ACT (operational status, create a flight, crew it), Flt
  Schedule is the RECORD. Home reverts to the DB status line and
  navigation. **626/626 verified against real Postgres 16**,
  reachability clean, including the moved add-flight tests, the status
  board reading seats from `operating_position` (CPT/CPT case included),
  both DB-failure paths, and `search_roster()`'s new column. Merged,
  pushed; branch deleted, remote and local.

  **⚠ Needs a reboot from Manage app after deploying** — adds
  `services/time_entry.py`, imported by two pages. **Third occurrence**
  of the stale-`sys.modules` condition; the rule is recorded below and
  in the 2026-08-20 entry.

  Took three real-Postgres rounds, all test-side, all one theme: tests
  addressing widgets by POSITION, plus one helper with an invisible
  side effect. Nothing in the pages themselves was wrong on any round.

- **`ops-findings-round-1` merged into `main` (2026-08-20).** The first
  operational-use findings: qualification renewal through the UI (which
  was blocking — renewing a medical previously required raw SQL), other
  occupants recordable at last, Control Room's single-crew path removed
  and crew made optional, a home-page ops banner, crew/flight display
  labels, and an honest UTC timestamp. **621/621 verified against real
  Postgres 16**, reachability clean, including the three paths that
  could not run locally: a 2020 medical renewing to 2030 through
  `update_crew`, occupant free text round-tripping, and the label
  fallbacks for a null staff ID and a null `flight_no`. See the dated
  log entry at the end of this file. Merged, pushed; branch deleted,
  remote and local.

  **⚠ Needs a reboot from Manage app after deploying** — it adds
  `services/display_labels.py`, a new module imported by four pages.
  This is the second time: the same stale-`sys.modules` condition took
  the Schedule Templates page down on 2026-08-19. Streamlit re-executes
  page scripts on every rerun but keeps imported modules for the life of
  the process, so a page calling into a module the running process never
  imported raises `AttributeError`/`ImportError` until restarted. Treat
  "this change adds or newly imports a service module" as meaning
  "reboot after deploy", every time.

- **⚠ ONE OUTSTANDING BRANCH, HELD DELIBERATELY (2026-08-19):
  `pin-python-version`.** Not merged, not abandoned. It adds
  `runtime.txt` declaring `python-3.12` and a guard asserting the suite
  runs on the declared interpreter. **Held because the problem it solves
  is not currently live:** the verification sandbox is already on 3.12
  and matches Streamlit Cloud. Merging it would also turn the suite red
  on any local venv still on 3.14 until that venv is rebuilt:

      py -3.12 -m venv venv
      venv/Scripts/python -m pip install -r requirements.lock

  Merge it when a machine that runs the suite is on a different Python
  from Cloud, or when Cloud's default moves. Note explicitly, so it is
  not merged under a false premise: **this guard would NOT have caught
  the 2026-08-19 outage** — that was investigated as a suspected
  3.12-vs-3.14 `pandas` difference and was neither.

- **`hotfix-schedule-templates-render` and `review-table-duty-window`
  merged into `main` (2026-08-19).** The first restored the deployed
  page after a delete affordance took it down; the second replaced
  first-departure/last-arrival with the real duty window in draft
  review. **595/595 and 600/600 verified against real Postgres 16**,
  the second including `compute_duty_window()` checked directly against
  independently derived production figures (domestic 18:15 → 00:00,
  international 00:45 → 11:30). Both merged, pushed; branches deleted,
  remote and local. See the dated log entries at the end of this file.

- **`schedule-template-fixes` merged into `main` (2026-08-19).** Schedule Templates:
  the widget-key data corruption that saved one template's legs into
  another, HHMM UTC time entry replacing `st.time_input`, route
  continuity validated at creation instead of days later at expansion,
  and delete-when-unused for recovering from a mistaken creation. See
  the dated log entry at the end of this file, including why the delete
  needed no trigger bypass. **592/592 verified against real Postgres
  16**, reachability clean. Merged, pushed; branch deleted, both remote
  and local.

  Took three real-Postgres rounds. The first (13 failures) and second
  (1 failure) were entirely test-side — `migrations/019` was sound from
  the first application, and the trigger rewrite never relaxed anything.
  Round one's cause is the one worth remembering: a test helper omitted
  two required arguments, so the whole delete and trigger-regression
  suite failed at setup and **never executed**, which locally is
  indistinguishable from skipping for want of Postgres. Same shape as
  the two environment failures logged on 2026-08-18. Every service call
  in the changed test files is now bind-checked against the real
  signatures before pushing.

  The database-level guarantee is proven still database-level:
  `test_the_trigger_still_refuses_a_delete_that_bypasses_the_service`
  issues a raw DELETE with no help from the service layer and expects
  the database to refuse it.

- **`auth-and-attribution` merged into `main` (2026-08-18).** Authentication for
  attribution — three fixed OCC accounts, PBKDF2 via stdlib `hashlib`,
  a `require_login()` gate in all 8 page files, and `app_user` threaded
  through 18 page call sites and 36 service-internal forwards, closing
  the NULL `audit_log.app_user` gap. See the dated log entry at the end
  of this file for the full design, including why `st.login()`/OIDC was
  evaluated and rejected. **563/563 verified against real Postgres 16**,
  reachability clean.

  `test_writes_by_a_logged_in_user_never_leave_a_null_app_user` — the
  end-to-end proof that `app_user` reaches the column rather than merely
  being passed — passed on that run, its first execution anywhere.
  `migrations/018_users.sql` applied to a database already at 017 with
  existing crew data: table created correctly, existing rows intact.
  Merged, pushed; branch deleted, both remote and local.

  Took three real-Postgres rounds, and **none of the three found a bug
  in the feature**: round 1 a CWD-dependent `AppTest.from_file()` path
  bug in the test harness (57 errors), round 2 an unpinned
  `streamlit>=1.38` resolving to 1.61.1 in a recycled sandbox (10
  failures, reproduced identically on unchanged `main`). The pattern
  behind every wrong conclusion drawn along the way was the same:
  treating a local environment as representative when it was silently
  under-reporting. Two independent instances of that in one piece of
  work — the version drift, and `openpyxl` declared but never installed,
  hiding 41 tests that had never run in that environment through every
  prior round. `REQUIRE_DB=1`, `tests/test_dependency_pinning.py`'s
  installed-versus-pinned check, and the DB-free harness guards in
  `tests/test_auth_coverage.py` all exist to make that failure loud
  instead of silent. The pinning guard proved itself on first contact in
  a fresh sandbox, reporting `pandas`/`sqlalchemy` drift precisely
  rather than as downstream errors.

  **Still open as its own scoped task: Streamlit 1.61.x compatibility.**
  Pinned to 1.60.0; the same failures reappear at the next upgrade. See
  the 2026-08-18 log entry for the shape of that work.

  **Production migration note.** `migrations/018_users.sql` needs
  applying to the real Supabase database, and the three OCC accounts
  seeding with `scripts/seed_users.py`, before the deployed app is
  usable — every page now requires a login, so deploying this without
  seeded accounts locks the app for everyone.

- **`flight-deck-crew-package` merged into `main` (2026-08-14).**
  Commander/Second
  Pilot seat model — see the dated log entry near the end of this file
  for the full design/build history. **527/527 verified against real
  Postgres 16** (two rounds: 517/526 first pass found nine failures —
  eight test-side, one a real bug in `publish_window()`'s per-rotation
  re-validation, fixed and documented in the log entry below; 527/527
  clean on re-verification), reachability clean, diff scope confirmed
  exactly the 16 files across the branch's two commits. Merged, pushed;
  branch deleted, both remote and local.

  **Production migration note — CORRECTED 2026-08-16, prior note here
  was stale.** `migrations/016_operating_position.sql` and
  `017_uncovered_seats.sql` ARE applied to the real Supabase database
  — confirmed via `run_migrations.py --status` (`Applied: 18,
  Pending: 0`), and the deployed app has been verified working against
  the new schema. This piece is live in production.

- **`home-page-branding` merged into `main`** — home page branding in
  two rounds: round 1 (2026-08-10, logo/background/theming) and round
  2 (2026-08-11, refinements from actually running round 1 — an inline
  double-size logo replacing "Air Eagle" in the title, a green
  DB-status circle, an inline UTC clock, `st.logo()` removed from all
  8 files in favor of the inline logo plus seven new `st.page_link()`
  navigation buttons, and a move to `st.navigation()` so the sidebar
  reads "Home" without ever renaming `app.py` — see the two dated log
  entries below for full detail on each). Round 2 also found and fixed
  a real bug in `scripts/check_reachability.py`: `ENTRY_POINTS` was
  dead code — nothing actually read it, so a renamed or missing entry
  point could produce a false-clean "all reachable" pass. Reproduced
  directly, fixed properly (the reachability scan now actually reads
  `ENTRY_POINTS` instead of a separate hardcoded string) and guarded
  going forward (`missing_entry_points()`, checked first in `main()`,
  three new tests).

  **480/480 verified against real Postgres 16.** The user independently
  tested the reachability guard directly — pointing `ENTRY_POINTS` at a
  nonexistent file now exits 1 with a named warning, where the
  identical scenario previously exited 0 reporting "all reachable" —
  and confirmed the sidebar genuinely reads "Home" with the page
  rendering correctly. Merged into `main`, pushed; branch
  `home-page-branding` deleted, both remote and local. **The
  background-image resolution open item (below) was resolved
  afterward, on its own branch (`background-image-update`,
  2026-08-11): 640x480 → 910x672, dark overlay removed, DB-status date
  format fixed to dd-mm-yyyy, and the redundant page-link button grid
  removed entirely (sidebar navigation already covers it). Merged into
  `main`, pushed; branch deleted, both remote and local.** A 3000px+
  original (likely available if this came from a phone camera) would
  still be worth swapping in later, same path, same approach — that
  part remains open. See the two dated log entries below for full
  detail on the original merge, and the newest entries at the end of
  the file for the image update and button removal.

- **Merged into `main` before that**: `streamlit-cloud-secrets` —
  `db/db.py`'s `get_engine()` raised `RuntimeError: DATABASE_URL not
  set` on every page when deployed to Streamlit Community Cloud —
  `.env` is gitignored and doesn't exist in the deployed container,
  and nothing read `st.secrets` anywhere (confirmed via grep). Added
  `_resolve_database_url()`: `.env`/environment still wins when
  present (unchanged — every test/script depends on this), falls back
  to `st.secrets` only when `DATABASE_URL` is absent from both.
  `streamlit` is imported INSIDE the function, not at module level —
  `db.py` is imported by `scripts/run_migrations.py`/
  `scripts/import_crew_from_xlsx.py` outside any Streamlit runtime,
  and shouldn't have to pull in the Streamlit runtime just to run a
  migration. `st.secrets` genuinely raises
  `StreamlitSecretNotFoundError` with no runtime context and no
  `secrets.toml` (confirmed directly, not assumed) — guarded with a
  broad `try/except Exception: return None`, falling through to the
  existing `RuntimeError` rather than crashing with a different,
  confusing exception. Three new tests in `tests/test_env_override.py`
  (now 7, alongside the two original `override=True` regression
  tests, unmodified and still passing): secrets used when the env var
  is absent; env var wins when both are present — proven with a spy
  that raises if `st.secrets.get()` is ever even called, not just by
  checking the winning value; and `_resolve_database_url()` works with
  NO Streamlit runtime at all (the real, unmocked `st.secrets` call,
  exactly `scripts/run_migrations.py`'s own situation) — the test that
  protects the migration script and the whole suite. `README.md` gets
  a new "Deploying to Streamlit Community Cloud" section with the
  exact secret TOML format.

  **476/476 verified against real Postgres 16.** The user additionally
  confirmed, beyond the test suite, the three things that actually
  protect the non-Streamlit callers: `_resolve_database_url()` returns
  `None` with no runtime and no env var (so `get_engine()` raises the
  correct `RuntimeError` rather than leaking a Streamlit-specific
  exception); `scripts/run_migrations.py` runs clean; and `db.py`
  genuinely does not pull `streamlit` into `sys.modules` on import —
  confirming the in-function-import design decision held. Merged into
  `main`, pushed; branch `streamlit-cloud-secrets` deleted, both remote
  and local. See the dedicated log entry below for full detail.

- **Merged into `main` before that**: `schedule-templates-page` — `pages/7_Schedule_Templates.py`,
  Phase 7's SECOND AND LAST UI. Three
  workflows in order: (1) view/create rotation templates and new
  versions, (2) expand a window into DRAFT instances, (3) review
  (approve/reject) drafts. One real, flagged addition to `services/
  rotation_template_service.py`: `get_all_rotation_codes()` — nothing
  existing could list what templates exist at all (`get_versions()`
  requires already knowing the code), a pure read accessor, same idiom
  as `get_versions()`/`get_instances()`. Leg entry is a fixed 5 blank
  rows, not dynamic add/remove — real rotations are 2-3 legs, and
  dynamic add/remove would need `session_state` + reruns inside what
  should be one atomic submit for no real benefit at this leg count.
  Client-side validation goes one step past `_validate_legs()` (which
  only enforces `flight_no`): also checks `arr_time` is after
  `dep_time` at creation time, closing a real gap confirmed directly —
  a template with dep 20:00/arr 19:00 is accepted by `create_template()`
  and only fails later, at `expand_and_persist()`, with "arr_time
  19:00 is not after dep_time 20:00." Creating a new version shows the
  least-obvious behaviour live, before confirmation: "this will end
  version N on {effective_from - 1 day}," computed with the same
  subtraction `create_new_version()` uses internally, via a date picker
  kept outside any form so the preview updates on every change; the new
  version's own days-of-week/legs pre-fill from the current version so
  the common "same route, new dates" case needs no retyping. Bulk
  review uses one `st.checkbox` per row (not `st.data_editor`, which
  turned out to have no `AppTest` accessor at all — confirmed directly
  before committing to the design, since an untestable design wasn't
  acceptable here) inside a plain `st.columns` grid, with each row's
  route/flights/report/debrief inlined as columns so "a draft must show
  its legs before approval" is satisfied without a per-row expander
  reintroducing the 36-click friction the checkbox/select-all design
  exists to avoid. "Select all visible"/"Clear selection" use a
  pending-flag pattern (`session_state` mutated *before* the checkboxes
  instantiate, not after — confirmed directly that setting it after
  raises `StreamlitAPIException`). Approve/reject each call
  `approve_instance()`/`reject_instance()` individually per selected id
  — batched is the clicking, not the decision or the audit record — and
  `selected_ids` is always computed from the *currently visible* id
  list, never scanned from all session keys, so a selection made under
  one rotation_code filter can't get swept into an action taken after
  switching filters (a dedicated test covers exactly this). Approve
  reports the real resulting flight count (`sum(len(flight_ids))`
  across every successful call) — the moment a draft becomes
  operational. Reject requires a reason; the button is simply disabled
  without one.

  A real `AppTest` timing finding shaped the test file: a single
  `at.run()` after a button click does NOT reliably surface a transient
  `st.success()` banner when `st.rerun()` follows it AND more script
  runs after that (every action here has later workflow sections
  below it) — confirmed directly via ad-hoc scripts, not assumed.
  Create/create-version/approve/reject are therefore tested via real
  effects (querying the service directly) rather than the transient
  banner text; Workflow 2 (expand) has no `st.rerun()` at all and IS
  tested via its banner directly. Validation-error paths (no rerun
  involved) are asserted via `at.error` text directly too.

  **473/473 verified against real Postgres 16, first run, zero
  failures** — the user additionally drove the page directly beyond the
  test suite (`get_all_rotation_codes()` correctly empty then
  populated, the page rendering without exception both empty and
  populated, a real expansion producing exactly 5 Mon-Fri drafts for
  3-7 Aug with a checkbox each) and confirmed the `arr_time`/`dep_time`
  validation gap empirically before ever approving the plan.
  `scripts/check_reachability.py`: still zero files flagged. See the
  dedicated log entry below for full detail.

- **Merged into `main` before that**: `roster-generation-page` — `pages/6_Roster_Generation.py`,
  Phase 7's first UI — presentation only over `roster_generator_service.generate_for_window()`/
  `publish_window()` and `rotation_template_service.get_instances()`, no
  new service logic. One linear flow: pick a date window, see a live
  pre-generate preview (approved-rotation count, time estimate, a
  non-blocking warning above ~35 days), Generate, see results, Publish.
  `uncovered` renders first and most prominently, with the real
  legality-gate rejection string verbatim; per-pilot duty counts derived
  from `filled` (already duty-level, no dedup needed) show fairness at a
  glance; `already_covered` shown de-emphasized. Publish is deliberately
  independent of whether Generate ran in the current session — computed
  fresh from the database every render, so a controller can publish a
  window generated earlier without re-running Generate — and states the
  unassign-to-reject workflow explicitly (`remove_assignment()` already
  marks `CANCELLED`, `publish_window()`'s own `WHERE status = 'PROPOSED'`
  already skips it; no new function needed). `st.session_state` used
  here for the first time on this page too, holding only the
  just-generated `GenerationSummary` for display across the Publish
  rerun — explicitly NOT a repeat of this project's "session_state is
  demo-only" lesson, since the real effect (the `PROPOSED` rows) is
  already durably written the moment `generate_for_window()` returns and
  that call is idempotent by design. `services/roster_generator_service.py`
  now clears `check_reachability.py` for the first time since Phase 1 —
  every file under `core/`, `services/`, `db/` is reachable. AppTest
  coverage (`tests/test_roster_generation_page.py`) seeds through the
  real chain (`create_template` -> `expand_and_persist` -> `approve_instance`),
  the first page test to exercise all of Phase 7 end to end, including a
  guaranteed no-candidates uncovered case and a real rest-math rejection
  reaching the page unaltered (distinct cases — one proves the section
  renders in the right place, the other proves the actionable string
  itself survives to the UI).

  First real-Postgres pass found one real issue, in the test file, not
  the page or service: `_QUALIFICATION_DEFAULTS` covered all eight
  expiry fields but omitted `date_of_birth`, so every seeded pairing
  hit `AE-CREW-PAIR-AGE-001_DOB_MISSING` -> `NEEDS_MANUAL_REVIEW` and
  no seat ever filled (459/464, all 5 failures traced to this one
  cause). Fixed with a single added line — a fixed, clearly-under-65
  `date_of_birth`. **464/464 verified against real Postgres 16, zero
  failures, on the re-verification pass.** `check_reachability.py`:
  **"All files under core/, services/, db/ are reachable from
  somewhere" — the first fully clean run since that checker was
  written in Phase 1.** Every file in the repo is now genuinely wired
  to something a user can reach. See the dedicated log entry below for
  full detail.

- **Merged into `main` before that**: `assistant-page-ui` — `pages/5_Assistant.py`,
  the OCC assistant's UI, first page to call `query_parser.parse()`/
  `reports.run_report()`. Presentation only, no new service logic.
  Interpretation always renders before results (including on an empty
  result — a controller most needs to see what was understood exactly
  when nothing came back). Editable date range (session-state only, no
  DB, no "persist to survive refresh" — this is a transient UI query
  result, not operational data). A real Streamlit `date_input`
  widget-key bug was found and fixed before the test suite existed —
  see the dedicated log entry. 455/455 verified against real Postgres
  16, zero failures; the user independently drove the page through
  `AppTest` with real seeded crew beyond the test suite
  (interpretation-before-table, the decision-refusal redirect, the
  real two-Mahmood ambiguity). `services/assistant/reports.py`
  confirmed no longer flagged by `check_reachability.py` — closes a
  gap open since 2026-08-01; `services/roster_generator_service.py`
  stays flagged, unrelated, no Phase 7 piece has UI yet.

- **Merged into `main` before that**: `query-parser-refusal-and-date-fixes`
  — operator scope decision (the OCC assistant generates tables only,
  never a legality/decision answer, superseding any earlier
  general-assistant framing) plus real-Postgres-tested
  `query_parser.py` fixes: a decision-question refusal layer (5
  confirmed wrong-template cases), DD-MM-YYYY/DD/MM/YYYY date ranges,
  bare "N days", plural role words, two wrong-semantics date bugs
  ("since"/"before"), and a `utilization` keyword gap. 443/443 verified
  against real Postgres 16, zero failures — the user independently
  confirmed all 5 refusal cases, both flagged word-boundary collisions,
  and every date fix beyond the test suite. One non-blocking follow-up
  the user found in that verification: "how close is Waqar to his 1000
  hour limit" routes to `utilization` correctly but resolves with no
  `window_days`, so the report has nothing to compute D9.2.3's rolling
  365-day limit against — recorded in `Open stubs`, not fixed here. See
  the dedicated log entry below for full detail.

- **Merged into `main` before that**: `open-stubs-cleanup-2026-08-08`
  — four operator decisions from `Open stubs`, implemented as one piece: `snack_provided`
  wired up exactly like `meal_provided` (migration 015,
  operator-confirmed); `reactivate_crew()` settled as NOT needed
  (documentation only, no code); age-pairing added to
  `find_legal_candidates_for_duty()` (reuses `_check_crew_pairing_age()`
  per candidate, no new function — plan-moded per the operator's own
  instruction, approved with a refinement on the missing-DOB case, now
  recorded as a known `DownstreamConflict.candidates` limitation);
  `query_parser.parse()` now populates `ReportRequest.status_filter`.
  Plus a record correction: the Supabase migration-status `Open stubs`
  entry was stale (see that entry's own 2026-08-08 correction). 415/415
  verified against real Postgres 16, first run, zero failures. The user
  additionally re-tested the age-pairing behavior directly beyond the
  test suite (domestic/international discrimination, the "pending"
  silent case) and confirmed it matches the design exactly. See the
  dedicated log entry below for full detail.

- **Merged into `main` before that**: `roster-generator-phase7-final`
  — Phase 7's last piece, the roster generator (fills CPT/FO seats on approved
  rotations via the real `assign_crew_to_duty()` gate, writes
  `PROPOSED` roster rows pending OCC publish). 403/403 verified against
  real Postgres 16, across three real-Postgres passes: the first found
  the `meal_provided` data gap (nothing in the pipeline ever populated
  it, so every international rotation permanently hit D25's
  `NEEDS_MANUAL_REVIEW` gate — fixed with migrations/014 and threaded
  through every real `Duty`-construction site, including historical
  duty rebuilding); the second and third found test-side bugs only (an
  obsolete `NEEDS_REVIEW` premise, a sector-rows-vs-duties miscount
  repeated in two call sites, and a test relying on `is_active=False`/
  `update_crew(is_active=True)`, neither of which actually work —
  `is_active` isn't in `crew_service.UPDATABLE_FIELDS` and no
  `reactivate_crew()` exists at all, a real service-layer asymmetry now
  flagged in `Open stubs`). See the four dated log entries below for
  full detail on each pass. The user additionally ran the full chain
  end-to-end against Air Eagle's real 28-day schedule (36 rotations, 6
  CPT/4 FO): 72/72 seats filled, zero uncovered, duty counts exactly
  matching the pre-computed demand arithmetic (6 and 9 per role) —
  confirming the greedy no-backtracking design reaches optimal
  distribution on the real problem, not just synthetic test scenarios.
  Phase 7 (templates -> drafts -> approval -> generation) is complete.

- **Merged into `main` before that**: `rotation-instance-approval-workflow`
  — DRAFT -> APPROVED promotion, the piece that makes a template
  actually produce operational flights (see the dedicated log entry
  below). 382/382 verified against real Postgres 16, including a real
  fix found on that first pass (a per-file test fixture omitting
  `audit_service`, closed by consolidating all five service modules'
  `get_engine()` patching into one shared `tests/conftest.py` fixture —
  the second time a fixture gap masked real behavior, so the class was
  closed, not just the instance). Adds this repo's second database-level
  uniqueness guarantee beyond migrations/011's `EXCLUDE` constraint — a
  partial unique index (migrations/012) against double-promotion.
  Before that: `rotation-templates-phase7-groundwork` (the
  recurring-schedule-template layer, 365/365, `btree_gist` confirmed
  available on Supabase), Step 7 (age-pairing, AE-CREW-PAIR-AGE-001),
  and Step 6 (transactional atomicity) — see prior entries below for
  full detail on each.

## Files changed
Since commit `727da58`'s push: services/assignment_service.py
(NEEDS_MANUAL_REVIEW branch in both assignment functions,
computed_report_time/debrief_time/fdp_hours added to
AssignmentResult), pages/4_Roster.py and pages/1_Control_Room.py
(NEEDS_REVIEW UI branch — Control Room's missing branch would have
caused a real IndexError on flight_ids[0]), tests/test_assignment_service.py
(5 new dedicated tests + 12 existing tests repaired — see the
NEEDS_MANUAL_REVIEW log entry above for why so many broke),
tests/test_control_room_page.py (seed fix + 1 new UI test),
HANDOVER.md. Also: services/crew_service.py got a CAPT->CPT synonym
added (confirmed from the operator's real data file spelling role as
"CAPT" not "CPT") — small, separate, already pushed alongside the
env-override fix.

## DB changes (migrations applied)
- 000_migration_tracking.sql through 007_crew_columns_reconcile_real_data.sql
  (8 total — see earlier entries in this file for what each does)
- **CONFIRMED applied to the real Supabase database**, not just
  local sandbox Postgres — `Applied: 8, Pending: 0`, cross-checked
  against Supabase's own dashboard. This took several real detours
  to get right: Direct connection failed (IPv6-only hostname, no
  IPv4 on the user's network — confirmed via `nslookup`), fixed by
  switching to Supabase's Session Pooler connection string instead
  (IPv4-proxied by design). No new migrations needed for the
  NEEDS_MANUAL_REVIEW fix — logic-only change.
- **008_drop_type_rating_and_contract_expiry.sql** and
  **009_roster_needs_review_status.sql** (originally authored as a
  second, independent 008 — see "Current active task" for the
  renumbering) — both independently verified by the user against real
  Postgres 16 with existing crew data present (000-007 already
  applied): 008 confirmed to drop both columns cleanly, no data loss.
  Neither confirmed applied to the real Supabase database yet — the
  verification above was against a real Postgres instance, not stated
  to be Supabase specifically; don't assume that step is done without
  checking `run_migrations.py --status` against the actual Supabase
  connection.

## Tests passed
207 total on branch `alert-summarization` (190 on `main` + 17 new: 12
pure-logic in the new tests/test_alert_summary.py, 5 DB-integration in
test_assignment_service.py) — tests/test_migrations.py (4),
tests/test_duty_summary.py (10), tests/test_pcaa_ano012_core.py (12),
tests/test_schema.py (18), tests/test_audit_service.py (3),
tests/test_crew_service.py (21), tests/test_crew_data_page.py (4),
tests/test_duty_builder.py (12), tests/test_flight_service.py (15),
tests/test_flight_log_page.py (5), tests/test_assignment_service.py
(66), tests/test_control_room_page.py (5), tests/test_roster_page.py
(4), tests/test_import_crew_script.py (12), tests/test_env_override.py
(4), tests/test_alert_summary.py (12, new).

All three merged branches independently verified by the user against
real Postgres 16 before merging — 178/178 (`remove-type-rating-contract-fields`),
190/190 (`step5-stale-data-fixes`, on top of that), and 207/207
(`alert-summarization`, on top of that) — see the 2026-08-01 log
entries for full detail on each.

**Unmerged, on branch `query-parser`**: 255 total (207 on `main` + 48
new, all pure logic, no DB — 41 in the new tests/test_query_parser.py,
7 in the new tests/test_check_reachability.py). Verified locally:
110/110 non-DB tests passing (110+145 skipped = 255), 6.41s. Not yet
independently re-verified by the user against real Postgres — though
none of this branch's additions need a database at all:
`query_parser.py` takes the crew directory as a plain argument, same
principle as `core/duty_summary.py`, and `check_reachability.py` is a
pure filesystem/text-scanning script.

177/177 independently verified against real Postgres 16 (2026-07-31,
`qualification-gate` branch, commit `45252da`) — this covers the
crew-qualification gate as it stood then (8 fields, no
type_rating_expiry) plus the debrief-date and AME-synonym-test fixes
that verification run required. Everything since — the
type_rating_expiry addition (`b5d9c05`) AND its removal, plus the
type_rating_expiry/contract_expiry schema drop, on branch
`remove-type-rating-contract-fields` — was independently re-verified
by the user against real Postgres 16: 178/178 passing, migration 008
confirmed to drop both columns cleanly against a database already
carrying crew data, no data loss.

**Independently verified by the user against real Postgres 16 and
merged into `main`**: 301/301 passing on branch `assistant-report-
functions` (255 already on `main` as of the `query-parser` merge this
branch was cut from, + 46 genuinely new — 18 in
tests/test_reporting_export.py, 28 in tests/test_assistant_reports.py;
`flight_service.get_all_flights()` and `assignment_service.search_roster()`
were extended in place, not duplicated, so neither adds a test file of
its own) — including all 20 DB-integration tests this environment
could only trace by hand. Filename format and both plan-approved
report behaviors (crew_duty_history's notes/duty_id, roster_coverage's
comma-joined role lists as they stood at the time) confirmed exact.

**Independently verified by the user against real Postgres 16 and
merged into `main`**: 313/313 passing on branch `operator-crew-scope-
and-coverage-reshape` (301 on `main` + 12 net new — see the 2026-08-02
log entry for the exact breakdown), including all 174 tests this
environment could only trace by hand. Migration 010 confirmed to apply
cleanly against a database already carrying data, no data loss.
LM/AME exclusion confirmed to hold on well-formed rows, and
`_count_occupants()`'s shorthand parsing confirmed correct.

**Independently verified by the user against real Postgres 16 and
merged into `main`**: 318/318 passing on branch `transactional-
atomicity-control-room-write` (313 on `main`, this branch cut directly
from `main` rather than from the still-unmerged crew-data-dropdown
branch, + 5 net new — 1 in `tests/test_assignment_service.py`'s
Control Room regression test, 1 in its `assign_crew_to_duty()`
counterpart, 3 in `tests/test_audit_service.py` for `log_audit()`'s new
`conn` parameter). Also independently confirmed by the user via a
manual crash simulation on both versions of the code (sabotaging the
second `log_audit()` call mid-sequence): before the fix, 1 orphaned
flight + 1 roster row survived; after, full rollback to zero.

**Independently verified by the user against real Postgres 16 and
merged into `main`**: 338/338 passing on branch
`age-pairing-rule-ae-crew-pair-age-001` (Step 7 — see the dedicated log
entry below for full detail), including a real-data empirical check
(domestic 67+67 rejected, domestic 67+41 allowed, international 67+41
rejected) and reachability unchanged. One real test-fixture gap found
and fixed along the way (see that log entry) — not a bug in the rule.

**`crew-data-role-dropdown-cpt-fo-only`**: originally verified at
314/314 on its own base, predating Step 6/7. Per instruction, not
merged on the strength of that figure — `main` merged into this
branch and the full current suite re-verified here (2026-08-02) before
resuming towards merge. See `Current active task` above for actual
merge status, rather than repeating it here.

## Open stubs / known blockers
- **RESOLVED 2026-09-05 — only PLANNED flights may be crewed, enforced
  at the service.** Operator decision: PLANNED only; anything else, OCC
  handles outside the system. `assignment_service.
  CREWABLE_FLIGHT_STATUSES` is the one place that says so, and
  `_refuse_uncrewable_flights()` is called at the top of BOTH write
  doors — `assign_crew_to_duty()` and `assign_pair_to_duty()` — before
  the partner lookup and before any audit row can be written.

  Kept here rather than deleted because the SHAPE of the gap is worth
  remembering: the Roster picker had already stopped offering
  non-PLANNED flights the day before, and that looked like the fix. It
  guarded one caller. The generator, a future page, a script and a
  console session all reached the function directly. **A guard in front
  of one door is not a guard on the room** — and the picker version
  would have kept looking correct indefinitely, because the only way to
  notice is to call the service yourself.

  NOT added to `_validate_pair_internal()`, which the two write doors
  share with the read-only `validate_pair()`. That function is what the
  swap-alert scan asks "could this seat be filled by someone else"
  through. A scan is a QUESTION, not an assignment, and making a
  question raise on a flight that has since been cancelled would turn a
  report into a crash.
- **`query_parser.py`: "how close is Waqar to his 1000 hour limit"
  resolves to `utilization` correctly but with no `window_days`, so the
  report has nothing to compute against — found 2026-08-08 by the user
  in real-Postgres verification, not a blocker.** D9.2.3 is a rolling
  365-day limit — the operator's own suggestion: default
  `window_days=365` when the query mentions an hour limit but names no
  period, since that's the only window the question can sensibly mean.
  Not implemented yet; would need a keyword/pattern check in `parse()`
  similar to how `template` is already threaded into `parse_dates()`
  for the bare-"N days" direction fix (2026-08-08) — the same
  "already-resolved template informs date parsing" mechanism would
  apply here too.
- RESOLVED 2026-08-09: `scripts/check_reachability.py` flags nothing —
  `pages/6_Roster_Generation.py` now imports `services/roster_generator_
  service.py` (see the dedicated log entry below), which was the last
  file flagged since `assistant-page-ui` (2026-08-08). Every file under
  `core/`, `services/`, `db/` is reachable from `app.py`/`pages/` for
  the first time since Phase 1. Note this cleared on an unmerged branch
  (`roster-generation-page`) — re-run this check after `main` catches up.
- **RESOLVED 2026-08-08 — no `reactivate_crew()` in `services/
  crew_service.py`, and it stays that way.** Found 2026-08-08 tracing
  a test bug (see the dedicated log entry for that date). Operator
  decision: Air Eagle doesn't need reactivation — a deactivated crew
  member stays deactivated. Not building it. Still worth knowing as a
  trap, though: `deactivate_crew()` is a real, dedicated, audited
  soft-delete (`is_active = FALSE`), and `is_active` is deliberately
  absent from `UPDATABLE_FIELDS`, so neither `add_crew()` nor the
  generic `update_crew()` can set it — any future attempt to
  reactivate via `update_crew(crew_id, {"is_active": True})` will
  silently no-op (the field gets filtered out before the UPDATE is
  built), not raise, not that anyone's meant to try. The only way to
  flip `is_active` back to `TRUE` is a raw SQL statement outside the
  service layer, which is correct given the decision above — no
  service-layer path should exist for something that isn't supposed
  to happen.
- **`Duty.meal_provided` is now real data, not a permanent `None`
  (migrations/014, 2026-08-08) — see the dedicated log entry below.
  ASSUMPTION, needs airline validation:** the operator confirmed a meal
  is provided on every rotation today; `rotation_templates.meal_provided`
  defaults `TRUE` and is threaded through expansion/approval/ad-hoc
  Control Room flights into every `Duty`-construction site
  `services/assignment_service.py` has. If this operational fact ever
  stops being universally true, it's now a real per-template value to
  change (`create_new_version(..., meal_provided=False)`), not a
  hardcoded assumption to hunt down.
- **`snack_provided` deliberately still stays unset — not an accident,
  a decision (2026-08-08).** `D2.18_D25_SNACK_REQUIRED` only alerts
  when `snack_provided is False`; `None` produces no alert, which
  happens to be harmless today but isn't *correct* the way
  `meal_provided`'s fix now is. Not wired up because the operator's
  confirmation was specifically about meals, not snacks — a materially
  different D2.18/D25 category — and inferring one from the other would
  be exactly the kind of code-level guess the `meal_provided` fix
  exists to eliminate. Needs its own explicit operator confirmation
  before ever being wired up the same way.
- RESOLVED 2026-08-08: `find_legal_candidates_for_duty()` now checks
  the age-pairing rule (`AE-CREW-PAIR-AGE-001`) per candidate — see the
  dedicated log entry. **New, narrower limitation left in its place**:
  when a candidate is excluded for their OWN age-pairing violation,
  fine; but when a candidate stays included despite the *other* seat's
  real occupant having a missing `date_of_birth`
  (`AE-CREW-PAIR-AGE-001_DOB_MISSING`, deliberately not excluded — see
  that entry for the full reasoning), `DownstreamConflict.candidates`
  (a bare `List[str]`) has nowhere to surface that the uncertainty
  belongs to someone else, not the suggested candidate. First thing to
  fix if `DownstreamConflict` ever grows per-candidate detail.
- RESOLVED 2026-08-08: `query_parser.py`'s `parse()` now populates
  `ReportRequest.status_filter` via `parse_status()` — see the
  dedicated log entry. "cancelled"/"delayed"/"diverted" resolve to
  `flights.status`'s real CHECK-constraint values.
- RESOLVED 2026-08-02: `roster_coverage`'s earlier "required-crew-
  count-per-role is unconfirmed" open question no longer applies —
  see the dedicated log entry below. Coverage is now CPT/FO only;
  LM/AME are never crew records for Air Eagle at all, so there's no
  role-count question left to resolve for them.
- **DG certification for LM/AME is untrackable, accepted deliberately
  (ASSUMPTION, needs airline validation)** — see the 2026-08-02 log
  entry below. `flights.cargo_dg` and `crew.dg_expiry` both exist, but
  free-text occupant names can't be checked against either once
  neither role has a crew record. The operator's stated position is
  that OCC handles this by process. If DG tracking through this
  system ever matters, reintroducing LM/AME as crew records (even
  without FTL applicability) is the fix.
- RESOLVED 2026-08-02: `pages/2_Crew_Data.py`'s manual "Add crew
  member" form no longer offers LM/ENGR as selectable roles — see the
  dedicated log entry below. `ROLE_OPTIONS` is now `["CPT", "FO",
  "Other"]`, closing the inconsistency flagged in the previous entry.
- `services/assistant/regulation_reference.py`'s numbers are boundary-
  tested against the real validator for D9.1.1/D9.1.2/D9.1.3/D9.2.1/
  D9.2.2/D9.2.3/D21.1/D8.2.1 (see `tests/test_assistant_reports.py`).
  D23.1/D23.2/D25 are NOT independently boundary-tested — those three
  entries rely on the plain-English description matching the
  docstrings/comments at the cited `pcaa_ano012_core.py` line ranges,
  not a re-derived test. If any of those three's enforcement logic
  ever changes, this file has no automatic guard for it yet.
- The `crew` table schema (001_crew_table.sql) is built against the
  19-column template, not yet against real operator data. When the
  route network + crew data comes back: check it actually matches
  this shape before writing a new migration to add anything — don't
  assume the template survived contact with a real spreadsheet
  unchanged.
- `crew.role` is deliberately NOT validated against a fixed list at
  either the schema or service layer (the template explicitly allows
  "Other"). If this proves too loose once real data arrives, tighten
  at the service layer, not schema.
- RESOLVED 2026-07-20: Air Eagle's domestic vs international route
  mix — rather than needing an answer up front, this is now decided
  per-flight via flights.domestic (006_flights_domestic_column.sql),
  required at creation time on both Flight Log and Control Room's
  forms. Handles a mixed ad-hoc+scheduled operator flying both
  without needing a single global answer.
- Waiting on: real crew data from operator, and Air Eagle's route
  network — both confirmed still not received as of 2026-07-20
  ("not today, as was expected"). Neither blocks further building —
  see the phase-sequencing discussion in project chat history for
  why. Phase 7's roster generator is the first phase that actually
  needs the route network for its real content (the OR-Tools engine
  itself can still be built schedule-agnostic without it).
- The "reoptimize roster accordingly" scope was explicitly decided as
  alert + suggest legal candidates, human confirms — NOT full
  auto-reassignment. See the Architecture note above. Don't expand
  `_check_downstream_impact()` / `find_legal_candidates_for_duty()`
  to auto-apply a swap without that being a deliberate, separate
  decision — that's real Phase 7/reoptimization-engine territory.
- RESOLVED 2026-07-19: D21 (charter rest) confirmed as the
  applicable rule for Air Eagle's cargo ops. D20 (home/away base)
  code path still exists in the ported engine for a future
  scheduled-carrier client but is not currently exercised by Air
  Eagle's confirmed operation_type="cargo_charter" default.
- "Engr" role definition unconfirmed (flight-deck FE vs
  line-maintenance AME) — flagged on the crew data template,
  still pending with the rest of the operator data.
- **Auth — spec SETTLED (2026-08-04), trigger PARTIALLY FIRED
  (2026-08-16), build starting.** Real crew data is about to be
  entered for a shadow trial — see `docs/AirEagle_Shadow_Trial_
  Training_Guide.md` — which is the trigger condition below. This is
  a deliberate partial fire, not drift: auth is being built now, but
  the Supabase Pro plan upgrade and automated backups (the other two
  legs of the original "all three land together" trigger) are still
  deferred, on the operator's judgement that shadow-trial data is
  re-creatable rather than operationally critical. Full spec as
  originally settled: three
  accounts, all full access, no permission tiers — the app is not
  publicly reachable, so a CONTROLLER/ADMIN split isn't buying
  anything real right now. Session-level login is acceptable (re-login
  on a hard refresh is fine for three OCC staff) — no cookie-
  persistence mechanism needed. This resolves the two open questions
  the 2026-08-02 plan had been waiting on; see that entry for the
  fuller design (self-contained `users` table + password hashing,
  `require_login()` at the top of each page).
  **The important half of this isn't restriction, it's attribution**,
  and **THAT HALF IS NOW DONE — CORRECTED 2026-09-05.** The paragraph
  that stood here said `audit_log.app_user` was `NULL` on every row
  because no page passed it through. That was true when written and
  quietly stopped being true when auth shipped. Measured against
  production on 2026-09-05, read-only:

  ```
  occ1           261 rows   2026-08-19 .. 2026-09-04
  import_script   10 rows   2026-08-18
  occ3             6 rows   2026-09-03 .. 2026-09-04
  ```

  **277 audit rows, every one attributed.** The permanent regulatory
  record now says WHO as well as what and when, which is what this
  entry was asking for. Every write function already accepted
  `app_user: Optional[str] = None`; the pages now populate it.

  Left here rather than deleted because the reasoning still governs
  anything new: a service write that does not carry the logged-in
  user's identity through to `log_audit()` is a gap in a
  PCAA-regulated operator's regulatory record, not a cosmetic
  omission. A new write path has to thread `app_user` the way the
  existing ones do.
  **Trigger to actually build this — shared with the Supabase item
  below**: the moment any real crew or flight data enters the
  production Supabase database. Not before.
- **Supabase stays on the free tier — deliberate, not an oversight,
  same trigger as auth above.** No automated backups, and the project
  pauses after 7 days of inactivity — both accepted as long as the
  database holds nothing real. **CORRECTED 2026-08-08 — the migration-
  status claim above was stale.** Migrations 000-007 were confirmed
  applied to the real Supabase database on 2026-07-30, verified two
  ways: `run_migrations.py --status` (`Applied: 8, Pending: 0` at that
  time) and cross-checked directly against Supabase's own dashboard
  (see the "DB changes" entry near the top of this file for the full
  detail, including the Direct-vs-Session-Pooler connection detour that
  made this possible). **CORRECTED AGAIN 2026-09-05: the "eight
  migrations behind" claim below was itself stale.** It said 008-015
  had never been applied. Measured against production on 2026-09-05 by
  reading `schema_migrations` directly (read-only — deliberately NOT
  `run_migrations.py --status`, which calls `ensure_tracking_table()`
  and is therefore DDL against production):

  ```
  000 .. 020 applied — ALL 21.  Pending: (none)
  ```

  Production is fully migrated, including 018_users and 019. Nothing
  is behind. The GitHub-integration collision risk (Supabase's native
  migration deploy expects a `supabase/migrations/` folder this repo
  does not use) was never confirmed resolved either way, and is moot
  while nothing is pending.

  **The pattern is worth more than either correction.** This entry has
  now been stale TWICE in opposite directions — first claiming nothing
  was applied when 000-007 were, then claiming eight were outstanding
  when all of them had landed. A migration-status claim written into
  prose goes out of date the next time anybody deploys, and reading it
  is not a substitute for querying `schema_migrations`. **Ask the
  database.**
  **Backup research findings, recorded now so they don't need
  rediscovering later**: Supabase's own docs recommend free-tier
  projects export via the `supabase db dump` CLI command and keep an
  off-site copy — a single binary, not a full Postgres install.
  Point-in-time recovery (PITR) was considered and ruled out: ~$100/mo
  for 7-day retention is roughly 4x the Pro plan itself, disproportionate
  for this operation's scale, and it REPLACES daily backups rather than
  supplementing them. Deleting a Supabase project permanently destroys
  its backups too, including whatever's in S3 — which is why an
  independent off-site dump stays worth keeping even after upgrading.
  Restoring from any backup takes the project offline for the duration
  of the restore.
  **The actual trigger, all three land together**: the moment real
  crew/flight data enters production — Pro plan (~$25/mo, daily
  backups, 7-day retention), auth, and backups all land in the same
  move. Not auth without backups, not backups without auth — real data
  existing at all is what makes both suddenly matter, together.

## Next safest step
NOT Phase 7. Still explicitly paused per the external review's core
recommendation (agreed, reconfirmed by a second independent review):
an OR-Tools generator on top of a legality gate with known holes
would efficiently produce a roster that looks legal and isn't.
Remaining remediation order (updated 2026-07-21), each step tested
and pushed independently before the next, same discipline as every
phase so far:

1. ~~Push already-verified-locally work~~ DONE — commit 32098e7,
   independently confirmed via fresh clone.
2. ~~Fix `NEEDS_MANUAL_REVIEW` being silently treated as ALLOWED~~
   DONE (this snapshot) — see the dedicated log entry above. Had a
   much bigger ripple effect than expected (12 existing tests needed
   repair) and surfaced a second real bug in the process (both
   Roster and Control Room pages were missing a UI branch for this
   status — Control Room's gap would have caused a real IndexError).
3. ~~Fix domestic/geographic-continuity~~ DONE — any
   international sector makes the whole duty use the international
   buffer; each flight keeps its own domestic flag independently.
   Geographic continuity (leg N destination == leg N+1 origin) added
   alongside the existing temporal check. This directly unblocks the
   real KHI-LHE-DWC-KHI rotation.
4. ~~The qualification gate~~ DONE (2026-07-31, field set revised
   2026-08-01 — see the dedicated log entries below, AE-CREW-QUAL-001,
   for full detail and the reversal). Role match was already enforced
   (Step 2's work); this closes the rest: is_active plus 8
   document-expiry fields (type_rating_expiry and contract_expiry
   were dropped from the crew schema entirely, migrations/008 — see
   below), checked against the duty's own debrief (end) date — not
   report date, not date.today() — applied to every role including
   FTL-exempt LM/ENGR, and folded into both `_validate_new_duty()` and
   `find_legal_candidates_for_duty()` so an unqualified crew member
   can no longer be suggested as a
   downstream-swap candidate either.
5. ~~Three related "stale data" findings~~ DONE (2026-08-01, branch
   `step5-stale-data-fixes` — verified by the user against real
   Postgres 16, 190/190, D9.2.3 confirmed empirically firing with
   ~300 seeded duties; see the dedicated log entry below for full
   detail). LOOKBACK_DAYS widened 35 -> 370 (D9.2.3's
   365-day/1000h check can now actually see enough history to fire);
   flight_service.update_flight()'s actual-times path now recomputes
   FDP and revalidates crew via a new
   assignment_service.update_flight_actual_times_and_revalidate();
   flight_service.cancel_flight()'s roster rows are now cascaded via
   a new assignment_service.cancel_flight_and_roster(). Both new
   wrappers live in assignment_service.py, not flight_service.py —
   see the log entry for why.
5b. ~~Alert-volume explosion, found during Step 5's own real-Postgres
   verification~~ DONE (2026-08-01, branch `alert-summarization` —
   verified by the user against real Postgres 16, 207/207, merged
   into `main`; see the dedicated log entry below for the full
   measured before/after). A direct, unplanned consequence of item
   5: with 370 days of history to iterate instead of 35,
   `_check_cumulative_limits()`'s one-alert-per-breached-rule-PER-
   HISTORICAL-DUTY design produced 2,215 alerts for a single
   assignment against ~300 seeded duties. New `services/alert_summary.py`
   collapses historical repetition into per-rule-code counts (never
   touching `ValidationResult.status`/`legality_status` — an
   assignment resting on a genuine historical breach still reports
   ILLEGAL, confirmed on the exact same measured scenario) and adds
   `blocked_by_history_only` so a controller can tell whether an
   assignment attempt is itself the cause or the crew member already
   had disqualifying history. Also fixed one real correctness bug
   found in passing: one of 5 audit-log join sites
   (`_recompute_one_duty_after_delay()`) was unfiltered by status
   entirely. `find_legal_candidates_for_duty()`'s own, separate,
   larger per-candidate cost is explicitly deferred, not touched here.
6. ~~True transactional atomicity for Control Room's flight+assignment
   write~~ DONE (2026-08-02, branch `transactional-atomicity-control-
   room-write` — see the dedicated log entry below for full detail).
   The 4 separate `engine.begin()` blocks in `assign_crew_to_new_flights()`'s
   ALLOWED path are now one transaction; `assign_crew_to_duty()`'s
   smaller-scale version of the same gap (roster insert + its audit
   record) is fixed the same way.
7. ~~Age-pairing rule~~ DONE (2026-08-02, branch
   `age-pairing-rule-ae-crew-pair-age-001` — see the dedicated log
   entry below for full detail, including how the architectural
   blocker described below was actually resolved). Wording as recorded
   here was confirmed accurate by the operator before this was built:
   CONFIRMED route-dependent, not uniform (updated 2026-07-21, after
   the single-rule version above was recorded): for a rotation's
   flight-deck pair —
     - **Domestic**: illegal only if BOTH CPT and FO are 65 or older;
       legal if at least one is under 65.
     - **International**: illegal if EITHER CPT or FO is 65 or older
       — both must be under 65. Materially stricter than domestic,
       not the same rule applied to a different route.
   Exactly 65 does not count as "below 65" either way. LM/AME age is
   irrelevant to this rule regardless of route. Age calculated on the
   rotation's first operating date. Missing DOB on either pilot ->
   NEEDS_MANUAL_REVIEW, must not auto-save (ties directly to fixing
   item 2 first — there's no point building a rule whose "needs
   review" output gets silently auto-allowed anyway).
   This can reuse the SAME duty-level `domestic` classification
   already built for the D7.1.2 buffer fix above (any international
   sector -> whole duty is international) — one boolean, two
   consumers, not two separate classification schemes to keep in
   sync. Proposed rule code: AE-CREW-PAIR-AGE-001. Belongs in the
   orchestration layer (assignment_service.py or a dedicated crew-
   composition policy module) as an Air Eagle operating rule, NOT
   inside core/legality/pcaa_ano012_core.py — confirmed directly
   against the actual ANO-012 document (OCR'd, all 32 pages searched)
   that it contains no age-eligibility provision at all; this is a
   licensing restriction from a different PCAA order, labeled as an
   Air Eagle mandatory rule until/unless that source is supplied.
   **Real architectural blocker, not yet resolved**: assignment
   currently happens one crew member at a time
   (assign_crew_to_duty/assign_crew_to_new_flights each take a single
   crew_id). A pair-level rule needs a point where BOTH pilots for a
   rotation are known at once — likely a check that runs after each
   individual assignment, evaluating whether a complete (CPT+FO)
   pairing now exists for that duty/rotation and validating it if so.
   This needs real design before it's built, not just the rule logic
   itself. crew.date_of_birth is also currently nullable with no
   constraint forcing it for CPT/FO specifically — worth deciding
   whether to enforce that at the schema level or catch it via
   NEEDS_MANUAL_REVIEW at assignment time (leaning toward the latter,
   consistent with "never silently block on missing data, flag it
   instead" — but not yet decided).
8. THEN revisit Phase 7. **Groundwork started 2026-08-04** — see the
   dedicated log entry below: recurring schedule templates (the
   template layer only — `rotation_templates`/`rotation_template_legs`/
   `rotation_instances`/`rotation_instance_legs`, migrations/011) so
   the generator has DECLARED rotation membership to work from instead
   of inferring it from Flight Log. **Approval workflow started
   2026-08-04** (branch `rotation-instance-approval-workflow`, not yet
   merged) — `approve_instance()`/`reject_instance()`, promoting a
   DRAFT instance's legs into real `flights` rows via the existing
   `flight_service.add_flight()`. The generator itself (whatever's
   left to actually decide WHICH crew fills each promoted flight) is
   still not built, and neither is any UI. Also: the 2026-07-19
   OR-Tools CP-SAT decision is REVERSED — see that entry too. Do not
   resume Phase 7 assuming CP-SAT is still the plan.

Lower-priority findings, not yet sequenced: airport timezones not
passed to the validator (every sector defaults to UTC+5 regardless
of actual destination — directly relevant given DWC is in the real
route network); standby/reserve/positioning duty types never reach
the legality engine (duty_type is hardcoded to FDP always — CONFIRMED
by the full requirements document that Air Eagle has no standby/
reserve arrangement at all, so this is lower priority than it looked
initially, not zero priority since positioning is explicitly
permitted); configs/airlines/AEAGLE/ still empty; audit records have
no real app_user or transaction_id (ties to both the no-auth gap and
the non-atomic-transaction gap); test depth for pcaa_ano012_core.py
is thin relative to its size and safety-criticality (12 tests for
1,293 lines, deliberately scoped rather than exhaustive — boundary-
value tests, one minute under/at/over each limit, would be a real
improvement whenever this file gets touched again); WhatsApp
notification + acknowledgment workflow (confirmed requirement, not
built); universal Excel export with the specific filename format
`AirEagle_[PageName]_DD-MM-YYYY_HHMMUTC.xlsx` (confirmed requirement,
not built) — both of these are real scope, not yet prioritized
against the legality-gate work above.

## Do not change without discussion
- migrations/000_migration_tracking.sql — once applied anywhere,
  treat as immutable; write a new numbered migration instead
  (scripts/run_migrations.py will warn, not silently allow, if this
  rule is violated)
- The directory structure itself (core/ / services/ / configs/
  split) — this implements the Ownership Table from Project
  Instructions directly; deviating from it reopens the SSOT
  ambiguity that caused the original crew-table conflict
- core/legality/pcaa_ano012_core.py — reviewed in full and tested,
  don't modify rule logic without adding/updating the corresponding
  test in the same change. This file is the actual legality
  authority; silent edits here are exactly the failure mode the
  whole rebuild was meant to prevent.
- migrations/001_crew_table.sql, 002_flights_table.sql,
  003_roster_table.sql — once applied anywhere, immutable like
  000_. Need a new column, e.g. for Engr/LM quals once confirmed?
  New numbered migration (004_...). Never edit these three in place.
- migrations/004_audit_log.sql — same rule, immutable once applied.
- services/crew_service.py — crew_id generation logic
  (_generate_crew_id) and the UPDATABLE_FIELDS allowlist are both
  load-bearing for data integrity (the allowlist is what prevents
  building an unsafe dynamic UPDATE from arbitrary keys). Don't
  loosen either without adding a test for whatever case motivated
  the change.
- core/duty_builder.py — build_duty() and recompute_fdp_after_delay()
  are deliberately separate functions, not one function reused for
  both cases. Do not merge them "for simplicity" — that merge is
  exactly how the historical block-time bug would come back. If a
  future change seems to need them merged, that's a signal to
  re-read the comments in this file first, not a green light.
- migrations/005_roster_partial_unique_index.sql,
  006_flights_domestic_column.sql — same immutability rule as every
  other applied migration.
- services/assignment_service.py — _validate_new_duty() is the single
  validation core for BOTH assign_crew_to_duty() and
  assign_crew_to_new_flights(). Do not let a future change duplicate
  this logic into one of the two callers "just for this one case" —
  that reopens exactly the two-sources-of-truth failure mode this
  whole rebuild exists to prevent. If Roster and Control Room ever
  need to validate differently, that's a sign the shared function
  needs a parameter, not a fork.
- services/assignment_service.py — _check_downstream_impact() had a
  real, silent bug (see Recently Completed, Phase 6) where it never
  actually included the future duty being assessed in its
  before/after comparison. Any future change to this function needs
  a test that asserts a SPECIFIC expected conflict with real numbers
  — a test that only checks "doesn't crash" would not have caught
  that bug and won't catch the next version of it either.
- scripts/import_crew_from_xlsx.py — KNOWN_CORRECTIONS is reviewed,
  human-confirmed, row-keyed data for ONE specific data drop. Clear
  it before the next batch (see the comment above the dict) — it
  matches by row number alone, no cross-check against which file.
  This exact collision happened in the test suite during development
  (synthetic test rows also start at row 3) before being isolated
  with an autouse fixture in tests/test_import_crew_script.py.
- services/assignment_service.py — FTL_EXEMPT_ROLES (LM, ENGR) is a
  role-based classification confirmed directly by the user
  (2026-07-21: "Engr is AME... No FTL applicable... same on LM"),
  not a general "these roles are less important" assumption. Don't
  extend this set without an equally explicit confirmation — the
  same operational fact needs to hold for any role added here.
- migrations/008_drop_type_rating_and_contract_expiry.sql — same
  immutability rule as every other applied migration. Don't
  re-add type_rating_expiry/contract_expiry as columns, and don't
  re-add either to QUALIFICATION_EXPIRY_FIELDS, without an equally
  explicit decision — see the 2026-08-01 log entry for why they were
  removed (both empty for every real crew row, holding every real
  crew member for review indefinitely).
- migrations/009_roster_needs_review_status.sql — same immutability
  rule as every other applied migration. Originally authored as 008
  in parallel with the migration above; renumbered to 009 after
  `remove-type-rating-contract-fields` merged first — see "Current
  active task."
- services/assignment_service.py — LOOKBACK_DAYS must stay wide
  enough to cover D9.2.3 (365-day/1000h cumulative flight time),
  the widest window any rule in core/legality/pcaa_ano012_core.py
  actually checks. Don't narrow it back down for a performance
  concern without confirming Air Eagle's crew pool has actually grown
  enough to make that a real cost — narrowing it silently
  reintroduces the exact bug this fixed (2026-08-01).
- services/assignment_service.py — cancel_flight_and_roster() and
  update_flight_actual_times_and_revalidate() are the only sanctioned
  way to cancel a flight or record actual times once crew may be
  assigned to it. Don't call flight_service.cancel_flight()/
  update_flight() directly from a page or script for these cases —
  that bypasses the roster cascade / delay-revalidation these exist
  specifically to guarantee.
- services/alert_summary.py — summarize_alerts()'s blocked_by_history_only
  gate requires ALL of target_duty_alerts, qualification_alerts, AND
  schedule_level_alerts to be zero-ILLEGAL, not just target_duty_alerts.
  This is a deliberate, explicitly-confirmed conservative choice
  (2026-08-01) — narrowing it to only check target_duty_alerts would
  reopen the exact case this was built to prevent (an expired
  medical, or a 7th-consecutive-duty-day, reported as "blocked by
  pre-existing history, this duty is not the cause" — backwards).
  Don't add a 5th bucket without checking whether it also needs to
  gate this field.
- core/legality/pcaa_ano012_core.py — any new `_check_*` method that
  builds a RuleAlert without passing `duty_id` will be classified by
  services/alert_summary.py's summarize_alerts() as either a
  qualification alert (if rule_code starts with "AE-CREW-QUAL-001")
  or a schedule-level alert (everything else with duty_id=None) —
  there is no third silent category. If a future rule genuinely needs
  its own bucket, that's a deliberate change to summarize_alerts(),
  not something to work around by giving it a fake duty_id.
- scripts/check_reachability.py — reachability is decided by EXACT
  module-path match only (`imp == mod` or `imp.startswith(mod + ".")`),
  never `mod.startswith(imp + ".")`. That direction used to let a bare
  `from services import crew_service, ...`-style capture silently
  vouch for every file under `services/`, at any depth, regardless of
  whether it was actually named anywhere — see the 2026-08-01 log
  entry for the real bug this was. Don't reintroduce it "to handle
  package-level imports" — `find_all_imports()`'s `X` + `X.a`/`X.b`/
  `X.c` candidate expansion already covers every legitimate case that
  direction was ever needed for. Also: `read_text()` calls in this
  file and scripts/run_migrations.py must keep `encoding="utf-8"`
  explicit — every page and every migration file contains real
  non-ASCII characters (em dashes), and the OS-default codec (cp1252
  on Windows) silently mis-decodes them without raising, not a
  hypothetical risk.
- **A TEST FOR THE `st.rerun()` SWALLOW MAY NOT ASSERT THAT A MESSAGE
  MERELY EXISTS. It must assert WHERE the message rendered** — above
  the control that produced it (rule added 2026-09-06; it invalidates
  a whole style of assertion, so it is stated here rather than left
  inside a dated entry).

  `st.rerun()` runs the script twice inside one `at.run()`, and the
  discarded first pass **survives in the element tree wherever the
  second render is shorter** — which on every page carrying this fix it
  is, because the form that wrote the message collapses after a
  successful submit. So `any("Swap alert" in e.value for e in
  at.error)` is TRUE whether the message was queued or thrown away. A
  presence assertion cannot distinguish the two states it exists to
  distinguish.

  Only a QUEUED message can render above the control, because the drain
  runs at the top of the page. That is the property to assert, and the
  helper for it (`_queued(at, marker)`) is in
  `tests/test_control_room_notices.py`,
  `tests/test_schedule_template_notices.py` and
  `tests/test_roster_flight_scope.py`.

  **Verified, not reasoned about.** Mutating `queue_*_notice()` to
  write immediately — exactly the pre-fix behaviour — left all three
  files green while nothing rendered above the control. A NARROWER
  mutation (one call site converted back to a direct write) does fail,
  which is why the weakness was not obvious and why two of these files
  had already been reported as mutation-verified. **A test that catches
  a one-line regression but not a wholesale one is worth knowing about
  rather than trusting**; when mutation-testing a queue, mutate the
  QUEUE, not one of its callers.

  Corollary: "the message is gone from the screen now" is also not a
  question the element tree answers honestly, because AppTest keeps
  stale elements from EARLIER `at.run()` calls too. Assert the queue is
  empty instead — `"..._notices" not in at.session_state`. Note
  `at.session_state.get(key)` raises `KeyError` on a missing key rather
  than returning `None`; the membership test is the one that works.
- **A PAGE IS NOT "AUDITED" BECAUSE ONE SECTION OF IT WAS**
  (2026-09-06). The 2026-09-05 Roster audit converted the
  flagged-for-review section and stopped, leaving both assignment
  handlers and the unassign confirmation writing into the discarded
  run. So the swap alert — *"this assignment breaks the legality of N
  already-scheduled future duty(ies)"* — was found and fixed on Control
  Room on 5 September and **left broken on Roster**, which is the page
  a controller actually crews scheduled flights from: the more
  important of the two.

  The check is `grep -n "st.rerun()" <page>` and then reading EVERY
  hit, not the memory of having looked at the file. Same shape as the
  reboot rule: run the grep, don't recall the conclusion.
- **ADDING REAL DATA TO A LOOKUP CAN SILENTLY RETIRE THE COVERAGE OF
  THE FALLBACK BENEATH IT** (2026-09-06). `crew_seat_name()` consults
  `CREW_DISPLAY_NAMES` first and falls back to the
  strip-titles/initial-plus-surname rule.
  `test_a_title_stored_inside_the_name_is_not_taken_for_a_given_name`
  measured that stripping on **CPT-06**, the real pilot stored as
  "CAPT MUHAMMAD ASAD ALI". The moment CPT-06 was added to the lookup,
  he stopped reaching the rule at all — and the test **kept passing
  while testing nothing**.

  It now runs against `CPT-99`, a fixture-only id carrying the same
  stored name and deliberately absent from the table. General rule: a
  test for a FALLBACK must use an input that cannot be captured by the
  layer in front of it, and that input must be one the production data
  cannot grow into.

## 2026-07-21: real data arrived — data quality findings, FTL
## exemption, schema reconciliation (unpushed as of this snapshot)

The operator's crew data (AirEagle_Crew_Data_Simple.xlsx) came back.
Findings and fixes, in the order they happened:

- **10 clean CPT/FO rows**, 3 needed a confirmed correction (License
  Exp read 1930, confirmed should be 2030 — recorded in
  scripts/import_crew_from_xlsx.py's KNOWN_CORRECTIONS, not guessed).
  Row 12's operator_staff_id was a placeholder ("-") — left genuinely
  NULL, not stored as literal text.
- **The Loadmaster section (rows 16-19) was misaligned** — filled in
  against a different header row that got pasted into the data area
  instead of adapted to the template's actual columns. Dates ended up
  in Base/Email/License No; the real qualification columns are empty
  for all three. NOT imported — needs redoing by the operator against
  the actual template, not algorithmically realigned (guessing which
  shifted date belongs where is exactly the kind of silent assumption
  that's dangerous here).
- **Two columns (Type Rating Exp, Contract Exp) are empty for every
  single row** — not a per-person gap, systematic. Flagged back, not
  silently left blank without note.
- **scripts/import_crew_from_xlsx.py built as reusable infrastructure**,
  not a one-off — does real validation (symmetric misalignment check:
  a text field should never hold a date, a date field should never
  hold arbitrary text once "-"/empty are normalized) rather than
  trusting the spreadsheet. Found its own real bug during first run:
  date_of_birth was miscategorized as a "text field" (should never be
  a date), which flagged every clean row's real DOB as misaligned —
  fixed, now correctly categorized as a date field with its own
  plausible-year exemption (a birth year like 1960 isn't suspect the
  way an expiry date would be). 11 tests, using synthetic workbooks,
  not the real file. Real import verified end-to-end against local
  Postgres: 10 rows in, all 3 corrections confirmed landed via direct
  SQL query (not the script's own claims) — but see the note below,
  this was NEVER run against the real Supabase DB.
- **migrations/007_crew_columns_reconcile_real_data.sql** — the
  operator's real column names differed from the template: "LPC/OPC
  Exp" -> their "SIM Exp", "Line Check Exp" -> their "Route Check
  Exp" (same concepts, their terminology — renamed, not duplicated,
  since no real data existed yet to make renaming costly), plus a
  genuinely new "IR" (Instrument Rating) column. crew_service.py,
  pages/2_Crew_Data.py, and the import script's HEADER_MAP all
  updated to match.
- **Major architecture finding, confirmed by the user directly**:
  "Engr is AME [line-maintenance, not flight-deck]... No FTL
  applicable... same on LM." This means Loadmasters and Engr are NOT
  subject to ANO-012's FDP/rest rules at all — but
  services/assignment_service.py was running the full legality gate
  for every role, including LM, before this was caught. Fixed:
  FTL_EXEMPT_ROLES constant, checked in _validate_new_duty() (skips
  history-loading and validate_schedule() entirely for exempt roles,
  returns a synthetic LEGAL result), both _check_downstream_impact()
  call sites (no FTL history to protect for an exempt crew member),
  and find_legal_candidates_for_duty() (every active crew member with
  an exempt role is trivially a candidate — no FTL history could
  exclude them). Deliberately implemented in the orchestration layer
  (assignment_service.py), not inside core/legality/pcaa_ano012_core.py
  — the core engine stays role-agnostic; "which roles this applies
  to" is an operational classification, not math. 7 new tests,
  including a guard-rail test confirming the exemption did NOT leak
  to CPT (the identical rest-violation scenario that's ALLOWED for LM
  must still be REJECTED for CPT).
- **A serious trust/process incident, worth recording plainly**: mid-
  session, garbled/duplicated text appeared in a response
  ("Validated data corrections and orchestrated comprehensive testing
  infrastructure" x2) with no clear explanation for how it got there.
  Separately, and more importantly, the user believed — reasonably,
  given how the update was phrased — that the crew import had
  happened against their real Supabase database. It had not. It ran
  against the local disposable test Postgres in the sandbox, the same
  one used for every phase's verification, for a hard, structural
  reason: sandbox network access is allowlisted to a fixed set of
  domains and does not include supabase.co — direct connection to
  the real DB has never been technically possible from here, for any
  phase, ever. This should already have been obvious from the
  established pattern ("I verify locally, then hand you code to run
  yourself") but wasn't stated clearly enough at the moment it
  mattered most. Restated explicitly here so it isn't lost: **nothing
  in this entire project has ever been run against the real Air
  Eagle Supabase database.** Every "verified" claim in every phase of
  this document means "verified against local sandbox Postgres,
  independently re-confirmed via fresh git clone" — never production.
  The user also pasted a Supabase project URL + publishable API key
  directly into chat; flagged as bad practice regardless of that
  specific key's lower sensitivity, key rotation suggested.
- **Real route network received** (via chat, not yet a structured
  file): two recurring pairs — EPE 786/787 (KHI-LHE-KHI, domestic,
  Mon-Fri nightly) and EPE 802/804/805 (KHI-LHE-DWC-KHI,
  international, Tue/Thu/Fri/Sat) — plus two real operated-flight
  examples with actual crew. This resolves the Phase 7 route-network
  blocker, but see the external review findings below — two of them
  directly collide with this exact data (mixed domestic/international
  within one duty; no geographic continuity check) and need fixing
  before this data can actually be used correctly.
- **RESOLVED 2026-08-02: "VAI" is dropped entirely.** The operator
  confirmed it should be read as AME — not a separate term, not a
  meaning this system needs to track. The open question this bullet
  used to record no longer applies; see the 2026-08-02 log entry for
  where "Eng: 2x VAI" actually came from (a real flight example) and
  why it no longer appears anywhere in this codebase after
  `roster_coverage()`'s reshape.
- **Age-65 rule wording CONFIRMED 2026-08-02, still NOT implemented.**
  "At least 01 crew member below 65 yrs... applicable to EPE." Checked
  the actual ANO-012 document (OCR'd the scanned PDF, searched for
  "age"/"65"/"60" across all 32 pages) — confirmed this document
  (titled "FATIGUE MANAGEMENT — FLIGHT AND CABIN CREW") contains NO
  age-eligibility provision at all; this is a licensing restriction,
  not an FTL/fatigue one, and lives in a different PCAA order this
  repo doesn't have. The operator has now confirmed the exact wording
  matches what's already recorded in "Next safest step" item 7 below
  (domestic: illegal only if BOTH pilots are 65+; international:
  illegal if EITHER pilot is 65+) — the regulatory wording is settled.
  Still NOT built: still blocked on the pair-level architecture
  question (assignment happens one crew member at a time today; this
  rule needs to see both pilots on a rotation together).

## 2026-07-21: external code review — verified, mostly correct, two
## findings now urgent given the route data above

An external review of commit 85afb76 (Phase 6) raised 14 findings.
Spot-verified the most severe ones directly against the code (not
taken on trust) — every one checked out exactly as described,
including confirming core/legality/pcaa_ano012_core.py genuinely
does implement a 365-day/1000h cumulative check
(D9.2.3_12_MONTH_FLIGHT_TIME_LIMIT) that has never once been able to
fire correctly, since assignment_service.py only ever loads 35 days
of history before calling it.

One correction to the review itself: it reported "32 passed, 90
skipped" from its own test run — that was its environment missing
TEST_DATABASE_URL (tests skip loudly by design when that's unset,
per conftest.py — not a flaw in the suite). The review's deeper
point stands independent of that though: 12 tests for a 1,293-line
safety-critical engine is genuinely thin, deliberately scoped to
"rules confirmed applicable right now" rather than exhaustive
boundary coverage.

Full findings and agreed remediation order below (Next safest step)
— none of these are fixed yet as of this snapshot. Agreed with the
review's core strategic call: do NOT start Phase 7 (roster generator)
until the gate is actually solid. An OR-Tools generator on top of a
gate with these gaps would efficiently produce a roster that *looks*
legal and isn't — worse than not having the generator at all.

## 2026-07-21 (continued): full requirements document — three direct
## conflicts with earlier confirmed instructions, all resolved

A comprehensive Air Eagle requirements/roadmap document arrived,
overlapping strongly with the external review above (same
remediation sequence, independently), plus substantial genuinely new
operational detail: PCAA Charter Class II cargo classification;
confirmed crew package (1 CPT, 1 FO, 1 LM, 1 AME + configurable
"Other Crew" per rotation); rostering workflow (draft -> OCC review
-> publish, crew sees only published, no silent reshuffle on
regeneration, explicit UNCOVERED state when no legal crew exists);
fairness definition (proportional to actual availability); standby
CONFIRMED not needed (validates an earlier decision made with less
information); Flight Log field list (matches what's already built);
WhatsApp notification requirement (not yet built); Excel export
requirement with a specific filename format (not yet built).

This document also directly contradicted three things the user had
told me directly, earlier in this same conversation. Flagged rather
than silently resolved either way — silently picking a number for
either of the first two would have meant building the wrong
safety-critical rule with full test coverage backing up the wrong
answer. All three now resolved by the user directly:

1. **Roster horizon: CONFIRMED 28 days**, not the document's 15.
   No code impact yet — Phase 7 (roster generator) is still paused.
2. **Age threshold: CONFIRMED "at least 1 crew member below 65"**,
   not the document's "both >=67". Still not implemented (see below
   — a second review arrived with a specific proposed rule shape
   before this got built, so it's now well-specified but still not
   coded as of this snapshot).
3. **D7.1.2 buffers: CONFIRMED as already correctly built** —
   45/15 domestic, 60/30 international, exactly what
   core/duty_builder.py already implements and tests. No change
   needed; this was the document's least accurate claim (a flat
   60/30 for everything), and it's the one already independently
   verified against the actual ANO-012 PDF text back in Phase 6's
   planning.

## 2026-07-21 (continued): second review — real bugs in TODAY's
## commit found and fixed before the day's work was even pushed once

A second, more detailed review (same overall direction as the first)
specifically audited commit 32098e7 — today's crew-data-reconciliation
commit — and found real, confirmed problems in it, not just the
broader pre-existing gap list. All verified directly against the
code before fixing (not taken on trust), all now fixed and tested:

- **openpyxl missing from requirements.txt** — the crew import
  script depends on it but it was only ever installed ad-hoc in the
  sandbox, never tracked. Fixed: added to requirements.txt.
- **FTL exemption was case-sensitive, and didn't recognize "AME" as
  a synonym for "ENGR"** — confirmed directly: FTL_EXEMPT_ROLES =
  {"LM", "ENGR"} did exact-case matching only. A role stored as
  "Engr" (any case) or "AME" (the user's own real-world term for
  the same role) would silently fail to match, wrongly subjecting an
  actually-exempt person to FDP/rest math that doesn't apply to
  them. Fixed at the write boundary, not scattered comparison sites:
  services/crew_service.py now has ROLE_SYNONYMS = {"AME": "ENGR"}
  and _normalize_role(), applied in both add_crew() and update_crew()
  — so crew.role is always canonical (uppercase, synonyms resolved)
  from the moment it's stored, and every downstream consumer
  (FTL exemption check, crew_id prefix generation, role matching)
  can trust that without repeating the normalization logic.
- **Critical, confirmed, real bypass: role_assigned was never
  cross-checked against the crew member's actual registered role.**
  An ENGR (FTL-exempt, correctly decided from crew_row["role"]) could
  be assigned with role_assigned="CPT" — retaining the FTL exemption
  while being recorded as filling the Captain role, with zero
  FDP/rest checking ever applied to a "Captain" assignment. Fixed:
  _validate_new_duty() (the shared validation core for both Roster
  and Control Room paths) now rejects with a ValueError if
  role_assigned (normalized, synonym-aware) doesn't match
  crew_row["role"]. Air Eagle's confirmed crew model (exactly one
  fixed role per rotation slot: 1 CPT, 1 FO, 1 LM, 1 AME) has no
  legitimate case where these should ever differ.
- **KNOWN_CORRECTIONS matched by row number alone** — a stale entry
  could silently misapply to an unrelated future row reusing the
  same row number (this exact collision already happened once, in
  the test suite, before being isolated — see the earlier entry).
  Hardened: corrections are now {"expected_name": ..., "value": ...}
  dicts, and a correction only applies if the row's actual Name
  matches who it was reviewed for. A row-number match with a
  mismatched name now falls through to normal suspect-date handling
  instead of silently applying someone else's correction — verified
  by a dedicated test.
- **Mixed domestic/international duty was rejected outright** — this
  was already known (Step 3 of the original remediation order) but
  the second review gave a specific, clean, adopted design: any
  international sector makes the WHOLE DUTY use the international
  (60/30) buffer; each flight independently keeps its own domestic
  flag for Flight Log/reporting. This directly unblocks the real
  KHI-LHE-DWC-KHI rotation, which mixes a domestic-classified KHI-LHE
  sector with international LHE-DWC/DWC-KHI sectors within one duty.
  Fixed in both assign_crew_to_duty(), assign_crew_to_new_flights(),
  and find_legal_candidates_for_duty() (which had the same bug in a
  different form — only checked the FIRST flight's domestic flag).
- **No geographic continuity check** — build_duty() only validated
  temporal ordering, never that leg N's destination equals leg N+1's
  origin. Added alongside the existing temporal check in
  core/duty_builder.py — a crew member can't physically be in two
  places at once, and nothing previously caught a duty built from
  disconnected flights.
- **Minor audit-accuracy fix while touching this code**: the
  rule_applied audit label used a ternary implying D21 (rest) and
  D8/D9 (FDP) were alternatives selected by domestic/international —
  they're not, both always apply simultaneously. Changed to
  accurately describe which D7.1.2 buffer was used, not which "rule"
  was "applied" as if there were only one.

13 new tests covering all of the above (153/153 total), including
direct tests of the actual bypass scenario (ENGR assigned as CPT is
now rejected) and the actual mixed-rotation scenario (a real
KHI-LHE-DWC-KHI-shaped 3-leg duty is now accepted with the correct
international buffer applied, verified against the exact
report/debrief timestamps, not just "doesn't raise").

Two of today's own test-writing mistakes are worth recording plainly,
not glossing over: a str_replace edit accidentally deleted a test's
body (caught immediately by syntax check, restored), and two
existing tests had to be rewritten because their whole premise
(mixed-domestic rejection) was the bug being fixed — both are normal
parts of doing real work, not incidents, but the discipline of
running the suite after every change is what caught both immediately
rather than either shipping silently broken.

## 2026-07-2x: first real attempt to connect to Supabase — a genuine
## systemic bug found and fixed, not yet fully resolved on the user's end

The user set up a real Supabase project (`bdpfkftgzsjqykkitmgx`),
walked through finding the Session/Direct connection string
(Supabase's dashboard now puts this behind a "Connect" button, not
Project Settings > Database as earlier guidance assumed — corrected
mid-conversation), and edited `.env` — with one credential-hygiene
incident along the way: a real database password briefly appeared in
a terminal paste shared in chat. Treated as compromised at the time;
the user's explicit, informed choice was to keep the same password
rather than rotate it. Worth knowing if anything about that DB looks
wrong later — the password has been visible in this conversation's
history since.

Running `python scripts/run_migrations.py --status` against the
freshly-configured Supabase connection first appeared to work — it
reported "Applied: 0, Pending: 8," matching the Supabase dashboard's
own "No migrations" indicator. Running it for real applied 000 and
001 successfully, then failed on 002 with `UndefinedColumn: column
"dep_time_planned" does not exist` — implying a pre-existing
`flights` table with different, incompatible columns. But checking
Supabase's SQL Editor directly showed the `public` schema
completely empty — not even `crew`, which had just been reported as
successfully applied.

**Root cause, found by checking actual code rather than guessing
further: `scripts/run_migrations.py` never called `load_dotenv()` at
all** — it only ever read a genuine shell/system environment
variable named `DATABASE_URL`, completely ignoring `.env`'s
contents, with no warning of any kind. The user's shell had exactly
such a variable already set, left over from earlier work, pointing
to a **Neon** database (`ep-summer-haze-a1e19z0g-pooler.ap-southeast-1
.aws.neon.tech`) — not Supabase at all. Every "Supabase" migration
run had silently been hitting that old Neon database instead, which
already had its own (incompatible) `flights` table from earlier
work — explaining the exact contradiction observed.

**This is not confined to one script.** `db/db.py` — the single
connection owner used by every page, every service, everything —
had the same class of bug in a subtler form: it *did* call
`load_dotenv()`, but without `override=True`, which means
python-dotenv leaves a pre-existing environment variable untouched
and silently never applies `.env` at all when one already exists.
The entire application has been vulnerable to this exact silent
shadowing whenever run in an environment with any stray
`DATABASE_URL` already set — not just this one diagnostic script.

**Fixed at the root, both files**: `db/db.py` and
`scripts/run_migrations.py` now both call `load_dotenv(override=True)`
— the project's own `.env` always wins over anything already present
in the shell. Confirmed via a systematic repo-wide grep that these
are the only two files reading `DATABASE_URL` directly; everything
else goes through `db.db.get_engine()`, so this one fix covers the
whole application. Two new regression tests
(tests/test_env_override.py, 155/155 total) prove both the fix
(`.env` wins with `override=True`) and demonstrate the bug directly
(without it, the stale value silently wins and `.env` is ignored) —
so if either `load_dotenv(override=True)` call is ever "simplified"
back to a bare `load_dotenv()`, this fails loudly instead of quietly
reintroducing the exact thing that already cost real debugging time
once.

**Not yet resolved on the user's end as of this snapshot**: the fix
exists in this sandbox, verified, but hasn't been pushed or copied
into the user's actual local checkout yet. Once it is: the user still
needs to either `unset DATABASE_URL` in their shell before running
anything (temporary, per-session), or — better — find and remove
whatever set that Neon URL persistently (Windows System/User
Environment Variables, or a shell profile script) so it doesn't
shadow future work in this or any other project. Also still
unconfirmed: whether the Neon `flights` table conflict was ever
actually resolved, or whether the user's Supabase database (which
*was* confirmed genuinely empty, "No migrations," before any of
this) has now actually received all 8 migrations for real. This
needs to be re-attempted, correctly, once the fix is in the user's
hands.

## RESOLVED: the Supabase connection actually worked, for real, for
## the first time — plus two more real bugs found in the process

Continuing directly from the above: after `unset DATABASE_URL`, the
migration status check correctly showed the real Supabase database's
true state (`Applied: 0, Pending: 8`, matching the dashboard). But
`python scripts/run_migrations.py` (the real apply) then hung/failed
with `could not translate host name "db.xxx.supabase.co" to address`
— a second, genuinely different problem: Supabase's Direct connection
hostname is IPv6-only by default, and the user's network couldn't
resolve it at all (confirmed via `nslookup` returning only an IPv6
address, no IPv4). This is exactly the risk flagged back when the
connection string was first chosen, but not actually acted on at the
time. Fixed by switching to Supabase's **Session pooler** connection
string instead (IPv4-proxied by design) — confirmed via `nslookup`
returning real IPv4 addresses, then confirmed by actually running the
migrations: **all 8 applied successfully, `Applied: 8, Pending: 0`,
independently re-confirmed via Supabase's own dashboard.** This is
the first time in this entire project that anything has been
genuinely verified against the real production database, not a local
sandbox stand-in.

Two more real, confirmed bugs found immediately after, both fixed:

- **Role synonym gap**: the operator's actual crew data file spells
  the role `"CAPT"`, not `"CPT"`. Without a synonym mapping, these
  captains would have gotten generated IDs like `CREW-01` instead of
  `CPT-01`. Fixed the same way `AME`->`ENGR` was fixed — added to
  `ROLE_SYNONYMS` in `services/crew_service.py`, one canonical
  mechanism, not a new one-off special case.
- **A second, unrelated hang, same root shape as the Supabase DNS
  issue but in the test suite**: running `pytest` locally hung
  indefinitely. Cause: `.env.example`'s `TEST_DATABASE_URL` used a
  well-formed-looking placeholder string
  (`postgresql://user:password@host:5432/dbname_test`). That string
  *looks* unset to a human but is non-empty and therefore truthy to
  Python — `tests/conftest.py`'s `if not test_url: pytest.skip(...)`
  never triggers on it, so it tries to actually connect to a literal
  host named `host`, which hangs. This had already been diagnosed in
  an earlier session as a known gap and described as "worth fixing
  properly later" — it then went on to block the same user a second
  time before actually being fixed. Lesson: when a real gap is found,
  fix it immediately, don't defer a known problem hoping it won't
  recur. Fixed: `.env.example`'s `TEST_DATABASE_URL` is now genuinely
  empty, not a placeholder — `conftest.py`'s existing skip logic was
  already correct, the bug was entirely in what a fresh `.env` copy
  contained. 4 new tests total across these two fixes (158/158),
  including one that demonstrates the exact truthy-placeholder
  mechanism directly (`bool(placeholder) is True`) rather than just
  asserting the fixed behavior.

**Real crew data update received from the operator**
(`AirEagle_Crew_Data.xlsx`, distinct from the earlier
`AirEagle_Crew_Data_Simple.xlsx`): the three 1930-dated License Exps
are now genuinely corrected at the source (2030), and row 12's
missing ID is now populated (`AE-153`) — both match exactly what was
manually corrected before, confirming the operator did fix those.
**The Loadmaster section (rows 17-19) is still exactly as misaligned
as before** — this update touched only the pilot rows, not that
section. Not yet imported anywhere; still needs the loadmaster
section redone by the operator before any import happens.

**Local environment cleanup, not yet done**: multiple redundant
local folders exist from this whole zip-based delivery process
(`K2`, `air_eagle_check`, various `air_eagle_pushN` staging folders)
plus the original `k2` GitHub repo, all superseded by `Air-Eagle-live`
and the `Air-Eagle` GitHub repo. User asked about deleting these;
correctly paused pending an actual `ls` of what exists, specifically
flagging `occ_roster` as needing confirmation before any deletion —
that name matches the *original, deliberately separate* prototype
project per this platform's own project structure, not necessarily a
redundant Air Eagle copy. Not yet resolved as of this snapshot.

**RESOLVED**: the above was pushed as commit `727da58`, independently
re-cloned and re-verified (14/14 files byte-identical, 158/158 tests
with real Postgres, 50/108 pass/skip with TEST_DATABASE_URL genuinely
unset confirming no hang, secrets check clean). This closed out a
large amount of accumulated, previously-unpushed work.

## Step 2 done: `NEEDS_MANUAL_REVIEW` no longer silently treated as
## ALLOWED — the smallest, most decoupled item in the remediation
## order, now fixed, with a much larger ripple effect than expected

The fix itself: `assign_crew_to_duty()` and `assign_crew_to_new_flights()`
previously only branched on `AlertStatus.ILLEGAL` — everything else
(LEGAL, WARNING, **and NEEDS_MANUAL_REVIEW**) fell through to the
same write path and was reported as `status="ALLOWED"`. Added a
dedicated branch: `NEEDS_MANUAL_REVIEW` now returns
`status="NEEDS_REVIEW"`, writes nothing (same as ILLEGAL in that
respect), and logs a distinct audit action type
(`ASSIGNMENT_HELD_FOR_REVIEW` / `ADHOC_FLIGHT_HELD_FOR_REVIEW`) — not
a rejection, since it isn't a known violation, just an unresolved
uncertainty needing a human decision. `AssignmentResult` gained
`computed_report_time`/`computed_debrief_time`/`computed_fdp_hours`,
populated regardless of status, so a human reviewing a held
assignment can still see what was computed even though nothing saved.

**The real discovery**: this codebase never populates
`meal_provided`/`snack_provided` on any Duty it builds, and
`core/legality/pcaa_ano012_core.py`'s D25 rule fires
`NEEDS_MANUAL_REVIEW` for any FDP over 6h with `meal_provided is
None` (its actual default). This means **every real assignment
attempt over 6h FDP has ALWAYS been secretly returning
NEEDS_MANUAL_REVIEW**, silently auto-allowed by the bug just fixed.
12 existing tests broke as a direct, correct consequence — every one
had used an 8h "prior duty" as an assumed-written precondition for
something else under test (D21 rest-conflict scenarios, downstream
candidate exclusion). Fixed two ways depending on what each test
actually needed:
- Tests that only needed a >6h duty to establish REST history (the
  D21 "scales above 12h floor" case specifically requires >6h FDP to
  be meaningful): switched to a new `_seed_duty()` test helper that
  inserts a roster row directly via SQL, bypassing the assignment
  API entirely for that "given" precondition — mirrors the existing
  raw-SQL seeding pattern already used in test_schema.py.
- Tests where the >6h duty wasn't actually essential to the point
  being tested (most downstream-conflict tests only needed the 12h
  rest FLOOR, which applies to duties of any length): shortened to
  5h FDP, preserving the same conflict-triggering gap to the future
  duty with the same numbers, avoiding the nutrition trigger entirely
  rather than working around it.
5 new dedicated tests added for the fix itself (not just the ripple
repairs) — confirming a real 7h duty genuinely returns NEEDS_REVIEW,
writes nothing, logs the right audit action type, and that this
extends correctly to the Control Room path too.

**A second, real bug found and fixed while finishing this**: neither
`pages/4_Roster.py` nor `pages/1_Control_Room.py` had a branch for
this new third status — both only checked `if status == "REJECTED"`
with an implicit "else = ALLOWED." A held assignment would have
fallen into that else branch and displayed as a success message. In
Control Room specifically this would have been worse than
cosmetically wrong: that branch references `flight_ids[0]`, but
`flight_ids` is genuinely empty for a held assignment — a real
`IndexError`, not caught until a dedicated AppTest was added
specifically to exercise this path (`test_needs_review_adhoc_assignment
_shows_warning_not_success`), which confirmed the crash would have
happened before the fix and doesn't after. Both pages now show a
distinct `st.warning` for `NEEDS_REVIEW`, including the computed (but
unsaved) duty times where available.

164/164 total. Reachability unchanged (`core/duty_summary.py` still
the only flag, as expected). Verified locally; not yet pushed as of
this snapshot.

## Step 4 done: crew qualification gate (AE-CREW-QUAL-001) — is_active
## + document validity, checked against the duty's own debrief date,
## closes the single biggest remaining gap in the assignment API

The gap this closes: `_crew_member()` previously passed only
crew_id/name/home_base into the legality engine — nothing checked
`is_active`, or whether license/medical/type-rating/SIM/route-check/
IR/SEP/CRM/DG were current. A deactivated captain, or one with an
expired medical, could be assigned through the service API with zero
checking. Role match (Step 2's earlier work) was already enforced;
this was the rest of the gap flagged in "Next safest step" item 4.

**The fix**: a new `_check_crew_qualifications(crew_row, duty_date)`
in `services/assignment_service.py`, orchestration-layer like
`FTL_EXEMPT_ROLES` — the core engine
(`core/legality/pcaa_ano012_core.py`) stays qualification-agnostic;
this is an Air Eagle operating decision, not FTL math. Every finding
gets its own rule code under the `AE-CREW-QUAL-001` family
(`AE-CREW-QUAL-001_INACTIVE_CREW`, `AE-CREW-QUAL-001_<FIELD>_EXPIRED`,
`AE-CREW-QUAL-001_<FIELD>_EXPIRY_MISSING`) — expired documents or
`is_active=False` are ILLEGAL, a missing/NULL expiry date is
NEEDS_MANUAL_REVIEW (never a silent pass, never a silent reject on
absent data). Every failing reason is collected, not just the first —
this file already documents first-failure-only evaluation as a real
bug elsewhere (`_check_downstream_impact`'s original before/after
comparison), not a hypothetical concern here. Folded into the
existing `ValidationResult` via `add_alert()`, so the
NEEDS_MANUAL_REVIEW/ILLEGAL branches Step 2 already built handle this
with zero new branching logic in `assign_crew_to_duty()` /
`assign_crew_to_new_flights()`.

**Fields checked** (8): license_expiry, medical_expiry, sim_expiry,
route_check_expiry, ir_expiry, sep_expiry, crm_expiry, dg_expiry —
plus `is_active`. [Corrected 2026-08-12: this originally said 9 and
included `type_rating_expiry`, accurate at the time this entry was
written but stale ever since `type_rating_expiry` was dropped from the
gate — see the 2026-08-01 entry below. Every other mention in this
file already said 8; this was the one stale holdout, found and fixed
while researching the flight-deck crew package piece.]
`contract_expiry` is deliberately excluded: an employment/HR date,
not a flight-safety qualification. This is an ASSUMPTION, not an
operator-confirmed decision, and should be revisited if Air Eagle's
actual policy ties contract status to flight eligibility — don't
silently start checking it without that being an explicit decision,
same discipline as `FTL_EXEMPT_ROLES`.

**Boundary convention, deliberately stricter than common aviation
"valid through" practice**: a document is invalid ON its own expiry
date (`expiry_date <= duty_date` is ILLEGAL), not valid through it.
This was an explicit decision made when this gate was designed, not a
default — revisit if Air Eagle's actual regulatory documents specify
"valid through" instead.

**Checked against the duty's debrief (end) date, not its report
(start) date — a real correction made after the first real-Postgres
run**: the first implementation checked
`duty_result.report_time.date()`. Wrong for any duty crossing
midnight — Air Eagle's real EPE 786/787 rotation (KHI-LHE-KHI,
domestic, Mon-Fri nightly) reports 18:15 and debriefs 00:00 the
following day; a document expiring on the debrief date would have
incorrectly passed if only the report date were checked, since the
crew member would already be unqualified before the duty was actually
over. Fixed at both call sites (`_validate_new_duty()` and
`find_legal_candidates_for_duty()`); a regression test using these
exact EPE 786/787 timings now asserts the debrief-date boundary
directly.

**Applies to every role, including LM/ENGR**: `FTL_EXEMPT_ROLES` only
ever exempted FDP/rest MATH — it says nothing about whether the
person holds valid documents to be on the roster at all. The
qualification check is NOT gated on `FTL_EXEMPT_ROLES`; a dedicated
guard-rail test confirms the exemption doesn't leak (an ENGR with an
expired license is still REJECTED, same as a CPT would be).

**Second code path closed**: `find_legal_candidates_for_duty()` runs
its own FDP/rest simulation independent of `_validate_new_duty()` —
without wiring the same check in there too, a deactivated or
expired-document crew member could still have been suggested as a
downstream-swap candidate. Both its FTL-exempt trivial branch and its
main per-candidate simulation loop now run the same qualification
check before including anyone in the candidate list.

**Real-data consequence, confirmed against the operator's actual crew
file**: `type_rating_expiry` ("Type Rating Exp") is empty for every
single row in the real data received so far (a systematic gap already
noted earlier in this file, not a per-person one) — every real crew
member imported to date will correctly hold for NEEDS_MANUAL_REVIEW
on this field specifically until the operator supplies it. This is
the gate working as designed (missing data is flagged, not silently
passed or silently rejected), not a new bug — but it means the
qualification gate will visibly block real assignments the moment
real crew are actually used, not just a theoretical future concern.

**Test coverage**: 14 dedicated tests in `test_assignment_service.py`
(expired license/medical/type-rating, missing-date, inactive crew,
multiple-failures-all-reported, duty-date-vs-today's-date,
exact-boundary, debrief-vs-report-date using the real EPE 786/787
timings, ENGR/LM still-subject guard-rail, candidate-search exclusion
on both branches, Control-Room-path parity), plus qualification
defaults added to the shared test-crew helpers in
`test_assignment_service.py`, `test_control_room_page.py`, and
`test_roster_page.py` so the pre-existing tests keep testing FDP/
rest/role logic rather than tripping the new gate. Also fixed in
passing: `test_role_match_recognizes_ame_engr_synonym` was calling
`crew_service.add_crew()` directly, bypassing the qualification
defaults, so it correctly (if confusingly) started failing the moment
the gate went in — switched to the shared `_add_crew()` helper.

177/177 independently verified against real Postgres 16 (not just
sandbox collection — see "Tests passed"); one further test
(`type_rating_expiry`) added after that run, not yet independently
re-verified. Built and iterated on branch `qualification-gate`
(commits `87044d4`, `45252da`, `b5d9c05`), merged into `main` after
the verification above.

## 2026-08-01: type_rating_expiry and contract_expiry removed from
## the qualification gate AND the crew schema entirely — a real-data
## consequence of the gate working correctly, reversed by user decision

Direct consequence of the previous entry, spotted immediately on
review: with `type_rating_expiry` added to `QUALIFICATION_EXPIRY_FIELDS`,
every real crew member in the operator's spreadsheet would hold at
`NEEDS_MANUAL_REVIEW` — because `type_rating_expiry` and
`contract_expiry` are empty for every single row (already noted in
the 2026-07-21 data-quality findings above, not a new discovery). The
gate was working exactly as designed — missing data flagged, not
silently passed — but the practical effect was that the system
couldn't assign anyone until the operator supplied two columns they
show no sign of tracking.

**User decision**: remove both fields from the qualification gate AND
the crew data model entirely, rather than wait on data that may never
arrive. Explicit reasoning given: assume OCC has already done its job
and removed any crew member who isn't actually qualified from the
pool being worked with — this system doesn't need to independently
re-derive that from two fields with no real data behind them.

**What changed**:
- `migrations/008_drop_type_rating_and_contract_expiry.sql` (new) —
  `ALTER TABLE crew DROP COLUMN` for both. Confirmed empty for every
  real row imported to date, so no data loss of consequence.
- `services/assignment_service.py` — `type_rating_expiry` removed
  from `QUALIFICATION_EXPIRY_FIELDS` (back to the original 8:
  license/medical/SIM/route-check/IR/SEP/CRM/DG). `contract_expiry`
  was never in the gate in the first place (excluded from the start
  as an HR/employment field, not a flight-safety qualification) —
  both are now absent for the same underlying reason: no column,
  nothing to check.
- `services/crew_service.py` — both removed from `UPDATABLE_FIELDS`.
- `scripts/import_crew_from_xlsx.py` — both removed from `HEADER_MAP`
  and `DATE_FIELDS`. A future workbook still carrying "Type Rating
  Exp"/"Contract Exp" columns will simply have them ignored
  (`HEADER_MAP.get()` returns `None` for unmapped headers), not
  misfiled into some other field.
- `pages/2_Crew_Data.py` — the two `st.date_input` widgets and their
  corresponding `add_crew()` dict entries removed from the add-crew
  form. The edit-crew form never referenced either field.
- `tests/test_schema.py` — `test_crew_table_has_all_template_columns`'s
  expected set updated; new
  `test_type_rating_and_contract_expiry_columns_removed` added,
  mirroring the existing `test_crew_table_old_column_names_are_gone`
  pattern from migration 007's rename — confirms the drop actually
  applied, not just that the migration ran without a SQL error.
- `tests/test_assignment_service.py` — `type_rating_expiry` removed
  from the shared `_QUALIFICATION_DEFAULTS`; the dedicated
  `test_expired_type_rating_is_illegal_and_blocks_save` deleted (the
  field it tested no longer exists to set). Net test count unchanged
  (one removed, one added in test_schema.py).
- `tests/test_control_room_page.py`, `tests/test_roster_page.py` —
  same default-field removal in their inline crew-seeding dicts.

**Verification status**: built and reasoned through in an environment
with no reachable database at all (no TEST_DATABASE_URL, no local
Postgres, no Docker) — collection and non-DB tests passed locally at
the time. **RESOLVED 2026-08-01 (same day, following verification)**:
the user independently verified this branch against real Postgres
16 — 178/178 passing, migration 008 confirmed to drop both columns
cleanly against a database already carrying crew data (000-007
applied), no data loss. Merged into `main`.

## 2026-08-01: Step 5 — the three "stale data" findings — done,
## planned before implementation, NOT yet verified against a real
## database

Branch `step5-stale-data-fixes`, forked from `main` (does not include
the separate, also-unmerged `remove-type-rating-contract-fields`
work — see "Current active task"). Design discussed and confirmed
with the user before writing any code, same discipline as the
qualification gate.

**Finding 1 — LOOKBACK_DAYS=35 starving D9.2.3.** `_check_cumulative_
limits()` in `core/legality/pcaa_ano012_core.py` already correctly
windows 7/14/28/30/365-day sums from whatever duty list it's handed
— the bug was entirely that `_load_duty_records_for_crew()` never
fetched more than 35 days of history, so D9.2.3 (365-day/1000h
cumulative flight time) has never once been able to see enough data
to fire, for any real assignment, ever. Fixed by widening
`LOOKBACK_DAYS` to 370 — confirmed deliberately NOT split into a
narrower window for the D7–D28 checks and a wider one just for
D9.2.2/D9.2.3 (user's explicit call: Air Eagle's crew pool is small
enough that every call site (`_validate_new_duty()`,
`_check_downstream_impact()` per future duty,
`find_legal_candidates_for_duty()` per candidate) pulling ~10x more
history per query is not a real cost).

**Findings 2 & 3 — same underlying architecture question, resolved
the same way.** Both `update_flight()` (recompute FDP on delay) and
`cancel_flight()` (exclude cancelled flights from legality history)
need to reach into `roster`, which `flight_service.py` deliberately
doesn't know about (Ownership Table). Resolved by adding two new
wrapper functions in `services/assignment_service.py` — which already
depends on `flight_service.py`, never the reverse — rather than
having `flight_service.py` import `assignment_service.py` (would
create a circular import). Pages now call these instead of
`flight_service.cancel_flight()`/`update_flight()` directly whenever
crew might be assigned:

- **`cancel_flight_and_roster(flight_id, reason, app_user)`** — calls
  `flight_service.cancel_flight()`, then cascades `roster.status =
  'CANCELLED'` to every roster row referencing that flight. Fixes the
  real gap directly: before this, `_load_duty_records_for_crew()`'s
  `WHERE r.status != 'CANCELLED'` filter never excluded a cancelled
  flight's duty, since it only ever checked `roster.status`, never
  `flights.status` — a cancelled flight kept counting toward that
  crew member's FDP/rest/cumulative-hours history exactly as if it
  had actually operated. Cascading here keeps that one filter as the
  ONE place deciding what counts as active history, rather than
  teaching every future roster+flights query to also join against
  `flights.status`.

- **`update_flight_actual_times_and_revalidate(flight_id,
  dep_time_actual, arr_time_actual, app_user)`** — calls
  `flight_service.update_flight()`, then finds every (crew_id,
  duty_id) pair referencing that flight (a single flight can carry
  SEVERAL different duty_ids at once — one per crew member assigned
  to it, since `_validate_new_duty()` generates a fresh duty_id per
  assignment call even for the identical flight) and recomputes each
  one independently via a new `_recompute_one_duty_after_delay()`
  helper: rebuilds the duty's debrief_time/fdp_hours using
  `core/duty_builder.py`'s `recompute_fdp_after_delay()` (report_time
  is NEVER recomputed — same historical block-time bug
  `recompute_fdp_after_delay()`'s own docstring explains), updates
  every roster row sharing that duty_id, then revalidates the whole
  duty against the crew member's other history (FDP/rest via
  `validate_schedule()`, skipped for FTL-exempt LM/ENGR same as
  assignment time, PLUS the qualification gate re-checked against the
  recomputed debrief date). Also re-runs the existing, already-tested
  `_check_downstream_impact()` — a delay can break an
  already-scheduled LATER duty's rest/cumulative math, not just the
  one being delayed, same ripple concept already built for new
  assignments.

  **User's explicit decision on what happens when a delay makes a
  duty no longer legal**: recompute the times regardless (the system
  can't refuse to record what actually happened), but flag the
  affected roster row(s) `status = 'NEEDS_REVIEW'` — not just an
  audit-log alert — so the problem is visible in the data itself.
  This needed `migrations/009_roster_needs_review_status.sql` to add
  `NEEDS_REVIEW` to `roster.status`'s CHECK constraint (roster.status
  already allowed `OPERATED`/`DISRUPTED` since 003_roster_table.sql,
  neither ever actually written by any code path yet — this migration
  doesn't change that).

  `pages/3_Flight_Log.py` updated to call this instead of
  `flight_service.update_flight()` directly, surfacing a warning for
  any NEEDS_REVIEW/ILLEGAL outcome and the same downstream swap-alert
  UI pattern already used on the Roster/Control Room pages.

**Migration numbering collision, flagged deliberately, now resolved**:
this branch's roster-status migration and
`remove-type-rating-contract-fields`'s
`migrations/008_drop_type_rating_and_contract_expiry.sql` were two
independent migrations both originally numbered 008 off the same base
(`main` at 007). Flagged here and in both migration files' own headers
rather than letting it become a silent surprise at merge time.
Resolved per the user's merge order: `remove-type-rating-contract-fields`
merged first and kept 008; this branch's migration renamed to
`migrations/009_roster_needs_review_status.sql`, rebased onto the
updated `main`, and re-verified before merging.

**Test coverage**: 12 new tests — 2 in `test_schema.py` (the
NEEDS_REVIEW CHECK constraint actually accepts the new value, and
still rejects garbage), 10 in `test_assignment_service.py` covering:
the LOOKBACK_DAYS constant itself, a 40-day-old duty now actually
loaded as history (proving the fix isn't just the constant on paper),
cancel-cascade (both with and without crew assigned), the cancelled-
duty-excluded-from-history scenario directly (the actual bug,
reproduced and confirmed fixed), a small delay that stays legal
(mechanical recompute proof — report_time fixed, debrief/fdp
updated), a bigger delay that correctly flags NEEDS_REVIEW (deliberately
tuned to 8h FDP, not more — a bigger delay would ALSO trip D8.2.1's
~13h max-FDP-for-one-sector limit and turn this into an ILLEGAL case
instead of the NEEDS_MANUAL_REVIEW case being tested — caught during
hand-tracing, not by running the test, since no database was
reachable here), multiple crew on one flight recomputed independently,
an FTL-exempt role's delay recompute skipping FDP/rest math entirely,
and a delay-triggered downstream conflict on a separate future duty.

**Verification status**: built and reasoned through in an environment
with no reachable database at all (no TEST_DATABASE_URL, no local
Postgres, no Docker) — collection and non-DB tests passed locally at
the time (190 total, 50 non-DB passing, 140 skipped). Every line
touching the database was traced by hand against the actual schema
and query behavior instead of executed, including working through one
real mistake caught this way (the 8h-vs-bigger-delay FDP-limit
collision above) before it could have been a silently wrong test.

**RESOLVED 2026-08-01 (same day, following verification)**: the user
independently verified this branch against real Postgres 16 —
190/190 passing. D9.2.3 (the 365-day/1000h cumulative check
LOOKBACK_DAYS was starving) confirmed empirically firing with ~300
seeded duties, not just traced by hand. Migration renumbered to 009
per the collision note above, rebased onto `main` (which by this
point already had `remove-type-rating-contract-fields` merged), and
merged.

## 2026-08-01: alert-volume explosion found during Step 5's own
## verification — fixed via services/alert_summary.py, planned before
## implementation, NOT yet verified against a real database

Direct, unplanned consequence of Step 5's own LOOKBACK_DAYS widening
(35 -> 370), caught by the user's real-Postgres re-verification of
that same change: `core/legality/pcaa_ano012_core.py`'s
`_check_cumulative_limits()` emits one `RuleAlert` per breached rule
**per historical duty** in the crew member's rolling window, not one
per breach overall — with 35 days of history this was rarely visible;
with 370, a crew member who has been over a limit for months now
generates one alert per historical duty in that stretch. Measured: a
single assignment attempt against ~300 seeded duties returned **2,215
alerts across 11 rule codes in 1.26s**. `pages/4_Roster.py` and
`pages/1_Control_Room.py` (3 render loops each) and
`pages/3_Flight_Log.py` (1 loop, multipliable across several crew/duty
pairs affected by one delay) all rendered `result.alerts` in uncapped
loops — a controller making one assignment would see thousands of
lines. Separately, `services/assignment_service.py` joined alert
messages into `audit_log.warning_or_failure_reason` (a `TEXT` column,
no length limit, so no write failure — but **~150KB in one row**
measured against the 2,215-alert case) at 5 sites; one of those 5
(`_recompute_one_duty_after_delay()`) had **no status filter at all**
— a real correctness bug independent of volume, since it could log
WARNING/LEGAL-tier alert text into a NEEDS_REVIEW/ILLEGAL audit entry.

**Planned before implementation, same discipline as every fix this
session**: proposed a plan, the user verified two specific claims
against the actual code before approving (the unfiltered join site,
and that `_check_crew_qualifications()` also builds `duty_id`-less
alerts — a 4th classification, not the 3 originally proposed), and
confirmed the conservative design for the new diagnostic field before
any code was written.

**The fix — new `services/alert_summary.py`**, pure Python, no
`streamlit`/`get_engine()` import — same placement principle as
`FTL_EXEMPT_ROLES`/the qualification gate: presentation/orchestration
decisions stay out of `core/legality/pcaa_ano012_core.py`, which stays
a pure, airline/UI-agnostic rule engine. Mirrors `core/duty_summary.py`'s
shape (a separate, pure, presentation module tested without a DB
fixture).

`summarize_alerts(validation_result, target_duty_id)` buckets every
alert into exactly one of four groups, by `duty_id`/`rule_code`:
- **target_duty_alerts** — `duty_id == target_duty_id`: the duty this
  call is actually about.
- **qualification_alerts** — `duty_id is None`, `rule_code` starts
  with `AE-CREW-QUAL-001` (from `_check_crew_qualifications()` in this
  same file — the crew member's *current* documents, not duty
  history).
- **schedule_level_alerts** — `duty_id is None`, everything else
  (currently only `D23.1_MANDATORY_5_DAYS_OFF` /
  `D23.2_SEVENTH_DAY_OFF` — genuine whole-schedule patterns, not tied
  to one duty).
- **historical_counts** — everything else (`duty_id` set, not
  `target_duty_id`), collapsed to one `HistoricalAlertCount` per
  `rule_code` — this is what actually fixes the 2,215-alert case:
  "D9.2.3 breached in 187 historical duties," not 187 separate lines.

`ValidationResult.status`/`AssignmentResult.legality_status` are
completely untouched by any of this — `summarize_alerts()` only
reshapes what's displayed/logged, never what determines legality. An
assignment resting on a real historical breach still reports ILLEGAL.

**New field: `blocked_by_history_only`** — `True` only when overall
status is ILLEGAL AND `target_duty_alerts` + `qualification_alerts` +
`schedule_level_alerts` contain zero ILLEGAL alerts between them, i.e.
every ILLEGAL alert present is historical. Deliberately conservative,
confirmed explicitly with the user rather than assumed: a genuinely
ambiguous case (this duty is what pushes a 6-day streak to a 7th, or
the crew member's medical happens to have expired) must report
`False`, not guess `True`. The 4th-bucket catch matters concretely
here — without separating qualification alerts from schedule-level
ones, an expired medical could have been reported as "blocked by
pre-existing history, this duty is not the cause," which is exactly
backwards and the single most misleading thing this feature could
say. Both the schedule-level-alone and qualification-alone cases have
dedicated regression tests, at both the pure-logic and DB-integration
level.

**Also in `services/alert_summary.py`**: `build_audit_reason(summary,
statuses)` replaces all 5 previously-duplicated inline audit-message
joins in `assignment_service.py` — includes non-historical alerts
matching `statuses` in full, summarizes historical ones to one line
per rule_code. The `_recompute_one_duty_after_delay()` call site now
correctly filters to `{ILLEGAL, NEEDS_MANUAL_REVIEW}` (matching the
`now_needs_review` condition that already gated it) instead of joining
every alert unfiltered — the correctness fix, not just the volume one.
`format_alert_lines(summary)` produces the markdown lines all 7 page
render loops now use (3 in `4_Roster.py`, 3 in `1_Control_Room.py`, 1
in `3_Flight_Log.py`) instead of the previous uncapped
`for alert in result.alerts` loops.

**Wiring**: `AssignmentResult` gained one additive field,
`alert_summary` (the existing `alerts` field is untouched — nothing
that already reads it needed to change). Computed once per call in
`assign_crew_to_duty()`/`assign_crew_to_new_flights()` right after
`_validate_new_duty()`, passed into all 6 `AssignmentResult(...)`
construction sites. `_recompute_one_duty_after_delay()`'s return
widened from a 2-tuple to a 3-tuple (safe — exactly one caller in the
whole codebase); `update_flight_actual_times_and_revalidate()`'s
result dicts gained one additive key to match.
`find_legal_candidates_for_duty()` — **not touched**, confirmed it
never reads `.alerts`, never builds `AssignmentResult`, never calls
`log_audit()`. Its own, separate, larger per-candidate cost (runs full
`validate_schedule()` once per candidate, discards every alert for a
single boolean) is explicitly deferred — likely a `fail_fast`
early-exit parameter on `validate_schedule()` itself later, a
core-engine change rather than a presentation one.

**Test coverage**: 12 new pure-logic tests in the new
`tests/test_alert_summary.py` (no DB — `RuleAlert`/`ValidationResult`/
`AlertSummary` are plain dataclasses, same principle as
`test_pcaa_ano012_core.py`/`test_duty_summary.py`) — **these actually
ran and passed locally, 0.41s**, the first tests all session able to
execute for real in this environment rather than only being traced by
hand. Cover: bucketing by `duty_id`, collapsing many historical alerts
to one count per rule_code, qualification vs. schedule-level
non-conflation, `blocked_by_history_only` true/false in five distinct
scenarios (pure-historical, target-duty-has-its-own, schedule-level-alone,
qualification-alone, status-not-ILLEGAL-at-all),
`build_audit_reason()` filtering and historical summarization,
`format_alert_lines()` section-omission and the exact blocked-by-history
sentence.

5 new DB-integration tests added to `test_assignment_service.py`, via
a new `_seed_many_duties()` helper (spacing duties 3 days apart, not
daily, specifically to avoid tripping `D23.1`/`D23.2` while
accumulating a genuine `D9.1.3` breach — see that helper's own
docstring for the reasoning) plus a shared
`_seed_heavy_history_and_assign_far_future()` setup: a pure-historical
ILLEGAL breach correctly reports `blocked_by_history_only=True`
(and, in a dedicated separately-named test, that `status`/
`legality_status` stay correctly `"ILLEGAL"` — the core guarantee);
the identical seed with the new duty placed inside the still-breached
window instead reports `blocked_by_history_only=False`; the
schedule-level edge case (6 legal daily duties, a 7th tripping only
`D23.2_SEVENTH_DAY_OFF`) at the integration level, not just unit
level; and the audit-row-size regression test (historical rule's
message appears exactly once, row stays under a fixed bound). One
existing test extended with an audit-non-empty assertion, confirming
the now-filtered `_recompute_one_duty_after_delay()` call site is
wired correctly (filter correctness itself already proven at the unit
level).

**Verification status, at the time this was written**: built and
reasoned through in an environment with no reachable database at all
(no TEST_DATABASE_URL, no local Postgres, no Docker) — same limitation
as every DB-dependent piece of this session's work. The 12 new
pure-logic tests genuinely executed here and passed, since
`summarize_alerts()`/`build_audit_reason()`/`format_alert_lines()` need
no database at all. The 5 new DB-integration tests, and the 190
pre-existing tests re-running against the new
`AssignmentResult.alert_summary` field, were traced by hand against
the actual schema and query behavior, including working through a
real correction mid-way (the "very next day" scenario's dominant
failure mode turned out to be the D21 rest floor, not D9.1.3, as first
assumed — fixed in the test's own docstring before it could have been
a misleading comment, not a wrong assertion).

**RESOLVED 2026-08-01 (same day, following verification)**: the user
independently verified this branch against real Postgres 16 —
207/207 passing, zero failures. Re-measured the same before/after
scenario on both `main` and this branch with identical seeded data
(299 duties, 2,168 flight hours) rather than the earlier ~300-duty
estimate:

| Metric | Before (`main`) | After (this branch) | Change |
|---|---|---|---|
| Display lines rendered | 2,216 | 29 | -98.7% |
| `audit_log.warning_or_failure_reason` size | 128,685 chars | 2,253 chars | -98.3% |
| Assignment time | 0.78s | 0.84s | negligible |
| Raw alert count | 2,216 | 2,216 (unchanged) | — |
| Result | REJECTED / ILLEGAL | REJECTED / ILLEGAL (unchanged) | — |

The unchanged raw alert count and unchanged REJECTED/ILLEGAL result
across both runs is the direct confirmation that summarization is
display-only — the deterministic legality outcome was never touched.
(The original ~150KB estimate quoted earlier in this file was an
estimate from the initial bug report, not a measurement — 128,685
chars is the actual measured figure and supersedes it; the 2,215 vs.
2,216 and "~300" vs. 299-duty figures are two distinct measurement
runs with their own exact seed data, not a discrepancy.)

Bucket breakdown on the measured scenario: 7 target-duty alerts, 0
qualification alerts, 11 schedule-level alerts, 8 distinct
historical-collapsed rule codes. `blocked_by_history_only=False` —
correctly so, since the target duty itself carried real ILLEGAL
alerts in this scenario, not a pure-historical-only breach.

Built on branch `alert-summarization`, off `main` at commit `245a13d`,
merged into `main` after the verification above.

## 2026-08-01: OCC assistant query parser (no LLM) — plus two real,
## stacked bugs found in scripts/check_reachability.py while placing
## it, both fixed on the same branch. NOT MERGED.

**The parser**: `services/assistant/query_parser.py` +
`tests/test_query_parser.py`, branch `query-parser`, off `main` at
commit `8441817`. Turns an OCC controller's natural-language question
into a `ReportRequest` (template name + parameters: crew_ids, role,
date_from/date_to, origin/destination, flight_no, window_days) —
nothing executes the request yet; the seven report functions that
would actually run one against the real schema are the next piece,
explicitly not built here.

**Why no LLM (decided after measuring a prototype)**: the assistant's
job is narrow — it returns records that already exist, never a
recommendation and never a legality determination (that authority
stays entirely with `core/legality/pcaa_ano012_core.py` via the
service layer, completely untouched by this work). For ~10 crew, 2
fixed rotations, and a handful of trained users, "which template,
which crew, which dates" is a controlled vocabulary, not open-ended
public input — keyword/pattern matching handles it without a model.
Buys, all of which matter for a safety-critical system under a
regulator: zero API cost, zero latency, no crew PII ever leaving the
operator's infrastructure, works with no network, fully unit-testable
without mocking a provider, every decision inspectable and
explainable. Costs: won't generalize to unanticipated phrasings — the
accepted failure mode is "I didn't understand, here's what I can
show you" (safe, self-correcting) rather than a confident answer to a
misread question. Unresolved queries retain their raw text
(`ReportRequest.unmatched_text`) specifically so the keyword lists
can grow from real logged usage, not guesswork — the same discipline
this whole file has followed since Phase 1.

Scoring, not first-match: an earlier prototype used "longest keyword
list wins," which mis-routed `'show the flight log for July'` to crew
duty history purely because both templates contain "flight."
Templates carry weighted positive AND negative keywords, and the
winner needs a minimum margin over the runner-up — below that margin
the parser returns unresolved with the candidates, rather than
guessing. Crew-name resolution deliberately surfaces real ambiguity
rather than picking one: Air Eagle's actual roster has both "SYED
FAHIM MAHMOOD" and "TAHIR MAHMOOD RAJA," so a bare "mahmood" query
genuinely identifies two people — the parser returns both crew_ids as
a named ambiguity rather than silently guessing one, the same
principle `services/assignment_service.py`'s own qualification gate
already applies to crew documents. 41 tests, all pure logic (no DB —
`query_parser.py` takes the crew directory as a plain argument, same
principle as `core/duty_summary.py`), genuinely run and passing here
(0.29s).

**Mojibake found and fixed while placing the pasted files**: both
source files as received contained "â" in place of what should have
been em dashes (—), including inside three regex character
alternations (the ISO/day-range date-separator patterns and the
airport-route separator) — left as-is, those regexes would silently
never match a real em-dash-separated input, a dead branch rather than
a working one. Corrected before writing either file to disk.

**Bug 1 in `scripts/check_reachability.py` — the actual reason this
became a bigger piece of work than placing two files**: running the
checker after adding `query_parser.py` showed it as reachable, which
was wrong — nothing imports it yet. Root cause: `find_all_imports()`
only ever captured the module path BEFORE `import` in a `from X
import a, b, c` statement — discarding `a, b, c` entirely — and
`main()`'s reachability check included `mod.startswith(imp + ".")`,
treating that bare `"X"` as proof that EVERY file under `X` is
reachable. Every page in this repo does `from services import
crew_service, ...`, so the bare token `"services"` was always present
in the imported set, and `"services.assistant.query_parser".startswith("services.")`
is trivially true — the file read as reachable not because anything
imports it, but because something imports its grandparent package.
`core/` only ever escaped this by luck: every existing `core/` import
already happens to be fully qualified (`from core.duty_builder import
X`), never a bare `from core import X`. Confirmed by planting a
deliberately unreferenced module and observing it go unflagged.

Fixed: `find_all_imports()` now parses `from X import a, b, c` (single
line AND the multi-line parenthesized form used throughout this
codebase's own service layer) into exact candidate paths — `X` itself
(needed for `from core.legality.pcaa_ano012_core import CrewMember`,
where `CrewMember` is a class inside that module, not a submodule —
`X` alone is already the exact watched path) AND `X.a`, `X.b`, `X.c`
(needed for `from services import crew_service`, where `crew_service`
IS itself a separate watched file that the bare package name does not,
on its own, prove reachable). `main()`'s `is_imported` check dropped
the `mod.startswith(imp + ".")` direction entirely — no more "any
ancestor, at any depth" matching in that direction, full stop, not
narrowed to direct parent-child as first considered, since the new
exact-candidate-path expansion already covers every case that
direction was ever legitimately needed for.

**Bug 2 — independent, and masked by bug 1 until it was removed**:
after fixing bug 1, `services/assignment_service.py` — genuinely
imported by every page — started showing as unreachable too. Root
cause: `find_all_imports()` read files via `Path.read_text()` with no
explicit encoding, which defaults to the OS locale codec (`cp1252` on
Windows) — and every file in `pages/` contains UTF-8 characters (em
dashes, emoji in `st.set_page_config`) that `cp1252` cannot decode.
That raised `UnicodeDecodeError` on all four page files, caught by a
bare `except Exception: continue`, and silently skipped their imports
entirely — not just one file's worth, all of `pages/`. This had
already been true before bug 1 was ever touched; it was invisible
because bug 1's over-broad prefix match was accidentally compensating
for it: `assignment_service.py` itself contains `from services import
crew_service, flight_service`, so that self-referential bare
`"services"` token alone was enough to "prove" `assignment_service.py`
reachable under the old rule, regardless of whether `pages/` had
actually been scanned at all. Two bugs silently canceling out — fixing
only the first turned the second into a wave of new false positives.

Confirmed this is the exact same corruption pattern as the mojibake
found in the pasted parser files above (`—` -> three wrong characters
under `cp1252`) — not a coincidence, the same root cause surfacing in
two different places on the same day.

Fixed: `read_text(encoding="utf-8")`, explicit. Also: the bare
`except Exception: continue` now prints a warning naming the file
before skipping it — a file this checker can't read is a checker
malfunction, not a normal condition, and staying silent about it is
exactly how bug 2 went unnoticed for as long as it did.

**Same bug class checked elsewhere and found**: `scripts/run_migrations.py`
had two more unencoded `read_text()` calls (applying `000_migration_
tracking.sql` and each pending migration). Every migration file
does contain non-ASCII bytes (em dashes in `--` comments) — confirmed
by direct byte inspection. Practical impact here is more benign than
`check_reachability.py`'s (Postgres line comments run to end-of-line
regardless of content, and `checksum()` was never affected — it
hashes `read_bytes()`, not `read_text()` — so the idempotency/edit-
detection guarantee was never at risk), but the same silent-corruption
class, so fixed the same way: explicit `encoding="utf-8"` at both
call sites. `scripts/import_crew_from_xlsx.py` checked too — no raw
text-file reads there at all (uses `openpyxl` for the binary `.xlsx`
format), nothing to fix.

**Test coverage for the reachability-checker fix**: `find_orphaned()`
extracted out of `main()` as its own pure function (watched list +
imported set -> orphaned list) specifically so it could be unit-tested
directly against synthetic fake repos, not scraped from stdout. 7 new
tests in `tests/test_check_reachability.py`, each building an isolated
fake repo under `tmp_path` (never the real project tree) and
monkeypatching `ROOT`: an unreferenced module in a subpackage IS
flagged (the actual regression case); a genuinely-imported module in
a subpackage is NOT flagged (the fix must narrow false positives, not
just add noise); a bare `from services import X` does not mark an
unrelated sibling reachable (the regression this whole fix exists
for); a symbol imported from an exact watched module (not a
submodule) still marks it reachable; the multi-line parenthesized
`from X import (...)` form is parsed fully, not just its first line;
a file with real non-ASCII content still has its imports counted (the
positive case for the encoding fix); and an unreadable file produces
a warning rather than a silent skip. All 7 genuinely executed and
passed here (no DB needed).

**Verification status**: 255 total tests on this branch (207 on
`main` + 48 new — 41 parser, 7 reachability-checker), all pure logic,
no database needed anywhere in this branch's additions. 110/110 non-DB
tests passing locally, 6.41s. `check_reachability.py` re-run against
the real repo after both fixes: exactly `core/duty_summary.py` and
`services/assistant/query_parser.py` flagged, nothing else — both
genuinely, currently unwired, which is correct.

**NOT MERGED — explicit, per instruction**. The parser produces
`ReportRequest` objects but nothing consumes them yet; the seven
report functions are the next piece. Built on branch `query-parser`,
off `main` at commit `8441817`.

## 2026-08-01 (continued): the seven report functions — the piece
## that makes query_parser.py's output actually run against the
## schema. NOT MERGED.

Design conversation happened first (plan approved with three answers
and one correction before any code was written), then implementation
proceeded on branch `assistant-report-functions`, off `main` at
`28b5da3` (the `query-parser` merge commit).

**Three new files, one deliberate split**:

- `services/reporting.py` — general-purpose export layer: `Dataset`
  (frozen dataclass, `build()` classmethod validates row-width
  consistency), `AirlineIdentity`, `dataset_to_csv/xlsx/markdown()`,
  `report_filename()`. Deliberately NOT under `services/assistant/`:
  the operator's Excel-export requirement ("every data-bearing page
  should download its currently filtered data as .xlsx, filename
  `AirEagle_[PageName]_DD-MM-YYYY_HHMMUTC.xlsx`") applies to Flight
  Log/Crew Data/Roster too, not just the assistant — this way every
  page's own export button and the assistant's report functions share
  one implementation instead of two. `Dataset`/`AirlineIdentity` are
  taken from the assistant bundle's `services/assistant/models.py`
  (received 2026-08-01); that file also defines `ToolResult`/
  `QueryRequest`/`AuditEvent`/`AssistantAnswer`, which look like
  remnants of a different, LLM-tool-calling architecture
  (`provider_mode`, `citations`, `tools_used`) than the deterministic,
  no-LLM approach already built — only `Dataset`/`AirlineIdentity` are
  used; the rest of that file is not brought in.
  `report_filename()`'s original bundled version used `%Y%m%d` and
  `airline.code` — fixed to the operator's actual required format
  (`%d-%m-%Y_%H%MUTC`, `airline.name` not `.code`) per this session's
  explicit correction; smoke-tested to an exact match:
  `report_filename(ds, AIR_EAGLE, 'xlsx', now=2026-07-24 17:35 UTC)`
  -> `AirEagle_FlightLog_24-07-2026_1735UTC.xlsx`.
- `services/assistant/regulation_reference.py` — curated, plain-
  English ANO-012 section summaries for the `regulation` template,
  scoped to exactly the sections `pcaa_ano012_core.py` actually
  enforces today (D7.1.2, D8.2.1, D9.1.1-3, D9.2.1-3, D21.1, D23.1,
  D23.2, D25). Deliberately excludes AE-CREW-QUAL-001 and the
  age-pairing rule — both are confirmed Air Eagle OPERATING decisions,
  not ANO-012 provisions; a regulation lookup answering with either
  would misattribute an airline policy to the regulator. D7.1.2's
  numbers are imported directly from `core/duty_builder.py`'s named
  constants (can't drift, by construction). The other sections have no
  named constants in `pcaa_ano012_core.py` (inline literals inside
  `_check_cumulative_limits`/`required_rest_minutes`/etc.) — deliberately
  NOT extracting those into named constants as part of this change, to
  avoid touching the one file this project's own rules protect most
  heavily for a reporting-only feature. Cross-checked instead by
  boundary tests exercising the ACTUAL validator (see below) rather
  than trusting a second, independently-typed copy of the same
  numbers to stay in sync on its own.
- `services/assistant/reports.py` — the seven functions
  (`crew_duty_history`, `flight_records`, `crew_qualifications`,
  `utilization`, `roster_coverage`, `audit_compliance`, `regulation`)
  plus `run_report()`, a dispatcher keyed on `request.template`. Each
  function reuses an existing canonical read function where one
  exists, extended rather than duplicated, per the Ownership Table's
  one-read-path-per-table convention:
  - `crew_duty_history`/`utilization` -> `assignment_service.search_roster()`
    (new — `get_roster_for_crew()` takes one crew_id and has no date
    filtering; this branch's search needs a crew_id LIST plus a date
    range).
  - `flight_records`/`roster_coverage` -> `flight_service.get_all_flights()`
    (extended with `date_from`/`date_to`/`flight_no` — was
    `status_filter`/`origin`/`destination` only).
  - `crew_qualifications` -> `crew_service.get_all_crew()` (unchanged;
    filtered/reshaped here using `assignment_service.QUALIFICATION_EXPIRY_FIELDS`
    as the single source of truth for which 8 fields matter, not a
    second hardcoded list).
  - `audit_compliance` -> `audit_service.get_audit_log()` (new —
    `audit_service.py` was write-only, `log_audit()` only, until now;
    this is the file's first-ever read function).
  - `regulation` -> `services/assistant/regulation_reference.py`'s
    `lookup()`.
  `REPORT_FUNCTIONS`'s dict keys are asserted equal to
  `query_parser.TEMPLATES`'s keys (minus `regulation`, handled as a
  special case — see below) at import time — a future template added
  to the parser without a matching report function now fails loudly at
  import, not silently at runtime, per the plan's explicit SSOT
  requirement.

**The three approved-plan decisions, as actually implemented**:

1. `crew_duty_history` stays sector-level (one row per roster row, not
   deduped to one row per duty) with `duty_id` visible as its own
   column, plus a `Dataset.notes` entry citing the Section 9
   warning directly: "fdp_hours, report_time, and debrief_time are
   duty-level values that repeat across every sector row sharing the
   same duty_id ... do not sum fdp_hours across these rows without
   first grouping by duty_id ... this is the single most repeated bug
   in this platform's history." `utilization` is the function that
   actually does the duty-level dedup, via `core/duty_summary.py`'s
   `group_roster_rows_into_duties()`/`calculate_crew_duty_summary()` —
   **this file's first real caller anywhere in this app**, confirmed
   by `check_reachability.py` no longer flagging it once `reports.py`
   existed. When `request.window_days` is set (a rolling-window
   phrasing like "last 28 days"), `utilization` additionally calls
   `calculate_max_rolling_fdp(duty_df, window_days)` and adds a
   `peak_N_day_fdp_hours` column — a genuinely different number from
   the range's own total, never conflated with it.
2. `roster_coverage` scoped to the 4 confirmed-required roles (CPT,
   FO, LM, ENGR — "Other Crew" deferred), each shown as a comma-joined
   list of assigned crew_ids rather than a single value, `UNCOVERED`
   only when a role's list is empty (not "not exactly 1") — because
   real Air Eagle data shows a flight with 2 engineers assigned ("Eng:
   2x VAI"), so more-than-one is expected, not an anomaly. A
   `Dataset.notes` entry and an `Open stubs` bullet (above) both flag
   that the actual required count per role is unconfirmed.
3. `regulation` derives its answers from `pcaa_ano012_core.py`'s
   actual enforced constants (via `regulation_reference.py`), not a
   stored copy of the ANO-012 text extract that exists elsewhere in
   the wider assistant bundle (verified, with SHA-256 provenance, but
   not used here) — confirmed as the right v1 approach specifically
   because the boundary tests below make drift detectable, which a
   stored-text copy wouldn't be.

**Gap resolved during implementation, not part of the original
plan**: `query_parser.ReportRequest` doesn't carry the original
question text forward once resolved (by design — the parser stays a
pure form-filler). `regulation()` needs the raw question to know which
D-section was actually asked about. Resolved by accepting `question:
str` as an explicit second parameter on `regulation()` (and threaded
through `run_report(request, question="")`), re-extracting the section
via `query_parser.SECTION_RE` — the exact same regex the parser itself
used to route to `regulation` in the first place — rather than
modifying the already-merged, already-tested `query_parser.py` to
retroactively store something it was deliberately built not to.

**One filter enhancement found while wiring `flight_records`, not
originally scoped**: `query_parser.parse_flight_no()` already extracts
a flight number (e.g. "EPE 786") that had nowhere to go —
`flight_service.get_all_flights()` had no flight_no parameter at all.
Added one, extending the same canonical function rather than a new
query path. The real stored format for `flights.flight_no` wasn't
confirmed against actual operator data at the time this was written
(only prose mentions of "EPE 786/787" exist in this file, not a
verified column value), so the SQL match normalizes both sides
(`REPLACE(UPPER(...), ' ', '')`) rather than assuming a specific
spacing convention — proven in
`test_flight_records_flight_no_matches_regardless_of_spacing` (a
flight stored as `"EPE786"` still matches a request for `"EPE 786"`).

**Boundary tests added specifically to backstop `regulation_reference.py`
against drift** (`tests/test_assistant_reports.py`, no DB needed — the
validator's methods are called directly): D9.1.1 (60h/7d),
D9.1.2 (110h/14d), D9.1.3 (190h/28d), D9.2.1 (35h/7d), D9.2.2
(100h/30d), D9.2.3 (1000h/365d) each get an exact at-limit/over-limit
pair — confirmed the real `_check_cumulative_limits()` check is
strictly-greater-than, not at-or-over, and confirmed the specific rule
code only fires past its exact stated threshold, not before. D21.1
(charter rest, `max(12h, 2xFDP)`) and D8.2.1 (the 13h00/12h00/11h00
report-time bands) get one boundary test each, reusing the same
`required_rest_minutes()`/`get_max_fdp_minutes()` public methods
`test_pcaa_ano012_core.py` already exercises. D23.1/D23.2/D25 are NOT
independently boundary-tested this round — flagged explicitly in
`Open stubs` above rather than silently claimed as covered; re-deriving
those three would need a materially larger duty-sequence setup than
the others. All 8 boundary tests were confirmed to actually pass by
direct interpreter execution in this sandbox (bypassing the DB-gated
pytest fixture the same tests sit behind in the actual test file,
which has no reachable database here) — genuinely run, not just
traced by hand.

**Test coverage**: 46 new tests. `tests/test_reporting_export.py` (18,
pure logic, genuinely run here, all passing): `Dataset.build()`
row-width validation, CSV/XLSX/Markdown rendering including the notes
section/sheet, and `report_filename()` matching the operator's exact
required format plus its sanitize-backstop behavior. `tests/test_assistant_reports.py`
(28: 8 pure-logic boundary tests confirmed passing by direct
interpreter execution as described above; 20 DB-integration tests, one
or two per report function plus the `run_report()` dispatcher, traced
by hand only — not run, no reachable database in this sandbox, flag
this explicitly when verifying) — covers: sector-level, non-deduped
`crew_duty_history` output with `duty_id` visible; `flight_records`
including cancelled flights by default and the flight_no
space-normalization fix; `crew_qualifications`'s expiry-window
filtering; `utilization`'s duty-level dedup (the exact Section 9
mistake, at the report layer) and its window_days peak column;
`roster_coverage`'s uncovered-role detection and multi-crew-per-role
handling (engineers) plus cancelled-flight exclusion;
`audit_compliance`'s action-type and crew-id filtering;
`regulation`'s question-text section extraction and its two distinct
"nothing to say" cases (no section mentioned vs. a real but unimplemented
section); `run_report()`'s rejection of unresolved requests and
unknown templates, its `regulation`-question routing, and every
template running end-to-end without error against an empty database.

**Verification status**: 301 total on this branch — see "Tests
passed" above for the exact breakdown. `pytest tests/`: 128 passed, 173
skipped (no `TEST_DATABASE_URL` here). `check_reachability.py`:
`services/assistant/reports.py` flagged (correct — not wired into a
page yet); `core/duty_summary.py` no longer flagged. **RESOLVED
2026-08-02**: independently verified by the user against real
Postgres 16 — 301/301 passing, including all 20 DB-integration tests
this environment could only trace by hand — plus a direct check that
the filename format and both plan-approved report behaviors
(`crew_duty_history`'s notes/`duty_id`, `roster_coverage`'s
comma-joined role lists) matched spec. Merged into `main`.

## 2026-08-02: Air Eagle's crew records narrowed to CPT/FO only —
## LM/AME become the operator's own responsibility, tracked nowhere
## in this system except as free text per flight. `roster_coverage()`
## reshaped to match. Plus: "VAI" resolved, age-rule wording settled,
## auth confirmed deliberately parked. NOT MERGED.

Five operator decisions came back in one batch; implemented together
on branch `operator-crew-scope-and-coverage-reshape`, off `main` at
the `assistant-report-functions` merge commit.

**1. Air Eagle's crew records are CPT and FO only — a real reversal of
the 2026-07-21 position.** That earlier finding ("Engr is AME... No
FTL applicable... same on LM") treated LM/AME as FTL-EXEMPT CREW
RECORDS — real rows in the `crew` table, just exempt from FDP/rest
math via `FTL_EXEMPT_ROLES`. The operator's 2026-08-02 decision goes
further: LM and AME are not crew records **at all**, for Air Eagle
specifically. They fly on real rotations, but FTLguard doesn't track
them as crew — that's the operator's own operational responsibility.
This directly explains why the 2026-07-21 Loadmaster spreadsheet
section (rows 16-19, misaligned — dates ended up in Base/Email/
License No, never imported) was never worth fixing: even a perfectly
realigned Loadmaster row was always going to hit this exclusion.

**Explicitly NOT touched, per the operator's own instruction**: the
FTL-exemption machinery itself — `FTL_EXEMPT_ROLES = {"LM", "ENGR"}`,
the exemption branches in `_validate_new_duty()`/
`_check_downstream_impact()`/`find_legal_candidates_for_duty()`, and
their ~7 dedicated tests all stay exactly as built. This is FTLguard's
CORE-vs-AIRLINE-CONFIG split working as designed: a future scheduled-
carrier client may roster loadmasters/engineers who ARE FTL-exempt
crew records on that platform. Air Eagle simply chooses not to create
those records at all — a data/import decision, not a code branch.
There is no `if airline == "AirEagle"` anywhere in this change, and
there shouldn't be one.

Concretely: `scripts/import_crew_from_xlsx.py` gained `EXCLUDED_ROLES
= {"LM", "LOADMASTER", "LOAD MASTER", "ENGR", "AME", "ENGINEER"}`,
checked against the RAW sheet value for every row, before the
misalignment/suspect-date logic even runs — so a row is excluded for
being LM/AME independent of whether it's otherwise clean, not as a
side effect of the existing "text field holds a date" check. Neither
`crew_service.py`'s `ROLE_SYNONYMS`/`ROLE_PREFIXES` nor
`assignment_service.py`'s `FTL_EXEMPT_ROLES`/`QUALIFICATION_EXPIRY_FIELDS`
were touched — existing LM/ENGR crew data (synthetic test fixtures,
or any real row already imported) stays completely valid; this is
about what gets imported going forward, exactly the same principle
already established for the type_rating_expiry/contract_expiry
removal (migrations/008).

**Known, accepted consequence — flagged as an explicit ASSUMPTION,
not a silent gap**: DG certification for whoever is actually handling
dangerous goods aboard (a loadmaster, an AME) is now untrackable
through this system for Air Eagle. `flights.cargo_dg` flags a flight
as carrying DG; `crew.dg_expiry` exists as a qualification field — but
neither LM nor AME has a crew record anymore, and a free-text occupant
name can't be cross-referenced against either column. On a cargo
airline, someone handling DG with lapsed certification is a real
regulatory exposure, separate from FTL (which they were already
exempt from). Accepted deliberately on the operator's stated position
that OCC handles this by process, not by this system — same reasoning
already used for the type_rating_expiry/contract_expiry removal. If DG
tracking through this system ever matters, reintroducing LM/AME as
crew records (even with no FTL applicability at all) is the fix, not
a workaround bolted onto the free-text fields below.

**2. `roster_coverage()` reshaped — supersedes the CPT/FO/LM/ENGR
comma-joined design from the previous piece entirely.** New columns,
exactly as specified: Date | Flight | Route | CPT | FO | Other
occupants — operating | Other occupants — non-operating | POB |
Remarks.

- Coverage is CPT/FO only, matching item 1 above — a cockpit column
  shows `UNCOVERED` only when that seat has no assigned crew_id.
  Neither occupant column can ever produce `UNCOVERED`; they're
  informational, not a coverage check.
- "Operating" = aboard and performing a function (a loadmaster working
  the load, an AME on maintenance duty); "non-operating" = aboard but
  not working, reason (if any) goes in Remarks. Both are plain OCC-
  entered free text — two new nullable columns on `flights`
  (`other_occupants_operating`, `other_occupants_non_operating`,
  migrations/010), not a structured occupants table, not a category
  list. `roster_coverage()` does not parse, classify, or cross-
  reference these against `crew` at all — "the system doesn't classify
  why someone is aboard" is the operator's own stated position, and
  the code takes that literally: it displays whatever text is there.
- POB is the one place free text gets interpreted, and only to count
  heads: 2 cockpit crew (or however many of the 2 seats are actually
  filled — not hardcoded to 2, since an `UNCOVERED` seat must not
  silently count as a person) plus every name in both occupant
  columns. OCC's own real shorthand for "more than one person in a
  single free-text entry" — confirmed twice now, first as "Eng: 2x
  VAI" in the 2026-07-21 real data, now again as the operator's own
  "2x AME" example — is recognized by a small `Nx ROLE` prefix pattern
  (`_count_occupants()`), so "Abdulghani (LM), 2x AME" correctly counts
  as 3 people, not 2 comma-separated segments.
- No UI page writes to the two new `flights` columns yet —
  `flight_service.py`'s `UPDATABLE_FIELDS` accepts them (so
  `add_flight()`/`update_flight()` CAN set them), and `roster_coverage()`
  reads them, but nothing in `pages/3_Flight_Log.py`'s form collects
  them today. Flagged in `Open stubs`, not silently left half-built:
  OCC has nowhere to actually enter this data through the UI yet.

**3. "VAI" resolved — the operator confirmed it's just AME**, not a
separate term needing its own investigation. The open question this
used to be (`services/assistant/reports.py`'s notes referenced "Eng:
2x VAI" from real 2026-07-21 flight data with unconfirmed meaning) no
longer applies — item 2's reshape removes every reference to it from
the codebase (the old comma-joined ENGR column and its notes entry are
gone entirely), and the open question is removed from `Open stubs`
above.

**4. Age-65 rule wording CONFIRMED, still NOT built.** The operator
confirmed the exact regulatory wording already recorded in "Next
safest step" item 7 is correct: domestic requires at least 1 pilot
under 65 (illegal only if BOTH are 65+); international requires BOTH
pilots under 65 (illegal if EITHER is 65+). Still blocked on the same
architecture question as before — assignment happens one crew member
at a time today, and this rule needs to see both pilots on a rotation
together before it can evaluate anything. Not part of this piece's
scope; recorded here only to mark the wording itself as settled, not
open for re-litigation.

**5. Auth stays parked — a deliberate deferral, recorded as such, not
an oversight.** A plan was proposed 2026-08-02 covering roles, where
the checks would go (`require_login()`/`require_permission()` at the
top of `app.py` and all four pages), how it would interact with
`app_user` in the audit trail (currently always `None` at every page
call site — the plan would thread the authenticated username through
instead, with no service-layer signature changes needed, since every
write function already accepts `app_user: Optional[str] = None`), and
what would happen to the existing 4 page-level `AppTest`-based test
files (each would need its shared `page_app` fixture to pre-seed
`session_state` with an authenticated test user, consolidated into one
place rather than duplicated 4 times). Two genuine architectural
questions — permission granularity (single tier vs. a CONTROLLER/ADMIN
split) and whether login needs to survive a hard browser refresh —
were sent to the operator and have not come back yet. Not implemented
pending that answer; see `Open stubs` above.

**Tests**: 25 new/changed — 6 in `tests/test_import_crew_script.py`
(parametrized across LM/AME spelling variants: `LM`, `Loadmaster`,
`Load Master`, `ENGR`, `AME`, `Engineer`, case-insensitive; plus the
direct regression test that a Loadmaster row which is ALSO misaligned
still reports as excluded-for-being-LM, not misaligned — the reason
reported must be the real, permanent one); 3 changed in
`tests/test_assistant_reports.py` (`roster_coverage`'s new column
shape, `UNCOVERED` triggering only on a missing cockpit seat and never
on occupant columns, POB counting including the "2x AME" shorthand,
cancelled-flight exclusion retained unchanged). `_count_occupants()`'s
"Nx ROLE" parsing was additionally confirmed correct by direct
interpreter execution in this sandbox (`"Abdulghani (LM), 2x AME"` ->
3), same discipline as the boundary tests in the previous piece.

**Verification status**: 313 total (301 already on `main`, + 12 net
new — the import-script file grew from 12 to 23 tests, +11; the
report tests grew by 1 net, two old tests replaced by two new ones).
`pytest tests/`: 139 passed, 174 skipped (no `TEST_DATABASE_URL` in
this sandbox — the DB-integration tests here are traced by hand, not
run, same limitation as every prior piece). `check_reachability.py`:
`services/assistant/reports.py` still the only file flagged, unchanged
by this piece. **RESOLVED 2026-08-02**: independently verified by the
user against real Postgres 16 — 313/313 passing, migration 010
confirmed to apply cleanly against a database already carrying
000-009 and existing flight data, LM/AME exclusion confirmed to hold
on well-formed rows, `_count_occupants()` confirmed correct. Merged
into `main`.

## 2026-08-02 (continued): close the LM/ENGR role-dropdown
## inconsistency in pages/2_Crew_Data.py

Small, focused follow-up flagged in this file's own `Open stubs` after
the previous piece: `scripts/import_crew_from_xlsx.py` permanently
excludes LM/AME/ENGR from bulk import, but `pages/2_Crew_Data.py`'s
manual "Add crew member" form still listed `LM`/`ENGR` in
`ROLE_OPTIONS` — a controller could still hand-create one of exactly
the crew records the operator just said Air Eagle doesn't track.

Fixed on branch `crew-data-role-dropdown-cpt-fo-only`, off `main` at
the merge commit above: `ROLE_OPTIONS` narrowed from `["CPT", "FO",
"LM", "ENGR", "Other"]` to `["CPT", "FO", "Other"]`. "Other" stays —
it's the escape hatch for a genuinely unanticipated role the operator
hasn't described yet, not for LM/AME, which now have a specific,
deliberate answer. `services/crew_service.py`'s `ROLE_SYNONYMS`/
`ROLE_PREFIXES`/`_normalize_role()` are untouched, same reasoning as
the import-script change: this is a page-level, Air-Eagle-specific
restriction on what this one form offers, not a platform-wide rule —
FTLguard itself still fully supports LM/ENGR crew records for a future
client.

**Test**: 1 new, `test_role_dropdown_excludes_lm_and_engr` in
`tests/test_crew_data_page.py`, asserting the role selectbox's actual
options via Streamlit's `AppTest` framework (`at.selectbox[0].options
== ["CPT", "FO", "Other"]`) rather than just checking the module-level
constant — the same UI-driving discipline this file already uses for
the rest of `test_crew_data_page.py`. No existing test in that file
referenced LM/ENGR selection, so nothing else needed changing.

**Verification status**: 314 total on this branch's own original base
(313 on `main` + 1 new), 139 passed / 175 skipped locally at the time.

**Re-verified 2026-08-02 after merging `main`, per instruction — not
merged on the strength of the original 314 figure**: this branch
predated Step 6 (transactional atomicity) and Step 7 (age-pairing
rule), neither of which it had ever run against. `main` merged into
this branch (one conflict, in this file's own overlapping narrative
sections — resolved by combining both, not discarding either); full
suite re-collected and re-run here: 339 total (338 from `main` + this
branch's own 1), 139 passed / 200 skipped locally (no
`TEST_DATABASE_URL` in this sandbox, same limitation as always).
`check_reachability.py`: unchanged — `services/assistant/reports.py`
still the only file flagged. See `Current active task` near the top
of this file for merge status.

## 2026-08-02 (continued): Step 6 — transactional atomicity for
## Control Room's flight+assignment write, extended to
## assign_crew_to_duty() too. NOT MERGED.

Plan proposed and approved (with one addition, one extension, and one
docstring clarification) before implementation. Built on branch
`transactional-atomicity-control-room-write`, off `main` at the
`operator-crew-scope-and-coverage-reshape` merge commit.

**The problem, confirmed by reading the code before proposing anything**:
`assign_crew_to_new_flights()`'s ALLOWED path (Control Room's ad-hoc
flight-creation-plus-assignment) was FOUR separate, independently-
committed transactions: `INSERT INTO flights`, `log_audit(FLIGHT_ADDED)`
(which opens its own transaction internally), `INSERT INTO roster`,
`log_audit(ASSIGNMENT_CREATED)`. The existing "no orphan flight on
rejection" guarantee is real and tested — nothing is written at all on
ILLEGAL/NEEDS_MANUAL_REVIEW — but once the gate passes and writing
starts, a crash between steps 1 and 3 left a real, committed, uncrewed
flight sitting in Flight Log: the exact orphan the gate exists to
prevent, just relocated to a later failure window.

Also confirmed directly: `assign_crew_to_duty()` (the Roster page's
path, assigning crew to a flight that already exists) doesn't have the
orphan-FLIGHT version of this problem — only one table is ever written
there (`roster`) — but it has the same class of gap at smaller scale:
its own roster insert and its own `ASSIGNMENT_CREATED` audit call were
two separate transactions, so a crash between them left a committed
roster row with no audit trail for it. `_check_downstream_impact()`
was confirmed read-only (only reads and returns candidates for
display; "alert + suggest, human confirms" is the existing, deliberate
design, not an auto-write) and correctly stays outside any transaction,
run after commit so it can see the newly-written duty.

**Fix**: `services/audit_service.log_audit()` gained an optional `conn`
parameter. When passed an already-open `Connection` (e.g. from
`with engine.begin() as conn:`), it executes the audit INSERT directly
on that connection instead of opening its own transaction — folding
the audit write into the caller's transaction. When `conn` is `None`
(the default, and every pre-existing call site across the codebase),
behavior is unchanged. Both `assign_crew_to_new_flights()` and
`assign_crew_to_duty()` now wrap their entire write sequence — data
insert(s) plus every `log_audit()` call in that sequence — in one
`engine.begin()` block, passing `conn=conn` through. Either every write
in the sequence commits, or none does.

**Explicit behavioral note, added to `log_audit()`'s docstring per
review feedback**: passing `conn` means the audit record shares the
CALLER's transaction fate — if the caller's transaction later rolls
back, the audit record disappears with it, unlike every other call to
this function (which always commits independently regardless of what
happens around it). This is the correct behavior here (an audit record
for a write that never actually happened would be worse than no record
at all), but it's a genuine difference from the default that needed
stating explicitly rather than left to be discovered by surprise.

**Included per review feedback**: the `assign_crew_to_duty()` extension
was originally offered as optional/your-call in the proposed plan;
approved for inclusion in this same piece rather than split out,
specifically because leaving it out would mean two write paths with
different transactional guarantees for no discoverable reason, and a
committed roster row with a lost audit entry is a real gap in a
permanent regulatory record.

**Out of scope, unchanged from the plan**: `_recompute_one_duty_after_delay()`
/ `update_flight_actual_times_and_revalidate()` (Step 5's delay-recompute
path) and `cancel_flight_and_roster()` (Step 5's cancel cascade) have
the same write-then-separately-audit pattern. Not touched here — a
broader audit-atomicity pass across every write path is a separate,
deliberate piece of work.

**Tests**: 5 new. `tests/test_assignment_service.py` gained the direct
regression test — monkeypatches `assignment_service.log_audit` so its
SECOND call (`ASSIGNMENT_CREATED`) raises mid-sequence, calls
`assign_crew_to_new_flights()` on an otherwise-LEGAL scenario, and
confirms BOTH `flights` and `roster` are still empty afterward (the
flight insert and the first audit call genuinely "succeeded" moments
before the simulated crash, and must not survive the rollback) — plus
the `assign_crew_to_duty()` counterpart (roster insert must not survive
a crash at its own audit call). `tests/test_audit_service.py` gained
three tests for `log_audit()`'s new parameter, added per review
feedback rather than assumed obvious: `conn=` genuinely writes a
queryable row on the normal success path (not just "raises no
exception" — a conn-passing bug that silently never executes on the
passed connection would only show up as missing audit rows in
production, so this is the test that would catch exactly that);
`conn=` shares the caller's rollback (the audit row is confirmed gone
after a forced rollback); and the no-`conn` default still commits
independently, unaffected by the new parameter's existence.

**Verification status**: 318 total (313 on `main`, cut directly from
`main` rather than the still-unmerged `crew-data-role-dropdown-cpt-fo-only`
branch, + 5 net new). `pytest tests/`: 139 passed, 179 skipped (no
`TEST_DATABASE_URL` in this sandbox — the new regression tests are
traced by hand here, not run; the logic was reasoned through directly
against `engine.begin()`'s standard rollback-on-exception semantics,
which this fix relies on rather than reimplements). `check_reachability.py`:
unchanged — `services/assistant/reports.py` still the only file
flagged. (Merge status: see `Current active task` near the top of this
file, not this line — this branch has since merged; see there.)

## 2026-08-02 (continued): Step 7 — the age-pairing rule
## (AE-CREW-PAIR-AGE-001)

Design conversation happened first (plan proposed covering where the
check runs, how a rotation's complete flight-deck package gets
identified, and what happens with only one pilot assigned; approved
with one addition, one extension, and one scope note) before any code
was written. Built on branch `age-pairing-rule-ae-crew-pair-age-001`,
off `main` at the Step 6 merge commit.

**The architectural blocker, resolved**: assignment happens one crew
member at a time, so there's no single call where both pilots on a
rotation are known simultaneously. Confirmed by reading the code
before proposing anything: `roster.duty_id` is a fresh random UUID
generated fresh on every assignment call — there is no existing
identifier linking two different crew members' roster rows as "the
same rotation." The only real structural link between a CPT's
assignment and an FO's assignment to what a human would call the same
rotation is that they cover the identical SET of `flight_id`s. New
query helper `_find_paired_pilot()` in `services/assignment_service.py`
keys off exactly that: it looks for another active (non-cancelled)
CPT/FO roster assignment whose own `flight_id` set is an EXACT match
to this duty's — not just any overlap, since two pilots could each
have a roster row referencing the same single `flight_id` while
actually flying different, unrelated duties.

Also confirmed: `assign_crew_to_new_flights()` (Control Room) can
never see an already-paired pilot — it creates the flight in the same
call, so nothing could already be assigned to a `flight_id` that
doesn't exist yet. `assign_crew_to_duty()` is the real enforcement
point (used both for the Roster page's normal flow and for adding a
second Control-Room pilot to a flight the first call already created).
Both callers go through the same shared `_validate_new_duty()`, so the
check is wired in once, not twice — it simply always resolves to
"nothing to check yet" on the Control Room path, automatically,
without any special-casing.

**Where the check runs and what happens with only one pilot assigned**:
`_validate_new_duty()` gained an optional `flight_ids` parameter
(`assign_crew_to_duty()` passes its real list; `assign_crew_to_new_flights()`
passes `None`) and now calls new `_check_crew_pairing_age()`, scoped to
CPT/FO only (LM/AME return immediately, untouched). Three outcomes:
nobody on the other seat yet -> nothing is blocked, and — the one
addition to the original plan, approved on review — nothing is silently
lost either: `AssignmentResult` gained `pairing_pending`,
`paired_crew_id`, and `pairing_constraint` fields (populated regardless
of status, same convention `computed_report_time` etc. already
established there), NOT a `RuleAlert`. A `WARNING`-severity alert would
have elevated `ValidationResult.status` from `LEGAL` to `WARNING` for
every single first-pilot assignment to any rotation — a false alarm
baked into the common case, exactly the kind of thing that teaches
people to ignore warnings. Paired pilot found but a DOB is missing on
either side -> `AE-CREW-PAIR-AGE-001_DOB_MISSING`,
`NEEDS_MANUAL_REVIEW`, matching HANDOVER.md's already-settled wording.
Paired pilot found, both DOBs known, rule evaluates ILLEGAL ->
`AE-CREW-PAIR-AGE-001_AGE_LIMIT`, blocking the SECOND pilot's own
assignment — a real, order-dependent consequence: whichever pilot
happens to be assigned first is never blocked by this rule at that
moment, since there's nothing yet to compare against.

**`pairing_constraint` — the actionable addition from review**:
populated only when `pairing_pending` is true AND the lone assigned
pilot is already 65+ — the real operational trap the review caught: a
controller assigns a 67-year-old to an international rotation (or a
domestic one) and it's accepted, since nothing to compare against
exists yet; if a second pilot is never assigned, an illegal-by-
composition rotation would otherwise sit in the roster looking
completely fine, with one seat legitimately still open. Getting the
message right required working through the actual arithmetic rather
than assuming symmetry between the two route classifications: for
**domestic**, if the lone pilot is already 65+, the pair is legal iff
the other seat is under 65 — a real, satisfiable constraint, and the
message says so. For **international**, if the lone pilot is already
65+, the pair is ALREADY illegal ("illegal if EITHER pilot is 65+")
**regardless of who fills the other seat** — there is no age that
fixes it. The message for this case says exactly that (no valid second
pilot exists), rather than the weaker and factually wrong "find someone
under 65."

**UI surfacing folded into this piece, not deferred**: both
`pages/1_Control_Room.py` and `pages/4_Roster.py` already had the
branching structure for this (the same `if result.status == ...`
ladder every other check already renders through) — added a small
block showing `pairing_constraint` as a warning when present, a plain
info note when pairing is pending with no constraint yet, and a quiet
confirmation of who the pairing was checked against when it resolved
legally.

**Explicitly out of scope, per the plan and confirmed on review**:
`find_legal_candidates_for_duty()` (downstream-swap candidate search)
does not check age-pairing for a candidate against whoever's on the
other seat of the future duty being protected — noted in `Open stubs`
above as a real gap, not a silent oversight. No schema change to make
`crew.date_of_birth` required for CPT/FO — missing DOB is still caught
at assignment time via `NEEDS_MANUAL_REVIEW`, consistent with "never
silently block on missing data, flag it instead."

**Tests**: 20 new. Pure logic (no DB, in
`tests/test_assignment_service.py`): `_age_on()`'s exact-65-boundary in
both directions (turning 65 ON the reference date counts as 65, the
day before is still 64); `_evaluate_pair_age()` across all 6
domestic/international x age combinations from the settled wording;
`_pairing_constraint_message()`'s domestic ("other seat must be under
65") vs international ("no second pilot can fix this") distinction —
confirmed correct by direct interpreter execution in this sandbox,
same discipline as prior pieces' boundary tests. DB-integration (9):
first pilot alone -> `pairing_pending`; first pilot alone at 65+ ->
`pairing_constraint` populated, domestic and international variants;
second pilot domestic both 65+ -> `REJECTED`, nothing extra saved;
second pilot domestic one under 65 -> `ALLOWED`; second pilot
international one 65+ -> `REJECTED` (stricter than the identical
domestic composition, which would be `ALLOWED`); missing DOB on the
already-assigned pilot -> `NEEDS_REVIEW`; LM/AME assignment never
triggers any of this; reassignment (cancel the first FO, assign a
new one) re-evaluates against the current pairing, not the cancelled
row, confirming `_find_paired_pilot()`'s `status != 'CANCELLED'` filter
actually works.

**Verification status**: 338 total (318 on `main` + 20 new). `pytest
tests/`: unchanged pass/skip ratio, no new failures — the 9 new DB-
integration tests are traced by hand here (no `TEST_DATABASE_URL` in
this sandbox), and the pure-logic tests were additionally confirmed
correct by direct interpreter execution before being written into the
test file. `check_reachability.py`: unchanged, no new files —
everything lives inside the existing `services/assignment_service.py`.

**One real-Postgres test failure found and fixed, not a bug in the
rule itself**: the user's first verification run found 337/338
passing — `test_delay_recompute_handles_multiple_crew_on_same_flight_
independently` failed. Root cause: `_add_crew()`'s `_QUALIFICATION_DEFAULTS`
sets all 8 expiry fields but never `date_of_birth`, so every pilot
created by this file's tests has always had a NULL DOB. That test
assigns both a CPT and an FO to the same flight through the real
`assign_crew_to_duty()` API — the second assignment correctly hit the
brand-new `AE-CREW-PAIR-AGE-001_DOB_MISSING` -> `NEEDS_MANUAL_REVIEW`
path and was correctly NOT written, so the delay recompute this test
was actually about found 1 crew member instead of 2. The age-pairing
rule was doing exactly what it's supposed to; the test fixture was
underspecified for a check that didn't exist when it was written.
Fixed by adding `"date_of_birth": dt.date(1980, 1, 1)` to
`_QUALIFICATION_DEFAULTS` (comfortably under 65 for every scenario in
this file) and giving `test_second_pilot_missing_dob_needs_review` an
explicit `date_of_birth=None` override, since that's the one test that
deliberately needs the NULL case now that it's no longer the ambient
default. Confirmed no other test in this file references
`date_of_birth` at all, so nothing else depended on the old default.
Also independently confirmed by the user directly against real
Postgres with real crew ages: domestic 67+67 -> second pilot REJECTED;
domestic 67+41 -> ALLOWED; international 67+41 -> second pilot REJECTED
(correctly stricter than the identical domestic composition);
`pairing_pending`/`pairing_constraint` populate correctly on the first
assignment in all three cases.

## 2026-08-04: two deferred decisions recorded — auth spec settled,
## Supabase free-tier deferral confirmed. No code, record only.

Both were actually settled in discussion but never written down here,
and nearly got lost across the two-day gap since the last entry —
recorded now specifically so that doesn't happen again. See the
updated `Open stubs` bullets above for the living, check-this-first
version of both; this entry is the narrative record of how they got
decided.

**1. Auth.** The two questions the 2026-08-02 plan had been blocked on
are answered: three accounts, all full access, no permission tiers —
the app isn't publicly reachable, so tiering doesn't buy anything real
yet. Session-level login (re-login on a hard refresh) is fine for
three OCC staff; no cookie-persistence mechanism needed. Still not
built. The part of this worth taking seriously isn't the login screen
itself — it's that `audit_log.app_user` is `NULL` on literally every
row in this system today, because no page has ever passed a real user
identity through to `log_audit()`. The audit trail records what
happened and when, never who, on a PCAA-regulated operator's permanent
regulatory record. When auth gets built, threading the logged-in
user's identity into every `app_user` parameter (already present on
every write function's signature, just never populated) is the actual
point, not a side effect of adding a login screen.

**2. Supabase free tier.** Deliberately staying on it: no automated
backups, and the project auto-pauses after 7 days idle. Both accepted
because the database currently holds nothing real. Backup research
done and worth keeping on record rather than redoing:
- Supabase's own documentation recommends free-tier projects export
  via the `supabase db dump` CLI command and keep the result
  off-site — one binary, no full Postgres install needed.
- Point-in-time recovery (PITR) was priced and ruled out: ~$100/mo for
  7-day retention, about 4x the Pro plan itself — disproportionate for
  this operation's actual scale — and it REPLACES daily backups rather
  than adding to them.
- Deleting a Supabase project destroys its backups permanently,
  including anything already in S3 — the reason an independent
  off-site `db dump` is worth keeping even after upgrading to Pro, not
  just as a free-tier workaround.
- Restoring from any backup, at any tier, takes the project offline
  for the duration of the restore — worth knowing before it's an
  emergency, not during one.

**Both share one trigger, and it's the same trigger**: the moment any
real crew or flight data enters the production Supabase database. At
that point all three — the Pro plan upgrade (~$25/mo, daily backups,
7-day retention), auth, and backups — land together, not piecemeal.
Real data existing at all is what makes every one of these matter;
none of them is worth doing in isolation before that.

## 2026-08-04: OR-Tools CP-SAT decision REVERSED — a direct assignment
## loop instead. Record only, no code (the generator itself isn't
## built either way).

The 2026-07-19 entry above adopted Google OR-Tools CP-SAT for the
28-day roster generator's assignment optimization. That decision
predates knowing Air Eagle's actual scale. Measured: 36 rotations per
28 days, FO load 9 duties each across 4 FOs, CPT load 6 each across 6
CPTs. Comfortably within a direct assignment loop's reach: walk
rotations in date order, pick the eligible candidate with fewest
duties assigned so far (the fairness rule — see below), check legality
through the existing `core/legality/pcaa_ano012_core.py` validator via
the existing `assign_crew_to_duty()` gate, move to the next candidate
on ILLEGAL, mark the rotation UNCOVERED if none work (an already-
established, named state — see the 2026-07-21 requirements-doc entry).

Reasons beyond simplicity, both concrete: (1) CP-SAT would require
re-expressing FTL rules in solver-constraint form — a second, parallel
rule representation, precisely the "two sources of truth" failure this
entire rebuild exists to prevent. `core/legality/pcaa_ano012_core.py`
is supposed to be the ONE FTL engine; a generator with its own solver-
encoded copy of D9/D21/D8.2.1 would be exactly the kind of drift this
project's own hard-lessons catalogue already warns about. (2) A direct
loop can explain a rejection the same way an interactive assignment
already does today — a real `RuleAlert` message like "needs 21.5h
rest, only 13.25h available" — reusing infrastructure that already
exists. A solver's INFEASIBLE result explains nothing without separate,
additional work to reconstruct a reason.

**Not implemented here** — the generator itself (however the direct
loop turns out to be shaped in detail) is still not built, same as
before this reversal. This entry exists so a future session doesn't
resume Phase 7 assuming CP-SAT is still the plan; see the 2026-07-19
entry above for what it originally said, left as-is rather than
rewritten, per this file's own discipline: a dated entry records what
was true and decided at the time, corrections get their own entry, not
a silent rewrite of history.

## 2026-08-04 (continued): Phase 7 groundwork — recurring schedule
## templates (the template layer only). NOT MERGED.

Plan proposed first (schema, expansion, coexistence with Control Room,
mid-window template changes), reviewed against real Postgres 16 by the
user before implementation (the `EXCLUDE` constraint specifically —
see below), approved with three corrections, then built. Branch
`rotation-templates-phase7-groundwork`, off `main` at the age-pairing-
rule/crew-data-dropdown merge point.

**Why templates, not Flight Log inference — confirmed by reading the
code, not assumed**: `services/assignment_service.py`'s
`_validate_new_duty()` generates `roster.duty_id` as a fresh
`uuid.uuid4()` at ASSIGNMENT time — nothing in `flights` declares which
rows belong to one rotation today. A generator inferring rotation
membership from aircraft/timing/continuity would be guessing, and a
wrong guess here produces a silently wrong FDP — already this
project's most-repeated bug class (`migrations/003_roster_table.sql`'s
own header warns about exactly this). Templates make rotation
membership declared data instead of an inference.

**Grounding, independently re-derived against the real engine, not
trusted from the numbers as given**: both real rotations' report/
debrief/FDP/rest numbers were hand-recomputed against
`core/duty_builder.py`'s actual constants
(`DOMESTIC_PRE/POST_FLIGHT_MINUTES` 45/15,
`INTERNATIONAL_PRE/POST_FLIGHT_MINUTES` 60/30) and confirmed exact —
EPE 786/787 (domestic): report 1815Z, debrief 0000Z, FDP 5.75h, D21
rest 12h (floor wins); EPE 802/804/805 (international): report 0045Z,
debrief 1130Z, FDP 10.75h, D21 rest 21.5h (scales above the floor).
Confirmed independently again through `core/rotation_expansion.py`'s
actual `expand_template()` output fed through the real, unchanged
`build_duty()`/`required_rest_minutes()` — see
`tests/test_rotation_expansion.py`. This confirms `core/duty_builder.py`
needed ZERO changes: expansion's only job is producing `FlightLeg`-
shaped data that flows through the existing, already-tested engine
unchanged. Also confirms neither rotation's own LEGS cross a UTC
calendar day — only the domestic duty's DEBRIEF crosses midnight, a
buffer-addition side effect, not a leg-level date rollover — and
`_check_crew_qualifications()` already checks against
`duty_result.debrief_time.date()`, not report date, so that midnight
crossing was already handled correctly with no change needed there
either.

**Already-agreed requirement this piece implements the first half
of**: the 2026-07-21 requirements-doc entry already records "rostering
workflow (draft -> OCC review -> publish, crew sees only published, no
silent reshuffle on regeneration, explicit UNCOVERED state)" as
confirmed. This is the "draft" half. Review/publish and the generator
itself are later pieces, explicitly out of scope here. Also noted: that
same requirements-doc entry's "1 CPT, 1 FO, 1 LM, 1 AME" crew package
predates the 2026-08-02 CPT/FO-only decision — templates plan for
CPT+FO only; LM/AME are free text at Flight Log time
(`flights.other_occupants_operating`/`_non_operating`, migrations/010),
never templated.

**Schema** (migrations/011_rotation_templates.sql — this repo's first
use of a database trigger, an `EXCLUDE` constraint, and a Postgres
extension): `rotation_templates` (rotation identity, `days_of_week`,
`effective_from`/`effective_until`, `version`, `superseded_by`),
`rotation_template_legs` (ordered legs as TIME-of-day + `day_offset`,
no calendar date — a template has none of its own), `rotation_instances`
(the draft layer, one row per calendar occurrence, `status` DRAFT/
APPROVED/REJECTED), `rotation_instance_legs` (real computed datetimes).
`flights` gains one nullable `rotation_instance_id` column for future
traceability — NULL for every Control Room ad-hoc flight, unset by
anything in this piece; promotion (DRAFT -> real `flights` rows via
the existing `flight_service.add_flight()`) is a later piece.

**The overlap gap caught on review, closed with two layers, not one**:
the original plan's `effective_until` was nullable-open-ended with no
mechanism actually preventing two versions of the same rotation from
both being effective on the same date — `superseded_by` recorded the
relationship but didn't bound it. Closed with (1) `create_new_version()`
atomically closing the prior open version's `effective_until` to the
day BEFORE the new version's `effective_from` (not the same day — see
below), and (2) the actual guarantee, a `daterange` `EXCLUDE` constraint
(`btree_gist`) on `rotation_code`, treating a NULL `effective_until` as
infinity, making an overlap impossible even if the service-layer path
is bypassed entirely. **Verified against real Postgres 16 by the user
before implementation**: `btree_gist` available, the constraint creates
cleanly, an open-ended v1 + a later v2 is correctly rejected, and the
`create_new_version()` ordering (close-then-insert) succeeds. The
'`[]`' inclusive bound means a v1 ending 2026-08-31 and a v2 starting
the SAME day (2026-08-31, not 2026-09-01) is also rejected — a single
day cannot belong to two versions — confirming `create_new_version()`
must close to the day before the new `effective_from`, exactly as
built; both the adjacent-accepted and same-day-rejected cases are
directly tested (`test_adjacent_non_overlapping_versions_are_accepted`,
`test_same_day_boundary_is_rejected_not_accepted`).

**Immutability, also two layers, refined during implementation beyond
what was reviewed**: `rotation_template_legs` is a hard block on every
UPDATE/DELETE via trigger — legs never have a legitimate reason to
change after insert. `rotation_templates` needed a narrower rule
discovered while implementing, not the original blanket-immutable
design: `create_new_version()`'s own job is legitimately updating the
prior version's `effective_until` (NULL -> a date) and `superseded_by`
— a blanket "reject every UPDATE" trigger would have blocked the one
operation the whole overlap-prevention mechanism depends on.
`guard_rotation_templates_mutation()` allows exactly that one
transition (and rejects re-editing an already-closed `effective_until`
a second time) while still rejecting every other column change and
every DELETE — tested directly
(`test_rotation_templates_arbitrary_update_is_rejected`,
`test_rotation_templates_cannot_reopen_or_re_close_effective_until`).
Both triggers enforce this even against direct SQL, not just through
`rotation_template_service.py` — "by convention" was explicitly
rejected as insufficient on review, this project's own history with
`ensure_basic_crew_table()` cited as the reason not to repeat it.

**Why drafts are persisted, not computed on demand**: `status`
(DRAFT/APPROVED/REJECTED) and immutability-anchoring (an approved
instance must permanently reference the exact template version that
produced it, even after that version is later superseded) both need
something durable to attach to — a value that only exists in memory
during a computation can't be approved, rejected, or referenced by a
later `flights.rotation_instance_id`. Idempotent re-expansion also
needs persisted state to check against — an on-demand recomputation
has no memory of what a prior run already offered.

**Coexistence with Control Room**: two independent origins, one
convergence point (`flights`, still owned exclusively by
`flight_service.py` — Ownership Table unchanged). Ad-hoc flights:
`rotation_instance_id = NULL`. Template-approved flights (once
promotion exists, a later piece): `rotation_instance_id` set, traceable
to the exact version and date that produced them. Nothing in this
piece writes to `flights` at all.

**Mid-window template change vs. already-published assignments**:
structurally impossible for a version change to reach a published
roster, not just discouraged by convention — roster rows reference
real `flights` rows, which only ever get created from an already-fixed
`rotation_instance` at approval time (a later piece). A new template
version can only affect `rotation_instances` that don't exist yet;
anything already approved is permanently anchored to the version that
produced it, by the data model itself, confirmed directly
(`test_expand_and_persist_after_new_version_only_adds_forward_never_touches_existing`
manually marks an instance APPROVED, creates a new version, re-expands,
and asserts that instance is byte-for-byte untouched).

**Fairness, recorded for the later generator, not built here**: even
duty counts within each role only, not balancing international vs.
domestic exposure.

**Explicitly out of scope**: the generator itself; the approval/publish
workflow (DRAFT -> APPROVED, promoting legs into real `flights`, crew
notification); any UI for managing templates or reviewing drafts;
WhatsApp notification and Excel-export-for-rosters (already flagged
elsewhere as separately not-yet-built).

**Tests**: 26 new. Pure logic (`tests/test_rotation_expansion.py`, 10,
genuinely run here and passing): both real rotations' exact hand-
verified numbers re-derived through the actual engine; `days_of_week`
filtering; leg ordering independent of input order; `day_offset`
rolling a date forward (neither real rotation needs this, confirmed
the schema's claim to support it anyway); a leg crossing midnight
within one `day_offset` correctly rejected (the deliberate scope
limit); empty-input and `date_from > date_to` validation; a window
with no matching weekday producing an empty list, not an error.
DB-integration (`tests/test_rotation_template_service.py`, 16, traced
by hand — no `TEST_DATABASE_URL` in this sandbox): `create_template()`/
`create_new_version()` correctness; the `EXCLUDE` constraint's overlap
rejection, adjacent-acceptance, and same-day-boundary-rejection cases
described above; a different `rotation_code` never conflicting; both
immutability triggers on both tables, including the narrow legitimate-
transition case; `expand_and_persist()`'s idempotency and its forward-
only behavior across a version boundary.

**Verification status**: 365 total (339 on `main` + 26 new). `pytest
tests/`: 149 passed, 216 skipped locally (no `TEST_DATABASE_URL` in
this sandbox) — the 10 pure-logic tests genuinely pass here (confirmed,
including hand-verification against `core/duty_builder.py`'s real
constants); the 16 DB-integration tests are traced by hand, not run —
flag this explicitly, and flag the migration itself for extra scrutiny
beyond the usual verification pass: this is the first trigger, first
`EXCLUDE` constraint, and first extension (`btree_gist`) anywhere in
this schema. `btree_gist` and the `EXCLUDE` constraint were confirmed
working against real Postgres 16 by the user BEFORE this was built,
but `btree_gist`'s availability on Supabase specifically (not just
local Postgres) is not yet confirmed — check before assuming the
migration will apply there unchanged. `check_reachability.py`: **run, not assumed** —
`core/rotation_expansion.py` is correctly NOT flagged (imported by
`services/rotation_template_service.py`, a real cross-module import),
but `services/rotation_template_service.py` itself IS newly flagged:
`check_reachability.py` only counts imports from `pages/`/other
`services/`/`core/` files, not from tests, and nothing in `pages/`
calls this service yet (correct — there's no template-management UI,
same as `services/assistant/reports.py`'s own unwired state). Two
files flagged now, both correctly so, both for the same reason: built
ahead of being wired into a page.

**Two real-Postgres verification failures found and fixed (7 of 365):
one a genuine bug, one a test asserting the wrong exception class.**

**Bug (2 failures) — `create_new_version()`'s circular dependency,
unresolved by the original (non-deferred) `EXCLUDE` constraint.** The
new version's row must exist before the old version's `superseded_by`
can reference its id — so the code always inserted the new version
first. But at that exact INSERT, the old version was still
open-ended, and a plain `EXCLUDE` constraint checks on every
statement: two open-ended ranges for the same `rotation_code`
necessarily overlap at that instant, so the constraint correctly (by
its own non-deferred rules) rejected the INSERT with an
`ExclusionViolation`. The reverse order doesn't resolve it either —
`superseded_by` can't reference a row that doesn't exist yet. This is
a genuine circular dependency, not a simple ordering mistake; the
original docstring claimed the code closed the old version before
inserting the new one, which was simply wrong about what the code
actually did.

Fixed by declaring the constraint `DEFERRABLE INITIALLY DEFERRED`
(migrations/011) — the overlap check now runs once at COMMIT rather
than after every statement, so both the INSERT and the UPDATE can
happen in either order inside one transaction; only the final,
post-transaction state has to satisfy the constraint. The guarantee
itself doesn't move, only the timing of when it's checked — a genuine
overlap left at COMMIT (e.g. a v3 inserted while v2 is still open)
still fails. Verified directly against real Postgres 16, both
directions: the insert-then-close order now succeeds, and a real
overlap is still rejected at COMMIT. This is confirmed as the standard
Postgres pattern for exactly this "two rows must move together to
stay valid" shape, not a workaround specific to this schema.

**Wrong exception class (5 failures) — test-only, no code change.** A
PL/pgSQL `RAISE EXCEPTION` inside a trigger surfaces through SQLAlchemy
as `InternalError`, not `IntegrityError` — the five trigger tests
(`rotation_template_legs`'s update/delete block, `rotation_templates`'s
delete/arbitrary-update/re-close block) had asserted
`pytest.raises(IntegrityError)`, matching this file's two genuine
constraint-violation tests by habit rather than checking what a
trigger-raised exception actually surfaces as. The triggers themselves
were confirmed correct — right message, right rejection — this was
purely a wrong assertion. Fixed to `pytest.raises(DatabaseError)`, the
common SQLAlchemy parent of both `IntegrityError` and `InternalError`,
so the assertion doesn't need to know which of the two a given
Postgres error surfaces as. The two `EXCLUDE`-constraint tests (real
constraint violations, genuinely raising `IntegrityError`) were left
unchanged — confirmed correct as originally written, not swept up in
the same fix.

**Re-verification status**: not yet re-run against real Postgres by
the user as of this fix — pushed for that next. Locally: 365 total
unchanged (no tests added or removed, only the exception class
assertion changed in 5 and the migration/docstring in the `create_new_
version()` fix), 149 passed / 216 skipped (same DB-integration
limitation as always).

**Still open, unresolved by this fix**: `btree_gist`'s availability on
Supabase specifically remains unconfirmed. Worth checking before this
migration is the thing that discovers it isn't available there —
confirmed working on real Postgres 16 (unmanaged), but Supabase is
managed Postgres and extension availability on managed platforms isn't
guaranteed to match a self-hosted instance.

**RESOLVED 2026-08-04**: `btree_gist` confirmed available on Supabase
— `default_version 1.7`, `installed_version NULL` (not yet enabled,
which migrations/011's `CREATE EXTENSION IF NOT EXISTS` already
handles on its own). This was the last open risk on this piece.

## 2026-08-04 (continued): rotation_instance approval workflow —
## DRAFT -> APPROVED promotes a template into real operational flights

Plan proposed first (extend `add_flight()` the same way `log_audit()`
was extended in Step 6, promotion atomicity, idempotency, what's out
of scope), verified against merged `main` by the user before
implementation, approved with one addition after review. Branch
`rotation-instance-approval-workflow`, off `main` at the rotation-
templates-phase7-groundwork merge point.

**Why `add_flight()` needed the same `conn` treatment `log_audit()`
got in Step 6 — confirmed by reading the code, not assumed**:
`add_flight()` already inserted into `flights` inside its own
`engine.begin()`, then called `log_audit()` as a separately-committed
transaction — the exact same "data write, then separately-committed
audit" shape Step 6 fixed elsewhere. Control Room's ad-hoc path
bypasses `add_flight()` entirely with its own raw `INSERT`, specifically
because `add_flight()` couldn't join an existing transaction at the
time — unrelated to this change, still bypassing it, not touched here.
Fixed by giving `add_flight()` the identical `conn` parameter contract
`log_audit()` already has: when passed, both the `INSERT` and the
`FLIGHT_ADDED` audit record join the caller's transaction; default (no
`conn`) behavior is unchanged for every existing call site. This
incidentally closes `add_flight()`'s own version of the same gap Step
6 already fixed elsewhere, as a side effect of reuse, not separate work.

**`approve_instance()`/`reject_instance()`**, both in
`services/rotation_template_service.py` (the file already owns all
four `rotation_*` tables — `rotation_instances`' lifecycle belongs
there, not a new file). `approve_instance()`: one transaction,
all-or-nothing — loads a DRAFT instance's legs from
`rotation_instance_legs` (already carrying everything `add_flight()`'s
`REQUIRED_FIELDS` needs, guaranteed by that table's own `NOT NULL`
columns), calls `flight_service.add_flight(..., rotation_instance_id=
instance_id, conn=conn)` once per leg in order, then flips
`rotation_instances.status` to `APPROVED` and logs one
`ROTATION_INSTANCE_APPROVED` audit entry alongside the per-leg
`FLIGHT_ADDED` entries `add_flight()` already writes — same layered-
audit pattern Control Room's own ad-hoc path already uses. Idempotent
on an already-`APPROVED` instance: returns the existing `flight_ids`
(via `flights.rotation_instance_id`) rather than creating anything new
or erroring — same principle already established for
`expand_and_persist()`. `reject_instance()` requires `DRAFT`, sets
`REJECTED`, never touches `flights` at all — deliberately NOT
idempotent the way approve is (rejecting an already-rejected instance
raises; there's no prior side effect to safely return).

**The gap caught on review, closed with a real database constraint,
not left service-layer-only**: the original plan's duplicate-
promotion protection was `approve_instance()`'s idempotency check
alone. Reviewed against this piece's own established standard — the
template layer chose database-level guarantees (`EXCLUDE`, triggers)
for both of ITS invariants, explicitly rejecting "by convention" per
this project's `ensure_basic_crew_table()` history — service-layer-
only here would have been a visible inconsistency with that standard.
Closed with `(rotation_instance_id, flight_no)` being genuinely unique
per rotation: one rotation should never produce two flights sharing a
flight number. New migration `012_rotation_legs_flight_no_required.sql`
(011 is already merged and immutable — a new migration, not an edit):
`flight_no` changed from nullable to `NOT NULL` on both
`rotation_template_legs` and `rotation_instance_legs` (a template leg
describes a known, named, recurring rotation — unlike an ad-hoc Control
Room charter, where a missing flight number is legitimate and
`flights.flight_no` itself stays nullable, untouched), plus a PARTIAL
unique index on `flights (rotation_instance_id, flight_no) WHERE
rotation_instance_id IS NOT NULL` — mirroring this repo's own existing
precedent (`migrations/005_roster_partial_unique_index.sql`'s `WHERE
status != 'CANCELLED'`) rather than relying on plain `UNIQUE`'s
implicit NULL-handling, so ad-hoc flights are explicitly, visibly never
subject to this constraint. `create_template()`/`create_new_version()`
gained a matching service-layer check (`_validate_legs()`) rejecting a
missing `flight_no` with a clean `ValueError` before ever reaching the
database, same principle `add_flight()`'s own `REQUIRED_FIELDS` check
already follows.

**Explicitly out of scope**: un-approving an already-promoted instance
(APPROVED -> REJECTED, cancelling the flights it produced) — would need
the same kind of cascade `cancel_flight_and_roster()` (Step 5) already
does for one flight, generalized across a whole rotation, and needs its
own design once the generator exists and there's real crew to consider.
Per-leg field overrides at approval time (aircraft, DG flag, remarks,
LM/AME free text) — `approve_instance()` promotes exactly what
`rotation_instance_legs` already has; OCC can fill these in afterward
via the existing `update_flight()`. Any UI. The generator itself.

**Tests**: 17 new. `tests/test_flight_service.py` gained 3 for
`add_flight()`'s `conn` parameter, mirroring `log_audit()`'s own three
Step-6 tests exactly (writes a real row on the success path; shares the
caller's rollback; default behavior unaffected).
`tests/test_rotation_template_service.py` gained 14: `approve_instance()`
promoting every leg correctly with `rotation_instance_id` set on all of
them; the layered audit entries; idempotency on a second call;
rejecting a nonexistent or already-`REJECTED` instance; **the
atomicity regression** — force the second leg's `add_flight()` to fail
(same monkeypatch technique as Step 6's own crash-simulation test) and
confirm ZERO flights exist afterward and the instance is still `DRAFT`,
not partially approved; `reject_instance()`'s own success/error cases,
including the deliberate non-idempotency on a second rejection; **the
new database constraint tested directly**, not just through the
idempotency path — a raw duplicate `(rotation_instance_id, flight_no)`
insert is rejected even bypassing `approve_instance()` entirely, and
two ad-hoc flights sharing a `flight_no` are confirmed NOT rejected
(the partial index correctly ignores them); `create_template()`
rejecting a leg with no `flight_no`. Plus **one end-to-end grounding
test tying the whole groundwork arc together**: expand EPE 786/787 for
a real date, approve it, assign real crew to the resulting flights
through the actual, unmodified `assign_crew_to_duty()` legality gate,
and confirm the exact same report 1815Z / debrief 0000Z / FDP 5.75h
numbers already hand-verified in the template-layer piece — now
produced by a template-sourced flight indistinguishable from a
manually-entered one to every downstream consumer.

**Verification status**: 382 total (365 on `main` + 17 new). `pytest
tests/`: 149 passed, 233 skipped locally (no `TEST_DATABASE_URL` in
this sandbox) — every new test here is DB-integration and traced by
hand, not run; the logic was reasoned through directly, including
hand-tracing the end-to-end test's exact datetime values against the
already-confirmed `core/rotation_expansion.py` output. `check_
reachability.py`: unchanged — `services/rotation_template_service.py`
and `services/assistant/reports.py` remain the only two flagged files;
`rotation_template_service.py` now importing `flight_service` doesn't
change either file's own reachability status (`flight_service.py` was
already reachable from `pages/`). See `Current active task` near the
top of this file for merge status, not this line.

**One real-Postgres failure found and fixed (381/382): a test-fixture
gap, not a logic bug.** `test_expand_approve_then_assign_crew_
reproduces_hand_verified_numbers` failed with `RuntimeError: DATABASE_URL
not set`. Cause: this file's own `_patch_engine` fixture patched
`rts`/`flight_service`/`assignment_service`/`crew_service` but omitted
`audit_service` — invisible in every OTHER test here, since they all
reach `log_audit()` through a `conn` parameter that's already joined to
an already-patched connection. This one test calls
`crew_service.add_crew()` directly, whose own `log_audit()` call has no
`conn` and falls through to `get_engine()` — never patched, hence the
real `DATABASE_URL` lookup failing.

**Fixed at the root, not just the symptom**: this is the SECOND time a
per-file fixture gap has masked something (the first was Step 7's
`_QUALIFICATION_DEFAULTS` missing `date_of_birth`, a different kind of
gap but the same shape — one file's own copy of a list, and forgetting
an entry). `tests/conftest.py` gained
`_patch_all_service_engines(migrated_db, monkeypatch)`, patching
`get_engine()` on all five service modules that have one
(`assignment_service`, `audit_service`, `crew_service`,
`flight_service`, `rotation_template_service`) in one place. Every test
file that previously hand-maintained its own subset
(`test_assignment_service.py`, `test_assistant_reports.py`,
`test_crew_service.py`, `test_flight_service.py`,
`test_rotation_template_service.py`) now has a 3-line `_patch_engine`
wrapper requesting this shared fixture instead — same fixture name in
every file (no test function signature needed to change anywhere), but
the actual module list now lives exactly once. This class of gap is now
structurally impossible, not just fixed for this one occurrence.

**Verification status after the fix**: 382/382 confirmed by the user
against real Postgres 16 — migration 012 applies cleanly, the
`flight_no NOT NULL` + partial unique index landed as specified, and
the end-to-end grounding test now passes, confirming a template-sourced
flight is genuinely indistinguishable from a manually-entered one to
`assign_crew_to_duty()`.

## 2026-08-04 (continued): the roster generator — Phase 7's final piece

Plan proposed first (seven explicit design questions — input source,
fairness-counting scope, ordering strategy, the age-pairing order-
dependence risk, output mechanism given draft -> review -> publish,
partial-failure behavior, idempotency), reviewed twice by the user.
First round: the plan's Q4 international analysis was confirmed
correct, but its domestic premise ("a 65+ pilot in the first-filled
domestic seat is never a problem") was proven wrong empirically against
the real gate — see below. Branch `roster-generator-phase7-final`, off
`main` at the `rotation-instance-approval-workflow` merge point.

**The mechanism, settled and unchanged from the plan**: the generator
never re-expresses an FTL or pairing rule — every legality decision
goes through `assign_crew_to_duty()`, every single time. It only
decides candidate ORDER (`core/roster_generation.py`'s
`order_candidates()`) and whether a seat is already filled. Fairness =
even duty counts within role only, scoped to the generation window
itself (a real, duty-deduped count via `core/duty_summary.py`'s
`group_roster_rows_into_duties()` — never a raw row count). Rotations
walked chronologically, same-day ties broken by `rotation_code`
alphabetically, for deterministic (idempotency-friendly) output.
Output is a new `roster.status = 'PROPOSED'` (migration 013, mirrors
009's exact CHECK-constraint pattern), never written straight to
`PLANNED` — `assign_crew_to_duty()` gained an optional `roster_status`
parameter (default `'PLANNED'`, every existing call site unaffected);
`get_roster_for_crew()`/`get_roster_for_flight()`/`search_roster()`
gained a matching `include_proposed` parameter (same shape as their
existing `include_cancelled`) so `PROPOSED` stays invisible to
crew-facing reads by default — "crew sees only published."
`roster_coverage()` is the one deliberate exception
(`include_proposed=True`): a generator-filled seat must not show as
falsely UNCOVERED to a reviewing controller. `publish_window()` is the
mechanical `PROPOSED -> PLANNED` flip, the roster-level analog of
`approve_instance()`. Partial failure mid-run is not wrapped in one
transaction — each `assign_crew_to_duty()` call is already its own
atomic, committed unit (Step 6); a crash simply stops, safely resumable
by re-running. Idempotent by construction: a seat with any existing
ACTIVE assignment (including a prior run's own `PROPOSED`) is skipped,
never re-attempted or replaced; a previously-UNCOVERED seat is the one
thing retried on every run.

**The domestic premise the plan got wrong, and the fix, proven
empirically against the real pairing math before being coded** — not
theorized. Original claim: only international suffers from candidate-
selection order dooming a seat (a 65+ pilot in EITHER international
seat makes the pair unconditionally illegal, so a naive fewest-duties
pick can lock out a reachable, fully-legal crewing). Domestic was
assumed safe because its rule is looser ("illegal only if BOTH 65+").
Tested directly: a domestic rotation with an FO pool that's entirely
65+ (66 and 68) and a CPT pool with a 67yo (fewest duty, picked first
under plain ordering) and a 40yo alternative — plain fewest-duties
picks the 67yo CPT, both FO candidates get REJECTED (both pilots 65+),
seat UNCOVERED. The fix has to be asymmetric, not the same rule copied
onto domestic: international's under-65-first ordering is
UNCONDITIONAL on both seats (a 65+ pilot can never fly international
once paired, so deprioritizing them costs nothing); domestic's must be
CONDITIONAL — only kicks in for the seat filled SECOND, and only when
the seat filled FIRST turned out to be 65+ — because applying it
unconditionally on domestic would systematically under-assign legal
65+ pilots and break the even-duty-counts fairness goal for no reason
in the common case. `order_candidates(candidates, domestic,
partner_age=None)`: `partner_age` is the actual age of whoever already
fills the OTHER seat, `None` for the seat filled first. Age-aware
ordering applies when `not domestic` (international, always) or
`partner_age is not None and partner_age >= 65` (domestic,
conditional).

**A second gap, found by directly simulating the approved design
against the real pairing math BEFORE writing the DB-integration
tests** (same "verify via direct interpreter execution before trusting
it" discipline this session has used for every pure-logic module):
the domestic conditional fix only works at all if the SEAT WHOSE POOL
IS AT RISK gets filled FIRST, since the first-filled seat is never
blocked by pairing (no partner exists yet) and unconditionally locks in
whoever plain fewest-duties picks — the SECOND seat's conditional
ordering can only route around an already-known bad partner age, it
cannot undo the first seat's own unconditioned pick. Filling CPT before
FO (the naive, arbitrary choice) fails the exact scenario above: the
67yo CPT gets locked in first (nothing blocks a solo assignment,
however old), then FO's conditional ordering is powerless because
EVERY FO candidate is 65+ — no reordering within an entirely-65+ pool
changes the outcome. Filling FO before CPT fixes it: FO's forced 65+
pick becomes the known `partner_age` feeding CPT's own conditional
ordering, which then correctly prefers the 40yo over the 67yo. Verified
directly in the interpreter (`order_candidates()` fed the exact
scenario both ways) before being encoded as
`services/roster_generator_service.py`'s `ROLES = ("FO", "CPT")`
constant, with the reasoning recorded in that file's own comment — not
an arbitrary pick, and it doubles as the operationally sound choice
anyway, since FO is the smaller real-world pool (4 vs 6 CPT) and
therefore the more likely one to end up homogeneously 65+ in the first
place.

A third, related gap caught the same way: seats were originally
processed in one loop over a fixed role order, meaning an
ALREADY-filled seat's age (e.g. a controller's own manual `PLANNED`
assignment made before a generation run) would only reach the other
seat's conditional ordering if that already-filled role happened to
come first in `ROLES` — otherwise the information existed but arrived
too late to matter. Fixed by splitting into two passes: first record
every seat's existing state (populating `seat_ages` for anything
already filled, regardless of role order), then fill only what's
actually still needed, so a pre-existing seat's age is always available
to condition the other seat's ordering, not just when it happens to be
processed first.

**Files**: `migrations/013_roster_proposed_status.sql`.
`core/roster_generation.py` (new, pure, no DB — `Candidate`,
`order_candidates()`; takes pre-computed `age: Optional[int]` rather
than a raw `date_of_birth`, so age arithmetic is never duplicated —
the caller computes it via the real, already-tested
`assignment_service.age_on()` (promoted from private `_age_on`) and
hands in plain ints, keeping `core/` modules' existing "never import
from `services/`" placement principle intact rather than bending it for
this one case). `services/roster_generator_service.py` (new —
`generate_for_window()`, `publish_window()`, `GenerationSummary`/
`SeatResult` dataclasses; writes only via `assignment_service.
assign_crew_to_duty()`, Ownership Table unchanged).
`services/assignment_service.py`: `age_on()` promoted public;
`assign_crew_to_duty()` gained `roster_status`; `get_roster_for_crew()`/
`get_roster_for_flight()`/`search_roster()` gained `include_proposed`.
`services/rotation_template_service.py`: `_promoted_flight_ids()`
factored out of `approve_instance()`'s own idempotency check, plus a
public `get_promoted_flight_ids()` wrapper — the "which real flights
make up this approved rotation" lookup the generator needs for every
candidate rotation it considers. `services/assistant/reports.py`:
`roster_coverage()` now calls `get_roster_for_flight(...,
include_proposed=True)` — the one deliberate exception to "crew sees
only published." `tests/conftest.py`: `roster_generator_service` added
to `_patch_all_service_engines`' patched-module list, per the
consolidation discipline from the previous piece — no new one-off
local fixture.

**Tests**: 8 pure-logic (`tests/test_roster_generation.py`, no DB) —
plain fewest-duties when `partner_age` is `None` or under 65; the
conditional domestic switch once `partner_age >= 65` (including the
explicit regression proving ordinary domestic ordering is unaffected
when the first pick is under 65); international's unconditional
under-65-first regardless of `partner_age`; a missing-age candidate
pushed later, never excluded; deterministic tie-breaking. 13
DB-integration (`tests/test_roster_generator_service.py`, grounded in
the real EPE 786/787 domestic and EPE 802/804/805 international
rotations via the actual `expand_and_persist()` + `approve_instance()`
arc, not synthetic shortcuts): basic domestic/international fill as
`PROPOSED`; back-to-back international for a sole candidate ends
UNCOVERED (the real gate's rest math, not the generator, causing it —
isolated by using exactly one CPT candidate so fairness has no escape
route to a different pilot); back-to-back domestic for a sole candidate
succeeds (proving the reverse is legal, same isolation technique);
duty counts stay even within role (max-min <= 1) and the smaller pool
(FO) carries more average load than the larger one (CPT), scaled down
from the real 6/4 grounding ratio; the corrected domestic scenario
itself, end-to-end — an entirely-65+ FO pool still gets a fully-crewed
pair when an eligible under-65 CPT candidate exists; idempotency on a
fully-generated window (no duplicate rows); gap-filling on a partially
(manually) generated window, confirming the pre-existing row is never
touched; a previously-UNCOVERED seat retried and filled once the
blocker (an inactive crew member) is resolved between runs;
`publish_window()` flipping only in-range `PROPOSED` rows; `roster_
coverage()` showing a `PROPOSED` seat as covered while `get_roster_
for_crew()` hides it by default.

**Verification status**: DB-integration tests NOT yet run against real
Postgres — no `TEST_DATABASE_URL` in this sandbox, same as every other
DB-integration piece this session; their expected outcomes were
reasoned through by hand against the real gate's documented rules
(age-pairing, back-to-back rest math already established as grounding
facts earlier this session), including, for the two ordering-sensitive
scenarios, literally simulating `order_candidates()` plus the pairing
math in the interpreter first to confirm the predicted outcome before
writing the assertion, not guessed. Everything that COULD run locally
did: pure-logic tests (`test_roster_generation.py`, 8/8) and full-suite
collection (`pytest tests/`: 157 passed, 246 skipped, both new test
files collected cleanly with zero import errors). `check_reachability.py`
re-run: flags exactly `services/assistant/reports.py` (unchanged) and
`services/roster_generator_service.py` (no Phase 7 piece has UI yet).
`services/rotation_template_service.py` is no longer flagged, as
predicted — `roster_generator_service.py` is now its first real caller.
`core/roster_generation.py` was never flagged (same one-hop reasoning
as `core/duty_summary.py`/`core/rotation_expansion.py`). See `Current
active task` near the top of this file for merge status, not this
line.

## 2026-08-08: meal_provided data gap — the generator's first real-Postgres
## finding, fixed on the same branch

The user's own real-Postgres verification of `roster-generator-phase7-final`
found 5 failing tests, all tracing to one cause: `core/legality/
pcaa_ano012_core.py`'s D25 rule (`_check_nutrition()`) fires
`NEEDS_MANUAL_REVIEW` whenever a duty's FDP exceeds 6h and
`Duty.meal_provided` is `None` — and nothing anywhere in this codebase
had ever populated that field. Every international rotation (FDP
10.75h) hit this on both seats, permanently — 16 of 36 real rotations
per 28 days, 44% of the roster, UNCOVERED. This exact gap was already
noted as a discovery in this file's 2026-08-01 "Step 2" entry, but its
consequence for a fully-automated generator (no human in the loop to
manually override a `NEEDS_MANUAL_REVIEW`) was never followed through
until the generator itself existed to expose it.

Plan proposed first (schema layer, data flow through expansion/
approval/assign, the `snack_provided` decision, and — found only
during planning, not assumed — which existing tests structurally
depended on the bug being fixed), reviewed and approved by the user,
who independently re-verified the `D16.2.2` night-duty-window math used
to redesign one test (see below) before approving. Same branch, new
commit — this is a fix to that piece's own verification, not separate
work.

**Root cause, confirmed by reading the code, not assumed**: `Duty.
meal_provided: Optional[bool] = None` (`core/legality/pcaa_ano012_core.py`
line 141) is a deliberate tri-state — `None` (unknown, the branch that
always fired), `False` (confirmed not provided, a real `WARNING`),
`True` (confirmed provided, silent) — and the rule engine itself is
correct and untouched by this fix. There are exactly 3 real `Duty(...)`
construction sites in `services/assignment_service.py`:
`_load_duty_records_for_crew()` (rebuilds **historical** duties for
lookback/rest checks — matters just as much as the "new duty" site,
since `_check_duties()` loops over every duty in the list passed to
`validate_schedule()`, historical included, so an unfixed historical
>6h duty would have kept blocking that crew member's future
assignments indefinitely even after this fix shipped — the subtlest
part of this piece, confirmed by reading `_check_duties()`'s own loop),
`_validate_new_duty()` (the new duty being gated, shared by
`assign_crew_to_duty()`/`assign_crew_to_new_flights()`), and
`find_legal_candidates_for_duty()`'s inline `candidate_duty`
construction. None of these had anything from `flights` to feed
`meal_provided` — the column didn't exist anywhere in the schema.

**Operator-confirmed (2026-08-08): a meal is provided on every
rotation, today.** Recorded as DATA (migration 014), not a code
default — hardcoding `True` in the rule engine itself would silently
defeat D25 for a future rotation where a meal genuinely isn't provided.
`rotation_templates.meal_provided BOOLEAN NOT NULL DEFAULT TRUE` is the
source of truth, one value per template *version* (not per leg — meal
provision is a whole-duty operational fact in this airline's own
framing, "provided on every rotation," not a per-leg one the way
`domestic` genuinely can be). Broadcast onto every `rotation_instance_
legs` row at expansion time (`expand_and_persist()`, same denormalize-
at-expansion-time treatment `domestic`/`flight_no` already get — an
already-expanded instance keeps the value that was true when expanded,
not a later template version's edit) and from there onto every
promoted `flights` row at `approve_instance()` time. `flights.
meal_provided` is the column every `Duty`-construction site actually
reads (via `get_flight()`'s `SELECT *`); an ad-hoc Control Room flight
that omits it falls back to this column's own `DEFAULT TRUE`.
`create_template()`/`create_new_version()` gained a **required**
`meal_provided: bool` parameter — no Python default, mirroring the
column's own `NOT NULL` intent at the call-site level, the same way
`rotation_code`/`effective_from` already have none.

**Every Duty-construction site now aggregates `meal_provided` the same
way `domestic` already was** (`all(bool(f["meal_provided"]) for f in
flights)`, reusing an existing pattern rather than inventing a new
one) — a duty only "had a meal provided" if every leg genuinely did.
`assign_crew_to_new_flights()`'s pre-insert version needed one extra
deliberate detail: `flights_data` are plain caller-supplied dicts that
may omit the key entirely (nothing in the DB yet), so its computation
explicitly defaults missing entries to `True`
(`f.get("meal_provided", True)`) — this function gates legality
*before* any write happens, so the in-memory computation has to mirror
`flights.meal_provided`'s own `DEFAULT TRUE`, or the pre-insert gate
and the post-insert reality would silently disagree.

**`snack_provided` — a deliberate decision, not left an accident.**
Not wired up. `D2.18` only alerts when `snack_provided is False`;
`None` produces no alert, harmless today but not *correct* the way
`meal_provided` now is. The operator's confirmation was specifically
about meals; inferring a snack is therefore also provided would be
guessing at a legally distinct D2.18/D25 category never actually
asked about — exactly the kind of assumption this fix exists to
eliminate for `meal_provided`. Recorded as an explicit, visible open
item (see `Open stubs`), not silently left as today's accidental
non-alert.

**A real, necessary test redesign, not a find-replace** — caught
during planning, not discovered mid-implementation: several tests in
`tests/test_assignment_service.py` deliberately used the *bug itself*
(an unset-meal >6h duty) as their real-rule `NEEDS_MANUAL_REVIEW`
trigger. Once `meal_provided` is `NOT NULL` everywhere,
`D25_NUTRITION_DATA_MISSING` becomes structurally unreachable through
the real API — correct, that's the fix — so these needed a genuinely
different real trigger, not deletion or a mock (this codebase tests
against the real rule engine, not synthetic/mocked triggers, per this
file's own established discipline):
- `test_needs_manual_review_does_not_write_and_returns_needs_review_status`,
  `test_needs_manual_review_still_reports_computed_duty_times`,
  `test_needs_manual_review_writes_audit_record_with_held_action_type`,
  `test_needs_manual_review_via_control_room_saves_neither_flight_nor_
  assignment`: switched to a missing crew qualification-expiry field
  (`_add_crew("CPT", license_expiry=None)` →
  `AE-CREW-QUAL-001_LICENSE_EXPIRY_MISSING`) — a real, already-
  implemented, static trigger fully decoupled from nutrition data.
  Arguably better than before: no longer accidentally dependent on the
  very gap this piece fixes.
- `test_delay_recompute_flags_needs_review_when_no_longer_legal` was
  harder: it specifically needs "legal at assignment time, flips to
  `NEEDS_MANUAL_REVIEW` because a delay made the duty longer" — a
  missing qualification field can't do that (static, not duration-
  dependent, would fail the test's own first assertion). Found a real,
  still-reachable, duration-sensitive alternative:
  `D16.2.2_NIGHT_DUTY_OVER_10H_FRM_REQUIRED` (`has_approved_frm`
  defaults `False`, never overridden by this codebase's module-level
  `validator = ANO012CoreValidator()`) — fires when a duty's local time
  overlaps 02:00-04:59 and FDP exceeds 10h. Verified directly against
  the real validator in the interpreter before being locked in (same
  discipline as the EPE 786/787 grounding numbers): report 19:00 UTC
  2026-07-20 (local 00:00 on 2026-07-21, UTC+5) with a short initial
  arrival stays `LEGAL` with zero alerts; delaying `arr_time_actual` to
  2026-07-21 05:15 grows the duty to exactly 10.5h (630 min) whose
  local span now overlaps 02:00-04:59, firing `NEEDS_MANUAL_REVIEW` via
  `D16.2.2` alone (no other alert), confirmed comfortably under this
  band's own 660-minute (11h), 1-sector D8.2.1 ceiling — the same
  "don't also trip a bigger rule" constraint the original test already
  needed. The user independently re-verified this exact window (the
  overnight band's 11h cap and D16.2.2's 10h floor) before approving.
- `test_warning_only_status_still_allowed_and_written` and
  `test_adhoc_assignment_that_breaks_future_scheduled_duty_is_flagged`:
  no functional change — both already avoided the >6h/D25 trigger by
  design; only stale docstring prose updated (both cited D25/meal-data
  as their reasoning, now inaccurate).
- `_seed_duty()` (the direct-SQL history-seeding helper, ~10 call
  sites) and its docstring's framing of D25 as *the* reason an 8h+
  prior duty couldn't go through the real API: left alone, deliberately
  — legitimate test infrastructure independent of this bug; the
  docstring becomes slightly imprecise but not wrong enough to justify
  auditing ~10 call sites for a fix that isn't about test
  infrastructure.

**Files**: `migrations/014_meal_provided_columns.sql` (new).
`services/rotation_template_service.py` (`create_template()`/
`create_new_version()` gain required `meal_provided`;
`expand_and_persist()`/`approve_instance()` thread it through).
`services/flight_service.py` (`meal_provided` added to
`UPDATABLE_FIELDS`, deliberately not `REQUIRED_FIELDS`).
`services/assignment_service.py` (all 3 `Duty`-construction sites plus
both `_validate_new_duty()` callers). `core/legality/pcaa_ano012_core.py`:
untouched, confirmed correct as-is. `tests/test_rotation_template_
service.py` (8 call sites via 2 helpers + direct calls) and `tests/
test_roster_generator_service.py` (2 helpers) updated to pass
`meal_provided=True`. `tests/test_assignment_service.py`: the redesign
above.

**Verification status**: full local suite collection clean (no
`TEST_DATABASE_URL` in this sandbox, same as every DB-integration piece
this session) — see "Run full suite + reachability check" in the
current task list for the exact numbers. The `D16.2.2` scenario was
additionally run directly against the real validator (bypassing the DB
entirely, pure `core/legality` logic) confirming the exact predicted
transition: `LEGAL` (zero alerts) → `NEEDS_MANUAL_REVIEW` via
`D16.2.2_NIGHT_DUTY_OVER_10H_FRM_REQUIRED` alone, FDP 630 min. Not yet
re-verified against real Postgres — acceptance criterion is 403/403,
plus the user specifically checking the least-covered consequence: a
crew member with an existing >6h historical duty in their lookback
window should no longer be permanently blocked from new assignments.
See `Current active task` near the top of this file for merge status,
not this line.

**Explicitly out of scope, same as the plan**: any UI (review,
trigger, template management). Re-optimization or changing an existing
assignment once made. True optimal bipartite matching / backtracking —
this stays a greedy, single-pass loop with two targeted ordering fixes
for the two identified failure modes, not a claim of finding the best
possible crewing in every case.

## 2026-08-08 (continued): second real-Postgres pass — the meal_provided
## fix itself confirmed working, 3 test-side bugs found and fixed

The user's second real-Postgres verification confirmed the fix itself
works (`test_generate_for_window_fills_international_rotation_as_proposed`
passes in isolation, D25 no longer blocks international rotations) —
the remaining 5 failures were all test-side, not product bugs.

**One obsolete test premise**: `test_assignment_service.py::
test_mixed_domestic_international_duty_uses_international_buffer`
asserted `NEEDS_REVIEW` with a comment citing "no meal data" — written
during the mixed-domestic/international buffer work, predating this
piece, so it wasn't caught by the earlier redesign pass (which only
searched for tests exercising the `NEEDS_MANUAL_REVIEW` GATE itself,
not every test that happened to assert that status as a side effect of
the same now-fixed gap). Fixed: now asserts `ALLOWED`; the buffer
assertions (report 04:00, debrief 13:30 — the actual point of the
test) now check the written roster row directly instead of the
held-assignment `computed_*` fields.

**Sector rows vs. duties — Section 9's trap, caught by the user, not
avoided**: `tests/test_roster_generator_service.py`'s
`_roster_row_count()` helper did `SELECT COUNT(*) FROM roster` and
tests asserted `== 2` for a fully-crewed 2-leg rotation — but
`003_roster_table.sql`'s own header says, in capitals, this table
stores ONE ROW PER CREW PER FLIGHT SECTOR, so a 2-leg rotation crewed
by CPT+FO is genuinely 4 rows, 2 duties. The helper's own name invited
the exact mistake it made. Fixed: renamed `_roster_duty_count()`,
implemented via `core/duty_summary.py`'s `group_roster_rows_into_duties()`
— the canonical dedup this whole platform already has for exactly this
— rather than a raw `COUNT(*)`, which is what makes the helper honest
about which unit it measures going forward.

**A second, unrelated test-authoring bug, found while checking the
user's "may resolve, may be separate" third failure**:
`test_uncovered_seat_is_retried_and_succeeds_once_the_blocker_is_removed`
used `_add_crew("CPT", is_active=False)` intending a genuinely
inactive (zero-candidate) CPT pool, then `crew_service.update_crew(cpt_id,
{"is_active": True})` intending to reactivate it. Neither actually
works: `is_active` is not in `crew_service.UPDATABLE_FIELDS` (`crew_service.py`
line ~30), so both `add_crew()` and `update_crew()` silently filter it
out of the fields they actually write — the crew member was active the
entire time, on both calls, which is exactly why the first run's
"uncovered" list came back empty (`assert [] == ['CPT']`) instead of
holding the CPT seat. **A related, real product gap surfaced by
tracing this**: there is no `reactivate_crew()` anywhere in
`crew_service.py` — `deactivate_crew()` exists (a real, dedicated,
audited soft-delete), but nothing symmetric brings a crew member back.
Not fixed here (out of scope for this piece — flagged in `Open stubs`
below). Fixed the test itself by switching to a real, achievable
block/unblock mechanism already established in this same piece's
qualification-based test redesigns: `license_expiry=None` (missing
data → genuine `NEEDS_MANUAL_REVIEW`, correctly `uncovered`) then
`update_crew(cpt_id, {"license_expiry": ...})` (`license_expiry` IS in
`UPDATABLE_FIELDS`) to genuinely clear it between runs.

**Files**: `tests/test_assignment_service.py` (the obsolete-premise
fix), `tests/test_roster_generator_service.py` (the counting-helper
rename/reimplementation and the `is_active`/`license_expiry` test
redesign).

**Verification status**: full local suite collection clean (157
passed, 246 skipped, 403 total, zero import errors) — same sandbox
limitation as every DB-integration piece this session. Not yet
re-verified against real Postgres. Acceptance criterion unchanged:
403/403.

## 2026-08-08 (continued): third real-Postgres pass — same two patterns,
## caught in spots the second pass missed

The second pass's fixes were correct as far as they went, but two of
the three real-Postgres findings this round are the SAME two patterns
recurring in spots not covered the first time — not new bugs, missed
instances. The third is a genuinely new instance of the D25-premise
pattern.

**1. A leftover duplicate assertion, not a new bug**: `test_
mixed_domestic_international_duty_uses_international_buffer`'s edit
(previous pass) correctly added the `ALLOWED`/written-row assertions
but didn't remove the OLD `assert len(roster_df) == 0  # held, not
written` block that used to follow it — a stale duplicate from before
the edit, not caught because the file still collected and the earlier
assertions passed before reaching the dead one. Removed; also
strengthened per the user's own suggestion: `roster_df["duty_id"].
nunique() == 1` now confirms the 3 sector rows are genuinely one duty,
not just "3 rows of some kind."

**2. The row-vs-duty pattern, same root cause as `_roster_row_count()`,
different call site**: `test_publish_window_flips_only_proposed_rows_
in_range` asserted `publish_window()`'s return value `== 2`, but that
return value is a raw `UPDATE ... rowcount` — sector rows, not duties,
by design (`publish_window()`'s own docstring already says "returns
the number of rows flipped"; this was never meant to be duty-counted).
2 legs x 2 crew (CPT+FO) in the August rotation is genuinely 4 rows.
Fixed the assertion, not the function — `published_count` staying
row-based is correct given what a bulk `UPDATE`'s `rowcount` actually
measures; a duty-level version would need a separate `SELECT` and
isn't what this function is for. A full sweep of `tests/*.py` for any
other `len()`/`COUNT(*)` over roster rows found nothing else
miscounting — the remaining ones either measure single-sector duties
(where sector-count and duty-count coincide, so the distinction
doesn't matter) or are deliberately raw-row schema tests
(`test_schema.py`'s partial-unique-index regression tests, which are
correctly testing "N rows exist," not "N duties exist").

**3. A third, genuinely new D25-premise test, found by the same sweep
the user asked for**: `tests/test_control_room_page.py::
test_needs_review_adhoc_assignment_shows_warning_not_success` drove
the Control Room form with a 7h FDP duty specifically to trigger D25
nutrition-data-missing, asserting the page shows "HELD FOR MANUAL
REVIEW" without crashing on `flight_ids[0]` against an empty list
(the actual regression this test guards). Missed in the first redesign
pass because that pass only searched `test_assignment_service.py`.
Same substitution as that file's own tests: the crew member gets
`license_expiry=None` via `crew_service.update_crew()` after
`_seed_crew()` (not a change to `_seed_crew()` itself, which 3 other
tests in this file rely on staying fully-qualified) —
`AE-CREW-QUAL-001_LICENSE_EXPIRY_MISSING` now provides the trigger
instead. A repo-wide grep for `NEEDS_REVIEW`/`NEEDS_MANUAL_REVIEW`/
`D25`/`nutrition`/`meal_provided` across every test and page file
found no further instances: `test_alert_summary.py`'s `D25_
NUTRITION_DATA_MISSING` references construct `RuleAlert` objects by
hand to test the summarization/bucketing logic itself, independent of
whether D25 can fire through the real pipeline — not affected by this
fix at all. `test_schema.py`'s `NEEDS_REVIEW` references test the
`roster.status` CHECK constraint's own vocabulary, not D25.
`pages/*.py`'s references are generic `result.status` branch
rendering, not D25-specific.

**Files**: `tests/test_assignment_service.py` (leftover-assertion
removal + duty_id strengthening), `tests/test_roster_generator_
service.py` (the `publish_window()` count fix), `tests/test_control_
room_page.py` (the third trigger substitution).

**Verification status**: full local suite collection clean (157
passed, 246 skipped, 403 total, zero import errors). Not yet
re-verified against real Postgres. Acceptance criterion unchanged:
403/403.

## 2026-08-08 (continued): four Open Stubs decisions from the operator,
## implemented as one piece — snack_provided, reactivate_crew (settled
## NO), age-pairing in find_legal_candidates_for_duty(), status_filter

Branch `open-stubs-cleanup-2026-08-08`, off `main` at the Phase 7
merge point. Three of four items were explicitly straightforward, no
plan needed; the age-pairing item was plan-moded first per the
operator's own instruction, since it needed real design care (a pair
rule being checked per-candidate, against a real-vs-hypothetical
assignment) — reviewed and approved with one refinement recorded
below.

**1. `snack_provided` — operator-confirmed 2026-08-08: a snack is
provided on every rotation, today.** Wired up as an exact mirror of
`meal_provided` (migrations/014): migration 015 adds `snack_provided
BOOLEAN NOT NULL DEFAULT TRUE` to `rotation_templates`,
`rotation_instance_legs`, `flights`; `create_template()`/
`create_new_version()` gain a required `snack_provided` keyword, same
no-default footing as `meal_provided`; threaded through
`expand_and_persist()`/`approve_instance()` and all three real `Duty`-
construction sites in `services/assignment_service.py`, including
`_load_duty_records_for_crew()` for the same historical-duty reason
`meal_provided` needed it (an unfixed historical duty would keep
producing stale D2.18 signal on every future check otherwise).
Recorded as `**ASSUMPTION — requires airline validation**` (migration
015's own header + this file's `Open stubs`), same as `meal_provided`.
Every `create_template()`/`create_new_version()` test call site (8 in
`tests/test_rotation_template_service.py`, 2 in `tests/test_roster_
generator_service.py`) updated to pass it. New tests: `test_approve_
instance_promotes_every_leg_to_a_real_flight` strengthened to confirm
`meal_provided`/`snack_provided` both flow all the way from template
to promoted flights; `test_snack_not_provided_produces_warning_but_
still_allowed` fires the real `D2.18_D25_SNACK_REQUIRED` WARNING for a
domestic duty over 4h (but under D25's 6h) with `snack_provided=False`
on the flight, confirming it warns without blocking the write —
D2.18/D25's asymmetry (meal missing/unknown behaves differently from
snack missing/unknown) hadn't been exercised for real anywhere before.
`test_warning_only_status_still_allowed_and_written`'s docstring
updated — its own stale "snack_provided is never set" reasoning no
longer applies now that migration 015 exists.

**2. `reactivate_crew()` — operator decision: NOT needed.** Air Eagle
doesn't need reactivation; a deactivated crew member stays deactivated.
Documentation only, no code — the `Open stubs` entry (added while
tracing a test bug in the previous piece) is updated from "worth a
deliberate decision once operationally needed" to a settled RESOLVED
entry recording the decision, while keeping the actual trap intact:
`is_active` is absent from `crew_service.UPDATABLE_FIELDS`, so
`update_crew(crew_id, {"is_active": True})` still silently no-ops —
true and worth knowing regardless of the decision not to build
reactivation, since it's not that no one is SUPPOSED to try, it's that
the service layer correctly has no path for it now that the decision
is settled.

**3. Age-pairing in `find_legal_candidates_for_duty()` — plan-moded,
approved with one refinement.** The gap: this function powers
downstream-swap candidate suggestions but never checked whether a
candidate would pair legally (`AE-CREW-PAIR-AGE-001`) with whoever
really holds the other seat of the future duty being evaluated. Fixed
by reusing `_check_crew_pairing_age()` wholesale — the exact call
`_validate_new_duty()` already makes for a real assignment — once per
candidate in the loop, rather than writing anything new: that function
was already generic enough (a plain `crew_row` + `flight_ids`, no
assumption that its own caller's assignment is real) that asking it
about a hypothetical candidate is the same question it already answers
for a real one. `reference_date` (the rotation's first operating date)
hoisted above the candidate loop, same treatment `domestic`/
`meal_provided`/`snack_provided`/`duty_date` already get there. The
FTL-exempt branch (LM/ENGR) needed no change — `_check_crew_pairing_age()`
already returns immediately for non-CPT/FO roles.

Alerts fold into the same `ValidationResult` the candidate's FDP/rest/
qualification check already produces, using the existing `!= ILLEGAL`
inclusion threshold — no new return type, no separate flagging
channel. `AE-CREW-PAIR-AGE-001_AGE_LIMIT` (ILLEGAL) excludes a
candidate; the "pending" case (nobody real on the other seat yet)
emits no alert at all, by design, exactly matching the real assignment
gate's own false-alarm avoidance.

**The missing-DOB case (`AE-CREW-PAIR-AGE-001_DOB_MISSING`,
NEEDS_MANUAL_REVIEW) stays included, not excluded — approved, but with
a more complete reason than originally proposed.** The plan's own
argument was consistency with `_check_crew_qualifications()`'s
identically-severed missing-expiry-field case, which is already
included today under this function's existing threshold. **The
reviewed, fuller version**: there's a real asymmetry the consistency
argument alone understates. A missing qualification field is about the
CANDIDATE themselves — a controller reviewing that suggestion
encounters the problem directly, on the person being suggested. A
missing DOB on the OTHER seat's real occupant is about someone the
controller isn't even looking at — the suggestion just reads "swap in
Waqar," with nothing signaling that the uncertainty actually concerns
Saleem, who occupies the other seat. Including the candidate is still
correct: `DownstreamConflict.candidates` is a bare `List[str]` with
nowhere to carry that nuance, and excluding would silently drop a
legitimate candidate over someone else's incomplete data. But this is
recorded as a known LIMITATION of that return type, not a claim the
two cases are equivalent — flagged in `Open stubs` below as one of the
first things that should change if `DownstreamConflict` ever grows
per-candidate detail.

Tests (`tests/test_assignment_service.py`, alongside `find_legal_
candidates_for_duty`'s existing block): domestic age-illegal pairing
(both 65+) excludes the candidate, a young candidate stays included;
the same for international (illegal if EITHER is 65+ — a young real
partner does NOT save a 65+ candidate, unlike domestic); the other
seat genuinely uncovered — a 65+ candidate stays included (pending
stays silent); the other seat's real occupant has no recorded DOB — the
candidate stays included (the documented limitation above, pinned as a
permanent test, not just prose); and one end-to-end test through the
real caller (`_check_downstream_impact()`), confirming
`DownstreamConflict.candidates` itself excludes an age-illegal
candidate, not just the function in isolation.

**4. `query_parser.parse()` now populates `ReportRequest.status_filter`.**
Previously never called from `parse()` at all, even though "cancelled"/
"delayed"/"diverted" were already scoring keywords for the
`flight_records` template — a question like "which flights were
cancelled in June" correctly routed to `flight_records` but returned
ALL flights in June, since `reports.flight_records()` already passed
`request.status_filter` through and had nothing to pass. New
`parse_status()` (mirrors `parse_role()`'s exact shape) maps to
`flights.status`'s real CHECK-constraint values
(`migrations/002_flights_table.sql`: PLANNED/OPERATED/CANCELLED/
DISRUPTED) — "cancelled" -> `CANCELLED`; "delayed"/"diverted" -> `DISRUPTED`,
since there's no separate DELAYED/DIVERTED status to map to, both mean
"didn't go as planned but did fly." Reuses the same keyword vocabulary
already scored in `TEMPLATES["flight_records"]`, not a second list to
keep in sync. `services/assistant/reports.py`'s own module docstring
(which documented this exact gap) updated to RESOLVED — that file
needed no code change, `flight_records()` already had the pass-through
in place. Tests (`tests/test_query_parser.py`): each mapped keyword
(`cancelled`/`delayed`/`diverted`) resolves to its real status value;
an unmentioned status leaves `status_filter` `None`; one end-to-end
`parse()` test confirming the field actually reaches `ReportRequest`,
not just the standalone `parse_status()` function.

**Record correction: Supabase migration status.** The `Open stubs`
entry claiming migrations "were not yet confirmed applied against the
real Supabase DB" was stale — migrations 000-007 WERE confirmed
applied on 2026-07-30 (`run_migrations.py --status`: `Applied: 8,
Pending: 0` at that time, cross-checked against Supabase's own
dashboard — already recorded accurately in this file's "DB changes"
section, just not reflected in the `Open stubs` entry itself). What's
actually true now: migrations 008-015 have never been applied to
Supabase — eight migrations behind, not zero. Corrected in place; see
that entry's own 2026-08-08 note.

**SUPERSEDED 2026-09-05:** that "eight behind" figure is itself long
out of date — production has all 21 migrations (000-020) applied,
verified by reading `schema_migrations`. Left as written because it is
a dated log entry recording what was true that day; the live status
lives in the `Open stubs` entry, and the only reliable answer comes
from querying the database rather than from either paragraph.

**Files**: `migrations/015_snack_provided_columns.sql` (new).
`services/rotation_template_service.py`, `services/flight_service.py`,
`services/assignment_service.py` (snack_provided threading + the
age-pairing addition, same file, different functions).
`services/assistant/query_parser.py`, `services/assistant/reports.py`
(docstring only). `tests/test_rotation_template_service.py`, `tests/
test_roster_generator_service.py`, `tests/test_assignment_service.py`,
`tests/test_query_parser.py`.

**Verification status**: full local suite collection clean — 415
total, 163 passed, 252 skipped, zero import errors (the DB-dependent
new tests skip locally, same sandbox limitation as every other
DB-integration piece this session). `scripts/check_reachability.py`
re-run: unchanged (still exactly `services/assistant/reports.py` and
`services/roster_generator_service.py`, as expected — no new files in
this piece). Not yet run against real Postgres — no `TEST_DATABASE_URL`
here. See `Current active task` near the top of this file for merge
status, not this line.

## 2026-08-08 (continued): OCC assistant scope settled to tables-only,
## plus query_parser.py fixes found by testing 16 realistic questions
## against real Postgres

Branch `query-parser-refusal-and-date-fixes`, off `main` at the Open
Stubs cleanup merge point. The user tested `query_parser.py` against
16 realistic OCC shift questions on real Postgres: 9 reporting
questions routed correctly, but 5 decision-shaped ones resolved
CONFIDENTLY to the wrong template and returned a plausible-looking
table that didn't answer them, and testing surfaced several date-
parsing gaps the same way. **Operator scope decision, off the back of
that finding: the assistant generates tables only. It must never
answer a legality or decision question — supersedes any earlier
framing of this as a general OCC assistant** (the module docstring
already said "not an advisor" from 2026-08-01, but this makes it an
explicit, enforced boundary, not just a design rationale).

**1. Decision-question refusal layer.** New `is_decision_question()`,
checked in `parse()` before `score_templates()` runs at all —
unconditionally, so a decision-shaped question that also happens to
reference a D-section (`SECTION_RE`'s own regulation-lookup boost)
still refuses rather than winning on that boost. Confirmed wrong-
template cases, now refused instead:
- "who can fly tonight's 786" (was `crew_duty_history`)
- "is Waqar legal for tomorrow" (was `crew_duty_history`)
- "the 802 is delayed 3 hours, who's still legal" (was `utilization`)
- "Tahir called in sick, who can replace him Thursday" (was `crew_duty_history`)
- "what happens if I swap Shahbaz onto Friday" (was `crew_duty_history`)

Keywords (`can`, `could`, `should`, `legal`, `replace`, `swap`, `what
if`) are matched with `\b...\b` word boundaries deliberately, not a
plain substring check the way `score_templates()` itself works: a bare
substring check on "can" would also match inside "cancelled" (a real,
legitimate `flight_records` keyword — "which flights were cancelled in
June" must not be refused), and a bare "legal" would match inside
"illegal" (`audit_compliance`'s own real keyword). Verified directly in
the interpreter against every existing test phrasing in this file
before landing the list, then pinned as a permanent regression test
(`test_reporting_questions_sharing_vocabulary_are_not_falsely_refused`)
covering one phrasing from each of the 7 templates. The refusal message
redirects to the Roster page ("it runs the real legality check") and
sets `unmatched_text`, same as every other unresolved path — logged for
tuning, not silently dropped.

**2. Parser gaps, all found by the same real-question testing:**
- **DD-MM-YYYY / DD/MM/YYYY date ranges** ("01-07-2026 to 31-07-2026",
  "01/07/2026 - 31/07/2026") — the format the operator's own filename
  spec uses; only ISO ranges parsed before. Structurally distinct from
  the ISO regex (4-digit year LAST here, not first), so no ambiguity
  between the two regardless of check order.
- **Bare "N days"** ("expiring 60 days" / "expiring in 60 days", no
  next/last/past qualifier) — direction now depends on the
  already-resolved template (`parse_dates()` gained a `template`
  parameter, passed from `parse()`'s own `best_name`, which is already
  known by the time `parse_dates()` is called): forward for
  `crew_qualifications`, since document expiry is inherently a future
  concept; backward (rolling lookback, matching "last N days") for
  every other template, matching the dominant N-day concept elsewhere
  in this domain — D9.1.x/D9.2.x's own cumulative checks are rolling
  LOOKBACK windows, not forward projections. Documented in
  `parse_dates()`'s own docstring, per the "your call, but document
  which" instruction.
- **Plural role words** ("all captains" didn't set role; "all FO"
  happened to work only because FO's own plural looks identical).
  `parse_role()`'s `\b...\b` match required the exact singular form —
  fixed generically with `s?` on every keyword, not a special case for
  "captain" alone, since the same gap existed for loadmasters/engineers
  too.
- **Two wrong-semantics date bugs, both silently returning a confident
  table for the wrong period** — the higher-severity class of bug here,
  since neither failed honestly:
  - `"since 1 july"` returned the FULL month (1-31 July) instead of
    1 July -> today. The bare-month-name fallback matched "july" and
    ignored both "since" and the day number entirely. Fixed with a
    `since\s+(\d{1,2})\s+([a-z]+)` check placed before that fallback.
  - `"before august"` returned August's own 1-31 range instead of
    everything up to 31 July. Fixed with a `before\s+([a-z]+)` check
    resolving to `(None, <end of the PRIOR month>, None)` — "everything,
    no lower bound" through the day before the named month starts,
    including December/January year-wraparound.
- **`utilization` keyword gap**: "how close is Waqar to his 1000 hour
  limit" resolved ambiguously — `utilization`'s own `"hours"` keyword
  is plural and doesn't match this singular phrasing. Added `"hour
  limit"` as a new positive keyword.

**Files**: `services/assistant/query_parser.py` (all of the above;
module docstring updated to record the tables-only scope decision
explicitly). `tests/test_query_parser.py` (28 new tests: 5 for the
refusal cases, 9 regression-checking existing template-routing
phrasings aren't falsely refused, 2 for DD-MM-YYYY ranges, 4 for bare
N-days, 3 for since/before — including both wrong-semantics cases
pinned as permanent regressions, per the "add tests for each,
including the two wrong-semantics cases specifically as regressions"
instruction — 4 for plural roles, 1 for the hour-limit keyword).

**Verification status**: full local suite — 443 total (was 415),
191 passed (was 163), 252 skipped, zero import errors.
`scripts/check_reachability.py` re-run: unchanged, no new files. Every
fix verified directly in the interpreter against both the reported-
broken phrasing and the existing "must still work" phrasings before
being written up as a test, same discipline as every prior piece this
session.

**Confirmed 443/443 against real Postgres 16, zero failures.** The
user independently re-tested beyond the suite: all 5 refusal cases;
both flagged word-boundary collisions specifically ("which flights
were cancelled in June" still routes to `flight_records`, "everything
blocked this week" still routes to `audit_compliance`); every date
fix (DD-MM-YYYY/DD/MM/YYYY ranges, bare "N days" forward window,
plural role words, both since/before corrections). One follow-up
found in that pass, not a blocker — see `Open stubs`: "how close is
Waqar to his 1000 hour limit" resolves to `utilization` with no
`window_days` set, so the report has no window to compute D9.2.3's
rolling 365-day limit against.

See `Current active task` near the top of this file for merge status,
not this line.

## 2026-08-08 (continued): pages/5_Assistant.py — the OCC assistant's
## UI, the first page to call query_parser.py/reports.py

Branch `assistant-page-ui`, off `main` at the query-parser fixes merge
point. Plan-moded first (the user's own instruction). Presentation
only, as scoped — no changes to `query_parser.py`/`reports.py`/
`reporting.py`; everything the page needs already existed and was
already tested.

**The hard requirement, verified working, not just built**: the
interpretation (template, crew resolved to real names, date range,
role, status, route, flight number — extended slightly past the
user's own named list, since a misread airport code is exactly as
dangerous as a misread month) renders unconditionally before the
results, INCLUDING when the result is empty — confirmed directly via
`AppTest`, checking actual element order (`at.main`'s DOM order), not
just presence, since ordering was the actual requirement, not
existence.

**Three additions from plan review, all implemented**:
1. Interpretation shown before "no matching records," not only before
   a populated table — pinned as its own test
   (`test_empty_result_still_shows_interpretation_before_no_matching_records`).
2. Editable date range — the shown range can be corrected without
   retyping the whole question. Implemented with page-level
   `st.session_state` only (this page's first use anywhere in the
   repo): `dataclasses.replace()` on the resolved `ReportRequest`
   swaps in the edited dates while leaving crew/role/route untouched,
   clearing `window_days` deliberately (it's tied to the ORIGINAL
   relative phrasing — "last 28 days" — and keeping it after the range
   no longer matches that phrase would silently compute a rolling peak
   against a window nobody asked for). A code comment at the
   `session_state` initialization explicitly distinguishes this from
   the project's own hard lesson ("session_state lost unassigned count
   on refresh -> persist unassigned duties in DB, session state is
   demo-only") — that lesson is about OPERATIONAL data the system is
   responsible for; this holds a transient UI query result that costs
   nothing to lose on refresh. Written in so a future session doesn't
   "fix" this by persisting query results to the database.
3. Not fixed, by design: `regulation` has no date dimension at all
   (keyed by D-section only), so the editable-date section is skipped
   for it specifically — confirmed via `at.date_input` count == 0 on a
   regulation question.

**A real Streamlit bug found and fixed during implementation, not
assumed away**: `st.date_input`'s `value=` parameter is only honored
the FIRST time a given `key` is rendered — confirmed directly via
`AppTest` (a widget re-rendered with a NEW `value=` under the SAME key
kept showing the OLD value). Without a fix, asking a second, different
question would have silently left the date_input widgets showing the
FIRST question's stale dates — which the "did the user edit this"
diff check would then have mistaken for a manual edit and used to
silently overwrite the SECOND question's correctly-parsed dates with
the first question's. Fixed with a `assistant_generation` counter in
`st.session_state`, incremented on every newly-submitted question and
included in the date_input `key`s (`f"edit_date_from_{gen}"`) — a
fresh generation forces fresh widgets (and therefore the real new
default) on every new question, while edits WITHIN one question's
result still persist normally via the same key. Verified directly:
asking "last week" then "last month" now correctly shows July dates on
the second question, not stale August dates from the first.

**Files**: `pages/5_Assistant.py` (new). `tests/test_assistant_page.py`
(new, 12 tests, `AppTest` pattern matching `test_roster_page.py`/
`test_control_room_page.py` — real crew/flight/roster data seeded via
the actual services, dates computed relative to the real "today" the
test runs on rather than hardcoded, since the page has no injectable
`today` the way `query_parser.parse()` itself does).

**Verification status**: full local suite — 191 passed, 264 skipped
(was 252 — +12, all new, all correctly skipped, no `TEST_DATABASE_URL`
in this sandbox), zero import errors. Every branch (resolved/empty/
refusal/ambiguous-crew/ambiguous-template/unparseable/regulation-
passthrough/date-edit/generation-counter-fix) additionally verified
directly via `AppTest` with mocked `crew_service.get_all_crew()`/
`reports.run_report()` before being written up as a formal test — same
"verify via direct execution before asserting" discipline used
throughout this session, here necessarily via mocks rather than a real
DB (this sandbox's real `DATABASE_URL` — separate from the
test-only-gated `TEST_DATABASE_URL` — turned out to be genuinely
configured and reachable, confirmed when an early ad-hoc, unmocked
`AppTest` run made a real ~4s round-trip to it; every check after that
discovery used mocks specifically to avoid further, unnecessary
contact with a real database from ad-hoc scripts outside the
`TEST_DATABASE_URL`-gated fixture path).
`scripts/check_reachability.py` re-run: `services/assistant/reports.py`
confirmed no longer flagged; `services/roster_generator_service.py`
is the only file still flagged, unrelated to this piece. Not yet run
against real Postgres — no `TEST_DATABASE_URL` here. See `Current
active task` near the top of this file for merge status, not this
line.

## 2026-08-09: pages/6_Roster_Generation.py — Phase 7's first UI,
## clears the last `check_reachability.py` flag. NOT YET MERGED.

Requested via Plan Mode: pick a date window, generate, see results,
publish. Everything needed already existed — `services/roster_
generator_service.py`'s `generate_for_window()`/`publish_window()`,
`services/rotation_template_service.py`'s `get_instances()` — so this
piece is presentation only, no new service logic. Scoped to this page
alone: it assumes approved rotations already exist; template
management and draft review are a separate, not-yet-built page.

**Layout, in the order the operator specified as non-negotiable**:
a shared `date_from`/`date_to` pair (plain `st.date_input`s, not in a
form — a form would block the live pre-generate preview from updating
as dates change) drives both Generate and Publish. The preview applies
`get_instances(status="APPROVED")` filtered to the window — the same
filter `generate_for_window()` applies internally — to show a
rotation count and a time estimate (`rotation_count * (120/36)`,
scaled from the operator's own measured ~2min/36-rotation real-data
run), or, if zero, to say so plainly and point at the Rotation
Templates page by name (not built yet) instead of offering a Generate
button that would fail obscurely on nothing. A window over ~35 days
gets a non-blocking `st.warning` — 28 days is the operational
horizon, but this deliberately doesn't hard-block a controller
running two cycles together, matching the same "alert + suggest,
human decides" philosophy already established elsewhere in this app
(downstream-conflict swaps) rather than inventing a new, inconsistent
paternalistic pattern here.

**Results**, rendered from `st.session_state.generation_summary` in
the operator's stated priority order: `uncovered` first and most
prominent (a real dataframe with the actual legality-gate rejection
string verbatim in the `Reason` column — e.g. "CPT-01 (REJECTED):
needs 21.5h rest, only 13.25h available" — never summarized, since
that string already is the actionable detail); then per-pilot duty
counts derived from `filled` only (already duty-level — one
`SeatResult` per seat filled this run, not per sector — so counting
`crew_id` occurrences per role directly gives the fairness numbers, no
`group_roster_rows_into_duties()` dedup needed), CPT and FO in
side-by-side columns; then `already_covered` de-emphasized (a caption
count, detail behind a collapsed expander) — confirmation the
idempotency worked, not news.

**Publish is deliberately independent of whether Generate ran in this
session** — the PROPOSED row count and the Publish button are computed
fresh from `assignment_service.search_roster(..., include_proposed=True)`
on every render for the currently selected window, so a controller
returning later to publish something generated earlier doesn't need to
redundantly re-run the ~2-minute generation step first. The page states
the review mechanism explicitly, since a controller has no way to infer
it: unassigning a proposed seat on the Roster page marks it `CANCELLED`,
and `publish_window()`'s own `WHERE status = 'PROPOSED'` clause already
skips it — no new function needed, confirmed by direct reasoning against
both functions' real code before writing a line of page logic, then
proven by a dedicated test (see below).

**`st.session_state`**, this page's own first use of it, holds only the
just-generated `GenerationSummary` so it survives the rerun a later
Publish click triggers — a code comment at the initialization explicitly
distinguishes this from this project's own hard lesson ("session_state
lost unassigned count on refresh -> persist to DB, session state is
demo-only"): that lesson is about OPERATIONAL data the system is
responsible for; here, the real effect of Generate (the `PROPOSED`
rows) is already durably written to the database the instant
`generate_for_window()` returns, so losing the in-memory summary on a
refresh only loses the DISPLAY of what already happened, and
`generate_for_window()` is idempotent by design — re-running to see the
summary again is the intended recovery path, not a workaround.

**Tests** — `tests/test_roster_generation_page.py`, the first page test
to exercise Phase 7's whole chain end to end (`create_template()` ->
`expand_and_persist()` -> `approve_instance()`, not synthetic
shortcuts, reusing the real EPE 786/787 domestic and EPE 802/804/805
international grounding data from `tests/test_roster_generator_
service.py`). Dates computed relative to `dt.date.today()` via a
`_next_weekday()` helper, never hardcoded. Nine tests: page load;
zero-approved-rotations pointer message; a full fill with correct
fairness counts; an uncovered case proven both structurally (a
guaranteed no-candidates seat, checked via `AppTest` DOM order —
`Error` before the fairness `Markdown`, same ordering-is-the-real-
requirement check used on `pages/5_Assistant.py`) and substantively (a
back-to-back-international rest-math rejection, the SAME real scenario
`test_roster_generator_service.py` already established, proving the
actual rule-derived string reaches the page unaltered rather than the
"No candidates in pool" fallback — added after the user's own plan
review specifically asked for this second, distinct case); idempotency
across two Generate clicks; Publish showing the correct row count
(4, not 2 — the domestic rotation's 2 legs x 2 crew, same sector-row
unit `publish_window()` itself returns, not a duty count) and flipping
PROPOSED to PLANNED; a reject-then-publish test calling
`assignment_service.remove_assignment()` directly on both of a seat's
sector rows (catching, in review before ever running the test, that
`search_roster()`'s sector-level shape means a single-leg cancel would
have left the duty half-cancelled) and confirming `publish_window()`
correctly skips the cancelled row while still publishing the rest; and
the window-size warning appearing above ~35 days but not at the 28-day
default.

**Verification status**: full local suite — 191 passed, 273 skipped
(+9, all new, all correctly skipped — no `TEST_DATABASE_URL` in this
sandbox), zero import errors. Every code path (empty state, full fill,
both uncovered cases, idempotency, publish, reject-then-publish, the
window warning) additionally verified directly via `AppTest` with
mocked `crew_service.get_all_crew()`/`rotation_template_service.
get_instances()`/`roster_generator_service.generate_for_window()`
before being written up as a formal test — same "verify via direct
execution before asserting" discipline used throughout this session,
via mocks rather than a real DB per this sandbox's now-established
practice. `scripts/check_reachability.py` re-run: **zero files
flagged** — `services/roster_generator_service.py` was the last one,
now cleared.

**Real-Postgres verification (2026-08-09), pass 1: 459/464, 5 failed
— one cause, a test-fixture gap, not a page or service bug.** The
test file's own `_QUALIFICATION_DEFAULTS` (all eight expiry fields)
omitted `date_of_birth`; every seeded pairing therefore hit
`AE-CREW-PAIR-AGE-001_DOB_MISSING` -> `NEEDS_MANUAL_REVIEW`, and no
seat in any of the five affected tests ever filled — visible directly
in the generator's own real reason string
("CPT-01 (NEEDS_REVIEW): Cannot evaluate the age-pairing rule... date
of birth is missing"), exactly the kind of actionable detail this
page's `uncovered` section exists to surface, here surfacing a test
bug instead of a production one. Fixed with a single added line — a
fixed, clearly-under-65 `date_of_birth` in that dict.

**Pass 2 (2026-08-09), re-verification: 464/464, zero failures.**
`check_reachability.py`: **"All files under core/, services/, db/ are
reachable from somewhere" — the first fully clean run since that
checker was written in Phase 1.** Every file in the repo is now
genuinely wired to something a user can reach. Merged into `main`,
pushed; branch `roster-generation-page` deleted, both remote and
local. See `Merge status as of this snapshot` near the top of this
file for the authoritative merge state, not this line.

## 2026-08-10: pages/7_Schedule_Templates.py — Phase 7's second and
## last UI. NOT YET MERGED.

Requested via Plan Mode: three workflows in order — view/create
templates, expand a window into drafts, review (approve/reject)
drafts. Everything needed existed in `services/rotation_template_
service.py` except one genuine gap, found during research and flagged
before writing any page code: there was no way to list existing
rotation codes at all (`get_versions(code)` requires already knowing
the code). Added `get_all_rotation_codes()` — one pure read accessor
(`SELECT DISTINCT rotation_code FROM rotation_templates ORDER BY
rotation_code`), same idiom as `get_versions()`/`get_instances()`, the
one deviation from "presentation only."

**Leg entry**: fixed 5 blank rows, not dynamic add/remove — real
rotations are 2-3 legs (EPE 786/787 has 2, EPE 802/804/805 has 3), and
a dynamic add/remove form needs `session_state` + reruns inside what
should be one atomic submit, real complexity for no benefit at this
leg count. A row counts as "filled" if any of flight_no/origin/
destination has content; filled rows get sequential `leg_order` in row
order regardless of which of the 5 rows were used. Validation goes one
step past `rotation_template_service._validate_legs()` (which only
enforces `flight_no`): also checks `arr_time` is after `dep_time` at
creation time — proven, not assumed, to be a real gap: a template with
dep 20:00/arr 19:00 is accepted by `create_template()` and only fails
later, at `expand_and_persist()` time, with "arr_time 19:00 is not
after dep_time 20:00." Without the earlier check, a controller
discovers a broken template only when expansion fails, possibly days
after creating it.

**Create a new version** shows the operator's own named "least obvious
behaviour" live, before confirmation: "this will end version N on
{effective_from - 1 day}," computed with the exact subtraction
`create_new_version()` performs internally, via a date picker
deliberately kept outside any `st.form` so the preview recomputes on
every change (a form would batch it until submit, defeating the live
preview — same reasoning already established on `pages/6_Roster_
Generation.py`). The new version's days-of-week and legs pre-fill from
the CURRENT version's own stored values, keyed by rotation_code
(`cv_{code}_...`) so switching which rotation is being versioned always
gets fresh, correct defaults — reusing the exact widget-key staleness
fix already established on `pages/5_Assistant.py`/`pages/6_Roster_
Generation.py` (a widget's `value=`/`default=`/`index=` is only
honored the FIRST time a given key renders).

**Bulk review — the real interaction question, and a real mid-build
finding.** The obvious design, `st.data_editor` with a checkbox column,
turned out to have NO `AppTest` accessor at all — confirmed directly
(`dir(at)` lists every other input widget type but not `data_editor`)
before committing to it, not discovered after building it. Redesigned
around one `st.checkbox` per row (`key=f"select_{instance_id}"`) inside
a plain `st.columns` grid instead — fully testable, and each row
already carries its own route/flights/report/debrief inlined as
columns (built from that instance's real `get_instance_legs()`), which
is what actually satisfies "a draft must show its legs before
approval, not just rotation code and date" — inlined rather than
behind a per-row expander, since an expander per row would reintroduce
the exact 36-click friction the checkbox/select-all design exists to
avoid. "Select all visible"/"Clear selection" needed a second real
finding to work at all: `st.session_state[key] = value` raises
`StreamlitAPIException` once a widget with that key has already been
instantiated THIS run — confirmed directly via a minimal `AppTest`
repro before writing the real page code. Fixed with a pending-flag
pattern (the button sets a flag and reruns; the flag is applied, and
cleared, at the TOP of the script, before the checkboxes render).
Approve and reject each call `approve_instance()`/`reject_instance()`
individually per selected id, never batched into one call — batched is
the clicking, not the decision or the per-instance audit record, per
the operator's own framing. `selected_ids` is always computed by
filtering the CURRENTLY VISIBLE id list against session state, never
by scanning every session key — so a selection made under one
rotation_code filter can't silently get swept into an action taken
after switching to a different filter (a dedicated test proves this:
select under filter A, switch to filter B, Select-all-visible there,
Approve — A's instance stays DRAFT, only B's gets approved). Approve
reports the real resulting flight count (summed `len(flight_ids)`
across every successful call) — the moment a draft becomes
operational, per the operator's explicit ask. Reject requires a reason
via a `disabled=` gate on the button; there's no way to reject without
one.

**A third real finding shaped the test file itself**: confirmed
directly, via ad-hoc `AppTest` scripts before writing any formal test,
that a single `at.run()` after a button click does NOT reliably
surface a transient `st.success()` banner when `st.rerun()` immediately
follows it AND more script executes after that point — which every
action on this page has (later workflow sections always follow).
`AppTest`'s `.run()` appears to run the internal rerun through to a
stabilized final state rather than stopping at the pre-rerun banner,
confirmed by direct comparison against a minimal repro with nothing
after the `rerun()` call (where the banner IS reliably visible — this
is exactly why `pages/6_Roster_Generation.py`'s own Publish banner
test works, since that success+rerun is the literal last code in that
file). Rather than fight this timing, create/create-version/approve/
reject are tested via real effects (querying `rotation_template_
service` directly afterward) instead of the transient banner text —
arguably a more robust test design regardless. Workflow 2 (expand) has
no `st.rerun()` at all and IS tested via its banner directly, confirmed
working the same way. Validation-error paths (no rerun involved) are
asserted via `at.error` text directly too, unaffected by any of this.

**Tests** — `tests/test_schedule_templates_page.py`, same `page_app`
fixture pattern as `tests/test_roster_generation_page.py`. Nine tests:
page load; create template via the real form (validates real rows via
`get_versions()`/`get_template_legs()` afterward); a partially-filled
leg row shows the exact error and writes nothing; create-new-version
shows the live preview text before submit and the real `effective_until`
after; expand creates the right count and is idempotent on a second
click; the review table shows real route/flight data, not just id/date;
select-all then approve promotes the right instances and reports the
right flight count (2 instances x 2 legs = 4 flights); reject stays
disabled until both a selection AND a reason exist, then writes the
real reason; and the filter-scoping test described above.

**Verification status**: full local suite — 191 passed, 282 skipped
(+9, all new, all correctly skipped — no `TEST_DATABASE_URL` in this
sandbox), zero import errors. Every flow (empty state, create template,
partial-leg validation, create-version preview, expand + idempotency,
review table content, select-all/approve, reject gating, filter
scoping) additionally verified directly via `AppTest` with mocked
`rotation_template_service` functions before being written up as a
formal test — same "verify via direct execution before asserting"
discipline used throughout this session, and the specific discipline
that caught all three real findings above (the validation gap, the
`data_editor`/`session_state` timing issues, and the success-banner
timing issue) before they became either a shipped bug or a flaky test.
`scripts/check_reachability.py` re-run: unchanged, zero files flagged
— `rotation_template_service.py` was already reachable from
`pages/6_Roster_Generation.py`; this page is a second caller, not a
newly-reachable one.

**Real-Postgres verification (2026-08-10): 473/473, first run, zero
failures.** All three findings held up under real data — the user
confirmed the `arr_time`/`dep_time` validation gap empirically before
ever approving the plan (dep 20:00/arr 19:00 genuinely accepted by
`create_template()`, genuinely only failing days later at expansion),
and specifically endorsed testing real service-layer effects over
transient banner text as "testing what actually happened beats testing
what was displayed about it." The user additionally drove the page
directly beyond the test suite: `get_all_rotation_codes()` correctly
empty then populated, the page rendering without exception both empty
and populated, and a real expansion through the full chain producing
exactly 5 Mon-Fri drafts for 3-7 Aug with a checkbox each.
`check_reachability.py` on this same pass: still clean. Merged into
`main`, pushed; branch `schedule-templates-page` deleted, both remote
and local. See `Merge status as of this snapshot` near the top of this
file for the authoritative merge state, not this line.

## 2026-08-10 (continued): Streamlit Community Cloud deployment fix —
## db/db.py falls back to st.secrets. NOT YET MERGED.

Requested via Plan Mode, alongside home-page branding (a separate,
independent piece — see the next log entry — deliberately pushed to
its own branch, `home-page-branding`, since the two touch unrelated
files and have no sequencing dependency). The blocker: `.env` is
gitignored, so it doesn't exist in a deployed Streamlit Cloud
container, and nothing anywhere read `st.secrets` — confirmed via
grep, zero matches — so every page raised `RuntimeError: DATABASE_URL
not set` on deploy.

**Precedence, exactly as specified**: `.env`/environment wins when
present — unchanged, every test and script already depends on this —
`st.secrets` consulted only when `DATABASE_URL` is still absent after
`load_dotenv(override=True)` runs. Purely additive: the existing
`override=True` fix (a real, already-regression-tested bug — a stale
shell `DATABASE_URL` silently shadowing `.env`, `tests/
test_env_override.py`'s two original tests) is completely untouched,
confirmed by those two tests still passing unmodified.

`db/db.py` gets one new function:
```python
def _resolve_database_url() -> str | None:
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        return db_url
    try:
        import streamlit as st
        return st.secrets.get("DATABASE_URL")
    except Exception:
        return None
```
`streamlit` is imported INSIDE the function, not at module level —
`db.py` is imported by `scripts/run_migrations.py` and `scripts/
import_crew_from_xlsx.py` specifically outside any Streamlit runtime,
and a plain migration run shouldn't have to pull in the whole
Streamlit runtime for no reason. Confirmed directly (not assumed):
`st.secrets.get(...)` raises `StreamlitSecretNotFoundError` with no
Streamlit runtime context and no `secrets.toml` file present —
exactly `scripts/run_migrations.py`'s and the test suite's own
situation — so the broad `except Exception: return None` is what lets
this fall through cleanly to `get_engine()`'s own existing
`RuntimeError`, rather than crashing with a different, confusing
exception from inside `_resolve_database_url()` itself.
`get_engine()` changes from reading `os.environ` directly to calling
this function; its `RuntimeError` message now mentions both `.env`
and Streamlit Cloud's secrets.

**Tests** — three added to `tests/test_env_override.py` (now 7 total,
alongside the two original regression tests, run unmodified to prove
`override=True` itself is untouched): `st.secrets` used when the env
var is absent; the env var wins when both are present — proven with a
spy `st.secrets` that raises `AssertionError` if `.get()` is ever even
called, not just by checking which value won, so a future refactor
can't accidentally start preferring secrets while happening to still
return the right value; and `_resolve_database_url()` works with NO
Streamlit runtime at all and no `DATABASE_URL` set, using the REAL,
unmocked `st.secrets` (not a mock standing in for "no runtime") —
returns `None` rather than raising, the exact test that protects
`scripts/run_migrations.py` and the whole test suite's own ability to
import `db.py`.

`README.md` gets a new "Deploying to Streamlit Community Cloud"
section: the exact secret TOML format (a flat top-level
`DATABASE_URL` key, matching `.env`'s own variable name — no
`[section]` needed), a note that `.env` keeps working locally
unaffected, and that `TEST_DATABASE_URL` is deliberately NOT needed in
the deployed app's secrets since the test suite never runs there.

**Verification status**: full local suite — 194 passed (+3, the three
new tests; the 282 skipped DB-dependent tests are unchanged, since
none of this touches anything DB-fixture-dependent), zero import
errors. `scripts/check_reachability.py`: unchanged, clean.
`st.secrets`'s actual raised-exception behavior (`StreamlitSecretNotFoundError`
with no runtime/no secrets file) confirmed directly via a real,
unmocked call in this sandbox before writing the guard, not assumed
from documentation.

**476/476 verified against real Postgres 16.** The user additionally
confirmed, beyond the test suite, the three things that actually
protect the non-Streamlit callers this whole piece exists for:
`_resolve_database_url()` returns `None` with no runtime and no env
var set (so `get_engine()` raises the correct, existing `RuntimeError`
rather than leaking a different, Streamlit-specific exception up to
`scripts/run_migrations.py`); `scripts/run_migrations.py` itself runs
clean end to end; and `db.py` genuinely does not pull `streamlit` into
`sys.modules` on import — confirming the in-function `import streamlit`
(rather than a module-level one) actually achieves what it was
designed for. Merged into `main`, pushed; branch
`streamlit-cloud-secrets` deleted, both remote and local. See `Merge
status as of this snapshot` near the top of this file for the
authoritative merge state, not this line.

## 2026-08-10 (continued): home page branding — logo, background,
## theming. NOT YET MERGED.

Requested via Plan Mode, alongside the `st.secrets` deployment fix (a
separate, independent piece on its own branch — see `Merge status`
above). `AE-image.jpg`/`logo.png` were sitting untracked in the repo
root; moved into `assets/` via `git mv` (well, `mv` — they were never
committed, so there was no history to preserve) so the root stays
config/entry-points only. Checked file sizes before anything else, per
the operator's own instruction: 28KB JPEG (640x480) and 41KB PNG
(540x462), confirmed via `file`/`du`, not assumed — **neither needs
resizing**, nowhere near a real concern against Community Cloud's
~1GB memory budget. Operator confirmed the aircraft photo (an ordinary
promotional ramp shot — tail art, nose art, blue sky) is fine to be
publicly visible in this public repo.

**`st.logo()` — a real finding that changed the file list.** The
request assumed one call in `app.py` would reach all seven pages;
read Streamlit's own installed source (`streamlit/commands/logo.py`,
confirmed independently by the operator against 1.60.0 too) instead of
trusting the docstring alone — `logo()` builds a `ForwardMsg` and calls
`ctx.enqueue(fwd_msg)` on the CURRENT script run's context, no
app-wide or session-persisted state. Since Streamlit's multipage
navigation only re-executes the target page's own script, a single
call in `app.py` does not reach the other six. Added
`st.logo("assets/logo.png", size="large")` to all eight files
(`app.py` + all seven `pages/*.py`) instead, placed right after each
file's existing `st.set_page_config(...)` — matching this repo's own
existing precedent of repeating `st.set_page_config(...)` per-page
rather than factoring it into a shared helper; the same treatment for
one added line with no per-page variation is consistent, not a new
pattern. `size="large"` (32px max height, not the 24px default) since
the logo is a compact roundel+wordmark, not a wide banner — more of
the roundel detail stays legible at 32px. Confirmed directly, not
assumed: a relative path (`"assets/logo.png"`) resolves against the
Streamlit PROCESS's working directory (Python's own `open()`/
`os.path.isfile()` semantics inside `image_to_url()`), not the calling
script's own directory — meaning the SAME relative path is correct
uniformly across `app.py` and every `pages/*.py` file, as long as
`streamlit run app.py` is invoked from the repo root (already this
project's documented convention). Verified via `AppTest` across all
eight files with mocked services before writing the formal test file.

**Background image, `app.py` only, base64-embedded** into a `<style>`
block (`.stApp`, `background-size: cover`, `background-attachment:
fixed`) injected via `st.markdown(..., unsafe_allow_html=True)`.
Scoping to the home page is structural, not a guard that had to be
written: this CSS only ever executes when `app.py` itself runs, and
Streamlit's page navigation never re-executes `app.py`'s script body
while another page is open — the seven working pages stay clean by
construction, exactly the operator's own requirement (a background
behind a roster table or a legality alert would actively harm the
screens where real decisions get made).

**Legibility — two layers, and a real correction during plan review.**
The FIRST draft proposed wrapping the title/DB-status/nav text in a
manually-opened `<div>` panel via `st.markdown(unsafe_allow_html=True)`.
Caught before any code was written: a `<div>` opened in one
`st.markdown()` call does not contain elements later, separate
`st.title()`/`st.success()`/`st.error()` calls render — each `st.*`
call gets its own Streamlit-managed container, and there's no reliable
way to open a container in one call and close it in a later one. Fixed
by styling Streamlit's OWN existing container instead of replacing the
elements with raw HTML: `.block-container` — the real element every
`st.*` call in `app.py` already renders inside — gets `background:
rgba(255,255,255,0.92)`, rounded corners, padding, and a gold
(`#CDAF6F`) left border accent. Same visual result, zero markup
changes, `st.success`/`st.error` keep their real semantics and stay
assertable in `AppTest`. Layer two, baked into the background image's
own CSS: `linear-gradient(rgba(10,15,35,0.6), rgba(10,15,35,0.6))`
composited under the photo, dimming it everywhere, not just behind
text. Two layers because this is a screen read at 0300 during a live
disruption — a single dim gradient alone is a reasonable bet, not a
guarantee, and the second layer costs a few lines of CSS against an
already-existing container.

**`.streamlit/config.toml`**: `primaryColor = "#001A7B"` — sampled
directly from `logo.png`'s own pixel data (a histogram of the most
common opaque, non-white colors: navy `#001A7B` at ~43k pixels, the
dominant roundel/wordmark fill; gold `#CDAF6F` at ~7k, the eagle icon)
rather than eyeballed, per the operator's own preference.
`backgroundColor`/`secondaryBackgroundColor`/`textColor` deliberately
left at Streamlit's defaults — every page's `st.dataframe()`/
`st.error()`/`st.success()` output already depends on those staying
legible, and overriding them risks a clash this piece has no reason to
introduce. Gold has no dedicated slot in `config.toml`'s theme keys,
so it's used as the panel's accent border in `app.py`'s own CSS
instead — both sampled brand colors end up genuinely used, not just
one.

**Tests** — `tests/test_home_page.py` (new), same fixture pattern as
every other page test: the page loads without exception with a real,
reachable DB (the real `assets/logo.png` path resolves, the real
`assets/AE-image.jpg` base64-encodes into the CSS, no exception from
any of it) and shows "Database connected"; a second test (needs no DB
fixture at all, runs locally) exercises the DB-down path via a mocked
`test_connection()`, confirming the error message stays present and
the background/logo additions don't break that branch either. All
seven OTHER pages additionally verified via `AppTest` with mocked
services (not formal tests — a lighter touch, since the only change
to those files is the one added `st.logo()` line) to confirm the logo
addition doesn't raise anywhere.

**What pytest can't verify, said plainly rather than implied**:
whether the background/overlay/panel/logo actually look right and
stay legible is a visual judgment call, not something `pytest` or
`AppTest`'s element-tree assertions can make. The real check is a
manual `streamlit run app.py`, not this test file — noted here so
"192 passed" is never read as "confirmed to look right."

**Verification status**: full local suite — 192 passed (+1 locally
runnable, the DB-down `test_home_page.py` test; the DB-connected one
correctly skips, no `TEST_DATABASE_URL` in this sandbox), 283 skipped,
zero import errors. `scripts/check_reachability.py`: unchanged, clean
(no new `core`/`services`/`db` files). **Merge gate for this piece is
a manual `streamlit run app.py` look, not real-Postgres verification**
— this branch does not merge until the user has actually seen it
render.

**475/475 verified against real Postgres 16.** The user additionally
confirmed, beyond the test suite, the specific thing the plan-review
correction was about: `st.success`/`st.error` are still real
Streamlit elements (checked directly at `app.py:63`), not hand-written
HTML — the `.block-container`-styling fix over the manually-opened
`<div>` approach held up. Assets confirmed moved, repo root clean,
`st.logo()` present on all 8 files as built. **Not yet merged — Arif
still needs to run `streamlit run app.py` and look at it himself; see
the follow-up log entry below for a second round of changes made
after doing exactly that.** See `Merge status as of this snapshot`
near the top of this file for the authoritative merge state, not this
line.

## 2026-08-11: home page branding, round 2 — refinements from actually
## looking at round 1 running, plus st.navigation(). NOT YET MERGED.

Round 1 got 475/475 and looked right in the ways `pytest` could check;
running it surfaced six real refinements, requested together: an
inline double-size logo replacing "Air Eagle" in the title text (color
matched to the logo), a green status circle on DB-connected, an inline
UTC clock next to the nav text, removing the sidebar logo from all 8
files, real navigation buttons for each page, and getting "app" in the
sidebar nav to read "Home." One clarifying question asked and answered
before planning: whether removing the sidebar logo should be
everywhere or just the home page — everywhere, confirmed, since the
new inline logo plus the page-link buttons make it redundant on every
page, not just the landing one.

**The `st.navigation()` decision — assessed properly, not guessed,
per an explicit request to compare both options and recommend one.**
The obvious path for "app" → "Home" was renaming `app.py` to
`Home.py`: confirmed by reading `streamlit/source_util.py` directly
that the classic `pages/`-directory pattern derives every nav label,
including the entry script's, purely from the filename via regex, with
no override short of a bigger architecture change. But a rename
requires manually updating Streamlit Community Cloud's "Main file
path" setting — outside this repo, in lockstep with the merge, with
nothing in the codebase able to warn if it's missed, discovered only
after an apparently-successful deploy. `st.Page(title=...)` sets the
sidebar label directly, independent of filename, avoiding that risk
entirely. The deciding question was whether it breaks any of the seven
existing `AppTest.from_file("pages/...")` test files: confirmed,
empirically and from source, that it doesn't — `AppTest.from_file()`
execs a script directly with zero dependency on the entry point
(confirmed by reading its own source), and a temporary `st.navigation()`
router referencing a real page (`pages/1_Control_Room.py`, which calls
its own `st.set_page_config()`, unmodified) ran clean via `pg.run()`
with no "called twice" conflict. Recommended and built on
`st.navigation()`: `app.py`'s content moved into a new `home.py`
(registered via `st.Page("home.py", title="Home", icon="🏠",
default=True)`, exactly like the other seven, just living at the repo
root instead of under `pages/`); `app.py` itself is now a ~15-line
router with no content of its own. `app.py` is never renamed, so the
Streamlit Cloud "Main file path" setting needs no update at all — the
external-action risk from the rename path is moot, not just mitigated.
All seven `pages/*.py` files needed zero code changes beyond removing
their `st.logo()` line (already planned) — confirmed, not assumed.

**A real bug reproduced and fixed in `scripts/check_reachability.py`**,
per explicit instruction to build it regardless of the navigation
decision: copying this branch, renaming `app.py` → `Home.py` WITHOUT
updating `ENTRY_POINTS`, and running the checker produced a clean,
exit-0 "all reachable" pass — with its own declared entry point not
existing on disk at all. Root cause, found while fixing it: `ENTRY_POINTS`
was already dead code — nothing read it. The actual reachability scan
(`app_reachable_source_files()`) independently hardcoded the string
`"app.py"` and a separate `{"pages"}` check, so the constant's own
staleness was invisible; it was also internally inconsistent (mixing
absolute glob paths for `pages/` with one relative string for
`app.py`). Fixed properly, not just guarded: `app_reachable_source_files()`
now actually reads `ENTRY_POINTS` (`["app.py", "home.py"]`, both
root-level entries — `pages/` is still handled generically, no
per-file listing needed there), and a new `missing_entry_points()`
check runs first in `main()`: any declared entry point that doesn't
exist on disk stops the checker immediately with a named, non-zero-exit
warning, before it ever attempts a scan it would otherwise report as
clean. Three new tests in `tests/test_check_reachability.py` (now 10):
the reproduced failure mode itself, the positive "nothing missing"
case, and an end-to-end `main()` check confirming it exits 1 and never
prints reachability language while blind to a declared entry point —
all in the same synthetic-fixture style as the file's existing nine
tests, none of which needed to change (they don't touch `ENTRY_POINTS`
or call `main()` directly, so the fix doesn't affect them).

**A real, non-obvious Streamlit behavior found while building the
green circle**: `st.success("🟢 Database connected")` does NOT leave
the emoji in the rendered body text. Streamlit's own `extract_leading_icon`
pulls a leading emoji out of `body` into a separate, dedicated icon
slot (shown "slightly enlarged" next to the alert) whenever `icon` is
not explicitly passed — confirmed directly via `AppTest`, where
`.value` showed only "Database connected" and the emoji turned up in
`.icon`/`.proto.icon` instead. Arguably a nicer result than plain
inline text (a real, enlarged icon rather than a character mixed into
a sentence) — but the test had to be written against `.icon`, not
`.value`, or it would have silently asserted the wrong thing.

**Other changes**: the nav line now reads "Choose where to go below,
or use the sidebar — {UTC timestamp}" (reworded per the operator's own
suggestion to reconsider it now that page-link buttons are arguably
the more prominent route, not just the sidebar) — a render-time
snapshot, not a live tick, said plainly rather than implied (a real
clock would need `streamlit-autorefresh`, not a current dependency).
Seven `st.page_link()` buttons in a 4-then-3 grid, icon and label
matching each page's own `st.set_page_config()` identity. Two stale
docstring lines in `pages/2_Crew_Data.py`/`pages/3_Flight_Log.py`
("Deliberately unstyled — matches app.py") were already inaccurate
once `app.py` got styled in round 1 — corrected to point at `home.py`,
the file that actually carries the styling now.

**Tests**: `tests/test_home_page.py` rewritten — `AppTest.from_file("home.py")`
is now the primary target (matching every other page's own test
pattern, since `home.py` IS a page now, just one living at the repo
root), plus a light `AppTest.from_file("app.py")` check confirming the
router itself resolves every `st.Page()` path and renders the default
page. `st.page_link()` renders as a generic `UnknownElement` in
`AppTest` — no dedicated type, no `.click()` — said plainly: these
tests confirm every page-link's label is present, not that clicking
one actually navigates.

**Verification status**: full local suite — 196 passed (+4: three
`check_reachability` guard tests, one router test; the two DB-fixture-gated
`test_home_page.py` tests correctly skip, no `TEST_DATABASE_URL` here),
284 skipped, zero import errors. `scripts/check_reachability.py`:
clean — both `ENTRY_POINTS` (`app.py`, `home.py`) exist on disk,
confirmed by the very guard this round added.

**480/480 verified against real Postgres 16.** Arif confirmed the
sidebar genuinely reads "Home" and the page renders correctly — the
one thing `AppTest` couldn't answer either way. The user also
independently tested the reachability guard directly rather than
trusting the test suite alone: pointing `ENTRY_POINTS` at a nonexistent
file now exits 1 with "This checker cannot safely report anything
while blind to a declared entry point," where the identical scenario
previously exited 0 reporting "all reachable." **One open item, raised
on this same verification pass, not fixed here**: the background image
(`assets/AE-image.jpg`, 640x480) still reads as poor quality once
stretched across a full-page background — no higher-resolution source
was available; this needs either a better source photo or a design
change (e.g. a smaller/blurred treatment that hides the low resolution)
in a future round. Merged into `main`, pushed; branch
`home-page-branding` deleted, both remote and local. See `Merge status
as of this snapshot` near the top of this file for the authoritative
merge state, not this line.

## 2026-08-11 (continued): background image swap — 640x480 to 910x672.
## NOT YET MERGED.

Follow-up to the open item above. A new source photo arrived at
910x672 (up from 640x480) — real progress, but a first attempt to
land it on disk didn't go as described: the operator reported it as
already converted to JPEG quality 85 at ~80KB, but what was actually
found at `assets/AE-image.jpg` on the first check was unchanged (still
the old 640x480 file), and on a second check the new file had landed
as `assets/AE-image.png` instead — the original, unconverted 820KB
PNG, not the described JPEG. Verified directly rather than trusting
either description, per this session's own standing practice — caught
both mismatches before touching git.

Converted it here instead of bouncing it back again: `Image.open(...).convert("RGB")`
(PNG is RGBA; JPEG has no alpha channel) saved as JPEG quality 85,
replacing `assets/AE-image.jpg` in place (910x672, 84293 bytes ≈
82KB, matching the operator's own ~80KB target closely), the leftover
`assets/AE-image.png` removed. Same filename as before, so `home.py`
needed no code change — the base64-embedding and dim-overlay CSS
treatment are both untouched, deliberately: at 910px the image is
still upscaled roughly 2x on a typical desktop, and the existing dim
overlay is what makes that read as deliberate atmosphere rather than a
blurry photo, per the operator's own instruction to keep it as-is
rather than compensate further.

**Still not fully resolved, said plainly rather than closed out**: a
3000px+ original — likely available if this photo came from a phone
camera — would still be worth swapping in later. Same path
(`assets/AE-image.jpg`), same JPEG-quality-85 conversion approach, same
"verify actual dimensions/format on disk before committing" discipline
that caught this round's two mismatches.

**Verification status**: full local suite — 199 passed, 284 skipped,
zero import errors (unchanged counts — an asset swap, no code touched
beyond the one-off conversion script run directly, not committed).
`scripts/check_reachability.py`: clean. `home.py` re-run via `AppTest`
with the new file in place: no exception.

**Follow-up on the same branch, same day**: looking at the rendered
page, the dim overlay (`linear-gradient(rgba(10, 15, 35, 0.6), ...)`
composited under the photo, added in round 2 for the "screen read at
0300" legibility argument) made the image read as too dark — removed
per the operator's own request. Re-examined whether this actually
weakens legibility: it doesn't — the title/status/nav text sits inside
`.block-container`'s own near-opaque white panel (`rgba(255, 255, 255,
0.92)`), which is the real protection regardless of how bright the
underlying photo is; the dim overlay was a second, largely redundant
layer. `.stApp`'s `background-image` is now just the photo, no
gradient. Re-verified after the change: 199 passed, `check_reachability.py`
clean, `home.py` still loads without exception via `AppTest`.

**Second follow-up, same branch, same day**: looking at the rendered
page surfaced two more real fixes, from the operator's own description
of what was actually showing. (1) The seven `st.page_link()` buttons,
laid out in a `st.columns(4)` grid, wrapped icon and label onto
separate lines for the longer labels ("Roster Generation", "Schedule
Templates") — each column was too narrow, producing tall, awkwardly
stacked boxes instead of compact links. Fixed by dropping the grid
entirely: a plain sequential list, one `st.page_link(..., use_container_width=True)`
per row, each getting the full page width. (2) The UTC clock moved
from its own separate line ("Choose where to go below, or use the
sidebar — {timestamp}") to sit inline with the DB-status message
itself (`🟢 Database connected — 🕐 {ddmmyyyy HHMM} UTC`), with a clock
emoji and the format changed to compact `ddmmyyyy HHMM` (no
separators) per the operator's own spec — the leading 🟢 still gets
extracted into `st.success()`'s own icon slot as before; the clock
emoji further into the string stays as literal text, confirmed
directly rather than assumed. `tests/test_home_page.py` updated to
match: the UTC assertion now checks `at.success`, not `at.markdown`.
Re-verified: 199 passed, `check_reachability.py` clean.

**Third follow-up, same branch, same day — a fix reported as not
working, handled honestly rather than guessed at again.** The
icon-above-label stacking on the page-link buttons was STILL present
after dropping the `st.columns()` grid, reported directly from the
rendered page — proving the previous fix's premise (narrow columns
causing wrap) was wrong; the stacking wasn't a width issue at all.
Also fixed: the UTC date format, `ddmmyyyy` → `dd-mm-yyyy`
(`%d-%m-%Y`), a one-line change.

For the stacking: `st.page_link()`'s actual icon-vs-label layout is
decided in Streamlit's compiled frontend, not visible from the Python
source (`streamlit/elements/widgets/button.py`'s `_page_link()` only
sets protobuf fields — `icon`, `label`, `icon_position` — with no
layout logic of its own to read). Genuinely could not verify this one
directly the way nearly everything else in this file was verified, and
said so explicitly rather than asserting a third confident fix. Best
available reasoning, given two things already confirmed in this exact
file: (1) `st.success()`'s leading-emoji extraction shows Streamlit
alert bodies treat a leading emoji as a special, separately-laid-out
icon; (2) `page_link`'s own `icon=` parameter is plausibly the same
mechanism. Changed to fold the icon directly into the label string
(`label=f"{icon} {label}"`, no `icon=` parameter) — plain text has no
special icon-slot behavior, sidestepping whatever `icon=` does on its
own, regardless of whether that specific mechanism was the actual
cause. `tests/test_home_page.py` updated for the new label shape
(substring match, `"Control Room" in "🛫 Control Room"`, not exact
match) and for the date format (computed relative to the real
`datetime.now()` at test time, never hardcoded). Re-verified: 199
passed, `check_reachability.py` clean. **This one is explicitly
unconfirmed visually — flagged as the operator's own next check, not
claimed fixed.**

**Fourth follow-up, same branch, same day — a real screenshot,
confirming the icon fix and settling the buttons question.** The
operator sent an actual screenshot of the rendered page (not just a
text description) — first time this piece had genuine visual
verification rather than reasoning from source alone. Confirmed: the
icon-in-label-text fix from the previous entry worked, every page-link
showed icon and label correctly inline, no more stacking. Also
visible: a faint "Air Eagle" cursive watermark ghosting through
`.block-container`'s 92%-opaque panel, from the photo's own painted
fuselage script showing through the 8% transparency where the panel
happens to overlap that part of the image — flagged to the operator as
an emergent, not necessarily unwanted, effect rather than silently
left unmentioned.

Operator's actual call, having seen it working: remove the page-link
buttons entirely — `st.navigation()` already renders the sidebar's own
automatic page list regardless of anything on this page, making the
buttons a genuinely redundant second navigation surface, not a needed
one. `home.py`'s `PAGES` list and the `st.page_link()` loop removed;
the nav text reverts to "Use the sidebar to navigate." (accurate again
without "below" pointing at buttons that no longer exist).
`tests/test_home_page.py`: the page-link-label test and its
`_page_link_labels()` helper removed; a new test confirms zero
`UnknownElement`s remain in the tree (page_link was the only source of
that element type on this page) and the nav text reads exactly "Use
the sidebar to navigate." One combined test
(`test_utc_clock_is_inline_with_db_status_and_page_links_are_all_present`)
replaced by two narrower ones (`test_utc_clock_is_inline_with_db_status`,
`test_nav_text_points_to_sidebar_only`) — net one more test file-wide,
DB-gated like the one it replaced, so the locally-passing count stays
199; skipped goes 284 → 285. `check_reachability.py` clean. Merged into
`main`, pushed; branch `background-image-update` deleted, both remote
and local (per the operator's explicit merge instruction — full suite
+ reachability re-confirmed clean first, and confirmed nothing outside
`home.py`/`tests/test_home_page.py` changed).

## 2026-08-12/13: Flight-deck crew package — Commander/Second-Pilot
## seat model. IN PROGRESS, NOT YET MERGED.

The biggest architectural change to the assignment path since the
rebuild. Full design reasoning lives in the plan the operator
approved before any code was written — not reproduced in full here;
this entry tracks what's actually been built and the decisions made
along the way. Four empirically-reproduced defects drove it: no DB-
level concept of a flight-deck seat (five Captains on one flight was
`ALLOWED`), a pilot pair committed as two separate non-atomic calls
(first pilot real before the second was validated), `remove_
assignment()` corrupting a multi-sector duty on a partial unassign,
and `find_legal_candidates_for_duty()` offering candidates the real
gate would then refuse (NEEDS_MANUAL_REVIEW mislabeled legal).

**Core change**: new `roster.operating_position` (COMMANDER/
SECOND_PILOT, nullable) separates *seat on this flight* from `crew.
role`/`roster.role_assigned` (grade — CPT/FO/LM/ENGR, unchanged
meaning). Commander must be CPT-graded; Second Pilot may be CPT or FO.
Age-pairing rule (`_evaluate_pair_age`, `services/assignment_
service.py`) kept byte-for-byte identical, re-keyed from grade to
operating position — a CPT+CPT pair now goes through the exact same
domestic/international check a CPT+FO pair always did.

**Architectural fork, decided**: `operating_position` column + partial
unique indexes + a new atomic pair-assignment function (`migrations/
016_operating_position.sql`), not a separate `crew_packages` table —
`roster` stays the single source of truth for "who's assigned,"
avoiding exactly the drift-bug class `migrations/003_roster_
table.sql`'s own header calls "the single most repeated bug in this
platform's history." Migration 016 independently verified by the
operator against real Postgres 16 before implementation started — all
six invariants (seat CHECK values, Commander-must-be-CPT CHECK, the
flight-scoped partial unique index, and its non-collision with a
pilot's own multi-sector rows) confirmed.

**The one gap Option B leaves — durable UNCOVERED** (an uncovered seat
has no roster row, nothing to query once the generator's in-memory
`GenerationSummary` is gone after a refresh) — closed with a small,
negative-only `migrations/017_uncovered_seats.sql` table, not by
inflating the roster model. A sentinel/NULL-`crew_id` row was rejected
(breaks the `NOT NULL REFERENCES crew(crew_id)` FK, needs pervasive
filtering); live-diffing APPROVED rotations against roster coverage
was rejected (loses the real rejection reason from the specific
attempted-candidate ordering at generation time).

**Design decision, deliberately flagged for review rather than
assumed**: should `remove_assignment_from_duty()` (the new duty-scoped
unassign, replacing the old per-flight `remove_assignment()`) also
write to `uncovered_seats` when it leaves a rotation-linked seat
manually vacated? Decided **yes** — a controller manually removing a
Commander leaves that seat just as genuinely uncovered as a generator
search that never found one, and a silently-uncovered manually-vacated
seat would undercut the whole point of the table: being the single
durable source of truth for "which seats are currently empty," not
merely a generator failure log. Implemented via the same upsert
pattern the generator itself uses (`ON CONFLICT (rotation_instance_id,
operating_position) DO UPDATE`), with a distinct "Manually unassigned"
reason string so the two write paths stay distinguishable in the data.
**This means `uncovered_seats` is written by two call sites, not one
— `migrations/017_uncovered_seats.sql`'s header comment originally
scoped the table to "the generator's own bulk-fill use case" only,
which no longer matched actual usage once this decision was made.
Corrected in the migration file itself (2026-08-13) to describe both
writers; this log entry is the "why," the migration file is the
current "what."** Control Room's ad-hoc path remains the one genuine
exception — synchronous, always resolves immediately (REJECTED or
written), so it never leaves a durable gap to record.

**Built so far**: `migrations/016_operating_position.sql` (user-
verified against real Postgres), `migrations/017_uncovered_
seats.sql` (not yet independently verified), full rewrite of
`services/assignment_service.py` (`SEAT_ELIGIBLE_GRADES`, `validate_
pair()`, `assign_pair_to_duty()`, `assign_pair_to_new_flights()`,
`assign_crew_to_duty()`/`assign_crew_to_new_flights()` now reject CPT/
FO outright, `remove_assignment_from_duty()` replacing `remove_
assignment()`, `find_legal_candidates_for_seat()` replacing `find_
legal_candidates_for_duty()`), full rewrite of `services/roster_
generator_service.py` (seat-based pair search replacing sequential
per-role fill, `publish_window()` re-validating each rotation's actual
pair fresh before flipping PROPOSED→PLANNED), `pages/4_Roster.py`
(pair-based assignment form, duty-scoped unassign — verified via
mocked `AppTest`), and `pages/1_Control_Room.py` (ad-hoc pair-based
flow for pilots, original single-crew form kept for LM/ENGR/Other).

**`pages/6_Roster_Generation.py` done (2026-08-13)**: uncovered is now
read from `uncovered_seats` directly (new `get_open_uncovered_seats()`,
joined against `rotation_instances`) as its own always-current panel,
independent of session state — shown right after the date picker, not
gated behind clicking Generate. Fairness display is seat-aware
(Commander/Second Pilot duty counts, not CPT/FO). Publish now reports
how many rows remain PROPOSED after a publish click, surfacing the
per-rotation re-validation gate's skips rather than leaving them
silent.

**Real gap found and fixed while writing tests for this piece
(2026-08-13)**: `assign_pair_to_new_flights()`'s REJECTED/NEEDS_
MANUAL_REVIEW branch never called `log_audit()` — every other
rejection path in this file (`assign_pair_to_duty()`, `assign_crew_
to_duty()`, `assign_crew_to_new_flights()`) logs a record even when
nothing is saved, and this file's own existing tests establish that as
a settled invariant, not an incidental behavior. Fixed: `ADHOC_PAIR_
REJECTED`/`ADHOC_PAIR_HELD_FOR_REVIEW` audit records added, naming
mirrored off the LM/ENGR ad-hoc path's existing `ADHOC_FLIGHT_
REJECTED`/`ADHOC_FLIGHT_HELD_FOR_REVIEW` convention.

**Tests reworked (2026-08-13)**: `tests/test_assignment_service.py`
(full rewrite — `_assign_pilot()`/`_assign_pilot_adhoc()` helpers
auto-pair a subject pilot with a disposable, always-otherwise-legal
partner so the many FDP/rest/qualification/downstream-conflict tests
that were never really ABOUT pairing keep reading the same as before;
a dedicated pairing/composition section calls `validate_pair()`/
`assign_pair_to_duty()` directly for the tests that ARE about pairing).
`tests/test_roster_generator_service.py` (seat-based fairness,
`uncovered_seats` durability tests, `publish_window()`'s per-rotation
skip behavior — several scenarios re-derived, not just renamed: the
widened `SECOND_PILOT` eligibility (CPT-or-FO) and pair atomicity both
genuinely change which crewing outcomes are reachable, see each
test's own docstring). New `tests/test_flight_deck_seat_constraints.py`
— migrations 016/017's CHECK/unique/FK constraints proven directly
against real Postgres via raw SQL, same discipline as
`test_rotation_template_service.py`'s own migration-011/012
precedents. `tests/test_control_room_page.py`, `tests/test_roster_
page.py`, `tests/test_roster_generation_page.py` updated for the new
form layouts (`AppTest`, positional widget indices per this
codebase's established convention).

**Status: functionally complete.** Full local suite green (199
passed, 327 skipped — all DB-integration tests skip without
`TEST_DATABASE_URL` in this sandbox, same standing limitation as every
piece this session) and `scripts/check_reachability.py` clean. Diff
scoped exactly to what the plan named: `services/assignment_service.py`,
`services/roster_generator_service.py`, `pages/1_Control_Room.py`,
`pages/4_Roster.py`, `pages/6_Roster_Generation.py`, their tests, the
two new migrations, and this file — no other page or service touched.

**Not yet done**: the manual click-through this session's UI pieces
normally get before merging. One branch (`flight-deck-crew-package`),
merged as one unit once that's in — a half-migrated state would leave
the app genuinely broken against a shared `roster` table.

**First real-Postgres verification round (2026-08-14): 517 passed, 9
failed.** Diff scope confirmed exactly the 14 files the plan named;
`check_reachability.py` clean; migration 017 confirmed alongside 016.
All nine failures were investigated individually rather than patched
by adjusting expected numbers — eight were genuinely test-side, one
was a real, previously-undiscovered bug:

- **Real bug, fixed**: `publish_window()`'s per-rotation re-validation
  (the whole reason this piece exists — re-checking a PROPOSED pair
  fresh before flipping it to PLANNED) called `validate_pair()` on a
  pair that was ALREADY written. `_validate_new_duty()`'s history
  lookup had no way to know the candidate duty it was about to build
  WAS the same duty already sitting in the pilot's own roster history
  for those exact flight_ids — so the validator saw two duties
  covering the identical report/debrief window and correctly (from its
  own perspective) flagged a zero-rest violation, rejecting a pair
  that was actually still perfectly legal. `_recompute_one_duty_after_
  delay()` (pre-existing, unrelated to this piece) already had the
  right pattern for this — exclude the duty being re-validated from
  its own history before re-adding it — but `_validate_new_duty()`
  always mints a fresh synthetic duty_id, so it had no duty_id to
  exclude by. Fixed by excluding, from history, any existing duty
  whose OWN flight_ids exactly match the ones being validated — a
  correct proxy for "this is the same physical duty," safe for every
  other caller (a genuinely new assignment never has an existing duty
  for flight_ids it hasn't been assigned to yet, so the exclusion is a
  no-op there). New targeted test: `test_validate_pair_on_an_already_
  written_pair_does_not_double_count_itself_as_history` in `tests/
  test_assignment_service.py`, alongside the two integration-level
  publish tests that originally surfaced it.
- **Test-side, `tests/test_assignment_service.py` (2 fixed)**:
  `test_fill_remaining_seat_after_manual_unassign_uses_current_partner`
  had a fixture that contradicted its own stated intent (both pilots
  65+, asserted the initial pair ALLOWED when domestic rules correctly
  reject it) — rebuilt so the initial pair is genuinely legal and the
  refill scenario now proves something real: refilling against a 65+
  candidate correctly gets REJECTED against the still-active, real
  65+ Commander, not silently allowed. `test_find_legal_candidates_
  includes_candidate_when_other_seats_dob_missing` asserted the
  superseded pre-this-piece behavior (DOB-missing treated as
  "legal") — rewritten (and renamed to `..._excludes_...`) to assert
  the actual, correct, intended behavior: NEEDS_MANUAL_REVIEW, a real
  reason, excluded from the selectable set.
- **Test-side, un-reworked callers outside this piece's own test files
  (5 fixed)**: `tests/test_assistant_page.py`'s `_seed_duty_yesterday()`
  (used by 4 tests) and `tests/test_rotation_template_service.py`'s
  `test_expand_approve_then_assign_crew_reproduces_hand_verified_
  numbers` both called `assign_crew_to_duty(..., "CPT")` directly,
  which the new pilot guard now correctly rejects. The Assistant page
  tests aren't about pairing at all — switched to seeding the roster
  row directly via SQL, same pattern `test_assignment_service.py`'s
  own `_seed_duty()` already uses. The rotation-template test IS about
  proving the real gate computes correct numbers for a template-sourced
  flight — kept on the real API, switched to `assign_pair_to_duty()`.

**Second real-Postgres verification round (2026-08-14): 527/527
passed, zero failures.** Reachability clean, diff scope confirmed
exactly the 16 files across both commits. The operator independently
confirmed all four original defects closed, beyond this piece's own
test suite: five simultaneous Captain assignments on one flight now
blocked 5/5 at the API (0 written, was all 5 `ALLOWED`); a partial
unassign of a 2-sector duty now cancels the whole duty (0 rows left,
was 1 orphaned row carrying stale duty-level times); the solo-pilot
path now raises, the pair path commits together; a missing-DG
candidate now returns `status=NEEDS_MANUAL_REVIEW` with the real
reason instead of appearing in a bare "legal" list.

**Merged into `main`, pushed; branch `flight-deck-crew-package`
deleted, both remote and local (2026-08-14).** See the "Merge status
as of this snapshot" paragraph near the top of this file.
`migrations/016`/`017` still need applying to Supabase — production is
at 015 as of this merge.

## 2026-08-18: authentication for attribution — closing the NULL
## `audit_log.app_user` gap. MERGED into `main` 2026-08-18.

Every `audit_log` row written before this branch had `app_user` NULL:
the audit trail recorded WHAT happened and WHEN, never WHO. The 22
service functions that take an `app_user` parameter had all been
carrying it since they were written; nothing was ever passing one,
because there was no notion of a logged-in user to pass. This branch
adds that notion and threads it through.

**Scope is deliberately attribution, NOT access control.** Three fixed
OCC accounts, full access each, no roles/tiers, no self-registration,
no self-service password reset (re-running `scripts/seed_users.py` for
an existing username IS the reset mechanism, via `ON CONFLICT DO
UPDATE`). `migrations/018_users.sql` has no role/permission column on
purpose — adding one later is a migration, but shipping one now would
imply a tiered-access model the operator did not ask for.

- `services/auth_service.py` — `hash_password`/`verify_password`
  (PBKDF2-HMAC-SHA256, stdlib `hashlib`, 600k iterations, per-user
  16-byte random salt, `secrets.compare_digest` for the compare),
  `authenticate()`, and `require_login()`. No new dependency: bcrypt/
  passlib would be the reflex, and would be the wrong call for three
  internal accounts when the stdlib covers it.
- `scripts/seed_users.py` — operator-run account creation. The password
  is never a CLI argument (it would land unredacted in shell history)
  and never echoed; always `getpass.getpass()` twice.

**`st.login()`/OIDC was evaluated and rejected, and the reasoning is
worth keeping.** It does work with bare Google accounts — no Workspace
domain required — so the usual objection doesn't apply. It was rejected
because it authenticates identity without restricting entry: OIDC would
prove *some* Google account signed in, but any Google account would
satisfy it. Restricting to three specific people would mean maintaining
an allowlist of their Google identities on top of the OIDC flow, which
is strictly more moving parts than the three local accounts the settled
spec already calls for, for a deployment with no external users.

**The gate lives in every page, not only in `app.py`'s router.** This is
easy to get wrong in the direction that looks fine: `AppTest.from_file()`
execs a page script directly and bypasses `st.navigation()` entirely, so
a router-only check would leave every page unprotected under test — and
in production too, since the router is the normal navigation path, not
an enforcement boundary. Each of the 8 page files (`home.py` +
`pages/*.py`) calls `auth_service.require_login()` immediately after its
own `st.set_page_config()`; the function `st.stop()`s until
`session_state["app_user"]` is set, so nothing below it executes.

**Two structural guards, in `tests/test_auth_coverage.py`** — both for
failure modes where the broken state is indistinguishable from the
working one by reading any single file:

- **Missing gate.** 8 files each need one call; the failure mode is
  forgetting one, and an unprotected page renders identically to a
  protected one until somebody navigates straight to it. The guard
  globs `pages/*.py` + `home.py` (never a hardcoded list — a hardcoded
  list passes forever the moment page 8 is added and the list isn't
  updated) and asserts each calls the gate, at module level, before any
  service call that takes `app_user`. Same discipline as
  `test_check_reachability.py`.
- **Dropped `app_user`.** A call site that omits it writes a NULL
  `app_user` on a real audit record — the exact deficiency this branch
  exists to fix, and invisible unless someone thinks to query for it
  months later. The guard checks all 18 page call sites AND all 36
  service-internal forwards (a page can thread `app_user` correctly into
  `assign_pair_to_duty()` and still lose it if that function doesn't
  forward it down to `log_audit()`).

Both guards parse with `ast`, not grep, because the threading in this
codebase mixes keyword (`app_user=app_user`) and positional
(`_write_pair_rows(..., app_user)`) passing. A keyword-only grep reports
six false failures on the positional sites in `assignment_service.py`
and `roster_generator_service.py`; a substring grep for `app_user`
reports false passes on any line that merely mentions it. Only resolving
each callee's signature separates the two — which is also how the "36
sites are threaded" claim above was actually confirmed rather than
assumed.

**The guards were mutation-tested, not just observed passing.** Five
deliberate breakages — a new ungated page, a stripped gate, a page call
site dropping `app_user`, a service dropping a forward, and a gate moved
below the writes — were each confirmed to make the corresponding test
fail, and the tree restored afterward. A guard that has never failed is
not yet known to work.

**Known property, recorded deliberately: `app_user` identifies the
ACCOUNT that acted, not necessarily the person.** With three shared OCC
accounts, anyone who knows an account's password acts under that
account's name in the audit trail. This is acceptable at three OCC staff
and matches the settled spec — it is written down here so it surfaces as
a known limitation rather than being discovered mid-audit. Per-person
attribution would require per-person accounts, which is a spec change,
not a bug fix.

**Test-side change every future page test inherits:** all 9 `AppTest`
construction sites across the 8 page-test files now go through
`tests/conftest.py`'s `authed_app_test()`, which pre-sets
`session_state["app_user"]`. Without it a page test asserts against a
login form instead of the page, and fails confusingly — the page renders
with no exception, just none of its own content (this is exactly how the
two `test_home_page.py` failures first surfaced). It lives in `conftest`
for the same reason `_patch_all_service_engines` does: nine sites across
eight files is nine chances to forget. Tests that deliberately exercise
the unauthenticated path build their own `AppTest` directly.

**Verification status — read before merging.** 176 passed, 0 failed,
334 skipped. Every skip is DB-dependent: this machine has no Postgres
(no `TEST_DATABASE_URL`, no local server, no Docker), so the DB-backed
suite could not run here, exactly as it could not before this branch.
The static guards and the 8 runtime "unauthenticated page shows the
login form" checks need no database and did run. **The one test that
has never executed is
`test_writes_by_a_logged_in_user_never_leave_a_null_app_user`** — the
end-to-end proof that `app_user` actually lands in the column rather
than merely being passed. Its calls were verified to bind against the
real service signatures and to satisfy `REQUIRED_FIELDS`/
`UPDATABLE_FIELDS` (which caught two genuine bugs in it: `flight_number`
should be `flight_no`, and the required `domestic` was missing), but
binding is not execution. Run the DB suite against real Postgres before
merging, the same way the meal_provided and flight-deck pieces were
verified. `migrations/018_users.sql` also needs applying, and the three
accounts seeding via `scripts/seed_users.py`, before the app is usable
on any deployment.

### 2026-08-18 (continued): real-Postgres 16 verification — the branch did NOT pass

The paragraph above was correct about its own environment and wrong
about the branch. Against real Postgres: **484 passed, 10 failed, 57
errors.** Every one of those had been among the 334 local skips. This is
the fourth time in this file's history that a real-Postgres round found
what a local run could not; the standing practice earned its keep again.

**All 57 errors were one bug, in the test harness rather than the app.**
`authed_app_test()` passed a repo-relative path to
`AppTest.from_file()`. That method's resolution is two-stage and its own
source comments call it fragile: it first tries `script_path.is_file()`,
evaluated **against the current working directory**, and only falls back
to resolving against the *calling file's* directory. So a relative path
works when pytest runs from the repo root and resolves to
`tests/pages/...` — which does not exist — from anywhere else.

The subtlety worth recording, because the obvious diagnosis is slightly
off: moving the helper into `conftest.py` was not itself the trigger.
Both the old call sites and the new one live in `tests/`, so the
fallback branch produces the same wrong path either way. The real
variable is the CWD. Fixed by resolving through `conftest.page_path()`,
which returns an absolute path and so takes the first branch
unconditionally — independent of both the CWD and the calling file.
`test_auth_coverage.py`'s unauthenticated-page test builds its own
`AppTest` (correctly — it is testing the unauthenticated path) and
needed the same treatment; that was the 8 remaining failures in group
(a).

**Why the guard suite passed while 65 tests could not run at all.** The
structural guards check the *app*: every page is gated, every call site
threads `app_user`. The pages were correct. The *harness* was broken,
and every consumer of that harness is a DB-backed page test that skips
without Postgres — so nothing was left to fail. A structural guard
cannot report that the tests it protects never executed. Two responses,
both added here:

- `test_authed_app_test_resolves_every_page_to_a_real_file` and
  `test_page_path_is_cwd_independent` exercise the harness itself and
  need no database, so they fail in exactly the environment where the
  page tests cannot run. Both were mutation-tested against the original
  relative-path behavior, from a different CWD, and both catch it.
- `REQUIRE_DB=1` makes the DB fixtures **fail** instead of skip. Set it
  for any run whose result will be quoted as evidence the branch works;
  without it, "178 passed" and "178 passed, 334 skipped" look alike at a
  glance. `TEST_DATABASE_URL` must point at a throwaway database — the
  fixture opens with `DROP SCHEMA public CASCADE`, and this machine's
  `DATABASE_URL` is the production Supabase pooler.

**Still open: 10 form-submission failures** across Control Room, Crew
Data, Flight Log, Roster, and Roster Generation. The natural hypothesis
— that adding a login form shifted `at.button[0]`/`at.text_input[0]` —
was tested directly and **disproved**: with `session_state["app_user"]`
set, `require_login()` returns before rendering anything, and a probe
mirroring Crew Data's form structure produced identical widget counts
(1 text_input, 1 selectbox, 1 button) and a successful submission with
and without the gate. `test_require_login_already_authenticated_session_
skips_the_form` asserts the same property directly. The cause is
therefore something else and is not yet identified; it cannot be
reproduced on a machine whose only reachable Postgres is production.

### 2026-08-18 (continued): the 10 failures were an unpinned dependency

Resolved, and the answer was in neither the branch nor the tests.
Running the same failing tests against unchanged `main` — the check
that separates "caused" from "exposed" — reproduced them exactly. The
sandbox that produced the earlier 527/527 had been recycled and its
dependencies reinstalled from scratch. `requirements.txt` said
`streamlit>=1.38`, so the resolver picked **1.61.1** instead of the
**1.60.0** everything had been verified against. 1.61.1 changed
`AppTest` behaviour: unchanged `main` went 527/527 → 468 passed, 2
failed, 57 errors, with the same `FileNotFoundError` signature.

Worth being precise about how much of this was a red herring, because
each layer looked like the real bug at the time:

- The `7 → 10` widget growth is **normal** `st.rerun()` behaviour. It
  reproduces with and without the login gate, and on `main`. It is the
  add form (7) plus the edit section (3) that renders once a crew record
  exists, and it proves only that the write succeeded.
- The missing `st.success` is what `st.rerun()` legitimately discards.
- The login gate is genuinely transparent when authenticated:
  `require_login()` returns before rendering anything, verified by
  running the **real** `pages/2_Crew_Data.py` through AppTest with
  `crew_service` faked in memory — identical widget counts to the
  failing run, and the assertion passed.
- The timing-race hypothesis was wrong too. It was a version difference.

**Everything is now pinned exactly.** `requirements.txt` carries the 7
direct dependencies at exact versions; `requirements.lock` (53 entries,
`pip freeze`) carries the full transitive set. `>=` on a test-facing
dependency means the suite tests whatever the resolver picked that
morning, which is not a controlled input — this was already flagged in
this file as a known gap, and it cost two verification rounds to
collect on it.

`tests/test_dependency_pinning.py` guards both, DB-free: the pins stay
exact, the lock agrees with them, and — the one that would have caught
this at its source — the **installed** versions match the pins. In the
recycled sandbox that check would have said "streamlit 1.61.1 installed,
1.60.0 pinned" on the first run, instead of 57 errors that read as a
code regression. All three guards were mutation-tested, including
reproducing the exact drift.

Also fixed in passing: `openpyxl` was declared in `requirements.txt` but
had never been installed in the local venv, so
`test_import_crew_script.py` and `test_reporting_export.py` failed at
collection and their 41 tests had never run locally at all. Installing
it took the local run from 178 to **219 passed, 0 failed, 334 skipped**,
and it is now in the lock — a naive `pip freeze` from the old venv would
have silently omitted it.

**Open, scoped as its own piece of work: Streamlit 1.61.x
compatibility.** Pinning to 1.60.0 restores green but leaves the project
one version behind, and the same failures will reappear at the next
upgrade. That task is: understand what changed in 1.61's `AppTest`,
decide whether the affected assertions should be robust across both
versions (several assert on messages that `st.rerun()` discards, which
is fragile independent of version), then move the pin deliberately with
a full real-Postgres run. Not a blocker for auth.

### 2026-08-18 (continued): green, and merged

**563 passed, 0 failed against real Postgres 16 on the pinned
environment.** Reachability clean. This supersedes the "Verification
status — read before merging" paragraph above, which was accurate when
written and is now historical: the test it named as never having
executed anywhere,
`test_writes_by_a_logged_in_user_never_leave_a_null_app_user`, **passed
on this run** — the end-to-end proof that `app_user` actually reaches
the column rather than merely being passed to a function that accepts
it. Auth and coverage suites 26/26. `migrations/018_users.sql` applied
to a database already at 017 carrying existing crew data: table created
correctly, existing rows intact.

The dependency guard proved itself on first contact rather than in
theory: the fresh sandbox had `pandas` and `sqlalchemy` off-pin, and
`test_dependency_pinning.py` reported "installed X, pinned Y" precisely,
where the same class of drift had previously surfaced as 57 errors that
read as a code regression. Realigning took one command.

Merged into `main`, pushed; branch `auth-and-attribution` deleted, both
remote and local. See the "Merge status as of this snapshot" paragraph
near the top of this file.

## 2026-08-19: Schedule Templates — widget-key data corruption, HHMM
## times, leg continuity, delete-when-unused. MERGED into `main`
## 2026-08-19.

Found in real use. An operator created EPE-786-787 (2 legs), then
EPE-802-804-805 (3 legs) without reloading the page. The second
template saved **leg 2 carrying the first template's values** — LHE→KHI
22:00-23:45 domestic instead of LHE→DWC 04:30-08:00 international — and
**leg 3 was missing entirely**. This is silent data corruption in the
schedule that later becomes real flights, so it is the most serious
defect found in this project to date.

**One root cause, two symptoms.** The leg widgets used fixed keys
(`{key_prefix}_flightno_{i}` etc.) with no generation counter. Streamlit
ignores `value=` once a widget with that key exists, so the second
submission inherited the first's values wherever the controller didn't
overwrite every field. Leg 3's disappearance was NOT a separate
continuity problem: `_collect_and_validate_legs()` treated a row as
"filled" only if `flight_no`/`origin`/`destination` had content, ignoring
times. Leg 3 had times entered but its text fields still stale-empty, so
it was silently skipped as blank. Same single cause — the time widgets
updated, the text widgets didn't.

The fix is the generation counter already used for
`st.session_state.assistant_generation` on `pages/5_Assistant.py`,
threaded into every leg widget key and bumped after each successful
save. Applied to the create form and the "create new version" form.

**On the "create new version" form, honestly stated:** the same pattern
was there (prefix varied by `rotation_code` but not by submission) and
is now fixed, but I could not construct a single-user flow where it
corrupts data. Its stale content always equals the last saved content
for that code, so the wrong value and the right value coincide. What it
DID affect is every widget whose default comes from `cv_current` —
days/meal/snack/legs — which all change the moment a version is saved,
so the form showed the previous submission rather than the newly-current
version. Those are regenerated now. `cv_code` (the selector, which must
persist) and `cv_effective_from` (defaults to `today()`, nothing
template-derived to go stale) deliberately are not; the reasoning is in
the page comment so it reads as a decision rather than an oversight.

**Silent skip replaced by a named error.** A partially-filled row now
reports which fields are missing instead of vanishing. This only became
expressible because of the next item: `st.time_input` always yields a
value, so a row could never be "empty" in the time columns.

**HHMM text entry replaces `st.time_input`** (operator request): a
dropdown is slow for four times per rotation and controllers already
write times as `0905`. Accepts `0905` or `09:05`, rejects anything else
with a message naming the value, labels say UTC explicitly.

**Route continuity now checked at creation.** `_validate_legs()` gained
the destination→next-origin check that `core/duty_builder.py`'s
`build_duty()` has always had. Previously a disconnected template saved
cleanly and only failed at expansion, days later and far from the
mistake. It lives in the service, not the page, so a non-UI caller
can't sidestep it, and the message echoes `build_duty()`'s so the two
never disagree.

### Delete, and why it needed no trigger bypass

Recovering from the corrupted template required manually disabling the
immutability triggers on the live database, which is not an acceptable
workflow. The question posed was how to scope a bypass so it couldn't
become a general escape hatch. **The premise was wrong: no bypass is
needed.** `migrations/019` moves the condition INSIDE the guard —
"DELETE is never allowed" becomes "DELETE is allowed only when no
`rotation_instances` reference this template". There is then nothing to
bypass, and the rule applies identically to the service layer and to a
hand-written `DELETE` in psql. The guard gets narrower in scope and
stronger in kind. Every alternative was an escape hatch by construction:
`session_replication_role = replica` disables all triggers session-wide;
`ALTER TABLE ... DISABLE TRIGGER` is global rather than session-scoped,
leaving concurrent sessions unprotected (this is what the manual
recovery used); a session GUC flag is a reusable "turn the guard off"
switch.

**Sole-version only, and this is a real limitation.**
`create_new_version()` closes the previous row's `effective_until` and
sets `superseded_by`, and the guard permits that exactly once. Deleting
a v2 would require reopening v1's `effective_until`, which the guard
forbids — correctly. So deletion covers a template that is the only
version of its code: precisely the "just created it by mistake" case. A
bad v2 is superseded by a v3.

**The foreign-key audit is load-bearing.** "Unused" is only a true
statement if the set of things that can reference a template is the set
the trigger checks. Confirmed before implementing:
`rotation_templates.superseded_by` (self), `rotation_template_legs.
template_id`, `rotation_instances.template_id`, and nothing at all
referencing `rotation_template_legs`. Since `flights.rotation_instance_id`
points at instances, "no instances" implies no flights. That set is now
pinned by a test against `pg_constraint`, so a future migration adding a
referencing table fails loudly rather than letting a delete orphan it.

**Explicitly rejected: a separate "dormant" state.** Recording this so
it isn't re-proposed. Versioning already covers retirement-with-
replacement, and `effective_until` covers a rotation stopping
permanently. Delete-when-unused covers the only remaining case, a
template that never produced anything. Three mechanisms for what is
really one question — "is this schedule still in use?" — would be two
too many, and each additional state multiplies the combinations every
expansion and generation path has to reason about.

**Also worth recording: the guards were not the problem.** The
immutability trigger and the version-overlap EXCLUDE constraint both
fired correctly throughout. The fault was a guard applied where there
was no history to protect, which `migrations/019` fixes without
weakening either.

**Verification status.** 235 passed, 0 failed, 355 skipped locally;
reachability clean. Every new DB-backed test skips here — this machine
still has no Postgres, and `DATABASE_URL` is the production Supabase
pooler. **`migrations/019` has never been executed anywhere.** It
rewrites two guard functions that have been protecting live data since
`migrations/011`, so roughly half of
`tests/test_rotation_template_delete.py` asserts the OLD rules still
hold — legs `UPDATE` still refused (including when the parent is
deletable, since deletable and mutable must not be conflated), immutable
template columns still refused, `effective_until` still closable exactly
once. A rewrite of a guard is exactly where an unintended relaxation
hides, and none of that would show up in a test of the new behaviour.
Needs a real-Postgres run against a database already carrying 018 and
real data before it goes near Supabase.

### 2026-08-19 (continued): first real-Postgres round — 577 passed, 13 failed, all test-side

**`migrations/019` is sound.** It applies cleanly, and the
UPDATE-on-legs case was tested directly against the live rewritten
trigger and is still refused, from
`reject_rotation_template_legs_mutation()`. The silent relaxation that
was the whole reason for pinning the old rules did not happen. All 13
failures were in test code, from three causes:

1. **The `_create()` helper omitted `meal_provided`/`snack_provided`,
   which are REQUIRED parameters of `create_template()`.** Every test in
   `test_rotation_template_delete.py` failed at setup, so **the entire
   delete and trigger-regression suite never executed** — including the
   test written specifically for the deletable-but-still-immutable leak.
   This is the same shape as the two environment failures logged on
   2026-08-18: a suite that cannot run reports nothing, and locally it
   was indistinguishable from a suite that skipped for want of Postgres.
   Every `rts.*` call across the changed test files is now bind-checked
   against the real signatures with `inspect.signature().bind()` — the
   technique that caught two such bugs during the auth work, applied
   this time to all calls rather than only the reported one.

2. **`test_rotation_template_legs_delete_is_rejected` encoded behaviour
   `migrations/019` deliberately changed** — it asserted legs deletion
   is refused unconditionally. Rewritten as a pair, so the file
   documents the new rule in both directions: refused once the template
   has instances, permitted while it has none.

   Found while fixing it: **`test_rotation_templates_delete_is_rejected`
   was still PASSING, for the wrong reason.** It deleted the template
   while its legs were still present, so the legs' foreign key raised
   before the guard was ever consulted. It read as proof the guard
   refuses and was actually proof that a foreign key does. Now it
   removes the legs first so the DELETE reaches the guard, and asserts
   the guard's own message.

3. **Label lookup was ambiguous.** The create form and the
   "create new version" form both render "Description" and
   "Days of week *", so the regression test for the reported bug — the
   one that most needed to run — failed on `found 2`. The create form's
   non-leg widgets had no keys at all, which is not the same as being
   stateless: Streamlit auto-keys them, so after a save the form still
   held the previous template's code, description and weekdays. That is
   the reported defect with a quieter symptom, so they are now
   generation-keyed like everything else, which fixes the staleness and
   the ambiguity together.

**Verified DB-free before pushing**, since the tests that matter still
skip locally: the real page was run through AppTest with
`rotation_template_service` faked in memory, in both states (no
templates, and one existing template). That reproduces the exact
ambiguity — 2 widgets per label once a template exists — and confirms
every key lookup the tests use resolves to exactly one widget. A wrong
key pattern would otherwise have failed the same way on the next round.

### 2026-08-19 (continued): second round — 590 passed, 1 failed, and the message came from the wrong guard

The delete and trigger-regression suite executed properly for the first
time, including
`test_updating_a_template_leg_is_refused_even_when_the_template_is_deletable`,
which passes: an unused template is deletable and its legs are still
immutable, which was the leak worth pinning.

The single failure was not a security gap. Deleting a template that HAS
instances was correctly refused — but by the **legs** trigger
(`rotation_template_legs rows are immutable — create a new template
version instead`), because `delete_template()` removes legs first and
so never reached the template DELETE. The invariant held perfectly; the
explanation was about the wrong guard. A controller clicking Delete on a
rotation that is in use would have been told about leg immutability,
which is true and says nothing about why that template can't go.

**Fixed by checking deletability before touching anything**, which
means reversing reasoning previously written into
`delete_template()`'s own docstring — it argued against a pre-check on
TOCTOU grounds and because it would imply Python enforces the rule. That
was half right, and the half it got wrong is the direction the race can
fail in: **a pre-check can only ever produce a better error message,
never a permission.** If an instance appears between the check and the
DELETE, the trigger still refuses and the caller sees the trigger's
message — exactly the old behaviour. The check can never turn a "no"
into a "yes". So the layering is: the check exists for the message, the
trigger is the truth.

To keep that claim honest rather than asserted,
`test_the_trigger_still_refuses_a_delete_that_bypasses_the_service`
issues the DELETE directly, as a hand-written recovery would, and
expects the database to refuse with no help from the service layer.
Without it, moving the check into Python would quietly turn a
database-level guarantee into an application-level convention any other
caller could sidestep.

The decision itself now comes from `migrations/019`'s own
`rotation_template_is_deletable()` rather than a Python
reimplementation, so the message a controller sees cannot describe a
different rule from the one enforced. Python's remaining job is turning
"false" into a sentence, which is the one thing a BOOLEAN can't carry.

**Deployment consequence, stronger than before:**
`pages/7_Schedule_Templates.py` now calls that SQL function on every
render that lists a sole-version template, so the page **requires
`migrations/019`** — against a pre-019 database it fails to render at
all, not merely to delete. 019 must be applied before this page is
deployed.

### 2026-08-19 (continued): green, and merged

**592 passed, 0 failed against real Postgres 16.** Reachability clean.
The four that carried the most risk all pass:

- `test_the_trigger_still_refuses_a_delete_that_bypasses_the_service` —
  the database guarantee is still enforced by the database, not
  downgraded to an application convention when the pre-check moved into
  the service.
- `test_updating_a_template_leg_is_refused_even_when_the_template_is_deletable`
  — deletable and mutable stayed separate; the relaxation the guard
  rewrite could most easily have introduced did not occur.
- `test_immutable_template_columns_are_still_refused` — the pre-existing
  rule survived the rewrite.
- `test_second_template_created_without_reload_does_not_inherit_the_first`
  — the reported corruption, genuinely fixed, on the test that took two
  rounds to actually execute.

**Three real-Postgres rounds, and none of them found a fault in
`migrations/019`.** It was sound from first application. Round 1 (13
failures) and round 2 (1 failure) were both test-side. That is worth
recording because it is the same lesson as 2026-08-18 from a different
angle: the risk in this project has repeatedly been in whether a test
*ran* and whether it asserted the *right thing*, not in the change under
review. Round 1's helper-arity bug meant an entire suite never executed;
round 2's failure was a correct refusal reported by the wrong guard.

Merged into `main`, pushed; branch `schedule-template-fixes` deleted,
both remote and local. See the "Merge status as of this snapshot"
paragraph near the top of this file — including the deployment-ordering
warning, which is a harder prerequisite than any previous migration.

## 2026-08-19 (continued): the deployed page went down, and it was not the Python version

`migrations/019` applied to Supabase cleanly (`Applied: 20, Pending: 0`),
and the deployed Schedule Templates page then crashed on render with
`AttributeError` at the deletability lookup. The page was **down for the
operator** — unable to view, create, expand or review anything, and
unable to clean up the very templates the delete feature exists to
remove.

**The reported hypothesis was that `pandas 3.0.5` behaves differently at
`versions.iloc[0]["id"]` on Streamlit Cloud's Python 3.12 than on the
local venv's 3.14. That is disproven.** A 3.12 venv built from
`requirements.lock` gives byte-identical results for `read_sql`,
`.iloc[0]["id"]`, `int()`, and `_deletability()`'s `.mappings().first()`
row access. The full suite also passes on 3.12 with exactly the same
numbers as on 3.14 (238 passed, 357 skipped). The DataFrame layer and
the interpreter version are both uninvolved.

**What actually fits every fact.** `get_versions()` and
`get_template_legs()` on the same `rts` module succeed earlier in the
same render, so the module imported fine — which leaves
`rts.get_template_deletability` itself absent at runtime. Both files ship
in the same commit (`4a646da`), so they agree on disk. The mechanism is a
**stale module object**: Streamlit re-executes the page script on every
rerun but keeps imported modules in `sys.modules` for the life of the
process, so a page updated without a full process restart calls into the
previously loaded module. This was the first deploy where a page calls a
**newly added** service function, which is why it is the first to fail
this way — earlier deploys changed existing functions' signatures, which
a restart happened to cover. Confirm from the app log's message text; a
reboot resolves it.

**The durable fix is not the diagnosis, it's the blast radius.** Whatever
made the lookup fail, a convenience that decides whether one button is
greyed out should never be able to take down the page that lists the
schedules. The lookup is now wrapped: on any failure the delete control
is shown disabled **with the reason**, and everything else renders. Not
silent — a degraded control says so rather than looking like an ordinary
undeletable template.

That also removes the hard migration prerequisite this call had
introduced, so the deployment-ordering warning near the top of this file
is now softer than when it was written.

**Why the suite didn't catch it, which is the part worth keeping.** Every
test covering this page was DB-gated and skips wherever Postgres is
absent — so nothing anywhere had ever rendered this page in an
environment without a database, which is precisely the condition that
broke. The three new regression tests are deliberately **DB-free**: they
fake the service layer, render the real page, and assert it survives the
attribute being missing entirely (the actual outage) and a pre-019
database. Mutation-tested: both fail without the wrap. This is the third
time in three pieces that the gap was "the test could not run here",
after the Streamlit version drift and the `openpyxl` absence.

**Python 3.12 alignment** is being handled separately (`runtime.txt` plus
a version guard) — worth doing on its own merits, since tests running on
a different interpreter from production is a real latent risk, but it
would **not** have caught this outage and is not being presented as the
fix for it.

## 2026-08-19 (continued): the review table showed the wrong duty window

Draft expansion was correct — right rotations, right dates, full routes —
but the review table displayed **first-departure and last-arrival** under
headings "Report" and "Debrief". Those are not the same thing: ANO-012
D7.1.2 adds a pre-flight and post-flight buffer either side.

Real production rotations:

| rotation | table showed | actual duty | buffers |
|---|---|---|---|
| domestic | 19:00 → 23:45 | **18:15 → 00:00** | 45 / 15 |
| international | 01:45 → 11:00 | **00:45 → 11:30** | 60 / 30 |

A controller judging whether a rotation is flyable read the FDP as about
an hour shorter than it is, and the draft contradicted what the Roster
page showed for the same rotation once crewed. **Display only** — the
stored data was right, and `rotation_instances` correctly holds no
report/debrief, since FDP is a property of a duty and a draft has no
crew.

**Relabelling to "First dep"/"Last arr" was rejected.** It would have
been honest and trivial, but the duty window is the thing actually worth
reviewing — a controller looking at a draft is asking "is this flyable",
and first-departure does not answer that.

`rotation_template_service.compute_duty_window(legs)` computes it via
`core.duty_builder.build_duty()`. The calculation lives in the service
rather than the page for a specific reason: `build_duty()` takes ONE
duty-level `domestic` flag while legs carry one each, and
`assignment_service` resolves that with `all(bool(leg["domestic"]))` at
five separate sites — a duty is domestic only if EVERY leg is, so one
international sector applies the longer 60/30 buffers to the whole duty.
Re-deriving that in a page would have been a sixth copy of a rule the
page has no business knowing, and getting it wrong understates the
window, which is the very direction of error being fixed. Routing
through `build_duty()` also means the draft review and the Roster page
now agree **by construction** rather than by two copies of the same
arithmetic.

It returns `None` rather than raising, and the page shows "—":
`build_duty()` raises on out-of-order legs, and a display value must
never be able to take the review table down — the same lesson as the
delete affordance earlier the same day. Both production figures are
pinned as DB-free tests, along with the mixed-domestic case.

### Two smaller findings from the same session

**Text fields needed Enter before submitting.** Filling several leg
fields and clicking submit without pressing Enter on the last left it
uncommitted, and the page then reported "Leg 1 is partially filled —
missing origin, destination, departure time, arrival time". The message
was good; it pointed at the wrong cause. This only ever affected the
"create new version" section, which used a bare `st.button` — the create
form above has always been an `st.form`, which is why the same leg
widgets behave correctly there. Replacing `st.time_input` with text
entry is what exposed it: a time input commits on selection, a text
input only on Enter or blur. That section is now an `st.form` so its
fields commit together. `effective_from` stays outside it (it drives the
live "this will end version N on …" preview), and so do
`open_ended`/`effective_until` — a checkbox inside a form doesn't take
effect until submit, so the conditional date field would not have
appeared until after a first submit.

**The delete control had no signposting.** It sat immediately below the
"Show all versions' legs" checkbox with nothing between them, so a
destructive action read as part of a display toggle — and the expander
itself is labelled only with the rotation code. Now separated by a
divider under its own "Delete this template" heading.

## 2026-08-19 (continued): st.form and widget commit timing — a known wart, recorded so it isn't rediscovered

Recorded as its own entry because it is a **live, un-fixed behaviour**
in the create form, deliberately left alone, and it will otherwise be
found again and filed as a bug.

**The rule.** A widget inside an `st.form` does not take effect until
the form is submitted — that is what makes text fields commit together.
Outside a form, an `st.text_input` commits only on Enter or blur.
Neither is a defect; they are the same mechanism seen from two sides,
and each is wrong for the other's use case.

**What was fixed (2026-08-19).** The "create new version" section used a
bare `st.button`, so filling several leg fields and clicking submit
without pressing Enter on the last left it uncommitted, and the page
reported *"Leg 1 is partially filled — missing origin, destination,
departure time, arrival time"*. The message was accurate and pointed at
the wrong cause. That section is now an `st.form`.

Worth stating precisely, because it was reported more widely than it
was: **the create form was never affected.** It has been an `st.form`
since it was written, which is why the same leg widgets behaved
correctly there. Replacing `st.time_input` with HHMM text entry is what
exposed the gap — a time input commits on selection, a text input does
not — so the fault was introduced by that change but only in the one
section that lacked a form.

**What is deliberately NOT fixed, in both forms.** Toggling
"Open-ended (no end date)" does not reveal the "Effective until" date
field until after a submit, because the checkbox is inside the form and
so does not take effect until then. This is pre-existing in the create
form and was not reported; fixing it means moving the checkbox and its
dependent date field outside the form, which is what the new-version
section now does:

    # outside the form — a checkbox inside one does not take effect
    # until submit, so the conditional field would not appear
    cv_open_ended = st.checkbox("Open-ended (no end date)", ...)
    cv_effective_until = None if cv_open_ended else st.date_input(...)

    with st.form(...):
        ...everything else...

So the two forms now differ: the new-version section reveals the date
field immediately, the create form does not until a submit has
happened. That inconsistency is known and accepted rather than
overlooked. Applying the same treatment to the create form is a small,
safe change whenever it is worth a verification round — it was out of
scope for a fix to a different section.

**General principle for this page.** Anything whose VISIBILITY depends
on another widget belongs outside the form; anything that is just a
value to submit belongs inside it. `cv_effective_from` is outside for
the same family of reasons — it drives the live "this will end version
N on …" preview, which has to update before submit to be a preview at
all.

## 2026-08-20: first operational-use findings — renewal, occupants,
## Control Room, home banner. MERGED into `main` 2026-08-20.

Six findings from the operator's first real use of the deployed app,
ordered by whether they blocked work.

**Qualification renewal was impossible through the UI (blocking).**
`crew_service.UPDATABLE_FIELDS` has always accepted all eight expiry
fields, but the Crew Data edit form exposed only phone, email and base,
so renewing a medical or licence required raw SQL. Documents renew
constantly; this blocked normal operation. All eight plus date of birth
are now on the form.

Expired dates are marked in the label and **stay editable** — renewing
one is the entire purpose, so `disabled=True` would lock the operator
out of exactly the field they came to change. The marking uses the
legality gate's boundary (`expiry <= today` is already expired), not the
everyday reading: a field saying "valid" while the roster refuses the
assignment would be worse than no marking at all.

**These widgets deliberately carry NO `key=`.** They use
`value=selected[...]` and rely on Streamlit deriving the widget id from
its parameters, `value` included, so switching crew member re-renders
correctly — verified by driving the real page with two crew members and
switching between them. Adding a stable key would BREAK it: a keyed
widget ignores `value=` once it exists, so the form would show the
previous member's data while writing to the newly selected `crew_id`.
This is the exact OPPOSITE of the Schedule Templates fix, where keys are
stable across submissions and needed a generation counter. Same
mechanism, opposite correct answer, and the difference is only whether
the intended reset is driven by `value=` or by a key. Worth reading both
comments together before touching either.

**Other occupants could not be recorded anywhere.** The columns have
existed since `migrations/010`, `flight_service.UPDATABLE_FIELDS`
accepts them and `reports.roster_coverage()` has always displayed them —
but no page ever wrote them, so the LM and AMEs aboard every real Air
Eagle flight had nowhere to go. Added to Flight Log (add and edit) and
Control Room. Free text by design. In Flight Log's edit form they go
through `update_flight()`, kept deliberately separate from
`update_flight_actual_times_and_revalidate()`: correcting a name on an
occupant list must not drag a crew member's duty back through FDP
revalidation.

**Control Room's single-crew path removed.** There is no such thing as a
flight operated by one crew member. It predated the pair model, from
when LM and AME were crew records assigned individually. It was already
nearly unreachable — Crew Data creates CPT/FO/Other, the branch offered
LM/ENGR/Other, and `assign_crew_to_new_flights()` rejects CPT/FO
outright AND requires role to match the person's registered role — but
one combination still worked: an "Other" crew member assigned role
"Other", creating a flight with no flight deck at all.

`assign_crew_to_new_flights()` itself is **kept**, with a comment on it
saying why, because its tests state a pair-model guarantee by contrast
that has nowhere else to live:
`test_assign_crew_to_new_flights_rejects_pilots_outright` pins that a
solo pilot assignment cannot bypass pair-atomicity. Deleting the
function would delete that assertion. It has no UI path, and that is
recorded on the function so the next reader doesn't find an uncalled
function and remove it.

**Control Room crew is now optional.** "Charter confirmed, crew TBC" had
no path, which forced the operator into Flight Log for the same job and
made this page read as redundant. Checked before building: nothing
downstream assumes a Control Room flight has crew. Flight Log has always
created uncrewed flights, every flights-to-roster join is roster-driven
so one simply doesn't appear, and `roster_coverage()` already reports an
empty seat as UNCOVERED. The pair path calls
`assign_pair_to_new_flights()` **unchanged** and the flight-only path
calls `add_flight()` — deliberately different calls, so the "illegal
pair leaves no orphan flight" guarantee is preserved by not being
touched. Requesting a pair that cannot be formed refuses rather than
silently downgrading to an uncrewed flight.

### The home-page ops banner, and the boundary in its count

Two counts on landing: uncovered seats today, and documents expired /
expiring within 7 days. Presentation over existing services.

**`crew_qualifications()` was the wrong shape and is not what the banner
calls.** It takes a `query_parser.ReportRequest` and returns an
exportable `Dataset` — the assistant's natural-language surface. A
landing page should not construct an NL request object to learn a count.
Rather than duplicate the query, the expiry-window predicate now lives
once in `assignment_service.expiry_in_window()`, and
`reports._expiry_in_window()` delegates to it — so the banner and the
report can never disagree about the same crew member. Same reasoning as
`compute_duty_window()` routing through `build_duty()`.

**Expired and expiring are counted separately and never summed.** The
legality gate treats `expiry <= duty_date` as already expired, so a
document expiring TODAY is blocking assignments right now rather than
"due soon". One combined number would hide that behind a word implying
there is still time.

**Recorded so a future reader doesn't treat it as a bug: the uncovered
count is rotation-only.** `get_open_uncovered_seats()` reads the
`uncovered_seats` table, which ONLY the roster generator populates, and
only for rotation instances. An ad-hoc flight saved with crew TBC never
appears there — which is precisely the flight the crew-optional change
above makes easy to create. The count is therefore labelled "Uncovered
rotation seats today" with a caption saying ad-hoc flights are excluded,
rather than being widened.

**Trigger for revisiting:** if the operator reports ad-hoc gaps going
unseen, the fix is to widen the count to flights with an empty cockpit
seat, accepting the per-flight roster lookup on every home page load
that `roster_coverage()` does. That cost is the only reason it is not
already done.

**The banner is SKIPPED, not merely wrapped, when the DB is
unreachable.** Found while building it: `try/except` catches a failing
query but not a hanging one, and against an unreachable database the two
queries sat in connection retries until the page took over three seconds
to render. It was still correct and still rendered — it just stopped
being usable at the moment an operator most needs to see something.
`db_status` is already known one line above, so the banner is gated on
it. Each half is still independently wrapped for the case where the
connection is up but one query fails on its own.

### Two smaller items

**Crew and flight identity display.** A controller thinks "AE92" and
"EPE 786", not `CPT-01` and `#4242`. `services/display_labels.py` is now
the single home for how both are named on screen — nine `format_func`
call sites across four pages each used to roll their own. `crew_id` and
`flight_id` are unchanged and remain the identifiers; only the label
moves. The fallbacks are the whole difficulty, and are why this is one
module rather than nine copies: one real crew member has no
`operator_staff_id`, and every ad-hoc flight has a null `flight_no`.
Flight labels carry the date because flight numbers repeat daily.

**The UTC clock read as stale.** It was a render-time snapshot displayed
as a clock face, so a page left open showed a time ten minutes old.
Static would have been tolerable; wrong is not, on a system where times
drive legality and a controller reads UTC off the screen while entering
duty times. Relabelled to "checked {time} UTC" rather than removed:
worded as the moment the check happened, it does a real job — it says
how fresh the connection status beside it is. No auto-refresh; that is a
dependency and a wakeup cycle for one line of text.

### Not a bug: Control Room's pair fields

Reported as a possible gap — that Control Room showed only a "Role *"
selectbox rather than Commander and Second Pilot. It was fully migrated
in the flight-deck package. "Role *" appeared only in the single-crew
mode of the "Crew type" radio, whose selection persisted across sessions
via `key="control_room_crew_type"`. Investigated before building, and it
reordered nothing — but the finding did lead directly to removing that
mode entirely, above.

### Explicitly not doing: duty swap in Control Room

Swap stays a Roster operation. One write path through the legality gate,
not two. Operator agrees; recorded so it isn't re-proposed.

### 2026-08-20 (continued): verified green, merged, and a deployment rule worth naming

**621 passed, 0 failed against real Postgres 16.** Reachability clean.
The three paths that could not run locally were checked directly: a
medical expired in 2020 renewing to 2030-06-01 through `update_crew`
(the blocking gap genuinely closed), occupant free text round-tripping
as an operator would type it, and the label fallbacks for a null staff
ID and a null `flight_no`.

**A deployment rule, now that it has happened twice.** This change adds
`services/display_labels.py`, a new module imported by four pages, so it
needs a **reboot from Manage app after deploying**. The same
stale-`sys.modules` condition took the Schedule Templates page down on
2026-08-19: Streamlit re-executes page scripts on every rerun but keeps
imported modules for the life of the process, so a page calling into a
module the running process never imported fails until restarted.

The rule to apply without thinking about it: **if a change adds a
service module, or makes a page import one it did not import before,
reboot after deploying.** Both incidents fit it, and neither was
detectable from the diff alone — the code was correct in both cases.

## 2026-08-21: Control Room / Flt Schedule restructure — act vs. record.
## MERGED into `main` 2026-08-21.

Control Room and Flight Log both carried an identical seven-field
add-flight form, so "where do I add a flight?" had two answers and
neither page had a clear job. The split now follows what a controller is
doing:

* **Control Room is where you ACT** — see the operation, create work.
* **Flt Schedule is the RECORD** — what happened, searchable, and where
  actuals are entered.

**Control Room is three sections.** Operational status; add a flight;
crew it, optionally. Flight creation moved here in full, gaining
"Flight No (optional)" (which Flight Log's form had and this one didn't)
and HHMM UTC times.

**Home reverts to the DB status line and navigation.** The ops banner
moved to Control Room and was extended with today's flights. Home is the
page every session starts on and should stay the cheapest in the app;
`test_home_makes_no_service_queries_at_all` pins that by patching both
services to raise.

### The column this section was nearly built on

Section 1's flight board shows whether each cockpit seat is filled. The
obvious source is `role_assigned` — and it is **wrong**. Under the pair
model a Captain can legitimately occupy the SECOND_PILOT seat, so
reading coverage off the grade reports the Commander seat filled twice
and the Second Pilot seat empty. `operating_position` is the seat.

That grade-versus-position conflation is exactly what the flight-deck
crew package existed to remove, so building a status board on it would
have reintroduced the defect in the one place a controller looks to
decide whether a flight is crewed.

`search_roster()` did not expose `operating_position`; it now does. The
change is additive — no consumer selects columns positionally — and it
is what lets the board read a whole day's seats in ONE query instead of
a `get_roster_for_flight()` per flight, on a page that has to render
under pressure. `test_status_board_reports_seats_from_operating_position`
pins it with a deliberately CPT/CPT pair, which is the case that would
pass if the wrong column were used and the seats happened to line up.

### An ordering trap in the section layout

Section 3 (crew) renders BELOW section 2 (the form) for the operator,
but the submit handler needs `commander_id` — which section 3 defines.
The naive layout puts the handler inside the form, where it runs before
section 3 has executed: a `NameError` on the first click, from code that
reads as correct.

The form now declares `submitted` only, and the handler sits after
section 3. Both ordering rules — crew controls outside the form, handler
after section 3 — are in the page's module docstring, because each looks
like a mistake until you hit the failure it prevents.

### `services/time_entry.py`

`_parse_hhmm`/`_format_hhmm` were page-local in Schedule Templates. Two
consumers is where a page-local helper stops being page-local: a second
copy would be a second place for the accepted formats and the error
wording to drift. Moved to `services/time_entry.py` as `parse_hhmm` /
`format_hhmm`, following the precedent `display_labels.py` set.

Its blank-is-not-an-error contract matters in both callers and for
opposite reasons: Schedule Templates needs blank to distinguish an
untouched leg row from a filled one, and Flt Schedule needs blank to
mean "this hasn't happened yet" rather than midnight. Callers that
REQUIRE a time check for `None` themselves — Control Room does.

### The rename is display-only

"Flight Log" → **"Flt Schedule"** (operator's wording). The sidebar label
comes from `app.py`'s `st.Page(title=...)`, so no file rename was
needed — renaming `pages/3_Flight_Log.py` would have touched every
`AppTest.from_file()` in the tests and `check_reachability.py`'s
entry-point handling, for a label. The `flights` table and
`flight_service` are untouched.

Stale references were checked. `pages/4_Roster.py` said *"No flights in
Flight Log yet — add one there first"*, which after this change was both
mislabelled AND factually wrong — flights are created in Control Room
now. It points there instead.

### Tests moved, not deleted

Flight Log's two add-flight tests moved to
`tests/test_control_room_page.py` — same coverage, different page — and
`test_flight_log_no_longer_offers_flight_creation` pins the absence, the
way `test_single_crew_path_is_gone` does. A duplicate form is exactly
the kind of thing that gets helpfully re-added.

The three DB-backed pair tests on Control Room filled the form by
POSITION (`text_input[0]`, `time_input[0]`). Both are now wrong — the
form gained a field and the times became text — so they were switched to
label-based filling. They skip locally, so this would otherwise have
surfaced as three failures on the next real-Postgres round.

Section 1 also broke three existing DB-free tests, because the page now
calls `test_connection()` at module level and those fixtures did not
fake it — so they reached for a real database. Their fixtures now report
the DB down, which skips the board and keeps those tests on sections 2
and 3 where they belong.

### 2026-08-21 (continued): positional widget access, and the rule that stops it

The restructure's real-Postgres round found two more failures, both the
same shape as two I had already caught and fixed in the same change:

* `test_roster_page.py` pinned the OLD wording of the Roster info
  message ("No flights in Flight Log"). The message was correctly
  changed — it was mislabelled and factually wrong — but the assertion
  still held the old string. Now asserts on "create one in Control
  Room", the part that carries the meaning.
* `test_flight_log_page.py`'s cancel test filled `at.text_input[4]` and
  clicked `at.button[2]`. Removing the add-flight section shifted every
  index on the page. Switched to label-based.

**Four instances, one cause: a test addressing a widget by POSITION in a
list whose order is a property of the page layout.** Indices describe a
layout; labels describe intent. When the layout moves, positional tests
break in files nobody opened — which is exactly why two of the four
survived into a verification round.

**The rule, alongside the reboot rule:**

> When a change alters a page's widget layout — adding, removing or
> reordering any input — grep that page's test file for
> `at.text_input[`, `at.button[`, `at.selectbox[`, `at.date_input[`,
> `at.time_input[` before pushing.

It is a two-second grep and it would have caught all four.

**Converted in this change:** `test_control_room_page.py` and
`test_flight_log_page.py` are now entirely label-based, via
`_by_label` / `_select_pair` / `_set_flight_dates` / `_fill_flight_form`
/ `_click_save`. Those are the two pages that just moved and are most
likely to move again.

**Deliberately NOT converted, with reasoning** — 49 sites across four
files, all on pages untouched by this restructure and all passing:

| file | sites | why left |
|---|---|---|
| `test_assistant_page.py` | 24 | page unchanged; a mechanical rewrite of 24 DB-gated assertions I cannot run locally risks breaking passing tests for no current benefit |
| `test_roster_page.py` | 12 | the page has TWO parallel assignment forms with similarly-labelled widgets, so index order is partly load-bearing — one test documents why index 0 is correct. Converting needs keys added to the page first |
| `test_auth_service.py` | 6 | drives a fixed two-widget inline `AppTest.from_string` script; indices are safe by construction there |
| `test_crew_data_page.py` | 5 | page unchanged this round |
| `test_roster_generation_page.py` | 2 | page unchanged |

The honest position: converting all of them is the right end state, but
blind-converting DB-gated tests I cannot execute is how a third round
gets spent. Convert each file when its page is next touched — at which
point the rule above requires opening it anyway.

**Follow-up (same day): the label conversion introduced its own bug.**
The IndexError was gone, but the cancel test then failed on its
assertion. The new `_click()` helper runs the script and RETURNS the
new state; the call site discarded it and called `at.run()` separately,
which reruns the stale object and loses the click. The cancel never took
effect.

An audit found 32 call sites across the suite capturing the return
correctly and 2 discarding it — the reported one, and a second in
`test_control_room_page.py` that passed only because it asserted on the
service rather than on `at`, so the trap was latent there rather than
absent.

Convention kept as-is (helper runs, caller captures `at = _click(...)`)
because 32 sites already follow it; switching would have created a
second convention, which is worse than the one that exists. What
changed is that the contract is now stated in each helper's docstring
with the wrong form shown explicitly, because a helper that runs the
script INVISIBLY is easy to misuse and being right by accident at 32
sites is not the same as the contract being clear.

### 2026-08-21 (continued): green, and merged

**626 passed, 0 failed against real Postgres 16.** Reachability clean.
Everything that could not run locally was checked: the moved add-flight
tests on Control Room, the status board reading seats from
`operating_position` (including the CPT/CPT pairing that is the only
one where reading `role_assigned` could accidentally look right), both
DB-failure paths, and `search_roster()` exposing the new column.

**Three real-Postgres rounds, all test-side, all one theme.** Nothing in
the pages was wrong on any round:

1. Two stale test assumptions — a pinned message string, and positional
   widget access broken by removing a page section.
2. The label conversion that fixed round 1 introduced a hidden-side-
   effect bug: `_click()` runs the script and returns the new state,
   and two call sites discarded it.
3. Green.

Worth keeping from round 2: one of the two discarding call sites passed
anyway, because it asserted on the SERVICE rather than on `at`. A latent
trap, not an absent one — it would have surfaced later, for someone
else, looking unrelated to the change that introduced it. Auditing every
call site rather than fixing the reported one is what found it.

Merged into `main`, pushed; branch `control-room-restructure` deleted,
remote and local. See the "Merge status as of this snapshot" paragraph
near the top of this file.

## 2026-08-21 (continued): aircraft registration AP-BNW

Operator supplied the registration, so the placeholder default is now
real. Two things came out of it that were not just flipping a constant.

**Flt Schedule had no aircraft field at all.** The question was whether
its edit form should default the registration too — but there was
nothing to default: `aircraft` is in
`flight_service.UPDATABLE_FIELDS` and Control Room sets it at
creation, and no page exposed it afterwards. A flight recorded without
one could never be corrected. Small version of the qualification-renewal
gap, same fix: expose the field.

The default fills an EMPTY value only —
`selected["aircraft"] or AIRCRAFT_DEFAULT` keeps whatever is stored
and never overwrites it. A flight against a leased or substituted
airframe keeps its own registration, and the pre-filled value is visible
in the field before saving, so a controller sees and clears it rather
than having it applied behind them.
`test_aircraft_never_overwrites_a_value_already_set` pins that.

**The constant lives in `flight_service`, not a new module.** Two
consumers would normally argue for its own home (the
`display_labels` / `time_entry` precedent), but a new service module
obliges a Manage-app reboot on deploy under the stale-`sys.modules`
rule — three occurrences so far — and that is a poor trade for one
constant. `flight_service` owns the `flights` table and `aircraft`
in `UPDATABLE_FIELDS`, and both pages already import it, so this adds
no new import edge and **needs no reboot**.

**Why one default is correct, and what changes it.** Air Eagle operates
one B737. This is an AIRLINE-CONFIGURATION value, not a platform
assumption — nothing in `core/` or the legality engine reads it.
**The trigger is a second aircraft**: at that point a default is the
wrong shape entirely and becomes a selector over a fleet, because a
silent default would start attributing flights to the wrong airframe,
which is worse than an empty field. Do not extend it to a "primary
aircraft" default; make it a choice. The Control Room test asserts
against the constant rather than the literal, so the page and the test
break together when that day comes.

## 2026-08-21 (continued): the UPDATABLE_FIELDS sweep, and an unwatched directory

Two follow-ups from the aircraft work, both cheap, both worth having
done deliberately rather than by accident.

### Sweep: can a page actually set every field a service will write?

Twice a column had been in `UPDATABLE_FIELDS` with no UI path —
qualification expiries (renewing a medical needed raw SQL) and
`flights.aircraft` (a flight recorded without one could never be
corrected). Both were found by accident, so the whole set was checked.

A first pass matched field names anywhere in `pages/` and reported only
one gap; that was too generous, since a template's `meal_provided`
falsely cleared `flight_service`'s. The tightened pass attributes a dict
literal to a service only when its keys are a subset of that service's
own `REQUIRED ∪ UPDATABLE` fields. Results, after verifying each flagged
field by hand:

**`crew_service` — 18/18 reachable.** Clean.

**`flight_service` — 17 fields:**

* 11 reachable through Control Room / Flt Schedule payload dicts.
* `dep_time_actual` / `arr_time_actual` — reachable, flagged only
  because they are passed as KEYWORD ARGS to
  `update_flight_actual_times_and_revalidate()` rather than in a dict.
  A limitation of the sweep, not a gap.
* `rotation_instance_id` — correctly unreachable. It is provenance, set
  only by `approve_instance()` at promotion time; a controller setting
  it by hand would be falsifying which rotation produced a flight.
* `meal_provided` / `snack_provided` — no UI on FLIGHTS. Set from the
  template at promotion; an ad-hoc Control Room flight takes the column
  DEFAULT TRUE. Arguable rather than clearly wrong (the operator has
  confirmed a meal is universal), but an ad-hoc flight where a meal was
  NOT provided currently cannot be recorded as such.
* **`status` — a real gap.** The only write is `cancel_flight()` setting
  `'CANCELLED'`. Recording actual times writes the two timestamps and
  nothing else. So a flight that has flown stays `PLANNED` forever, and
  `DISRUPTED` is unreachable entirely — while the Flt Schedule filter
  offers `PLANNED / OPERATED / CANCELLED / DISRUPTED` as if they were
  all real states, and that section's own subheader says "Record
  actuals, **update status**, or cancel a flight".

**Not fixed here, deliberately.** When a flight becomes `OPERATED` is an
operator policy question — automatically once actual times are recorded,
or an explicit control — and `DISRUPTED` needs a definition before it
needs a widget. Flagged for a decision rather than guessed at. The
subheader is currently promising something the page does not do, which
is the part worth fixing first whichever way the decision goes.

**The pattern is worth naming**, since this is the third instance: a
column being writable by a service says nothing about whether any human
can set it. The service layer and the UI were built at different times,
and `UPDATABLE_FIELDS` is the service's statement of intent, not
evidence of a path. Re-run the sweep after adding columns.

### An unwatched directory is a blind spot

`configs/` was not in `check_reachability.py`'s `WATCHED_DIRS`, so
anything placed there would never be flagged as orphaned — the precise
failure that script exists to prevent, in a directory the script could
not see.

It is NOT dead space, which is why it was watched rather than removed:
`README.md` lists it in the project structure, and
`services/reporting.py` names a planned `configs/airlines/AEAGLE/`
layout for multi-tenant airline configuration. Removing it would have
contradicted both; the blind spot was the problem, not the directory.

`WATCHED_DIRS` now includes it. A no-op today (only `__init__.py`, which
the scan skips), verified active by planting an orphan there and
watching it get flagged. The success message is now built FROM
`WATCHED_DIRS` rather than hardcoded — it had already drifted, still
naming three directories after a fourth was added, which is the same
class of stale-statement problem in the guard's own output.

## 2026-08-21 (continued): flight status transitions — OPERATED automatic,
## DISRUPTED manual. MERGED into `main` 2026-08-21.

Closes the gap the UPDATABLE_FIELDS sweep found. `flights.status` could
only ever become `CANCELLED`: `cancel_flight()` was its sole writer, and
recording actual times wrote the two timestamps and nothing else.

**The operational consequence, which explains why this mattered more
than it read:** a flight that had flown stayed `PLANNED` forever and
`DISRUPTED` was unreachable entirely, while the Flt Schedule filter
offered all four states as if they were real. Every report keyed on
status returned nothing for `OPERATED`, and the shadow trial's
end-of-period reconciliation — "which flights actually flew" — had no
answer at all. That section's own subheader had been promising "update
status" for a control the page did not have.

### status is NOT the answer to "did it fly" — read this before writing a report

The most important thing in this entry, and it corrects the framing the
work started from.

Status is ONE column, so `OPERATED` and `DISRUPTED` are mutually
exclusive. A flight marked disrupted keeps that label after it flies —
deliberately, because "it flew" is recoverable from the actual times and
"it was disrupted" is recoverable from nothing else. **So some flown
flights will always carry a label other than OPERATED, under any rule
set.** No transition design fixes this; it is a property of storing one
value.

    -- "which flights actually flew" — the honest test
    WHERE dep_time_actual IS NOT NULL AND arr_time_actual IS NOT NULL

    -- NOT this: under-counts every disrupted flight that still flew
    WHERE status = 'OPERATED'

The status rule earns its place for a **different job**: making the
filter meaningful and the record readable. Two jobs, and they were
conflated when this work was scoped. Someone will eventually write a
report keyed on `status = 'OPERATED'` believing it means "flew" — the
note is repeated in `migrations/020`, in `_apply_operated_rule()`'s
docstring, and in the shadow-trial guide's Stage 2 reconciliation
exercise, so it is findable from wherever that person is standing.

### The rule

`services/flight_service._apply_operated_rule()`, called from
`update_flight()` — the single generic UPDATE on `flights` and the only
writer of the actual-time columns.

It has to live there rather than in a page for a reason beyond layering:
**the updates dict alone cannot answer the question.** Departure actual
is commonly recorded on one shift and arrival on the next, so any given
call sees only one column. `update_flight()` already loads the stored
row, so it can merge. A page-level rule would look correct and fail
silently in exactly the normal case.

An INVARIANT, not a default:

1. **CANCELLED is terminal** — actuals never revive a cancelled flight,
   and an explicit status change away from CANCELLED is refused.
2. **Explicit status wins, except `PLANNED` on a flown flight, which
   raises.** Without that exception the rule would be optional: a caller
   could assert PLANNED over two recorded actuals and have it stick.
3. **The automatic transition fires only from PLANNED**, so a manual
   DISRUPTED survives.

### Transitions

| from | to | trigger |
|---|---|---|
| PLANNED | OPERATED | automatic — both actuals present |
| PLANNED | DISRUPTED | manual, reason required |
| DISRUPTED | PLANNED | manual, when actuals incomplete |
| DISRUPTED | OPERATED | manual, when both actuals present |
| PLANNED / DISRUPTED | CANCELLED | existing cancel control |
| OPERATED, CANCELLED | — | terminal |

**The un-disrupt control names its outcome rather than warning about
it.** Clearing the label on a flight with both actual times yields
OPERATED, so the button says `Clear DISRUPTED → OPERATED`. Offering
"PLANNED" there and letting the automatic rule move it afterwards would
be a control that says one thing and does another — the edge case that
surfaced while planning, where an undo produced a status the controller
never chose.

**Both directions are audited with a required reason.**
`FLIGHT_DISRUPTED` populates `linked_disruption_event` — the column
already designed for this, rather than adding one — and
`FLIGHT_DISRUPTION_CLEARED` records the undo. An unaudited undo would
leave a record showing a flight that was never disrupted, when it was
labelled and then relabelled, which is precisely what an auditor asks
about.

### migrations/020 — apply BEFORE deploying, and this is NOT the reboot rule

`020_backfill_operated_status.sql` sets `OPERATED` on rows that
demonstrably flew, scoped to `status = 'PLANNED'` so CANCELLED and
DISRUPTED are untouched, and idempotent.

**It must be applied before the deploy that ships the rule.** A separate
requirement from the reboot rule and not a variant of it: if the code
lands first, freshly-recorded actuals start setting OPERATED while older
rows with identical data still read PLANNED, and the record becomes
inconsistent in a way that looks like the bug rather than the fix. This
change adds no new module, so **no reboot is needed** — the two
requirements are independent and only one applies here.

Backfilling rather than leaving history was a decision: `PLANNED` on
those rows was never a judgement anyone made, only the absence of any
way to record one.

### 2026-08-21 (continued): verified green, merged

**647 passed, 0 failed against real Postgres 16.** Reachability clean.
Every transition was run end to end, including the split-shift case
where departure and arrival actuals arrive on separate calls — the one a
page-level rule would have got wrong, and the reason the merge lives in
`update_flight()`. Both audit action types confirmed written, in both
directions.

Two refinements from review that improved the design rather than just
the code:

* The un-disrupt control **names its outcome** (`Clear DISRUPTED →
  OPERATED`) instead of warning about an unexpected destination. A
  control that says what it does removes the surprise rather than
  announcing it.
* The automatic transition is an **invariant, not a default**.
  `test_explicit_planned_on_a_flown_flight_is_refused` is what makes it
  a rule: without it, any caller could assert `PLANNED` over two
  recorded actuals and have it stick.

Merged into `main`, pushed; branch `flight-status-transitions`
deleted, remote and local. See the "Merge status as of this snapshot"
paragraph near the top of this file for the two deployment requirements,
which are independent of each other.

## 2026-08-22: roster generation was unusable in production — 4,822 round-trips to fill 10 seats

The system's core function took **7+ minutes** on the deployed app for 7
rotations, against a 23-second estimate. Not stuck, not slow queries,
not the expired documents: **4,822 database round-trips to process 10
seats**, roughly 480 per seat.

Locally that is 7.2 seconds, because a round-trip to a local database
costs microseconds. Against Supabase from Streamlit Cloud each carries
50–300ms of network latency, which is where the minutes came from. **The
only environment where it hurts is the deployed one.**

### Why 647 passing tests did not catch it

Nothing in the suite measured round-trip COUNT — and count is the only
environment-independent measure of this defect. A timing assertion
cannot see it, because locally there is nothing to see. Every test
passed throughout, on every round, while the core function was unusable.

### Where they came from

Four multipliers, stacked:

1. **`_age_of()` queried per candidate.** A database round-trip to read
   a birthday, for every candidate, on every seat — data already loaded
   in `all_crew`.
2. **The second-pilot candidate list was rebuilt inside the commander
   loop.** Its CONTENTS never vary with the commander — only the
   ordering and one exclusion — so this cost `C × S` age queries per
   rotation to re-sort a list that never changed.
3. **Every trial re-fetched crew rows and flights.** `_validate_new_duty()`
   re-fetched crew rows `_validate_pair_internal()` already held, and
   the same two flights were re-read for every candidate pair.
4. **Duty history loaded per trial.** `start`/`end` derive from
   `build_duty(legs)` and the legs are the rotation's own — identical
   for every candidate — so the same query repeated `C × S` times.

For Air Eagle's real pool (6 commanders, 10 second-pilot-eligible) that
is ~672 round-trips per uncrewed rotation.

### Result

Measured with the counter below, at Air Eagle's real pool shape, 5
rotations:

| | round-trips | @50ms | @150ms | @300ms |
|---|---|---|---|---|
| before | 4,822 | 4.0 min | 12.1 min | 24.1 min |
| after | **79** | 4.0 s | 11.8 s | **23.7 s** |

**61× fewer round-trips**, and growth is now linear in pool size rather
than quadratic: the duty-history query count is exactly `C + S`.

Note the last cell. The ~23s estimate was never wrong — it was measured
locally and is correct there. The round-trip count was the defect, and
at worst-case latency the fixed generator now lands almost exactly on
the original prediction.

### The fix that was NOT needed

A qualification pre-filter — skipping candidates with expired documents
before spending ~10 round-trips discovering it — was planned and is
**deliberately not implemented**. The structural fixes alone solved the
problem, so the pre-filter would now be a correctness risk (moving a
qualification check outside the gate) bought for no remaining need.

Its benefit was also always **data-dependent**: with 7 of Air Eagle's 10
pilots currently carrying expired documents it would collapse C=6,S=10
to C'=2,S'=3, but once documents are renewed it saves nothing. Anyone
reading a "15 seconds" figure later needs to know it would have been
measured against a degraded crew state. The structural fixes help
unconditionally; that is why they came first, and why they were enough.

### `Prefetch` — passing rows in without weakening the gate

`assignment_service.Prefetch` carries crew rows, flight rows and cached
duty-history rows. Passing rows in DOES weaken the "always current"
guarantee a direct fetch gave, so four things bound it:

* **Opt-in.** Every lookup falls back to a live fetch, and every
  existing caller passes nothing — Control Room, Roster and Flt Schedule
  are byte-for-byte unchanged. Only the generator supplies one.
* **Lifetime is one `generate_for_window()` call.** Not module-level,
  not memoised across runs.
* **A shared snapshot is required for correctness, not merely tolerated
  for speed.** Any pre-filter and the gate must judge a candidate on the
  same data, and they would not if one read a snapshot while the other
  re-fetched.
* **PROPOSED is not authoritative.** `publish_window()` re-validates
  every pair against FRESH data before anything becomes PLANNED, so a
  crew edit landing mid-run cannot reach a published roster.

`validate_pair()` deliberately does NOT take a prefetch — it is the
fresh-data path.

**The duty-history cache stores the DATAFRAME, not the built records.**
That distinction is load-bearing: `Duty` is a plain mutable dataclass,
so handing the same objects to every trial would share mutable state
through the legality engine. Rows are inert; records and their `Duty`
objects are rebuilt fresh on every call, exactly as before.

### The guard: `tests/test_generation_round_trips.py`

Counts round-trips with NO DATABASE. The leaf functions that each issue
one query are replaced with counting fakes and the real orchestration
runs on top. The fixture makes every candidate fail the qualification
gate, so the search performs the full `C × S` scan — the case that
matters — and never reaches a write, which is what lets it run in the
environments where this defect was invisible.

Three assertions, and the second is the important one:

* ages cost no query (`get_all_crew` is the one crew read per run)
* **growth: 3×3 vs 6×6 pools, asserting ≤2×.** Measured 15 → 21 (1.4×)
  with the fix and 129 → 537 (4.16×) without. A single-point budget
  catches "it got worse"; only a growth assertion catches a reintroduced
  `C × S` loop, because a small fixture keeps the absolute number low.
  **That is the defect class that actually broke.**
* an absolute ceiling against a pinned pool

Mutation-tested: disabling the duty-row cache fails both the growth test
and the budget test.

**`_read_duty_rows()` is a one-line seam** so the test can count queries
while the REAL caching still runs. Patching `_fetch_duty_rows` instead
would have replaced the cache with the test's own copy of it — the exact
drift this codebase has repeatedly paid for.

### Still deferred: `fail_fast`

Generation discards all alerts and reads only `.status`, so constructing
thousands of `RuleAlert` objects per candidate is waste — but it is CPU
waste, not round-trips, and was deliberately kept out of this change so
the measurement stayed clean. It is now the dominant LOCAL cost (the
6×6 fixture takes ~47s of pure CPU) and is irrelevant to production
latency. Worth doing on its own terms, measured on its own terms.

### 2026-08-22 (continued): two defects in the fix itself

**1. A NameError shipped in the ad-hoc pair path.**
`assign_pair_to_new_flights()` referenced `prefetch`, which it never
received — a global edit matched two functions with identical
crew-fetch shapes. Control Room could not assign crew at all and showed
a raw exception.

Exactly the risk flagged when fix 4 was planned: replacing a fetch with
a parameter means the function stops being self-sufficient, and one
caller was not threaded. Reverted to direct fetches rather than adding a
parameter — that path CREATES flights, so it has nothing to prefetch and
no caller that benefits.

The audit that should have run at the time now has: an AST check that
every function using `prefetch` has it in scope, and a bind-check of all
**71 call sites** of every signature changed in this branch. Both clean.

**2. The round-trip guards had never executed anywhere.** They reached
`get_engine()`, which raises when `DATABASE_URL` is unset — so they
failed on the verification machine and only ran locally because this
machine's `.env` happens to point somewhere. They never actually
queried (engines are lazy and every query function is patched), but
depending on the variable at all defeated the point.

**Deliberately NOT switched to `TEST_DATABASE_URL` and the standard
page fixture.** That would make them SKIP wherever Postgres is absent —
precisely the environment this defect was invisible in, and the whole
reason they exist. They now patch `get_engine` with a sentinel that
RAISES if anything tries to use it, so an unpatched query path fails
loudly instead of quietly opening a connection. Verified passing with
both `DATABASE_URL` and `TEST_DATABASE_URL` unset.

**Third occurrence of the same shape** (after the auth harness and the
Streamlit version drift): a test written to catch what local testing
cannot see, itself unable to run. The lesson that keeps recurring is
narrower than "test the tests" — it is that a guard which needs the
environment it is guarding against is not a guard. Run new DB-free
tests with the environment variables explicitly unset before believing
them.

**Also added:** a DB-free smoke test for the ad-hoc pair path, which had
none — every test of it was DB-gated, which is why an 11-test failure
appeared only on Postgres. Expired-document crew means validation
rejects before any write, so the whole read path runs without a
database. Mutation-tested: it catches the shipped NameError in 1.4
seconds.

### 2026-08-26: INCIDENT — test runs wrote 2,954 rows into the production audit trail

**What happened.** The round-trip guards patched `audit_service.log_audit`
but not `assignment_service.log_audit`. Those are DIFFERENT OBJECTS:
`from services.audit_service import log_audit` binds a COPY into the
importing module, so patching the source module does nothing to the
copy. The same is true of `from db.db import get_engine`.

Generation writes a `PAIR_ASSIGNMENT_REJECTED` audit row for every
rejected candidate pair. With the real `log_audit` still live, each of
those became a genuine INSERT. On the verification machine, which has no
`.env`, that surfaced as `RuntimeError: DATABASE_URL not set`. On the
development machine, whose `.env` points at the production Supabase
pooler, it silently wrote.

**Damage:** 2,954 `PAIR_ASSIGNMENT_REJECTED` rows, `app_user=occ1`,
`affected_crew` CPT-01..CPT-06, timestamps 2026-08-26 05:08–11:11. The
table held 165 rows before that day, so roughly 94% of `audit_log` is
now test noise. **Nothing was deleted** — audit is append-only by design
in this project, and removing rows is the operator's call, not a cleanup
to perform quietly. The rows are exactly identifiable by that
action_type + date + app_user combination.

**Why `env -u` did not catch it.** `db/db.py` calls `load_dotenv()`,
which reads `.env` off disk. Clearing the process environment does not
stop that. The practice recorded on 2026-08-22 — "run new DB-free tests
with the variables unset" — was therefore insufficient, and the stronger
form is: **prove isolation by making every `get_engine` binding raise,
then confirm the tests still pass.** That is the check that finally
found this, and it is what the guards are now verified against.

**The fix.** `isolate_from_database()` in the test module enumerates
every service module and replaces `get_engine` and `log_audit` wherever
the attribute exists, so a new service joins the net automatically
rather than being missed by name. The engine sentinel raises on ANY
attribute access, so an unpatched path fails loudly instead of opening a
connection. Verified with every binding forced to raise: 4 passed in
2.0s, down from 75s — the difference was real network round-trips.

### The measurement this corrects, and the quadratic that remains

Because those audit writes were escaping uncounted, the figures reported
on 2026-08-22 were **reads only**. Corrected, with audit writes counted:

| pool | reads | audit writes | total |
|---|---|---|---|
| C=3, S=6 | 15 | 15 | 30 |
| C=6, S=12 | 21 | 66 | 87 |
| C=6, S=10 (Air Eagle) | 19 | 54 | 73 |

**Reads are linear and fixed.** `audit_write` is exactly C x S — one
INSERT per rejected candidate pair — so **the quadratic term is not
fully gone, it moved**. For Air Eagle that is ~73 round-trips per
uncrewed rotation rather than the ~16 previously claimed; at 150ms and
5 rotations, roughly 55 seconds rather than ~12. Still far better than
12 minutes, and worth stating accurately rather than leaving the
optimistic number standing.

**OPEN QUESTION FOR THE OPERATOR — not a refactor to make
unilaterally.** Should a speculative candidate trial that was rejected
leave a permanent audit row at all? `uncovered_seats.reason` already
records why a seat could not be filled. One generation run currently
adds hundreds of rows to a table that held 165 in total — which is what
the incident above demonstrated at scale. Options: leave as-is
(regulatory completeness), batch the writes per rotation, or stop
auditing generator-internal rejections while keeping audit on real
assignment decisions. Touching an audit trail is the operator's call.

Tracked as `test_audit_writes_do_not_grow_quadratically`, marked
`xfail(strict=True)` — strict, so it FAILS if someone fixes the cause
without removing the marker, and cannot rot into a permanently ignored
test.

## 2026-08-26: the audit trail records decisions, not options considered

**Operator decision, made by Arif, not by whoever wrote this code.** The
roster generator no longer writes an audit row for each speculative
candidate pair its internal search tries and discards. Everything a
person actually decides is still fully audited: assignments created,
unassignments, publishes, approvals, manual rejections, crew changes.

The evidence was concrete rather than theoretical. Of 3,157 rows in the
production `audit_log`, 2,954 were `PAIR_ASSIGNMENT_REJECTED` written by
a morning of test runs (see the incident entry above). Arif deleted them
and **no information anyone would ask for was lost**:
`ROSTER_GENERATION_SEAT_UNCOVERED` (26 rows) already recorded which
seats went unfilled and why, and survived independently. What remains is
195 rows that each record something that happened. A regulator asks "why
was this crew legal" and "why was this flight uncovered" — both still
answerable. Nobody asks which combinations the algorithm evaluated.

### How the generator is told apart from the pages

`assign_pair_to_duty()` and `assign_crew_to_duty()` are called by both,
so the distinction is an explicit `audit_trials` parameter rather than
anything inferred from context. The obvious hazard is that a flag a page
could set would let a REAL rejection — a decision by a real controller —
go unrecorded. Four things bound it, and they are checks rather than
intentions:

* **The default is `True`, and the direction is the point.** Silence is
  never what a caller gets by saying nothing. A page written next year
  is fully audited without its author knowing the parameter exists; only
  an explicit opt-out turns anything off.
* **It gates ONLY trial outcomes.** `ASSIGNMENT_CREATED` is written
  unconditionally, outside any branch the flag can reach, so an
  assignment that IS created cannot go unaudited whatever any caller
  passes. `tests/test_audit_scope.py` proves this by walking the AST
  ancestor chain rather than grepping, so it survives a reindent.
* **Only the generator may pass it, and only as the literal `False`.**
  Statically enforced over every non-test `.py` file, so it holds for
  pages that do not exist yet and cannot be satisfied by a
  copy-pasted `audit_trials=x` that later becomes a variable.
  `ADHOC_PAIR_REJECTED` and the manual paths are untouched.
* **Both speculative loops are pinned at two call sites.** If that count
  drops to one, a loop has started writing a row per discarded option
  again.

All five guards were mutation-tested: a page passing the flag, the
generator passing a variable, the default flipped to `False`,
`ASSIGNMENT_CREATED` moved under the flag, and trial auditing restored
wholesale — each fails the suite. (The first mutation attempt did NOT
fail it, and the guard was right: the text edited was a mention of
`assign_pair_to_duty(` inside the page's module docstring, not a call.
Worth knowing before concluding a guard is asleep.)

### Re-measured, not inferred

Predicting this from arithmetic is what produced the wrong number last
time, so the whole table is measured with the counter, both columns
from the same fixtures:

| fixture | before: reads / writes / **total** | after: reads / writes / **total** |
|---|---|---|
| C=3, S=3, 1 rotation | 15 / 15 / **30** | 15 / 0 / **15** |
| C=6, S=6, 1 rotation | 21 / 66 / **87** | 21 / 0 / **21** |
| C=6, S=10, 1 rotation | 25 / 90 / **115** | 25 / 0 / **25** |
| C=6, S=10, 5 rotations | 109 / 450 / **559** | 109 / 0 / **109** |

Writes are **zero**, not merely linear. Reads are untouched — this
change was never about them. Against the operator's independently
measured 133 for 5 rotations at the real pool, and 4,822 originally.

Note the write column before: 90 for C=6/S=10, not the 54 previously
claimed. Commanders are eligible for the second-pilot seat too, so the
second-pilot pool is 16 and each commander trials 15 partners after
self-exclusion — 6 x 15. **That is the third time an inferred figure in
this file has been wrong and the measured one right.**

### The guards

`test_generator_trials_write_no_audit_rows` asserts **zero**, not a
growth ratio: a ratio would still pass if one row per rotation crept
back, and the point is that a discarded trial writes nothing at all. The
growth and budget guards now count reads and writes TOGETHER — the
carve-out that existed while writes were a tracked open question is
gone, which makes them both simpler and stricter.

`test_the_generator_still_audits_what_it_actually_decides` runs a pair
that PASSES validation and asserts `ASSIGNMENT_CREATED` is written even
with `audit_trials=False`. Without it, "stopped auditing generator
rejections" and "stopped auditing the generator" look identical to the
suite.

**`uncovered_seats.reason` is now the only record of why a seat could
not be filled**, so it carries more weight than when the rejection rows
sat beside it.
`test_uncovered_reason_is_identical_with_and_without_trial_auditing`
runs the same generation twice — flag on, flag off — and asserts the
text handed to `_record_uncovered()` is equal across both. Asserting
that the reason "looks right" would pass while quietly dropping a
clause; asserting the two runs are equal cannot.

## 2026-08-28: seat versus grade, the third and fourth instances

`operating_position` is the SEAT (COMMANDER / SECOND_PILOT).
`role_assigned` is the GRADE (CPT / FO). Under the flight-deck pair
model a CPT may legitimately occupy the Second Pilot seat, so the two
are not interchangeable — and this is now the same defect **four
times**: the Control Room status board (fixed 2026-08-21),
`reports.roster_coverage()` and
`roster_generator_service._seed_duty_counts()` (both fixed here).

That is why the new guards live in **one file named after the
distinction**, `tests/test_seat_vs_grade.py`, rather than being
scattered through three modules' own suites. A reviewer looking for
"has this been checked" now has one place to look. It is **DB-free on
purpose**: the equivalent checks in `test_assistant_reports.py` are
DB-gated and skip wherever Postgres is absent, which is exactly where
the first two instances survived review.

### `roster_coverage()` reported a crewed flight as half-empty

Seen in production on 2026-08-31:

```
EPE 787  LHE-KHI   Commander: CPT-03, CPT-04   Second Pilot: UNCOVERED
```

Two crew under Commander and none under Second Pilot — a state the
database itself forbids. `uq_roster_flight_operating_position_active`
(migrations/016) is unique on `(flight_id, operating_position)` for
non-cancelled rows, so two active COMMANDER rows on one flight cannot
exist. **The report was describing something impossible**, which is the
tell: the assignment was correct and only the report was wrong.
Confirmed against the actual rows — CPT-04 COMMANDER, CPT-03
SECOND_PILOT on flights 15/16.

Fixed to group by `operating_position`. The headers were renamed
`CPT`/`FO` -> `Commander`/`Second Pilot`, which is **half the fix, not
cosmetics**: columns headed by grade invite exactly the grouping that
was wrong, and a reader cannot otherwise tell a seat report from a
grade report by looking at it.

### The third state, and the production count

A cockpit row with no `operating_position` belongs to neither seat and
would have silently vanished from a seat-grouped report — **worse than
the bug being fixed**, because a coverage report that drops a crew
member is actively misleading rather than merely wrong.

Such crew are now **named in a note** against their flight, not pushed
into a seat cell: putting them in one column asserts a seat the data
does not record, and putting them in both asserts two. They still count
toward POB — they are aboard regardless of what was recorded — and they
do NOT mark their seat as covered.

**Production count: ZERO** (checked 2026-08-28, read-only). No cockpit
row in any status has a NULL `operating_position`, so this is purely
defensive against pre-016 rows. Not a data finding — recorded because
"we checked and it was zero" is worth more later than silence.

### `_seed_duty_counts()` — a filter that was a no-op

It filtered `role_assigned.isin(grades)` while its own docstring claimed
to count "every duty a pilot flies **in that seat**". Since every row
for a CPT reads `role_assigned='CPT'` whichever seat they sat in, and
the Commander pool is exactly the CPTs, **the filter did nothing in
either direction**: a CPT's Second Pilot duties counted toward their
Commander total and vice versa. It measured total workload.

**THIS CHANGES GENERATED ROSTERS, and it is a decision about what fair
means rather than a bug fix.** Operator decision (2026-08-28): the
ordering chooses who is OFFERED a particular seat, and command is the
position carrying the responsibility, so the opportunity being
distributed is seat-specific. A CPT who has flown many Second Pilot
duties now sorts as under-used for Commander, where before they sorted
as heavily used. **Fatigue is not what this balances** — the FTL gate
handles that and is untouched.

The pool is still selected by grade (who MAY sit there); only the count
is by seat. `NULL` positions count toward neither seat, which is what
"duties in this seat" means.

### Mutation-tested

Four mutations, each failing the suite: `roster_coverage` reverted to
grade (3 tests), seatless crew silently dropped (1), fairness reverted
to grade (2), and seat counts no longer deduping sectors into duties
(1). That last one guards the oldest trap in this codebase —
migrations/003's "single most repeated bug in this platform's history"
— because regrouping was an opportunity to reintroduce raw row
counting.

**The DB-free tests earned themselves immediately**: they caught an
`UnboundLocalError` in the new seatless path (`date_value` used before
it was computed) on a branch that only executes when a NULL-position
row exists. Production has none, and every DB-gated test would have
skipped here — so that would have shipped and only ever fired against
pre-016 data.

### One unreproduced failure, recorded rather than buried

A backgrounded full run took **3h00m** and reported
`test_delete_control_is_enabled_normally_when_the_lookup_succeeds`
FAILED. Its traceback was lost with the run's output. It did not
reproduce: that test passes alone (8.6s), passes with its whole file
(3 passed, 17 skipped, 4.6s), no test file takes even 10s on its own,
and a foreground full run of the identical tree gives **308 passed,
361 skipped in 34.4s** — 300x faster with nothing changed.

So the slow run was almost certainly a stalled or descheduled
background process and the failure collateral to it, but that is an
inference and not a diagnosis, which is why it is written down. **If
that test fails on real Postgres, this is where to start** — it would
mean the run was telling the truth and three green runs since were
lucky.

## 2026-08-31: six trial findings, and the one question three of them were

### The question, answered once: how does a schedule change after it has been used?

**It is superseded by a new version. There is no other way.** Verified
against the triggers rather than inferred:

* `rotation_template_legs` are immutable — the trigger raises on UPDATE
  and DELETE. migrations/019 narrowed that to allow deletion only while
  the template is unused.
* `rotation_templates` rows are immutable except closing an open
  `effective_until`, exactly once.
* `create_new_version()` inserts the new version and closes the old one
  the day before. Instances already generated keep pointing at the
  version that produced them — that is the orphan prevention.
* Deleting works only while a template is unused: no instances, sole
  version.

In production **both templates are in use** (EPE-786-787: 27 instances,
EPE-802-804-805: 19). So both are permanently un-editable and
un-deletable. Findings 3, 5 and 6 are three faces of that one fact, and
were answered together to avoid three inconsistent answers.

### 5 — "Create a new version" RELOCATED, not removed

The operator asked for it off the page. Removing it would have left **no
way to change a schedule already in use**: legs immutable, template
undeletable, and the create form rejects an existing rotation_code
outright (it already says "use 'Create a new version' below"). EPE 786
could never have departed twenty minutes later.

Pushed back with that; operator agreed. It now lives in each template's
own expander as **"Change this schedule"** (`render_change_this_schedule()`).
The page loses a third competing top-level form — the actual complaint —
and the workflow is found next to the template it changes rather than by
remembering a section further down. The old section-level rotation-code
selectbox is gone: the choice is now which expander you opened.

Widget keys that were fixed strings (`cv_effective_from`) are now keyed
per rotation code, because the block renders once per template instead
of once per page.

### 3 — the Delete button is no longer rendered when it cannot work

In production it was permanently disabled for **both** templates. The
reason caption stays — "why can't I remove this?" must be answered in
place — but the dead control is gone and the caption now points at the
action that IS available, immediately above it.

Deliberate asymmetry: a delete whose availability could not be
DETERMINED (pre-019 database, stale module) still reads as a fault,
because that is a different statement from "this template is in use".

### 6 — sections merged, and the note the operator drafted was FALSE

"Go back to Create a new template and edit" cannot be followed:
templates cannot be edited, and an existing rotation_code is rejected.
It would have walked a controller into an error message. Replaced with
what is actually true — reject the draft, open the template, use
**Change this schedule**.

"Expand a window into drafts" and "Review drafts" are now one section.
They previously had two ideas of "which window am I looking at": you
picked dates in one and reviewed a list that ignored them entirely.

**Drafts outside the window are COUNTED, not silently dropped.**
Narrowing a view to hide work looks like tidying and isn't — a draft
that scrolls out of sight is a draft nobody approves or rejects.

### 2 — "not listing all flights" was FINDABILITY, measured not argued

`tests/test_flt_schedule_selector.py` renders the real page at
production's real size (103 flights, 5 legs/day, newest-first) and
asserts the selector offers **every** flight the table holds, in order,
oldest included. It does. There is no date filter, no default status
filter and no LIMIT anywhere in that path, and production has **no two
flights sharing a number, date and route**, so no label collision was
hiding rows either.

So the fix is a date window to narrow BY CHOICE — and **it defaults to
every flight**. A window defaulting to "the last week or so" reads as
helpful and is the same mistake in a new place: recording actuals is
what you do about a flight that already operated, sometimes weeks late,
so a controller chasing a three-week-old actual would meet "No flights
in this date range" and conclude the flight was gone. Anything a
narrowing does hide is counted in a caption.

Also fixed in passing: the `format_func` did
`flights_df[flights_df["flight_id"] == fid].iloc[0]` per option — an
O(n) scan inside an O(n) loop, ~10,600 row scans per render, on a page a
controller uses daily. `display_labels.flight_labels()` exists for
exactly this and was not being used.

### 1 — `2003z`, and the two exclusions that matter

`display_labels.utc_stamp()` renders `25 Aug 2003z`; `format_timestamps()`
converts a whole frame for display. **Seconds are dropped everywhere**
(operator decision): FDP, rest and the D7.1.2 buffers are all minutes,
and `:35` implies precision the data does not have.

**PLAIN DATES ARE NOT TOUCHED, and this is the load-bearing part.**
`25 Aug 2003z` drops the year, which is right for a schedule read inside
a month-long window and *wrong* for a crew qualification — rendering a
medical expiring 2026-07-01 as "01 Jul" removes the digit that says
whether the pilot may fly. Columns are detected by dtype, and the
`isinstance` order puts `datetime` before `date` precisely because
datetime subclasses date. `dt.time` IS converted: a bare clock has no
year to lose.

**THE CSV EXPORT KEEPS ISO, DELIBERATELY** (operator decision), and
`reporting.dataset_to_csv()` now says so in its own docstring. A CSV is
a machine-readable regulatory artefact that outlives its session:
`2026-08-25 20:03:35` sorts as text, parses anywhere, and carries the
year. Anyone "fixing" the inconsistency with the screen is proposing to
drop the year from a compliance record.

`flight_label()` gained the time — `EPE 786 · 20 Aug 1900z`. Production
has no collisions today, but a rotation flying the same number twice in
one day is ordinary for a cargo operator and would have rendered as one
duplicated entry, which looks exactly like a missing flight.

### 4 — day offset explained, and kept

**Every leg in production carries `day_offset = 0`** — all five, across
both templates; Air Eagle's rotations are same-day. Kept anyway: an
overnight sector is ordinary for a cargo operator, removing the column
would need a migration, and it costs one field. Help text now says what
it means, including the part that is easy to get wrong — it shifts the
DEPARTURE, so a leg departing 2300 and landing 0130 is still offset 0.

### Mutation-tested

Four mutations, each failing the suite: the formatter converting plain
dates (expiry years lost), `flight_label` losing the time again, the
hidden-flight count removed, and the dead Delete button restored.

330 passed, 361 skipped locally; reachability clean.

### The one test that had to be rewritten rather than deleted

`test_delete_is_disabled_with_a_reason_once_the_template_is_used`
asserted a single DISABLED "Delete template" button, which is exactly
the behaviour this change replaced — so it failed on real Postgres
finding zero buttons. It is now
`test_a_used_template_refuses_deletion_and_offers_the_change_path`.

Deleting it would have been the easy move and would have lost real
coverage: it is the only test that proves a USED template refuses
deletion against migrations/019's own rule rather than against a faked
deletability lookup. What it pins now is what actually matters — the
template and its generated rotations survive, the refusal still says
why, and the page names the one action that is available.

**A test pinning behaviour you deliberately changed is stale, not
wrong.** Rewrite it to the new contract; deleting it silently drops
whatever else it was covering.

### ⚠ Reboot required

`services/display_labels.py` is now imported by pages 5, 6 and 7, which
did not import it before. That is the second limb of the
stale-`sys.modules` rule — "a page importing a service module it did not
import before" — so this needs a **reboot from Manage app after
deploying**. No migration.

## 2026-09-01: generation proposes, accept writes — and the side effect that was holding legality together

### The defect this branch exists to prevent, not to fix

Generation used to write PROPOSED roster rows as it walked the window.
Nobody chose that as the cross-rotation legality mechanism, but it was
one: by the time rotation 2 was validated, rotation 1's rows were
already in the `roster` table, so `_fetch_duty_rows()` returned them and
rest, overlap and cumulative limits were all checked across the whole
window. **The enforcement was a side effect of the writes.**

A preview writes nothing. Remove the writes and that enforcement
disappears **in silence** — every rotation validates against an empty
history, every rotation passes on its own, and the SET is illegal.
Nothing raises. No count looks wrong. The seats just fill, and a
controller publishes a roster with one pilot in two cockpits.

`assignment_service.ProvisionalDuties` replaces the side effect: each
pair the run accepts is recorded in memory in the duty-row shape and
unioned into the gate's own history read, through a one-line seam
(`_provisional_duty_rows()`) that exists so a test can switch it off.

### The test was built first, and watched fail

`tests/test_cross_rotation_legality.py`. Thirty-six rotations, three in
the air together each day, **two** commanders. Every rotation is
individually legal for either of them; taking two at once is not.

- union ON: 14 of 36 filled, 22 uncovered, **zero double-bookings**
- union OFF: **all 36 filled, 30 double-bookings**

The refusal text is the real gate speaking, not a re-derived rule:
`Duty overlaps previous duty.; Insufficient rest before duty. Available
-04:45, required 12:00.` A **negative** available-rest figure can only
be computed against a duty the engine can actually see.

**The failure is asserted, not described.** A third test runs both
configurations and asserts the difference. "We watched it fail once" is
not a property of the suite and does not survive the next refactor.

**Why 36 and not 2, and why a TIGHT pool.** Two rotations prove the
mechanism. They do not prove it survives the case that matters — and a
*healthy* pool hides the defect completely. At 6 commanders and 36
rotations, fairness ordering alone spreads pilots six days apart, so the
set comes out legal whether the union works or not. The first fixture
drafted here did exactly that and would have passed with the union
disabled. The defect only bites where real operations bite: a pool tight
against the schedule.

**Two count predictions in this file were wrong and were corrected to
properties.** Coverage under a tight pool depends on the fairness
ordering — a CPT taken for a Second Pilot seat is a commander lost for
the next rotation that day — so exact counts were replaced by ranges,
by the reason text, and by a direct comparison of the two runs.

### Round-trips: measured, not assumed

36 rotations, 6 commanders, 10 second pilots, all crew valid, all
rotations fill. Counted with the existing `RoundTrips` counter.

| | round-trips |
|---|---|
| BEFORE — `generate_for_window()` on main | **652** |
| AFTER — `generate_preview()` (writes nothing) | **256** |
| AFTER — `accept_preview()` | **542** |
| AFTER — generate → accept | **798** |

**+146 (+22%) end to end.** `publish_window()` is unchanged and applies
identically to both sides, so it cancels out of the comparison.

**Provisional rows themselves cost ZERO round-trips, and invalidate
nothing.** The union is applied AFTER `Prefetch.duty_rows` is consulted,
never into it, so the cache holds database answers only — and during a
preview nothing is written, so a database answer read at rotation 1
cannot have been invalidated by rotation 35. Caching the union instead
would have to drop every entry for a crew member on every fill and would
put the per-candidate duty-history query back, once per rotation: the
exact 2026-08-22 defect.

The preview is **cheaper** than the old generate (256 vs 652) — no
writes (324 gone) and no `_check_downstream_impact` reads (72 gone,
correctly: nothing was written, so nothing downstream broke). The whole
+146 is one extra validation pass, which is what buys the review step.

**One regression was found by measuring and fixed.** Accept first cost
685, of which **144 were single-row `crew_service.get_crew()` calls** —
2 pilots x 2 reads x 36 rotations — because accept deliberately passes
no prefetch. Same shape as the per-candidate birthday lookup removed on
2026-08-22, and minutes against Supabase. Accept now takes ONE fresh
bulk snapshot and passes a per-ROTATION `Prefetch` carrying only that.
Per-rotation matters: duty history must still be read live, because the
whole point is that rotation N+1 sees rotation N's committed rows.

Taking the snapshot at accept rather than reusing the preview's is what
keeps "re-validated against fresh data" true — it is as old as the
Accept click, not the Generate click.

### Partial accept — designed, not discovered

35 written, one refused on re-validation. What the page shows:

- **Written rotations are marked written and stop being offered.** They
  are real rows now; a screen still presenting them as pending invites a
  second Accept.
- **The refused rotation is KEPT, with its proposed crew and the
  re-validation reason.** Discarding it destroys the only record of what
  was refused and why — the one thing on the screen needing action.
- **There is no second Accept, and the service refuses one too**
  (`accept_preview()` raises on an already-accepted preview). This is
  not tidiness: the 35 rotations that just committed have CHANGED the
  legality context the refused rotation was proposed in, so replaying it
  would be proposing from stale information — **the same defect class
  this whole branch exists to prevent**. Re-running Generate rebuilds
  against the rows that are now real.

Per-rotation, not all-or-nothing — the same choice `publish_window()`
already makes, for the same reason: one pilot's changed circumstances
must not cost a controller the other thirty-five rotations of work.

### Accept writes PROPOSED, and Publish is untouched

**A call worth flagging.** The preview adds a review stage BEFORE the
first write; it does not remove the one that already exists after it.
`publish_window()` and its fresh re-validation are unchanged, and a
roster still becomes visible to crew only by being published. Nothing in
production behaviour is lost. It is one constant
(`ACCEPTED_ROSTER_STATUS`) if that should have been PLANNED instead.

### Two tests were rewritten to the new contract, not deleted

- `test_generation_makes_no_query_per_candidate_for_ages` asserted
  `get_all_crew == 1`. It is now **2** — one bulk read per stage — and
  that is the contract, since accept must take its own fresh snapshot.
  Rewritten as `test_generation_reads_crew_in_bulk_and_never_per_candidate`,
  which now asserts the real guard **directly**: `get_crew == 0`. The old
  assertion was a proxy for it.
- `tests/test_roster_generation_page.py` pinned the one-click flow
  throughout. Every test needing rows now clicks Generate AND Accept;
  what each was actually covering is still covered. Two are new, having
  no predecessor: that Generate writes NOTHING (asserted against
  `search_roster()` **between** the clicks — the only way to tell this
  redesign from a relabelling), and what a partial accept leaves behind.

### ⚠ Reboot required — and a THIRD limb of the rule

`pages/6_Roster_Generation.py` now does
`from services.roster_generator_service import ... OUTCOME_PROPOSED, ...`
— **new names from a module it already imported.** No new module appears
anywhere, so neither of the two known limbs fires, and this one fails
*harder* than either: against a stale `sys.modules` entry it is an
`ImportError` at page load, not a subtly wrong result.

**Why this rule keeps catching people, recorded because it will happen
again.** The first three occurrences all ADDED A MODULE, so that is the
shape everyone now looks for. The second limb — three existing pages
gaining an import of an existing module (`display_labels`, 2026-08-31) —
is invisible unless specifically checked for, because **no new file
appears in the diff**. This third limb is worse still: no new file AND
no new import line, just a longer one. The reliable check is not "did a
file get added" but **"does any page's import statement differ from what
the running process executed"** — which includes the names inside it.

No migration.

### Verified

333 passed, 364 skipped locally; reachability clean. **The DB-gated
tests are unverified here** — no local Postgres, so
`test_roster_generation_page.py` (including both new tests) and
`test_roster_generator_service.py` skip. The partial-accept page test in
particular depends on fairness giving two different commanders to two
consecutive days; it asserts that precondition explicitly rather than
failing obscurely if it does not hold.

**Also confirmed: the 2026-08-28 "slow background run" is real and
reproducible as an ARTIFACT.** The round-trip measurement took 1109s
(18m29s) when the tool backgrounded it at 600s, and **5.09s in the
foreground immediately afterwards with identical output and identical
counts**. Same 300x-scale discrepancy, same cause, nothing to do with
the code. Prefer foreground runs when timing matters.

### 2026-09-01 (later): the partial-accept "failure" was the fixture, and the guard is now DB-free

Real Postgres 16: 696 passed, 1 failed.
`test_a_rotation_refused_at_accept_keeps_its_crew_and_its_reason`
asserted `set(rows["duty_date"]) == {mon}` and got `set()` — **nothing
committed at all**, while every UI assertion above it passed.

That reads exactly like all-or-nothing transaction scope. **It is not.**
Two independent confirmations, because the read is genuinely ambiguous:

1. `accept_preview()` opens no transaction. The only `engine.begin()` on
   the path is inside `assign_pair_to_duty()`, scoped to one rotation's
   two seats.
2. Reproduced DB-free: with 2 CPT + 2 FO over two days, the fairness
   ordering crews **both CPTs on both days** — CPT-01 commands Monday
   with CPT-02 as Second Pilot, and they swap on Tuesday. The FOs are
   never used at all.

So `doomed_id` (Tuesday's Commander) was **also Monday's Second Pilot**.
Grounding them correctly refused BOTH rotations. Nothing was written
because nothing was legal, and the page reported that correctly.

**The precondition guard checked the wrong thing.** It asserted the two
Commanders differ — they did — while the doomed pilot occupied a seat on
the other day anyway. The Second Pilot pool includes CPTs, and under
seat-scoped fairness (2026-08-28) a CPT with zero Second Pilot duties
sorts as *under-used for that seat*, so an all-CPT crew is the natural
output of a small pool. That is the fairness rule working as designed;
it is only a problem for a fixture that assumed seats imply distinct
people.

**Fixed by dooming the pilot crewed on the FEWEST rotations** (checked
across BOTH seats, not just Commander) over a five-day window with 3 CPT
+ 3 FO, and asserting the written and refused sets are both non-empty —
so a fixture that stops producing a genuine partial failure says so
instead of inverting its own claim.

#### `tests/test_partial_accept.py` — the guarantee no longer depends on Postgres or on crew selection

A fixture that can raise a false alarm about a designed behaviour is a
reason to test that behaviour where crew selection cannot reach it. Two
tests, both DB-free:

* the rotations that pass are written and stay written, asserted against
  the **statements actually issued** rather than the returned summary;
* **one transaction per written rotation, not one for the window.**

The second exists because a recording engine cannot roll back, so the
INSERT assertions alone would pass unchanged against an all-or-nothing
implementation. Counting transaction boundaries is what separates them.

**Mutation-tested, both caught:** wrapping the loop in a single outer
`engine.begin()` (transactions 3 != 2), and reporting a refused rotation
as WRITTEN (both tests fail). The measured run is a genuine partial
accept — 2 written, 1 refused.

**Correction to the entry above:** it named the fairness precondition as
the risk in this test and was right that there was one, but wrong about
which. The risk was not "fairness might give both days the same
Commander"; it was "a pilot can crew two rotations without commanding
either."

335 passed, 364 skipped locally; reachability clean.

### 2026-09-01 (later still): accept IS publication — PLANNED, not PROPOSED

**Operator decision, reversing the call in the first entry above.** That
entry kept accept writing PROPOSED on the reasoning that the preview
adds a review stage BEFORE the first write without removing the one
after it. The question that settles it is what the second stage was FOR.

`publish_window()` re-validates because the old generator wrote PROPOSED
rows speculatively and arbitrary time passed before anyone looked at
them. `accept_preview()` validates and writes **in the same call**, per
rotation, with no gap between the check and the commit — so publish's
re-check is re-checking work validated moments earlier.

And the review that stage was supposed to enable — rejecting a proposal
on the Roster page — **did not work: Roster does not show PROPOSED
rows.** A review stage whose review mechanism is missing is not a stage,
it is an unlabelled delay. The preview is the better place for that
review anyway: rejecting there costs nothing because nothing is written,
where rejecting a PROPOSED row means writing a CANCELLED row to undo a
write that should not have happened.

So `ACCEPTED_ROSTER_STATUS = "PLANNED"`, the button reads **"Accept and
publish"**, and the page warns that accepting makes the roster visible
to crew immediately.

The one thing publishing did still buy is worth naming, because it is
now gone deliberately: **a controller could accept on Monday and publish
on Thursday, with a freshness re-check at the moment crew were told.**
That is a publication-scheduling feature, not a legality one. If it is
ever wanted, it should come back as a scheduled publication date, not as
a status a controller has to remember to flip.

#### publish_window() is kept, as cleanup, and the page hides it

Production holds PROPOSED rows from before this change. They are
invisible on the Roster page and nothing else promotes them, so deleting
`publish_window()` would strand real roster rows.

**The control renders only when such rows exist in the window.** A
permanent Publish button would reassert the three-step flow this change
removed, and a controller would reasonably read it as accepting not
having finished the job. When the last legacy row is published the
section disappears for good, and the function can go with it.

#### Tests that could no longer get a PROPOSED row from the generator

Four tests in `test_roster_generator_service.py` and two page tests
existed by generating PROPOSED rows and then publishing them. Generation
cannot produce one any more, so they seed it through the real pair API
(`_seed_proposed_pair()` / `_legacy_proposed_rotation()`) — validated,
paired, atomic, exactly what the old generator left behind. **Only the
provenance of the rows changed; `publish_window()` is untouched, and so
is every property those tests pin** — per-rotation re-validation, pair
atomicity, the row-vs-duty count, and the PROPOSED visibility contract
(covered for OCC, hidden from crew), which still governs every
pre-2026-09-01 row.

Two page tests renamed to what they now check: accept publishes directly
and offers no further step, and legacy rows can still be published.

335 passed, 365 skipped locally; reachability clean. Everything
DB-gated here is unverified locally as usual.

### 2026-09-01 (last): the button that would not go away was AppTest, not the page

Real Postgres: 698 passed, 2 failed, both the same shape —
`assert not [b for b in at.button if b.label == ...]` after a click that
had visibly done its work. The refusal message rendered and named the
crew, the publish reported four rows, the roster was correctly PLANNED.
Only "the control is gone" failed.

**The page is correct. The assertion was.**

#### The trap, which is not obvious and will catch the next person

A click handler ending in `st.rerun()` runs the script TWICE inside a
single `at.run()`. Both passes enqueue into the SAME `ForwardMsgQueue` —
`LocalScriptRunner` builds its queue once in `__init__` and parses the
tree from whatever is in it when the run ends.
`ForwardMsgQueue.enqueue()` replaces messages **by delta-path index**, so
the second pass overwrites the first only as far as it reaches.

**When the post-rerun render is SHORTER than the pre-rerun render, the
tail of the first pass survives into the parsed tree.**

That is exactly this page's shape. The proposal branch is long — info,
proposal table, uncovered expander, two fairness columns, warning,
button — and the accepted branch is short. The Accept button sits near
the end of the long render, so nothing in the short render overwrites
it. Identical story for the legacy publish section, which disappears
entirely once its rows are published.

**Reproduced minimally, outside this app, before changing anything:**

* two branches of the SAME length — button correctly gone after the click
* long branch (6 elements + button) replaced by a short one — **stale
  button still listed**, alongside the new content, and gone on the next
  `at.run()`

That second case is the failure, in eleven lines and no database. It
also explains why this never bit before: every earlier click on this
page replaced a branch with one at least as long.

`AppTest.run()` constructs a NEW `LocalScriptRunner` with a new queue,
so one more `run()` is a clean tree. `_rendered_afresh()` is that one
line, with the reasoning attached so nobody "simplifies" it back.

**Assertions that a control EXISTS were never affected** and need no
help — a leftover can only falsify "it is gone."

#### Was the button merely displayed, or genuinely live?

Merely displayed, and only inside the test harness. On a fresh render
the control is not in the tree at all, and a Streamlit widget that is
not rendered cannot be clicked — there is no live path to a second
accept in the running app.

**Hardened anyway, because the objection was right about what a stale
click should meet.** The accept call is wrapped so `accept_preview()`'s
refusal surfaces as its own explanation rather than a traceback. The
branch should be unreachable — reaching the button requires
`is_accepted` to have been False when the page rendered, and nothing
else can flip it — but a safety rule that presents as a raw traceback
has failed at the only moment it matters.

#### The rule now lives where no page can weaken it

`test_a_spent_preview_refuses_a_second_accept_and_writes_nothing`
(DB-free) pins the actual guarantee: a second `accept_preview()` raises,
opens no transaction and writes no row. The absent button is the UI's
expression of that rule; this is the rule. Asserted with the recorder,
so "refuses" means nothing was touched — not merely that an exception
came back after the damage.

336 passed, 365 skipped locally; reachability clean.

## 2026-09-02: why a seat could not be crewed — readable first, complete underneath

Reported from live use. One rotation's reason was every attempted pair
concatenated into a paragraph:

```
CPT-05+FO-01 (REJECTED): commander: CPT-05's MEDICAL expired 2026-08-31...;
second pilot: FO-01's SIM expired...; CPT-05+FO-03 (REJECTED): commander:
CPT-05's MEDICAL expired 2026-08-31...; CPT-05+FO-04 (REJECTED): ...
```

The content was right; the presentation buried it. **The same commander
rejection repeats in every line**, because the search tries that
commander against every second pilot in turn and records each failure
whole. It now reads:

```
No eligible Commander — CPT-05's MEDICAL expired 2026-08-31, not valid
for duty date 2026-09-07. Tried 3 combination(s). Detail: <every trial>
```

### The seat asymmetry is the whole insight

When the **Commander** is unavailable, every pair fails for that reason
whoever the Second Pilot is — so the Second Pilot reasons are **noise**,
and naming them points a controller at the wrong renewal. When a
Commander was available and only Second Pilots failed, the opposite
holds and the list of who and why **is** the answer.

The commander test is deliberately *every trial that named a commander
was blocked by that commander*, not *some trial mentioned a commander
problem*. A pool where one commander is grounded and another is merely
busy must NOT report "no eligible Commander" — that would send someone
to renew a medical when the real blocker was rest. Pinned by
`test_a_partly_blocked_commander_pool_does_not_read_as_no_commander`,
and mutation-tested by weakening `all(...)` to `any(...)`.

Same shape as the alert-volume problem and the same treatment: bucket by
root cause, lead with the blocking one, collapse repeats, and keep
status derived from the full set rather than from the summary.

### Summarised from STRUCTURE, never parsed back out of the string

The facts are all in hand at the moment each trial fails.
`_pair_reject_trial()` / `_reject_trial()` now return a `RejectedTrial`
carrying the commander reason, the second pilot reason, the pair reason
AND the full sentence; `_pair_reject_reason()` / `_reject_reason()` are
thin wrappers returning `.text`, byte-identical to what they always
produced.

Joining those sentences and re-splitting them later would have made the
display depend on the exact punctuation of prose written for humans.
That is the drift this codebase has repeatedly paid for.

### The record loses nothing — the summary is PREPENDED

`uncovered_seats.reason` is the only surviving explanation of an
unfilled seat and is regulatory evidence (2026-08-26 decision). So this
does **not** substitute a summary for the detail: every trial line
survives character for character after `Detail:`, asserted by
`test_every_trial_survives_verbatim_in_the_stored_reason`. The stored
column strictly gains a derived headline.

A small win found on the way: the summary read `CPT-05: CPT-05's MEDICAL
expired...` because `build_audit_reason()` already opens with the crew
member's own id. Naming a pilot twice in a sentence written to reduce
repetition would be a poor joke; the prefix is now added only to reasons
that do not already identify who they are about.

A second one found in verification: the joined reason read
`...duty date 2026-09-08.. Tried 3 combination(s).` — `build_audit_reason()`
punctuates its own sentence and the joiner added another. The terminator
is now added only when the summary has not supplied one, rather than
stripped afterwards, which would eat a legitimate ellipsis. Both
directions pinned, and mutation-tested by making the append
unconditional.

### ⚠ Reboot required — and a FOURTH LIMB of the rule

**No import changed anywhere**, so both known checks pass this branch
clean. But `pages/6_Roster_Generation.py` reads `rotation.outcome_summary`,
a NEW ATTRIBUTE on a `PreviewRotation` built by
`roster_generator_service`. Against a stale `sys.modules` the page gets
the OLD dataclass, which has no such field, and every uncovered rotation
raises `AttributeError` — the 2026-08-19 outage exactly.

**The rule now has four limbs, and the honest statement of it is not
about imports at all:** a reboot is needed whenever a page depends on
anything the running process's copy of a service module does not have —
a new module, a new import edge, a new NAME in an existing import, or a
new ATTRIBUTE on an object that module builds.

The page reads it through `getattr(..., None)` with a fallback to the
old unsummarised sentence, truncated. That does not remove the reboot
requirement; it decides what a missed reboot costs — the old wall of
text instead of the page.

## 2026-09-03: a delay that made a duty impossible produced no warning

Reported from live use. Flight 53 (EPE 786, KHI-LHE) planned 1900-2045z
was recorded 2200-2345z while its second sector, flight 54, still read
2200-2345z. Sector 1 landed at the moment sector 2 departed. Status went
to OPERATED and nothing was reported.

**It detected nothing** — not "detected and failed to surface". Traced:

* `update_flight_actual_times_and_revalidate()` ->
  `_recompute_one_duty_after_delay()`, which **never called
  `build_duty()`**. It computed `debrief = sectors[-1].arrival_utc +
  buffer` directly.
* the validator's `_check_overlaps()` compares `duty.start_utc` /
  `duty.end_utc` **between duties**. Nothing checked sectors **within**
  one.
* `build_duty()`'s continuity check ran only at assignment time and at
  rotation expansion — on PLANNED times, where this duty is perfectly
  coherent. That is exactly why planning-time checking never caught it.

### The worse defect underneath: actuals used for arithmetic, never for ordering

`duty.sectors` are appended in query order — `ORDER BY r.report_time,
f.dep_time_planned` — and never re-sorted once actuals arrive. So
`sectors[-1]` is the last sector by PLANNED departure.

**A delay on a NON-FINAL sector therefore ended the duty earlier on
paper than the crew actually finished, and the recorded FDP understated
it.** Measured on the real classes:

```
sectors[-1].arrival_utc   : 2026-09-02 23:45   <- flight 54, untouched
max arrival across sectors: 2026-09-03 01:00   <- when the crew finished
debrief computed          : 2026-09-03 00:00
```

Now `max(s.arrival_utc for s in duty.sectors)`. **THIS CHANGES FDP
VALUES ON EXISTING DELAYED DUTIES**, upward, and only where a sector
overtook another — a duty whose sectors are still in order is
bit-identical, which is pinned. Upward is the safe direction: the old
number was too low, and a plausible number that is too low is worse
than a missing one, because nothing about it invites a second look.

In the reported case the two arrivals TIE, so FDP stayed 5.75h — not
stale, recomputed to the same number. Every figure on screen looked
right while the duty was impossible.

### One rule, two callers, opposite behaviour

`sector_continuity_problems()` extracted from `build_duty()` into
`core/duty_builder.py` as a pure predicate returning sentences.

* **Planning must refuse** — `build_duty()` turns any problem into
  `ValueError`, unchanged.
* **Recording must not** — what happened, happened. A controller
  entering an actual mid-duty is doing their job, and blocking it would
  leave the record less accurate, not more.

A second copy of those two comparisons living in the recording path is
precisely how the two would drift into disagreeing about what
"continuous" means.

**NEEDS_MANUAL_REVIEW, not ILLEGAL and not WARNING** (operator
decision). No ANO-012 limit has been exceeded, so it is not a violation;
it is a duty whose recorded times cannot be assessed, which is what that
status means. It also flags every roster row sharing the duty_id as
NEEDS_REVIEW through machinery that already existed, so the duty carries
a durable marker rather than a sentence that prints once.

### Found while fixing it: every delay warning was being thrown away

`pages/3_Flight_Log.py` wrote its warnings and then called `st.rerun()`,
which **abandons the current run** — so nothing written before it ever
reached the browser. The swap alerts have been invisible since the day
they were written, and the new continuity warning would have joined
them. Notices are now queued in `session_state` and rendered at the top
of the next run.

**This is the FOURTH distinct `session_state` case, and they are not
interchangeable:** Schedule Templates needs generation-keyed widget
keys, Crew Data must have none, Roster Generation holds computed work
deliberately not persisted, and this one carries a message across
exactly one rerun and drops it. Do not consolidate them.

A suspected fifth defect was NOT one: `result.status in ("ILLEGAL",
...)` compares an enum to strings, but `AlertStatus(str, Enum)` makes it
work. Checked rather than assumed, and recorded so nobody "fixes" it.

### The guards, and why the first version of them was theatre

`tests/test_sector_coherence.py`, DB-free. **The first six tests
exercised the pure rule and a REIMPLEMENTATION of the debrief
arithmetic — and deleting the continuity check from the service, or
putting `sectors[-1]` back, left every one of them green.** Same shape
as the `_read_duty_rows` seam lesson: a test that reimplements the thing
it tests measures its own copy.

Three further tests drive the real `_recompute_one_duty_after_delay()`
over fake leaves, patching `_read_duty_rows` so the real
record-building runs. All four mutations now fail: the check removed,
`sectors[-1]` restored, the alert downgraded to WARNING, and the rule
itself defanged.

No new module, no new page import, no new attribute read by a page — so
**no reboot**, and no migration.

## 2026-09-03: anything written before `st.rerun()` is discarded — and no test can see it

Its own entry, because it is not about the bug it was found beside and
it will happen again.

`st.rerun()` **abandons the current script run**. Every `st.warning`,
`st.error` and `st.write` issued earlier in that same run is thrown
away and never reaches the browser. The user sees the page re-render
with none of it.

`pages/3_Flight_Log.py` did exactly this after recording an actual:
computed the revalidation outcomes, wrote the warnings, then called
`st.rerun()` to refresh the flight table.

**So the swap alerts have never been visible.** Not since a regression
— since the day they were written. A feature that was designed, built,
tested, verified against real Postgres and merged, and never once
reached a controller's screen.

### Why the suite could not catch it, and still cannot

`AppTest.run()` executes the script, and a `st.rerun()` inside it runs
the script AGAIN within that same `at.run()`. Both passes enqueue into
one `ForwardMsgQueue`, and `enqueue()` replaces messages by delta-path
index — so a message from the discarded first pass **survives into the
parsed tree** wherever the second pass is shorter. (Same mechanism as
the 2026-09-01 finding about asserting a control is gone.)

The consequence is worth being blunt about: **an AppTest assertion that
a warning appears can pass while the browser shows nothing.** The test
is not merely blind to this defect; it actively reports the opposite of
the truth. No amount of care writing that assertion helps.

### What to do instead

Queue the messages and render them at the top of the NEXT run:

```python
_NOTICES = "some_page_notices"
for notice in st.session_state.pop(_NOTICES, []):
    st.warning(notice)          # rendered on the run AFTER the rerun
...
st.session_state[_NOTICES] = notices
st.rerun()
```

**The check to apply when reviewing any page:** find every
`st.rerun()`, and read upward. Anything written between the top of that
branch and the rerun is invisible. If it matters, it must survive the
rerun; if it does not matter, it should not have been written.

Three other `st.rerun()` calls on that page write only `st.success`
lines for actions whose result is visible in the refreshed table, so
they are harmless — but that was checked, not assumed, and the same
check is owed to `pages/1_Control_Room.py`, `pages/4_Roster.py`,
`pages/6_Roster_Generation.py` and `pages/7_Schedule_Templates.py`,
which have not been audited for this. **NOT DONE — an honest open item
rather than a claim of completeness.**

## 2026-09-03: the Roster table reads as seats, not as rows

"Current assignments" showed one row per crew member per sector, with
`crew_id`, `role`, `duty_id`, `flight_id` and a serial column — internal
identifiers on a screen nobody debugs from. It is now one row per
flight: **Flight, Route, Commander, Second Pilot**.

### Commander / Second Pilot, not PIC / SIC — a choice, not the schema leaking

PIC and SIC are the operator's own words, and migrations/016 records
them as equivalent to COMMANDER / SECOND_PILOT. The display could
legitimately diverge from the data model here.

It does not, because `roster_coverage`'s headers were standardised on
Commander / Second Pilot on 2026-08-28, and **one concept with two names
across two screens is worse than either name**. Operator decision
(2026-09-03). Recorded so this reads as settled rather than as nobody
having noticed the operator says PIC.

### `crew_seat_name()` — the rule was chosen against the real names

`CPT M Waqar`: grade, given-name initial, surname. Added to
`display_labels` beside `crew_label()`/`flight_label()` rather than
started as a new module.

The rule was picked by running candidates over all ten of Air Eagle's
actual crew records, not reasoned about in the abstract:

* **first-name-only renders SIX OF TEN pilots as "Muhammad"** and
  identifies nobody. The operator's own illustration ("CPT Fahim") was
  a rule that worked on one name.
* **initial-plus-surname separates all ten**, and gives `CPT S Mahmood`
  for the pilot a controller may say aloud as "Fahim". Trade accepted
  deliberately.
* **one stored name begins with a rank** — `CAPT MUHAMMAD ASAD ALI` —
  so the naive rule initials a pilot from their title: `CPT C Ali`.
  `NAME_TITLES` strips it. Two names also carry trailing whitespace and
  all are stored uppercase.

### The same NULL means opposite things depending on grade

`operating_position` is NULL in two unrelated situations, and the
distinction is kept explicit in code rather than handled by analogy
(operator decision):

* **on a CPT or FO it is an ANOMALY.** Someone holds a flight-deck seat
  the data failed to record; dropping them hides a real assignment.
  Surfaced in a "Seat not recorded" column, and the seat they did not
  fill still reads UNCOVERED rather than being treated as covered by
  them. Same treatment as `roster_coverage`.
* **on an LM or ENGR it is NORMAL.** They are outside the flight-deck
  model by design — Air Eagle does not even hold them as crew records.
  A flight carrying only them has no flight-deck assignment, so there
  is nothing to omit, and it does not appear at all. Same reason a
  wholly uncrewed flight stays out; Roster Generation's uncovered panel
  owns those.

**Conflating them fails in both directions**: treat LM like the anomaly
and the table fills with cargo flights until the column is ignored;
treat the anomaly like LM and a real assignment vanishes. Both
mutation-tested.

`COCKPIT_GRADES` is derived from `SEAT_ELIGIBLE_GRADES` rather than
retyped, so a grade added there cannot quietly become "not cockpit"
here — which is exactly what decides which of the two meanings applies.

### A latent crash found by the fixture

`a["operating_position"] or ""` was not safe. When EVERY roster row on
a flight has a NULL seat — an LM/ENGR-only flight — pandas types the
column float64, and **`nan or ""` evaluates to `nan`, because nan is
truthy**. The unassign selectbox then concatenated a float to a string
and the page raised `TypeError`.

Latent rather than live, since Air Eagle holds no LM/ENGR crew records
today — and it surfaced only because a DB-free fixture created what
production has not. Now an explicit `pd.isna` check.

### Mutation-tested

Four mutations, each failing the test written for it: LM/ENGR treated as
an anomaly, the cockpit anomaly silently dropped, flights with no
flight-deck assignment included, and honorific stripping removed.

370 passed, 365 skipped locally; reachability clean. No migration.

### ⚠ Reboot required — and I wrote "no reboot" first

`pages/4_Roster.py` imports `crew_seat_name` and `flight_label` from
`services/display_labels.py`, a module it already imported. That is
**limb three** — a new NAME in an existing import — which needs a
reboot, and against a stale `sys.modules` it is an `ImportError` at
page load.

I documented that limb on 2026-09-01, wrote "no new page import, so no
reboot" here, and only caught it by running the check mechanically
before pushing rather than trusting the sentence I had just written.

Which is the point worth keeping: **the rule is not hard to remember,
it is hard to APPLY, because the diff looks unremarkable.** Run the
check, do not recall the conclusion:

```bash
git diff main...HEAD -- 'pages/*.py' | grep -E '^\+\s*(import |from )'
```

and then read any new attribute access on a service-built object,
which grep cannot show you.

## 2026-09-05: correcting a crew field now re-checks the duties already written

Reported from live use: an OCC member set CPT-03's SIM expiry to a past
date and saved it. CPT-03 stayed on already-written PLANNED rosters and
nothing was flagged. Future assignments were correctly refused the whole
time — the gate works — but **a pilot whose document lapses today stayed
on next week's published roster silently**, and the only remedy was
somebody noticing.

Built to the GENERAL scope, not the reported slice: the scenario is an
OCC member entering a crew field wrong and correcting it days later, and
that is not only expiry dates. A wrong `base` corrected a week later is
the same operator scenario as a wrong expiry, so all three tiers go
behind **one entry point** — nobody should have to know which field is
"cheap" to trust that correcting it revalidates the roster.

### Two findings that changed the shape of the fix

**1. `update_crew()` was not the only door.** `is_active` is not in
`UPDATABLE_FIELDS` at all — it is written exclusively by
`deactivate_crew()`, a different function with its own call site. A fix
wired only into `update_crew()`, as originally scoped, would have left
**taking a pilot out of service while they still hold future duties**
bypassing revalidation entirely. Both writers now call the one door, and
`test_both_crew_writers_go_through_the_one_door` fails if a third
appears. (There is still no reactivate path anywhere: `is_active` can
only ever go FALSE through the service.)

**2. Nothing could clear `NEEDS_REVIEW`. Anywhere.** The only writer was
`_recompute_one_duty_after_delay()`; no service, page or migration ever
reversed it, and no page even LISTED flagged duties. Combined with "flag
every affected duty, no cap", one corrected field could have permanently
flagged thirty duties with no way back short of unassigning and
reassigning every one — which is how a safety flag becomes a thing
people route around instead of read.

So the clear control shipped WITH the flagger that made it necessary,
not after it. `clear_duty_review_flag()` is the only exit, it is
deliberately human, and it **requires a reason** — the audit row has to
say who decided this duty was fine and why.

### The design

`assignment_service.revalidate_crew_duties(crew_id, changed_fields)` —
one door, three tiers resolved from the fields that ACTUALLY changed:

* **tier 1** (expiries, `is_active`) — `_check_crew_qualifications()`
* **tier 2** (`date_of_birth`) — partner via `_find_paired_pilot()`,
  then `validate_pair()`, which is deliberately the fresh-data path
* **tier 3** (`base`, `role`) — the full gate, `validate_schedule()`

**No new legality logic anywhere.** Every check is the function the
assignment gate already uses; this only re-asks them about duties that
already exist. The tier sets were derived from what the engine actually
consumes, checked rather than assumed: `CrewMember` carries only
crew_id/name/home_base, so `base` feeds rest; `role` feeds
`FTL_EXEMPT_ROLES` and seat eligibility.

`_tier_for()` takes the HIGHEST tier among changed fields, because the
higher check subsumes the lower — and returns 0 when nothing
legality-relevant changed, which is what stops a corrected phone number
walking the roster.

**`changed_fields` is computed by DIFFING old against new**, not taken
from the caller. `pages/2_Crew_Data.py` submits every field on every
save, so trusting the caller's dict would have meant every save
revalidated everything — including a save where the operator opened the
form and changed nothing.

### The decisions, all confirmed by the operator

* **Every affected duty flagged, no cap.** If a correction affects
  thirty duties then thirty duties are genuinely affected.
* **Never auto-clear.** A field moving in the safe direction — a later
  expiry, a base put right — does NOT clear an existing flag. The flag
  does not record "the data is bad"; it records "nobody has looked at
  this duty since the data changed".
* **Future PLANNED only**, and the four exclusions are all deliberate:
  OPERATED (a duty already flown does not retroactively un-happen),
  NEEDS_REVIEW (already flagged; re-examining risks the auto-clear this
  must never do), DISRUPTED (carries its own manual label), PROPOSED
  (legacy rows only since accept began writing PLANNED on 2026-09-01).
  "Future" is measured on `report_time`, not `duty_date` — a duty that
  reported this morning has already started.
* **Tier 2 flags both seats.** Age-pairing is a PAIR rule, so one
  pilot's DOB can make the pair illegal in a way that is genuinely the
  partner's problem too; flagging one seat and leaving the other reading
  as fine is the seat-vs-grade half-truth this project keeps stamping
  out.

**Whole-schedule rules flag no single duty** (D23.1 mandatory days off,
D23.2 seventh day). They belong to no one duty, so attributing one to an
arbitrary duty would invent a precision the rule does not have — they
are REPORTED loudly instead, because silently dropping a legality
failure the change caused is the worse of the two errors.

### The import cycle, broken deliberately in one place

`assignment_service` imports `crew_service` at module level (including
`from services.crew_service import ROLE_SYNONYMS`), so the reverse
import at module level is a genuine cycle — crew_service would begin
loading, hand control to assignment_service, and be asked for
ROLE_SYNONYMS before it had defined it. `_revalidate_after_crew_change()`
therefore imports **inside the function body**, and it is the only place
in `services/` that does.

Wiring this into the SERVICE rather than the page is the whole point —
it is what stops a second caller bypassing revalidation — so the cycle
had to be broken here rather than dodged by moving the call somewhere
weaker.

It also **never raises into the caller's write**: the crew edit has
already committed by the time revalidation runs, and a failure here must
not make the operator think their correction did not save. It audits
`CREW_REVALIDATION_FAILED` and returns an empty result.

### Two page defects found while wiring it

* **The `st.rerun()` swallow again, on Crew Data** — both save handlers
  ended `st.success(...); st.rerun()`, so even "Updated CPT-03" has
  never been seen, and the revalidation report would have landed in
  exactly that discarded space. Queued now. That is the fifth distinct
  `session_state` case in the app and they are NOT interchangeable.
* **`crew_names` was defined inside `if roster_rows:`** on the Roster
  page and is now read by a second section that runs regardless — a
  `NameError` on an empty roster. Hoisted. Same shape as the stale-module
  outage: a name defined in one branch and read from another looks fine
  right up until the first branch does not run.

The flagged-duties section also sits **above** the assignment forms,
because those begin `if flights_df.empty: st.stop()` — appended at the
end it would have silently never rendered on a database with no flights.

### Mutation-tested

Six mutations, each failing a different test: the crew path flagging any
status rather than PLANNED only; the tier router taking the lowest tier;
`is_active` dropped from tier 1 (deactivation silently stops
revalidating); `deactivate_crew` no longer calling the door;
`changed_fields` trusting the caller instead of diffing; and
`clear_duty_review_flag` losing its `NEEDS_REVIEW` guard.

393 passed, 365 skipped locally; reachability clean.

### ⚠ Reboot required — TWO limbs, and the check was RUN

* `pages/2_Crew_Data.py` imports `utc_stamp` from
  `services/display_labels.py`, a module it already imported — limb 3.
* `pages/4_Roster.py` calls `assignment_service.duties_needing_review()`
  and `clear_duty_review_flag()`, new attributes on a module it already
  imports — limb 4. Against a stale `sys.modules` that is `AttributeError`
  at page load.

No migration.

## 2026-09-05: rescued from a cancelled branch — swap alerts, a required field, and two notes

The Flt Schedule column removal and the "move actuals to Control Room"
work were **cancelled** by the operator (2026-09-05), not deferred: Flt
Schedule keeps "Record actuals, update status, or cancel a flight"
exactly as it is. `flt-schedule-readonly-actuals-to-control-room` is
therefore abandoned unmerged.

Three things on it were independent of the move and were rescued here
rather than lost with it.

### A controller has never seen a swap alert

**A live defect on `main`, not something the cancelled branch
introduced.** Control Room's crew-assignment handler, ALLOWED branch,
wrote its success line, its pair alerts, and its SWAP ALERTS —

> ⚠️ Swap alert — this assignment breaks the legality of N
> already-scheduled future duty(ies) for the Commander:
> - Duty ... : **no legal candidates found**

— and then called `st.rerun()`, which abandons the run and discards
everything written in it. The same on the flight-only save path. So the
one message that tells a controller their new assignment has just
broken duties already on the roster has never reached a browser.

**Scoped precisely rather than blanket-queued**, because not every
branch was broken: REJECTED and NEEDS_REVIEW do NOT call `st.rerun()`,
so their messages have always been visible. Only the two paths that end
in a rerun needed queueing, and
`tests/test_control_room_notices.py` pins BOTH directions — so a later
tidy-up that adds an `st.rerun()` to the visible branches, or that
routes a queued message back to a direct write, fails.

That test file also documents why it must assert on the run AFTER the
rerun: an AppTest assertion that a message merely EXISTS can pass while
the browser shows nothing, because a rerun runs the script twice inside
one `at.run()` and the discarded first pass survives wherever the
second render is shorter.

**Pages audited for this defect so far:** Flt Schedule (2026-09-03),
Roster Generation, Control Room (here), Crew Data and Roster (both in
the crew-revalidation branch, same day). **Schedule Templates remains
un-audited** — the last one.

### Flight No. required in the UI, nullable in the schema

Not a contradiction, and recorded so it does not read as one:

* the **form** now demands it (`Flight No. *`), because Air Eagle's
  ad-hoc flights always carry an EPE number;
* the **column** stays nullable — 103 existing flights were created
  under the old assumption, and a NOT NULL migration would make the
  column lie about what the programmatic paths permit;
* `flight_label()`'s `#123 · 04 Sep 1900z` fallback **stays**, because
  it protects the case the schema still allows.

Checked rather than assumed: rotation expansion already REFUSES a leg
without a number (`rotation_template_service` raises, saying numberless
flights go through Control Room), and the import script creates crew,
not flights. **This form was the only path that ever produced a NULL.**

### `cargo_dg` — SETTLED 2026-09-05: the current behaviour is correct

**Operator decision (Arif, 2026-09-05): assume YES — every Air Eagle
flight potentially carries dangerous goods.** The question raised on
2026-09-04 is answered, and the answer makes the code right as it
stands. NO CODE CHANGE, deliberately.

What was flagged, and why it is not a defect: `dg_expiry` sits in
`QUALIFICATION_EXPIRY_FIELDS`, so a DG certificate is checked on EVERY
duty for EVERY crew member regardless of `flights.cargo_dg`. That
reads like a check applied where it does not belong — until the
operator's answer, which is that the premise was wrong rather than the
check. If any flight may carry DG, then **every duty is a DG duty**,
and checking on every duty is exactly right.

**`flights.cargo_dg` is therefore RECORDED FOR REPORTING ONLY and is
DELIBERATELY NOT CONSULTED BY THE LEGALITY GATE.** Not an oversight,
not a TODO: making the check conditional on it would introduce a way
for a crew member with a lapsed DG certificate to be legal for a
flight that turns out to carry DG after the roster was written. The
column answers "did this flight carry DG", which is a reporting
question; it must not become an input to "may this person fly".

`_check_crew_qualifications()` takes a crew row and a date and could
not consult `cargo_dg` even in principle. That is now a property to
PRESERVE rather than a limitation to work around — if a future change
wants to pass a flight into the qualification gate, this entry is the
reason to stop and ask why.

**The LM/AME side is SEPARATE and STILL OPEN** (recorded 2026-08-02):
neither role has a crew record, so a free-text occupant name cannot be
checked against `dg_expiry` at all. Worth noting that this decision
makes that gap MATTER MORE, not less — if every flight is potentially
DG-carrying, then whoever actually handles the goods is the person
whose certification is untracked. Unchanged, and unchanged
deliberately, but no longer a gap that only applies to some flights.

### The positional-access rule, extended to `at.dataframe[`

From the same cancelled branch, and general: `st.dataframe()` carries
no `label=` to match on, so the only honest guard is COUNT rather than
position.

> A test may not index `at.dataframe[N]` unless the page renders
> exactly one dataframe by construction. Where more than one could
> exist, or where the count itself might change, assert the count
> first. The grep for the WIDGET version of this rule
> (`at.text_input[`, `at.button[`, `at.selectbox[`, `at.date_input[`,
> `at.time_input[`) now reads `at.dataframe[` too.

Not swept across the rest of the suite — recorded as the occasion that
found the gap, not as a claim the gap is closed everywhere.

### No reboot, and the check was RUN

Stated from the check, not from memory:

```
added imports in pages/          : (none)
new service attribute reads      : (none)
migrations                       : (none)
```

The only new name is `queue_cr_notice()`, defined in the page itself —
a page's own module-level function is re-created on every script run,
so no limb applies. **No reboot. No migration.**

Worth noting because the two branches merged on the same day differ:
`crew-change-revalidation` DID need one, on two limbs. The rule is easy
to remember and hard to apply, so the grep decides, not the memory of
what the last branch needed.

## 2026-09-05: the last un-audited page, a shared sort turned round, and a name a rule cannot reach

Four UI requests from the operator, plus the Schedule Templates
`st.rerun()` audit riding with them because the UI work touched that
file anyway. **One of the four is NOT built** — see "Replace crew and
the dropdown that cannot narrow" below, which is the finding rather
than the fix.

### Schedule Templates: a bulk approve that refused two drafts said nothing

The **last un-audited page** for the `st.rerun()` swallow (the running
list: Flt Schedule 2026-09-03, Roster Generation, Control Room, Crew
Data, Roster, and now this one — that closes the sweep). It had **five
discarding sites**, and two of them matter:

> "Approve selected" and "Reject selected" loop over the selection,
> collect every instance the service REFUSED, write
> `Instance 12: <why>` for each — and then call `st.rerun()`.

So a controller who selected five drafts and had two refused saw **no
error at all**. The list came back two rows shorter with the two
refusals still sitting in it, and nothing on screen said why. Same
shape as the swap alert on Control Room the same day: a *partial
failure* reported into a run that was already being thrown away, which
is worse than a total failure because the screen looks like it worked.

The other three (new version created, template deleted, template
created) were plain confirmations and are queued too.

**Scoped, not blanket.** "Select all visible" and "Clear selection"
also call `st.rerun()` and were **correct as they stood** — they write
no messages, they only stage `session_state` for the next run.
Queueing them would have been a change with no defect behind it.
`tests/test_schedule_template_notices.py` pins BOTH directions, so a
later change that starts writing a confirmation on those paths fails.

**Mutation-checked**: restoring `st.error(f"Instance {iid}: {msg}")` —
exactly what `main` did — fails
`test_a_refused_approval_says_which_one_and_why` and *only* that test.
Precise rather than broad, which is what says the test measures the
defect rather than the shape of the fix.

Worth noting about the pre-existing coverage: the DB-gated test named
`test_select_all_then_approve_promotes_instances_and_reports_flight_count`
**never asserted the message it is named after**. It could not — the
message was being discarded. A test title promising more than the test
checks is how a defect stays invisible while the suite stays green.

### `ORDER BY dep_time_planned` is now ASC

Operator request: flight lists read chronologically. This is **ONE
shared sort in `services/flight_service.py:get_all_flights()` with five
consumers**, so the direction was checked against each rather than
changed locally and hoped about:

| consumer | effect of ascending |
| --- | --- |
| `pages/1_Control_Room.py` day board | single day; a day reads earliest-first |
| `pages/3_Flight_Log.py` table + actuals selector | **the page's own comment already called newest-first the problem** |
| `pages/4_Roster.py` pair form | the form already re-sorts the SELECTION ascending before building the duty, so picker and duty now agree |
| `services/assistant/reports.py` × 2 | row ORDER changes, row CONTENT does not |

Nothing computes from a row's position: no caller takes a head, a tail
or an `.iloc[0]` off this frame. The Flt Schedule comment is worth
quoting because it was already arguing for this change:

> 103 options ordered newest-first is not something you can find a
> three-week-old flight in.

**The one place that depended on the direction was a test's fake read.**
`tests/test_flt_schedule_selector.py` sorted its fixture `ascending=
False` to reproduce the SQL, and
`test_the_oldest_flight_is_reachable_not_just_the_newest` was built
around newest-first. Rewritten as
`test_both_ends_of_the_list_are_reachable`, asserting both ends **by
position rather than by direction**, so the next sort change does not
require rewriting it.

**The direction itself is now measured where it lives.** No page-level
test can see an `ORDER BY`: their fake reads hand back a frame someone
already sorted, and would keep passing if the clause were deleted
outright. Two DB-gated tests in `tests/test_flight_service.py` insert
late/early/middle — so insertion order and `flight_id` order both
differ from departure order — and one of them repeats the check through
a status filter, because the `ORDER BY` is appended after the `WHERE`
is assembled and a filtered read is where a mis-built query drops it.

### A name no rule can reach

`CREW_DISPLAY_NAMES` in `services/display_labels.py`: a hardcoded table
keyed by `crew_id`, consulted BEFORE the mechanical rule.

The reason it is a table and not a better rule is that **there is no
rule**. CPT-03 is stored as `SYED FAHIM MAHMOOD` and is called
"Fahim". The mechanical rule renders `CPT S Mahmood` — correct,
unambiguous, and not what anybody on the frequency would recognise. No
amount of parsing gets from the stored string to "Fahim", because the
information is not in the string. The initial-plus-surname rule and the
`NAME_TITLES` stripping both **stay, as the fallback**, and still do
their job for anyone unlisted (`CAPT MUHAMMAD ASAD ALI` → `CPT M Ali`).

Design points, each of which is a test:

* **Keyed by `crew_id`, never by name.** Six of Air Eagle's ten pilots
  are stored as some form of "MUHAMMAD"; a name-keyed table would map
  six people onto one entry. `crew_id` is also the foreign key across
  roster and `audit_log`.
* **The value is the PERSON part only.** The grade is prefixed from the
  crew record, so a promotion changes the label without anyone editing
  `display_labels.py`.
* **Consulted before the missing-name branch.** A crew record with a
  blank name is exactly where knowing what people call this person is
  worth most; falling through to `CPT CPT-09` would waste the one
  source that still has the answer.
* **An unlisted crew member is not a bug.** They read exactly as they
  did before the table existed. That is what makes it safe to fill in
  one name at a time instead of needing to be complete before it is
  correct.
* **It does not leak into `crew_label()`.** That is the audit-facing
  label — staff id, `crew_id`, and the name as STORED — because a
  support conversation is about the record. The friendly name stops at
  the Roster table.

**SEEDED WITH ONE ENTRY, and that is a gap, not a design choice.**
Only `CPT-03` is filled in, because it is the only preferred name
anybody has actually stated. Producing a complete stub needs a
read-only `SELECT crew_id, name FROM crew` against the live database,
which was **blocked by the sandbox** rather than skipped. **The
operator has to supply the other nine names** — nobody else can, which
is the whole premise of the table.

#### Whether a `crew.display_name` column is the better home

Asked explicitly, answered honestly: **the column is the better home
eventually, and the code table is right today.**

For the column: OCC could fix a name through Crew Data without a
deploy, which is the real advantage and the only one that matters
long-term. It also travels with the record.

Against it now: ten pilots, names that change about never, and a wrong
entry that is cosmetic rather than operational. A migration plus a form
field plus a writer is a great deal of machinery for a dict with ten
lines. **It is checked that the column would NOT interact with
crew-change revalidation** — `LEGALITY_FIELDS_TIER1/2/3` are explicit
allowlists, so a `display_name` edit would score tier 0 and revalidate
nothing, which is correct. That is the one place a new crew column
could have caused real trouble, and it does not.

**The trigger to switch: the moment OCC wants to edit these
themselves, or the crew list grows past roughly thirty.**

### "Replace crew", and the dropdown that cannot narrow the way it was asked to

The rename is done and is **label only**: `pages/4_Roster.py`'s
subheader now reads "Replace crew". Confirmed unaffected, from the code
rather than from memory — the form still calls
`assignment_service.assign_pair_to_duty()`, which validates Commander
and Second Pilot **together and commits both or neither** (the
atomic-pair guarantee), and still runs the full legality check plus the
downstream swap-alert scan on every submission. Renaming a heading
cannot reach any of that.

**The dropdown scoping is NOT built, and the check is why.** The
request was to scope the pair form's flight list — which spans
20 Aug – 21 Sep — to what appears in "Current assignments". Two things
came out of looking first:

1. **It would strand every crew-TBC flight.** `grep` for
   `assign_pair_to_duty(` returns `pages/4_Roster.py` and the generator,
   and nothing else — **this form is the only UI path to crew an
   existing flight**. Control Room says so to the operator's face when
   a flight is saved without crew: *"Both cockpit seats will show as
   UNCOVERED until assigned in Roster."* "Current assignments"
   deliberately **excludes** flights with no flight-deck assignment
   (`if not seats: continue` — a flight in the list because somebody
   loaded cargo would be noise). So scoping the picker to that table
   removes exactly the flights that still need crewing, and leaves no
   way to crew them at all.

2. **It would not fix the date range either.** "Current assignments"
   itself iterates every flight and shows any that has crew — **it
   spans the same 20 Aug – 21 Sep**. Scoping to it removes uncrewed
   flights, not old ones. The stated symptom is a month of history in a
   picker; this would not touch it.

**Recommended instead: scope by time, not by crewed-ness** — future
and PLANNED, with the same "narrow it if you want to, it shows
everything by default" affordance Flt Schedule already uses. That drops
the past-flight noise, which is the actual complaint, and keeps every
crew-TBC flight reachable. **Not built pending the operator's
decision**, because the two readings produce materially different
screens and this one is theirs to make.

### Reboot: no crash risk, but a restart IS needed for any of it to show

Stated from the check, not from memory:

```
added imports in pages/          : (none)
new service attribute reads      : (none — the one grep hit is a comment)
migrations                       : (none)
```

None of the four reboot limbs applies, so **nothing can raise
`AttributeError` on a stale module**. `queue_st_notice()` is defined in
the page itself and a page's own module-level function is re-created on
every script run.

**But a restart is still required, for a different reason worth
separating.** Streamlit re-executes `pages/*.py` on every run and keeps
imported service modules in `sys.modules` for the life of the process.
The two behavioural changes here — the `ORDER BY` in
`services/flight_service.py` and the lookup in
`services/display_labels.py` — live in imported modules, so a running
process keeps the old sort and the old names until it is restarted. Not
a crash, not a limb; simply invisible. Recorded because "no reboot
required" has meant "nothing will break" on this project, and here the
honest answer is "nothing will break, and nothing will change either".

## 2026-09-06: the picker scoped by time, three swallows the first Roster audit missed, and a test that measured nothing

Second pass on `roster-ui-and-display-names`, after the operator
confirmed the #1 finding and supplied the display names.

### The pair form's picker: scoped by time, and by PLANNED

The operator's original request was to scope it to what appears in
"Current assignments". **Checked before building, and it was wrong on
both counts** — the operator agreed and asked for the alternative
instead:

1. **It would strand every crew-TBC flight.** `grep assign_pair_to_duty(`
   returns this page and the generator, nothing else, so **this form is
   the only UI path to crew an existing flight** — and Control Room
   says so to the controller's face: *"Both cockpit seats will show as
   UNCOVERED until assigned in Roster."* "Current assignments"
   deliberately excludes flights with no flight-deck crew
   (`if not seats: continue`). Scoping to it removes exactly the
   flights that need crewing.
2. **It would not have fixed the reported problem.** "Current
   assignments" lists every crewed flight regardless of date and spans
   the same 20 Aug – 21 Sep. It removes uncrewed flights, not old ones.

**Built instead: PLANNED, in a date window that defaults to today
onwards.**

The PLANNED half is **a correctness fix, not tidying**:
`assign_pair_to_duty()` does not check flight status at all, so the old
picker would happily crew a CANCELLED or already-OPERATED flight and
nothing downstream would object — and cancelling a flight cascades
CANCELLED to its roster rows, so crew assigned to a cancelled flight is
written and immediately meaningless.

**The window reaches backwards, and that is the part that keeps this
from repeating the mistake it replaced.** A PLANNED flight in the past
was never flown and never cancelled: it is uncrewed work, and there is
no other UI that can crew it. So:

* the default starts at today **only when there is something on or
  after today to start with** — a database whose only uncrewed work is
  overdue opens on those flights, not on an empty picker;
* widening the "Flights from" date reaches them in every other case;
* the captions say what the scope and the window leave out, because a
  picker that quietly drops flights is how *"not listing all flights"*
  gets reported a second time.

**Both forms share the scope.** The LM/ENGR picker had the identical
problem and takes the identical fix — two pickers onto one flight list
must not disagree about which flights exist.

### Three `st.rerun()` sites this page's FIRST audit missed

Found while editing the file for the scoping, and worth recording as a
miss rather than as a discovery: **the 2026-09-05 Roster audit
converted only the flagged-for-review section.** It left:

* the pair form's ALLOWED branch — success line, pair alerts and
  **swap alerts**;
* the LM/ENGR form's ALLOWED branch — success, legality status and its
  alert lines, and **swap alerts**;
* the unassign confirmation.

So the swap alert — *"this assignment breaks the legality of N
already-scheduled future duty(ies)"* — was found and fixed on Control
Room on 2026-09-05 **and left broken here**, on the page a controller
actually crews scheduled flights from. `queue_roster_notice()` gained a
`lines` parameter to carry the per-duty detail, matching Control
Room's.

The lesson is narrow and worth keeping: **a page is not "audited"
because one section of it was.** The grep to run is `st.rerun()` on the
page, every hit, not the memory of having looked at the file.

### THE IMPORTANT ONE: a presence assertion measures nothing here

The three notice test files asserted that a message EXISTS after the
click. **That passes whether or not the fix is present.**

`st.rerun()` runs the script twice inside one `at.run()`, and the
discarded first pass survives in the element tree **wherever the second
render is shorter** — which on these pages it is, because the form that
wrote the message collapses after a successful submit. So the
discarded message is still in `at.error` / `at.success`.

**Verified rather than reasoned about.** Mutating
`queue_roster_notice()` to write immediately — exactly the pre-fix
behaviour — left all thirteen Roster tests green. The same mutation
left `test_control_room_notices.py` and
`test_schedule_template_notices.py` green too, **including tests this
project had already reported as mutation-verified**. That earlier
evidence was a NARROWER mutation (one call site converted back to a
direct write), which does fail; a test that catches a one-line
regression but not a wholesale one is worth knowing about rather than
trusting.

**The fix is to assert POSITION, not presence.** A queued notice can
only render above the control that triggered it, because the drain runs
at the top of the page. A discarded one cannot. All three files now
collect the elements rendered before that control and assert on those,
and all three now FAIL under the general mutation:

```
tests/test_control_room_notices.py     3 failed
tests/test_roster_flight_scope.py      2 failed
tests/test_schedule_template_notices.py 3 failed
```

> **RULE.** A test for the `st.rerun()` swallow may not assert that a
> message merely exists. It must assert WHERE the message rendered —
> above the control that produced it. Presence-based assertions on
> these pages pass in both directions and are worse than no test,
> because they read as coverage.

One test moved further still: "the queue is popped, not read" is now
asserted on `session_state` (`"..._notices" not in at.session_state`)
rather than on the next render, because AppTest keeps stale elements
from an EARLIER `at.run()` too — so "the message is gone from the
screen" is not a question the element tree answers honestly, while "is
the queue empty" has one answer. Note `at.session_state.get(key)`
raises `KeyError` on a missing key rather than returning `None`; the
membership test is the one that works.

### The display names, all ten verified

`CREW_DISPLAY_NAMES` now carries nine entries. **Every `crew_id` was
verified against the `crew` table by read-only SELECT before being
committed** — the mapping came from an operator list, not from the
database, and a mis-keyed entry would label the WRONG PILOT on the
roster board, silently, because a plausible name in the wrong seat
looks exactly like a correct one. The stored name sits beside each
entry so the check is repeatable without a database.

What the table is for shows up in the list itself — the rule picks the
surname, the operator picks the given name people are known by, and
those disagree for six of the ten:

| crew_id | stored | rule gives | operator says |
| --- | --- | --- | --- |
| CPT-01 | MUHAMMAD WAQAR | CPT M Waqar | CPT Waqar |
| CPT-03 | SYED FAHIM MAHMOOD | CPT S Mahmood | **CPT Fahim** |
| CPT-04 | TAHIR MAHMOOD RAJA | CPT T Raja | **CPT Tahir** |
| CPT-05 | ADNAN SARWAR KHAN | CPT A Khan | **CPT Adnan** |
| CPT-06 | CAPT MUHAMMAD ASAD ALI | CPT M Ali | **CPT Asad** |
| FO-01 | IBTISAM MUZZAFAR | FO I Muzzafar | **FO Ibtisam** |
| FO-02 | MUHAMMAD WASIM | FO M Wasim | FO Wasim |
| FO-03 | MUHAMMAD SHAHBAZ | FO M Shahbaz | FO Shahbaz |
| FO-04 | MUHAMMAD SULEMAN AZIZ | FO M Aziz | **FO Suleman** |

**CPT-02 (MUHAMMAD SALEEM) IS DELIBERATELY ABSENT.** The operator
supplied nine names and his was not among them; "Saleem" was an
inference from the stored name. This table exists precisely because the
preferred name is NOT derivable from the stored one, so inferring one
entry would contradict the reason for the other nine. The database
confirms he is MUHAMMAD SALEEM; it cannot confirm what a controller
calls him. Until the operator says, he falls through to the rule and
reads `CPT M Saleem` — correct and unambiguous. **That is the fallback
doing its job, not a gap.**

**A test had to be re-pointed, and the reason generalises.**
`test_a_title_stored_inside_the_name_is_not_taken_for_a_given_name`
measured the `NAME_TITLES` stripping on CPT-06 ("CAPT MUHAMMAD ASAD
ALI"). CPT-06 is in the lookup now and never reaches the rule — so the
test would have kept passing while testing nothing. It now runs against
`CPT-99`, a fixture-only id carrying the same stored name and
deliberately absent from the table. **Adding real data to a lookup can
silently retire the coverage of the fallback beneath it.**

#### The `display_name` column: recorded trigger

Confirmed by the operator. The column is the better home **eventually**
— it would let OCC fix a name through Crew Data without a deploy — and
the code table is right today: ten pilots, names that change about
never, a wrong entry that is cosmetic rather than operational. Checked
that such a column would score **tier 0** against crew-change
revalidation (`LEGALITY_FIELDS_TIER1/2/3` are explicit allowlists), so
it could not accidentally flag duties.

> **TRIGGER TO SWITCH:** OCC wanting to edit these names themselves, or
> the crew list passing roughly thirty.

### Headings

"Replace crew" (the flight-deck pair) and **"Assign other occupants
(LM / ENGR / Other)"**. The two used to read as a contradiction — both
said *assign* — when they are different acts on different people: the
flight-deck pair is a SEAT that gets replaced, and an LM/ENGR is an
occupant added to a duty who holds no `operating_position` at all.

### Reboot: unchanged from the first pass, and stated from the check

```
added imports in pages/          : (none)
new service attribute reads      : (none)
migrations                       : (none)
```

No limb applies, so nothing can raise `AttributeError` on a stale
module. **A restart is still required for anything to CHANGE**, for the
separate reason recorded on 2026-09-05: the `ORDER BY` in
`flight_service` and the lookup in `display_labels` live in imported
service modules, which Streamlit keeps in `sys.modules` for the life of
the process. Nothing will break, and nothing will change either.

## 2026-09-05: two operator questions answered, and PLANNED-only enforced where it belongs

Both long-standing operator questions closed on the same day. One
needed no code; the other needed code in a different place from where
it looked like it belonged.

### DG: assume yes, and the current behaviour was already right

Settled in the 2026-09-05 rescue entry above (`cargo_dg` section).
Short version: **every Air Eagle flight potentially carries dangerous
goods**, so checking `dg_expiry` on every duty is correct, and
`flights.cargo_dg` is **recorded for reporting only and deliberately
not consulted by the legality gate**. No code change.

Worth restating the direction of that decision, because it is the
opposite of what the 2026-09-04 note assumed: the flag looked like a
check applied where it did not belong. The premise was wrong, not the
check.

### Only PLANNED flights may be crewed

Operator decision: **PLANNED only. Anything else, OCC handles outside
the system.**

`assignment_service.CREWABLE_FLIGHT_STATUSES` is the single place the
rule lives, and `_refuse_uncrewable_flights()` runs at the top of BOTH
write doors — `assign_crew_to_duty()` and `assign_pair_to_duty()` —
before the partner lookup, and before any audit row could be written.
The refusal names every offending leg and its status, because a duty is
a list of sectors and naming only the first would send a controller to
fix one leg and meet the same refusal again.

**Where it is NOT** is the more interesting half:

* **Not in `_validate_pair_internal()`**, which the write doors share
  with the read-only `validate_pair()`. That is what the swap-alert
  scan asks *"could this seat be filled by someone else"* through. A
  scan is a QUESTION, not an assignment, and making a question raise on
  a flight that has since been cancelled would turn a report into a
  crash.
* **Not in `assign_crew_to_new_flights()` / `assign_pair_to_new_flights()`**,
  which CREATE the flights they assign to. `flights.status` defaults to
  PLANNED (migrations/002), so there is nothing yet to refuse.
* **Not left to the picker.** `pages/4_Roster.py` stopped offering
  non-PLANNED flights on 2026-09-06 and that looked like the fix. It
  guarded one caller.

### THE GENERATOR IS NOT UNAFFECTED, and the reason is a real data shape

Checked rather than assumed, and the expected answer was wrong.

`rotation_template_service._promoted_flight_ids()` selects an approved
rotation's legs **by `rotation_instance_id`, with no status filter**.
So a leg cancelled after its rotation was approved is *still handed to
the generator*. Harmless while `assign_pair_to_duty()` accepted any
status; from this change it raises.

And the two generator call sites differ:

| call site | before this change | after, unguarded |
| --- | --- | --- |
| `accept_preview()` write path | already inside `try/except ValueError` | that rotation REJECTED with the reason — fine |
| `generate_preview()` candidate search | **no try/except**, called C x S times | **the whole window's preview dies** |

So the guard is also applied **once per rotation** in
`generate_preview()`, before the search loops: the rotation is marked
`OUTCOME_UNCOVERED` with a reason naming the leg and its status, and
the run continues. Caught there rather than by wrapping the loops
because the answer does not vary by candidate — and because *"this
rotation's flights cannot be crewed"* is a different fact from *"nobody
was legal for it"*, which is what wrapping the loop would have made it
look like.

The rotation stays IN the preview rather than disappearing from it, and
`accept_preview()` already records an UNCOVERED rotation's per-seat
reasons into `uncovered_seats`, so the explanation outlives the
session.

### What the existing data actually holds (READ-ONLY, nothing migrated)

Checked against production before shipping the rule, at the operator's
instruction to report rather than migrate:

```
flights:   99 PLANNED, 3 OPERATED, 1 CANCELLED
roster rows whose flight is not PLANNED:  4  (all OPERATED, none CANCELLED)
  2 x roster PLANNED   on flight 53 (EPE 786, dep 2026-09-04)
  2 x roster PROPOSED  on flight  9 (EPE 786, dep 2026-08-26)
approved rotations with a leg no longer PLANNED: 4
  instance 2  EPE-786-787     2026-08-21 -> flight  3 CANCELLED
  instance 5  EPE-786-787     2026-08-26 -> flight  9 OPERATED
  instance 20 EPE-786-787     2026-09-04 -> flight 53 OPERATED
  instance 33 EPE-802-804-805 2026-09-04 -> flight 50 OPERATED
```

**NONE OF THIS IS A VIOLATION, and saying so precisely matters more
than the count.** A crewed flight becomes OPERATED *after* it is
crewed: `update_flight()` sets OPERATED once both actual times are
recorded. Every one of those four roster rows is the ORDINARY
LIFECYCLE — crewed while PLANNED, flown, marked OPERATED. The new rule
governs when an assignment is MADE, not what the flight becomes
afterwards, and nothing here was written in a state the rule would have
refused.

**No roster row points at the CANCELLED flight.** That is the case that
would have been genuinely wrong, and it does not exist.

The two `PROPOSED` rows on flight 9 are the one loose end, and a benign
one: a generator proposal from 2026-08-26 that was never published, on
a flight that has since flown. Drafts, not assignments. Left alone.

The four approved rotations with a non-PLANNED leg are what the
generator will now mark UNCOVERED if that window is re-run — expected,
visible, and explained on screen rather than silent.

### Tests

`tests/test_planned_only_crewing.py` (14, DB-free) covers the rule, the
message, both write doors, the ordering (refusal before the partner
lookup), the silence about a missing flight, and the two places the
guard must NOT be — asserted structurally, because the alternative is a
DB-gated test that skips exactly where it would break. Mutation-checked:
removing both entry-point guards fails four of them.

`tests/test_roster_generator_service.py` gained one DB-GATED test —
two rotations, one leg cancelled after approval, asserting the broken
rotation comes back UNCOVERED with a reason **and the other is crewed
normally**. It skips where Postgres is absent, so it is unverified on
the development machine and needs the operator's run.

## 2026-09-05: one seat, one pilot — a latent hole, confirmed and closed

Flagged as an unverified lead in the operational-readiness assessment,
confirmed in ten minutes, and it was real.

### Nothing refused a second holder of the same seat

Not the database. `migrations/005`'s partial unique index is on
`(crew_id, flight_id, role_assigned)`, so two **different** Captains
both written as COMMANDER on one flight collide on nothing — different
`crew_id`, no conflict. The index prevents the same person twice, which
is a different question.

Not the service. `assign_pair_to_duty()` went from
`_validate_pair_internal()` — which checks crew existence, grade
eligibility and legality, and **never reads the roster at all** —
straight to `_write_pair_rows()`, which INSERTs unconditionally.
Verified by extracting both function bodies and listing every `SELECT`,
`FROM roster` and `raise ValueError` in them: the only refusals are
same-crew-twice, missing crew, wrong grade for the seat, and missing
flight.

**And it was reachable from the UI in one obvious move.** The Roster
pair form offers every PLANNED future flight, crewed ones included
(deliberately — scoping it to uncrewed flights would strand the
crew-TBC case, see 2026-09-06). So selecting an already-crewed flight
and assigning a different pair produced **two Commanders and two Second
Pilots**, silently, with no error and nothing flagged.

The form is labelled **"Replace crew"** as of 2026-09-06. That is
precisely the operation that was not implemented. It added.

**LATENT, NOT LIVE.** Checked against production read-only before
fixing: no flight had two active holders of one seat (24 COMMANDER
rows, 24 SECOND_PILOT rows, no duplicates at any status). Closed before
it was reached, which is the only good time to close something.

### Refused, not auto-replaced

`_refuse_occupied_seats()` names who holds the seat and sends the
controller to Unassign. It does NOT quietly cancel the incumbent, and
that is the design decision worth recording:

> Cancelling somebody's duty is a decision with its own reason field
> and its own audit row. `remove_assignment_from_duty()` already exists
> to make it, and it cancels EVERY sector of that person's duty — which
> a silent overwrite in the assignment path would not. An assignment
> path that quietly unassigns somebody is worse than one that refuses
> and says who is in the seat.

Both write doors are guarded — `assign_pair_to_duty()` for both seats,
`assign_crew_to_duty()` for the one it fills. `assign_crew_to_duty()`
had always confirmed the OTHER seat was taken (that is what
`_find_paired_pilot()` is for); nothing had ever confirmed the seat
being filled was free.

**PROPOSED counts as a holder; CANCELLED does not.** A generator
proposal is a real claim until somebody rejects it, and production
holds 24 PROPOSED rows today, so that is a live case rather than a
hypothetical. Cancelled rows must not block, or unassign-then-reassign
— the exact workflow the refusal sends people to — would be impossible.

### The cost had to be engineered, not just accepted

The first version opened its own connection per call. That broke twelve
tests, and the reason is the interesting part: the generator's
candidate search calls `assign_pair_to_duty()` **C x S times per
rotation**, so a naive seat read reintroduces the quadratic term
generation spent 2026-08-22 removing.

Two changes fixed it, and both are properties to preserve:

* **The read goes through `_read_duty_rows()`**, the one-line seam
  every other duty read uses. That seam is what the round-trip budget
  tests count — *a read that bypasses it is a read nothing can
  measure*, which is why the DB-free generator tests failed on the
  fake engine rather than on the query count.
* **`Prefetch` caches seat holders per `(duty, seat)`.** Correct as
  well as cheap: a preview writes nothing, so occupancy cannot change
  during one. Accept builds a FRESH `Prefetch` per rotation, so
  rotation N+1 sees rotation N's writes.

The candidate being trialled is excluded **in memory**, not in the
WHERE clause — otherwise the cache key would have to include the
candidate and would never hit during a search.

After both, the full suite passes with the round-trip budgets unchanged.

### Mutation-checked

Removing both guards fails **9 of the 12**. The three that stay green
are exactly the "must not be refused" cases — an empty seat, the same
pilot re-assigned to the seat they already hold, and a CANCELLED row
not blocking — which SHOULD pass in both directions, because they
assert the guard does not fire. A guard that is absent and a guard that
is correctly quiet look identical to those three, and that is the point
of having them.

## 2026-09-05: PLAN — backups (the trigger fired weeks ago). NOT BUILT.

**Operator instruction: plan it, do not build. Arif decides Pro versus
`supabase db dump` to his own storage, and the decision is his.**

### Why this is first

HANDOVER's own rule, written before any of this happened: *"the moment
real crew/flight data enters production — Pro plan, auth, and backups
all land in the same move. Not auth without backups."* Auth landed on
2026-08-16. Backups did not, on the judgement that shadow-trial data
was re-creatable.

**That judgement has expired.** Measured 2026-09-05, read-only:

```
audit_log      277 rows, every one attributed (occ1 261, occ3 6, import_script 10)
crew            10 records, 9 active
flights        103
roster          48 rows across 22 duties
rotation_instances  42 APPROVED, 14 DRAFT
uncovered_seats     10 open
```

The audit log is **the permanent regulatory record for a PCAA-regulated
operator**, and there is no copy of it anywhere. It is also the one
thing here that cannot be re-created: flights and rosters could be
rebuilt from the templates, but "who did what, when" cannot be
reconstructed from anything.

Two free-tier properties matter, and they are different problems:

* **No automated backups.** The exposure above.
* **The project pauses after 7 days of inactivity.** A paused project
  means the app is DOWN, not degraded. Less likely now that it is used
  daily, but it is the failure that looks like a total outage on a
  Monday morning after a quiet week.

### The decision, stated so it can be made in one sitting

| | **Supabase Pro** | **`supabase db dump` to own storage** |
| --- | --- | --- |
| cost | ~$25/mo | $0 plus wherever the files live |
| backups | daily, automatic, 7-day retention | whatever cadence is actually run |
| pausing | no longer pauses | still pauses after 7 idle days |
| restore | Supabase console, project offline for the duration | manual `psql` restore |
| survives project deletion | **no** — deleting the project destroys its backups | **yes** |
| ongoing effort | none | somebody has to run it, or automate it |

**They are not alternatives.** Pro fixes pausing and gives daily
backups nobody has to remember; the independent dump is the only thing
that survives the project itself being deleted, which is a real
recovery scenario. HANDOVER already recorded that conclusion in the
2026-08 research and it still holds. PITR was ruled out there too —
~$100/mo for 7-day retention, roughly 4x the Pro plan, and it REPLACES
daily backups rather than supplementing them.

**Recommendation, and it is a recommendation and not a decision:** Pro
for the pausing and the automatic dailies, plus a periodic
`supabase db dump` kept off Supabase entirely. If only one, take the
dump — it is free and it is the one that survives everything.

### What building it would actually involve

Deliberately concrete, so the estimate is not a guess:

1. **`scripts/backup_db.py`** — wraps `supabase db dump` (a single
   binary; no Postgres install needed, per the 2026-08 research),
   writes a timestamped `.sql` to a configured directory, prunes beyond
   N copies. Reads `DATABASE_URL` the way every other script does.
2. **A restore rehearsal, written down.** A backup nobody has restored
   is a hypothesis. Restore into a scratch database, run
   `run_migrations.py --status` and a row count against it, record what
   the commands actually were.
3. **Scheduling** — Windows Task Scheduler on the machine that already
   has the repo, or a GitHub Action with the connection string as a
   secret. The Action is tidier and puts a production credential in
   GitHub; the local task keeps the credential where it already is.
   **Operator call.**
4. **A HANDOVER entry recording where the backups live and how to
   restore**, because the person restoring will not be the person who
   set it up.

Roughly an afternoon for 1 and 2. Step 3 depends on the answer above.

### What NOT to do

Do not write a backup script that connects and `SELECT *`s each table
into CSV. It loses constraints, triggers (migrations/019 exists
precisely because of an immutability trigger), sequences, and the
`schema_migrations` table — and it would restore into something that
looks like the database and is not it.

---

## 2026-09-05: PLAN — the concurrency race. NOT BUILT, and deliberately.

**Operator instruction: plan, do not build. The fix touches the
legality write path, which is the highest-risk code here, and it
deserves a design conversation.**

### The failure shape

Verified by grep, 2026-09-05: **no `FOR UPDATE`, no `SERIALIZABLE`, no
advisory locks anywhere in `services/` or `db/`.** Every assignment is
read-then-write across two separate statements with nothing in between.

Concretely: controllers A and B both assign **the same pilot**, at the
same moment, to two duties that overlap or leave insufficient rest.

```
A: read the pilot's duty history      B: read the pilot's duty history
A: validate -> LEGAL                  B: validate -> LEGAL
A: INSERT                             B: INSERT
```

Neither read includes the other's write, because neither write had
happened yet. Both pass. The roster now holds two duties that are
individually legal and jointly illegal, **with no error, no alert and
nothing flagged for review.**

Three OCC accounts exist and at least two are active (occ1: 261 audit
rows, occ3: 6). This is not hypothetical for a three-controller team
working the same window.

**It is the worst failure shape in this system**: silent wrongness in
the one component whose entire job is to be right, and invisible
afterwards — the roster looks normal, and only a hand-check of FDP and
rest across duties would find it.

### Why it is not a quick fix

The natural instinct — wrap the validate-and-write in a transaction —
is not sufficient on its own. Postgres's default READ COMMITTED means
B's re-read inside a transaction still would not see A's uncommitted
insert. The options genuinely differ:

1. **Advisory lock per crew member.** `pg_advisory_xact_lock(hashtext(crew_id))`
   taken at the top of the write, released at commit. Serialises
   assignments for ONE pilot while leaving assignments for different
   pilots fully concurrent. Small blast radius, no isolation-level
   change, no retry logic. **My provisional preference.** The
   complication is that a pair assignment touches two pilots, so it
   needs both locks taken **in a fixed order** (sorted by crew_id) or
   two controllers assigning the same two pilots in opposite roles can
   deadlock.
2. **SERIALIZABLE for the assignment transaction.** Correct by
   construction, and it makes the database the authority rather than a
   convention anyone can forget. Costs: serialisation failures become
   a normal outcome, so every write path needs retry logic and every
   page needs to handle "try again" — and the generator's accept loop
   writes rotation by rotation, so it would need to retry per rotation.
3. **Re-validate inside the writing transaction.** Cheapest to reason
   about, and it does not actually close the window without one of the
   two above.

### What has to be decided before anything is written

* **Which pilot's history is the lock?** A pair assignment is two
  pilots. Locking both, in a deterministic order, or the deadlock is
  the new bug.
* **What does the generator do?** `accept_preview()` writes rotation by
  rotation and already catches `ValueError` per rotation. A lock wait
  there is fine; a serialisation failure needs a retry it does not have.
* **What does a controller see when they lose the race?** "Somebody
  else just assigned this pilot — re-check" is a real message that has
  to be written, queued through the notice mechanism (it reruns), and
  tested. A silent retry that succeeds is worse than the refusal,
  because the second controller believes they made the assignment they
  did not make.
* **How is it tested?** Two connections in one test, deliberately
  interleaved. The suite has no concurrency test today and no fixture
  shape for one. This is most of the work.

### The honest ranking

Lower than backups and lower than the expiry horizon, because it needs
two controllers acting on the same pilot within seconds of each other,
whereas the expiry gap has **already happened** and the backup gap is
one accident from being permanent. But it is above every remaining
nicety, and it should be built deliberately rather than slotted in
behind other work.

## 2026-09-05: both branches failed verification, and both failures were mine

Reported from real Postgres 16. Two different faults, both introduced by
me in the same session, and both are worth recording because neither was
visible on the machine that wrote them.

### 1. An em dash where a SQL comment marker belonged

`seat-occupancy-and-stale-handover`: **770 passed, 52 failed.**

```
LINE 4:   — operating_position added 2026-08-21, additi...
```

A blanket `" -- "` → `" — "` pass over `services/assignment_service.py`
— run to make the comments I had just written match house style —
rewrote **eleven lines inside a SQL string literal**:

```sql
SELECT r.roster_id, ..., r.debrief_time,
       — operating_position added 2026-08-21, additive: no
       — existing consumer selects columns by position.
       r.operating_position,
```

SQL comments are `--`. An em dash is not syntax, so
`get_roster_for_flight()` failed to parse on every call, and the 52
failures were five test files plus the four inherited from the base
branch — `roster_generator_service` (17), `roster_generation_page`
(11), `seat_occupancy` (9), `assistant_page` (6),
`assistant_reports` (5).

**THIS IS THE MOJIBAKE CLASS AGAIN**, in a new form: *a typographic
character standing where a syntactic one was needed, which reads
correctly and parses as nothing.* The previous instance was em dashes
inside regex alternations, which compiled fine and then matched nothing
forever. Both were introduced by a well-meaning pass that made prose
nicer **without asking what each occurrence WAS**.

The aggravating detail: earlier the same session I checked that this
file already held 32 `" -- "` on `main`, concluded a blanket replace
would "pollute the diff", and decided against it — then ran one anyway
two steps later. **The check was done and the conclusion was not
applied.**

Restored byte-identical to `main`; `git diff main` over that block is
empty.

> **RULE.** Never run a cosmetic character replace across a whole
> source file. A file contains Python, prose, SQL, regex and format
> strings, and `--`, `-`, `'` and `"` are syntax in some of them.
> Convert the lines you wrote, by anchor, or leave the file alone.

**`tests/test_sql_string_hygiene.py` is the thing that notices**, since
reading the diff did not. It parses every file under `services/`,
`core/`, `db/`, `pages/` and `scripts/` with `ast`, finds the string
literals that are SQL, strips each line's `-- comment` tail, and fails
on an em dash, en dash, minus sign, smart quote or non-breaking space
in what remains.

Three things it took to make that test honest, each a small lesson:

* **`ast`, not a regex.** "Is this line inside a string" is not a
  question a regex answers reliably.
* **A statement, not a word.** The first version asked whether
  `"UPDATE "` appeared anywhere and flagged four docstrings and a Crew
  Data caption ("Renewing a document? Update its expiry date"). Prose
  about the database is not SQL, and a hygiene test that cries wolf
  gets suppressed rather than fixed. It now requires a line that BEGINS
  a statement AND a clause keyword, and skips docstrings entirely.
* **Only what the database parses.** `get_roster_for_flight()`'s query
  has carried `-- role_assigned is NOT — under the pair model` since
  2026-08-21 and is perfectly valid: Postgres stops reading at the
  `--`. Flagging it would have demanded that correct code change. The
  test strips comment tails, so the em dash that broke everything is
  caught precisely because it was standing WHERE the `--` should have
  been — and the test pins BOTH directions with that exact pair of
  strings.

### 2. A "DB-free" test that reached `get_engine()`

`enforce-planned-only-crewing`: **806 passed, 4 failed**, all
`RuntimeError: DATABASE_URL not set` from
`services/assignment_service.py:1782` — `engine = get_engine()`, which
runs at the top of `assign_pair_to_duty()` BEFORE any guard.

They passed here because this machine's `.env` supplies the URL. **They
were not DB-free; they were .env-dependent** — which is a worse state
than a DB-gated test, because a DB-gated test skips honestly.

Same class as the round-trip guards of 2026-08-27, and the fix is the
net that already exists: `isolate_from_database()` from
`tests/test_generation_round_trips.py`, which replaces `get_engine` and
`log_audit` on EVERY service module — necessary because
`from db.db import get_engine` binds a COPY into each module's
namespace, so patching one module does nothing to another. The engine
it installs raises on any use, so a guard that lets something through
now fails loudly instead of opening a connection to production.

Wired into `tests/test_planned_only_crewing.py` and
`tests/test_seat_occupancy.py`. Both also needed `tests/` on
`sys.path`, the same two lines
`tests/test_partial_accept.py` and `tests/test_cross_rotation_legality.py`
already carry.

> **RULE.** A test that calls a service entry point is not DB-free
> because it patches the reads. Entry points take `engine =
> get_engine()` first, so the test is only DB-free once `get_engine`
> itself is isolated. **Verify it by running with `.env` moved aside**,
> not by observing that it passes.

That last line is the operational half: this file's tests now run with
`.env` temporarily renamed before being pushed, because "passes on my
machine" is exactly the state that produced both of these.

### What did NOT change

The #6 finding stands entirely. A form labelled "Replace crew" that
added a second Commander instead of replacing one, reachable in one
move, latent only because production happened to hold no duplicate. The
guard, its placement, the refuse-don't-overwrite decision and the
`_read_duty_rows()` routing are all unchanged — the SQL fault was in a
cosmetic pass over the same file, not in the fix.

## 2026-09-05: PLAN — crew non-availability. NOT BUILT.

**New operator requirement, not a deferred one.** Air Eagle needs to
record when a crew member is unavailable for flight duty — leave,
simulator, a course, sickness — as dated ranges with a reason, and the
roster must honour them.

**Plan only, no code, at the operator's instruction.**

### What already exists: nothing, and that was checked

* **No `crew_availability_service`, no `crew_unavailability` table, no
  stub, no deferred entry.** Grepped across `*.py`, `*.sql` and `*.md`.
* **`DutyType.AIRPORT_STANDBY` / `OTHER_STANDBY` exist in
  `core/legality/pcaa_ano012_core.py` and are deliberately unreached** —
  `duty_type` is hardcoded to FDP, and the full requirements document
  **CONFIRMED Air Eagle has no standby/reserve arrangement at all**
  (HANDOVER 2026-07). **Do not build unavailability as a duty type.**
  Leave is not a duty; modelling it as one would resurrect a concept
  the operator has explicitly said they do not have, and would push it
  through FDP/rest math that has nothing to say about it.
* **The vocabulary is already in OCC's head.** The Assistant's
  decision-question refusal list contains *"Tahir called in sick, who
  can replace him Thursday"* — a real phrasing, currently refused with
  a redirect to the Roster page.
* **One live hook worth knowing about.** The requirements document
  defines fairness as *"proportional to actual availability"*.
  `_seed_duty_counts()` currently counts duties per seat and has no
  availability input, so **that definition is not implementable
  today**. This data is the missing input. NOT scope now; recorded
  because it is the second thing this unlocks.

### 1. Where it lives

`crew_unavailability`: `id`, `crew_id` (FK), `date_from`, `date_to`,
`reason`, `note`, `created_by`, `created_at`. Three decisions inside
that shape:

**DATE, not TIMESTAMP — and the check compares against BOTH duty
dates.** This is the `_check_crew_qualifications()` lesson repeated:
that function checks the **debrief** date, not the report date,
because Air Eagle's real EPE 786/787 rotation reports 18:15 and
debriefs 00:00 the following day. Leave recorded 12–19 Sep against a
duty reporting 19 Sep and debriefing 20 Sep is a conflict, and a check
that looked only at report date, or only at debrief date, gets one of
those cases wrong. **Any overlap between [date_from, date_to] and
[report date, debrief date] is unavailable.** Both ends inclusive,
stated in the column comment, because "is date_to the last day off or
the first day back" is the ambiguity that produces a pilot rostered on
the last day of their own leave.

**Reason: a CONTROLLED LIST, plus a free-text `note`.** Free text
cannot drive behaviour, and behaviour is the point — the generator
needs to say "on leave" rather than "unavailable", and the question
"should any reason be softer" can only be asked of a closed set.
Controllers will still need to say more than a code ("course in Dubai,
back on the 20th"), so `note` is nullable and never parsed.

**Where the list is enforced is a real choice with precedent both
ways.** `crew.role` is deliberately NOT constrained at schema level
("tighten at the service layer, not schema"); `flights.status` IS a
CHECK constraint, because "disrupted-duty counting already checks
status == 'DISRUPTED' literally". The rule those two imply: **constrain
at the schema when code branches on the value; constrain at the service
when it does not.** Under the recommendation in §3 no reason is
softer, so nothing branches — **service-layer allowlist**, which means
adding "compassionate leave" later is not a migration. If a soft type
is ever introduced, that is the trigger to move it to a CHECK
constraint.

**Overlaps: ALLOWED.** Overlapping records are how reality arrives —
sickness extending into booked annual leave, a course inside a leave
block. Refusing overlap forces a controller to edit history in order
to record the present. The overlap query is `EXISTS ... WHERE ranges
intersect`, which is indifferent to how many rows match, so permitting
them costs nothing. **Refuse only `date_to < date_from`**, and an exact
duplicate of an existing row.

**Migration number is a decision, not a detail.** HANDOVER currently
reserves **"MIGRATION 021"** by name for dropping the `PROPOSED`
roster status — a migration that does not exist and cannot be written
until the 24 legacy PROPOSED rows are resolved. Taking 021 for
unavailability means amending that reservation in the same change.
Leaving a numbering gap is worse. **Recommend: unavailability takes
021, and the PROPOSED drop is renamed to "the next free number" in its
own entry** — a reserved number that names the wrong thing is exactly
the confusion the numbering rule exists to prevent, and this file has
already been stale twice about migrations.

### 2. How it reaches the gate — the SSOT question

**The anchor is `_check_crew_qualifications()`, which is called from
five places** (`assignment_service.py` lines 1103, 2317, 2361, 2651,
2895 — the immediate gate, two candidate-search paths, the delay
recompute, and revalidation tier 1). Those five are today's single
authority for "may this person fly this duty".

**It cannot host the new check.** Its signature is
`(crew_row, duty_date)` — a row and a date, no engine, no prefetch.
That is the same limitation already recorded for `cargo_dg`: it "could
not consult a flight even in principle". Availability needs a database
read.

**So: a sibling, and one wrapper both go through.**

```
_check_crew_availability(engine, crew_id, report_date, debrief_date,
                         prefetch=None) -> List[RuleAlert]

_check_crew_is_usable(...)  ->  qualifications + availability
```

Every one of the five sites calls the **wrapper**, never either half.
That is what makes it one authority rather than three checks that can
disagree — **a sixth call site added next year gets both or neither**,
which is the property the picker-versus-gate lesson (2026-09-05) says
to design for rather than to remember.

Two integration details that will bite if not planned:

* **Round-trip cost, and this is the seat-guard lesson arriving
  early.** The generator calls the gate **C x S times per rotation**.
  An uncached read per call reintroduces the quadratic term removed on
  2026-08-22. The read must go through **`_read_duty_rows()`** — the
  seam the round-trip budget tests actually count, *a read that
  bypasses it is a read nothing can measure* — and cache on
  `Prefetch`, keyed by `crew_id`, exactly as `seat_holders` now does.
* **`alert_summary` buckets by rule prefix.** It splits qualification
  alerts out by `QUALIFICATION_RULE_PREFIX = "AE-CREW-QUAL-001"`, and
  routes any alert with `duty_id is None` that is NOT a qualification
  alert into `schedule_level_alerts`. A new `AE-CREW-AVAIL-001` alert
  would land in the wrong bucket and be reported as a whole-schedule
  pattern. Either it carries a `duty_id`, or the prefix handling
  becomes a tuple. **Decide before writing the alert, not after seeing
  it in the wrong place.**

### 3. What it produces: ILLEGAL, and the override is a record, not a click

**ILLEGAL, not a warning.** The distinction that makes it clear-cut:
an expiring qualification is a fact about a document and an FDP breach
is a computation, but **leave is a recorded human decision**. There is
nothing for the system to weigh.

**No reason type should be softer, and the reason is about
auditability rather than strictness.** A WARNING that can be clicked
through is exactly the affordance a controller wants when somebody's
leave is cancelled by agreement — and it leaves no record that the
agreement happened. **Shortening or deleting the unavailability record
is the override**, and it is audited, attributable and visible to the
next person. A soft reason type would give the same flexibility with
none of the trail.

### 4. Revalidation: reuse the existing door

Recording leave for a pilot who already holds future duties **is** the
crew-change scenario from 2026-09-05, and it should flag those duties
the same way.

**It can reuse `revalidate_crew_duties()`, and the change that makes it
reuse cleanly is the same change §2 already requires.** That function
takes `changed_fields` and computes `_tier_for()`; unavailability is
not a crew field, so it needs a sentinel — add `"unavailability"` to
`LEGALITY_FIELDS_TIER1` and call
`revalidate_crew_duties(crew_id, {"unavailability"}, app_user)`.

Tier 1 currently asks `_check_crew_qualifications()` directly
(line 2895). **Point it at the wrapper instead** and reuse falls out
for free — no second revalidation path, no chance of the two drifting.

Three properties carry over unchanged and should be stated so nobody
"fixes" them:

* **Future PLANNED duties only.** Sickness is often recorded
  retroactively; a duty already flown must not be re-flagged.
* **NEVER CLEARS.** Deleting or shortening an unavailability record
  must NOT auto-clear the flags it raised — the flag records that
  nobody has looked, not that the data is currently bad.
  `clear_duty_review_flag()` stays the only exit.
* **Every affected duty is flagged, no cap.**

### 5. The generator — and the obvious approach is wrong

**"Exclude them from the candidate pool" cannot be done at the pool.**
`pools` is built ONCE per run, before the rotation loop
(`roster_generator_service.py:591`), from all crew filtered by grade.
**Unavailability is date-shaped**: a pilot on leave 12–19 Sep is a
perfectly good candidate on the 20th, and a run spanning both would
exclude them from every rotation or none.

**Exclusion happens PER ROTATION**, against that rotation's
`reference_date`, in the two places candidates are built: the
fresh-pair search (~line 740) and the one-seat-remaining branch
(~line 683).

**And the exclusion must carry its own reason, or it destroys the
message it was meant to produce.** An excluded candidate is never
trialled, so it contributes no rejection reason, and
`summarize_rejected_trials()` would report "no legal pair found" —
with the single most useful explanation missing. That is the swap-alert
swallow in a new place. `_record_uncovered()` already takes a free-text
`reason` and needs no schema change, so:

> `CPT-04 on leave 12–19 Sep; CPT-06 on leave 12–14 Sep — no
> Commander available`

is reachable, and is worth far more to a controller than a rest
failure.

### 6. UI

**A new section on Crew Data, not a new page.** It is crew data, and
OCC will record it while looking at the crew record. But it is
**date-shaped and list-shaped**, so it does not belong in the edit
form: select crew → date range → reason → optional note → Add, plus a
table of current and future records with a Remove control. Crew Data
is 302 lines today with two forms; this is a third section, not a
rewrite.

**It must also be visible where it bites.** A controller who picks a
pilot on leave in the Roster form and only learns at submit has been
told late. Minimum: the refusal names the dates and the reason. Better:
mark them in the picker label — `CPT Tahir (on leave to 19 Sep)`.

**And it belongs in the front-door summary** proposed on 2026-09-05
(the expiry horizon and what-is-uncrewed piece). "Who lapses in 30
days", "what is not crewed" and "who is away this week" are the same
question asked three ways, and they belong on the same screen.

### Sequencing, and what it costs

1. Migration + service + the wrapper (§1, §2) — the SSOT, and nothing
   works without it.
2. Revalidation wiring (§4) — small, once §2 is done.
3. Generator exclusion + reason (§5).
4. UI (§6).

**Reboot required: two limbs** — a new service module, and a new
import in `pages/2_Crew_Data.py`. Plus a migration. Stated in advance
from the rule, to be confirmed by running the grep at merge time
rather than recalled.

**Tests: DB-free through `isolate_from_database()`, verified with
`.env` moved aside** — the correction of 2026-09-05, applied from the
start rather than after a failed verification.
