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

*(Ruling requested from the hub 2026-09-06; recorded here as the ADR's proposed
position. The live state until D8THREAD is a correctly-labelled red.)*

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
