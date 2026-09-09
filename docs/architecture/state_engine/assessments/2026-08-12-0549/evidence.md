# Task 6 Retained Evidence

## Results

- `EV-TASK6-SQLITE`: 6/6 passed in 3.18 seconds; SQLite 3.47.1 WAL,
  single-process leader; no warnings, skips, xfails, or xpasses.

The separate local PostgreSQL 16.13 cohort passed 16/16 focused checks in
14.97 seconds. It is implementation support only: the catalog binds
PostgreSQL to the maintained AWS deployment, and a local testcontainer cannot
promote that combined profile.

The exact argv, checkout-local `PYTHONPATH`, node index, JUnit, profile report,
stdout, stderr, hashes, and promoted tuples are recorded in
[assessment.json](assessment.json).

## Promotion boundary

Promoted cells cover selected RC-01/02/04 coordination
success/refusal/rollback cells and the SQLite initial-leader process boundary.
A passing node is support-only unless its exact identity appears in a coverage
tuple.

No SQLite follower node is relabelled as single-process evidence. Local
PostgreSQL does not promote live AWS composition. RC-05 remains catalogued
not-applicable for both retained profiles. AUX-02, AUX-06, AUX-07, RC-06,
RC-07, read-model, maintenance, and uncited crash/restart cells remain unknown.

## Validation

Run:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  collect-evidence docs/architecture/state_engine/assessments/2026-08-12-0549/assessment.json
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  validate-package docs/architecture/state_engine/assessments/2026-08-12-0549/assessment.json
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  check-links
```
