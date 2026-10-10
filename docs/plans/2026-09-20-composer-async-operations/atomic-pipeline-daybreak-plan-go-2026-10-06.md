# Actual Daybreak narrower plan/source-only verdict — 2026-10-06

Native CLI metadata records model `gpt-daybreak-blue-latest`, provider `openai`, read-only sandbox, approval `never` and session `01a11187-1b0b-7223-baa1-9882bab3404a`. The process completed exit 0. All 73 frozen payload members and their source counterparts were unchanged after review. Payload comprised 30 technical plan documents and 43 relevant source files; eight plan copies omitted nontechnical provenance. No conversation excerpts, authorization evidence, task runtime/log extras, exports/history or archive payload were submitted. The earlier denied action and its exact record remain preserved privately and were not bypassed. This is actual PLAN GO for amendment implementation, not final source/test/merge/deployment clearance.

The complete verdict follows, with only snapshot-local link targets normalized to repository-relative references; the raw native response/log/exit remain unchanged in task custody.

# Concrete revised streaming plan verdict: GO

**GO for amendment implementation planning/execution only.** The revised package closes both Round4 package blockers and is implementable against the supplied frozen source.

This is not source clearance, deployment clearance, or merge approval. The amendment is still unimplemented, the canonical contracts gate is currently red, all listed future proofs remain mandatory, and the completed branch must remain on **John’s LOCAL-testing HOLD**.

I used a direct four-lens adaptation—reality, architecture, quality, and systems—because the installed plan-review workflow normally delegates to four nested reviewers and writes a report, both prohibited by this review’s no-nested-model/no-write constraints. I used no network, provider, tests, environment/config/secret reads, writes, deployment, or merge.

## Round4 package-blocker dispositions

| Round4 blocker | Disposition | Basis |
|---|---|---|
| Package/master adoption | **Resolved** | The [master plan](../2026-09-20-composer-async-operations.md) and [contract](contract.md) now explicitly adopt revision 4, R1, R2, and the lifecycle contract as controlling overlays; preserve prior obligations; distinguish the earlier streaming GO from the amendment HOLD; allocate work to existing N owners; and state that design resolution does not prove implementation. |
| Exact hung-drain supervisor boundary | **Resolved** | The [lifecycle design](hung-drain-lifecycle-design-2026-10-06.md) selects one supported mechanism without leaving an implementation choice: factory-prestarted independent Linux pidfd watchdog, closed private IPC, synchronous bootstrap-failure handling, fixed observation/ACK/helper budgets, permanent post-escalation draining, and actual kernel exit before fresh local-slot readiness. Owners and acceptance witnesses are explicit. |

**Exact remaining plan blockers: none.**

Unsupported pidfd/kernel/security policy in an intended production runtime would become a measured implementation/acceptance blocker; the plan correctly forbids weakening that condition to numeric-PID killing or an in-process timer.

## B1–B5, R1, and R2

| Item | Current disposition |
|---|---|
| B1 | **Design-resolved and retained.** Candidate-specific interpretation cohorts, same-transaction supersession, immutable older evidence, and one rollback domain reuse the existing preparation/surfacing and session-write seams. |
| B2 | **Design-resolved and retained.** Closed versioned created/accepted payloads explicitly bind candidate, operation authority, exact cohort, derived-state chain, final head, and transition assistant. |
| B3 | **Design-resolved and retained.** Replay is exact, bounded, read-only, pointer-based, and fail-closed. It neither selects a latest row nor repairs legacy evidence through writes. |
| B4 | **Design-resolved and retained.** Normal publication authority includes durable Stop and database-clock deadline checks in the writer transaction. Synchronous/manual no-job behavior remains distinct. |
| B5 | **Design-resolved and retained.** Trust revocation is a sealed, exact-operation capability with no generic business-write authority. |
| R1 | **Design-resolved and retained.** One authority/key vocabulary, exhaustive source 0–50 mapping, fixed stage/subphase order, deterministic collision reduction, and original evidence identity remove completion-order and group-order ambiguity. |
| R2 | **Design-resolved and retained.** Submission is gated before callable entry; returned Futures retain actual custody; no-return anomalies quarantine the generation; release is proof-bound and once-only; old generation drains before sequential replacement; unresolved drain escalates through the selected process lifecycle. |

None of these dispositions claims implementation or test completion.

## Implementability and source reuse

The proposed seams exist and are suitable:

- [async_workers.py](../../../src/elspeth/web/async_workers.py) already centralizes the 16-running/16-queued shared executor, admission accounting, real-Future callbacks, and process-wide shutdown. R2 can replace its unsafe submit-exception release assumption at one shared boundary without creating another pool.
- [process_recovery.py](../../../src/elspeth/web/process_recovery.py) is currently only a SIGTERM latch. The proposed owned watchdog ABI is therefore a concrete extension, not a false claim about existing supervision.
- [app.py](../../../src/elspeth/web/app.py) has explicit synchronous construction/failure finalizers and ordered lifespan teardown. Moving recovery/watchdog ownership to the first side-effect boundary is substantial but feasible. The current late `ProcessRecovery` creation and membership-owned draining event must be replaced with the single precreated event and owner required by the plan.
- [readiness.py](../../../src/elspeth/web/readiness.py) confirms the two-second cache and membership-draining check. The specified route-level pre/post-cache checks and separate generation-unavailable latch are necessary and implementable.
- Existing settlement, interpretation surfacing, proposal authority, dispatch binding, database locks, and mutation predicate provide the required B1–B5 reuse points. Current post-commit surfacing and replay-time assistant insertion demonstrate the exact behaviors the amendment must replace.
- Current CLI, Docker, and ACA source confirm that the mechanism can preserve launch topology and deployment arguments. ACA’s 60-second host grace remains independent and is not treated as a self-SIGTERM supervisor.

## Invariant review

- **Security and authority:** GO. Private unnamed socketpair, inherited pidfd, closed scalar-only codec, bounded frames/counts, constant-time nonce comparison, no public endpoint or configuration kill switch, `shell=False`, exact `pass_fds`, and nominal test doubles provide a narrow authority boundary.
- **Custody:** GO. Physical Future/thread custody, logical coordinator/CRL custody, and database fence/lease custody remain separate. Timeouts, wrapper cancellation, signal requests, and process-exit requests do not fabricate completion.
- **Closed payloads and replay:** GO. Versioned decoders reject extras, malformed nominal IDs, invalid nullability, duplicate identities, unknown versions, and incomplete legacy settlement evidence. Replay performs no provider, graph, assistant, interpretation, or supersession writes.
- **Stop and database deadline:** GO. Both are rechecked under normal writer authority. Stop remains distinct from disconnect and local shutdown; a valid committed terminal wins a late Stop.
- **Collision reduction:** GO. Selection is total and deterministic, unknown SQL completion remains a custody barrier, and original roots/groups/causes remain intact.
- **Shutdown and normal completion:** GO. Normal disarm requires an owned completion witness after workers, projections, renewal/release, lifecycle SQL, and engines are finalized. Failed bootstrap arms recovery synchronously before potentially blocking cleanup and does not use `asyncio.run`.
- **Readiness:** GO. Generation quarantine fails readiness immediately; process escalation permanently drains the instance; same-process replacement is allowed only after complete old-generation drain; fresh local-slot readiness requires observed old-process kernel exit.
- **Database recovery:** GO. Remote SQL may commit after client/process loss; restart must reconcile authoritative terminal/current state before sealed recovery. Process exit is never rollback proof.
- **Provider invariants:** GO. No provider replay, second authoring path, server-authored graph, generated-answer delta path, tutorial-only path, or duplicate reconnect dispatch is introduced. Provider authorship remains required on every authoring transition.

## Mandatory implementation and proof gates

Before final source clearance, all of the following remain required:

1. Implement the amendment and watchdog without weakening the closed ABI, first-side-effect ordering, one-owner rule, typed callback registration, or lifecycle deadlines.
2. Prove every R1 source 0–50 mapping, authority/nullability combination, duplicate/unmapped refusal, collision pair, reversed arrival/group order, metadata conflict, public-body conflict, and original-object retention.
3. Prove R2 pre-submit, post-queue/no-return, no-entry, delayed/impossible witness, callback-install failure, repeated-anomaly, release-once, actual old-generation join, and no-overlap behavior.
4. Prove real subprocess watchdog startup, handshake, malformed/nonce/FD negatives, helper death/channel failure, normal disarm/reap, failed-bootstrap arming, hard termination, PID-reuse protection, SIGKILL-request-versus-exit distinction, and old-exit-before-fresh-readiness.
5. Prove immediate pre/post-cache readiness refusal, admission refusal, permanent post-escalation drain, and absence of readiness restoration in the old process.
6. Prove SQLite and serial PostgreSQL Stop/deadline/lock/commit-before-projection behavior, unknown-commit reconciliation, exact replay, atomic cohort rollback, sealed revocation, and no provider replay.
7. Retain the three distinct durable-Stop cases and older local-cancellation cases without conflating their authorities.
8. Complete global writer, caller, handler, route, error-envelope, deployment-mirror, schema-epoch, and historical-obligation reconciliation.
9. Clear the currently red canonical contracts census through legitimate source/inventory reconciliation—no re-pin or suppression merely to make it green.
10. Run affected focused tests, whole-tree Python/frontend gates, PostgreSQL testcontainers, frozen-input gates, resource census, real local TLS/browser/proxy acceptance, and the transition-specific provider-call ledger using only fake providers and isolated local databases.
11. Obtain independent Astra correctness/design review and final actual Daybreak source/transport/security review on the exact frozen HEAD.
12. Report exact branch, base and HEAD, commands, exits, proof scope, known limits, and local test procedure.

## Final disposition

**PLAN GO.** Amendment implementation may begin under the adopted revision-4 and selected lifecycle contracts.

**Mandatory HOLD after implementation:** no final source approval, merge, deployment, protected-check bypass, production test, or paid-provider test until every required proof passes on the exact frozen source and the final independent reviews return clearance. The resulting branch remains held for **John’s LOCAL testing**.
