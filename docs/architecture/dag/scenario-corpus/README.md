# Maintained DAG Scenario Corpus

This directory contains the live, executable inventory used to answer one
question: which parts of ELSPETH's mandatory DAG lifecycle have current
production-path evidence?

Start with the [v1 corpus manifest](v1/manifest.yaml). It is schema version `2`
and currently contains 15 scenarios, 11 dimensions, 114 evidence records, and
a derived verdict of `not_complete`. The [DAG information hub](../README.md)
explains how this evidence fits the broader product-quality criteria.

## Authority boundary

These sources have distinct jobs:

- The [completeness criteria](../completeness-criteria.md) define the quality
  bar and mandatory scenarios.
- The [v1 manifest](v1/manifest.yaml) owns the live scenario inventory,
  evidence and verdict inputs, gap metadata, exit gates, and executable cases.
- The [typed schema](../../../../tests/fixtures/dag_scenario_corpus/schema.py)
  defines the closed manifest and observed-evidence shapes and derives
  `complete` only when every cell is `pass` or `not_applicable`.
- The [strict loader](../../../../tests/fixtures/dag_scenario_corpus/loader.py)
  rejects duplicate, missing, orphaned, or invalid declarations and validates
  fixtures and evidence locators.
- The [production-path harness](../../../../tests/fixtures/dag_scenario_corpus/harness.py)
  executes registered cases and returns common configuration, build, runtime,
  audit, and recovery evidence.
- The [unit contract test](../../../../tests/unit/architecture/test_dag_scenario_corpus_contract.py)
  pins the exact scenario, dimension, case, and evidence registries and checks
  the links from this live documentation.

Filigree does not replace the manifest. It owns delivery status, dependencies,
and work ownership. Conversely, the manifest's `owner_issue` values connect
evidence gaps to work but do not replace live tracker state.

## Product criteria and lifecycle cells

The assessment uses two related but non-interchangeable views. The framework
defines **15 product-quality criteria** for the overall DAG capability. The
manifest evaluates **11 executable lifecycle cells** for each mandatory
scenario: `config`, `build`, `contracts`, `runtime`, `audit`, `recovery`,
`concurrency`, `freeform`, `guided`, `round_trip`, and `scale`.

The criteria judge whether the product is supportable as a whole. The cells
show exactly where executable scenario evidence exists or remains incomplete.

## Status vocabulary

| Status | Meaning | Required shape |
| --- | --- | --- |
| `pass` | Current executable evidence proves the complete cell. | At least one applicable `harness` or `pytest` evidence reference; no gap metadata. |
| `partial` | Evidence proves part, but not all, of the cell. | Precise reason, Filigree owner issue, and observable exit gate. |
| `fail` | Evidence demonstrates behavior that misses the requirement. | Precise reason, Filigree owner issue, and observable exit gate. |
| `unknown` | Adequate current production-path evidence has not been executed or does not exist. | Precise reason, Filigree owner issue, and observable exit gate. |
| `not_applicable` | The dimension genuinely does not apply. | Narrow reason; no evidence, owner, or exit gate. |

Documentary evidence may explain a cell, but only executable `harness` or
`pytest` evidence can support `pass`. `unknown` remains an owned result; it is
not permission to infer success or hide a case with `skip` or `xfail`.

## Register and promote evidence

For a corpus harness case:

1. Add deterministic inputs and canonical YAML under
   `tests/fixtures/dag_scenario_corpus/v1/<scenario-id>/`.
2. Add the case to the matching manifest scenario with the narrowest honest
   workflow: `build`, `run`, or `recovery`.
3. Add a top-level `kind: harness` evidence record with the same
   `<scenario-id>:<case-id>` locator and only the stages it proves.
4. Reference the evidence only from cells its assertions directly prove.
5. Extend the table-driven production-path integration assertions when the
   common expectation schema is insufficient.

For an existing executable test, use `kind: pytest` and a repository-relative
pytest node locator. The loader validates the file and node, and the contract
suite batch-collects every declared pytest locator.

Promote evidence and status in the same commit. Change a cell to `pass` only
when evidence covers the whole cell, then remove its `reason`, `owner_issue`,
and `exit_gate`. When evidence closes only part of a gap, retain `partial` and
rewrite the reason and exit gate to state exactly what remains.

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

The unit suite must reject malformed inventory or evidence. The integration
suite must run registered cases without skips or expected failures and assert
observed evidence, not merely successful process exit.

## Active Filigree work

This status snapshot was taken on 2026-08-29. It is navigation aid only; use
the commands below for current status, ownership, and dependencies.

| Issue | Snapshot status | Purpose |
| --- | --- | --- |
| `elspeth-ef29ef6ba4` | `in_progress`; blocked by `elspeth-cb1053fe46` | Complete the maintained production-path scenario matrix. |
| `elspeth-cb1053fe46` | `open`; blocks `elspeth-ef29ef6ba4` | Define and gate the supported scale envelope. |
| `elspeth-be41d0ea25` | `open` | Repair and CI-bind the normative execution-graph contract. |

```bash
filigree show elspeth-ef29ef6ba4 --json
filigree show elspeth-cb1053fe46 --json
filigree show elspeth-be41d0ea25 --json
```

Do not copy tracker-maintained case totals into this page. The manifest and
contract test own corpus counts; Filigree owns the state of the work.

## Historical assessment work

Temporary dated notes may be useful while collecting evidence. Before such
work is retired, update the live manifest first and run its contract test.
Public readers should use Git history to inspect earlier manifests and
verdicts. Maintainers may also keep an optional local archive outside the
published documentation tree, but the live docs must not depend on a dated
snapshot or present one as current authority.

The [assessment framework](../assessment-framework.md) defines the complete
reassessment workflow.
