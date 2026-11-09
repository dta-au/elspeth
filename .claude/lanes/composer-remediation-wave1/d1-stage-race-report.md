# D1 proposal-publication resource race

Implemented and staged, without a commit, on exact base `cf23f38d889eac66231797782ed129fa299f250f` in `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`. Nine tracked files are staged. The deliberately untracked frontend `node_modules` symlink remains present and unstaged. No agents, broad backend suite, PostgreSQL suite, all-rule lint scan, policy edits, or signed-metadata edits were performed by this lane.

## Behavior

`sessions/proposal_blob_refs.py` now raises `ProposalResourceUnavailableError` only for the validator's queried missing, foreign-session, and known non-ready blob outcomes. An owned row must first have an exact string status in the existing `BLOB_STATUSES` authority. Unknown or malformed owned status raises `AuditIntegrityError`; malformed reference/owned-authority parsing remains `ValueError`. Public copy is fixed and contains no resource identifier, foreign ownership detail, SQL text, or parser text. Transaction locks, operation/principal/claim checks, canonical base validation, reference validation ordering, and proposal/event insertion ordering are unchanged.

Both freeform creation APIs are covered by the narrow catch, including an incremental mutation promoted to canonical `set_pipeline` by required-control finalization. Expected failure becomes the existing cached `_UnappliedAdmission` refused disposition, then traverses canonical dispatch/outcome/feedback once. It bypasses the entire publication-success block, does not invoke the handler again, and does not increment the proposal count or business-success flag. The existing completed-operation audit status is `SUCCESS` with `ToolResult.success=False`; `turn_has_mutation` continues to mean an attempted mutation. New closed code `proposal_resource_unavailable` is registered and survives persisted redaction. The original state/version and prior runtime preflight are preserved.

Direct planner staging catches only the named domain failure around the deferred-cancellation creation await. Required staging first projects the privately issued creation handoff and returns the refusal only for no row, exactly one nominal domain failure, and no creation cancellations. Both return the original state, fixed backend text, explicit `raw_assistant_content=""`, no commit intent, no candidate runtime verdict, and no duplicated invocation/provider cohort. The real failed SQL receipt remains retained, SQL/projection tickets complete, and no rejection child is created for a nonexistent proposal. A returned row plus telemetry failure still faults and retains its actual row; arbitrary/grouped faults and cancellation retain existing propagation.

## Producer coverage

- `test_proposal_blob_refs.py`: real SQL query classification for missing, foreign, pending, error, ready, unknown owned status, and null owned status; malformed nested marker, malformed sentinel, and malformed owned authority fail before querying a nonexistent table.
- `test_freeform_proposal_prevalidation.py::test_publication_resource_race_refuses_once_then_allows_valid_repair`: real canonical, ordinary durable-blob, and required-control-promoted canonical publishers; resource changed after successful preview/admission and before real transactional creation. Pins single refused feedback/outcome/audit, exact handler/API call counts, persisted registered code, false business-success observation, no raced proposal/event/state row, and one later valid repair proposal/event.
- `test_pipeline_rejection_callers.py::test_actual_planner_resource_race_retains_receipts_and_original_cancellation`: real direct and finish-once required publishers across all four snapshot/live trust-mode combinations. The barrier is the actual blob-reference validator SELECT, not proposal INSERT. Pins exact delivered cancellation identity, direct cancellation's real domain-error cause, required SQL error object/receipt identity, completed SQL/projection obligations, no rejection child, unchanged backend result, and exact existing-plus-planner cohort occurrence counts. Additional real producer controls cover missing/error resources, corrupt status, unrelated ValueError, SQL failure, grouped faults, and returned-row post-SQL telemetry failure.
- `test_audit_cohort_settlement.py::test_resource_race_after_planner_admission_preserves_cohort_once_and_no_candidate_verdict`: valid changed candidate and green Stage 2, followed by resource change; exact three-member persisted planning cohort remains once, with zero proposal/event/state rows and no candidate verdict exposed on the unchanged result.
- Existing `test_actual_planner_creation_cancel_retains_nonzero_child_rejection_scope` retains the separate proposal-INSERT barrier for real returned-row creation with repeated cancellation, with/without post-SQL telemetry failure. Existing required finish-once producer tests also pass unchanged.
- `test_composer_proposals.py`: updated two existing exception expectations for the new nominal resource outcome, retaining missing/cross-session/pending and both serial proposal/delete winner controls, plus the existing owned-ready success control.

## Verification and provenance

All commands ran from the explicit worktree path with both roots exported in `PYTHONPATH` and pytest's explicit `-o pythonpath`; `d1-stage-race-logs/provenance.log` records cwd, exact HEAD, both imported module paths, and preserved symlink. All results below are terminal exits, not inferred from partial output. Logs are under `.claude/lanes/composer-remediation-wave1/d1-stage-race-logs/` in the main checkout.

| Evidence | Terminal result |
| --- | --- |
| `red.log`: new validator/freeform tests before the fix | exit 1; 8 failed, 1 ready control passed |
| `planner-red4.log`: direct real publisher before caller fix | exit 1; named resource error propagated |
| `planner-required-red.log`: required real publisher before caller fix | exit 1; named resource error propagated after real projection |
| `freeform-green.log` | exit 0; 9 passed |
| `final-producers2.log`: final targeted race/validator/malformed-authority cases before the added null-status case | exit 0; 42 passed |
| `focused-frozen.log`: all six bounded producer test files named in the brief | exit 0; 202 passed in 106.48s |
| `session-proposals-final.log`: complete existing session proposal test file after nominal exception expectation update | exit 0; 44 passed |
| `contracts-gates.log`: attribute, tool-envelope gate/envelope, validation error-code registry, owned-authority tests | exit 0; 301 passed |
| `mypy.log`: four changed production files | exit 0; no issues; existing unused-config-section note |
| `ruff-final.log` plus final ninth-file check/format check | exit 0; clean |
| `git diff --cached --check` | exit 0 |

The complete six-file producer regression ran on a frozen eight-file patch: `frozen-before.sha256` and `frozen-after.sha256` both contain `3cb3f841cfa60516433d12f8e0dbc8ccac4beb3f814b2a94e42d39f46db2b845`. The only subsequent change was the isolated existing session-test expectation update; its complete 44-test file passed. Production code did not move.

Earlier intermediate test-only assertion failures are retained rather than discarded: `planner-red.log` and `planner-red2/3.log` expose test harness setup mistakes before the meaningful caller reds; `planner-green.log` used the wrong cancellation observation seam for required SQL (corrected to observing its real retained cancellation); `planner-green2.log` caught missing metadata in a test candidate; `planner-controls.log` pinned the real telemetry failure's existing wrapper/group shape; `focused.log` rejected the incorrect assumption that attempted-mutation tracking meant business success; `final-producers.log` exposed the fixture's preexisting audit occurrence and the ordinary publisher's valid repair API call. These were corrected to actual producer semantics without changing production to accommodate tests. `existing-session-expectations-red.log` has 4 expected old-ValueError-contract failures and 1 proposal-wins control pass before updating those two old expectations.

## Single-rule lint comparison

The selected `trust_tier.tier_model` rule was invoked with the four changed production paths in `--files`; this rule still emits its global source/allowance corpus. Both exact-base and final scans exit 1. The baseline was extracted with `git archive` at exact `cf23f38...`, with both baseline source roots used. Comparing finding multisets by `(file_path, rule_id, message)` gives 5,220 records in each corpus. The sole added/removed message pair is the existing `web/composer/tool_batch.py` per-file allowance diagnostic moving from `matched 9/5` to `matched 10/5`, attributable to the one new narrow refusal catch. Full evidence is in `tier-model-baseline.json`, `tier-model-final.json`, their stderr logs, and `tier-model-delta.json`. Existing expired/stale allowance warnings remain. No suppression, allowance adjustment, or signature was introduced. This is not signed lint clearance; parent/operator review retains that accounting decision.

## Frozen handoff and limits

`d1-stage-race-review.diff` contains all nine staged paths, including all test changes; there are no new source/test files to omit. Final review-diff SHA-256: `011b1b0578a07c2db9efebeab951ccbec9b3c2723881f033ee4ced37858a06bf`.

The bounded tests use real SQLite/session/required-work producers with controlled provider/preflight/fault seams; they are not a PostgreSQL contention claim or a live-provider acceptance claim. Parent owns the final full backend, serial PostgreSQL, full/static contract gates, and combined all-rule comparison. No commits or external writes were made.

## Fix round 1 — same-batch capacity proof

Read the complete Important finding in `d1-stage-race-review.md` and verified its counter-lifetime claim against the source. The existing later-batch repair could not prove that an admitted publication refusal preserved capacity in the failed batch.

Amended only `tests/integration/web/composer/test_freeform_proposal_prevalidation.py` for this round. The existing real-producer race fixture now runs both `repair_batch=later` and `repair_batch=same` for canonical, ordinary, and required-control-promoted canonical publishers. The same-batch arm sends one provider completion containing the admitted, publication-time resource refusal followed by ten distinct valid metadata proposal calls. It asserts exactly one dispatch carrying eleven outcomes, ten persisted proposals with exactly the ten repair IDs, ten proposal events, eleven semantic invocations in exact call order, eleven persisted tool invocations with exactly those IDs, one refused outcome/feedback, and zero graph-state rows. Normal compose completion proves no cap exception was raised. Existing publisher/handler cardinality assertions remain, and the original later-batch repair coverage is retained.

No retained production change or refactor was needed. The round adds three same-batch cases. The source under test still uses the final transactional validator, actual session publishers, and existing admission/outcome paths.

### Commands and mutation evidence

Commands ran from explicit cwd `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1` using this exact shell prefix for pytest:

```bash
cd /workspace/elspeth/.claude/worktrees/composer-remediation-wave1
export PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src
```

Focused positive and negative commands were identical:

```bash
.venv/bin/python -m pytest -n 0 -o pythonpath="$PYTHONPATH" tests/integration/web/composer/test_freeform_proposal_prevalidation.py -k 'publication_resource_race and same'
```

- Reviewed production: terminal exit 0, **3 passed**, in `d1-stage-race-logs/fix1-same-batch-green.log` (`.exit` is 0).
- Negative control: temporarily added precisely `proposals_this_turn += 1` to `except ProposalResourceUnavailableError` before `_unapplied_admission`. The exact one-line mutation is saved as `d1-stage-race-logs/fix1-counter-mutation.diff`. The same command terminated exit 1, **3 failed** — `[same-canonical]`, `[same-ordinary]`, and `[same-promoted]` — all at the actual `_pending_proposal_cap_error` guard in `tool_batch.py`, raising `ComposerConvergenceError`. See `fix1-counter-mutation-red.log` and its `.exit` file. This proves the added tests detect the reviewer's specific erroneous slot increment rather than relying on a later-batch repair.
- Removed that exact diagnostic line with the inverse patch. `git diff --exit-code -- src/elspeth/web/composer/tool_batch.py` returned 0 before the final run, proving the production file again matched the reviewed staged version. The mutation was never staged.

Final restored-code verification command:

```bash
.venv/bin/python -m pytest -n 0 -o pythonpath="$PYTHONPATH" tests/integration/web/composer/test_freeform_proposal_prevalidation.py
```

Terminal exit 0: **88 passed in 70.92s**, including all six race/repair variants, in `d1-stage-race-logs/fix1-freeform-final.log`; `fix1-freeform-final.exit` is 0. Scoped formatting/check commands were:

```bash
cd /workspace/elspeth/.claude/worktrees/composer-remediation-wave1
.venv/bin/python -m ruff format tests/integration/web/composer/test_freeform_proposal_prevalidation.py
.venv/bin/python -m ruff check tests/integration/web/composer/test_freeform_proposal_prevalidation.py
```

Both completed successfully; Ruff check exit 0 and output are recorded in `fix1-ruff.exit` / `fix1-ruff.log`. `git diff --cached --check` also returned 0. No broad test or lint scans, commits, or subagents were used for this round.

### Fix-round freeze and review artifacts

The test and production file hashes before/after the final full-file run are identical (`fix1-frozen-before.sha256`, `fix1-frozen-after.sha256`; `cmp` exit 0):

- Test: `5cd98a5f180fc2c15cad4665b007e9ee9a629049e79395dbeca9b2fa09b68e06`.
- Reviewed `tool_batch.py`: `f8c77f608048f74c8c31222ecae4b8ac84e97daf246bc2999a67a739a5520963`.

Only the amended test path was restaged. There are no unstaged tracked changes; the untracked `node_modules` symlink remains untouched. The existing nine-file staged set is otherwise unchanged.

The refreshed **full** `d1-stage-race-review.diff` now has SHA-256 `4fb168dd3521f6019baf8b6e4fb074a75d6b7227176d540ec280420215c9bead`, superseding the earlier final-diff hash above. The **scoped** `d1-stage-race-fix1.diff` compares the amended test against the parent's saved `d1-stage-race-pre-fix1-test.py` using `diff -u` with repository-relative `a/` and `b/` labels; its SHA-256 is `ba25091512f8dae12de1ec0b58eec5bc1bda8d109acae9ae51f1ec8a48090b7b`. It contains only 27 added / 12 removed test lines. The parent-preserved pre-fix full diff/test remain available for the same reviewer's scoped rereview. Parent-owned broad gates remain outside this fix-round verification.
