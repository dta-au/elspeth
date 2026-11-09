# D1 legacy acceptance and terminal feedback

Status: DONE_WITH_CONCERNS. The implementation is frozen for parent review; canonical settlement and proposal staging remain explicitly OPEN in their separate tasks. No commit, push, schema, SQL, lock-authority, or immediate-auto-commit change was made.

Worktree: `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`

Base: `5e192342c3fb025fd9a91244bebe437158e9de8e`

Review artifact: `/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d1-legacy-review.diff` (includes the new test file, excludes dependencies/logs).

Freeze fingerprints: `d1-legacy-freeze.txt` in this lane. All logs named below are under `<worktree>/.claude/lanes/composer-remediation-wave1/`.

## Changes

- Legacy `execute_tool` replay catches only `ToolArgumentError`, drains a terminal rejection, and returns structured 422 `proposal_argument_invalid`. Diagnosis uses the exception's canonical `args[0]`, without serializing its cause. The actor is `system:argument-invalid:user:{id}`.
- After existing required-control/review finalization, a successful ordinary result with the same `composition_content_hash` terminalizes with 422 `proposal_no_change`, actor `system:no-change:user:{id}`. This catches unchanged versions and identical edits that increment versions. It bypasses state-data preparation/runtime preflight/state writes.
- Both new terminal refusals share a narrow rejection helper. A competing terminal transition preserves its event and returns truthful already-terminal wording; a missing loaded row, integrity fault, or generic unexpected error escapes. Existing validation-failure handling remains unchanged.
- The new refusal path retains dispatch/finalization/rejection cancellations through the existing deferred-cancellation carrier. Existing lease-drain behavior is retained. Cancellation cannot release the compose lock/lease while required rejection is in flight.
- Frontend refreshes proposals for both new types and existing `proposal_validation_failed`, retaining validation details only for the validation subtype. Session identity and publication generation guard publication; refresh failures preserve refusal detail and the previous list.

## Verification commands and exits

For every Python test command below, from the specified worktree root, the prefix was:

```sh
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest -n0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src"
```

Every command wrote its own log; its process exit was read explicitly. Imports resolve into the active worktree (`d1-provenance.log`), including `elspeth_lints`. No broad/full suite or all-rule lint corpus scan was run; those remain parent-owned.

| Check | Selection / command | Exit and result | Log |
| --- | --- | --- | --- |
| Existing baseline | Four files listed in focused regressions, `-k 'accept or reject'` | 0; 45 passed, 204 deselected | `d1-baseline.log` |
| Backend final baseline red | New test file, `-k 'content_identity or real_malformed or legacy_create_blob or unexpected_dispatch'`, in separate baseline worktree | 1; 4 failed, 3 passed, 21 deselected | `d1-red-baseline-backend.log` |
| Frontend final baseline red | `npm test -- src/stores/sessionStore.test.ts -t 'terminal proposals\|refusal detail\|does not publish delayed\|transient accept'`, in separate baseline worktree frontend | 1; 8 failed, 2 passed, 180 skipped | `d1-red-baseline-frontend.log` |
| New backend final green | `tests/unit/web/sessions/routes/composer/test_proposal_accept_terminal_refusal.py` | 0; 28 passed | `d1-green-backend.log` |
| Focused regressions | `test_proposal_accept_secret_wiring.py`, `test_proposal_accept_preflight_failure.py` under `tests/unit/web/sessions/routes/composer/`; `tests/unit/web/sessions/test_blob_proposal_accept.py`; `tests/unit/web/sessions/test_routes.py`; `-k 'accept or reject'` | 0; 45 passed, 204 deselected | `d1-regressions.log` |
| Frontend final green | Frontend cwd: `npm test -- src/stores/sessionStore.test.ts` (includes dependency tests) | 0; 190 store tests passed | `d1-green-frontend.log` |
| TypeScript | Frontend cwd: `npm run typecheck` | 0; app and OIDC TypeScript checks | `d1-typecheck.log` |
| Frontend lint | Frontend cwd: `./node_modules/.bin/eslint src/stores/sessionStore.ts src/stores/sessionStore.test.ts` | 0 | `d1-eslint.log` |
| Python lint | `.venv/bin/ruff check src/elspeth/web/sessions/routes/composer/proposals.py tests/unit/web/sessions/routes/composer/test_proposal_accept_terminal_refusal.py` | 0 | `d1-ruff.log` |
| Python format | `.venv/bin/ruff format --check` with those same two paths | 0 | `d1-format.log` |
| Scoped mypy | Both source roots in PYTHONPATH; `.venv/bin/python -m mypy src/elspeth/web/sessions/routes/composer/proposals.py` | 0; no issues in one source file | `d1-mypy.log` |
| Contracts | Both source roots in PYTHONPATH; `.venv/bin/python -m scripts.check_contracts` | 0; census matches | `d1-contracts.log` |
| Focused tree gates | `tests/unit/web/test_sessions_composer_attribute_contracts.py tests/unit/architecture/test_session_db_mutation_authority.py tests/unit/test_mock_discipline_baseline.py` | 0; 306 passed, 1 xfailed (existing authority burn-down diagnostic) | `d1-focused-gates.log` |
| Diff whitespace | `git diff --check` | 0 | `d1-diff-check.log` |

The baseline-red tree is `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1-d1-red`, detached at the same base. It contains only copied test changes and dependency symlinks. Both baseline Python imports were printed and resolve there. Active production files were never rolled back while checks ran. The final red failures are the intended same-version no-op (409), empty/same-value metadata edits (200), and real filename `ToolArgumentError` escape; the changed counterpart, generic unexpected error, and unsupported legacy-create boundary pass on the original production inputs.

The original malformed test setup lacked originating-message provenance and failed before reaching argument admission. That fixture was corrected to a real originating message and complete provider authorship before the final baseline-red/green runs. An initial frontend run lacked dependencies and exited 1 before collection; a non-staged `node_modules` symlink to the existing main-worktree dependency installation fixed setup. Current npm uses Vitest 4.1.11 from that installation. No package/lockfile changed. Both worktree dependency symlinks are excluded from the review diff.

## Route and event coverage

The 28 new cases use the real HTTP route, dispatcher, supported plugins, SQLite persistence, and existing fenced test authorities. No runtime-preflight result is faked.

- Four content-identity variants seed a real CSV/JSON pipeline through actual acceptance/preflight: successful same-version `get_pipeline_state`, empty metadata patch, identical-value metadata edit with version advance, and a real changed edit. Assertions cover response type, dispatch result/version, preflight call count, current state identity, database history cardinality, terminal actor/event count, second accept without redispatch, and pending-list reload.
- Real malformed `create_blob` filename arguments exercise the real handler's typed error, safe diagnosis, no leaked raw content/cause text, terminal rejection, one event, no state, and no second dispatch. Complete composer provenance is retained.
- A generic dispatch `ValueError` remains the exact original exception, pending with no new event.
- Three competing-terminal cases persist real decisions under the actual request fence: committed no-change winner and rejected winners for both refusal subtypes. The winning event/actor/audit binding is unchanged, response wording is truthful, and repeat accept is 409 without a new event. A committed malformed-blob winner is not fabricated: that tool has no supported proposal receipt (see boundary below).
- Eight repeated-cancellation cases cover both refusal types at dispatch, before rejection, after durable rejection, and during lease close. Rejection and cleanup drain before completion, terminal event cardinality remains one, retry does not dispatch, and dispatch/rejection cancellations preserve the actual captured exception identity/marker. The existing lease-close cancellation path exposes a cancellation after draining; this slice does not change its synthetic-cancellation identity semantics.
- Four cases combine cancellation during rejection, cancellation during cleanup, a named original `KeyError`/`AuditIntegrityError`, and a cleanup `RuntimeError`. The original rejection exception survives by identity, and cleanup failure/cancellation is recorded in its notes.
- Six named writer-fault controls (`KeyError`, `AuditIntegrityError`, `ValueError` for both refusal types) escape by identity, retaining pending status and no terminal event. These inject the named error at the real service writer seam after an actual authoritative load; they do not delete rows or fabricate owned records.

Existing blob acceptance tests cover real update/delete effects, applied receipt replay, retry, and rejection/cancellation behavior. Existing secret-wiring tests retain server policy enforcement and existing preflight tests retain structured transient 500/pending/retry behavior. Existing route accept/reject cases and frontend stale/transient controls remain green.

## Compatibility and unresolved boundaries

New client-visible compatibility changes are limited to legacy replay: successful content-identical proposals now return terminal 422 `proposal_no_change` instead of pending 409 or a redundant commit; real typed handler argument errors now return terminal 422 `proposal_argument_invalid` instead of an unstructured failure. No lifecycle status was added. Existing generic validation failures, stale-base 409, transient preflight 500/pending, exact secret policy, provider authorship, required controls, canonical settlement, and immediate tool behavior are retained.

All blob-store-only tools are exempt from graph-equality rejection. The narrower approval-required family still controls receipt replay and same-version acceptance. This distinction matters for historical pending `create_blob`: replay can write a real blob, but the existing service requires a proposal-effect receipt that `create_blob` does not produce. Its existing 409/pending/no graph-state-advance behavior is explicitly pinned, rather than falsely rejecting a durable effect as no change. Normal `create_blob` remains immediate and never staged. Receipt support for historical staged `create_blob` remains unsupported and is not closed by this slice.

Tests use SQLite and named error/race controls; no PostgreSQL contention proof, full backend/frontend suite, all-rule lint corpus, or security scan was run. Parent owns sequential independent review, broader gates, integration, canonical settlement, and staging follow-ups. The source tree and new test are frozen after the recorded hashes.

## Fix round 1: finalization-before-equality regression

The review identified a test gap: the initial four-way identity matrix did not make required-control finalization change otherwise-identical dispatch content. Production ordering was already correct. Fix1 changes only `tests/unit/web/sessions/routes/composer/test_proposal_accept_terminal_refusal.py`; production and frontend fingerprints remain identical to the previous frozen package.

The new parameterized route test `test_legacy_accept_compares_content_after_required_control_finalization[empty-patch|same-value]` seeds a CSV -> LLM -> JSON graph through real `set_pipeline` dispatch, inline-blob custody, and fenced state persistence. It reuses `_bare_llm_candidate` and the actual compiled `_guardrail_profile_view` from the existing authoring/control tests. The current graph has no required controls. The real `set_metadata` replay advances the version but leaves the authored-content hash equal to the current graph. The route then runs its actual required-control finalizer, invokes actual runtime preflight, accepts, and persists a distinct state with selected `prompt-approved`/`content-approved` controls and canonical pending `pipeline_decision` disclosures. Assertions bind the persisted hash to the preflight input, verify that both differ from dispatch/current content, validate coverage, and check exactly two history rows and one accepted lifecycle event. Dispatch/preflight wrappers delegate to their real implementations; no fake finalizer or preflight result is installed.

Fixture detail for auditD22 reconciliation: `tests/fixtures/composer_fakes.py:65`, `StaticPluginSnapshotFactory.for_user_id` at lines 76–88, recreates a snapshot for the route principal but omits `control_modes`. Thus using it for this required-control route case silently projects the compiled policy to empty/default control modes. Fix1 does not modify that shared fixture. It constructs a nominal `PluginAvailabilitySnapshot` for `local:alice` from the actual compiled fixture, explicitly retaining `control_modes`. Source search found this static factory only under tests, with no production reference in `src`. Production `src/elspeth/web/plugin_policy/availability.py` constructs the snapshot with `control_modes=policy.control_modes` (line 186). This observed issue is in the test projection, not evidence that the production builder drops compiled modes; the auditD22 item is not declared resolved here.

Verification after the amendment, with both source roots and explicit worktree cwd:

```sh
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest -n0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" tests/unit/web/sessions/routes/composer/test_proposal_accept_terminal_refusal.py tests/unit/web/composer/test_required_control_autowire.py
```

Exit 0: **93 passed** (30 amended route cases + 63 existing required-control cases), log `d1-fix1-green.log`. This supersedes the initial 28-case module result for the current test file. Ruff check and Ruff format check on the amended module both exit 0 (`d1-fix1-ruff.log`, `d1-fix1-format.log`); `git diff --check` and reverse applicability check of the fixdiff both exit 0. Broad architecture gates were not repeated for this test-only amendment.

The ordering negative control runs in a disposable Python process with a pytest collection plugin. It AST-moves the complete existing equality/refusal block immediately before required-control finalization and replaces only the already-registered endpoint function's in-memory code object. It asserts exactly one block move and writes no production file:

```sh
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python .claude/lanes/composer-remediation-wave1/d1-fix1-order-mutant.py
```

Exit 1: **both new variants fail** (2 failed, 28 deselected) at expected HTTP 200 versus mutated 422 `proposal_no_change`, after the test has already verified a successful valid dispatch with equal authored content. Log `d1-fix1-mutant.log`; runner retained at that lane-local path for reproduction. Normal production files were never edited for the mutation. Setup iterations initially used an unsupported raw-path source, then the static factory's omitted modes, and finally an incorrect disclosure `artifact_hash` field. These test-fixture issues were corrected to real inline custody, the compiled-mode-preserving snapshot, and canonical pending review fields before the final green/mutant runs; they are not mutation evidence. The last setup failure is retained in `d1-fix1-setup.log`.

The prior test was saved before edits at `<worktree>/.claude/lanes/composer-remediation-wave1/d1-legacy-before-fix1.py`. Amendment-only artifact: `/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d1-legacy-fix1.diff`. The full `d1-legacy-review.diff` and four-file `d1-legacy-freeze.txt` have been regenerated; previous fingerprints are retained in `d1-legacy-freeze-before-fix1.txt`. No production correction, shared-fixture change, commit, or expanded task closure is included.
