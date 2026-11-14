# Final combined D1 review

Specification verdict: **APPROVE for the frozen D1 source scope.** The combined implementation follows the user-authorized direct-refusal/no-change design, including the distinctions between partial-draft admission, complete canonical admissibility, and external blob effects.

Code-quality verdict: **APPROVE for the frozen D1 source scope.** No new Critical, Important, or Minor finding was identified. This is a source-review verdict, not a claim that the parent-owned final integration gates have completed.

## Immutable scope and method

Reviewed on 2026-11-09 in `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`:

- Base: `5e192342c3fb025fd9a91244bebe437158e9de8e`.
- HEAD: `14fef5e5342ccbd79b4fdd69b276c20f77d66dc1`.
- Package: `d1-combined-review.diff`, 507864 bytes, SHA256 `373260745252a85c4cc95400507812db3aaaef31066caa4635434df229eef830`.
- Independently checked HEAD, package SHA256, and clean worktree status. Read the entire 10424-line package in bounded chunks, including changed tests and the inventory updates; reread portions whose initial aggregate tool output was truncated.

Read the final-review brief first, the D1 implementation plan and authorized design/addenda, relevant AGENTS/CONTRIBUTING guidance, requesting-code-review skill, progress ledger, per-task reports and final reviews. Followed cross-task risks into complete changed production functions and concrete unchanged dependencies where patch context was insufficient. No tests, scanners, source/index/HEAD mutations, or subagents were run. This report is the only review write.

## Findings

- **Critical:** None identified.
- **Important:** None identified.
- **Minor:** None identified.

The earlier Important findings recorded in task reviews are closed by the included fix rounds: same-batch publication-race/cap accounting, legacy equality after real required-control finalization, and complete AUTO near-equality controls preserving newly required controls, review requirements, and blob identity changes. They are not parked exceptions to this verdict.

## Cross-task assessment

### Admission, publication, and truthful outcomes

`src/elspeth/web/composer/tool_batch.py` preserves a single actual preview result for pure-state/marker admission and consumes it at dispatch. `_unapplied_admission` returns original state and standing observations, empty affected nodes, and no fabricated applied-component/prior-validation evidence. Known refusals contribute failure feedback; no-change is neutral to anti-anchor success/failure accounting. The post-dispatch branch does not install candidate state or candidate preflight for unapplied results.

Complete canonical candidates pass the real finalizer and admissibility checks before equality. Required-control changes are promoted to canonical authority where necessary; ordinary incremental AUTO version behavior remains distinct. A partial draft's unrelated invalidity does not become a new universal prohibition on useful incremental proposals.

The proposal cap follows semantic admission and precedes inline custody/publication. A failed publication does not consume a successful proposal slot. Cap-error evidence preserves the actual invocation prefix and does not fabricate an additional dispatched invocation. Both freeform publication APIs catch only `ProposalResourceUnavailableError`; the resulting cached refusal is finished once and is not redispatched.

These interactions are pinned in the freeform prevalidation integration tests, including both tool-call dialects, refusal/no-change followed by repair, ten-slot accounting, at-cap refusal/no-change, real publication races, same-batch race followed by ten successful proposals, secret-policy and marker-context fidelity, standing invalid drafts, and control-finalization promotion. The canonical fix-round controls exercise equality-looking inputs that must still produce real control/review/blob-identity changes.

### Blob inspection and subsequent resource races

`tools/blobs.py::admit_blob_mutation` uses BLOB_CUSTODY and real session authority, observes debt/metadata under the coordination fence, inspects files outside the Sessions transaction, then reobserves the protected snapshot. It does not execute the destructive handler or recover debt as a preview. Admission returns only a refusal result or an admitted signal; the actual effect remains deferred.

`coordination/repository.py` shares replacement/deletion prerequisites between admission and execution while retaining authoritative prepare/commit rechecks. Replacement quota includes real accounting and reservations; retention checks include current graph and competing retained references without excluding a future proposal that does not yet exist. File/debt helpers in `blobs/replacement.py` and `blobs/service.py` preserve exact path, phase, metadata, hash, and artifact checks. Owned inconsistency remains a fault rather than a recoverable user refusal.

Publication can still race with resource closure after admission. `sessions/proposal_blob_refs.py` classifies only missing, foreign, or known nonready resources as expected unavailability, after validating owned status type/membership. Corrupt statuses and malformed owned authority are not silently converted. Acceptance retains receipt-first blob recovery and excludes blob-store-only effects from graph equality, so same-text replacement remains a real identity/effect operation.

The blob tests assert unchanged rows/files and prohibit destructive admission paths, include positive mutation oracles, and cover actual interrupted producer phases, supported artifact alternatives, corruption, expired/wrong authority, every relational retention prerequisite, and quota/reservation boundaries. The supplied seven PostgreSQL cases address genuine independent-engine lock waits, metadata/fence changes across file IO, reservation visibility, and custody blocking. No PostgreSQL run was repeated for this review.

### Terminal acceptance, recovery, and cancellation

`composer/pipeline_commit.py::prepare_pipeline_proposal_commit` checks authority, base/hash, owned candidate/review restoration, candidate admissibility, and durable recovery identity before no-change. A valid recovery binding remains available when equality is settled; missing or mismatched recovery pairs cannot be bypassed. Typed candidate/executor argument failures remain argument failures, and only successful execution supplies successful recovery evidence.

`sessions/routes/composer/pipeline_settlement.py` maps the new closed outcomes to terminal rejection and the documented public error contract, persists actual buffered invocation evidence before rejection, and retains required-work/cancellation obligations. `sessions/routes/proposals.py` applies legacy equality after required-control finalization, settles typed argument refusals terminally, preserves a concurrent winning outcome, and keeps original child/caller errors and cancellation. Existing blob receipts precede replay/equality checks. The frontend refreshes terminal failure state with the existing current-session/generation guard, preventing a stale pending acceptance prompt.

The route, authority, settlement, and frontend tests cover exact actor/event counts, original state/preflight, repeated acceptance, required-work SQL/projection custody, matching/mismatched/missing recovery evidence, unacceptable equal candidates, real malformed arguments, competing winners, cancellation identity, and real finalization changes. Historical manually staged create-blob receipt behavior is not presented as repaired by this tranche.

### Planner persistence and required creation

`composer/planning_application.py::_stage_pipeline_plan` persists its cohort before returning an equal finalized candidate, emits an empty `raw_assistant_content`, and avoids claiming a new provider response, Stage2 result, or proposal. Changed plans retain live trust intersection and preflight prerequisites.

Expected resource failure in required creation is converted only after `project_creation_handoff` verifies the actual creation outcome, completes the unused projection ticket, and establishes no returned row, one exact expected error, and no deferred cancellations. Returned rows with post-SQL/projection failures are not reclassified as refusal. Inspection of `sessions/pipeline_rejection_custody.py::project_creation_handoff` and the direct deferred-cancellation helper confirms that the narrow new catch does not erase those obligations. Planner tests cover equal/changed cohorts and failure positions, truthful stored history, direct/required resource races, returned-row telemetry faults, and original cancellation.

### Verification support changes

The conditional-splat scanner walks both conditional arms recursively while retaining fail-closed treatment of unreadable mappings and existing ownership/cast rules. Real-producer mutation controls distinguish unknown-key detection from a generic scanner failure. The two diagnostic forwarder additions are exact expression/function/path tuples, with near-miss negative controls and catalogue provenance; they do not create an arbitrary forwarding exemption.

The authority manifest changes in the combined patch are line-position maintenance, not changed fingerprints, authority policy, or new escapes. Neither this review nor the patch supplies operator signatures or claims existing generic authority debt is cleared.

## Evidence and remaining limits

Read the supplied task evidence and final independent reviews with their freeze/scope qualifications. Representative completed evidence includes canonical 714 passed and authority 547 passed / 3 skipped / 1 generic xfailed, canonical fix-round affected module 97 passed, blob 271 / 295 / 145 selections plus seven PostgreSQL cases, publication-race 202 / 44 / 301 selections plus the 88-test fix round, state-admission 112 passed, legacy 93 affected plus 45 existing checks, scanner final 664 passed, and frontend 190 targeted plus 4447 full-suite and 26 dependency checks. These selections overlap and must not be summed as unique tests. Independently read the terminal summaries of the canonical 714, 547, 97, and frontend 190 logs; other evidence is attributed to its saved task reports/reviews and receipts, not to new execution by this reviewer.

Prior failing scanner runs remain accurately recorded as the diagnostic-remediation regression and intermediate repair evidence. The later bounded repair and controls close that issue; the earlier run is not relabeled green. Reused frontend evidence is limited to the supplied unchanged-input manifest and reuse receipt.

At review completion, the parent-owned combined backend, serial PostgreSQL, static, and all-rule semantic comparison receipts have not been supplied to this reviewer. Final integration still requires those receipts and disposition of any new failures. Existing nonzero keyless lint and generic authority inventory debt remain subject to the parent's semantic comparison; this approval is neither whole-tree green nor signed clearance. No concrete uncovered doubt requiring an additional focused test was identified during this source review.
