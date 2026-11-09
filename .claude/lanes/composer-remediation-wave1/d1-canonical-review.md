# D1 canonical independent specification and quality review

Reviewed the frozen 13-file patch against base `e3d07d59d56305bf807f4a133275f3a5dbdafb89`, using the corrected `d1-canonical-brief.md` (including its appended corrections), map A–E, design review, report, repository AGENTS/CONTRIBUTING, and the requesting-code-review skill/template. No subagents, test runs, source edits, index edits, or HEAD changes were made. This report is the only review write.

## Verdicts

- **Specification: changes requested.** The implementation matches the requested production ordering and outcomes, but the corrected brief's complete-AUTO near-equality producer controls are incomplete. Important finding I1 below is a test requirement, not a demonstrated production defect.
- **Code quality: production implementation approved; verification completion requires I1.** The patch is small, reuses the existing admission and required-custody mechanisms, and does not introduce a competing lifecycle or success-binding representation. No Critical production defect was found.
- **Ready for task acceptance: with fixes.** Add the missing focused producer controls, then re-review that test delta. Full backend, serial PostgreSQL, global gates, and final cross-task review remain parent-owned gates; this is not merge approval.

## Strengths and specification evidence

1. **Canonical equality follows admission and recovery proofs.** `src/elspeth/web/composer/pipeline_commit.py:531` catches only `ToolArgumentError` around candidate construction. Candidate acceptability, owned proposed-content identity, recovery-pair presence, and recovered executor-content identity precede the equality branch at line 554. A matching recovery binding is carried on `NO_CHANGE`; fresh equality reaches neither `begin_dispatch` nor the executor. The new tests at `tests/unit/web/composer/test_pipeline_commit_operation_authority.py:214`, `:474`, and `:498` exercise fresh/matching/mismatched/missing-pair cases, a real current-policy rejection of equal content, and the actual malformed public candidate builder.

2. **Argument refusal preserves audit meaning.** The executor catch at `pipeline_commit.py:610` leaves the actual dispatch recorder's one ARG_ERROR buffered and creates no SUCCESS-only binding. The settlement path at `src/elspeth/web/sessions/routes/composer/pipeline_settlement.py:468` persists that buffer before required rejection. Build-time failure has no executor invocation. The exact category/payload assertions at `test_pipeline_commit_operation_authority.py:375` were retained while changing the expected wrapper. The HTTP matrix at `tests/unit/web/sessions/test_pipeline_rejection_callers.py:50` exercises public/owned fresh equality, persisted successful recovery, build argument refusal, and executor argument refusal through actual preparation/settlement and real storage. Argument errors are injected at the build/executor sites in that HTTP matrix; the real builder malformed-input test and real dispatch audit production supplement it.

3. **Terminal refusal uses actual rejection custody.** `pipeline_settlement.py:495` maps only the new codes into the existing required rejection operation, adding `no_change` to the two closed vocabularies and reusing `validation_failed` for argument refusal. The response at line 528 is constructed after required custody and deferred-cancellation handling. The mounted HTTP tests pin the exact legacy `error_type`/`detail` text, repeat-terminal 409, unchanged durable state, one event, actor, and recovery binding. Legacy copy was compared with `src/elspeth/web/sessions/routes/composer/proposals.py:564` and `:630`. The real SQL cancellation test at `test_pipeline_rejection_callers.py:541` preserves original exception identities, the registered child's ordinals, actual SQL/projection completion, and exact terminal event. The closed-reason round-trip at `tests/integration/web/composer/test_pipeline_proposal_lifecycle.py:1238` now covers `no_change` without changing schema or lifecycle.

4. **Planner no-change remains honest and durably audited.** `src/elspeth/web/composer/planning_application.py:662` returns only after the entire existing audit persistence call, before proposal creation, live trust lookup, preflight, or commit intent. It retains original state and explicitly sets `raw_assistant_content=""`. The cohort test at `tests/unit/web/composer/test_audit_cohort_settlement.py:434` now runs equal and changed controls across every write failure index, checks actual invocation occurrences, and stores/reloads the synthesized assistant row through the real publication/history helpers. The cancellation matrix retains both cases. The scripted-provider test at `tests/unit/web/composer/test_pipeline_planner.py:1024` pins one call, repair count zero, accepted DONE, and no execution invocation for an already-current candidate. Stage-2 fixtures now carry changed matching authority and retain their original preflight assertions; equal explicit, auto, and downgrade cases separately assert no preflight/proposal/intent at `test_planner_stage_runtime_preflight.py:264`.

5. **Complete-AUTO uses the existing unapplied outcome.** `src/elspeth/web/composer/tool_batch.py:1822` checks the already-finalized acceptable candidate, excludes prepared inline custody, applies normal result enrichment, and compares exact composition hashes before creating `_UnappliedAdmission`. Its existing dispatch consumer still records one actual feedback invocation, retains original version/state/validation/preflight, avoids mutation publication and pending-review preflight, and keeps anti-anchor accounting neutral. The expanded actual compose-loop test at `tests/integration/web/composer/test_freeform_proposal_prevalidation.py:1687` covers singleton/named-source equal candidates in both trust modes with executor prohibition, one invocation, original state/version, unchanged progress, and no delta. The new profile-rejection control at line 1754 proves nonacceptable equal-shaped input does not enter that shortcut. Ordinary incremental AUTO and changed canonical builder-count controls remain present.

## Findings

### Critical

None found.

### Important

**I1 — Complete-AUTO finalization/provenance near-equality cases required by the corrected brief are not pinned on the new route.**

References: `src/elspeth/web/composer/tool_batch.py:1822`; `tests/integration/web/composer/test_freeform_proposal_prevalidation.py:1687`, `:2143`, `:2333`, `:2388`; `tests/integration/web/composer/test_freeform_required_controls.py:448`, `:557`; corrected brief's “Complete freeform set_pipeline auto is also owned here” correction.

The new equal AUTO test uses a plain candidate that remains equal through finalization. Its changed AUTO companion starts from `_incremental_base_state`, an incomplete graph with no outputs, and therefore does not challenge equality versus a complete current graph. The existing required-control AUTO cases also start from that incomplete base. They prove ordinary changed execution and wiring, but cannot detect an equality check moved before required-control/review finalization when the provider's raw complete candidate already matches current state.

The stronger existing `test_equal_incremental_edit_stages_required_controls_before_hash_comparison` is explicit approval and uses `set_metadata`; it never enters this new non-explicit `set_pipeline` arm. Likewise `test_inline_same_text_with_new_blob_authority_is_a_real_change` uses `_harness`'s explicit-approval default (`:208`) throughout and asserts two proposals. It does not test new inline identity through AUTO's new no-change admission. The new patch adds no AUTO variant of those controls. The deliberate mutations establish shortcut presence, recovery ordering, acceptability, and audit-cohort ordering; they do not supply this missing route-specific proof.

This matters because skipping required controls/review provenance or suppressing a new inline identity would be a consequential false no-op, and the user explicitly required producer tests for these behaviors. The implementation currently appears correctly ordered; the finding is incomplete acceptance evidence.

**Requested fix:** add focused real compose-loop AUTO controls built from a complete current state: (a) raw content otherwise equal where real required-control/review finalization changes authority, asserting execution/publication of the finalized change rather than unchanged admission; (b) same inline bytes with a newly created identity/provenance, asserting actual custody and changed state/version, one actual audited call, and no false unchanged feedback. Include a review/provenance-only change as needed to pin the corrected brief's named cases, using real builders/finalizers/custody rather than a mocked candidate. Retain the existing simple changed-builder and ordinary incremental AUTO controls. Run only the new focused cases and relevant existing controls for this test-only delta; broad suites remain the parent's responsibility.

### Minor

None raised.

## Verification evidence and limits

Independent read-only measurements during review:

```text
git rev-parse HEAD
e3d07d59d56305bf807f4a133275f3a5dbdafb89
sha256sum d1-canonical-review.diff
a326e9a51b990791b800c1be19485358a4d15bdb6abf42348910b3e2ebb349ab
git --no-optional-locks diff --cached --binary | sha256sum
a326e9a51b990791b800c1be19485358a4d15bdb6abf42348910b3e2ebb349ab
sha256sum -c d1-canonical-logs/focused-final-before.sha256
all 13 named paths: OK; command exit 0
```

I read the terminal summaries in the supplied frozen-run logs: `focused-final.log` reports 714 passed with the two documented dependency warnings; `authority-gates.log` reports 547 passed / 3 skipped / 1 xfailed; `frontend.log` reports 190 passed; `post-mutation-green.log` reports 58 passed. The four mutant logs show their stated intended failures (7, 2, 1, and 7 respectively), and the restoration records plus the independent hash check match the reviewed candidate. Ruff, mypy, and contracts logs report their stated successful outcomes. These are existing implementer-run results, not newly executed review suites; their shell exit statuses are documented in the handoff report.

The tier comparison artifact records 5220 before / 5220 after, zero semantic additions/removals, and controlled comparison including duplicate multiplicity and semantic fields. I did not repeat that scan or independently rerun the comparator. It is a bounded unchanged-corpus comparison, not a clean-corpus or signed-clearance result.

The parent's later replacement of the frontend dependency symlink with an ignored physical copy is environment preparation outside this patch. The 13-file hashes and staged diff remained exact during this review.

## Named cross-function inspection outside the patch

- Read canonical preparation and settlement surroundings to trace recovery lookup, buffered ARG_ERROR persistence, and refusal/cancellation ordering.
- Read `_finalize_complete_set_pipeline_candidate`, `_unapplied_admission`, and their dispatch/result consumers to check finalization count, state/preflight preservation, neutral anti-anchor behavior, and actual feedback auditing.
- Read `tools/_dispatch.py::finalize_tool_result` and `tools/sessions.py::SetPipelineCandidate.acceptable` to check that the new shortcut uses normal enrichment and real admission semantics.
- Read the legacy proposal error construction solely to verify public text parity.
- Read the named existing freeform producer tests and harness solely to distinguish incomplete-base changed controls from the complete-AUTO near-equality controls in I1.

No broad source scan or suite rerun was performed.

## Declined to judge

- Full backend and serial PostgreSQL readiness: explicitly assigned to the parent gates; existing focused SQLite evidence is not a PostgreSQL claim.
- Global all-rule and operator signature clearance: explicitly separate parent/operator gates; the existing corpus remains non-clean and unsigned here.
- Final interaction across every D1 tranche: the parent owns the cross-task review. This review checks this frozen patch against its exact predecessor and the named integration seams above.

## Scoped follow-up: I1 fix round 1

**I1: ADDRESSED. Specification verdict: APPROVED for this task. Code-quality verdict: APPROVED.** These scoped verdicts supersede the initial changes-requested/task-acceptance verdicts above. No new Critical, Important, or Minor finding was identified in the fix delta. The previously stated parent-owned full/backend/PostgreSQL/global/cross-task gates remain separate; this is not merge or full-D1-closure approval.

Reviewed the appended “Review fix round 1 — Important I1” report and the complete `d1-canonical-fix1.diff`, SHA-256 `696e872ed9df1fb71b144b338c506eca5a9f104e6317cbc59ac9f77e8d19e0ed`, against exact pre-fix index tree `ccec0218c209389d48e142baff31a3ffebd879ab`. This delta adds 221 lines to the existing `tests/integration/web/composer/test_freeform_proposal_prevalidation.py`; it changes no existing test assertion or production code.

The new `test_complete_auto_raw_equality_still_applies_required_control_and_review_finalization` at line 2333 closes the finalization-order gap. It obtains a complete current graph through real tool execution, serializes its public arguments, and observes real candidate builders. It proves that the first acceptable raw candidate equals current content while the finalized candidate differs. It then requires exactly one real finalizer/executor call, changed content and version, control nodes and canonical review disclosures in the actual state and audit arguments, durable state publication, one successful invocation, no unchanged feedback, and unchanged authored input. Its wrappers observe the real producers rather than supplying candidate results.

The two cases of `test_complete_auto_same_bytes_new_source_authority_is_published` at line 2424 close the custody/provenance gap. The inline case starts from a real prior AUTO publication with its persisted head id, repeats the same bytes through actual inline custody, and requires two distinct ready artifacts and the newly published identity. The existing-blob case uses two real stored artifacts and a complete real tool-built current graph, isolating the replacement identity. Both assert that non-identity source options and remaining graph content stay equal while content hash/version advance, one real executor invocation runs, feedback does not claim unchanged, and the durable current state contains the new identity. The inline case additionally pins the actual invented-source review row. Together these cases distinguish near-equality from ordinary changed-from-incomplete execution, which was the original gap.

Read the terminal supplied evidence: `fix1-mutant-premature-final.log` shows the required-control case fails and both identity cases pass (1 failed / 2 passed); `fix1-mutant-identity-loss.log` shows both identity cases fail and the control/review case passes (2 failed / 1 passed). These targeted controls challenge the precise false-no-op regressions described in I1. After restoration, `fix1-affected-module.log` reports 97 passed, including the new cases and affected existing controls; `fix1-mock-hygiene.log` reports 3 passed; scoped Ruff/format records are successful. Their terminal exit codes are recorded in the handoff report. No review test rerun was needed.

Independent read-only measurements returned exit 0: HEAD remains `e3d07d59d56305bf807f4a133275f3a5dbdafb89`; the cached fix delta has exactly one changed test module / 221 insertions; `git diff --cached --exit-code ccec0218c209389d48e142baff31a3ffebd879ab -- src` is empty; the cached binary fix delta hashes to the stated `696e872e…` digest; all 13 entries in `fix1-final-before.sha256` verify OK. The refreshed full review artifact and cached binary diff independently hash to `3d0d7b5942cce2c8046869aa699c33a17b6ee03c116354165b6630840c5dd4d5`; the initial full-diff digest above remains historical evidence of the original review.

This follow-up was limited to I1 and new breakage in its test-only delta. It did not re-audit the approved production patch, rerun existing suites, change source/index/HEAD, or dispatch subagents. The unchanged production retains the original focused/authority/frontend/static evidence with its original limits.

