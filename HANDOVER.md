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
Today's work (schema reconciliation, FTL exemption, import script,
review remediation planning) is verified locally but UNPUSHED as of
this snapshot — see "Next safest step" for the agreed remediation
order once pushed.

## Files changed
Since Phase 6's push: migrations/007_crew_columns_reconcile_real_data.sql
(new), services/assignment_service.py (FTL_EXEMPT_ROLES),
services/crew_service.py (UPDATABLE_FIELDS for renamed/new columns),
pages/2_Crew_Data.py (SIM/Route Check/IR fields), scripts/
import_crew_from_xlsx.py (new), tests/test_schema.py (+2),
tests/test_assignment_service.py (+7), tests/test_import_crew_script.py
(new), HANDOVER.md.

## DB changes (migrations applied)
- 000_migration_tracking.sql (schema_migrations tracking table)
- 001_crew_table.sql (crew — matches the 19-column operator template
  plus operator_staff_id; no hardcoded base default)
- 002_flights_table.sql (flights — flight_no nullable for ad-hoc ops,
  CHECK-constrained status, CHECK on arr > dep)
- 003_roster_table.sql (roster — one row per crew per flight sector,
  duty_id NOT NULL, FKs to crew/flights, CHECK on debrief > report)
- 004_audit_log.sql (audit_log — single unified table, all action
  types, per Section 16's required field list)
- 005_roster_partial_unique_index.sql (replaces roster's UNIQUE
  constraint with a partial index — non-cancelled rows only — so
  unassign-then-reassign of the same crew/flight/role works)
- 006_flights_domestic_column.sql (flights.domestic, NOT NULL, no
  default — required at flight-creation time, never guessed)
- 007_crew_columns_reconcile_real_data.sql (renames lpc_opc_expiry ->
  sim_expiry, line_check_expiry -> route_check_expiry to match the
  operator's actual terminology; adds ir_expiry)
- ALL SEVEN confirmed applied only against local sandbox Postgres.
  NONE have been run against the real Supabase DB — see the
  2026-07-21 entry above. This needs to actually happen before any
  of this is real.

## Tests passed
140/140 — tests/test_migrations.py (4), tests/test_duty_summary.py
(10), tests/test_pcaa_ano012_core.py (12), tests/test_schema.py (16),
tests/test_audit_service.py (3), tests/test_crew_service.py (17),
tests/test_crew_data_page.py (4), tests/test_duty_builder.py (10),
tests/test_flight_service.py (15), tests/test_flight_log_page.py (5),
tests/test_assignment_service.py (27), tests/test_control_room_page.py
(4), tests/test_roster_page.py (4), tests/test_import_crew_script.py
(11). Against real Postgres 16, local sandbox instance — independently
re-confirmed via fresh clone for every phase through Phase 6; today's
work verified locally but not yet pushed/re-confirmed as of this
snapshot.

## Open stubs / known blockers
- `core/duty_summary.py` is the only file still flagged by
  `scripts/check_reachability.py`. Correctly so — it's for cumulative-
  hours reporting/dashboards, not the assignment flow (which uses
  pcaa_ano012_core.py's validator directly on Duty objects, a
  different code path). No page uses it yet. Not urgent; would matter
  for a future crew-profile or compliance-dashboard page.
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
- Auth (require_login/require_permission) is NOT wired anywhere yet
  — none of the four pages have any access control right now. Needs
  a real decision on when to build this — not urgent while only
  synthetic test data exists, genuinely urgent before any real
  operator data goes in permanently. This gap has now persisted
  across 6 phases; worth deciding deliberately rather than by default
  much longer.
- Supabase: DATABASE_URL is saved locally (2026-07-19) but
  dependencies (`pip install -r requirements.txt`) hadn't been
  installed in that venv as of the last update, so migrations were
  not yet confirmed applied against the real Supabase DB. Explicitly
  deferred by the user ("tackle Supabase later"). The GitHub-
  integration collision risk (Supabase's native migration deploy
  expects a supabase/migrations/ folder we don't use) was explained
  but not yet confirmed resolved one way or the other. Check status
  before assuming this is settled — two new migrations (005, 006)
  have been added since this was last touched, so there's more to
  apply than there was when it was set aside.

## Next safest step
NOT Phase 7. Explicitly paused per the external review's core
recommendation (agreed): an OR-Tools generator on top of a legality
gate with known holes would efficiently produce a roster that looks
legal and isn't. Agreed remediation order (2026-07-21), each step
tested and pushed independently before the next, same discipline as
every phase so far:

1. Push today's already-verified-locally work (schema reconciliation,
   FTL exemption, import script) — done, tested, just needs pushing.
2. Fix `NEEDS_MANUAL_REVIEW` being silently treated as ALLOWED in
   assignment_service.py (only `AlertStatus.ILLEGAL` currently blocks
   a save — NEEDS_MANUAL_REVIEW must hold for review, not auto-pass).
   Smallest, clearest, fully decoupled from everything else below.
3. Fix the domestic/geographic-continuity design: `domestic` is
   currently over-constrained as "every flight in a duty must have
   the identical value," which would reject the real International
   pair (KHI-LHE-DWC-KHI) the moment it's actually entered — that
   pair legitimately mixes domestic and international-classified
   sectors within one duty. duty_builder.build_duty() already takes
   domestic as its own parameter; the fix is likely to stop deriving
   it from the flights and let the caller (Roster/Control Room)
   specify it for the duty as a whole. Also add the missing
   geographic-continuity check (leg N's destination must equal leg
   N+1's origin — currently only temporal ordering is checked).
4. The qualification gate: `_crew_member()` currently passes only
   crew_id/name/home_base into the legality check — role match,
   is_active, license/medical/SIM/route-check validity against the
   duty date are not checked at all during assignment. This is the
   single biggest gap found — a deactivated captain could currently
   be assigned through the service API today.
5. Three related "stale data" findings, likely fixed together:
   LOOKBACK_DAYS=35 starves the engine's own 365-day/1000h cumulative
   check (D9.2.3) of data it needs — that rule has never once been
   able to fire correctly; flight_service.update_flight() doesn't
   recompute FDP or revalidate crew when actual times change (the
   docstring already flags this gap, nothing closes it yet); cancel_
   flight() doesn't cancel/exclude the associated roster rows from
   future legality history.
6. True transactional atomicity for Control Room's flight+assignment
   write — currently 4 separate `engine.begin()` blocks, not one
   transaction, so the "no orphan flight on rejection" guarantee
   (tested and true today) doesn't extend to "no orphan flight if the
   process crashes mid-sequence."
7. Age-65 rule, once the user has the exact wording from the
   licensing ANO (not yet received as of this snapshot — do not
   implement from general ICAO knowledge, the exact rule shape is
   unconfirmed).
8. THEN revisit Phase 7.

Lower-priority findings from the same review, not yet sequenced:
airport timezones not passed to the validator (every sector defaults
to UTC+5 regardless of actual destination — now directly relevant
given DWC is in the real route network); standby/reserve/positioning
duty types never reach the legality engine (duty_type is hardcoded
to FDP always); configs/airlines/AEAGLE/ still empty; audit records
have no real app_user or transaction_id (ties to both the no-auth
gap and the non-atomic-transaction gap); test depth for
pcaa_ano012_core.py is thin relative to its size and safety-criticality
(12 tests for 1,293 lines, deliberately scoped rather than
exhaustive — boundary-value tests, one minute under/at/over each
limit, would be a real improvement whenever this file gets touched
again).

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
- **"Eng: 2x VAI" in the real flight examples — meaning not yet
  confirmed.** Don't assume what VAI means; ask before building
  anything around it.
- **Age-65 rule flagged as IMPORTANT by the user, NOT yet
  implemented.** "At least 01 crew member below 65 yrs... applicable
  to EPE." Checked the actual ANO-012 document (OCR'd the scanned
  PDF, searched for "age"/"65"/"60" across all 32 pages) — confirmed
  this document (titled "FATIGUE MANAGEMENT — FLIGHT AND CABIN CREW")
  contains NO age-eligibility provision at all; this is a licensing
  restriction, not an FTL/fatigue one, and lives in a different PCAA
  order this repo doesn't have. User is getting the exact wording
  from the licensing ANO before this gets built — do NOT implement
  from general ICAO knowledge in the meantime, the exact rule
  (simple 65 cutoff vs. a 60-65 sub-band rule) is still unconfirmed.

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
