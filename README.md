# Air Eagle OCC — FTLguard

Airline Operations Control Centre platform for Air Eagle (B737 cargo,
ad-hoc + scheduled flights). Built on FTLguard's platform architecture
— see the FTLguard Project Instructions for the SSOT rules and
ownership model this repo implements.

## This is a fresh repo (2026-07-19)

Rebuilt from scratch after a critical assessment of the previous repo
(K2) found real structural damage — three incompatible schema
definitions for one table, a dead legality engine still sitting in
the canonical validator file, a confirmed live bug in the assignment
flow, and ~17 files never wired into the app. Root cause was a
missing verification layer, not any single bug. See `HANDOVER.md` for
the full context and what did/didn't carry over.

## Structure

```
core/                  Deterministic logic. No DB writes, no side effects.
  legality/
    pcaa_ano012_core.py   The ANO-012 rule engine. AI explains, this decides.
  duty_builder.py         Canonical FDP calculator (report->debrief).
  duty_summary.py         Canonical duty-level dedup for cumulative hours.

services/              All writes go through here. Every write validates
                        first, audits after.
  flight_service.py
  assignment_service.py
  crew_availability_service.py
  audit_service.py

configs/
  airlines/AEAGLE/        Air Eagle's own values. Nothing airline-specific
                           belongs anywhere else in this repo.
  rule_packs/              Regulatory limits, loaded at startup, passed in
                           — never hardcoded in core/.

migrations/             Numbered .sql files. Only sanctioned way schema
                        changes happen. See scripts/run_migrations.py.

pages/                  Streamlit pages. Collect input, call services,
                        display output. No legality logic, no direct SQL.

tests/                  pytest. conftest.py provides an isolated, disposable
                        Postgres fixture for anything that needs real DB
                        behavior. Pure logic (core/) doesn't need it.

scripts/
  run_migrations.py       Apply pending migrations, tracked, idempotent.
  check_reachability.py   Flags any core/services/db file nothing imports.
```

## Setup

```bash
python -m venv venv
source venv/Scripts/activate   # or venv/bin/activate on Mac/Linux
pip install -r requirements.txt

cp .env.example .env
# fill in DATABASE_URL and TEST_DATABASE_URL

python scripts/run_migrations.py --status
python scripts/run_migrations.py

pytest
python scripts/check_reachability.py

streamlit run app.py
```

## Before ending any session that touched core/, services/, or db/

```bash
python scripts/check_reachability.py
pytest
```

Then update `HANDOVER.md`. Both steps are cheap and both would have
caught real problems that shipped in the previous repo.
