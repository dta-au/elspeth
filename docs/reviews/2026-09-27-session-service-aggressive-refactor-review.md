# Session service: aggressive refactoring review

Reviewed `release/0.8.1` snapshot `763f81d14`, including the extraction in
`6f2ccb32d`. Review only: no production or test files changed. The acceptance
standard is a material improvement in reliability, integrity, or supportability;
file length and stylistic preference are insufficient reasons to refactor.

**Verdict: GO for the telemetry correction and a bounded guided-operation rules
extraction. NO-GO for extracting the archive lifecycle now, a broad service
split, or a generic transaction framework.** The telemetry correction is a
verified defect. The guided rules extraction has a concrete shared-consumer
boundary, but is a lower-priority supportability improvement, not a release
correctness blocker. The earlier archive recommendation is withdrawn after
checking its existing filesystem boundary and failure tests: its proposed
benefit did not justify the new collaborator and dependency wiring.
This is a refactoring recommendation, not a claim that a further implementation
has passed its release gates.

## 1. High value: project the committed audit cohort, not the requested cohort

**Priority P1; verified correctness defect.**

At `src/elspeth/web/sessions/service.py:11321`, `add_messages_atomic` builds
`active_drafts`, removes already checkpointed provider envelopes, and drops an
audit draft with no remaining envelopes. At line 11329 it can return without
inserting anything. Its post-commit callback at line 11376 ignores the worker
result and projects the original `drafts` instead.

This is observable through an ordinary public entry point.
`finish_provider_attempt` at `service.py:6996` sends its provider audit draft to
this method. `audit_checkpoint.py:15` explicitly supports terminal replays by
validating and filtering already settled attempts. The telemetry consumer at
`src/elspeth/web/composer/provider_telemetry.py:148` converts an accepted envelope
into provider-call facts, and line 123 increments the request's provider call
count without deduplication. A retried checkpoint therefore projects a second
call despite inserting no second audit row. Later buffered audit persistence
also uses this writer (`src/elspeth/web/composer/service.py:5049`).

**Measured reproduction:** using the existing `quota_service` fixture factory
from `tests/unit/web/composer/test_provider_quota_end_to_end.py:43`, a real
SQLite store, a real COMPOSE lease, and a real pending provider attempt, called
`finish_provider_attempt` twice with the same terminal `ComposerLLMCall`.
Replaced only the projection function with a recording function. Import
provenance resolved to this worktree. The script exited 0 and printed:

```text
initial durable_audit_rows= 1 projection_calls= 1
replay durable_audit_rows= 1 projection_calls= 2
```

The initial checkpoint is the positive control; its replay is the negative
control. This demonstrates the incorrect projection invocation, not an assumed
database race. The audit-row deduplication itself succeeded.

A second diagnostic retained the real projection functions and substituted
recording instruments only for the OpenTelemetry counter/histograms. It also
exited 0. The initial write added one provider counter point and one 0.041-second
duration observation; the replay added another of each while the durable row
count remained one. Finishing the enclosing request recorded:

```text
request_provider_calls= [(2, {'surface': 'freeform', 'status': 'completed'})]
```

The projection is therefore **not idempotent**. Externally exported provider
call totals, provider latency observations, and per-request call counts can
overstate the work. This evidence does not show a second provider dispatch,
double quota charging, or a duplicate durable audit record.

The exact setup was: construct the real service through `quota_service`;
create an `alice` session; acquire `_lease(service, session.id)`; call
`begin_provider_attempt(session_operation_context=lease.context,
source="composer")`; take the existing telemetry test `_call()` fixture and
replace `call_id` with `attempt.attempt_id` and both timestamps with
`attempt.started_at`; invoke `finish_provider_attempt` twice with that same
call and lease context. Count `chat_messages_table` rows after each invocation.
For the second diagnostic wrap both calls in
`begin_composer_request_metrics(surface="freeform")` and finish with
`status="completed"`. No provider or network call is required.

The production quota decorator settles recorded attempts through this exact
entry point at `src/elspeth/web/composer/provider_quota.py:62`. Buffered planner
audit persistence constructs LLM audit drafts at
`src/elspeth/web/composer/service.py:5009` and writes the cohort at line 5049.
These are production consumers of the same persistence/projection contract;
the diagnostic exercised the real public checkpoint replay rather than a
mocked persistence path.

**Proposed boundary:** make `_write` and `_sync` return the exact immutable tuple
of drafts actually inserted, including filtered envelope content. Return `()`
when nothing was inserted. `_project` consumes this tuple. Keep the public
`add_messages_atomic` return contract unchanged. The existing generic
`_run_sync_with_post_commit_projection` at `service.py:11154` already carries a
worker result and drains cancellation; use that contract rather than adding a
new framework or a mutable closure holder.

**Benefit:** telemetry becomes a projection of durable facts. This fixes replay
overcounting and makes future filtering unable to silently decouple persistence
from post-commit reporting.

**Risk and validation:** preserve worker draining and cancellation precedence;
projection must still happen after a successful commit even if cancellation is
pending, and never after rollback. Cover the first insert, complete replay,
mixed fresh/replayed drafts, a partially filtered draft, rollback, and cancelled
commit. Extend `tests/unit/web/sessions/test_composer_provider_telemetry.py` and
run the provider quota end-to-end tests. Run the session mutation-authority
inventory and applicable contract gates because this edits a persistence
method; include the required serial PostgreSQL selection for the integrated
change. No schema change is needed.

## 2. Moderate value: give shared guided operation rules one owner

**Priority P2; bounded supportability improvement, not a reported runtime
failure or a prerequisite for accepting the preceding extraction.**

The extracted mutation capabilities still obtain pure domain calculations by
calling back into the whole service:

- `mutation_capabilities.py:770` calls
  `service._guided_operation_event_values`.
- Lines 796–804 call `service._merge_guided_binding` for terminal locators.
- Lines 907–908 validate the actor and response hash through the service.
- Line 938 calls `service._guided_completion_values` to encode the terminal
  result before the capability persists it.

The counterpart readers remain static methods on the service:
`service.py:1320` validates a complete operation row, line 1437 validates an
in-progress lease bundle, line 1485 validates terminal bundles, and line 1574
decodes the terminal result. The encoder at line 2132 and decoder at line 1574
jointly own the legal relationship between operation kind and result locator.
The event constructor at line 1204 explicitly says it does not own DML.

**Proposed boundary:** a cohesive `guided_operation_rules.py` containing the
pure input validation, event-value construction, binding merge, persisted-row
validation, terminal-result encoding, and terminal-result decoding. Move the
whole interacting ruleset, and update both service orchestration and mutation
capabilities to import it directly. Move the direct rule tests with it; callers
currently reach into `SessionServiceImpl._guided_terminal_outcome` in
`tests/unit/web/sessions/test_guided_operations_service.py:510` and line 559.
Keep row reads, clock reads, live authority checks, transactions, and writes in
their existing owners. Preserve exact nominal result-type checks and corruption
errors. Do not introduce a table-driven generic state-machine engine.

**Benefit:** adding or changing a guided result has an identifiable owner for
its persistence contract. Mutation capabilities no longer depend on service
internals for calculations which require neither a service nor a transaction.
This completes an actual domain boundary, rather than moving unrelated static
helpers into a utilities file.

**Countercheck against needless refactoring:** this does not eliminate the
capabilities' overall dependency on the service, and the existing static rule
tests already run without a database. Neither improved runtime reliability nor
cheaper tests has been demonstrated. The concrete seam is the existing second
production consumer: `mutation_capabilities.py` owns terminal writes while
`service.py` owns admission/replay reads, and both need the same operation
vocabulary and binding rules. Today their access to that vocabulary passes
through the orchestration class. The bounded move puts that shared contract
below both consumers without adding a collaborator, dependency injection, or
another dispatch layer. Acceptance can directly verify that those listed pure
rule calls no longer target `SessionServiceImpl`, while the transaction and
authority calls remain explicit. That is the supportability gain; reducing the
file's line count is not the gain.

**Risk and validation:** encoding, replay classification, and failure residue
checks must move together. Preserve the distinction between invalid requested
results and corrupt persisted results; do not replace nominal checks with
structural protocols. Compare executable ASTs apart from qualification changes.
Run guided operation, fork, revert, failure-classification, and atomic-settlement
tests; run the PostgreSQL guided settlement and session fencing proofs as part
of the required serial PostgreSQL selection. Re-derive affected exact contract,
read/write authority, and trust-tier bindings with their negative controls;
do not preserve old identities with aliases. An integrated change affecting
guided settlement should receive the frozen full-suite gate before merging.

The capability's remaining service calls for checkpoint insertion and live
authority (`mutation_capabilities.py:605` and line 724) have different semantics.
Removing them merely to eliminate every reverse dependency would require a
larger transaction design and is not part of this recommendation.

## 3. Rejected after countercheck: extracting the archive lifecycle now

**NO-GO for this review's scope. Earlier P2 recommendation withdrawn.**

`archive_session` at `service.py:3390` contains lease acquisition, shielded phase
joining, archive manifest reconciliation, filesystem staging, compensation,
database consumption, purge, and error aggregation. Its phase state is held in
`current_obligation_may_exist`, `current_stage_attempted`, and
`authority_uncertain` at lines 3417–3419, then changed across nested functions
at lines 3466, 3509, 3529, and 3604. None of this is session row decoding,
proposal settlement, or chat persistence.

The important seam already exists: database mutation goes through the exact
`SessionOperationLease` and its authority, while filesystem work goes through
`archive_quarantine.py`. Public production callers use the service method at
`src/elspeth/web/sessions/routes/sessions.py:956` and
`src/elspeth/web/sessions/routes/workflow/library.py:454`.

The initially proposed `archive_lifecycle.py` collaborator would receive the
authority, owner instance, lease duration, data directory, logger, and clock,
then run substantially the same operation behind a service delegation method.
That is not enough of a gain by itself.

**Counterevidence from the actual implementation and tests:**

- Filesystem custody already has an independent owner:
  `archive_quarantine.py:296` prepares manifests, line 341 stages payloads,
  line 369 restores them, line 398 purges them, and line 418 retires manifests.
  Filesystem-specific fault tests patch that module directly; for example,
  `tests/unit/web/sessions/test_service.py:313` injects purge failure there.
- The remaining lifecycle already has one per-invocation owner: the locals and
  closures of `archive_session`. Replacing those closures with an operation
  object's fields would not itself strengthen their legal transitions or
  eliminate shared mutable state; the state is already local to the call.
- Lifecycle failure injection is practical today.
  `tests/unit/web/sessions/test_archive_secondary_failures.py:44` intercepts
  the worker boundary and line 73 intercepts lease close. Those tests exercise
  repeated cancellation, phase failure, compensation failure, close failure,
  and preserved integrity errors. A collaborator extraction would mainly
  change the patch/import location, not unlock a presently inaccessible test.
- Both production route callers already share `archive_session`; no second
  implementation or alternative orchestration consumer was found that needs
  the proposed collaborator. The narrow dependencies are evidence that an
  extraction is possible, not that it is necessary.

No observed defect was traced to archive's placement in the service, and no
concrete coupling burden was established that outweighs the new wiring and
the risk to shield-and-join behavior, compensation order, authority checks,
and cleanup exception precedence. Retain the present boundary. Revisit only
if a specific archive change needs a reusable lifecycle or exposes an actual
state-ownership defect; do not preemptively build either abstraction.

## Refactors that do not earn their cost now

- **Split every large guided settlement method into a separate service or
  repository:** rejected. For example, `save_state_for_guided_operation`
  (`service.py:7925`), `settle_guided_state_operation` (8092), and pipeline
  acceptance (10037) must keep state, audit messages, proposal effects, and
  terminal operation settlement in one caller-owned transaction. Their shared
  shape is not evidence that their decisions are interchangeable. An extraction
  which passes the entire service around would relocate the complexity.
- **Generic transaction runners or mixins for all public methods:** rejected.
  The process-before-transaction ordering at `service.py:909`, globally ordered
  paired locks at line 915, and exact lock ownership guard at line 928 are
  correctness boundaries. A callback framework or dynamic delegation would
  obscure those boundaries without a demonstrated benefit.
- **Move every row converter, one-line delegate, or getter:** rejected as a
  package-size exercise. The service remains the public orchestration surface;
  straightforward methods such as `get_session` and `get_run` do not create a
  maintenance problem merely by remaining there.
- **Blindly unify the two database-clock helpers:** rejected as a mechanical
  move. `service.py:978` raises `AuditIntegrityError` for malformed clock values
  and returns `restore_utc(value)`; `coordination/database_clock.py:55` uses
  different exception behavior and explicitly converts aware timestamps to UTC.
  A deliberate clock-contract consolidation could be useful, but it must first
  settle and test those semantic differences. It is not needed to perform the
  bounded extractions above.
- **Move the extracted proposal/fork authority modules again:** rejected.
  Their ownership is already materially better defined by the preceding
  extraction. No new defect or consumer requirement established in this review
  warrants another reorganization.

## Scope and confidence

Reviewed the service's method/dependency structure, the extracted mutation
capabilities and their reverse calls, authority and locking seams, archive
lifecycle, guided row/result rules, provider audit filtering/projection, and
representative public callers and tests. Only the checkpoint replay behavior
was executed as a targeted diagnostic; this review did not run a release gate
or establish whole-service absence of defects. The structural recommendations
are maintenance judgments grounded in the cited boundaries, not claims of
additional runtime bugs.

The no-tech-debt policy warrants fixing the verified projection defect. The
guided rules extraction is a justified, bounded improvement to an existing
shared-consumer contract. The archive extraction has not earned its cost.
Neither the policy nor this review establishes an arbitrary target line count
or a requirement to eliminate every large method.
