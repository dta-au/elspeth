# Q1 implementation report — secret wiring policy on proposal acceptance

Status: DONE_WITH_CONCERNS — implementation and bounded verification are complete and uncommitted in
`/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`, based on
`6825994b482bc274df2ae85d8738b9078f0117b4`. Independent Astra review approved the
patch without findings (`task-1-review.md`). The focused gate completed with
pytest exit 0: 567 passed, 3 skipped, 1 xfailed. Concerns are the expected-failure
authority inventory check described below and the parent-owned integration/lint
limits. No further worktree edits are pending.

## Changes

- `src/elspeth/web/sessions/routes/composer/proposals.py`: import
  `runtime_secret_wiring_policy` and pass the policy constructed from
  `request.app.state.settings.secret_wiring_allowlist` into the real
  `execute_tool` accept replay. Production diff is two added lines. Policy
  comes only from server settings; tool/model arguments cannot supply it.
- `tests/unit/web/sessions/routes/composer/test_proposal_accept_secret_wiring.py`:
  six HTTP cases use the existing app/proposal/state factories and real dispatch,
  authorization, authored-state admission, preflight, and session persistence.
  The exact tuple accepts with 200, advances the graph version, persists the new
  secret marker, records committed proposal status and committed state identity,
  and produces a valid state after real runtime preflight. Empty policy and
  mismatches on secret, component type, plugin, and option key each return 422,
  settle the proposal as rejected, leave committed state identity absent, and
  preserve the exact prior graph record and version history.

The fixture uses the supported `azure_blob` sink's `sas_token` option. It seeds
an existing reference marker and replaces it with another reference to make the
mutation observable without authored credential values. The old graph is not
claimed execution-ready under an empty or mismatching deployment policy. The
accepted candidate runs real preflight and must persist `is_valid=True`. Only
the backing secret store is a specced double; the scoped resolver remains real.
Synthetic in-memory values are generated during the test and are never returned
or reported. No provider request or pipeline execution is needed.

Legacy proposal accept remains terminal: a second accept returns the existing
409 and leaves the committed state and two-version history unchanged. An initial
extra test assertion assumed a repeated legacy accept returned 200; observing the
existing 409 contract corrected that assertion, without changing production
replay behavior. Pipeline receipt-backed settlement was not changed.

## Commands and evidence

All commands ran from the worktree. Python commands used `.venv/bin/python`.
The common environment/pytest arguments were:

```bash
cd /workspace/elspeth/.claude/worktrees/composer-remediation-wave1
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
# pytest commands below include:
# -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src"
```

Provenance command:

```bash
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)'
```

Exit 0, both paths point into this task worktree:

```text
/workspace/elspeth/.claude/worktrees/composer-remediation-wave1/src/elspeth/__init__.py
/workspace/elspeth/.claude/worktrees/composer-remediation-wave1/elspeth-lints/src/elspeth_lints/__init__.py
```

Logs are under `/workspace/elspeth/.claude/lanes/composer-remediation-wave1/`.

| Run | Command after common setup | Exit and result | Log |
| --- | --- | --- | --- |
| Closest baseline | `.venv/bin/python -m pytest -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" tests/unit/web/sessions/test_routes.py::test_accept_proposal_executes_tool_and_commits_state` | 0; 1 passed | `task-1-baseline.log` |
| Pre-fix red / negative control | `.venv/bin/python -m pytest -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" tests/unit/web/sessions/routes/composer/test_proposal_accept_secret_wiring.py` | 1; exact-authorized case fails because route returns 422 `Secret wiring denied`; 5 unauthorized cases pass | `task-1-red.log` |
| Fixed regression | same new-module pytest command | 0; 6 passed | `task-1-green.log` |
| Existing acceptance | `.venv/bin/python -m pytest -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" tests/unit/web/sessions/test_routes.py -k 'accept_proposal or accept_failed or accept_schema_stale'` | 0; 7 passed, 223 deselected | `task-1-accept.log` |
| Contracts | `.venv/bin/python -m scripts.check_contracts` | 0; census and all contract checks pass | `task-1-contracts.log` |
| Scoped mypy | `.venv/bin/python -m mypy src/elspeth/web/sessions/routes/composer/proposals.py` | 0; no issues in 1 source file; existing unused-config-section note | `task-1-mypy.log` |
| Ruff | `.venv/bin/python -m ruff check src/elspeth/web/sessions/routes/composer/proposals.py tests/unit/web/sessions/routes/composer/test_proposal_accept_secret_wiring.py` | 0; all checks passed | `task-1-ruff-check.log` |
| Ruff format | `.venv/bin/python -m ruff format --check src/elspeth/web/sessions/routes/composer/proposals.py tests/unit/web/sessions/routes/composer/test_proposal_accept_secret_wiring.py` | 0; 2 files already formatted | `task-1-ruff-format.log` |

The pre-fix red run is the policy-kwarg removal control: it exercised the same
HTTP/real-dispatch path before the only production fix, and denied the exact
authorized tuple. No mock kwarg assertion stands in for authorization behavior.

The additional focused test command is:

```bash
.venv/bin/python -m pytest -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" \
  tests/unit/web/sessions/routes/composer/test_proposal_accept_secret_wiring.py \
  tests/unit/web/sessions/routes/composer/test_proposal_accept_preflight_failure.py \
  tests/unit/web/composer/test_secret_wiring_collector.py \
  tests/unit/web/secrets/test_wiring_policy.py \
  tests/unit/web/test_sessions_composer_attribute_contracts.py \
  tests/unit/architecture/test_session_db_mutation_authority.py \
  tests/unit/elspeth_lints/test_masquerade_gate.py \
  tests/unit/test_mock_discipline_baseline.py
```

Completed result: **pytest exit 0; 567 passed, 3 skipped, 1 xfailed in 782.78s
(0:13:02)**. Log: `task-1-focused.log`. Terminal output from unified exec session
55933 explicitly printed `exit=0`; this was retrieved by the implementation
agent after parent polling found that session IDs are agent-scoped. No suite was
rerun. Parent may now unfreeze the worktree.

The three skipped cases are in `test_masquerade_gate.py:1521`: PEP 696 defaults
require Python 3.13; this run used Python 3.12. The expected failure is
`tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities`,
with reason `Sessions mutation authority inventory drift`. Its detailed inventory
is in the complete log. This is not blanket clearance of all production writer
authority inventories; the check remains an expected failure. The other focused
cases completed under the existing suite policy.

## Limits and review

- Parent owns consolidated broad testing, PostgreSQL capacity, independent
  review, and the all-rule lint before/after corpus comparison. A duplicate
  task-local all-rule scan was terminated at parent request (exit 143) and is not evidence;
  use the parent's authoritative `lints-before.json` and final delta.
- No full pytest or PostgreSQL suite was launched by this task. No SQL, schema,
  lock, or persistence implementation changed.
- No `@trust_boundary` / `@observation_boundary` declaration, decorated function,
  or existing referenced test changed; boundary fingerprint gates are not
  triggered by this patch.
- Self-review: exact deny-by-default matching remains unchanged; no new settings,
  arguments, schemas, error envelope, or D1 staging policy. No credential values,
  suppressions, hand-edited pins, or test-only production code were introduced.
- New tests pin every behavior introduced or restored here. They do not
  independently re-prove unchanged provider authorship, execution acknowledgement,
  custody, review reconciliation, multi-process session fencing, or pipeline
  receipt-backed settlement. Existing subsystem suites remain the authority for
  those behaviors; this report makes no new exhaustive-coverage claim for them.
- `git diff --check` exited 0. The worktree remains detached on the specified
  base and changes are uncommitted.
