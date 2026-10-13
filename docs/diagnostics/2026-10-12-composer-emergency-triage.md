# Composer emergency diagnosis: current-source disposition

The principal diagnostic loss is confirmed: Composer stores display
projections in place of ordinary authored arguments and results. This is
an evidence-retention defect, not evidence that the discarded production
candidate can be reconstructed. D20 is still open. The concrete repair
design is in [Composer evidence loss](2026-10-12-composer-evidence-loss.md).

## Scope and provenance

This investigation uses base
`fdaa670394fd311fc5df61befb1a7b1941c01182` on
`codex/emergency-diagnostics-20261012`. The supplied October 11 incident
reports identify deployed source
`1dec423d306513f28325573cd07e164196cb5686`. Current-source checks below do
not establish equivalence with that deployment or reproduce its live
session. Tests use synthetic inputs and the intended worktree's two Python
source roots. This diagnostic repair phase used no paid provider calls or
production database reads. The subsequent local Composer exercise uses real
provider calls and is reported separately in
[Composer exercise](2026-10-12-composer-exercise.md).

## Findings and repair boundaries

| Report item | Current evidence | Disposition |
|---|---|---|
| D20: missing authored options and rejected candidates | The options walker retains shape/counts only. Normal persistence stores this projection. The alternate invocation serializer overwrites canonical payloads with projections and recomputes hashes. Cancellation uses that serializer too. | Confirmed and unfixed. Retain a separate private, credential-filtered evidence record atomically with display records; cover normal, rejected, malformed and cancelled outcomes. |
| D29: false prior-approval explanation | A real tool-level synthetic candidate with no prior reviews receives unconditional prior-approval guidance on the base. | Repair implemented: candidate and saved-review failures have distinct advice; the shared catalogue no longer infers approval from the generic code. |
| D30: ambiguous requirement IDs and missing valid IDs | The actual algorithm is `user_term.strip() + ':' + node_id`; internal spaces, case and punctuation survive. Unknown-reference errors on the base omit valid IDs. | Freeform repair returns candidate-local IDs and documents the exact algorithm. One-shot feedback carries an owned structured reference inventory, filtered by per-entry finalizer custody. Controlled real-planner/fake-provider retries use that inventory to correct the candidate. |
| D21 / D35: false jagged CSV warning | A synthetic 10,068-byte valid multiline file has full record widths `[3,3,3,3]`; its 8,192-byte prefix produces relaxed widths `[3,3,2]` and a jagged warning. A real jagged record before a cut must remain reportable. | Bounded repair implemented and 256 affected checks pass. Incomplete data tails are excluded from counts, types, URL hints and shape checks; complete headers, genuine jagged rows and relaxed CSV dialect semantics remain usable. |
| D12: running deadline described as queue delay | Current `deadline_expired_error` says the request timed out and directs reload before resubmission. Existing running and queued deadline tests both pass. | Historical queue-attribution wording is already corrected on this base. This does not establish production deployment or richer last-action diagnostics. |
| D31–D34: disconnected draft, lost row context, repeated repair, document consultation | The supplied report describes these outcomes. This investigation has not reconstructed the discarded candidate or the exact provider-message context. | Keep historical findings and uncertainty explicit. Do not claim provider convergence, document omission or atomic multi-call editing has been repaired. |

## Why diagnostic verbosity alone cannot recover the cause

The durable projection happens before session persistence. Raising log
verbosity after that point cannot recover option keys, prompt parts,
interpretation drafts or rejected inputs. A hash over a projection binds
the projection; it does not preserve the removed original.

There is a separate loss at provider-error boundaries: planner/gateway paths
record exception classes as both the class and message, and planner/guided
wrapping suppresses ordinary causal traceback display. Python's suppressed
exception context can still exist in memory. Other public diagnostic
surfaces deliberately collapse exceptions to classes. These require
purpose-specific operator diagnostics at the point of failure; dumping
arbitrary exception strings, credentials or request objects is not a
complete repair.

The existing conversation response can return stored tool-call envelopes.
Removing the options summarizer would therefore expose full authored
content through a display path and leave alternate/cancelled storage
lossy. The D20 design separates durable evidence from display and supplies
explicit session ownership and access attribution for its retrieval.

## LLM engineering interpretation

The report's call timeline supports investigating repair feedback and
context continuity before altering timeouts or token budgets. It does not
measure hidden reasoning time. Reported output usage is not a reasoning
trace, and absent cache counters are not proof of absent provider caching.
The reported gateway alias does not establish that configured reasoning
effort reaches the actual upstream model. Provider controls require an
end-to-end wire and capability check before changing them.

Every proposal remains provider-authored. These repairs change validation
feedback and source-inspection facts; they introduce neither server-authored
pipeline structure nor tutorial-specific execution.

## Verification and limits

The initial freeform regression failed for both `upsert_node` and
`set_pipeline` on the false prior-approval assertion. Saved-authority
provenance and one-shot missing-feedback controls also first failed before
their repairs. The affected planner/state/reconciliation/prompt/guidance/
response selection then exited 0 with `1311 passed`; structural and envelope
gates exited 0 with `106 passed` on that tree.

The first post-change keyless lint comparison found one new defensive
`isinstance` guard on the owned optional carrier. Review confirmed that
silently discarding a malformed first-party carrier hid an internal bug.
The consumer now reads the owned contract when it is non-None. Its new
corruption control first failed against the old guard, then passed.
The final formatted tree passed `25` focused reference/custody/planner
checks, `8` attribute/live-masquerade checks including red controls, contract
alignment, Ruff and formatting. Final scanned inputs were identical before
and after these runs. The earlier `1311`/`106` selections were not rerun for
this narrow correction.

Both baseline and final keyless all-rule scans exited 1 with `3156` findings.
A multiset comparison of rule, path, severity, message and suggestion found
no additions or removals; line/column/fingerprint movement was excluded.
The comparator passed reorder-negative and added/removed-positive controls.
The final corpus contains no `trust_boundary.tests`, `.scope` or `.tier`
findings. This is a baseline-relative result, not a clean lint corpus or
operator signature clearance.

The current running/queued deadline selection exited 0 with `2 passed`.
The CSV reproduction and its genuine-jagged controls exited 0. The final
CSV inspection/response-contract/blob-promotion/header-integration selection
exited 0 with `256 passed`; Ruff and format checks passed. These are
bounded local checks. Full Python and PostgreSQL contention suites, CI,
operator signature clearance and deployment have not been performed by this
diagnostic task. The subsequent local exercise establishes only the authoring
acceptance described in its separate report; it does not execute a pipeline.

The branch has no publication or integration claim. D20 remains open until
evidence construction, atomic persistence across outcomes, and private
retrieval/public isolation are implemented and validated together.

The subsequent [orchestrator review](2026-10-12-orchestrator-diagnostic-review.md)
found one new P2 cleanup-attribution gap using the production JSON logging
processors. That review made no source edits and does not establish another
Composer convergence defect.
