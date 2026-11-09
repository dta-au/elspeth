# D1 proposal-publication resource-race review

Reviewed the staged nine-file source delta at base `cf23f38d889eac66231797782ed129fa299f250f` in `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`. Frozen input: `d1-stage-race-review.diff`, SHA-256 `011b1b0578a07c2db9efebeab951ccbec9b3c2723881f033ee4ced37858a06bf`; both base and artifact hash were verified. Requirements were read first, including their source-preflight corrections, followed by design review, implementation report, AGENTS/CONTRIBUTING, and the requesting-code-review skill. No subagents or test runs were launched, and no production/test files, staged content, or HEAD were changed by this review.

## Verdicts

- **Specification: changes requested for one Important verification gap.** The production delta implements the specified narrow outcome correctly on inspection. Acceptance remains incomplete because the explicit requirement that every changed behavior be pinned by meaningful producer tests is not met for freeform proposal-cap preservation.
- **Code quality: production implementation is sound; test changes require the focused addition below.** The named domain exception, shared fixed copy, existing admission disposition, and existing required-work projection are appropriate reuse. No production correctness defect or unnecessary architectural expansion was found in this scope.

## Findings

### Important — creation-time resource refusal does not have a same-batch cap regression test

Location: `tests/integration/web/composer/test_freeform_proposal_prevalidation.py:2402`, in `test_publication_resource_race_refuses_once_then_allows_valid_repair`.

The new real-producer test places `call_raced` and `call_repaired` in two separate `_tool_turn` responses (lines 2403–2404). The production counter is initialized to zero inside each `run_tool_batch` invocation (`tool_batch.py:1134`), with cap 10 (`:215`). Therefore, creating a repair in the next batch cannot establish that the failed publication did not consume a proposal slot in the raced batch. Even an erroneous counter increment in the new `except ProposalResourceUnavailableError` arm would not affect any asserted outcome of this test: the counter is reset before the repair.

The existing `test_proposal_cap_counts_only_admitted_changes` (`test_freeform_proposal_prevalidation.py:1636`) exercises no-op, argument, pre-admission refusal, and admitted-change cases. Its blob-refusal case uses a nonexistent ID from the outset, so it never reaches the new post-admission transactional creation refusal. It does not close this gap.

The current source correctly clears `proposal_redacted_arguments` at `tool_batch.py:2313`, placing the increment at `:2315` behind the success guard. This finding concerns the binding test requirement, not an observed runtime failure in the submitted implementation.

**Required change:** add a producer test that reaches the real creation-time resource failure and then exhausts the still-available proposal capacity within the **same provider tool batch**. For example, fail one admitted publication, restore the resource as needed, and successfully publish ten valid proposals in that batch; assert exact proposal/event/invocation counts and no cap error. Exercise the applicable canonical/ordinary/promoted publishers using the existing fixture. Demonstrate a focused negative control that mistakenly increments the counter on refusal and makes this test fail. No production refactor is needed.

### Critical

None found.

### Minor

None raised.

## Evidence and bounded source checks

The frozen diff was reviewed in full. Additional source reads were limited to these named risks:

1. **Refusal accidentally entering success accounting or redispatching.** Checked `tool_batch.py` around proposal finalization/publication, the success block, `_unapplied_admission`, `_await_started_tool_stage`, and `_do_dispatch`. The catch spans both real creation APIs, including promoted `set_pipeline`; it caches the refused result, clears the success discriminator, and falls through to the existing dispatch path. `_do_dispatch` returns the cached result before calling the handler. The async stage wrapper only closes cancellation, so the new expected failure does not already have a competing audit completion. The new test checks false business-success observation, one refused feedback/outcome/invocation, registered persisted error code, unchanged graph, and later repair. The counter coverage limitation is the finding above.

2. **Cancellation, fabricated creation success, or post-SQL failure being swallowed by planner refusal.** Checked the deferred-cancellation wrapper, `_stage_pipeline_plan` cohort/preflight ordering, and `pipeline_rejection_custody.py::project_creation_handoff`. Direct cancellation is raised with the child error as its cause before the narrow caller catch. Required staging projects the actual handoff before checking for no row, exactly one nominal domain error, and no creation cancellations. The projector verifies the registered SQL outcome and completes the projection obligation; returned rows with telemetry faults remain rows plus failures, outside the refusal arm. New tests park at the actual validator SELECT, retain original cancellations/errors and SQL receipts, complete SQL/projection tickets, and distinguish telemetry-created rows/events from no-row refusal.

3. **Owned status corruption being converted into expected unavailability.** Checked the existing `contracts.blobs.BLOB_STATUSES` authority. The validator uses exact-string/closed-membership validation before owned non-readiness classification; missing/foreign outcomes use fixed copy, while unknown/null owned statuses fault. New SQL-backed validator tests cover ready, missing, foreign, pending, error, unknown, null, and malformed authority failing before a resource query.

4. **Candidate verdict or duplicated audit cohort escaping on unchanged planner result.** Checked cohort persistence and candidate preflight ordering in `_stage_pipeline_plan`. Both refusal returns use the original state and explicit `raw_assistant_content=""`, leaving commit intent/runtime verdict and returned invocation/provider lists empty. The new cohort test uses a valid changed candidate, green preflight followed by a resource change, and exact persisted cohort/proposal/event/state assertions. The direct/required publisher matrix additionally covers all four snapshot/live trust-mode combinations.

5. **Existing tests already covering the counter requirement.** Read the existing cap test and the new freeform test setup/assertions, plus the new exception/code references. The counter resets per batch and the new test uses two batches; the existing cap matrix does not invoke the new creation-time refusal. This is the concrete source evidence for the Important finding, not an inference from a test-name search.

## Verification limits and lint accounting

No existing suite was rerun: the supplied report records 202 bounded producer tests, 44 session proposal tests, and 301 contract/gate tests passing, with scoped static checks clean. These results are implementer evidence, not independent execution by this reviewer. Parent-owned full backend, PostgreSQL, and combined all-rule gates remain pending and are not approved by this review.

The supplied exact-base tier-model comparison has 5,220 findings on each side, with the existing per-file allowance diagnostic changing from 9/5 to 10/5 for the new narrow catch. The catch is justified by the reviewed domain outcome and is not a reason to distort the implementation. No allowance/signature change is requested; operator lint accounting remains separate from this source review.

## Fix round 1 scoped rereview

**Prior Important finding: ADDRESSED.** This conclusion supersedes the earlier changes-requested verdict for the reviewed candidate. **Specification: approved within this source-review scope. Code quality: approved within this source-review scope.** No new Critical, Important, or Minor finding was identified in the scoped fix. Parent-owned broad gates and operator lint accounting remain outside this approval.

Reviewed the appended fix-round report and the complete test-only `d1-stage-race-fix1.diff` (27 additions, 12 removals), whose verified SHA-256 is `ba25091512f8dae12de1ec0b58eec5bc1bda8d109acae9ae51f1ec8a48090b7b`. The refreshed full review artifact hash is verified as `4fb168dd3521f6019baf8b6e4fb074a75d6b7227176d540ec280420215c9bead`. Production is unchanged by this fix.

The amended test retains later-batch repair and adds same-batch cases for all three publishers. Each new case sends one provider completion with the real creation-time refusal followed by ten distinct admitted proposal calls. It pins one dispatch with eleven outcomes, eleven ordered semantic invocations, eleven persisted tool invocations with exactly those IDs, ten actual proposals/events containing only repair IDs, one refused feedback/outcome, and no graph-state row. This now exercises the lifetime of the actual proposal counter and closes the identified gap without changing production behavior.

Reviewed the recorded terminal exits and log results: the three same-batch cases pass with exit 0; the exact saved mutant adds only `proposals_this_turn += 1` in the resource-refusal catch and all three cases fail with exit 1 at the real proposal-cap guard; the restored full affected file passes all 88 tests with exit 0. Ruff's recorded exit is 0. The before/after freeze records match, and independently reading current hashes confirmed the test hash `5cd98a5f180fc2c15cad4665b007e9ee9a629049e79395dbeca9b2fa09b68e06` and production `tool_batch.py` hash `f8c77f608048f74c8c31222ecae4b8ac84e97daf246bc2999a67a739a5520963` match the restored-code evidence.

No tests were rerun, no broad audit or subagent was launched, and no code/index/HEAD changes were made during this rereview. Only this review conclusion was appended.
