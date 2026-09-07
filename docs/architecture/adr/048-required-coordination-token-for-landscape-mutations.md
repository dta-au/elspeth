# ADR-048: Required Coordination Token for Landscape Mutations — Every Mutation API Takes One Current Token, Keyword-Only

**Date:** 2026-09-06
**Status:** Proposed (P4-D8, elspeth-43ddb79074; ruled EXECUTE under Q1(b), elspeth-d729c26729)
**Deciders:** ELSPETH maintainer
**Review evidence:** the fail-closed gate `tests/unit/architecture/test_web_landscape_mutation_fencing.py` (4 red ids). Measured on `p4/d8` at `d8bf089be`: 90 mutation APIs — 11 already require an exact, non-defaulted `CoordinationToken`, 6 carry an optional/defaulted or non-exact authority parameter, and 73 have no authority parameter at all (72 once `begin_run`'s epoch-one exemption is removed from the count); 84 API violations, 278 caller violations (264 ordinary + 14 coordination), 93 transaction-order violations; the frozen inventories are 139 DML identities, 266 production callers, 15 coordination callers, 101 internal facade edges and 80 subordinate Connection-helper edges.
**Tags:** landscape, coordination, fencing, multi-replica, audit-integrity, related-adr-030, related-adr-047

## Context

ADR-030 made the Landscape database the arbiter of run leadership, and
`CoordinationToken(run_id, worker_id, leader_epoch)` the value that proves it.
The verify-and-extend fence (`verify_and_extend_leader_fence`) CAS-matches all
three fields against `run_coordination` as the first statement of a fenced
verb's transaction, so a token is the only thing in the system that can answer
"is this writer still the leader of this run?".

The token is not required. Of the 90 public Landscape mutation APIs, 73 do not
accept one at all, 6 accept one that is optional or not exactly typed, and 11
require one. Every unfenced verb therefore takes `run_id: str` — a plain
string, minted by whoever is calling — and writes audit rows under it. With one
process per Landscape that is survivable: there is one writer, and the run id
it holds is the run it owns. Release 0.8.0's goal (Phase 6/6b, Azure Container
Apps, replicas > 1) puts **two or more writers against one Landscape**. A
replica that has lost leadership — evicted, drained, partitioned, or resumed
after a pause — still holds a valid-looking `run_id` string and can still write
`node_states`, `token_outcomes`, `calls`, `operations`, `rows` and the run's
terminal status. Nothing in the schema, the API signature, or the transaction
can detect it, because nothing in the write path carries leadership.

This is the same shape of defect ADR-047 closed for time. There, "now" was
supplied by whichever process asked; here, "which run, on whose authority" is
supplied by whichever process asks. ADR-047 moved the clock into the database;
this ADR moves the authority into the signature. The two compose: a fenced verb
reads the Landscape database's clock for its deadlines (ADR-047) and CAS-matches
the caller's token for its right to write at all (this ADR). Neither substitutes
for the other — a current token with a divergent clock still writes a wrong
deadline, and a database-authoritative deadline written by a deposed leader is
still a corrupt audit row.

The gate is a scanner, not a policy document. Its four red ids pin:

1. `test_every_landscape_mutation_api_requires_current_typed_authority` — every
   normal mutation API takes a non-optional, exactly-typed `CoordinationToken`;
   84 findings today.
2. `test_landscape_production_caller_set_is_frozen` — the caller inventory is
   frozen by digest, and every production caller forwards one exact token and
   the token's own `run_id`; the inventories match today and 278 callers forward
   no token.
3. `test_every_landscape_dml_transaction_is_full_token_fenced_first` — every DML
   owner fences before its first payload statement, and every raw-`Connection`
   helper has exactly one fenced caller; 93 findings today.
4. `test_no_mutation_alias_wrapper_dynamic_or_raw_write_escape_exists` — no
   alias, wrapper, dynamic dispatch, raw write surface or cross-database access
   routes around the facade.

### Scanner defects fixed before the threading (P4-D8 stage 1)

The threading is measured by this scanner, so the scanner's own defects were
fixed first, on their own request, before any production signature moved. All
three were the same mistake in three places — **a spelling is not an identity**:

- **A table name is not an identity.** Sessions
  (`elspeth.web.sessions.models`) and Landscape
  (`elspeth.core.landscape.schema`) both define a `runs` table. The raw-write
  surface rule matched the bare string `runs`, so four honest Sessions writes
  (`web/coordination/repository.py` `create_pending_run` and
  `transition_run_status`, `web/coordination/run_recovery_authority.py`
  `_cancel_candidate` and `mark_landscape_reconciliation_outcomes`) were
  reported as Landscape escapes. Table decisions now key on the module the
  `Table` object is bound from. Tree-wide the split is clean: 168 constructions
  bind `elspeth.web.sessions.models`, 140 bind
  `elspeth.core.landscape.schema`, 12 bind an unresolved runtime table.
- **A callable name is not a DML constructor.** Classification keyed on the
  terminal spelling (`insert` / `update` / `delete` / `*_insert`), which both
  over- and under-matched: `_conflict_safe_insert(conn, table, ...)` was read as
  a DML construction whose "table" was the `Connection`, and
  `@router.delete("/{session_id}")` was read as a DELETE on a path string.
  Classification now resolves the callable's import binding, so
  `from sqlalchemy.dialects.postgresql import insert as postgresql_insert` is
  the same constructor under another name, and a same-spelled callable from any
  other origin is not one. The ten forced dialect aliases stopped being reported
  as escapes; the identity digest did not move.
- **A rebinding is not a cycle.** `resolve_statement`'s guard keyed on the
  *name*, so `query = select(...)` followed by `query = query.where(...)` — one
  name refined to a value derived from itself — was abandoned as a cycle, and a
  conditionally refined SELECT never resolved to its `select(...)` root. The
  guard now keys on the binding site.

The residue is listed in the D8SCAN request: statements supplied by the caller
(`DatabaseOps.execute_insert` / `execute_update`,
`ReadOnlyDatabaseOps.execute_fetch*`, `TokenOutcomeRepository._execute_*`)
cannot be classified at the execution site at all. Their admission rule under
this ADR is "it received the fenced `Connection`", which does not exist until
the threading lands; until then they stay violations with an honest label.

## Decision

### 1. `CoordinationToken` is a required, keyword-only parameter of every Landscape mutation API

> **CORRECTED 2026-09-07 — see the amendment "§1 overreached ADR-030 D4".** The
> paragraph below requires the *leader* token on all 90 APIs. That overreached
> ADR-030 D4, which is the design of record and specifies THREE fences, not one.
> What survives unchanged is everything about the *shape* of the parameter —
> required, keyword-only, exactly one concrete owned class, never a Protocol or
> a union. What changes is that the concrete class is chosen per verb SCOPE:
> `CoordinationToken` for run-scoped writes, `WorkerMembershipToken` for
> member-, item- and claim-scoped ones. Read this section as the shape rule and
> the amendment as the scope rule.

Every one of the 90 APIs takes `*, coordination_token: CoordinationToken`. Not
`token: CoordinationToken | None = None`, not `token: object`, not a
string-quoted annotation, not a defaulted parameter, and not a positional one.

- **Required** — a default is an unfenced arm. The 6 APIs that carry an
  optional token today (`RunLifecycleRepository.complete_run`,
  `update_run_status`, `finalize_run`, `DataFlowRepository.create_row_with_token`,
  `CheckpointManager.create_checkpoint`, `delete_checkpoints`) lose the default.
- **Keyword-only** — a positional token can be supplied by argument order, which
  makes a mis-threaded call a silent success. Keyword-only makes every
  unconverted caller a `TypeError` at import-time coverage, not a wrong row.
- **Exactly `CoordinationToken`** — not a Protocol, not a union, not
  `runtime_checkable` structural typing. ADR-032: nominally type what ELSPETH
  owns. A structural annotation admits an impostor with three matching
  attributes, which is exactly the failure the token exists to prevent.

The parameter name is `coordination_token` everywhere. The gate accepts
`token` as well because 11 APIs already use it; new code uses the long form.

### 2. The token carries the run identity; `run_id` is derived, never passed alongside

A verb that takes both `run_id: str` and a token has two answers to "which run"
and no rule for disagreement. Every fenced verb derives the run from
`coordination_token.run_id`. Where a caller today passes `run_id=run_id`, the
threading deletes the parameter; where the verb genuinely operates on a
*different* run's rows (the reconciliation and recovery sweeps), the second run
id is named for what it is (`target_run_id`) and the token still proves the
caller's own authority to run the sweep.

The gate already enforces this shape: `_is_exact_token_run_id` requires the
argument to be the token's own attribute, and `_run_id_is_bound_to_token`
rejects a run id that merely happens to equal it.

### 3. `PluginContext.record_*` and the provider recorders forward; they never mint

Nine `PluginContext` methods reach the Landscape facade
(`allocate_call_index`, `record_call`, `record_operation_call`,
`record_readiness_check`, `record_routing_event`, `record_routing_events`,
`record_transform_error`, `record_validation_error`,
`update_node_output_contract`), and the LLM providers reach them through
`GatewayLLMProvider` / `OpenRouterLLMProvider` / the Bedrock recorder closures.
All of them take the run's token **by value** from the executor that built the
context and pass it through unchanged.

A plugin never constructs a `CoordinationToken`, never reads one from settings
or the environment, and never receives a factory that can make one. The context
holds the token the executor was fenced with; if the executor was not fenced,
there is nothing to hold and the plugin cannot write. That is the intended
failure mode.

### 4. The web execution service is a token boundary, not a token source

> **CORRECTED 2026-09-07.** The six `ExecutionServiceImpl.update_run_status`
> reaches named below are **not Landscape reaches**. They are
> `SessionService.update_run_status` (`src/elspeth/web/sessions/service.py:10044`,
> protocol `web/sessions/protocol.py:4553`) called as
> `self._session_service.update_run_status` at `web/execution/service.py:1857`,
> `:2100`, `:2508`, `:2778`, `:2949` and `:3269` — a different symbol against the
> **Sessions** database. The Landscape verb
> `RunLifecycleRepository.update_run_status` has **no production caller at all**.
> The paragraph below described SCANNER ROWS, not a defect: the scanner could
> not resolve those receivers and rowed them as "unknown mutation receiver", and
> the prose then read the rows as reaches. Nothing was fixed, because nothing was
> broken. The receiver-precision gate rule of 2026-09-07 resolves the receiver to
> the owned Sessions type and the six rows leave — a GATE CORRECTION, never
> threading. `web/app.py::_finalize_orphaned_landscape_runs` reaching
> `complete_run` IS a real Landscape reach and the rest of this section governs
> it unchanged.

`ExecutionServiceImpl` currently reaches `update_run_status` at four points in
`_run_pipeline` plus `_handle_pipeline_submission_failure` and
`_persist_failed_run_status`, and `web/app.py::_finalize_orphaned_landscape_runs`
reaches `complete_run` — all with an "unknown mutation receiver" today. The web
tier obtains a token exactly once, from the leadership acquisition that starts
or adopts the run, and threads that value into every Landscape call it makes.

Where the web tier must act on a run it does *not* lead — orphan finalisation,
reconciliation, operator cancellation — it acquires leadership first (the
takeover CAS mints epoch+1) and writes under the token that CAS returned. There
is no "administrative" write path that skips the fence: an operator action that
cannot take the seat is an operator action that must not write the row.

### 5. Tests mint tokens through one helper, with real authority

`tests/fixtures/landscape.py::leader_coordination_token(factory, run_id)` reads
the run's own epoch-1 seat back out of `run_coordination` and returns it. That
is the only sanctioned way for a test to obtain a token for a run it did not
mint through the production path.

There are 108 direct `CoordinationToken(...)` constructions in the test tree
today. Each one is a fake authority: it satisfies a required parameter without
proving the seat exists, so a test that constructs its own token would keep
passing after a threading defect deposed the writer. The threading converts
them, in the same commit as the family they belong to, into either
`leader_coordination_token(...)` or the token the production call under test
actually returned. Direct construction survives only where the test's *subject*
is the token — fence-rejection tests that deliberately build a stale epoch, a
foreign worker id, or a mismatched run id. Those are the tests that must
construct one, and the gate's `_EXACT_ESTABLISHMENT_CALLERS` names them.

### 6. Task 8B sunsets the epoch-one creation exception

`RunLifecycleRepository.begin_run` is the one verb that cannot take a current
token, because it creates the run and its epoch-1 leader seat in one
transaction through `register_run_leader_on`: the token does not exist until the
statement that mints it commits. The gate carries this as an exact edge and
write set (`_FRESH_EPOCH_ONE_EXCEPTION`), not a repository, file, prefix or
wildcard allowance, and the standalone `register_run_leader` wrapper is never
admitted.

**The exception sunsets at Task 8B.** Its replacement is a two-phase creation:
`begin_run` returns the token it minted, and every row the run needs beyond the
`runs` row and the `run_coordination` seat is written by a second, fenced call
that presents it. When that lands, `_FRESH_EPOCH_ONE_EXCEPTION.temporary`
becomes `False` — or the entry is deleted — and
`test_epoch_one_creation_edge_is_the_only_temporary_authority_exception` pins
the empty set. Until then the exception is exact and non-release: its write
counts are pinned per table, so the transaction cannot quietly grow a third
write under the exemption.

### 7. The caller set is frozen; the threading may not move an identity

The four inventories (139 DML identities, 266 production callers + 15
coordination callers, 101 internal facade edges, 80 subordinate helper edges)
are frozen by canonical digest. Threading a token through a call changes the
call's *arguments*, not its identity: the digests project path, symbol, method,
receiver and ordinal, so a correct threading commit leaves all four unchanged.

A digest that moves during the threading is a signal, not a chore. It means a
caller was added, deleted, aliased, or replaced — which is precisely the
smuggling the freeze exists to catch. The re-pin rule is the manifest rule: rows
are re-derived only from the scanner's printed output, never hand-typed, and
each delta is recorded beside the constant with the commit that caused it.

### 8. The sidecar journal outbox drain is named, or it is fenced

`LandscapeJournal._drain_committed_outbox` runs four statements on a raw DBAPI
cursor from `engine.raw_connection()`: `BEGIN IMMEDIATE`, an advisory-lock
`SELECT`, a `SELECT` over `sidecar_journal_outbox`, and a **`DELETE FROM
sidecar_journal_outbox`** (`src/elspeth/core/landscape/journal.py:357, 364, 369,
401`). The first three classify honestly as transaction control and reads. The
`DELETE` is a raw write on a Landscape table outside both the SQLAlchemy DML
path and any fence.

It is not scanner noise and it is not classified away. Either the drain is
fenced like every other writer (it holds the run's token and CAS-matches before
the delete), or it becomes an **exact named exception in the shape of the
epoch-one one** — a pinned caller/callee edge with a pinned write set of exactly
one `sidecar_journal_outbox` delete, `temporary=True`, and a named sunset. A
prefix, file, or repository-level allowance is not available: the outbox is the
audit trail's own durability path, and a wildcard over it is a wildcard over the
evidence.

**RULED 2026-09-07 (EMC3): the named exact exception, `temporary=True`, sunset at
Task 8B.** The drain is not fenced, because there is no run to fence it against
(`sidecar_journal_outbox` has no `run_id` column — see the amendment's run-less
writer table). The write set pinned above is **incomplete** and is corrected in
the amendment: the same table also takes an `INSERT` at `journal.py:298`, and the
`DELETE` at `:401` is one statement executed once per acknowledged sequence
inside a `for` loop, not once per call.

### 9. What this ADR does not decide

It does not change the fence predicate (identity + epoch, ADR-030), the clock
authority (ADR-047), or the transaction shape. It does not introduce a token
factory, a token registry, a context variable, a thread local, or any other
ambient carrier: the token travels as a value in a parameter, and the absence of
a parameter is the absence of authority.

## Consequences

### Positive Consequences

- A deposed replica cannot write. The write path carries leadership, so the
  fence can reject it — today there is nothing to reject.
- A mis-threaded call is a `TypeError`, not a wrong audit row. Keyword-only,
  default-less parameters convert a silent integrity defect into an import-time
  failure that every test run surfaces.
- The audit trail becomes attributable at the row level: every mutation was
  written by a named worker at a named epoch, and the row's transaction proved
  it before writing.
- The four gate ids go green on evidence rather than on suppression, and the
  frozen inventories keep working afterwards as drift detectors.
- 108 tests stop asserting against self-minted authority.

### Negative Consequences

- The change is wide: 90 API signatures, ≥27 production files, 266 call sites,
  9 `PluginContext` methods, the provider recorders, and the web execution
  service. It is 60–120 h of work and it touches almost every executor.
- Every caller must have a token to forward. Where one does not exist today,
  the threading must decide whether the caller is legitimately unfenced (and
  must acquire leadership) or legitimately outside the run (and must not write).
  That decision is per-caller and cannot be batched.
- Test churn is large and mechanical, and mechanical churn is where a real
  regression hides. Each family's conversion runs with its own mutation proof.
- Until Task 8B, `begin_run` remains an exception, so the gate cannot pin the
  empty exception set.

### Neutral Consequences

- Signatures get longer. `coordination_token` on 90 verbs is verbose and that
  verbosity is the point: the parameter is the audit claim.
- The token is a value, so it can be logged. It carries no secret — run id,
  worker id, epoch — and the fence is a database CAS, not a bearer check, so a
  leaked token grants nothing a stale replica did not already have.

## Alternatives Considered

### Alternative 1: Keep `run_id: str` and validate leadership inside each verb

Each verb looks up the current leader and compares it with the caller's identity.
Rejected: the caller's identity is exactly what is missing. A verb that has only
`run_id` cannot tell a current leader from a deposed one, so the check either
reads an ambient identity (a thread local, which is the same defect in a new
place) or is not a check.

### Alternative 2: An ambient token — context variable, thread local, or a bound recorder

`ContextVar[CoordinationToken]` set by the executor and read by the repositories.
Rejected: it makes authority invisible at the call site, so the scanner cannot
prove it and a reviewer cannot see it. It also fails exactly where it matters —
across threads, async boundaries and worker pools, which is the multi-replica
shape this exists for. The gate's `_mutation_callable_escapes` rule is written
against precisely this pattern.

### Alternative 3: A `runtime_checkable` `Authority` Protocol instead of the concrete type

Rejected under ADR-032. Structural typing admits any object with `run_id`,
`worker_id` and `leader_epoch`, which is a two-line impostor; widening the
Protocol later silently reclassifies every implementation tree-wide; and since
Python 3.12 it rejects dynamic-attribute objects. Nominal typing against a class
ELSPETH owns is the whole point.

### Alternative 4: Optional token with a deprecation window

`coordination_token: CoordinationToken | None = None`, warn when absent, require
it later. Rejected: an optional token is an unfenced arm that exists for the
whole window, and the window is exactly the release the multi-replica shape
ships in. It also cannot be scanned — "every API has a token" becomes true while
nothing has changed.

### Alternative 5: Ticketed `--deselect` of the four ids with a sunset

The Q1(b) fallback. Ruled EXECUTE by the operator (elspeth-d729c26729), so this
alternative is closed; it is recorded because the gate's red state is otherwise
indistinguishable from an unmanaged failure.

## Related Decisions

- ADR-030: Multi-Worker Deployment Shape — the fence predicate and the token's
  three fields.
- ADR-047: Landscape Database-Clock Authority — the other half of a correct
  fenced write; this ADR fixes *who*, ADR-047 fixes *when*.
- ADR-032: Validate by Trust Domain — why the annotation is a concrete class.
- ADR-046: Audit Grade Is a Product Characteristic — why the gate protects
  runtime data and is not project ceremony.

## References

- Gate: `tests/unit/architecture/test_web_landscape_mutation_fencing.py`
  (`_MUTATION_APIS`, `scan_dml_identities`, `scan_production_calls`,
  `_api_authority_violations`, `_caller_authority_violations`,
  `_transaction_order_violations`, `_FRESH_EPOCH_ONE_EXCEPTION`).
- Token: `src/elspeth/contracts/coordination.py::CoordinationToken`.
- Test helper: `tests/fixtures/landscape.py::leader_coordination_token`.
- Tickets: elspeth-43ddb79074 (P4-D8), elspeth-d729c26729 (Q1(b) ruling).

## Notes — threading plan (implementation order, one visible commit per family)

Each commit: `mypy` at 0 on the touched source, ruff, the four gate ids rerun at
`-n 0` (finding counts may only fall; the set is diffed), the family's test
files rerun, one named mutation with cp-roundtrip restore, and
`tests/unit/architecture tests/unit/contracts` when a helper is added. The four
frozen digests must be **unchanged** at every commit; a moved digest stops the
commit.

| # | commit | APIs | production surface | tests to re-express | mutation |
|---|---|---|---|---|---|
| D8.0 | scanner fixes + this ADR (stage D8SCAN) | 0 | none | fixture-module scanner unit tests | revert each fix → its named fixture test goes red |
| D8.1 | `RunLifecycleRepository` (run-lifecycle, 13) | 13 | `run_lifecycle_repository.py`, orchestrator run lifecycle/resume | run finalisation, abandonment, resume | drop the token from `complete_run` → gate id 1 grows |
| D8.2 | `DataFlowRepository` (data-flow, 15) | 15 | `data_flow_repository.py`, `data_flow/*`, `TokenManager`, `RowProcessor` | token lineage, row creation, quarantine | forward a constructed token instead of the parameter → gate id 2's caller rule fails |
| D8.3 | `ExecutionRepository` (execution, 21) | 21 | `execution_repository.py`, `execution/*`, the executors, `PluginContext.record_*`, provider recorders | executor + provider tests, `PluginContext` tests | let `PluginContext` mint a token → `_mutation_callable_escapes` grows |
| D8.4 | `TokenSchedulerRepository` (scheduler, 25) | 25 | `scheduler_repository.py`, `scheduler/*`, `scheduler_drain.py`, barrier coordination | scheduler, lease, barrier, drain tests | leave `enqueue_ready_claimed_legacy_unfenced` reachable → gate id 4 keeps its callable escape |
| D8.5 | sink-effect (11) + checkpoint (2) + audit-export (3) | 16 | `execution/sink_effects.py`, `sink_effect_*`, `checkpoint/manager.py`, `audit_export_snapshots.py` | sink-effect lifecycle, checkpoint, export | drop the fence from `SinkEffectLifecycle.complete_plan` → gate id 3 grows |
| D8.6 | web tier: `ExecutionServiceImpl`, `web/app.py`, run recovery | 0 | `web/execution/service.py`, `web/app.py` | web execution + orphan finalisation tests | finalise an orphan without acquiring leadership → gate id 2 fails |
| D8.7 | tests: 108 direct `CoordinationToken(...)` → `leader_coordination_token` except the fence-rejection subjects | 0 | none | the 20 files listed by the grep | convert a fence-rejection test to the helper → that test stops failing on a stale epoch |
| D8.8 | closure: journal outbox drain per §8; caller-supplied-statement relays admitted on the fenced `Connection`; re-pin all four digests | 0 | `journal.py`, `_database_ops.py`, `data_flow/outcomes.py` | journal drain tests | — |

Task 8B (separate ticket) removes the epoch-one exception per §6.

Estimate: 60–120 h as planned. D8.3 and D8.4 carry most of the caller surface
(the 278 caller findings concentrate in the executors and the scheduler).

### Amendments recorded while threading (D8THREAD, 2026-09-06)

- **D8.8 helper admission, as implemented (ruling 2026-09-06).** Gate id 3 no
  longer requires a raw-`Connection` DML helper to have exactly one fenced
  caller. Cardinality was a proxy: the property the rule protects is that
  every execution of the helper's DML sits inside a proven fenced transaction
  on the fenced connection, and a shared ledger writer
  (`record_coordination_event`, 9 callers; `SchedulerEventStore.record`, 15)
  can only reach cardinality one by duplicating its row construction into
  every owner. A helper is now admitted when it has at least one caller and
  **every caller edge** is fenced: a fenced DML owner passing its exact fenced
  connection inside its fenced `with`; a transitively admitted helper passing
  its own exact `conn` (cycle-guarded); both endpoints inside one
  authority-establishment helper graph, whose writes the establishment's
  per-table counts already pin; or the one pinned unfenced-evidence edge,
  `_record_best_effort_event -> record_coordination_event` (ADR-030 §A.2: the
  refusal row is written by a writer the fence has just proven holds no
  authority), admitted only in its exact shape. Zero callers, one unfenced
  caller among many, a cyclic chain, a foreign run subject and two call sites
  stay red; the helper-side checks now run for every helper.
- **Export seat.** `RunCoordinationRepository.acquire_export_leadership(run_id,
  worker_id, window_seconds)` is the authority for re-driving a finalized
  run's audit export (§4). It is a separate verb from the resume takeover:
  admissible only on a terminal run whose seat is vacant or expired, it never
  touches `runs.status`, mints epoch+1 with the same worker row and
  `worker_register` / `leader_acquire` events, and is vacated by
  `release_seat`. Its write set is the fourth pinned establishment entry.
- **Web tier.** The orphan finaliser takes a dead leader's seat through the
  resume takeover (`entry_point="orphan-finalize"`) before stamping
  INTERRUPTED; a live seat means the run is not orphaned and reconciliation
  is deferred to the next sweep.
- **Plugin contexts.** A plugin never presents a token. `PluginContext` carries
  the executor's token by value; the gate admits `ctx.<forwarder>()` on a
  parameter annotated with an owned context type, and the context's own
  forwarding call only when the attribute is bound solely in `__init__` from
  an exact token parameter and the call sits below a fail-closed `is None`
  guard.

## Amendment 2026-09-07 — §1 overreached ADR-030 D4; the three-fence split restored

**What this amendment is.** §1 of this ADR required a `CoordinationToken` — the
*leader* token — on every one of the 90 mutation APIs. That overreached
[ADR-030](030-multi-worker-deployment-shape.md) **D4**, which is the design of
record and specifies **three** fences, not one:

| D4 fence | applies to | the predicate |
|---|---|---|
| leader epoch verify-and-extend CAS | run-scoped write verbs | `run_coordination` matches `(run_id, worker_id, leader_epoch)` |
| membership fence | claim/enqueue and member-scoped verbs | a `run_workers` row for `(run_id, worker_id)` with `status='active'` |
| item-lease CAS | item-scoped writes | the payload's own `expected_lease_owner` WHERE |

Requiring the leader token on item-scoped and claim/enqueue verbs does not make
those writes safer. It makes them **impossible for the worker that legitimately
performs them**: a follower holds no seat and no epoch, so under §1 as written
either the follower cannot write at all, or it is handed a leader token it has
no right to — and the second is the fail-open class this programme exists to
close. This amendment restores D4's split and names the owned authority type for
each fence. It does **not** add "a follower arm" to a leader design; it removes
an over-broad requirement that the leader design never made.

### A1. The tree had ONE fence wearing two names

Before this amendment there was no membership fence to build on. Two symbols
looked like two fences and were one:

- `run_coordination_repository.py::fenced_leader_transaction` — the real fence.
- `scheduler/fencing.py::fenced_write` — **a thin wrapper over it**, not an
  independent construct.

The gate's `_TRUSTED_FENCE_QUALIFIED` and `_FENCED_CONTEXT_NAMES` list both
names, and a reader of that list would reasonably conclude two independent
fences already existed and that D4 was therefore already implemented. It was
not. `fenced_member_transaction` is the **second** fence, and the first one that
is not the leader epoch CAS under another name.

### A2. Option (A): a second owned type, not one type with two meanings

Two options were on the table. Option (B) — keep `CoordinationToken` and let its
meaning depend on the verb — is **rejected**. Under (B) a leader-scoped verb that
accidentally accepted a follower's token would be *unprovable*: there is no
annotation, no gate rule and no test that can tell the two apart, because they
are the same class. Option (A) is adopted:

`WorkerMembershipToken(run_id, worker_id)` — frozen, slotted, **nominal**
([ADR-032](032-validate-by-trust-domain.md)). No Protocol, no union with
`CoordinationToken`, no inheritance in either direction. Established by
`admit_follower`, which returns it; a leader derives its own through
`CoordinationToken.membership`, because every seat mint — `register_run_leader_on`
(`run_coordination_repository.py:530`), the takeover CAS
(`_acquire_run_leadership_on`) and the export-seat CAS
(`_acquire_export_leadership_on`) — inserts the leader's `run_workers` row in the
same transaction as the seat. A leader IS a member by construction, so the
derivation invents nothing. **The reverse derivation does not exist.**

The gate admits **exactly one concrete type per verb class**. A leader-scoped
verb annotated with the member type is a violation, and so is the converse. No
verb accepts both, and neither type is ever spelled `| None`.

`fenced_member_transaction` composes `begin_write` with `verify_membership_fence`
as the first statement, in D7's **verify-UPDATE** form so the rowcount is the
proof rather than an EXISTS subquery's snapshot (which under PostgreSQL READ
COMMITTED would not hold the row lock).

That last clause is **measured, not asserted**, because a lock claim cannot be
proven on SQLite — one writer runs at a time there regardless, so a no-op lock
and a correct one are indistinguishable at the assertion. Replacing the
verify-UPDATE with the equivalent un-locked `SELECT` and running both forms:

| form | 131 SQLite tests | PostgreSQL two-writer contention proof |
|---|---|---|
| verify-UPDATE (this design) | pass | pass |
| `SELECT` snapshot (D7's warning) | **pass — the defect is invisible** | **fails**: both writers pass the fence, and the loser's payload CAS matches 0 rows and raises `AuditIntegrityError` |

So the row lock is load-bearing and only the container suite can say so. Every
membership-fence contention claim in this amendment rests on
`tests/testcontainer/core/test_run_coordination_release_postgres.py`, repeated
per race because a single green pass cannot be told from luck. Rowcount 0 rolls the whole transaction
back and raises `RunMembershipLostError`, the sibling of `RunLeadershipLostError`,
recording a `fence_refusal` event on a fresh connection exactly as the leader
fence does. The refusal row carries `leader_epoch=NULL` — a member holds no epoch
— and names the fence in its context so the ledger can tell the two apart.

### A3. Scope table — all 90 APIs

Scope classes: **LEADER** (leader seat only), **MEMBER** (a worker's own
liveness/departure, or a write both roles make), **ITEM** (follower-reachable
while processing a claimed work item), **CLAIM** (claim/enqueue/heartbeat-lease).
ITEM keeps the item-lease CAS as D4's third fence *in addition to* the membership
fence; the membership fence proves who the worker is, the lease CAS proves the
work item is still theirs.

Totals: **LEADER 68, ITEM 13, CLAIM 5, MEMBER 5** (91 rows for 90 APIs, because
`record_token_outcome` splits per R7.2). The source is the coordinator's
`verb-scope-classification.md`, corrected where the code disagreed — every
correction is named in the notes column or in A4/A5 below.

One axis note, because it caused a real disagreement while this table was
built: **the scope column means REACHABILITY, not verb kind.** CLAIM is the one
class whose name suggests a kind, and reading it that way puts
`claim_pending_sink` and the four sink-effect lease verbs in it. They are
leader-only by caller chain, so they are LEADER here. A verb's name is not its
scope, for the same reason its method name is not its owner (A6).

| # | verb | facade | scope | authority type | fence | notes |
|---|---|---|---|---|---|---|
| 1 | `begin_run` | RunLifecycleRepository | LEADER | `CoordinationToken` | leader epoch CAS | epoch-one creation exception (§6); sunsets at Task 8B |
| 2 | `complete_run` | RunLifecycleRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 3 | `record_source_field_resolution` | RunLifecycleRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 4 | `record_run_source` | RunLifecycleRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 5 | `update_run_source_contract` | RunLifecycleRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 6 | `update_run_status` | RunLifecycleRepository | LEADER | `CoordinationToken` | leader epoch CAS | NO production caller — the six web reaches are Sessions writes (§4 correction) |
| 7 | `record_secret_resolutions` | RunLifecycleRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 8 | `record_preflight_results` | RunLifecycleRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 9 | `record_readiness_check` | RunLifecycleRepository | LEADER | `CoordinationToken` | leader epoch CAS | R7.8 latent trap; the follower arm is silently skipped — elspeth-df7daf5667 |
| 10 | `set_export_status` | RunLifecycleRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 11 | `set_export_failed_unless_completed` | RunLifecycleRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 12 | `set_export_pending_unless_completed` | RunLifecycleRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 13 | `finalize_run` | RunLifecycleRepository | LEADER | `CoordinationToken` | leader epoch CAS | reads the reproducibility grade OUTSIDE its own fence, then delegates to `complete_run` |
| 14 | `create_row` | DataFlowRepository | LEADER | `CoordinationToken` | leader epoch CAS | R7.6 DELETE |
| 15 | `create_row_with_token` | DataFlowRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 16 | `insert_row_with_token_on` | DataFlowRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 17 | `create_token` | DataFlowRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 18 | `fork_token` | DataFlowRepository | ITEM | `WorkerMembershipToken` | membership + item-lease CAS |  |
| 19 | `coalesce_tokens` | DataFlowRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 20 | `finalize_coalesce_effect` | DataFlowRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 21 | `expand_token` | DataFlowRepository | MEMBER | `WorkerMembershipToken` | membership | R7.1 BOTH → member-scoped |
| 22a | `record_token_outcome` (item arm) | DataFlowRepository | ITEM | `WorkerMembershipToken` | membership + item-lease CAS | R7.2 SPLIT — never an Optional |
| 22b | `record_token_outcome` (finalize arm) | DataFlowRepository | LEADER | `CoordinationToken` | leader epoch CAS | R7.2 SPLIT — `run_lifecycle_repository.py:839`, `sink_effect_finalization.py:299` |
| 23 | `register_node` | DataFlowRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 24 | `register_edge` | DataFlowRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 25 | `update_node_output_contract` | DataFlowRepository | MEMBER | `WorkerMembershipToken` | membership | R7.1 BOTH → member-scoped; no node-scoped fence is invented |
| 26 | `record_validation_error` | DataFlowRepository | LEADER | `CoordinationToken` | leader epoch CAS | R7.8 latent trap stated below |
| 27 | `link_validation_error_to_row` | DataFlowRepository | LEADER | `CoordinationToken` | leader epoch CAS | R7.6 DELETE |
| 28 | `record_transform_error` | DataFlowRepository | ITEM | `WorkerMembershipToken` | membership + item-lease CAS |  |
| 29 | `begin_node_state` | ExecutionRepository | MEMBER | `WorkerMembershipToken` | membership | R7.1 BOTH → member-scoped |
| 30 | `record_completed_node_state` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 31 | `record_completed_node_state_on` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 32 | `reconcile_source_completions_from_scheduler` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 33 | `begin_node_states_many` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 34 | `complete_node_state` | ExecutionRepository | MEMBER | `WorkerMembershipToken` | membership | R7.1 BOTH → member-scoped |
| 35 | `complete_node_states_completed_many` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS | R7.6 DELETE (facade) |
| 36 | `record_routing_event` | ExecutionRepository | MEMBER | `WorkerMembershipToken` | membership | R7.1 BOTH → member-scoped |
| 37 | `record_routing_events` | ExecutionRepository | ITEM | `WorkerMembershipToken` | membership + item-lease CAS |  |
| 38 | `allocate_call_index` | ExecutionRepository | ITEM | `WorkerMembershipToken` | membership + item-lease CAS |  |
| 39 | `record_call` | ExecutionRepository | ITEM | `WorkerMembershipToken` | membership + item-lease CAS |  |
| 40 | `begin_operation` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 41 | `complete_operation` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 42 | `allocate_operation_call_index` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 43 | `record_operation_call` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 44 | `create_batch` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 45 | `add_batch_member` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS | R7.6 DELETE (with `AggregationExecutor.buffer_row`) |
| 46 | `update_batch_status` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 47 | `complete_batch` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 48 | `retry_batch` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 49 | `register_artifact` | ExecutionRepository | LEADER | `CoordinationToken` | leader epoch CAS | R7.6 DELETE (facade) |
| 50 | `enqueue_ready` | TokenSchedulerRepository | CLAIM | `WorkerMembershipToken` | membership |  |
| 51 | `enqueue_ready_claimed` | TokenSchedulerRepository | CLAIM | `WorkerMembershipToken` | membership |  |
| 52 | `enqueue_ready_claimed_legacy_unfenced` | TokenSchedulerRepository | CLAIM | `WorkerMembershipToken` | membership | R7.6 DELETE |
| 53 | `ingest_row_with_initial_claim` | TokenSchedulerRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 54 | `claim_ready` | TokenSchedulerRepository | CLAIM | `WorkerMembershipToken` | membership |  |
| 55 | `claim_pending_sink` | TokenSchedulerRepository | LEADER | `CoordinationToken` | leader epoch CAS | R7.4 — the classification read it CLAIM from its verb KIND; the code says leader-only and the code wins. Follower contract quoted in A4 |
| 56 | `recover_expired_leases` | TokenSchedulerRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 57 | `recover_expired_leases_legacy_unfenced` | TokenSchedulerRepository | LEADER | `CoordinationToken` | leader epoch CAS | R7.6 DELETE |
| 58 | `heartbeat_lease` | TokenSchedulerRepository | CLAIM | `WorkerMembershipToken` | membership | OWNER-KEYED: the scheduler verb is CLAIM; `SinkEffectRepository.heartbeat_lease` is LEADER |
| 59 | `mark_blocked` | TokenSchedulerRepository | ITEM | `WorkerMembershipToken` | membership + item-lease CAS |  |
| 60 | `mark_terminal` | TokenSchedulerRepository | ITEM | `WorkerMembershipToken` | membership + item-lease CAS |  |
| 61 | `mark_terminal_with_ready_children` | TokenSchedulerRepository | ITEM | `WorkerMembershipToken` | membership + item-lease CAS |  |
| 62 | `mark_failed` | TokenSchedulerRepository | ITEM | `WorkerMembershipToken` | membership + item-lease CAS |  |
| 63 | `mark_failed_with_ready_children` | TokenSchedulerRepository | ITEM | `WorkerMembershipToken` | membership + item-lease CAS |  |
| 64 | `mark_pending_sink` | TokenSchedulerRepository | ITEM | `WorkerMembershipToken` | membership + item-lease CAS |  |
| 65 | `mark_pending_sink_with_ready_children` | TokenSchedulerRepository | ITEM | `WorkerMembershipToken` | membership + item-lease CAS |  |
| 66 | `mark_pending_sink_terminal` | TokenSchedulerRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 67 | `mark_pending_sink_terminal_many` | TokenSchedulerRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 68 | `terminalize_pending_sinks_with_terminal_outcomes` | TokenSchedulerRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 69 | `complete_barrier` | TokenSchedulerRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 70 | `mark_blocked_barrier_pending_sink_many` | TokenSchedulerRepository | LEADER | `CoordinationToken` | leader epoch CAS | NO production caller at either layer. DELETE candidate — RAISED, not ruled |
| 71 | `mark_blocked_barrier_terminal` | TokenSchedulerRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 72 | `adopt_blocked_barrier_item` | TokenSchedulerRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 73 | `reset_adoption_marker_to_pending` | TokenSchedulerRepository | LEADER | `CoordinationToken` | leader epoch CAS | R7.3 LIVE UNFENCED LEADER WRITE — elspeth-ee18e446ff (P1) |
| 74 | `adopt_group_losses` | TokenSchedulerRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 75 | `reserve` | SinkEffectRepository | LEADER | `CoordinationToken` | leader epoch CAS | no authority fence of any kind today (ordering CAS only) |
| 76 | `claim_preparation` | SinkEffectRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 77 | `complete_plan` | SinkEffectRepository | LEADER | `CoordinationToken` | leader epoch CAS | the ONLY sink-effect verb with the lease in SQL |
| 78 | `acquire_lease` | SinkEffectRepository | LEADER | `CoordinationToken` | leader epoch CAS | no rowcount check |
| 79 | `heartbeat_lease` | SinkEffectRepository | LEADER | `CoordinationToken` | leader epoch CAS | OWNER-KEYED: the scheduler verb is CLAIM; `SinkEffectRepository.heartbeat_lease` is LEADER |
| 80 | `takeover_expired` | SinkEffectRepository | LEADER | `CoordinationToken` | leader epoch CAS | no rowcount check |
| 81 | `begin_attempt` | SinkEffectRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 82 | `record_attempt_result` | SinkEffectRepository | LEADER | `CoordinationToken` | leader epoch CAS |  |
| 83 | `complete_member_result` | SinkEffectRepository | LEADER | `CoordinationToken` | leader epoch CAS | accepts a lease it never puts in SQL |
| 84 | `mark_response_lost` | SinkEffectRepository | LEADER | `CoordinationToken` | leader epoch CAS | accepts a lease it never puts in SQL; omitted at 5 of 6 call sites |
| 85 | `finalize` | SinkEffectRepository | LEADER | `CoordinationToken` | leader epoch CAS | compares `lease_owner` in Python only |
| 86 | `create_checkpoint` | CheckpointManager | LEADER | `CoordinationToken` | leader epoch CAS | fence is OPTIONAL at the manager; `CheckpointCoordinator._require_fence` is what fails closed |
| 87 | `delete_checkpoints` | CheckpointManager | LEADER | `CoordinationToken` | leader epoch CAS | same optional fence; same coordinator guard |
| 88 | `register_candidate` | AuditExportSnapshotRepository | LEADER | `CoordinationToken` | leader epoch CAS | NO production caller; its own docstring deprecates it. DELETE candidate — RAISED, not ruled |
| 89 | `register_verified_candidate` | AuditExportSnapshotRepository | LEADER | `CoordinationToken` | leader epoch CAS | writes with NO coordination fence; leader-only by caller chain alone |
| 90 | `bind_winner` | AuditExportSnapshotRepository | LEADER | `CoordinationToken` | leader epoch CAS | performs NO DML — a verify-and-project read miscounted into the census |

Three verbs beyond the 90 are fenced by this amendment's implementation and
belong in the same picture: `release_seat` is **LEADER** (the seat is a
run-scoped row, so vacating it is D4's first fence, not its second), and
`depart_worker` and `worker_heartbeat` are **MEMBER**. All three reify their
refusal rather than propagating it — each is called from a teardown or liveness
arm where re-raising would mask the exception being unwound — so their evidence
is the durable `fence_refusal` row plus zero mutation. The F-10 inventory records
that distinction per verb.

### A4. Classification rulings folded in (EMC3, 2026-09-07)

**R7.1 — the four BOTH verbs are MEMBER-scoped, deliberately.** `begin_node_state`,
`complete_node_state`, `record_routing_event` and `update_node_output_contract`
are reachable by leader and follower alike. They are member-scoped because **the
leader is also a member**: a membership fence on a write both roles make asserts
exactly what the writer can prove, and nothing it cannot. This is recorded here
so that a later reader does not "tighten" one of them to LEADER as an apparent
improvement and thereby break every follower that makes the write. No node-scoped
fence is invented for `update_node_output_contract`; there is no node seat.

**R7.2 — `record_token_outcome` SPLITS into two verbs**, each with a required
concrete type. The item-scoped arm serves the lease path; the leader-scoped arm
serves the finalize-time callers `run_lifecycle_repository.py:839`
(`_abandon_undecided_tokens_in`) and `sink_effect_finalization.py:299`. Neither
takes the other's type and neither is Optional. One verb with an optional or
union-typed authority would be exactly option (B) in miniature.

**R7.3 — `reset_adoption_marker_to_pending` is a live unfenced leader write** on
the release branch (`scheduler/barrier.py:1204-1211`, a bare `begin_write`; the
caller `BarrierRecoveryCoordinator` binds the token at
`barrier_coordination.py:1662` and calls at `:2465`/`:2497`). Filed as
**elspeth-ee18e446ff, P1** — the priority follows the consequence, not the size of
the fix: a deposed leader replaying an adoption reset unrefused is precisely what
the epoch fence exists to prevent. The verb's docstring asserting it is
deliberately fence-free is refuted in source (the two-takeover window).

**R7.4 — `claim_pending_sink` is LEADER.** The evidence is the follower contract
in `engine/orchestrator/follower.py`, quoted rather than asserted:

> ``claim_ready`` only — never ``claim_pending_sink`` or pending-sink recovery
> (sink work is leader-only).

`processor.py` carries the same rule at `drain_follower_ready_work`, and
`leader_drain.py` states the other half: the leader drains the PENDING_SINK rows
that follower workers produced. (ADR-030 §B.1/§C.3.)

**R7.5 — `enqueue_ready`.** `follower.py`'s module docstring describes a
membership-fenced follower continuation path with no follower-reachable caller in
the tree. Wave 2 (lane SCHED) resolves stale-prose versus missing-path **in
source** and reports before it fences anything; if the prose is stale it is fixed
in the same commit. A corroborating lie is worse than no comment.

**R7.6 — nine dead facade verbs are marked DELETE**, not fenced, so wave 2 does
not spend a fence on a verb nothing calls: `create_row`,
`link_validation_error_to_row`, `add_batch_member` (with
`AggregationExecutor.buffer_row`), the facade `complete_node_states_completed_many`
and `register_artifact`, the `PluginContext.record_call` state arm,
`recover_expired_leases_legacy_unfenced` and
`enqueue_ready_claimed_legacy_unfenced`. **Hard precondition before deleting any
test alongside a verb:** classify each test by its SUBJECT via AST, not by the
file it lives in, and grep the behaviour's vocabulary tree-wide. Where the real
subject is a live path — for example the sub-repository `SinkEffectFinalization`
actually calls — MIGRATE the test onto that subject and mutation-test that it
still fails when the live path breaks. Deleting a doomed test file has already
deleted the only coverage of live code in this repository once.

**R7.7 — the follower's readiness evidence does not exist.**
`rag/transform.py:477 _record_readiness_check` guards on
`ctx.coordination_token is not None` and silently returns on every follower
`on_start`, because `cli.py` builds the follower's `PluginContext` without a
token. Two comments in the same chain contradict each other:
`contracts/plugin_context.py` says a context without a token "cannot write, and
that is the intended failure mode — never a silently skipped audit row", while
the RAG guard does exactly the silent skip. The remedy is the member token on
`PluginContext` (lane EXEC-PLUMB). Filed as **elspeth-df7daf5667**.

**R7.8 — `record_validation_error`'s LEADER classification rests on a
convention, and that is stated here beside the classification rather than only
in a ledger.** The convention is that only sources call it while the follower's
context carries a live audit writer. Nothing structural enforces it. If a
follower-reachable caller is ever added, the verb becomes a silent unfenced
write, not a refusal. The trap closes when the member token reaches
`PluginContext` (R7.7).

**R7.9 — two structurally distinct kinds of direct construction.** A direct
`CoordinationToken(...)` or `WorkerMembershipToken(...)` in a test that MOCKS the
repository is a **mock-only construction**: there is no seat and no `run_workers`
row to read back, so no helper could return one. That is legitimate and is not
the same thing as a **fence-rejection subject**, where the token is the subject of
the test and is deliberately built stale, foreign, or mismatched. §5's rule —
read the seat back, never mint — governs every other case; the member-scoped
helper is `tests/fixtures/landscape.py::member_token_for(engine, worker_id=...)`,
which reads the `run_workers` row back and does **not** check its status, because a
departed or evicted worker's token is exactly what a refusal test needs.

### A5. The run-less writers

ADR-048 never named these. Each gets an owned authority type **or** a named exact
exception; every exception carries a pinned write set of exact table + exact DML
verb — never a prefix, never a file, never a repository — and states **why no
seat can exist**, not merely that none does today. An exception class that grows
a second member later is how a fence becomes a formality, so the two writers
whose "no run" is a design claim rather than a fact get their own reasoning.

| writer | pinned write set (table: verb @ site) | disposition | why no seat CAN exist |
|---|---|---|---|
| `AuthAuditRepository.record_auth_event` / `record_login_success_and_token_issued` | `auth_events: INSERT` @ `auth_audit_repository.py:172`; `auth_events: INSERT` @ `:218` (one statement, two value rows) | named exact exception, **permanent** (`temporary=False`) | **Fact, not a claim.** `auth_events` has no `run_id` column and no FK to `runs` (`schema.py:2497-2542`). These rows are written at the HTTP auth boundary — login, token issuance, auth failure — before any pipeline run exists. There is no run to take a seat on, and adding one would mean inventing a run per login. |
| `LandscapeWriteRepository.record_synthesised_run` | `runs: INSERT` @ `write_repository.py:125`; `run_attributions: INSERT` @ `:151`; `nodes: INSERT` @ `:161` (N executions); `rows: INSERT` @ `:194` (N executions) | **NO exception. Fence it or delete it** — see below | **The design claim FAILS.** This verb *creates* the run, so a seat could exist: it already owns the right transaction shape (one `write_connection()` at `:123`) and could call `register_run_leader_on(conn, …)` immediately after the `runs` INSERT and return the token, which is precisely the two-phase creation §6 describes for `begin_run`. It does not, and the consequence is concrete: a cache-replay run has a `runs` row and **no `run_coordination` seat**, so `acquire_export_leadership` and `acquire_run_leadership` both raise `AuditIntegrityError` reading "the audit DB is corrupt" on it. Measured: **zero production callers in `src/`** — every construction site is a test, and `LandscapeWriteRepositories.__slots__` does not expose it. A test-only surface is an argument for deleting or fencing it, never for exempting it. |
| `reproducibility.update_grade_after_purge` | `runs: UPDATE` @ `reproducibility.py:332` (CAS-guarded: `reproducibility_grade = REPLAY_REPRODUCIBLE` → `ATTRIBUTABLE_ONLY`) | **NO exception on the normal branch — take the export seat.** One real hole remains; see below | **The design claim HOLDS, and then breaks on one branch.** `_EXPORT_SEAT_RUN_STATUSES` is all five terminal statuses, and finalisation vacates the seat, so on every run this verb can normally reach, `acquire_export_leadership` is admissible: §4's rule applies unchanged — take a seat or do not write. **But** the resume takeover flips FAILED/INTERRUPTED → RUNNING **without clearing `reproducibility_grade`**, and `update_run_status` does not clear it either. A resumed run therefore carries `REPLAY_REPRODUCIBLE` while RUNNING with a live seat, and the purge's run selection applies no status filter — so the UPDATE fires where the export seat is inadmissible on two independent grounds. That is a defect in the resume path (the grade should be cleared when the run leaves terminal), not a licence to exempt the writer. |
| `LandscapeJournal._drain_committed_outbox` | `sidecar_journal_outbox: DELETE` @ `journal.py:401` (one statement, once per acknowledged sequence) — **and** `sidecar_journal_outbox: INSERT` @ `journal.py:298` in `_before_commit`, which §8's pin omits | named exact exception, `temporary=True`, **sunset at Task 8B** (§8, now RULED) | **Fact.** `sidecar_journal_outbox` has no `run_id` column and no FK to `runs` (`schema.py:382-391`); it is keyed by `journal_owner`, and one batch can span records from any number of runs. There is no single run whose seat would authorise the drain. Note the asymmetry: the INSERT rides the caller's SQLAlchemy `Connection` and is fenced exactly when that transaction was; the DELETE runs on a raw DBAPI connection in its own `BEGIN IMMEDIATE`, outside every fence, from two different origins (`recover_pending` and the dialect commit hook). |
| `LandscapeDB._set_sqlite_schema_epoch` | `PRAGMA user_version = <int>` @ `database.py:1355` — **no table write at all** | named exact exception, permanent | **Fact.** It runs during schema management, before `metadata.create_all`; on a fresh database the `runs` table does not exist yet. The gate already classifies it by its own rule (`_SCHEMA_STAMP_PRAGMAS`) as a schema stamp rather than a row write, and it is a no-op on PostgreSQL. |
| `_database_ops` relays | **none of its own** — `_database_ops.py` contains zero DML constructions; it executes a caller-supplied `Executable` at `:113` and `:148` | **D8.8's wording is WRONG for this module — correct it** | See below. |

**D8.8's "admitted on the fenced `Connection`" is false as written for
`_database_ops.py`.** All three write-capable members open their **own**
transaction (`execute_insert` `:112`, `write_connection` `:132`, `execute_update`
`:147`) and none accepts a `Connection`. They are caller-supplied *statement*
relays on a *relay-owned* transaction; there is no fenced connection for them to
be admitted on. The claim holds instead for the relays that do take
`conn: Connection` and execute on it — `record_buffered_outcome_guarded` and
`record_terminal_outcome_guarded` (`data_flow/outcomes.py`, both called inside the
fenced barrier transaction), and `record_coordination_event`,
`record_coordination_events` and `_insert_worker_row`
(`run_coordination_repository.py`). One relay has both shapes and must be named
as such: `TokenOutcomeRepository.record_token_outcome` takes
`conn: Connection | None = None` and opens its own transaction when none is
passed, so whether any one of its production call sites is fenced depends on
whether that site passes `conn=`. Of the eleven `_database_ops` write callers,
nine are run-scoped repositories that hold a token; **the only two with no run at
all are the two `auth_audit_repository` sites**, so the relay's unfenced
self-owned transaction is load-bearing for `auth_events` and for nothing else.

### A5a. How the PluginContext admission survives ADR-032's ban on Protocols

The gate admits a plugin's `ctx.<forwarder>()` call through two arms, and one of
them is annotated with a Protocol. That looks, at first reading, like exactly the
thing [ADR-032](032-validate-by-trust-domain.md) forbids — a structural type used
as a security control. It is not, and the reason is worth stating in the
architecture decision rather than leaving in a lane report:

> **Arm (a) is a deferral, not a grant.** Admitting `ctx.<forwarder>()` on a
> Protocol-annotated parameter does not certify anything — it moves the proof
> obligation to whichever concrete class actually forwards to Landscape, and
> that class's own call is scanned like any other. **Arm (b), which grants, is
> keyed on a concrete owned class at an exact path.** A structural impostor
> cannot launder an unfenced write through (a); it can only relocate where the
> proof is demanded.

So the Protocol never answers "is this writer authorised". It answers "is this
call the end of the chain, or is there a further call still to prove?" — and the
answer "there is a further call" is safe under structural typing, because an
impostor that satisfies the Protocol still has to produce a concrete forwarder
whose own Landscape call the scanner will demand a token from. ADR-032's rule is
about the type that decides; this type decides nothing.

### A6. A defect class: key on the RESOLVED OWNER, never the method name

Two of the seven web false positives, and a wrong row in the coordinator's own
cross-tab, had one cause: **method-name collisions across owned types.**

| name | owners it collides across | what went wrong |
|---|---|---|
| `update_run_status` | `RunLifecycleRepository` (Landscape) vs `SessionService` (Sessions) | six Sessions writes read as Landscape reaches; corrected at §4 |
| `begin_attempt` | `SinkEffectRepository` vs `_PlannerAttemptTrail` (`web/composer/pipeline_planner.py:1121`) | a planner attempt-trail call rowed as a Landscape mutation |
| `heartbeat_lease` | `TokenSchedulerRepository` (work-item lease, CLAIM) vs `SinkEffectRepository` (sink-effect lease, LEADER) | the sink-effect facade was credited with the scheduler verb's classification and its membership fence; the sink-effect verb has neither |

**Any inventory, gate rule, ledger row or classification that keys on a method
name must key on the resolved owner instead.** This is not a scanner
implementation note: two of the three above reached prose in this ADR and in a
coordinator cross-tab, and each read as a finding about code that was fine while
hiding the state of code that was not.

The receiver-precision rule that follows from this admits a mutation-named call
by naming the receiver's **resolved non-Landscape owner**. It must never admit a
receiver on the grounds that the receiver resolves at all, and it must never
require the receiver to resolve *into* an owned Landscape class. An unresolvable
receiver stays a row.

**The justification first circulated with this rule, and written into an earlier
draft of this section, was wrong on the tree, and the correction matters more
than the error.** That draft said the eight unknown-receiver rows in the LLM
providers (`gateway.py` ×4, `openrouter.py` ×4) are UNRESOLVABLE, and that an
owner-keyed rule was therefore needed to keep them rowing. Measured through the
gate's own resolver, `audit_parent` is a parameter annotated `LLMAuditParent`
(`providers/gateway.py:556`, `:702`, `:731`) and resolves cleanly and exactly to
`elspeth.plugins.transforms.llm.provider.LLMAuditParent`, which at
`provider.py:41` really does declare `allocate_call_index` (`:97`) and
`record_call` (`:105`). The eight rows are not unresolved. They are fully
resolved, and they row because their owner is **absent from the allowlist**.

The conclusion survives the correction and is strengthened by it. Option (b),
"admit whatever resolves", was rejected as failing closed on the rows worth
catching. On the measured tree it is worse than that argument claimed: option
(b) would have **admitted all eight and lost them**, silently, because they
satisfy its admission criterion perfectly. The `LLMAuditParent` → `CallRecorder`
indirection that D8.3 exists to thread would have been erased from the inventory
by the very rule meant to sharpen it, and the escape counter would have fallen
by fifteen instead of seven while the real finding disappeared inside the
improvement.

So the operative distinction is **not resolvable versus unresolvable — it is
enumerated versus not enumerated.** Admission is a closed, pinned list of
(owner, method) pairs re-derived from the tree, and everything else rows,
whether or not it resolves. A receiver that resolves beautifully to a type
nobody allowlisted must still row.

### A7. What this amendment does not change

The fence predicates (ADR-030), the clock authority (ADR-047), the transaction
shape, and every *shape* rule in §1: required, keyword-only, exactly one concrete
owned class per parameter, never a Protocol, never a union, never Optional, never
an ambient carrier. The token still travels as a value in a parameter, and the
absence of a parameter is still the absence of authority. What changes is only
**which** owned class each verb requires, and that is now decided by the verb's
scope rather than assumed to be the leader's.
