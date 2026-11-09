# Composer orchestration, tool and plugin assessment

**Assessment baseline:** `3b28d537be98234da8135c418bf185a998c18b68`, restored
ELSPETH 0.8.9, inspected 2026-11-09. **Status:** reconnaissance and structural
remediation design complete; production implementation is separately scoped.
The abandoned full `.10` patch is not an authority for this work. Incoming
`.10 lite` changes require targeted reinspection of affected findings.

## Deliverables and reading order

| Deliverable | Contents |
|---|---|
| [Live inventory and 46 tool sheets](inventory.md) | Measured registration/exposure reconciliation and one standalone assessment for every distinct Web Composer tool |
| [Architectural synthesis](synthesis.md) | Source-linked recurring causes, current strengths, ownership boundaries and consequences for orchestration |
| [Structural remediation plan](remediation-plan.md) | Priorities, dependencies, responsible layers, affected components, compatibility and positive/rejection acceptance criteria |
| [Evidence and reproducible probes](evidence.md) | Exact local selections/results, controlled observations, independent-review dispositions, full probe source and limits |

Each tool sheet covers purpose/selection/prerequisites, arguments/defaults and
results, effects/approval/cache/retry/concurrency/idempotency, registration and
execution trace, alternative/rejection/recovery paths, classified findings,
structural ownership/remediation, and evidence. An appendix retains the live
argument schema and measured route membership so the sheet is usable on its own.
Semantic parser requirements can be stricter than transport syntax; the sheet
describes that distinction rather than treating wire JSON Schema as the whole
contract.

## Main assessment

Establish the first-class orchestrator by strengthening the existing invocation,
observation, candidate-admission and settlement contracts. Preserve the current
provider authorship, plugin-owned schema emission, policy snapshots, required
controls, review evidence, session fencing and durable operation replay.
The evidence supports bounded structural changes around those seams, not a
replacement agent framework.

The orchestrator needs dependable answers to four questions: what capabilities
and resources are actually available, whether their observations are complete,
what a tool changed or refused, and which approval/evidence/effect obligations
remain before completion. The current system provides many safeguards, but
route-specific optional context and uneven tool/result semantics leave those
answers inconsistent in the cases below.

## Findings register

These are finding identifiers, not a count of bugs. Demonstrated behavior,
intended restrictions and hypotheses remain distinct in the linked sheets.

| ID | Classification | Finding / affected tools | Structural work |
|---|---|---|---|
| SG-07 | Demonstrated public admission defect | [`upsert_node`](tools/upsert_node.md) drops forbidden queue scope fields before checking their owning invariant | R1 immediate preserve-before-validate slice; R5 kind-specific candidates |
| GS-01 | Source-demonstrated route defect with dispatcher controls | [`diff_pipeline`](tools/diff_pipeline.md) is advertised by freeform, whose call omits its required baseline; full-service probe did not reach dispatch | R1 explicit invocation dependencies/baseline semantics |
| BOS-01 | Demonstrated discovery defect | [`list_blobs`](tools/list_blobs.md), [`list_composer_blobs`](tools/list_composer_blobs.md) silently omit records beyond 50 | R2 complete/scoped observation and continuation contract |
| BOS-03 | Demonstrated metadata mismatch; other callers source traced | [`get_blob_metadata`](tools/get_blob_metadata.md), singular/plural source binding and inspection accept UUID forms then query raw text | R2 shared canonical resource identity |
| GS-02 | Demonstrated response-promise mismatch | [`explain_validation_error`](tools/explain_validation_error.md) omits matched code on the exact branch while returning it on fuzzy match | R2 branch-complete result contract |
| GS-03 | Source-confirmed user-facing capability defects | [`list_models`](tools/list_models.md) and [`get_audit_info`](tools/get_audit_info.md) tooltips overclaim credential checks/session lineage | R2 capability/description parity |
| SG-02 | Demonstrated undisclosed precondition; policy itself intended | [`splice_transform`](tools/splice_transform.md) refuses an otherwise valid insertion when unrelated standing failures leave the whole candidate invalid | R5 explicit draft-validity policy; disclose current restriction as containment |
| SG-03 | Demonstrated lifecycle inconsistency | Empty metadata/node patches advance version and count as progress while empty source patch does not | R4 explicit semantic no-op/progress |
| SG-04 | Demonstrated internal prerequisite inconsistency | [`inspect_source`](tools/inspect_source.md) can read without the custody context required by sibling content reads; no public bypass/race demonstrated | R1 dependency admission; R3 shared streaming snapshot read |
| BOS-02 | Demonstrated fault-semantics divergence | [`get_blob_content`](tools/get_blob_content.md), [`read_attachment`](tools/read_attachment.md) soften ready missing bytes into ordinary tool failures while blob service raises an owned fault | R3 common safe category/actionability, not mandatory raw exception exposure |
| BOS-04 | Source-established effect/retry limitation | Blob create/update/delete can change storage without graph version change; new-call retries lack a general advertised replay identity | R4 effect receipts retaining existing durable drivers |
| BOS-05 | Source-established resource-work limitation | Content paging/attachment/preview can materialize full files before bounded output or size refusal | R3 purpose-aware streaming/snapshot verification; impact unmeasured |
| SG-01 | Intended structural limitation | Visual processing edges and authoritative executable destinations differ; deletion does not infer reconnections | R5 explicit executable projection versus visual annotations |
| SG-05 | Intended disclosure restriction with planning limitation | Plugin listings omit transient/unusable prerequisites beyond categorical prohibitions | Consider authorized bounded availability evidence; preserve private profile policy |
| SG-06 | Source-established ownership/design finding | Candidate/plugin/profile/review admission repeats across authoring verbs; `clear_source` review consequence remains unverified | R5 shared staged admission with plugin/web/review owners kept distinct |

Unresolved hypotheses include permitted source-inspection race schedules,
`clear_source` review reconciliation consequences, and secret-reference
observation interleavings. They are not proven security or runtime defects.
The recovery sheets separately document a 100-park advertisement limit without
claiming loss of older parked graphs.

## Review and verification

Independent lanes investigated source/graph, blob/output/secret, and
generation/session/async tools. The parent traced orchestration and the three
recovery/planner controls, reconciled the runtime inventory and checked decisive
claims. Astra challenged the method and synthesis/plan. Peer reviews crossed
authorship boundaries; corrections were integrated. This independence supports
the assessment, not a claim of exhaustive correctness.

The [evidence record](evidence.md) reports completed local tests separately,
including the initial Azure prerequisite failures and their focused rerun.
No full suite, PostgreSQL contention selection, live provider, deployed service
or browser acceptance was run. No production source, test suite, policy,
dependency manifest or lockfile changed. The documents and probe observations
are ready for review and implementation scoping; none of the proposed fixes has
been implemented by this task.

## Using this assessment after incoming changes

Re-measure live tool names and compare affected handlers, callers, contracts and
test bodies against this baseline. Classify each selected finding as remaining,
changed, corrected or still unverified. Re-run its good/bad controls on the
receiving branch before implementing remediation. A line moving or a newer
release number is not evidence that the defect remains or has been fixed.
