# Landscape System Architecture

Maintained subsystem reference for the 0.8.3 candidate release line.

Landscape is ELSPETH's audit database and lineage read model. It records run
configuration, source rows, DAG nodes and edges, token lineage, node execution
states, external calls, routing events, terminal outcomes, checkpoints, durable
sink and coalesce effects, export snapshots, artifacts, and sidecar-journal
publication.

The maintained system-level overview lives in
[ARCHITECTURE.md](../../ARCHITECTURE.md). This document focuses on the
Landscape subsystem.

## Schema authority

[`schema.py`](../../src/elspeth/core/landscape/schema.py) owns the SQLAlchemy
metadata, constraints, indexes, and `SQLITE_SCHEMA_EPOCH`. The current release
requires Landscape epoch 49 and Sessions epoch 72. Startup accepts an empty
store or the exact current schema; it does not migrate or relabel earlier
evidence. Epoch 49 adds the `pending_identities_purged` authentication event.
The [schema reset runbook](../runbooks/staging-session-db-recreation.md) covers
the session-only 71-to-72 cutover, earlier paired cutovers, and preservation
of the separate local authentication store.

Source-file and tool counts are transient inventories, not subsystem contracts.
Use the live schema metadata and the [MCP guide](../guides/landscape-mcp-analysis.md)
when inspecting the installed system.

## Trust Model

Landscape data is Tier 1: it is ELSPETH's own audit evidence. The subsystem
should fail loudly on corrupt, cross-run, or internally inconsistent audit data
instead of coercing it into a plausible answer.

Important consequences:

- Row, token, node, and run ownership checks are part of the read and write
  contract.
- Composite keys and composite foreign keys are intentional, not incidental
  schema noise.
- Token outcomes use the ADR-019 two-axis model: `completed`, `outcome`, and
  `path`.
- External calls have exactly one parent: either a node state or a source/sink
  operation.
- Hashes and payload references must survive retention and payload deletion
  boundaries.
- Per-source schema contracts and per-source lifecycle state (ADR-025) live
  in `run_sources`. The singular run-level `contract_json` writer has been
  removed; resume of a run with no `run_sources` rows raises
  `EmptyResumeStateError` rather than reconstructing a fabricated contract.
- Scheduler lease ownership is CAS-gated (ADR-026). A row in
  `token_work_items.status = LEASED` with `NULL` or empty `lease_owner` is
  a Tier-1 invariant violation; the schema enforces the invariant
  structurally via `ck_token_work_items_lease_owner_required_when_leased`.
  Mismatched `expected_lease_owner` on a state-changing call raises
  `AuditIntegrityError`; scheduler row tampering is a crash-on-anomaly
  scenario, not a recoverable one.
- Routing events are run-scoped as of schema epoch 22. `routing_events.run_id`
  participates in composite foreign keys to `node_states(state_id, run_id)` and
  `edges(edge_id, run_id)`, so a stored gate decision cannot accidentally bind
  to a state or edge from another run.
- Token ancestry and validation-error associations are run-scoped as of epoch
  29. A token parent or quarantined-row link cannot bind to evidence from a
  different run.
- Sink and audit-export publication is represented as a durable effect stream.
  `UNKNOWN` is a valid blocked recovery result when target evidence cannot prove
  whether a request committed; it is never coerced into permission to retry.

## Module Layout

`src/elspeth/core/landscape/` is split by repository responsibility:

| Module | Responsibility |
|--------|----------------|
| `schema.py` | Authoritative SQLAlchemy Core table definitions, constraints, indexes, and schema epoch. |
| `database.py` | Database construction, validation, SQLite/PostgreSQL connection handling. |
| `_database_ops.py` | Shared database operation helpers. |
| `factory.py` | Composition point for repository instances and plugin audit writer adapters. |
| `run_lifecycle_repository.py` | Run creation, status, export status, runtime manifest, attribution, run-level metadata, and per-source lifecycle in `run_sources`. |
| `data_flow_repository.py` | Rows, tokens, token parents, token outcomes, validation errors, and transform errors. Enforces source-row identity invariants on write (`source_row_index` / `ingest_sequence` are non-fabricable). |
| `execution_repository.py` | Node states, routing events, calls, operations, batches, artifacts, audit-export snapshots, and the sink-effect repository surface. |
| `scheduler_repository.py` | Durable token scheduler (`token_work_items`): claim/lease, CAS-gated state transitions, expired-lease recovery, pending-sink handoff. ADR-026 authoritative surface. |
| `query_repository.py` | Lineage and audit read queries used by explain/MCP/export paths. |
| `model_loaders.py` | Tier-1 row-to-model validation. |
| `exporter.py` | Audit export assembly. |
| `formatters.py` | Human-facing lineage formatting. |
| `reproducibility.py` | Reproducibility grade calculation. |
| `row_data.py` | Source-row payload retrieval helpers. |
| `auth_audit_repository.py` | Web/auth audit records. |

There is no longer a monolithic `core/landscape/recorder.py` file. Older docs
that refer to that file are historical snapshots.

## Table Groups

The main table groups are below; the schema metadata is the complete inventory.

| Group | Tables |
|-------|--------|
| Run metadata and admission | `runs`, `run_start_admissions`, `run_attributions`, `auth_events`, `preflight_results`, `secret_resolutions`, `run_web_plugin_policy` |
| Schema identity | `elspeth_schema_identity` |
| Multi-source ingestion (ADR-025) | `run_sources` |
| Static graph | `nodes`, `edges` |
| Data flow | `rows`, `tokens`, `token_parents`, `token_outcomes` |
| Durable scheduler (ADR-026) | `token_work_items`, `scheduler_events` |
| Run coordination (ADR-030) | `run_coordination`, `run_coordination_events`, `run_workers` |
| Unified lineage and group accounting | `token_lineage_frames`, `group_records`, `group_losses`, `collector_group_failures` |
| Execution and outputs | `node_states`, `operations`, `calls`, `routing_events`, `artifacts` |
| Batching and aggregation | `batches`, `batch_members`, `batch_outputs`, `aggregation_results`, `aggregation_result_outputs`, `aggregation_result_members` |
| Replay and verification | Source-call linkage on `calls`, `call_verifications` |
| Errors | `validation_errors`, `transform_errors` |
| Recovery | `checkpoints` |
| Durable coalesce | `coalesce_effects`, `coalesce_effect_members` |
| Durable sink effects | `sink_effect_streams`, `sink_effects`, `sink_effect_members`, `sink_effect_attempts`, `sink_effect_export_snapshots` |
| Audit-export snapshots | `audit_export_snapshots`, `audit_export_snapshot_chunks` |
| Sidecar journal | `sidecar_journal_outbox` |

### Artifact logical-effect identity

`artifacts.idempotency_key` is an opaque logical-effect key scoped by `run_id`.
A non-null key is unique within the run, and an identical registration returns
the first artifact while a divergent retry raises a Tier-1 integrity error.

An artifact has exactly one production authority: a historical node state or a
durable sink effect. Effect-backed artifacts carry a composite reference to
`sink_effects(effect_id, run_id, sink_node_id)` and are registered from the
effect's immutable final descriptor. The artifact records whether publication
was performed and whether its evidence was returned, reconciled, inherited, or
virtual. Audit identity therefore converges with external-effect identity
rather than being added as an unrelated row after I/O.

### Durable sink and export effects

Every supported sink publication follows one persisted lifecycle:

```text
RESERVED -> PREPARED -> IN_FLIGHT -> FINALIZED
```

- `sink_effect_streams` orders effects for one target/role and prevents a
  successor from overtaking an uncertain predecessor.
- `sink_effects` stores the immutable target binding and plan, current fenced
  lease generation, reconciliation result, and final descriptor.
- `sink_effect_members` binds the ordered pipeline tokens and per-member
  outcome evidence. Failsink members retain the exact primary effect that
  produced them.
- `sink_effect_attempts` records inspect, commit, and reconcile intent before
  the adapter call, then records returned, response-lost, or error state.
- `audit_export_snapshots` and `sink_effect_export_snapshots` bind a sealed
  export snapshot to the effect that publishes it.

After a lost response, the coordinator may publish again only when the adapter
proves the exact plan was not applied. A proven application finalizes without
another commit; an `UNKNOWN` result remains blocked. Run-scoped ancestry and
error links keep recovery evidence bound to the effect's original members.
The sidecar journal outbox is written in the audit transaction, so journal
publication can recover without inventing a new audit event.

### Unified lineage and group accounting

`token_lineage_frames` and scheduler `lineage_path_json` are the lineage
authority. The retired `fork_group_id`, `expand_group_id`, and `branch_name`
columns are not alternate readers. `join_group_id` identifies a merge event,
not a lineage path. `group_records` and `group_losses` retain group lifecycle
and losses; `collector_group_failures` records a failed collector verdict even
when no member arrived. The [token lifecycle](token-lifecycle.md) explains how
these records relate to terminal outcomes and recovery.

### Multi-source ingestion (ADR-025)

`run_sources` records per-source lifecycle state, per-source schema
contract, and per-source plugin configuration for every named source in a
run. The singular `runs.contract_json` writer has been removed; per-source
contracts are the single source of truth.

| Column | Type | Notes |
|--------|------|-------|
| `run_id` | `String(64)` NOT NULL | FK to `runs.run_id`; composite PK with `source_node_id`. |
| `source_node_id` | `String(64)` NOT NULL | Composite PK with `run_id`; composite FK to `(nodes.node_id, nodes.run_id)`. |
| `source_name` | `String(64)` NOT NULL | Operator-facing name; unique per run via `UniqueConstraint(run_id, source_name)`. |
| `plugin_name` | `String(128)` NOT NULL | Source plugin identifier. |
| `lifecycle_state` | `String(32)` NOT NULL | One of `ready`, `loading`, `exhausted`, `loaded`, `interrupted` (enforced by `ck_run_sources_lifecycle_state`). Mirrors `RunSourceLifecycleState`. |
| `config_hash` | `String(64)` NOT NULL | Hash of plugin config at registration. |
| `schema_json` | `Text` | Declared schema (raw form). |
| `schema_contract_json` | `Text` | Resolved per-source `SchemaContract` (authoritative for resume). |
| `schema_contract_hash` | `String(32)` | Canonical hash prefix of `schema_contract_json` for drift detection. |
| `field_resolution_json` | `Text` | Per-field resolution metadata (original_name, normalised name). |
| `recorded_at` | `DateTime(tz)` NOT NULL | When the row was persisted. |

Indexes: `ix_run_sources_run`, `ix_run_sources_source_name`.

### Sealed finite-source recovery

With `snapshot_for_resume: true`, a single CSV/JSON source records a bounded,
content-addressed snapshot of its complete validated emission stream before
downstream processing. A completed `source_load` operation binds metadata to
the snapshot payload. Resume requires that unique completed operation and
validates the metadata hash, exact snapshot version, source identity, contracts,
and retained bytes. It restores quarantined emissions with their original
validation-error identities rather than recording a new classification.

The source lifecycle must have completed before recovery; partial ingestion
has no live-file fallback. Snapshot eligibility does not override checkpoint,
graph, coordination, or external-effect admission. See the
[resume runbook](../runbooks/resume-failed-run.md) for supported sources and limits.

The [purge manager](../../src/elspeth/core/retention/purge.py) follows retained
source-output dependencies, including the snapshot referenced by source-load
metadata. Active/interrupted runs and retained outputs protect their required
payloads. Corrupt dependency evidence refuses the affected purge; a failed
child deletion keeps its discovery metadata available for retry. Reproducibility
updates account for payloads already absent on an idempotent purge retry.

### Replay, verification, and portable export

Replay restores recorded source rows and external responses without live
provider calls. Verify reads current sources and makes admitted external calls
to compare their results with the source run. `calls` links replayed and
verified calls to the original source call; `call_verifications` records
comparison evidence. Missing or incompatible retained evidence cannot silently
downgrade either mode to live execution. Both modes write a new audit run and
suppress configured sink publication. See the
[replay/verify contract](design-notes/replay-verify-runtime-contract.md).

Audit-export reservations reuse the original durable effect and stream
position on exact retry. Delivered JSON or CSV evidence can be checked without
opening the original Landscape database using `elspeth audit-export verify`.
The verifier captures bounded regular files into private storage and checks
that snapshot's signatures, hashes, projections, and manifest. Its artifact
digest identifies the verified bytes; it does not grant continued custody of
the original path. See the [export guarantees](../release/guarantees.md).

### Durable scheduler (ADR-026)

`token_work_items` is the durable unit of work for the token scheduler.
Every scheduled continuation (initial ingest, downstream node hop, barrier
resolution, pending-sink handoff) writes a row before any in-memory
`WorkItem` is touched. The scheduler row is authoritative for resume;
in-memory `pending_items` is a cache and must never diverge from the
durable row (`SCREAM` invariant in the drain loop).

| Column | Type | Notes |
|--------|------|-------|
| `work_item_id` | `String(64)` PK | `sha256(f"{run_id}:{token_id}:{node_id or '<terminal>'}:{attempt}")` — deterministic. |
| `run_id` | `String(64)` NOT NULL | Indexed; tenant of the work item. |
| `token_id` | `String(64)` NOT NULL | Composite FK to `(tokens.token_id, tokens.run_id)`. |
| `row_id` | `String(64)` NOT NULL | Composite FK to `(rows.row_id, rows.run_id)`. |
| `node_id` | `String(64)` | Composite FK to `(nodes.node_id, nodes.run_id)`; `NULL` for terminal-handoff rows. |
| `step_index` | `Integer` NOT NULL | Secondary ordering within a token's lifetime. |
| `ingest_sequence` | `Integer` NOT NULL | Cross-source ordering primitive; mirrors `rows.ingest_sequence`. |
| `row_payload_json` | `Text` NOT NULL | Cached row payload; scrubbed on terminal/failure. |
| `status` | `String(32)` NOT NULL | One of `READY`, `LEASED`, `WAITING`, `BLOCKED`, `PENDING_SINK`, `TERMINAL`, `FAILED`. |
| `queue_key`, `barrier_key` | `String(128)` | Used by QUEUE fan-in and barrier-join coalesce. |
| `on_success_sink` | `String(128)` | Sink-bound continuation (preserved across resume). |
| `pending_sink_name` | `String(128)` | Set when the row is in `PENDING_SINK`. |
| `pending_outcome` / `pending_path` / `pending_error_hash` / `pending_error_message` | `String(32)` / `String(64)` / `String(64)` / `Text` | Pre-computed sink-outcome record so the transform does not re-run on lease expiry. |
| `join_group_id` | `String(128)` | Coalesce merge-event identity. |
| `lineage_path_json` | `Text` NOT NULL | Unified lineage path carried into the durable row; the token's frames live in `token_lineage_frames`. |
| `row_union_name`, `collector_name` | `String(128)` | Declared row-union or collector context retained across recovery. |
| `coalesce_node_id`, `coalesce_name` | `String(NODE_ID_COLUMN_LENGTH)` / `String(128)` | Resume-target for coalesce cursors. |
| `attempt` | `Integer` NOT NULL | Incremented when `recover_expired_leases` reaps a non-`PENDING_SINK` row; preserved for `PENDING_SINK`. |
| `lease_owner` | `String(128)` | Registered `worker:<run_id>:<uuid>` identity holding the row in production; direct legacy repository harnesses may use an explicit opaque identity. Required non-empty when `status='LEASED'` (see check constraint). |
| `lease_expires_at` | `DateTime(tz)` | Used by `recover_expired_leases`; CAS predicate. |
| `available_at` | `DateTime(tz)` NOT NULL | Earliest claim time (delayed-retry support). |
| `created_at`, `updated_at` | `DateTime(tz)` NOT NULL | Audit timestamps. |

Constraints:

- `UniqueConstraint(run_id, token_id, node_id, attempt)` — one row per
  attempt per token-node continuation.
- `CheckConstraint ck_token_work_items_lease_owner_required_when_leased`
  — `status='LEASED'` implies `lease_owner IS NOT NULL` and non-empty.
- Composite FKs to `tokens`, `rows`, `nodes` (twice — `node_id` and
  `coalesce_node_id`).

Indexes:

- `ix_token_work_items_ready` on `(run_id, status, available_at)` —
  drives `claim_ready`.
- `ix_token_work_items_lease` on `(run_id, status, lease_expires_at)` —
  legacy lease-recovery index.
- `ix_token_work_items_recovery` on `(run_id, status, lease_owner,
  lease_expires_at)` — covering index for the multi-worker drain sweep
  (strict `recover_expired_leases` scopes the query to the token's
  `run_id` and filters `lease_owner != coordination_token.worker_id`).
- `uq_token_work_items_terminal_identity` partial unique on
  `(run_id, token_id, attempt)` where `node_id IS NULL` — exactly one
  terminal-handoff row per attempt.

### Per-row source identity (ADR-025)

`rows` carries the source-identity primitives `source_node_id`,
`source_row_index`, and `ingest_sequence` as non-nullable columns. These
fields are Tier-1 evidence and must not be fabricated by sources or by
synthesized-run write paths; the `create_row` write boundary raises
`AuditIntegrityError` when any are missing. `row_index` is not a substitute
for either source index or ingest sequence. See [Plugin Protocol — Source row identity](../contracts/plugin-protocol.md#source-row-identity--no-fabrication).

| Identity column | Meaning |
|----------------|---------|
| `source_node_id` | Which named source emitted the row. Composite FK to `nodes`. |
| `source_row_index` | The source's own row index within its emission stream. |
| `ingest_sequence` | Global per-run monotone ordering across all sources. |
| `row_index` | Position in source as observed by the orchestrator (may differ from `source_row_index` during resume; do not use as a substitute). |

## Key Schema Rules

- `nodes` is keyed by `(node_id, run_id)`. Join nodes with both keys.
- `edges` carries `run_id` and uses composite foreign keys to nodes.
- `tokens` carries `run_id` and points to source rows.
- `token_outcomes` stores one terminal row per token via a partial unique index
  on `completed = 1`.
- `calls` is parented by exactly one of `state_id` or `operation_id`.
- `validation_errors.row_id` is nullable because some validation failures occur
  before a row can be persisted.
- `runtime_val_manifest_json` records the runtime validation manifest in force
  for a run.
- `rows.source_node_id` is `NOT NULL` — every row attributes to a specific
  named source. `(run_id, source_node_id, source_row_index)` is unique;
  `(run_id, ingest_sequence)` is unique (global per-run monotone ordering).
- `rows.source_row_index` and `rows.ingest_sequence` are Tier-1 fields
  that the engine refuses to fabricate. Source plugins must supply both
  on every emitted row; the `create_row` boundary raises
  `AuditIntegrityError` when either is missing. See
  [Plugin Protocol — Source row identity](../contracts/plugin-protocol.md#source-row-identity--no-fabrication).
- `run_sources` is the per-source contract surface: `(run_id,
  source_node_id)` is the PK, `(run_id, source_name)` is unique, and
  `lifecycle_state` is constrained to the five `RunSourceLifecycleState`
  enum values. Resume reconstructs schema contracts by joining
  `rows.source_node_id` to `run_sources.schema_contract_json`.
- `token_work_items` lease state mutations are CAS-gated on
  `expected_lease_owner` (ADR-026). The `LEASED` status carries a
  non-empty `lease_owner` by check constraint; `recover_expired_leases`
  rotates `work_item_id` and `attempt` on lease expiry except for
  `PENDING_SINK` rows where both are preserved (sink work isn't replayed).
- `token_parents` and `validation_errors` use run-scoped foreign keys; neither
  lineage nor quarantine evidence can cross a run boundary.
- `nodes.output_contract_hash` stores the canonical output-contract identity
  used to detect incompatible graph evolution.
- Sink effects and coalesce effects finalize their result and controlling
  state transition atomically. A result cannot become visible without its
  durable receipt.
- `sidecar_journal_outbox` is written in the audit transaction and owned by one
  canonical journal destination. Recovery cannot move or acknowledge a batch
  through a different sidecar path.

## Write Surfaces

Engine and plugin code should reach Landscape through repository/adaptor
interfaces rather than raw SQL:

- Run lifecycle and export status: `RunLifecycleRepository`.
- Rows, tokens, token outcomes, and errors: `DataFlowRepository`.
- Node states, routing, calls, operations, batches, and artifacts:
  `ExecutionRepository`.
- Durable token work and lease recovery: `SchedulerRepository`.
- Sink-effect reservation, attempts, fencing, and finalization:
  `ExecutionRepository.sink_effects`.
- Plugin-facing audit writes: `PluginAuditWriterAdapter` in `plugin_audit_writer.py`,
  constructed by `RecorderFactory.plugin_audit_writer()`.

Direct SQL belongs in schema migrations, diagnostics, or read-only operator
investigation where a maintained read API is not enough.

## Read Surfaces

Preferred read paths:

- `elspeth explain --run <RUN_ID> --row <ROW_ID> --database <DB>` for operator
  lineage investigations.
- `QueryRepository` for in-process lineage queries.
- `LandscapeExporter` for complete export/reimport evidence.
- `elspeth-mcp` for read-only MCP analysis against a Landscape database.

The MCP Landscape server exposes read-only tools from `src/elspeth/mcp/server.py`,
including run listing, token explanation, operations, calls, collisions, schema
description, outcome analysis, sink-effect recovery history, performance
reports, diagnostics, and contract queries.

## Operator References

- [Investigate Routing](../runbooks/investigate-routing.md)
- [Database Maintenance](../runbooks/database-maintenance.md)
- [Backup and Recovery](../runbooks/backup-and-recovery.md)
- [Scheduler Lease Recovery](../runbooks/scheduler-lease-recovery.md)
- [Sink Effect Recovery](../runbooks/sink-effect-recovery.md)
- [Landscape MCP Analysis Server](../guides/landscape-mcp-analysis.md)
- [Token Outcome Assurance](../contracts/token-outcomes/README.md)
- [ADR-025: Multi-Source Ingestion](adr/025-multi-source-ingestion.md)
- [ADR-026: Durable Token Scheduler](adr/026-durable-token-scheduler.md)
