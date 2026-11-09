# D1 canonical tranche handoff — frozen and staged for independent review

Base: `e3d07d59d56305bf807f4a133275f3a5dbdafb89`.
Worktree: `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`.
Requirements: corrected `d1-canonical-brief.md`, including all appended design-review corrections; design review and map A–E. No full audit or ledger was read. No agents or commits were created.

## Change and compatibility decisions

- Canonical preparation classifies acceptable finalized content equality only after exact proposal/candidate identity and recovery pair/executor-content checks. It throws `PipelineCommitError(code="NO_CHANGE")` with an existing validated recovery dispatch retained. Fresh equal candidates never execute; committed replay and terminal retries remain ahead of preparation.
- Only `ToolArgumentError` from candidate construction and audited execution is translated to safe `ARGUMENT_ERROR`, using `safe_message`. Restoration, ValueError and integrity paths are not broadened. Build failures have zero invocations. Executor failures retain exactly one ARG_ERROR, which settlement persists without any SUCCESS-only `PipelineDispatchAuditBinding`.
- `no_change` is the only added closed persisted rejection reason (protocol and authority allowlist). Argument errors reuse `validation_failed`, with a distinct public diagnosis. No lifecycle status, migration, schema, synthetic composition row, success binding or invocation was added.
- Both refusals reuse required rejection SQL plus real projection and exact system actor `system:pipeline_commit:user:<principal>`. HTTP normalization matches legacy: outer response `detail` contains `error_type=proposal_no_change` or `proposal_argument_invalid`, safe `detail`, `tool_name=set_pipeline`, and the normal request correlation id. Known fresh refusal copy says automatically rejected only after required custody completes.
- Planner equality is after the complete audit cohort and before proposal APIs, trust reread, runtime preflight or commit intent. It returns the original state/version, `runtime_preflight=None`, empty returned audit tuples, and explicit synthesized `raw_assistant_content=""`. Real storage/history coverage proves that notice is not replayed as provider-authored prose. Changed Stage-2/trust downgrade controls and candidate-None behavior remain covered.
- Complete canonical freeform AUTO uses its existing finalized candidate and `_UnappliedAdmission` before execution/custody/publication. Candidate acceptance and no prepared inline blob are mandatory. Normal result finalization and exact content hashing retain standing validation/preflight, neutral anti-anchor behavior, one audited feedback and truthful unchanged progress. Ordinary incremental AUTO version behavior remains unchanged. Existing resource-race catches and custody logic remain in place.

## Evidence and commands

All Python test commands use explicit `cd /workspace/elspeth/.claude/worktrees/composer-remediation-wave1`, exported-command `PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src`, `.venv/bin/python -m pytest`, and `-o pythonpath="$PWD/src $PWD/elspeth-lints/src"`. Import provenance is recorded in `d1-canonical-logs/provenance.txt` and resolves both packages into this worktree. Every suite writes a lane-private log and its shell terminal exit was observed. No full backend, PostgreSQL or all-rule suite was run here; those and independent Astra review remain parent-owned.

- `red-planner.log`: exit 1, three intended equal-plan failures / 12 changed and None controls passed, before production changes. Failure is actual preflight reached on equal finalized candidate.
- `red-equal.log`: exit 1. Fresh equality dispatched instead of rejecting; matching recovery returned RecoveredPipelineCommit. Initial control assertions also exposed tuple/list fixture expectations, corrected before green.
- `red-initial.log`: exit 1, 22 failed / 49 passed. It includes intended missing argument/no-change behavior plus an initial test-only owned-authority fixture schema error. The fixture was corrected to actual execution arguments before `red-planner.log`; all unintended fixture failures have later green coverage. `green-initial.log`, `green-more.log`, `http-equal*.log`, `http-refusals*.log`, and `cancellation.log` retain intermediate harness diagnoses rather than hiding failed runs.
- `green-boundaries.log`: exit 0, 17 passed: operation-authority tests, actual terminal planner test, and exact before-SQL / post-SQL cancellation originals and required child projection checks.
- `focused-suite.log`: exit 0, 713 passed, two Pydantic ReadOnly TypedDict warnings. Command selection: `-n 4 tests/integration/web/composer/test_pipeline_proposal_lifecycle.py tests/unit/web/composer/test_pipeline_commit_operation_authority.py tests/unit/web/composer/test_planner_stage_runtime_preflight.py tests/unit/web/composer/test_pipeline_planner.py tests/unit/web/composer/test_audit_cohort_settlement.py tests/unit/web/sessions/test_pipeline_rejection_required.py tests/unit/web/sessions/test_pipeline_rejection_callers.py tests/unit/web/sessions/test_pipeline_rejection_causal_custody.py tests/unit/web/sessions/test_pipeline_rejection_cancellation_orders.py tests/integration/web/composer/test_freeform_proposal_prevalidation.py tests/integration/web/composer/test_freeform_required_controls.py`. Warnings are `ChatCompletionReasoningItem.summary` ReadOnly qualifiers in `test_calculated_azure_cost_enforces_planner_continuation_cap` and `test_senior_handoff_gets_owned_visible_chronology_and_whole_request_once`.
- `precedence-history.log`: exit 0, 15 passed: late-added AUTO unacceptable-equal policy control and both equal/changed planner cohort all-write-index controls, including real stored synthesized history. These overlap tests in the 713 run; counts are not additive unique coverage.
- `frontend.log`: exit 0, 190 passed. Explicit cwd `.../src/elspeth/web/frontend`; `node --version` is v24.22.1; `./node_modules/.bin/vitest run src/stores/sessionStore.test.ts`. This exercises the real no-change/argument-invalid reload/error handlers. The node_modules symlink remains unstaged.
- `mypy.log`: exit 0, no issues in the six production modules. Command `.venv/bin/python -m mypy src/elspeth/web/composer/pipeline_commit.py src/elspeth/web/composer/planning_application.py src/elspeth/web/composer/tool_batch.py src/elspeth/web/sessions/proposal_authority.py src/elspeth/web/sessions/protocol.py src/elspeth/web/sessions/routes/composer/pipeline_settlement.py`, same cwd/PYTHONPATH. Mypy notes existing unused config sections.
- `contracts.log`: exit 0, `.venv/bin/python -m scripts.check_contracts`, same cwd/PYTHONPATH. Census unchanged: 2615 soft occurrences across 416 files, 60 boundary-parsed.
- `ruff-final.log`: exit 0, `.venv/bin/ruff check` and `.venv/bin/ruff format --check` over exactly all 13 paths in `d1-canonical-logs/owned-files.txt`.
- `scoped-tier.json` / `.err`: exit 1, command `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src .venv/bin/elspeth-lints check --rules trust_tier.tier_model --root src/elspeth --repo-root "$PWD" --files <six production paths above> --format json`. The global rule ignored the intended --files narrowing and emitted the full 5220-entry corpus. Parent compared it against exact-base `d1-stage-race-logs/tier-model-final.json` using the controlled semantic multiset comparator: 5220→5220, zero semantic additions/removals. See `d1-canonical-tier-comparison.json`; line/fingerprint shifts are retained separately. Existing stale allowlist entries reference deleted guided_operations/guided/guided_chat_atomic files. No signatures/allowlists were edited; this is not signed clearance or a clean corpus claim.

## Freeze record

The terminal authority, mutation, focused-test and staging records appear below. Parent owns integrated/full D1 closure and review; this report does not claim full D1 closed.

Final focused production-and-tests freeze: `focused-final.log` exited 0 with 714 passed and the same two named Pydantic warnings in 158.25s. It reran the exact 11-file selection above after the late test-only changes. `focused-final-before.sha256` and `focused-final-after.sha256` compare byte-for-byte (cmp exit 0); `focused-final-freeze-check.txt` independently verifies all 13 files (sha256sum -c exit 0). No source or test was edited during this run.

Authority gate terminal: `authority-gates.log`, exit 0, **547 passed, 3 skipped, 1 xfailed** in 872.20s. Command uses the Python cwd/path settings above with `-n 0 tests/unit/web/test_sessions_composer_attribute_contracts.py tests/unit/elspeth_lints/test_masquerade_gate.py tests/unit/test_mock_discipline_baseline.py tests/unit/architecture/test_session_db_mutation_authority.py`. Printed diagnostics include two stale reviewed read connections (`src/elspeth/cli.py:5043 health`, `src/elspeth/web/shareable_reviews/service.py:655 ShareableReviewService._latest_mark_ready_event`), zero stale non-Sessions classifications and zero invalid reviewed read connections. All static and focused test processes reached terminal status before any temporary source mutation. `skill-inventory.log` also exited 0 for `scripts/cicd/generate_skill_inventory.py --check` with the same cwd/PYTHONPATH.

## Mutation controls and restoration

Temporary mutations were applied only after the authority/tier/focused processes had exited. Every mutant command uses the same explicit cwd, both Python path roots, `-n 0`, a lane-private log and a shell EXIT trap restoring the exact backed-up production bytes. No mutation is part of the review diff or staged candidate.

1. `mutation-disable-shortcuts.log`: exit 1, **7 failed / 7 passed** after disabling canonical, planner and complete-AUTO equality branches. Fresh/matching canonical equality, all three planner trust outcomes, and singleton/named AUTO equality fail. Recovery mismatch/missing-pair, explicit equality, changed AUTO builder-count, changed green Stage-2 and unacceptable-equal controls still pass.
2. `mutation-recovery-order.log`: exit 1, **2 failed / 2 passed** after moving canonical equality before the recovery pair/hash proofs. Mismatch and missing-pair controls fail; fresh and valid matching cases pass.
3. `mutation-admission-precedence.log`: exit 1, **1 failed / 5 passed** after removing the AUTO candidate-acceptable guard. The real equal-shaped complete candidate rejected by current profile no longer executes its existing validation path and the precedence test fails; acceptable equality and changed AUTO controls still pass.
4. `mutation-cohort-order.log`: exit 1, **7 failed / 7 passed** after moving planner no-change before audit persistence. Every equal-plan actual audit cohort success/fault index fails its evidence oracle; all changed-plan controls still pass.

Per-mutation SHA-256 checks of the three restored production files exited 0. After all mutations, `post-mutation-freeze.txt` verifies the exact original hashes of **all 13 owned files**, matching the frozen 714-test run, exit 0. The final post-restoration green command and staging evidence follow below.

Final post-restoration green: `post-mutation-green.log`, exit 0, **58 passed in 34.77s**. This reruns operation authority, planner Stage-2/equality, all 14 actual cohort write-index cases, complete AUTO equal/unacceptable/changed controls, all eight public/owned fresh/recovered/build-argument/executor-argument HTTP cases, and both exact rejection cancellation boundaries. The post-run `post-mutation-green-freeze.txt` confirms all 13 files still match the frozen 714-test candidate (exit 0).

## Exact handoff state

`git rev-parse HEAD` remains `e3d07d59d56305bf807f4a133275f3a5dbdafb89`. `git add -- <the exact paths below>`, `git diff --cached --check`, `git diff --quiet`, and the independent `sha256sum -c focused-final-before.sha256` all completed successfully (`stage_freeze_exit=0`). Exactly these 13 owned tracked files are staged; no tracked files are unstaged. No new tracked file was added. All temporary mutant backups were removed. The untracked frontend node_modules symlink remains unstaged. `final-status.txt`, `staged-files.txt`, `staged-freeze.txt` and `review-diff.sha256` retain the raw freeze evidence.

`d1-canonical-review.diff` is the complete binary-capable `git diff --cached --binary` against this exact HEAD, containing the entire owned patch. No source/test edits remain pending after this freeze. The parent can now run the integrated/full Python, serial PostgreSQL, global all-rule and fresh independent Astra gates. No full-D1-closed, signed-clearance or merge claim is made here.

Staged paths:

```
src/elspeth/web/composer/pipeline_commit.py
src/elspeth/web/composer/planning_application.py
src/elspeth/web/composer/tool_batch.py
src/elspeth/web/sessions/proposal_authority.py
src/elspeth/web/sessions/protocol.py
src/elspeth/web/sessions/routes/composer/pipeline_settlement.py
tests/integration/web/composer/test_freeform_proposal_prevalidation.py
tests/integration/web/composer/test_pipeline_proposal_lifecycle.py
tests/unit/web/composer/test_audit_cohort_settlement.py
tests/unit/web/composer/test_pipeline_commit_operation_authority.py
tests/unit/web/composer/test_pipeline_planner.py
tests/unit/web/composer/test_planner_stage_runtime_preflight.py
tests/unit/web/sessions/test_pipeline_rejection_callers.py
```

No known focused behavioral failure remains. Material limits are the parent-owned integrated/full/PostgreSQL/review gates, the explicitly reported baseline tier corpus and stale classifications, the three skipped / one expected-failure authority cases, and the two named dependency warnings. Initial fixture mistakes and all deliberately red mutants remain in their logs with terminal exits; they are not represented as clean runs.

# Review fix round 1 — Important I1

Read the complete I1 finding in `d1-canonical-review.md` before editing. This round changes **only** `tests/integration/web/composer/test_freeform_proposal_prevalidation.py` relative to the parent's immutable reviewed index tree `ccec0218c209389d48e142baff31a3ffebd879ab`. Production ordering and behavior remain the previously approved implementation. No refactor, commit, subagent, migration, dependency change or production fix was made.

Added real complete-current-state AUTO producer controls:

- `test_complete_auto_raw_equality_still_applies_required_control_and_review_finalization` builds a complete current graph through the actual existing incremental tool producer, serializes its actual public set_pipeline arguments, and observes the real candidate builders. The first admitted raw candidate hashes exactly equal to current content; real required-control/review finalization then changes authority. The test requires one actual executor call, version/content change, required control nodes and canonical disclosure rows in both state and audited arguments, durable publication, one actual feedback invocation, no unchanged feedback, no proposal, and unchanged provider-authored input. Builder/finalizer wrappers always call the real implementations and return their real results.
- `test_complete_auto_same_bytes_new_source_authority_is_published[same-inline-bytes-new-custody]` first creates and publishes an actual AUTO inline artifact, then starts the measured call from that complete persisted state, passing its real current-state id. Repeating the same inline bytes creates a second ready custody artifact with a distinct identity; both files exist with the same content hash and exact bytes. The measured turn executes once, advances state/version, publishes the second identity, and records exactly one actual audited feedback without unchanged hints.
- The `only-blob-identity-changed` companion starts from a complete real tool-built state and binds another actually stored uploaded blob with identical bytes. Both identity cases assert all graph fields and every non-path/non-blob-ref source option remain equal, so a changed source storage identity alone is sufficient to remain a real change. The inline companion additionally pins the real invented-source review. Existing ordinary incremental AUTO equal/change version tests and changed canonical builder-count control are untouched.

No candidate object or custody result is mocked. The changed pending-review cases retain the existing completion/preflight behavior; no new green runtime claim or single-provider-call assumption is introduced. Initial test-only setup failures are retained in `fix1-new-initial.log` (incorrect single prose-call assumptions) and `fix1-inline-stronger.log` (missing real current-state id on a second published compose call). The fixtures now follow the existing runtime completion behavior and pass the actual persisted head guard; no production check was bypassed.

## Round 1 terminal commands and controls

Every Python command uses explicit cwd `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`, `PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src`, `.venv/bin/python -m pytest`, and `-o pythonpath="$PWD/src $PWD/elspeth-lints/src"`.

- `fix1-new-green.log`: exit 0, 3 passed before strengthening the inline seed to a real prior AUTO publication. `fix1-inline-stronger-green.log`: exit 0, both identity cases passed with actual prior-head custody/publication.
- `fix1-mutant-premature-final.log`: exit 1, **1 failed / 2 passed**. Temporary production mutation returned the initially acceptable equal candidate before actual required-control/review finalization. The required-control near-equality case fails its post-finalization hash oracle, while both actual identity/custody cases pass. The earlier `fix1-mutant-premature-equality.log` retains the same intended red outcome before strengthening the inline fixture.
- `fix1-mutant-identity-loss.log`: exit 1, **2 failed / 1 passed**. Temporary mutation erased only source `path` and `blob_ref` from composition hashing. The repeat-inline case detects loss of content authority identity despite actual execution/custody; the existing-blob case detects the consequential false no-op (executor called zero times). The required-control/review graph-change control still passes.
- Both mutations were protected by shell EXIT traps restoring exact backed-up production bytes. `sha256sum -c fix1-mutation-before.sha256` reported both files OK after restoration. `git diff --exit-code ccec0218c209389d48e142baff31a3ffebd879ab -- src` and its staged counterpart both exited 0. All production mutations and backups are removed from the final candidate.
- `fix1-affected-module.log`: exit 0, **97 passed in 42.48s**. Exact selection: `-n 4 tests/integration/web/composer/test_freeform_proposal_prevalidation.py tests/integration/web/composer/test_freeform_required_controls.py::test_auto_commit_persists_auto_wired_textract_state_and_disclosure tests/integration/web/composer/test_freeform_required_controls.py::test_auto_commit_does_not_duplicate_already_covered_controls tests/integration/web/composer/test_freeform_required_controls.py::test_auto_commit_wires_controls_when_set_output_completes_incremental_pipeline`. This covers all new cases and the affected module's existing ordinary incremental AUTO/version, changed builder, inline, and admission controls.
- `fix1-mock-hygiene.log`: exit 0, **3 passed in 34.28s**, selection `-n 0 tests/unit/test_mock_discipline_baseline.py`.
- `fix1-hygiene.log`: scoped Ruff check and Ruff format check over the one changed test file both exited 0; `git diff --check` and `git diff --cached --check` also exited 0.
- `fix1-final-before.sha256`, `fix1-final-freeze.txt`, and `fix1-staged-freeze.txt` prove all 13 owned file hashes remained exact during the final 97-test run and after staging (both freeze checks exit 0). No files were edited during that run.

The unchanged production retains the earlier frozen 714-test, 547-passed/3-skipped/1-xfailed authority, 190-test frontend, mypy/contracts, and unchanged-tier-corpus evidence above. Per parent instruction, this test-only round did not repeat the 14-minute authority gate, broader backend selection, frontend suite, global lint or serial PostgreSQL. Their earlier scope/limits and the parent-owned final gates remain explicit.

## Round 1 frozen handoff

- HEAD remains `e3d07d59d56305bf807f4a133275f3a5dbdafb89`; the full index still contains only the same 13 owned paths, with no unstaged tracked edits.
- Scoped `d1-canonical-fix1.diff` is the complete `git diff --cached --binary ccec0218c209389d48e142baff31a3ffebd879ab`, and names only the one test module. SHA-256: `696e872ed9df1fb71b144b338c506eca5a9f104e6317cbc59ac9f77e8d19e0ed`.
- Full `d1-canonical-review.diff` has been refreshed from `git diff --cached --binary`; SHA-256: `3d0d7b5942cce2c8046869aa699c33a17b6ee03c116354165b6630840c5dd4d5`. The parent-preserved `d1-canonical-pre-fix1.diff`/tree retain the originally reviewed patch; earlier digest records above are historical and do not describe the refreshed full diff.
- `fix1-changed-files.txt`, `fix1-final-status.txt`, `fix1-diffs.sha256` and `fix1-staged-freeze.txt` retain raw final evidence. Stage/freeze chain exited 0. No temporary source mutation or new tracked file is included.
- Parent environment preparation has replaced the old node_modules symlink with a physical ignored dependency directory of the same content. This is not part of either diff and was not changed in this round.

Ready for the same independent reviewer to re-review the scoped test delta against I1. This handoff does not assert reviewer acceptance or full D1 closure.
