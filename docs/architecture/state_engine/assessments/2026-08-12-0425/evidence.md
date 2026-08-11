# Executed Evidence

Evidence was captured in the task worktree at test commit
`9f78d2b2ae58cd93d8fafc51bf77c3fef65ed5ba` with Python 3.13.1, pytest 9.0.3,
SQLite 3.47.1, and SQLAlchemy 2.0.45. No full test suite was run.

## Reporter-bound focused cohort

`EV-TASK5-SQLITE` ran these exact file selectors with `-q -n 0` and the trusted
state-engine profile reporter:

- `tests/unit/core/landscape/test_scheduler_queue_contract.py`;
- `tests/unit/core/landscape/test_scheduler_events.py`;
- `tests/unit/core/landscape/test_scheduler_fencing.py`;
- `tests/e2e/recovery/test_source_ingress_contract.py`;
- `tests/e2e/recovery/test_concurrent_resume.py`;
- `tests/integration/pipeline/test_scheduler_plugin_dispositions.py`.

Result: **113 passed, zero failed/errors/skips/xfails/xpasses/warnings, exit 0,
36.447 seconds**. The runtime probe observed SQLite 3.47.1 through the actual
`sqlite3.Connection` used by the queue contract and bound all 113 exact JUnit
node identities to `sqlite-wal-single-process-leader`.

## Promoted cells

Every listed cell is `leg-contract` under
`sqlite-wal-single-process-leader`; no other profile is implied.

| Leg | Promoted dimensions |
| --- | --- |
| TS-00 | zero-mutation rollback |
| TS-01 | zero-mutation rollback |
| TS-02 | production entry |
| F-11 | guard refusal |

The assessment manifest maps each cell to exact test node IDs. A node is used
for only one proof subject, while it may support several dimensions of that
same leg/profile/case subject as permitted by the v2 evidence contract.

## Retained artifacts

| Kind | File | SHA-256 |
| --- | --- | --- |
| JUnit XML | `evidence/EV-TASK5-SQLITE.junit.xml` | `64f347fd99dc3283d2d148af7c1b81db5a3e32b2b3940c859b284da870aed9eb` |
| Exact node index | `evidence/EV-TASK5-SQLITE.nodes` | `f5d091c5219ebb80be25e22fed3ebea1424148d1a31914e2aaa33b214f010ab5` |
| Profile report | `evidence/EV-TASK5-SQLITE.profile.json` | `17677fe359410f65c12dbb146b143d2afd51713305cc60b59efc1849b752d1e2` |
| Standard output | `evidence/EV-TASK5-SQLITE.stdout` | `0362a2e7c5c2c077697905664b1fe4fc0e4843446edf030cb92cb75da75c47ad` |
| Standard error | `evidence/EV-TASK5-SQLITE.stderr` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |

## What this does not establish

- PostgreSQL 16 AWS behavior or either SQLite follower deployment;
- concurrency, crash/restart, maintenance, or read-model truth-table cells;
- PB-01, PB-02, or PB-03 generic cells; their selected tests do not cover
  every outcome arm required by the unsplit `leg-contract` case;
- uncited dimensions, legs, plugins, or provider-backed acceptance;
- completion of any all-required-v2 leg;
- a full-suite or merge-readiness result.

The separate exact masquerade whole-tree gate passed one test in 75.02 seconds.
It is verification of the test-tree constraint, not reporter-bound behavioral
evidence, so it is not attached to any proof cell.

## Package checks

From the repository root:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  validate-package docs/architecture/state_engine/assessments/2026-08-12-0425/assessment.json
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  collect-evidence docs/architecture/state_engine/assessments/2026-08-12-0425/assessment.json
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py check-links
git diff --check
```
