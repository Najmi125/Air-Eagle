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
Phase 5 complete (this snapshot). Phase 6 (Assignment + legality
gate) is next, pending confirmation.

## Files changed
Phase 5: core/duty_builder.py (new), services/flight_service.py
(new), app.py (updated — nav to Flight Log), pages/3_Flight_Log.py
(new), tests/test_duty_builder.py (new),
tests/test_flight_service.py (new), tests/test_flight_log_page.py
(new), HANDOVER.md.

## DB changes (migrations applied)
- 000_migration_tracking.sql (schema_migrations tracking table)
- 001_crew_table.sql (crew — matches the 19-column operator template
  plus operator_staff_id; no hardcoded base default)
- 002_flights_table.sql (flights — flight_no nullable for ad-hoc ops,
  CHECK-constrained status, CHECK on arr > dep)
- 003_roster_table.sql (roster — one row per crew per flight sector,
  duty_id NOT NULL, FKs to crew/flights, UNIQUE on
  crew_id+flight_id+role_assigned, CHECK on debrief > report)
- 004_audit_log.sql (audit_log — single unified table, all action
  types, per Section 16's required field list)
- No new migrations in Phase 5 — duty_builder.py is pure logic, no
  schema of its own; flight_service.py uses the flights table
  already built in Phase 3.

## Tests passed
89/89 — tests/test_migrations.py (4), tests/test_duty_summary.py
(10), tests/test_pcaa_ano012_core.py (12), tests/test_schema.py (11),
tests/test_audit_service.py (3), tests/test_crew_service.py (17),
tests/test_crew_data_page.py (4), tests/test_duty_builder.py (10),
tests/test_flight_service.py (13), tests/test_flight_log_page.py (5).
Against real Postgres 16 (local test instance; production Air Eagle
DB not yet provisioned).

## Open stubs / known blockers
- `core/legality/pcaa_ano012_core.py`, `core/duty_summary.py`, and
  now `core/duty_builder.py` are all correctly flagged by
  `scripts/check_reachability.py` — fully built and tested, but
  Flight Log doesn't call them (adding a flight isn't the same as
  building a duty or assigning crew to it). Expected to clear in
  Phase 6 (assignment + legality gate), which is what will actually
  tie a flight to a crew member through a duty.
- The `crew` table schema (001_crew_table.sql) is built against the
  19-column template, not yet against real operator data. When
  Monday's data comes back: check it actually matches this shape
  before writing a new migration to add anything — don't assume the
  template survived contact with a real spreadsheet unchanged.
- `crew.role` is deliberately NOT validated against a fixed list at
  either the schema or service layer (the template explicitly allows
  "Other"). pages/2_Crew_Data.py's dropdown offers CPT/FO/LM/ENGR/
  Other with a free-text field for Other. If this proves too loose
  once real data arrives, tighten at the service layer, not schema.
- Waiting on: real crew data from operator (was expected Monday
  2026-07-20, via AirEagle_Crew_Data_Simple.xlsx) — pages/2_Crew_Data.py
  is now ready to receive it.
- Waiting on: Air Eagle's actual route network — blocks duty
  template design and the 28-day roster generator's real content
  (the generator ENGINE can still be built schedule-agnostic in the
  meantime, per phase plan). Roster generator will use Google
  OR-Tools CP-SAT for the assignment optimization (decided
  2026-07-19) — add `ortools` to requirements.txt when Phase 7 starts.
- Air Eagle's domestic-only vs domestic+international route mix was
  never confirmed either — core/duty_builder.py's build_duty()
  requires an explicit domestic=True/False rather than guessing,
  specifically because this was never answered. Whoever calls
  build_duty() in Phase 6 needs this decided, or needs it as a
  per-flight input from Flight Log (arguably better — a mixed
  ad-hoc+scheduled cargo operator may fly both).
- RESOLVED 2026-07-19: D21 (charter rest) confirmed as the
  applicable rule for Air Eagle's cargo ops. D20 (home/away base)
  code path still exists in the ported engine for a future
  scheduled-carrier client but is not currently exercised by Air
  Eagle's confirmed operation_type="cargo_charter" default.
- "Engr" role definition unconfirmed (flight-deck FE vs
  line-maintenance AME) — flagged on the crew data template,
  answer expected with Monday's data. Also affects whether
  001_crew_table.sql needs AME/LM-specific columns added later.
- Auth (require_login/require_permission) is NOT wired anywhere yet
  — neither page has any access control right now. Needs a real
  decision on when to build this — not urgent while only synthetic
  test data exists, genuinely urgent before any real operator data
  goes in permanently.
- Supabase: DATABASE_URL is saved locally (2026-07-19) but
  dependencies (`pip install -r requirements.txt`) hadn't been
  installed in that venv as of the last update, so migrations were
  not yet confirmed applied against the real Supabase DB. Also
  flagged and then explicitly deferred by the user ("tackle Supabase
  later") — the GitHub-integration collision risk (Supabase's native
  migration deploy expects a supabase/migrations/ folder we don't
  use) was explained but not yet confirmed resolved one way or the
  other. Check status before assuming this is settled.

## Next safest step
Phase 6: Assignment + legality gate. services/assignment_service.py
— ties a crew member to a flight through a duty, calling
core/duty_builder.py to compute the duty window and
core/legality/pcaa_ano012_core.py to validate it before writing to
the roster table. This is also where the confirmed live bug from the
old repo (validate_single_assignment called without its route
params) needs to be built correctly from the start, not retrofitted
— there's no old buggy call site here to fix, just don't recreate
the missing-params version of it.

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
