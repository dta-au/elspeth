# DAG Information and Completeness

This directory is the live entry point for evaluating ELSPETH's directed
acyclic graph (DAG) capability. It separates product-quality criteria,
executable evidence, and delivery tracking so readers do not have to choose
between competing dated reports.

## Current authority

| Question | Authoritative live source |
| --- | --- |
| What quality bar applies? | [Completeness criteria](completeness-criteria.md) |
| How is the bar assessed? | [Assessment framework](assessment-framework.md) |
| What scenarios, evidence, gaps, and verdict are current? | [Scenario manifest](scenario-corpus/v1/manifest.yaml) |
| What enforces that inventory and evidence contract? | [Scenario corpus contract test](../../../tests/unit/architecture/test_dag_scenario_corpus_contract.py) |
| What delivery work is open and who owns it? | [Live scenario corpus and Filigree references](scenario-corpus/README.md#active-filigree-work) |

The manifest and its contract test are the **authoritative live** assessment
record. Read the manifest for its current schema version, scenario and
dimension inventories, evidence registry, and derived verdict; this page does
not copy those changeable values.

## Two related views

The [completeness criteria](completeness-criteria.md) define product-quality
criteria for judging the DAG capability as a supported product. The manifest
applies executable lifecycle cells to every mandatory scenario.

These are different axes. The product criteria describe what a credible
completeness claim must cover; the lifecycle cells record executable evidence
for concrete scenarios. Neither number should be used as a substitute for the
other.

## Start here

- [Live scenario corpus](scenario-corpus/README.md) explains the manifest,
  evidence rules, focused checks, and current delivery work.
- [Completeness criteria](completeness-criteria.md) defines the stable quality
  bar, hard gates, and mandatory scenario set.
- [Assessment framework](assessment-framework.md) defines how to reassess the
  live record without creating a second current authority.

## Updating the assessment

1. Add or strengthen executable evidence and observe the relevant assertion
   fail before repairing the behavior or proof.
2. Update the live manifest in the same change: evidence reference, affected
   cells, gap ownership, exit gates, and executable cases must agree.
3. Update the contract test when the closed inventory or schema changes.
4. Run the focused contract and production-path suites documented by the
   [scenario corpus](scenario-corpus/README.md#run-the-focused-checks).
5. Reconcile delivery status, dependencies, and ownership in Filigree. Do not
   copy tracker case counts into this page.

## History

Git history is the public record of earlier manifests, verdicts, and supporting
documents. Temporary assessment notes may help while evidence is being
collected, but update the live manifest first and do not leave a dated report
presented as the current result. Maintainers may keep an optional local archive
outside the published documentation tree; public readers should use Git
history.

## Authority boundaries

- [`../../contracts/execution-graph.md`](../../contracts/execution-graph.md) is
  the normative execution-graph contract.
- [`../adr/README.md`](../adr/README.md) indexes accepted architecture
  decisions.
- [`../state_engine/README.md`](../state_engine/README.md) records the durable
  scheduler state and evidence model.
- [`completeness-criteria.md`](completeness-criteria.md) defines the bar for a
  completeness claim.
- [`scenario-corpus/v1/manifest.yaml`](scenario-corpus/v1/manifest.yaml) owns
  the live corpus inventory, evidence inputs, cell states, and derived verdict.
- Filigree owns delivery status, dependencies, and work ownership.

When these surfaces disagree, record and resolve the contradiction. Do not
quietly select the most convenient source.
