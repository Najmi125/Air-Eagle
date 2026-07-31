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
Steps 2–4 of the agreed remediation order are done and merged into
`main` (NEEDS_MANUAL_REVIEW gate, domestic/geographic-continuity fix,
crew qualification gate — AE-CREW-QUAL-001). On top of that,
`type_rating_expiry`/`contract_expiry` have just been removed
entirely — from the qualification gate AND the crew schema itself
(migrations/008) — see the 2026-08-01 log entry below for why. This
latest removal is on branch `remove-type-rating-contract-fields`,
**not yet merged, not yet verified against any real database** — only
collection/non-DB tests confirmed locally as of this snapshot. Needs
a real `pytest` run (and, separately, migration 008 actually applied)
before this is considered safe to merge. See "Next safest step" for
what's queued next once this lands (item 5: the three stale-data
findings, or item 7: the age-pairing rule).

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
- **008_drop_type_rating_and_contract_expiry.sql — written but NOT
  YET APPLIED anywhere as of this snapshot**, not even local sandbox
  Postgres. No database was reachable in the environment this
  migration was written in (no TEST_DATABASE_URL, no local Postgres,
  no Docker) — so unlike every other migration in this file, this one
  has not been run at all, not against sandbox, not against Supabase.
  Drops both columns entirely; see the 2026-08-01 log entry below.
  Run `python scripts/run_migrations.py --status` (against a real
  test DB first, then Supabase) before assuming this applies cleanly.

## Tests passed
178 total — tests/test_migrations.py (4), tests/test_duty_summary.py
(10), tests/test_pcaa_ano012_core.py (12), tests/test_schema.py (16),
tests/test_audit_service.py (3), tests/test_crew_service.py (21),
tests/test_crew_data_page.py (4), tests/test_duty_builder.py (12),
tests/test_flight_service.py (15), tests/test_flight_log_page.py (5),
tests/test_assignment_service.py (51), tests/test_control_room_page.py
(5), tests/test_roster_page.py (4), tests/test_import_crew_script.py
(12), tests/test_env_override.py (4).

177/177 independently verified against real Postgres 16 (2026-07-31,
`qualification-gate` branch, commit `45252da`) — this covers the
crew-qualification gate as it stood then (8 fields, no
type_rating_expiry) plus the debrief-date and AME-synonym-test fixes
that verification run required. Everything since — the
type_rating_expiry addition (`b5d9c05`) AND its removal, plus the
type_rating_expiry/contract_expiry schema drop, on branch
`remove-type-rating-contract-fields` — has only been checked for
collection/non-DB-test correctness in an environment with no reachable
database (no TEST_DATABASE_URL, no local Postgres, no Docker). The net
change in test count is zero (one test removed, one added), but that
is not the same as re-verification — flagging this explicitly rather
than assuming a DB-dependent change passes just because collection
succeeds.

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
7. **Age-pairing rule — now well-specified, still not built.**
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
8. THEN revisit Phase 7.

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
  real row imported to date, so no data loss of consequence. **Not
  yet applied anywhere** — see "DB changes" above.
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

**Verification status — read before merging**: built and reasoned
through in an environment with no reachable database at all (no
TEST_DATABASE_URL, no local Postgres, no Docker). Collection and all
non-DB tests pass locally; migration 008 has not been run anywhere,
sandbox or Supabase. Built on branch
`remove-type-rating-contract-fields`, not yet merged. Needs: (1) a
real `pytest` run against a disposable test Postgres, (2) migration
008 actually applied (sandbox first, then Supabase, same sequence as
every prior migration), before this is safe to merge and treat as
done.
