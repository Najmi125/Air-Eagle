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
- duty_builder.py's hardcoded XYZ-specific DUTY_TEMPLATES — will be
  rebuilt schedule-agnostic/config-driven from the start
- crew_position.py / replacement_options.py (location tracking) —
  deliberately not included. All Air Eagle crew are KHI-based;
  nothing currently indicates away-from-base overnight layovers.
  If the route network (pending, see below) shows real layovers,
  build this properly against that actual pattern — don't
  speculatively rebuild it now.

## Current active task
Phase 2 complete (this snapshot). Phase 3 (schema: crew/flights/
roster tables) is next, pending confirmation.

## Files changed
Phase 2: core/legality/pcaa_ano012_core.py (new),
core/duty_summary.py (new), scripts/check_reachability.py (refined
— tests/ no longer counts toward reachability), HANDOVER.md.

## DB changes (migrations applied)
- 000_migration_tracking.sql (schema_migrations tracking table only)
- No business schema yet (crew/flights/roster tables are Phase 3)

## Tests passed
26/26 — tests/test_migrations.py (4), tests/test_duty_summary.py
(10), tests/test_pcaa_ano012_core.py (12). Against real Postgres 16
(local test instance; production Air Eagle DB not yet provisioned).

## Open stubs / known blockers
- `core/legality/pcaa_ano012_core.py` and `core/duty_summary.py` are
  correctly flagged by `scripts/check_reachability.py` — fully
  ported and tested (26/26 passing), but not yet called from any
  service or page. Expected: services/ (Phase 3+) is what will
  actually call these. Not a bug, matches db.py's existing note
  below for the same reason.
- `db/db.py` is currently flagged by `scripts/check_reachability.py`
  — correctly. Nothing imports it yet because `app.py` doesn't exist
  yet (Phase 1 deliberately stopped before pages/app.py). This is
  expected, not a bug; it'll clear once Phase 4/5 wires up the first
  page. Noting it here is the whole point of the checker existing.
- Waiting on: real crew data from operator (was expected Monday
  2026-07-20, via AirEagle_Crew_Data_Simple.xlsx)
- Waiting on: Air Eagle's actual route network — blocks duty
  template design and the 28-day roster generator's real content
  (the generator ENGINE can still be built schedule-agnostic in the
  meantime, per phase plan). Roster generator will use Google
  OR-Tools CP-SAT for the assignment optimization (decided
  2026-07-19) — add `ortools` to requirements.txt when Phase 7 starts.
- RESOLVED 2026-07-19: D21 (charter rest) confirmed as the
  applicable rule for Air Eagle's cargo ops. D20 (home/away base)
  code path still exists in the ported engine for a future
  scheduled-carrier client but is not currently exercised by Air
  Eagle's confirmed operation_type="cargo_charter" default.
- "Engr" role definition unconfirmed (flight-deck FE vs
  line-maintenance AME) — flagged on the crew data template,
  answer expected with Monday's data

## Next safest step
Phase 3: schema. crew/flights/roster tables as numbered migrations
(002_, 003_, ...), building on 000_migration_tracking.sql. Crew
table should match the 19-column simple template already sent to
the operator (ID, Name, Role, DOB, Nationality, Base, Ph No, Email,
License No, License Exp, Medical Exp, Type Rating Exp, LPC/OPC Exp,
Line Check Exp, SEP Exp, CRM Exp, DG Exp, Contract Exp, Remarks) —
confirm against whatever comes back Monday before finalizing column
types.

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
