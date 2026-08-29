# DAG Completeness Assessment Framework

Use this framework to reassess ELSPETH's DAG capability without creating a
second current authority. The [live scenario manifest](scenario-corpus/v1/manifest.yaml)
and its [contract test](../../../tests/unit/architecture/test_dag_scenario_corpus_contract.py)
hold the current inventory, evidence inputs, gap states, and derived verdict.

## Assessment basis

An assessment must identify:

- the assessed Git commit and whether the worktree was clean;
- the manifest path and serialized `schema_version`;
- the exact commands executed and their results;
- environmental limitations or unavailable evidence; and
- the Filigree issues consulted for delivery status and dependencies.

Evidence is strongest in this order:

1. deterministic production-path runtime or recovery execution;
2. focused executable tests of the exact claim;
3. source and contract inspection;
4. architecture records and maintained plans; and
5. issue metadata or narrative claims.

Lower-ranked evidence can explain a result, but it cannot replace missing
production-path proof.

## Two assessment views

The framework deliberately keeps two axes separate:

- **15 product-quality criteria** judge whether the DAG capability is
  functionally complete, reliable, supportable, observable, secure, and
  usable as a product. Their normative definitions are in the
  [completeness criteria](completeness-criteria.md).
- **11 executable lifecycle cells** are evaluated for every mandatory
  scenario in the manifest: configuration, build, contracts, runtime, audit,
  recovery, concurrency, freeform authoring, guided authoring, round trip, and
  scale.

The criteria are a product scorecard; the cells are a scenario evidence
matrix. A count or result on one axis does not imply a result on the other.

## Hard gates

The overall verdict is `not_complete` when any applicable scenario cell is
`partial`, `fail`, or `unknown`, or when a cross-cutting hard gate lacks
executable proof. Hard gates include:

- durable identity, checkpoint, and restart correctness;
- exactly-once or explicitly bounded external effects;
- stale-owner fencing and contention behavior;
- audit and portable-export integrity;
- authoring/runtime agreement and semantic round trip;
- secret-safe graph identity and diagnostics;
- a declared, repeatable scale envelope; and
- maintained normative contracts bound to executable evidence.

Do not calculate a maturity aggregate while mandatory evidence remains
unknown. The manifest's derived verdict is authoritative.

## Cell status rules

| Status | Use when |
| --- | --- |
| `pass` | Current executable evidence proves the whole applicable cell. |
| `partial` | Current evidence proves only part of the cell. |
| `fail` | Current evidence demonstrates a requirement violation. |
| `unknown` | Adequate current evidence has not been executed or does not exist. |
| `not_applicable` | The dimension genuinely does not apply to the scenario. |

Every non-pass applicable cell requires a precise reason, a Filigree
`owner_issue`, and an observable `exit_gate`. `not_applicable` requires a
narrow reason and no ownership or evidence. Documentary and decision records
may support a claim, but only `harness` or `pytest` evidence can support
`pass`.

## Product-criteria rating scale

Use the [normative 0–5 and `U` rating scale](completeness-criteria.md#evidence-and-rating-scale)
when assessing a product criterion. Do not infer a rating from a lifecycle
cell status or create a second scale in an assessment. Hard-gate failure
overrides any average and keeps the overall verdict `not_complete`.

## Reassessment workflow

1. Freeze the assessed commit and record worktree state.
2. Load the live manifest through the strict loader before interpreting any
   narrative summary.
3. Run the focused contract suite and all relevant registered production-path
   cases.
4. Add or strengthen executable evidence for changed behavior; do not promote
   by analogy from a nearby scenario.
5. Update the live manifest first: evidence registry, case declarations, cell
   states, ownership, and exit gates must agree in one change.
6. Run the contract test again and confirm the derived verdict.
7. Query Filigree for current work status, dependencies, and ownership. Do not
   copy tracker-maintained case counts into the assessment.
8. Update the live hub or corpus guide only when navigation, authority
   boundaries, or maintainer guidance changes. Do not restate values derived
   from the manifest.

## Review checks

Before publishing a reassessment, verify:

- every manifest scenario has exactly the closed 11-dimension inventory;
- every `pass` cell has applicable executable evidence;
- every registered harness case has a matching evidence locator;
- every non-pass applicable cell has live issue ownership and an observable
  exit gate;
- evidence locators collect and repository-relative documentation links
  resolve; and
- the stated verdict matches the manifest's derived verdict.

## History and temporary notes

Git history is the public record of earlier live manifests and verdicts.
Temporary dated notes can be used during evidence collection, but they must
not become a parallel current assessment. Update the live manifest first,
link public readers to Git history, and then remove the temporary material.
An optional maintainer-local archive may be retained outside the published
documentation tree; live documents must not depend on it.
