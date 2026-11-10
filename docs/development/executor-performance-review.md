# Executor performance review

Status: **review only — no fix has landed.** Measured on
`fix/composer-remediation-0.8.12` at `231cc44e`, CLI runs against file-backed
SQLite, one process, default settings. This is a working document in the
style of [composer-performance-remediation.md](composer-performance-remediation.md):
update the status rows as fixes land, or delete it when it stops being useful.

## Headline

The executor spends its CPU talking to the Landscape, not running plugins.
On the shipped 10,000-row `large_scale_test` example (one CSV source, one
gate, two CSV sinks) a run costs 4m06s of CPU, about 24 ms per row, and
issues about 84 SQL statements and 10 commits per row. SQLite itself
accounts for 9–13% of that CPU. Roughly 60% is SQLAlchemy Python code, and
about 27% of the whole process is spent rebuilding and re-keying SQL
statements that are textually identical on every row.

Two things follow:

1. The largest semantics-preserving saving is to stop constructing
   statements per call. A five-statement prototype at the fence and clock
   chokepoints plus a memoised contract hash (reverted, not committed)
   measured 12% lower CPU on the gate workload and 16–18% on the transform
   workload, with byte-identical sink output.
2. Only 12 of the 64 statements in a gate row's main-loop path are audit
   writes. The rest is transaction control, fences repeated inside a
   transaction that is already fenced, clock reads, and reading back rows the
   same transaction just wrote. Most of that can go without changing what is
   recorded; some of it needs a ruling because it touches ADR-030/ADR-047
   fences.

## Baseline

One run each unless stated; CPU is user+sys of the `elspeth run` process.
The three scaled workloads repeat the shipped example's input rows to 1,000
rows with the example's own `settings.yaml`.

| Workload | Rows | CPU | CPU / row ¹ | Statements / row | Commits / row | Time inside SQLite ² |
|---|---:|---:|---:|---:|---:|---:|
| `large_scale_test` (gate) | 10,000 | 246.5 s (4m26s wall) | 24 ms | 84 | 10.3 | 9% |
| `transform_pipeline` × 200 (two transforms) | 1,000 | 31.5 s | 24 ms | 91 | 13.1 | 6% |
| `batch_aggregation` × 67 (count-5 batches) | 1,000 | 28.4 s | 21 ms | 87 | 7.7 (+2.2 rollbacks) | 9% |
| `fork_coalesce` × 200 (fork, two branches, merge) | 1,000 | 107 s ³ | 100 ms | 394 | 53 (+8 rollbacks) | 15% |
| `transform_pipeline` as shipped | 5 | 7.2 s | — | — | — | — |

¹ After subtracting the ~7.2 s fixed startup measured on the 5-row run.
² Cursor time from SQLAlchemy `before/after_cursor_execute` hooks divided by
process CPU; statement and commit counts are from the same hooks (gate
counts from a 500-row slice).
³ Measured under the event hooks, so slightly inflated.

Per-row cost is flat from 500 to 10,000 rows on the gate workload; nothing
measured here is super-linear in run size. One run-end query in the fork
workload took 0.58 s on its own at 1,000 rows (the `token_outcomes … NOT
EXISTS (token_parents …)` scan) and was not measured at other sizes.

### Where the CPU goes (py-spy, 200 Hz, whole process)

| Share of samples | Gate, 2,000 rows | Transforms, 1,000 rows | Aggregation, 1,000 rows | Fork/coalesce, 200 rows |
|---|---:|---:|---:|---:|
| SQLite native execute + commit | 12.5% | 7.8% | 9.9% | 10.8% |
| SQLAlchemy: building statement objects | 19.1% | 18.4% | 21.7% | 18.6% |
| SQLAlchemy: cache-key generation | 8.0% | 7.7% | 8.0% | 7.1% |
| SQLAlchemy: per-execute overhead | 18.2% | 15.6% | 16.5% | 19.0% |
| SQLAlchemy: connection, transaction, result | 4.4% | 3.7% | 4.2% | 4.3% |
| ELSPETH code (leaf) | 13.2% | 15.5% | 9.6% | — |
| Canonical JSON (`rfc8785`) | 3.0% | 4.8% | 2.3% | — |

By phase on the gate run: ingest 34%, drain (gate execution and
disposition) 33%, sink flush 16%, startup and teardown the rest.

### One gate row, statement by statement

From an ordered trace with call sites (64 statements and commits in the
main loop; the sink flush adds about 20 more per row):

| Category | Count | Notes |
|---|---:|---|
| Audit and scheduler writes | 12 | `rows`, `tokens`, two `node_states` inserts, one update, `routing_events`, work item insert and two updates, three `scheduler_events` |
| Transaction control | 14 | Five write transactions and one read-only transaction (`BEGIN`, two `PRAGMA query_only`, `COMMIT`) |
| Leader fence | 11 | The ingest transaction fences three times (outer context, `insert_row_with_token_on`, `record_completed_node_state_on`), each a CAS update, a clock read and a heartbeat update, then renews once more after the body |
| Membership fence | 4 | One per member-fenced transaction |
| Other clock reads | 4 | Eight clock queries per row in total with the four inside the leader fence |
| Reads of rows this transaction just wrote | 13 | See finding 3 |
| Pre-write lookups | 6 | Routing target and ownership checks, the disposition before-image |

## Governing rule

The composer review's rule applies unchanged: **verify when taking
ownership, retain the verified immutable representation, and recompute only
facts whose inputs changed.** In the executor the retained thing is usually
one of three: a compiled SQL statement, a fence already taken on this
connection in this transaction, or a row the transaction itself just wrote.

## Findings

Shares are of whole-process CPU on the named workload unless stated.
"Ruling" means the change alters when or how often a fence, clock sample or
durable intermediate state is taken, and should not be made on a
performance argument alone.

| # | Finding | Evidence | Expected saving | Risk | Ruling |
|---|---|---|---|---|---|
| 1 | SQL statements are rebuilt and re-keyed on every call | 26–30% of samples in every workload; micro-benchmark 94 → 12 µs per fence statement | Up to ~27% | Low, mechanical, wide | No |
| 2 | The leader fence is re-taken inside an already-fenced transaction | 6 of 84 statements per row; leader-fence functions are 9–14% of samples | ~4–6% | Medium | **Yes** |
| 3 | Rows are read back after the transaction wrote them | 13 statements per gate row | ~10% | Low–medium | No |
| 4 | Sink flush resolves lineage one token at a time, each read in its own read-only transaction | ~20 statements per row; 4.8% of samples on the transform workload | ~4% | Low–medium | No |
| 5 | A transform's output contract is re-recorded on every row | One no-op write transaction per transform per row; 6.2% on the transform workload | ~6% (transform pipelines) | Low | No |
| 6 | `SchemaContract.version_hash()` is recomputed, and transforms rebuild an identical contract per row | 5 calls per row, 1.6–2.8% of samples | ~2–3% | Low | No |
| 7 | One gate decision takes four write transactions and one read transaction | 10 commits per row | Largest remaining lever after 1–4 | High | **Yes** |
| 8 | Fixed startup is ~7.2 s per run | 5-row run: pipeline work 0.32 s | Up to ~3 s per run | Low–medium | Part |
| 9 | Smaller per-row recomputation | Each 1–2% | ~3–4% together | Low | No |

### 1. Statements are rebuilt on every call

Every repository method builds its `select()`/`update()`/`insert()` inline
with the row's values as literals, so each call pays for expression
construction (`coercions.expect`, `BindParameter.__init__`, operator
dispatch) and then for `_generate_cache_key` to discover that the compiled
form is already cached. SQLAlchemy memoises the cache key on the statement
object, so a module-level statement with `bindparam()` placeholders pays
neither.

Measured in isolation on SQLAlchemy 2.1.8, in-memory SQLite, CPU per call:

| Statement | Built per call | Prebuilt | 
|---|---:|---:|
| Leader fence CAS (`UPDATE run_coordination …`) | 94.3 µs | 11.9 µs |
| Decision clock (`SELECT strftime(…)`) | 74.3 µs | 20.1 µs |

Prototype (reverted): prebuilt statements for five call sites only —
`read_landscape_decision_time`, `read_landscape_transaction_time`,
`verify_and_extend_leader_fence`, `_renew_leader_deadline_on` and
`verify_membership_fence` — which together issue about 25 of the 84
statements per gate row — plus finding 6's memoised
`SchemaContract.version_hash()`. Interleaved A/B, four runs each:

| Workload (1,000 rows) | Baseline CPU, min / median | Prototype CPU, min / median | Change, min / median |
|---|---:|---:|---:|
| Gate (`large_scale_test` slice) | 27.8 s / 28.3 s | 24.5 s / 25.0 s | −11.9% / −11.7% |
| `transform_pipeline` × 200 | 32.2 s / 32.2 s | 26.2 s / 27.2 s | −18.4% / −15.6% |

Both columns include the ~7.2 s fixed startup, so the per-row reduction is
larger: about 16% on the gate workload and 20–24% on the transform one. The
prototype's transform runs drifted downward across the four repetitions
(29.5 → 26.2 s) while the baseline did not, so treat the transform figure
as the less certain of the two.

Sink output digests were identical before and after on every run.

Recommended order: the fence and clock helpers first (five functions, a
quarter of all statements), then the per-row scheduler and node-state verbs
(`scheduler/queue.py`, `scheduler/leases.py`, `scheduler/dispositions.py`,
`scheduler/events.py`, `scheduler/work_items.py`,
`execution/node_states.py`, `data_flow/tokens.py`). Statements whose shape
varies by branch (the four `_transition_on` update shapes) become one
prebuilt statement per shape. This changes no SQL text and no recorded
value; the existing repository suites and the testcontainer suite are the
verification, since the statements also run on PostgreSQL.

### 2. The leader fence is repeated inside one transaction

`fenced_leader_transaction` verifies and extends the fence as the first
statement of its `BEGIN IMMEDIATE` transaction
(`run_coordination_repository.py:489`). The ingest verb then calls
`insert_row_with_token_on` (`data_flow/tokens.py:457`) and
`record_completed_node_state_on` (`execution/node_states.py:257`) on that
same connection, and each calls `verify_and_extend_leader_fence` again:
three statements apiece, for a seat whose row lock the transaction already
holds. The context then renews a fourth time after the body.

The repeated calls exist because the `_on` verbs accept a raw `Connection`
and cannot tell whether their caller fenced it. The fix that follows the
governing rule is to carry the proof: have `fenced_leader_transaction`
yield a witness type that only it can construct, and let the `_on` verbs
accept that instead of fencing again. `has_issued_deadline(conn, key)`
already records the same fact per connection, but a type is the stronger
form.

Needs a ruling because ADR-047 states that raw-connection callers retain an
explicit renewal obligation, and because the post-body renewal is a
deliberate part of the leader context. Dropping the two nested fences is
the conservative step (6 statements per row); making the post-body renewal
conditional on elapsed time is a separate, smaller question.

### 3. Read-back after write

Thirteen statements per gate row re-read what the same transaction just
wrote:

- `record_completed_node_state_on` inserts a `node_states` row and selects
  it back (`node_states.py:278`, `:299`); `complete_node_state_on` does the
  same after its update (`:630`, `:660`).
- `validate_work_item_references` selects the token, row and node
  (`scheduler/work_items.py:195`, `:208`, `:175`) eight statements after the
  ingest transaction inserted the first two.
- The work item is inserted, selected (`work_items.py:319`), selected again
  (`queue.py:339`), selected a third time (`leases.py:214`), updated, and
  selected a fourth time (`leases.py:326`).
- `_transition_on` selects the before-image, updates, selects the
  after-image, then runs a fourth statement for the bundle check
  (`dispositions.py:886`, `:989`, `:991`).
- `_insert_or_load_routing_decision` re-selects the events and the group
  ownership after inserting them (`node_states.py:1519`, `:1520`).

`UPDATE … RETURNING` and `INSERT … RETURNING` are already used on both
dialects (`scheduler_events` returns `seq`), so the after-images can come
back from the write itself, and the bundle predicate can ride in the same
`RETURNING`. The reference validation inside ingest can be skipped when
the caller is the verb that inserted those rows; standalone callers keep
it. None of this changes a recorded value. It does remove a defensive
re-read, so each removal should keep its `rowcount` check and its
`AuditIntegrityError` on a missing row.

### 4. Sink flush lineage is an N+1

`resolve_sink_effect_members` (`execution/sink_effect_identity.py:236`)
loops over the batch and, per token, calls `get_token`, the lineage walk
(`get_token_parents`, lineage frames) and `get_row`. Each goes through
`ReadOnlyDatabaseOps`, which opens a transaction, sets
`PRAGMA query_only = ON`, runs one select, sets it `OFF` and commits. That
is about 20 statements per sink row for a batch the caller already holds
in full. `_LineageQueryCache` memoises within the call but does not batch.

Fix: load tokens, rows, parents and lineage frames for the whole candidate
tuple with `IN (…)` queries (chunked) on one read-only connection, then run
the existing per-token validation over the loaded maps. The bounded
lineage resolver and every integrity check stay as they are.

### 5. The output contract is re-recorded per row

`TransformExecutor._prepare_success_completion` (`transform.py:800`) calls
`update_node_output_contract` whenever the emitted contract object is not
the input row's contract object, which for a field-adding transform is
every row. Each call opens a member-fenced write transaction, selects the
node `FOR UPDATE`, parses the stored contract JSON, hashes both contracts,
finds them equal and returns (`data_flow/graph.py:433–453`).

The record is a monotone fold: once this worker has folded version hash H
into node N, folding H again is a no-op by construction. A per-executor
memo of `(node_id, version_hash)` already folded skips the transaction on
every row after the first, with the first row and any genuinely new
contract taking the existing path unchanged.

### 6. Contract hashing

`SchemaContract` is a frozen, slotted dataclass, and `version_hash()`
serialises and hashes its fields on every call. `serialize_row_payload`
runs twice per row and reaches it twice each time
(`PipelineRow.to_checkpoint_format` and
`SchemaContract.to_checkpoint_format`). On the gate workload 2,507 calls
computed one distinct value. Memoising the digest on the instance removes
that.

On the transform workload the memo only partly helps: 11,007 calls, 3,999
computations over 3,508 distinct contract objects, because transforms
construct a fresh, equal `SchemaContract` for each row. Retaining the
output contract per (input contract, transform) removes the rebuild, the
hash, and — through finding 5's identity check — the per-row contract
write.

### 7. Transactions per transition

A config gate is an in-memory expression. Recording it takes
`begin_node_state` (commit), a read-only transaction for
`_assert_routing_targets_recordable`, `record_routing_event` (commit),
`complete_node_state` (commit) and `mark_pending_sink` (commit), after the
ingest commit. Each write transaction pays `BEGIN IMMEDIATE`, a membership
fence and a WAL commit.

For a transform that makes an external call, a durable OPEN state before
the call is the point. For a gate, and for the complete-then-dispose pair
of any node, the intermediate commits record states no reader can act on.
Folding them is the biggest lever left after findings 1–4, and it changes
what a crash leaves behind, so it is a design decision rather than an
optimisation: it needs a ruling and probably an ADR note against ADR-030.

### 8. Startup

py-spy on the 5-row run (7.2 s CPU; `elspeth --help` alone is 1.8 s):

| Cost | Share | Mechanism |
|---|---:|---|
| `import litellm` | 29% (~2.0 s) | `cli.py:2000` calls `read_openrouter_catalog_snapshot_id()` to stamp `runs.openrouter_catalog_sha256` on every run, including pipelines with no LLM node |
| Plugin discovery | 15% (~1.1 s) | `discover_all_plugins` imports every plugin module, including the Chroma client, whichever plugins the pipeline names; first reached from `_preflight_raw_settings_sink_effects` |
| Runtime VAL manifest | 9% (~0.6 s) | `build_runtime_val_manifest` hashes callable implementations in `begin_run`; cached per process, which a CLI run never reuses |

The catalog digest is an audit column, so skipping it for pipelines without
an LLM node needs a ruling; computing it without importing all of litellm
does not. This matters for short runs and for the examples and test suites
that start many of them, not for long pipelines.

### 9. Smaller recomputation

- `compute_full_topology_hash(graph)` runs for every checkpoint draft
  (`orchestrator/checkpointing.py:88`); the graph is fixed for the run.
  1.5% on the transform workload.
- `ValueTransform` re-derives its declared output fields and expression
  kinds per row (`created_output_fields`, `_derived_kinds`,
  `expression_kinds`); they depend only on configuration. 1.7%.
- `list_pending_blocked_barrier_items` polls in its own read transaction on
  every drain of an aggregation or coalesce pipeline (the rollbacks in the
  baseline table).
- `pending_sink_bundle_clause()` rebuilds a large boolean expression per
  call (1.7%); it folds into finding 1.

## Looked at and left alone

- **Payload store path validation** (`realpath`, symlink and directory
  checks on each of three stores per row, 1.4–3.3%). This is deliberate
  hardening of a filesystem trust boundary.
- **`BEGIN IMMEDIATE`, WAL and `synchronous=NORMAL`.** Native commit time
  is 2.5–3.4%; the settings are documented choices.
- **One clock sample per locked decision** (ADR-047). The count falls with
  findings 2 and 7; the rule itself is not the problem.
- **Plugin execution.** Source, gate expression and sink code are under 2%
  of samples on the gate workload. Plugin code is 7.5% on the transform
  workload, part of which is finding 9.

## Master fix list

Everything found, in the order it should be worked. Nothing here has
landed; update the status column as items do. "Finding" points at the
section above; items marked "new" were noticed during the review and are
described only here. IDs are stable so commits and issues can cite them.

### Ready to fix (no ruling needed)

| ID | Fix | Finding | Scope | Verification | Status |
|---|---|---|---|---|---|
| E-1a | Prebuild the fence and clock statements with `bindparam()` | 1 | `database_clock.py`, three functions in `run_coordination_repository.py` | Repository suites; testcontainer (statements run on PostgreSQL); smoke gate | open — prototyped, reverted |
| E-6a | Memoise `SchemaContract.version_hash()` on the frozen instance | 6 | `contracts/schema_contract.py` | Contract and checkpoint unit suites; pickle/equality unaffected by a non-compared slot | open — prototyped, reverted |
| E-6b | Stop transforms rebuilding an equal `SchemaContract` per row; retain it per (input contract, transform) | 6 | `plugins/infrastructure/base.py` stamp path | Transform executor and ADR-050 declaration suites | open |
| E-5 | Skip `update_node_output_contract` once this executor has folded that version hash into that node | 5 | `engine/executors/transform.py` | Executor unit suite with a negative control (a genuinely new contract still writes); smoke gate | open |
| E-1b | Prebuild the per-row scheduler, node-state and token statements | 1 | `scheduler/{queue,leases,dispositions,events,work_items}.py`, `execution/node_states.py`, `data_flow/tokens.py`; includes `pending_sink_bundle_clause()` | As E-1a; one prebuilt statement per branch shape | open |
| E-4 | Batch sink-flush lineage loads onto one read-only connection | 4 | `execution/sink_effect_identity.py`, `query_repository.py` | Sink-effect suites; testcontainer; identical `lineage_hash` values before and after | open |
| E-3a | Return after-images with `RETURNING` instead of selecting them back | 3 | `node_states.py`, `scheduler/dispositions.py`, `scheduler/leases.py`, `scheduler/work_items.py` | Repository suites; testcontainer; every removed read keeps its `rowcount` check and integrity error | open |
| E-3b | Skip `validate_work_item_references` and the repeated work-item selects when the caller is the ingest verb that just inserted those rows | 3 | `scheduler/queue.py`, `scheduler/work_items.py` | Scheduler suites; standalone callers keep the validation | open |
| E-9a | Compute the topology hash once per run, not per checkpoint draft | 9 | `orchestrator/checkpointing.py` | Checkpoint suites | open |
| E-9b | Derive `ValueTransform` declared fields and expression kinds once at construction | 9 | `plugins/transforms/value_transform.py` | Plugin unit suite | open |
| E-9c | Run the barrier intake poll on the drain's existing connection, or skip it when no barrier node has blocked items | 9 | `engine/barrier_coordination.py`, `scheduler/barrier.py` | Aggregation and coalesce suites | open — mechanism not yet read in full |
| E-8a | Compute the catalog digest without importing all of litellm | 8 | `plugins/llm/model_catalog.py` | Digest equals the current value on the same litellm version | open |
| E-8b | Avoid rebuilding the runtime VAL manifest on every CLI start | 8 | `contracts/runtime_val_manifest.py` | Manifest equality; invalidation on a changed tree needs care (compare composer item P-15) | open — design not settled |
| E-10 | Give engine-internal reads a plain read transaction instead of the `PRAGMA query_only` wrapper built for untrusted SQL | new | `landscape/database.py`, `_database_ops.py` | Confirm the MCP query path keeps the wrapper; testcontainer | open — partly subsumed by E-4 |
| E-11 | Measure the run-end `token_outcomes … NOT EXISTS (token_parents …)` query at two more run sizes and index or rewrite it if it is super-linear | new | One query; site not yet located | 0.58 s at 1,000 fork/coalesce rows is the only data point | open — measurement first |

### Needs a ruling first

| ID | Decision | Finding | Why it is a decision | Status |
|---|---|---|---|---|
| R-2a | Let `_on` verbs accept a fenced-connection witness instead of re-taking the leader fence | 2 | ADR-047 gives raw-connection callers an explicit renewal obligation | not started |
| R-2b | Make the leader context's post-body renewal conditional on elapsed time | 2 | Deliberate part of the leader context | not started |
| R-7 | Fold a gate's begin/route/complete/dispose commits, and the complete/dispose pair of any node, into fewer transactions | 7 | Changes what a crash leaves behind; likely an ADR-030 note | not started |
| R-8 | Record the OpenRouter catalog digest only for pipelines with an LLM node | 8 | Audit column on `runs`; today a CSV-only run is stamped `bundled` | not started |
| R-12 | Take the fence locks with `SELECT … FOR UPDATE` rather than a no-op `UPDATE` | new | ADR-030 D7 chose the verify-UPDATE form; about eight no-op writes per row land on one hot row, which on PostgreSQL means dead tuples. Inferred from the SQL, not measured | not started |
| R-13 | Decide whether `node_states.started_at`/`completed_at` should use the Landscape clock | new | `record_completed_node_state_on` uses the process clock while scheduler rows in the same transaction use the database clock; may be correct under ADR-047's scope | not started — confirm it is a defect first |

### Measurement and housekeeping

| ID | Item | Status |
|---|---|---|
| M-1 | Repeat the baseline on PostgreSQL (testcontainer) and with a follower worker; the review is SQLite, single process | open |
| M-2 | Re-measure after E-1a to E-3b and replace the estimates in the findings table with measured figures | open |
| M-3 | Profile one external-call pipeline (ChaosLLM example) to confirm the overhead here is the floor, not something the call path adds to | open |
| H-1 | Move the harness from `executor-perf-harness/*.py.txt` to `notes/` as lint-clean `.py` files (that path already allows `print`) | open |
| H-2 | Commit a generator for the scaled workloads so the 1,000-row inputs are reproducible rather than living in a gitignored lane directory | open |

Outside this review: PR 359's base (`release/0.8.11-candidate`) had moved
past the commit the 0.8.12 branch was cut from when it was checked (on a
shallow clone); confirm on a full clone before relying on its CI.

### Order

E-1a → E-6a → E-6b → E-5 → E-1b → E-4 → E-3a → E-3b → M-2 → E-9a/b/c →
E-8a/b → E-10 → E-11, with the R items raised once M-2 shows what is left.

E-1a, E-5, E-6a and E-6b are independent of each other and of the fences.
E-3 and E-4 touch Landscape SQL and locks, so they owe the
testcontainer suite as well as the default one. Every fix should keep the
smoke gate the composer plan used for engine changes: same exit code, same
row counts, same sink output digests and the same `node_states`
`input_hash`/`output_hash` values on `transform_pipeline` and
`error_routing` before and after.

## Method

- **CPU profile:** `py-spy record -r 200 -f raw -- python -m elspeth.cli run
  --settings <settings> --execute`, classified by leaf frame and by the
  presence of marker frames in the stack. `cProfile` was used for call
  counts and caller attribution only; it roughly doubles the run and
  over-weights Python-heavy code.
- **SQL counts and cursor time:** listeners on
  `sqlalchemy.engine.Engine` for `before_cursor_execute`,
  `after_cursor_execute`, `commit`, `rollback` and `begin`, installed before
  importing `elspeth.cli` and calling `app()` in-process.
- **Per-row trace:** the same listeners recording the five innermost
  ELSPETH frames for a window of statements in the middle of a run.
- **Timing:** bash `time` around `elspeth run`, interleaving baseline and
  prototype because run-to-run variation on this host was about ±3 s on a
  45 s run.

## Limits

- SQLite only, one process, CLI entry point. PostgreSQL, follower workers
  and the web execution service were not measured. The statement counts
  carry over; the cost of each round trip on PostgreSQL will be higher, so
  findings 2–4 and 7 should matter more there, and finding 1 less in
  relative terms.
- No LLM, HTTP or other external-call pipelines: those are dominated by the
  call, and the overhead measured here is the floor under them.
- Baseline rows are single runs; the prototype comparison is the only
  repeated measurement. Expected savings other than the prototype are
  estimates from sampled shares and statement counts, and they overlap
  (removing a statement also removes its construction cost), so they do not
  add up.
- The prototype was applied with a script and reverted; nothing in `src/`
  differs from `231cc44e`. Its diff is kept as
  `executor-perf-harness/prototype-AB.patch` for reference, not as a
  reviewed change.

## Harness

[`executor-perf-harness/`](executor-perf-harness/) holds the scripts behind
every number here: `sqlcount.py` (statement, commit and cursor-time
counts), `sqltrace.py` (ordered per-row trace with call sites), `spy.py`,
`spy_sql.py` and `spy_incl.py` (py-spy raw-stack analysis), `bench_stmt.py`
(the per-call versus prebuilt micro-benchmark), `count_vh.py` (contract-hash
hit rate, needs the prototype applied) and `timeit.sh`. The Python files
are stored as `.py.txt`: they are throwaway measurement scripts kept as
evidence, not maintained code, so drop the suffix to run one. They expect scaled
workloads under the gitignored `.claude/lanes/exec-perf/<name>/` with a
`settings.yaml`, `input.csv`, `output/` and `runs/`; delete them with this
document.
