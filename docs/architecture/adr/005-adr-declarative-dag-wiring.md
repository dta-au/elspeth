# ADR: Declarative DAG Wiring — Explicit Input/Output Connections on All Nodes

**Status:** Accepted
**Decision history:** commits `00d3c6bae` and `074e15e87`
**Date:** 2026-02-09
**Decision Makers:** ELSPETH maintainer
**Depends On:** ADR-004 explicit sink routing and the completed processor refactoring
**Review evidence:** Three-lens advisory design review; conditions are incorporated below
**Design Decisions (2026-02-10):** `on_success` lifted to settings level, `settings.name` drives node IDs, and aggregations stay post-transform

## Context

ADR-004 makes output routing explicit — every terminal node declares where its output goes. But input routing remains implicit: transforms receive data based on YAML list position. The DAG builder infers edges from ordering.

This creates two description models coexisting in one YAML:

- **Outputs**: Explicit (`on_success: sink_name`, gate `routes:`)
- **Inputs**: Implicit (position in list, fork branch association via gate `fork_to:`)

For simple linear pipelines, implicit inputs are fine. For complex topologies (forks, diamonds, multi-path convergence), the implicit wiring becomes opaque — you have to mentally walk the ordered list and fork annotations to understand the dataflow.

### Architectural Pattern: Positional Encoding

The current model encodes graph topology in list ordering — a form of *positional encoding* where information is carried by arrangement rather than declaration. This creates invisible invariants: the meaning of `transforms[2]` depends on the existence and position of `transforms[0]` and `transforms[1]`. Insert a new node at position 1 and you've implicitly rewired everything downstream without touching any explicit configuration.

This pattern is a known fragility in ordered-list-as-graph representations.
Established data and infrastructure tools use named dependencies to make graph
topology explicit. dbt's `ref()` model is the closest analogue: every model
declares its upstream dependencies, creating a declarative DAG in which
explicitness supports lineage.

### Current Model (Post on_success ADR)

```yaml
source:
  plugin: csv
  options:
    path: input.csv
    on_success: output

transforms:
  - plugin: enricher           # implicitly receives from source (position 0)
  - plugin: validator          # implicitly receives from enricher (position 1)
    on_success: output

sinks:
  output: { plugin: csv }
```

Edges are inferred: source → enricher (position), enricher → validator (position), validator → output (explicit `on_success`). Two implicit edges, one explicit.

### Proposed Model (Fully Declarative)

```yaml
source:
  plugin: csv
  options:
    path: input.csv
  on_success: enrichment        # named output connection

transforms:
  - plugin: enricher
    name: enricher
    input: enrichment           # explicit: receives from source
    on_success: validation      # named output connection

  - plugin: validator
    name: validator
    input: validation           # explicit: receives from enricher
    on_success: output          # routes to sink

sinks:
  output: { plugin: csv }
```

Every edge is explicit. The YAML IS the graph.

## Decision

### Add `input: str` to Transform, Gate, Aggregation, and Coalesce Settings

Every processing node declares where it receives data:

```python
class TransformSettings(BaseModel):
    name: str                     # NEW: user-facing wiring label, drives node IDs
    input: str                    # NEW: named input connection
    on_success: str | None = None # LIFTED from options to settings level
    plugin: str                   # existing
    options: dict | None = None   # existing (on_success removed from here)
```

### Named Connections Replace Positional Ordering

Connections are named strings that link one node's output to another node's input. They are **edge labels** in the DAG.

```yaml
source:
  on_success: raw_data           # source outputs to "raw_data"

transforms:
  - plugin: enricher
    input: raw_data              # enricher reads from "raw_data"
    on_success: enriched         # enricher outputs to "enriched"

  - plugin: classifier
    input: enriched              # classifier reads from "enriched"
    on_success: results_sink     # classifier outputs to sink
```

The DAG builder matches `on_success` values to `input` values to create MOVE edges. Validation rules:

- A connection name in `on_success` that appears in no `input` and is not a sink name → `GraphValidationError` (dangling output)
- A connection name in `input` that appears in no `on_success` and is not a source output → `GraphValidationError` (missing input)
- A connection name that collides with a sink name → `GraphValidationError` (namespace collision — see Q1 resolution below)
- Two transforms with the same `input` value → `GraphValidationError` (ambiguous fan-out — use a gate for explicit splitting)
- Error messages should include Levenshtein-distance suggestions for near-miss typos

### Connection and Sink Namespaces Are Separate

Connection names (internal wiring between nodes) and sink names (terminal destinations) occupy **separate namespaces**. A connection name that matches a sink name is a validation error.

- `on_success: enriched` where `enriched` is an `input:` on another transform → connection (MOVE edge to next node)
- `on_success: output_sink` where `output_sink` is a configured sink → terminal routing (MOVE edge to sink)
- `on_success: foo` where `foo` is BOTH a connection and a sink → `GraphValidationError` ("Connection name 'foo' collides with sink name 'foo'")

This prevents action-at-a-distance bugs where adding a new sink silently reroutes an existing connection.

### Gate Routes Become Connection Names

Gate `routes:` values can be connection names (feeding downstream transforms) or sink names (terminal routing):

```yaml
gates:
  - name: splitter
    input: enriched
    condition: "row['score'] > 0.8"
    routes:
      "true": premium_path       # connection to downstream transform
      "false": standard_path     # connection to downstream transform

transforms:
  - plugin: premium_enricher
    input: premium_path          # fed by gate's "true" route
    on_success: premium_sink

  - plugin: standard_enricher
    input: standard_path         # fed by gate's "false" route
    on_success: standard_sink
```

Gate route values that are connection names feed downstream transforms. Gate route values that are sink names are terminal routing decisions (existing behavior).

### Fork/Join Becomes Natural

Fork is expressed when a gate's routes feed multiple downstream transforms. Join (coalesce) is explicit when a coalesce node declares multiple inputs:

```yaml
gates:
  - name: forker
    input: enriched
    condition: "True"
    routes:
      "all": fork
    fork_to: [analysis_a, analysis_b]

transforms:
  - plugin: analyzer_a
    input: analysis_a
    on_success: merge_input_a

  - plugin: analyzer_b
    input: analysis_b
    on_success: merge_input_b

coalesce:
  - name: merger
    branches:                                   # dict format (ARCH-15): branch → input connection
      analysis_a: merge_input_a                 # branch routes through transform chain
      analysis_b: merge_input_b
    on_success: final_sink
    # List format also works for identity branches: branches: [branch_a, branch_b]
```

### Source-Only Pipeline

```yaml
source:
  plugin: csv
  options:
    path: input.csv
  on_success: output             # direct connection to sink

sinks:
  output: { plugin: csv }
```

No transforms, no `input:` fields needed. Source → sink via named connection.

## Prerequisites

### Prerequisite 1: ADR-004 `on_success`

The on_success ADR must be implemented first. It establishes the `on_success` field on protocols and config, removes `default_sink`, and creates the explicit output routing foundation.

### Prerequisite 2: Processor Refactoring (completed)

**COMPLETED (2026-02-10).** The processor now uses `node_id`-based traversal:

- `_WorkItem.current_node_id: NodeID | None` replaces the former `start_step: int`
- Processor follows DAG edges via `ExecutionGraph.get_next_node()` instead of list indexing
- `start_step` has zero references in processor.py
- All Phase 2 children resolved: hscm.1 (terminal deagg sink routing), hscm.2 (coalesce traversal invariant)

YAML order still matches topological order by convention, but the processor no longer depends on it.

## Impact Analysis

### What Changes

| Area | Change | Scope |
|------|--------|-------|
| `core/config.py` | Add `name:` + `input:` to `TransformSettings`; add `input:` to `GateSettings`, `AggregationSettings` | ~30 lines |
| `core/config.py` + `plugins/config_base.py` | Lift `on_success` from `TransformDataConfig`/`SourceDataConfig` (options) to settings level | ~50 lines (model changes) + broad YAML/test updates |
| `core/dag.py` | Replace positional edge inference with connection-name matching in `from_plugin_instances()` | ~200 lines (rewrite of edge creation) |
| `core/dag.py` | Add connection validation (dangling outputs, missing inputs, namespace collisions, near-miss suggestions) | ~80 lines |
| `core/dag.py` | Change node ID derivation to use `settings.name` instead of `plugin.name + sequence` | ~20 lines |
| `engine/processor.py` | Already refactored to use `node_id` (Phase 2 — CLOSED) | 0 (done) |
| `plugins/protocols.py` | No change — `input` is a config/wiring concern, not a plugin concern | 0 |
| All example YAMLs | Add `input:` to every transform/gate | All examples |
| All tests | Update pipeline construction | Broad (~800+ refs) |

### What Doesn't Change

- **Executors**: Return results. Don't know about wiring.
- **Orchestrator**: Routes tokens through DAG edges. No change (once processor refactored).
- **Landscape**: Records node_states and routing_events. No change.
- **Plugin protocols**: Plugins don't know about wiring — they receive rows and return results.

### Blast Radius (Quantified)

| Metric | Count | Notes |
|--------|-------|-------|
| `from_plugin_instances()` calls | 191 across 45 files | Every call gains `input:` on all transforms |
| `fork_to` refs | 201 across 31 files | Verify `fork_to` ↔ `input:` wiring |
| Transform list construction | 233 across 46 files | Every transform list needs `input:` added |
| Example YAMLs | 28 settings files | Add `input:` + `on_success:` to every node |
| **Estimated total** | **~900+ refs** | Larger than on_success ADR (~472 refs) |

**Critical leverage points:**

- `tests/fixtures/pipeline.py` — single highest-leverage file (cascades to 20+ test files)
- `tests/unit/core/test_dag.py` — most affected test file (37 `from_plugin_instances`, 22 `fork_to`)
- `tests/fixtures/factories.py` — factory infrastructure

## Risks

### R1: Configuration verbosity (MEDIUM)

A 3-transform linear pipeline goes from 0 wiring lines to 6 (3 `input:` + 3 `on_success:`). Mitigated by scaffold tooling (`elspeth scaffold`). Verbosity is the feature for an auditability framework — the YAML is the auditable artifact.

### R2: Namespace confusion (LOW if separate namespaces enforced)

Connection names colliding with sink names was identified as the primary risk loop. The decision uses separate namespaces with collision-as-error validation.

### R3: DAG construction rewrite (HIGH)

`from_plugin_instances()` is ~500 lines and the single most critical function for pipeline correctness. Rewriting its edge-creation logic is high-risk. Mitigated by: implementing AFTER on_success ADR (which simplifies the function), comprehensive property tests, and the prerequisite processor refactoring (which isolates the change to DAG construction).

### R4: Blast radius (HIGH)

~800+ references across tests and examples. Mechanical but extensive. Mitigated by updating `tests/fixtures/pipeline.py` first (single leverage point) and using migration scripts for example YAMLs.

## Decision Clarifications

| Question | Decision |
|----------|----------|
| Q1: Connection namespace | Separate from sink namespace; collision is `GraphValidationError` |
| Q2: Multiple `input:` on transforms | Single input only; multiple inputs reserved for coalesce |
| Q3: Backward compatibility | Atomic change, no phased rollout |
| Q4: Does `path:` survive | Moot — no `path:` field exists on `TransformSettings`; fork branch association is via `fork_to:` on gates, which maps naturally to connection names |
| Q5: YAML ordering | Enforce topological order as transitional constraint until processor refactored to follow DAG edges |
| Q6: Verbosity mitigation | Accept verbosity now; `chain:` syntactic sugar deferred to post-release user feedback |
| Processor refactoring | Must be completed before declarative wiring; `start_step` → `node_id` is a separate work item |
| Implementation sequencing | ADR-004 → processor refactoring → declarative wiring |

## Implementation Sequencing

```
Phase 1: ADR-004 on_success                     ← COMPLETE
    ↓
Phase 2: Processor refactoring                  ← COMPLETE
    (start_step → node_id, completed)
    ↓
Phase 3: Declarative DAG wiring (this ADR)       ← READY (all prerequisites met)
    (from_plugin_instances() rewrite, ~900+ test refs)
```

Phases 1 and 2 are complete. Phase 3 implementation proceeds in waves: config models → core rewrite → propagation → tests → verification.

### Design Decisions (resolved 2026-02-10)

| Topic | Decision | Rationale |
|------|----------|-----------|
| `on_success` location | Lift `on_success` from `options:` (plugin config) to settings level for all node types | Aligns with CoalesceSettings (already at settings level). All wiring fields (`name`, `input`, `on_success`) at same YAML level. High blast radius accepted. |
| Node identity | `settings.name` drives node IDs, appears in audit, must be unique | Matches gates/aggregations. Eliminates sequence-dependent node IDs. Position-independent checkpoint resume. |
| Aggregation ordering | Aggregations stay post-transform; `input:` makes wiring explicit within that constraint | Mid-chain aggregations require a future decision. Current model supports accumulate-then-transform via natural chain order. |

### Runtime Semantics (Phase 3 additions)

Phase 3 extends DAG construction AND runtime route resolution:

1. **Route-to-node targets**: Gate routes can target downstream processing nodes by connection name (not only sinks). The route resolution contract becomes `(gate_node_id, route_label) → RouteTarget` where RouteTarget is one of: `sink:<sink_name>`, `node:<node_id>`, or `fork:<branches>`.

2. **Graph traversal API**: `ExecutionGraph` gains explicit route-target lookup APIs for gate labels. `get_next_node()` remains for structural "continue" traversal but is not the only continuation source for routed branches.

3. **Node ID derivation**: Changes from `{prefix}_{plugin_name}_{config_hash}_{sequence}` to `{prefix}_{settings_name}_{config_hash}` — position-independent, human-readable in audit records.

## References

- Prerequisite ADR: `docs/architecture/adr/004-adr-explicit-sink-routing.md` (on_success — Phase 1 CLOSED)
- Prerequisite refactoring: processor `node_id` traversal (completed)
- DAG construction: `core/dag.py:from_plugin_instances()` (lines 561-1110, ~550 lines)
- Processor traversal: `engine/processor.py` (`current_node_id`, `_WorkItem`) — node_id based
- Gate routing: `core/config.py:GateSettings.routes`
- Fork/join: `core/dag.py` fork edge creation
- Node ID derivation: `core/dag.py:node_id()` (lines 601-629)
- Config models: `core/config.py:TransformSettings` (line 552), `GateSettings` (line 292), `AggregationSettings` (line 238)
- Plugin config: `plugins/config_base.py:TransformDataConfig.on_success` (line 359), `SourceDataConfig.on_success` (line 153)
- Design decisions: `on_success` level, name identity, and aggregation ordering
- Analogous systems: dbt `ref()`, Apache Beam PCollections, Airflow `>>` operator
