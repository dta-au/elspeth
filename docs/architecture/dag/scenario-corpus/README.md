# Maintained DAG Scenario Corpus

This directory holds the evergreen, executable inventory used to answer a
specific question: which parts of Elspeth's mandatory directed acyclic graph
(DAG) lifecycle have current production-path evidence?

Start with the [v1 corpus manifest](v1/manifest.yaml). It contains all 15 mandatory
scenarios, all 11 assessment dimensions, the evidence registry, owned gaps,
observable exit gates, and the cases that the production-path harness runs.
The `v1/` directory names the corpus revision; the manifest's serialized
`schema_version` is independently versioned and is currently `2`. Schema v2
preserves each durable token parent as an explicit ordinal/key pair.
The [DAG information hub](../README.md) supplies the broader completeness
assessment and remediation context.

## Authority boundary

These files have distinct jobs:

- The [completeness criteria](../completeness-criteria.md) define the quality
  bar and the mandatory scenario set. Change them only when the intended bar
  changes.
- The [v1 manifest](v1/manifest.yaml) is the authoritative live inventory of
  scenario cells, declared evidence, ownership, exit gates, and executable
  case declarations.
- The [typed schema](../../../../tests/fixtures/dag_scenario_corpus/schema.py)
  defines the closed manifest and observed-evidence shapes. It also derives
  `complete` only when every cell is `pass` or `not_applicable`.
- The [strict loader](../../../../tests/fixtures/dag_scenario_corpus/loader.py)
  binds the manifest to the exact scenario and dimension inventory, rejects
  duplicate or orphaned declarations, validates fixtures, and checks evidence
  locators.
- The [production-path harness](../../../../tests/fixtures/dag_scenario_corpus/harness.py)
  executes registered cases and returns one common `ScenarioRunEvidence`
  record for configuration, build, runtime, audit, and recovery facts.

Harness workflows are deliberately typed by the boundary they cross. `build`
loads the real YAML, instantiates plugins in preflight mode, constructs and
validates the production execution graph, and assembles the pipeline config.
`BuildExpectation` declares exact node and edge counts, typed node-type counts,
and sorted edge labels with duplicates preserved. The observed `GraphEvidence`
records that exact graph shape plus a separately computed topology hash.
It does not create an audit database or an orchestrator, and its runtime,
audit, and recovery evidence remains explicitly unattempted. A `build` case is
therefore executable evidence only for `config` and `build`; it cannot support
runtime, audit, or recovery cells. A `run` or `recovery` case may use the
exact-audit `RunExpectation` or the narrower `SummaryRunExpectation`, which
pins only overall status, output count, and required audit record types. A
summary expectation cannot by itself establish exact runtime, audit, or
recovery completeness. A `run` case may instead use
`SemanticRunExpectation` when scheduler ordering prevents a stable raw
identity oracle. That expectation pins exact outputs, counters, record counts,
and an order-insensitive runtime projection, but deliberately excludes raw
audit identity. Its harness evidence is therefore limited to exactly
`[config, build, runtime]` and cannot support an audit or recovery pass.

Exact projections preserve stateful runtime records as typed evidence rather
than collapsing them into record counts. Aggregation cases include immutable
batch membership and a separate `intermediate_outcomes` collection for
non-terminal `BUFFERED` history; `terminal_dispositions` remains exactly one
terminal record per token. Expansion cases include stable parent identity,
expected child count, dense child ordinals, and durable parent links. Source
validation and transform failures retain their typed error records, while
sink-effect audit material pins publication and inspect/reconcile/commit
attempts. This lets the B3 cases prove exact runtime and portable-audit parity
without broadening terminal semantics or inferring omitted material.

The current B3 set is deliberately bounded: EOF immutable aggregation, JSON
parent/child expansion, retry success, source quarantine, transform discard,
transform error routing, and one ordinary write-once sink. The ordinary cases
provide runtime/audit evidence. Three dedicated fresh-object cases also
provide recovery evidence at the EOF aggregation, expansion child-handoff,
and pending-sink redrive seams. The pending-sink case deliberately spans three
fresh runtime/object lifetimes within one process: the initial run durably
reaches `PENDING_SINK` after source exhaustion, the first public resume claims
that exact work item and faults before sink-effect reservation, and the second
public resume uses an injected clock to expire and recover that lease before
publishing. These cases do not promote concurrency or scale, whose gaps remain
independently owned in the manifest.
The disposition scenario also keeps runtime and audit partial until the
authoritative scheduler-disposition and follower-drain work tracked by
`elspeth-2e66723070` and `elspeth-6f6bbbec00` is integrated; the local exact
cases remain evidence without substituting for those authorities.

Every `recovery` case also declares a closed `recovery_kind`, so the shared
harness cannot silently apply one topology's restart assertions to another.
The `parallel_sink_finalization` evidence records exact before/after sink
effect, artifact, and attempt identities while fixing `held_barrier_proven` to
false: terminal-arm asymmetry is useful partial evidence, not proof that one
parallel coalesce barrier remained held. The `eof_aggregation` case injects one
fail-once fault before an EOF transform result, then proves that public resume
retains the original failed batch, creates one distinct completed retry batch,
and reuses the same ordered three-member token set. The
`expansion_child_enqueue` case faults after the source is durably exhausted and
all children are enqueued but before sink flush, then proves exact 3-parent,
6-child, 3-group, and 9-work-item identity across public resume. That case uses
the supported observed-schema JSON source contract; fixed-schema `any` resume
reconstruction remains separately owned by P1 `elspeth-0b0eaa63df`. Both cases
also require terminal token/work state, checkpoint removal, no source replay,
canonical outputs, and exact durable/export parity. The
`pending_sink_redrive` case proves the same work, token, row, payload, sink,
outcome, path, error, and scheduler-attempt bundle survives expiry. It requires
one exact sink-specific `RECOVER_EXPIRED_LEASE` transition that clears the old
owner before a fresh claim, no effect or artifact before recovery, and exactly
one final sink effect, member, artifact, and publication with the expected
three public sink-effect attempts. It also proves terminal state, checkpoint
cleanup, no source replay, and durable/export parity. All three recovery
proofs remain provisional until they are rerun after the deferred-platform
rebase.

The manifest does not replace the criteria, and a dated assessment does not
replace the live manifest. Documentary evidence can explain a cell, but only
executable `harness` or `pytest` evidence can support `pass`.

## Status vocabulary

The manifest accepts exactly these lower-case statuses:

| Status | Meaning | Required shape |
| --- | --- | --- |
| `pass` | Current executable evidence proves the complete requirement for this cell. | One or more evidence IDs, including at least one `harness` or `pytest` reference; no reason, owner, or exit gate. |
| `partial` | Current evidence proves part, but not all, of the requirement. | A precise reason, Filigree owner issue, and observable exit gate. Evidence may be attached. |
| `fail` | Current evidence demonstrates behavior that misses the requirement. | A precise reason, Filigree owner issue, and observable exit gate. Evidence may be attached. |
| `unknown` | Adequate current production-path evidence has not been executed or does not exist. | A precise reason, Filigree owner issue, and observable exit gate. Evidence may be attached. |
| `not_applicable` | The dimension genuinely does not apply to this scenario. | A narrow applicability reason; no evidence, owner, or exit gate. |

`unknown` is a result, not a skipped test and not permission to infer success.
Keep the cell visible and owned until executable evidence proves a different
status. Registered harness cases must run normally: do not hide a coverage gap
with `skip`, `xfail`, or a plan-only reference.

## Register executable evidence

Use one of the two executable evidence kinds.

For a corpus harness case:

1. Add deterministic inputs and canonical YAML below
   `tests/fixtures/dag_scenario_corpus/v1/<scenario-id>/`.
2. Add a case beneath that scenario's `cases` list. Its locator is
   `<scenario-id>:<case-id>`.
3. Select the narrowest honest workflow: `build` with an exact
   `BuildExpectation`; `run` with an exact-audit `RunExpectation`, a
   runtime-only `SemanticRunExpectation`, or a `SummaryRunExpectation`; or
   `recovery` with an exact-audit `RunExpectation` or
   `SummaryRunExpectation`. The schema rejects build expectations on later
   workflows and semantic-runtime expectations on recovery.
4. Add one top-level evidence record with `kind: harness`, the same locator,
   a precise claim, and only the stages it proves. Build-only evidence must use
   exactly `[config, build]`; semantic-runtime evidence must use exactly
   `[config, build, runtime]`.
5. Reference that evidence ID only from cells its assertions actually prove.
6. Extend the table-driven assertions in the
   [production-path integration test](../../../../tests/integration/core/dag/test_dag_scenario_production_path.py)
   when the common expectation schema is not sufficient.

For an existing executable test, add a top-level record with `kind: pytest`
and a repository-relative pytest node locator such as
`tests/path/test_file.py::test_name`. The loader validates that the file and
node exist, and the contract suite batch-collects every declared pytest
locator.

Use `document` and `decision` references only as supporting context. They
cannot make a cell pass by themselves.

## Promote a cell

Promote evidence and status in the same commit:

1. Add or strengthen the executable assertion and observe it fail for the
   missing behavior or proof.
2. Make the production path and assertion pass.
3. Register the exact evidence locator in the manifest.
4. Attach the evidence ID to every cell it directly proves.
5. Change a cell to `pass` only when that evidence covers the whole cell, then
   remove its `reason`, `owner_issue`, and `exit_gate` fields.
6. Run the focused contract and integration suites before committing.

If evidence closes only part of the gap, keep `partial` and rewrite its reason
and exit gate to state exactly what remains. Do not promote a nearby cell by
analogy.

## Run the focused checks

From the repository root, validate the manifest, schema, locators, fixtures,
documentation links, and evidence contracts:

```bash
.venv/bin/pytest -q \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py
```

Run every registered production-path harness case:

```bash
.venv/bin/pytest -q \
  tests/integration/core/dag/test_dag_scenario_production_path.py
```

The [unit contract suite](../../../../tests/unit/architecture/test_dag_scenario_corpus_contract.py)
must reject malformed inventory or evidence. The integration suite must run
registered cases without skips or expected failures and must assert the
observed evidence, not merely successful process exit.

## Dated assessments

The live corpus evolves; [dated assessments](../assessments/) remain immutable
records of one commit. A new assessment should cite:

- `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`;
- the manifest `schema_version`;
- the assessed Git commit; and
- the exact corpus commands and observed results.

Do not rewrite an older assessment when the manifest changes. Add a new dated
assessment, or add an explicit erratum when the older record itself is wrong.
The [assessment framework](../assessment-framework.md) defines the complete
snapshot workflow.

## Active Filigree work

The foundation and remaining corpus coverage are tracked by
Filigree issue `elspeth-ef29ef6ba4`. Inspect its live state rather than copying
a status into this evergreen page:

```bash
filigree show elspeth-ef29ef6ba4 --json
```

Keep the issue open while applicable cells still rely on incomplete evidence
owned by it. Close it only when its full acceptance scope—not merely the
manifest and harness foundation—is satisfied.
