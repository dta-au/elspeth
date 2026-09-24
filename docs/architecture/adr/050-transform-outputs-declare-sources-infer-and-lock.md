# ADR-050: Transform Outputs Declare, Sources Infer-and-Lock — Join Only at Multi-Producer Seams

**Date:** 2026-09-25
**Status:** Accepted
**Deciders:** ELSPETH maintainer (operator rulings 2026-09-24 and 2026-09-25 on elspeth-5887fb7928 S1, after a seven-seat specialist panel)
**Review evidence:** `.claude/lanes/5887-batch-row/PANEL-S1-S3-synthesis.md` §S1 (six of seven panelists objected to widening the node record as rows arrive; the measured shapes are listed under Context)
**Tags:** schema-contract, transform, source, sink, coalesce, audit-integrity, tier-1, tier-2, declaration-contract
**Depends on:** [ADR-011](011-declared-output-fields-contract.md), [ADR-014](014-schema-config-mode-contract.md), [ADR-032](032-validate-by-trust-domain.md)
**Amends:** invariant 6 of [docs/contracts/execution-graph.md](../../contracts/execution-graph.md) ("schema contracts are frozen after first row"), which now applies to sources only

## Context

Every node's output contract is a Tier-1 record: `nodes.output_contract_json`
carries `locked: true`, its hash is part of the row's integrity check, and the
column comment called it "what the node guarantees". Two different mechanisms
wrote it:

- A **source** infers its contract from the first valid row, locks it, and
  validates every later row's *values* against the lock (`ContractBuilder`,
  `json_source`). A later row of another type is quarantined. That is the
  published invariant 6.
- A **transform** re-inferred the contract of every field it created from
  *that row's value* (`FieldContract.inferred` via `propagate_contract` /
  `narrow_contract_to_output` / `SchemaContract.with_field`), and the
  Landscape merged each emission into the node record with an exact-equality
  type merge (`merge_for_batch` → `merge_union_contracts`). Nothing checked
  the values.

Those two mechanisms disagree, and the disagreement was live. Measured on
`release/0.8.1` and reproduced on this branch before any change
(`.claude/lanes/5887-batch-row/logs/round5b/S1a-declare/before-*.log`):

| shape | what happened | exit |
|---|---|---|
| `value_transform` copying `row['meta']['copies']` over rows holding `2`, `"S"`, `3` (observed) | `ContractMergeError: field 'copies' has conflicting types 'int' and 'str'` at the node-contract merge, traceback, remaining tokens abandoned | 4 |
| the same with an int and a float, a null first, a null later; `json_explode` over `[1,2]`, `[3,"S"]`; over `[3,null]`; a `field_mapper` dotted extraction; a conditional overwrite | the same abort, six more spellings | 4 |
| `value_transform` with an **explicit** `copies: any` | the same abort — the declaration was dropped from the pin because it was `any` | 4 |
| `value_transform` declaring a carried field `score: int?` on a **valid** all-int row | Tier-1 `SchemaConfigModeViolation` — the carried field kept upstream's inferred metadata, which the ADR-014 exact-metadata check rejected | 4 |
| two **observed sources** typing `id` as `int` and `str`, one sink, no transform | `FrameworkBugError: Contract merge failed` at the sink batch merge | 4 |
| `json_explode` with `page: int` declared, one element a str | run **completed**, the node record said `page: int, declared`, and the str was **delivered to the sink** | 0 |

The last row is the worst: a Tier-1 record making a false claim about a
delivered row. `validate_output_against_contract` had no production caller,
and a field-adding transform's strict `output_schema` check is an observed
pydantic model that types nothing. The other rows are the same defect seen
from the other side: an inference that can only be checked by equality with
the previous inference, so the first row that differs ends the run — with
which row that is depending on arrival order and on scheduling under a worker
pool (the C4 principle: crash timing must not change the outcome).

The 2026-09-24 ruling tried "WIDEN": let the node record join conflicting
types to `object` as rows arrive. The panel measured that it rewrites a
`locked: true` Tier-1 record mid-run with no event, cannot tell a courier's
legitimate variance from a plugin computing the wrong type, and does not even
stop the abort — a node-only join moves it to the sink. It was withdrawn.

## Decision

**A transform DECLARES the contract of every field it creates, before the
first row, and the declaration is enforced on every emitted value. A source
still infers and locks. Contracts are JOINED only where several producers'
rows meet.**

1. **Precedence, fixed at construction:** the operator's `schema.fields` type
   for the created field, else the type the plugin declares in
   `created_output_fields()`, else `any`. `any` is nullable. The output
   config builders still add a required `any` placeholder for every
   guaranteed name they know only by name (`declare_missing_guaranteed_fields`,
   now `nullable=True`); that placeholder is not an operator declaration and
   yields to the plugin's type.
2. **One stamp.** `BaseTransform._apply_declared_output_field_contracts` is
   the single authority that rewrites an emitted contract to the declared
   metadata (`source="declared"`, the declared type, required-if-guaranteed,
   nullable). It runs in every schema mode, keeps the emitted field's
   `original_name` (a declaration types a field, it does not rename it), and
   never reads the row's value. The plugin-side pin value_transform keeps
   reads the same table, so the pin and the stamp cannot disagree.
3. **Two plugin hooks, both owned types:** `created_output_fields() ->
   tuple[FieldDefinition, ...]` (created fields with the type the plugin's
   code fixes, or `any`) and `carried_output_fields() -> frozenset[str]`
   (declared names whose value is copied from an input field under that
   field's contract — a field_mapper flat rename — which the stamp leaves
   alone). A plugin whose created NAMES are data (blob_csv_expand's CSV
   headers) passes them to the stamp per emission with the type its code
   fixes (`str`); the field set may grow row to row, the types cannot.
4. **Completeness, Tier 1.** `OutputDeclarationCompletenessContract`
   (`output_declaration_completeness`, post-emission site) requires every
   emitted key absent from the input row and not carried to carry a
   `source="declared"` contract. A created field that bypassed the stamp is
   owned-code drift, not a row fault: the token's terminal is recorded and
   `UndeclaredOutputFieldsViolation` ends the run. The batch-flush site is
   deliberately not claimed (see Consequences).
5. **Values, Tier 2.** After the dispatched contracts, `TransformExecutor`
   validates every declared concrete-typed field whose value the transform
   PRODUCED — a field absent from the input row, or an input field whose
   value it rewrote — with `validate_output_against_contract`. An input
   value passed through unchanged is not re-adjudicated: the strict input
   check admitted it under pydantic's rules (which accept an `int` or a
   `Decimal` for `float`, where `SchemaContract.validate` compares exact
   types), and a resumed row legitimately carries a type-faithful `Decimal`
   under its `float` declaration. A violation raises
   `DeclaredOutputTypeViolation(PluginContractViolation)`, routed through
   `on_error` like every Tier-2 violation, with a reason that carries the
   field, both type names, the emitted index and an **authorship bit** —
   `computed` when the transform created the field, `carried` when it
   rewrote an input field — and never the value. A multi-row emission fails
   its parent token once. A plugin that returns its own error first
   (value_transform's `type_mismatch`) emits nothing, so nothing is routed
   twice. Per the 2026-09-25 ruling a plugin's own computed value breaking
   its own declared type is ROUTED, recorded as evidence with
   `authorship: computed`; the bit is what lets that tighten later without
   rework.
6. **The node record never changes type.** `DataFlowRepository.update_node_output_contract`
   folds an emission with `SchemaContract.merge_for_node_evolution` (field
   union, `require_all=False` flags, a type conflict raises). Because every
   emission of a node carries the same declared types, a conflict there is a
   bug in owned code and is re-raised as `FrameworkBugError`, value-free.
   Sources share the writer and keep raising: invariant 6 for sources is
   unchanged.
7. **J1 at the two multi-producer seams only.** `SchemaContract.merge_for_batch`
   is now the description join `join_batch_contracts`: equal types keep
   their type, different types become `object` (`int ⊔ float` and `bool ⊔
   int` included — `validate()` compares exact types), `object` absorbs,
   nullable is OR, required is AND, a field some member lacks is optional and
   nullable, `original_name` falls back to the identity when carriers
   disagree, mode is the most restrictive, locked is OR. It is a lattice
   join — commutative, associative, idempotent on `version_hash`, and sound
   (every contributing row validates) — pinned by property tests. Its only
   callers are `SinkExecutor._merge_batch_contract` and `display_headers`.
   There is no `type_conflict` parameter anywhere: the two raising merges
   and the one join are three named entry points.
8. **A coalesce keeps raising**, and `any` is not a wildcard there: an `any`
   branch beside an `int` branch is a `ContractMergeError` (routed as
   `contract_type_conflict` at runtime, `coalesce_union_type_incompatible`
   at build time), exactly as the build-time check already treated `any`
   against `int`. A union coalesce promises one type to its consumers and
   `any` promises nothing; the operator's lever is to declare the type on
   the transform's schema. Pinned in `tests/unit/contracts/test_union_merge.py`.
9. **`ContractMergeError` is seam-neutral.** It carries a field and two type
   names; the caller assigns the tier (a coalesce routes the row, the node
   writer re-raises Tier 1, the description join never raises it).
10. **Declaration location is the runtime stamp.** The planner-visible
    `_output_schema_config` projection is unchanged; promoting plugin types
    into the flexible output schema is a follow-up behind the
    composer/runtime agreement test.

### What this is NOT

- Not coercion. A value is written as computed; the declaration is checked
  against it, never applied to it.
- Not a source change. `ContractBuilder` and the source seam are untouched.
- Not an aggregation change. Batch and collector outputs record no node
  output contract today and do not carry the stamp.

## Consequences

### Positive

- The eleven measured shapes map: the nine observed-mode variance shapes and
  the explicit-`any` shape complete with exit 0 and a node record that is
  byte-identical after row 1 and after row N; the declared `page: int` shape
  routes the parent token with a value-free reason; the typed-consumer shape
  passes the valid rows and routes the wrong-typed one; the two-source sink
  completes. All run end to end in
  `tests/integration/pipeline/test_output_declaration_routing.py`.
- The recorded contract is order- and scheduling-independent (two arrival
  orders and a four-worker pool record the same bytes).
- A declared type is now a promise the engine keeps, at the node that made it.
- The plugin-drift masking the panel named is closable: a plugin that
  declares a concrete type gets it enforced. Until the 22-transform sweep
  (unit S1b) lands, a plugin-computed field declared `any` is carried, not
  caught — the named residual.

### Negative

- `version_hash` moves for every observed-mode field-adding transform
  (`source: declared`, `any` nullable), and the LLM prompt `contract_hash`
  with it. Prior-run comparisons across the change differ.
- A pre-change run at the same Landscape epoch that is resumed after the
  change meets `T inferred` in its node record and `object declared` in the
  resumed emission: `merge_for_node_evolution` raises and the resume ends
  with `FrameworkBugError` at the first stamped node's evolution. There is
  no refusal and no compatibility shim by design (the pre-1.0 "no old rows"
  posture): `implementation_compatibility` compares per-node
  `plugin_version` / `determinism` / `source_file_hash`, none of which a
  base-class change moves. Landscape epoch 45 is undeployed, so no deployed
  database holds such a run.
- The stamp keeps the emitted `original_name` where it used to overwrite it
  with the normalized name; a renamed declared field's recorded lineage
  changes accordingly (deliberate, measured: no corpus projection moved).
- Sinks now receive mixed-type columns where they used to receive an abort.
  Enumerated: `json`, `text`, `document`, `azure_blob` (jsonl/json) and
  `aws_s3` serialise any JSON value; `csv` and the blob sinks' csv format
  write `str()` of each cell; `database` types its columns from the sink's
  OWN declared schema (`SCHEMA_TYPE_TO_SQLALCHEMY`, `any` → `Text`), so a
  mixed column reaches an `any`/`Text` column as text and a typed column as
  a database write error that the sink's write-failure path owns;
  `dataverse` and `chroma` write to schemas their own config fixes. JSON and
  CSV are tested end to end; none crashes unrouted.

### Neutral

- `nodes.output_contract_json` now means "the node's declared output
  contract (types fixed; the field set evolves)" for transform nodes; the
  column comment and `execution-graph.md` invariant 6 say so.
- `docs/guides/data-trust-and-error-handling.md` §Implications: "plugin
  returns wrong type" is ROUTE-as-PCV (with the authorship bit), and "plugin
  emits a field it never declared" is CRASH.
- `EXPECTED_CONTRACT_SITES` gains `output_declaration_completeness`.

## Alternatives Considered

### WIDEN (the 2026-09-24 ruling)

Join conflicting types to `object` in the node record as rows arrive; None +
T → T nullable. **Withdrawn 2026-09-25.** It mutates a `locked` Tier-1 record
mid-run with no event, discards a computed conflict, masks plugin-computed
drift (the merge compares contracts, never provenance), relocates the abort
to the sink unless the sink joins too, and the null rule is non-associative
and unsound on the representation it inherits (`[1, "x", None, 2]` → `int?`,
which rejects `"x"`).

### LOCK on first emission (mirror the source seam)

Validate each emission's values against the type the first emission
recorded. Rejected unanimously: it fabricates a promise from one row, is
order- and scheduling-dependent, and lets an injected first row choose the
lock.

### Catch `ContractMergeError` at the transform and route the row

Rejected: it false-faults null-bearing rows the source seam accepts and
routes a fact about the RECORD as a fact about the ROW.

### End the run when a plugin's own computed value breaks its own declared type

The panel's TD/TS/SA position. Deferred by ruling: routed as a PCV with the
`authorship` bit recorded, so the tightening can be decided on evidence.

## Tests and gates

- `tests/integration/pipeline/test_output_declaration_routing.py` (T1–T6,
  T8, T10–T12), `tests/unit/engine/test_output_declaration_enforcement.py`
  (D5/D6 through the Orchestrator), `tests/unit/plugins/infrastructure/test_output_declaration_stamp.py`
  (precedence, nullable, lineage, carried, dynamic, plugin hooks),
  `tests/invariants/test_output_declaration_completeness.py` (the roster
  gate with controls), `tests/property/contracts/test_schema_contract_properties.py::TestJ1LatticeLaws`,
  `tests/unit/core/landscape/test_graph_recording.py` (the node writer
  raises `FrameworkBugError`), `tests/testcontainer/core/test_output_contract_concurrency_postgres.py`
  (the PostgreSQL twin under the row lock).
- Plugin `source_file_hash` re-pins, the scenario-corpus registry digest,
  the soft-mapping census and the trust-tier lint corpus delta ride the
  landing commit.
