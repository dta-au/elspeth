# Actual Daybreak atomic amendment revision 4 — 2026-10-06

Native CLI metadata records model `gpt-daybreak-blue-latest`, provider OpenAI, read-only sandbox and session `01a1115c-2997-7951-b8a1-5d757de4c9dd`. The process completed exit 0. All 161 captured source/document members and the unchanged authorization transcript were verified after review. This is a plan-only NO-GO: B1–B5/R1/R2 design-resolved, master adoption and exact hung-worker supervisor contract blocking. No amendment implementation or final source clearance is implied.

The complete verdict follows. Source link targets are normalized to repository-relative paths; raw output, native invocation log, exact exit, frozen input and byte identities remain retained in the task workspace. Earlier review/provenance contradictions and separate archive-payload denial remain preserved.

# Revision 4 plan verdict: NO-GO

Revision 4 resolves the substantive B1–B5, R1, and R2 design defects. The two normative documents are mutually consistent and sufficiently concrete. However, the complete plan package is not yet internally authoritative because the master plan neither incorporates revision 4 nor defines the operational supervisor boundary required by R2.

This is a plan-only verdict. The source remains unimplemented, and no tests, providers, network access, environment reads, or writes were performed.

## Dispositions

| Item | Disposition | Finding |
|---|---|---|
| B1 | Design-resolved | Candidate-specific insertion, same-transaction supersession, immutable historical evidence, and one rollback domain are concrete and reuse existing preparation/surfacing seams. |
| B2 | Design-resolved | Closed created-v3/accepted-v2 payloads bind candidate, cohort, actual derived states, final head, transition assistant, and nullable operation authority without timestamp/latest-head inference. |
| B3 | Design-resolved | Replay uses complete semantic material, persisted IDs, explicit produced-state pointers, deterministic transformations, an explicit stopping point, and no writes. Accepted-v1 remains legacy read-only and cannot authorize settlement replay. |
| B4 | Design-resolved | The positive writer predicate adds durable Stop and database-clock deadline checks inside the writer transaction while retaining synchronous/manual behavior when no matching durable job exists. |
| B5 | Design-resolved | The sealed revocation capability binds the exact job, claim, attempt, fence, actor, proposal, dispatch, and candidate. It cannot authorize business publication or accept legacy/unbound authority. |
| R1 | Design-resolved | One canonical authority/key now covers durable COMPOSE, manual PROPOSAL, and synchronous COMPOSE. The 0–50 source table, 15 stages, eight subphases, Cantor recurrence allocation, original root/group preservation, metadata-conflict rule, and public-body rule remove the revision-3 identity ambiguity. |
| R2 | Design-resolved | The gated witness closes the submit/no-return execution uncertainty; valid aborted exit releases admission once, incomplete witnesses quarantine the generation, old capacity is drained off-loop, replacement is sequential, and terminal/renewal/release barriers are explicit. |
| Package/master | **Blocking** | The master plan does not adopt or reference revision 4 and still says implementation is already authorized/in progress under the earlier review. |
| Hung-drain operations | **Blocking** | R2 requires grace-bound supervised termination and old-process-exit-before-readiness, but the package does not identify the exact existing supervisor contract, grace setting, termination request, or acceptance owner. The master also excludes infrastructure changes. |

## Remaining blockers

1. **Make the master authoritative for revision 4.**
   The master currently says “actual Daybreak Blue plan review GO” and “Authorized implementation is in progress” at [the master plan](../../../docs/plans/2026-09-20-composer-async-operations.md:3), and repeats GO at [line 137](../../../docs/plans/2026-09-20-composer-async-operations.md:137). It contains no reference to revision 4, R1, R2, or either normative document. Conversely, the amendment says no amendment code exists and implementation is held pending this review at [line 13](../../../docs/plans/2026-09-20-composer-async-operations/atomic-pipeline-review-amendment-2026-10-06.md:13).

   Required repair: update the master to identify revision 4 and both normative documents as controlling overlays, replace the stale implementation/GO status, allocate their implementation work to concrete N-tasks/owners, and state that this verdict does not prove implementation.

2. **Close the supervisor boundary without leaving an implementation choice.**
   R2 requires a hung generation to stop admission and, after a grace period, cause supervised process termination; replacement readiness must wait for observed old-process exit ([R2 line 70](../../../docs/plans/2026-09-20-composer-async-operations/required-future-custody-design-2026-10-06.md:70)). The master simultaneously excludes infrastructure changes ([master line 7](../../../docs/plans/2026-09-20-composer-async-operations.md:7)).

   Required repair: cite the existing supported lifecycle/supervisor mechanism and exact grace-budget authority, or explicitly add the necessary deployment/lifecycle work to scope. Assign the proof showing readiness remains false until old-process exit. This must not be left for an implementer to invent.

## Source and runtime consistency

The supplied Python 3.13.15 runtime text confirms the critical R2 premise: `ThreadPoolExecutor.submit()` enqueues with `_work_queue.put(w)` before `_adjust_thread_count()` and before returning the Future. Therefore `_adjust_thread_count()` can raise after queueing but before the caller receives custody. The proposed preallocated gate and generation quarantine are appropriate.

Current source also confirms that:

- The shared pool has 16 workers, 16 queued admissions, and releases capacity from the returned concurrent Future ([async_workers.py](../../../src/elspeth/web/async_workers.py:194)).
- Submission currently assumes an exception means admission can immediately be released ([async_workers.py](../../../src/elspeth/web/async_workers.py:212)); R2 deliberately replaces that unsafe assumption.
- Current app shutdown drains the worker pool before disposing the database engine, providing a reusable lifecycle anchor.
- Current publication and later interpretation surfacing are separate operations, matching the amendment’s diagnosed atomicity gap.
- The current mutation predicate checks Stop but does not yet include `deadline_at`, so B4 remains unimplemented.
- Tool-call IDs are strings, including `backend_auto_surface:{UUID}`; revision 4 correctly avoids coercing them to UUIDs while retaining UUID validation for owned UUID columns.

## Security, cancellation, accounting, and invariants

No further design blocker was found in these areas:

- Authority is nominal and scope-bound; manual and synchronous nullability is explicit.
- Unknown SQL completion remains a custody barrier rather than a sortable failure.
- Stop499, database deadline504, SQL/storage/audit priority, accounting markers, and valid-terminal-wins are explicitly ordered.
- Callback order, group order, timestamps, and random UUIDs cannot select a winner.
- Cancellation cannot release an executing or unresolved invocation merely because its async awaiter ended.
- Required audit/terminal work is not cancelled during generation shutdown.
- Closed JSON rejects extras, malformed nominal IDs, invalid nullability, and unknown versions.
- Legacy accepted-v1 replay is read-only and cannot manufacture missing cohort evidence.
- The provider remains on every authoring transition; no server-authored graph or tutorial-only path is introduced.
- Missing original reports and the denied archive payload remain explicit evidence limits, not silently closed ([amendment line 109](../../../docs/plans/2026-09-20-composer-async-operations/atomic-pipeline-review-amendment-2026-10-06.md:109)).

## Mandatory future proof and review

After the two plan repairs and implementation, clearance still requires:

- Exhaustive live registry proof for all source 0–50 mappings, authority/nullability combinations, duplicate keys, and unmapped-source refusal.
- Reversed receipt and original-group order across every category pair, metadata conflict, conflicting public body, and original-object identity.
- Post-queue/no-return, no-entry, delayed witness, impossible witness, callback-install, repeated-anomaly, and release-once controls.
- Measured old-generation shutdown, no overlapping replacement, and supervised hung-process exit-before-readiness.
- SQLite and serial PostgreSQL Stop/deadline/lock/commit-before-projection proofs.
- Atomic cohort rollback and exact read-only replay controls for every interpretation kind.
- Whole-tree gates, full frozen gate, frontend/E2E/local TLS acceptance, historical-obligation reconciliation, Astra correctness review, and final actual Daybreak transport/security review.
- Exact branch/HEAD reporting and continued **John HOLD** for local testing. No merge or deployment.

The mandatory `plan-review` skill informed the reality, architecture, quality, and systems lenses. Its nested-reviewer and report-writing workflow was deliberately replaced by direct read-only self-review as authorized.

**Final decision: NO-GO for the revision-4 plan package.** R1 and R2 are design-resolved; the remaining blockers are master-plan adoption/status consistency and a concrete, in-scope supervisor termination contract.
