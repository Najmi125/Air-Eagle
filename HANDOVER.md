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

**Merge status as of this snapshot (2026-08-02)** — this paragraph,
not the individual dated log entries below, is the single place to
check what's actually landed. A dated log entry describes what was
built and why, and that doesn't go stale; a "MERGED"/"NOT MERGED" note
buried inside one does, once several branches are in flight from
different points in history. Keep merge status here only, going
forward.

- **Merged into `main` — no outstanding branches as of this snapshot
  (2026-08-04).** Most recent: `rotation-instance-approval-workflow` —
  DRAFT -> APPROVED promotion, the piece that makes a template actually
  produce operational flights (see the dedicated log entry below).
  382/382 verified against real Postgres 16, including a real fix found
  on that first pass (a per-file test fixture omitting `audit_service`,
  closed by consolidating all five service modules' `get_engine()`
  patching into one shared `tests/conftest.py` fixture — the second
  time a fixture gap masked real behavior, so the class was closed, not
  just the instance). Adds this repo's second database-level uniqueness
  guarantee beyond migrations/011's `EXCLUDE` constraint — a partial
  unique index (migrations/012) against double-promotion. Before that:
  `rotation-templates-phase7-groundwork` (the recurring-schedule-
  template layer, 365/365, `btree_gist` confirmed available on Supabase),
  Step 7 (age-pairing, AE-CREW-PAIR-AGE-001), and Step 6 (transactional
  atomicity) — see prior entries below for full detail on each.

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
- `scripts/check_reachability.py` currently flags two files:
  `services/assistant/reports.py` (as of the `assistant-report-
  functions` branch, 2026-08-01 — nothing calls `run_report()` yet,
  there's no assistant UI page) and `services/rotation_template_service.py`
  (as of `rotation-templates-phase7-groundwork`, 2026-08-04 — nothing
  calls it either, there's no template-management UI, and the generator
  that will eventually be its real caller isn't built). Both correctly
  so — built ahead of being wired into a page, same reason in both
  cases. `core/duty_summary.py` and `core/rotation_expansion.py` are
  NOT flagged: `reports.py`'s `utilization()` and
  `rotation_template_service.py`'s `expand_and_persist()` are each
  other's first real caller.
- **`find_legal_candidates_for_duty()` does not check the age-pairing
  rule (AE-CREW-PAIR-AGE-001, Step 7, 2026-08-02).** This function
  powers the downstream-swap candidate suggestions shown when an
  assignment breaks a future duty's legality — it currently only
  checks FTL/rest and qualification legality for each candidate, not
  whether swapping them in would create an illegal flight-deck age
  pairing with whoever's already on the other seat of that future
  duty. Suggesting a replacement who'd create an illegal pairing is a
  real gap, deliberately not fixed as part of Step 7 (kept scoped to
  the direct assignment gate itself) — worth a deliberate decision
  later, not a silent oversight.
- `query_parser.py`'s `parse()` never actually populates
  `ReportRequest.status_filter`, even though "cancelled"/"delayed"/
  "diverted" are scoring keywords for the `flight_records` template.
  A question like "which flights were cancelled in June" correctly
  routes to `flight_records` but currently returns ALL flights in
  June, not just cancelled ones, because `request.status_filter` stays
  `None`. `reports.flight_records()` already passes `request.status_filter`
  through to `flight_service.get_all_flights()`, so this fixes itself
  the moment the parser starts setting it — a small, separate parser
  enhancement, not touched as part of the report functions themselves.
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
- **Auth — spec now SETTLED (2026-08-04), still NOT built.** Three
  accounts, all full access, no permission tiers — the app is not
  publicly reachable, so a CONTROLLER/ADMIN split isn't buying
  anything real right now. Session-level login is acceptable (re-login
  on a hard refresh is fine for three OCC staff) — no cookie-
  persistence mechanism needed. This resolves the two open questions
  the 2026-08-02 plan had been waiting on; see that entry for the
  fuller design (self-contained `users` table + password hashing,
  `require_login()` at the top of each page).
  **The important half of this isn't restriction, it's attribution**:
  `audit_log.app_user` is `NULL` on every row today, because no page
  passes it through — the audit trail currently records WHAT happened
  and WHEN, but never WHO. For a PCAA-regulated operator, that's a
  real deficiency in what's supposed to be the permanent regulatory
  record, not a cosmetic gap. When this gets built, every service
  write must carry the logged-in user's identity through to
  `log_audit()` — no service-layer signature changes needed, every
  write function already accepts `app_user: Optional[str] = None`,
  it's just never populated by any page today.
  **Trigger to actually build this — shared with the Supabase item
  below**: the moment any real crew or flight data enters the
  production Supabase database. Not before.
- **Supabase stays on the free tier — deliberate, not an oversight,
  same trigger as auth above.** No automated backups, and the project
  pauses after 7 days of inactivity — both accepted as long as the
  database holds nothing real. DATABASE_URL is saved locally
  (2026-07-19) but migrations were not yet confirmed applied against
  the real Supabase DB as of that check — re-verify before assuming
  that's settled, since migrations have been added since. The GitHub-
  integration collision risk (Supabase's native migration deploy
  expects a `supabase/migrations/` folder this repo doesn't use) was
  explained but not yet confirmed resolved one way or the other.
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

**Fields checked** (9): license_expiry, medical_expiry,
type_rating_expiry, sim_expiry, route_check_expiry, ir_expiry,
sep_expiry, crm_expiry, dg_expiry — plus `is_active`.
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
