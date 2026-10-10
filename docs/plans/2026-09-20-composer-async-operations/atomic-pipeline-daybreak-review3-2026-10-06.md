# Actual Daybreak atomic amendment review — revision 3

Native Codex CLI 0.160.0 invocation identifies `model: gpt-daybreak-blue-latest`, provider OpenAI, read-only sandbox, approval never, session `01a11135-9fff-7500-a40d-cb1c8175de6e`. Completed exit 0. The unchanged authorization transcript accompanied the bounded streaming plan/private-source input; no archive payload or credentials were included. The native banner, full log, full response, explicit exit and 153-file frozen input remain in the consumer workspace.

Disposition: NO-GO. B1–B5 design closures are accepted, not implemented. R1 needs one canonical stage/authority identity shared by the reducer and Future custody. R2 needs complete no-return submission recovery/release/shutdown behavior instead of permanent capacity retention. The previous round's contradictory self-description remains unedited in its separate provenance-limit record; this invocation does not erase it.

The complete verdict below normalizes machine-specific source link targets only. Implementation of the amendment remains held; existing focused tests are not final evidence. No merge, deployment, production or paid-provider test is authorized.

# Revision 3 verdict: NO-GO

B1–B5 are now sufficiently resolved at the design level. R1 and R2 remain mutually inconsistent at their integration boundary, and R2’s no-return submission state has no complete recovery/release contract. Those defects prevent deterministic implementation of this concrete revision.

No implementation, tests, network/provider calls, nested reviewers, source writes, environment reads, merge, deployment, or production actions were performed.

## Individual disposition

| Item | Disposition | Finding |
|---|---|---|
| B1 | Resolved | Settlement-specific insertion establishes a candidate-bound cohort, supersedes only older pending cards, preserves historical terminal evidence, and shares one rollback domain. |
| B2 | Resolved | Versioned closed payloads explicitly bind candidate, ordered cohort, derived states, final head, operation identities, and transition assistant without relying on timestamps or latest-head inference. |
| B3 | Resolved | Semantic replay identity, persisted transport-ID projection, immutable-material checks, explicit stopping pointer, assistant verification, and read-only replay are sufficiently concrete. |
| B4 | Resolved | The proposed positive writer predicate correctly adds database-clock deadline and durable-Stop checks inside the locked transaction while preserving non-job-backed synchronous authority. |
| B5 | Resolved | Creation-v3 plus the sealed revocation operation supplies an exact job/claim/fence/attempt/actor/proposal/dispatch binding and refuses legacy or ambiguous authority. |
| R1 | Blocking | The reducer is locally total, but its receipt-stage identity is incompatible with R2’s normative stage-key taxonomy and does not define manual-settlement receipt identity. |
| R2 | Blocking | The synchronous gated-submission protocol closes the cancellation window, but `SUBMISSION_UNKNOWN` has no complete lifecycle recovery and can permanently consume both shared-pool admission and logical custody. |

“Resolved” above means the revision describes an implementable design closure, not that the frozen source implements or verifies it.

## Blocking repair R1: one canonical stage/receipt identity

R1 defines `ComposerFailureReceipt` with an exact durable operation/fence/epoch/attempt and this fixed stage vocabulary:

- creation binding
- preparation read
- review preparation/validation
- provider-attempt accounting
- token-usage accounting
- required-turn audit
- title accounting
- pipeline publication
- dispatch audit
- revocation audit
- postcommit review reconciliation
- required continuation child
- terminal publication
- lease renewal
- lease close

R2 instead defines `RequiredSQLStageKey` with a different closed vocabulary: ingress, provider admission/settlement, compose checkpoint, proposal creation, interpretation validation, terminal writer read/failure, and others. Lease observations are explicitly outside its SQL-ticket enum. Compare [collision reducer lines 45–60](../../../docs/plans/2026-09-20-composer-async-operations/collision-reducer-design-2026-10-06.md:45) with [Future custody lines 17–19](../../../docs/plans/2026-09-20-composer-async-operations/required-future-custody-design-2026-10-06.md:17).

Consequently, implementers would have to invent mappings such as:

- `PROVIDER_ADMISSION` → provider-attempt accounting or required-turn audit;
- `PROVIDER_SETTLEMENT` → token accounting or required-turn audit;
- `TERMINAL_WRITER_READ` → terminal publication or required continuation child;
- `TERMINAL_FAILURE` → terminal publication, lease close, or a new stage.

Those choices alter the R1 winner because stage ordinal is part of the minimum key. The design therefore does not yet guarantee identical results across implementations.

R1 also requires a non-null “exact durable operation/fence/epoch/attempt,” while R2 expressly supports manual PROPOSAL settlement with null durable-job ID and attempt. The amendment requires that path at [amendment line 74](../../../docs/plans/2026-09-20-composer-async-operations/atomic-pipeline-review-amendment-2026-10-06.md:74).

Exact repair:

1. Define one canonical receipt key used by both documents.
2. Publish an exhaustive mapping from every R2 SQL stage, projection, producer failure, and lease observation to exactly one R1 stage ordinal.
3. Represent manual settlement explicitly—such as a closed authority-kind discriminator plus nullable durable-job fields—rather than contradicting R1’s required durable-operation identity.
4. Specify that an unmapped stage or invalid nullability is an integrity failure before reduction.
5. Test every mapping and reverse receipt arrival/group order.

R1’s same-category handling itself is adequate: receipts are deterministically selected by declared keys; multiple audit witnesses within a receipt use common validated metadata or the fixed metadata-conflict body; conflicting same-kind HTTP bodies use generic500; original roots/groups remain intact.

## Blocking repair R2: close the no-return lifecycle

The preallocated gate is a sound way to prevent SQL from entering before ticket binding. It also makes a post-enqueue/no-return `submit()` exception safe for database effects: the caller aborts the gate, and any queued wrapper observes `ABORTED` instead of running SQL.

The remaining problem is resource and ownership finalization. The design says:

- retain `SUBMISSION_UNKNOWN`;
- keep physical admission charged;
- forbid terminal, transition-lock, lease, and logical-concurrency release;
- do not release admission even when the gated wrapper later supplies a completion/nonexecution witness;
- defer recovery to a “separately authorized measured lifecycle recovery” that is not designed here.

See [Future custody line 46](../../../docs/plans/2026-09-20-composer-async-operations/required-future-custody-design-2026-10-06.md:46) and [lines 87–91](../../../docs/plans/2026-09-20-composer-async-operations/required-future-custody-design-2026-10-06.md:87).

That is fail-closed for integrity but incomplete operationally. Repeated no-return anomalies can permanently exhaust the process-wide bounded admission pool, while affected turns retain leases/transition/CRL ownership indefinitely. The current pool is explicitly bounded and admission normally follows actual Future completion in [async_workers.py:194](../../../src/elspeth/web/async_workers.py:194) and [async_workers.py:220](../../../src/elspeth/web/async_workers.py:220).

Exact repair:

1. Define the owned gate-wrapper witness as a state machine created before `submit`, including entered, observed-ABORTED, exited, and impossible-state handling.
2. State precisely whether wrapper exit after observing `ABORTED` proves this invocation’s callable and SQL never executed.
3. If it does, authorize a release-once path for that admission and define when transition/lease/logical custody can move from unresolved to a terminal recovery result.
4. If it does not, include the promised measured lifecycle recovery in this amendment: owner, authority, durable marker, triggering conditions, resource-release rule, and process-restart behavior.
5. Define shutdown behavior when an unresolved ticket prevents `prepare_lease_release`.
6. Add repeated no-return controls demonstrating that neither SQL runs nor bounded capacity leaks after sufficient proof becomes available.

A design may deliberately retain capacity forever, but then that denial-of-service behavior and its operational recovery must be part of the approved architecture rather than deferred beyond the implementation scope.

## Required verification after repair

The existing test obligations remain appropriate, including:

- all B1–B5 atomicity, replay, legacy, operation-binding, and mutation-removal controls;
- pairwise R1 collisions in reversed arrival and group order, including metadata/body conflicts and original object identity;
- actual cancellation at every R2 submission boundary;
- callback-install failure and completed-inline callbacks;
- queued versus running Futures and commit-before-projection;
- both pre-queue and post-queue/no-return submission exceptions;
- repeated no-return events proving bounded admission and logical custody behavior;
- actual SQLite and PostgreSQL lock/Stop/deadline cases;
- affected whole-tree gates, full frozen gate, frontend/E2E/TLS, and historical-obligation reconciliation.

Focused green tests would not constitute final approval. The two unavailable original reports and denied archive payload remain evidence limits, and no unknown issue from them is closed.

The local `plan-review` skill informed the reality, architecture, quality, and systems lenses. Its normal delegation and report-writing workflow were intentionally not used because this invocation expressly required direct read-only self-review.

**Final decision: NO-GO for revision 3.** Exact branch/head remains held for John.
