# State Engine

This directory is the sole canonical entrypoint for Elspeth's durable state
engine architecture, completeness bar, proof inventory, assessment procedure,
current verdict, and historical assessments.

## Current verdict

| Field | Current value |
| --- | --- |
| Assessment | [2026-08-15 05:37 AEST](assessments/2026-08-15-0537/README.md) |
| Code baseline | `codex/state-engine-v3-assessment` at `2b4b04a8a852a839b7b395b0bcdfceb95676606b` |
| Catalog | `elspeth-state-engine-v3`, catalog schema 2, assessment schema 3, 73 legs, 7,010 required executable cells, per-cell applicability |
| Landscape schema epoch | 32 at the frozen baseline (SQLite); epoch is not pinned by the catalog |
| Verdict | **Not complete** |
| Current observations | 745 reporter-bound nodes across the three local SQLite WAL lanes all pass (712 single-process, 30 same-host follower, 3 web-hosted CLI follower); 489 exact cells are promoted with per-cell node attribution (417 pass, 72 partial after independent evidence review) |
| Primary gaps | PostgreSQL 16 AWS composition lane and all 38 protected live provider lanes unexecuted (`elspeth-82592e3aa1`); local cells whose dimensions carry no mapped assertion, including 38 provider-backed PB-09 cases without local internal-composition evidence (`elspeth-efb47cb5fd`) |

This is the first full v3 assessment. It inherits nothing: every promoted cell
binds retained JUnit/profile/node-index artifacts captured at the frozen
baseline, and every unpromoted required cell remains `unknown` with a named
tracker owner. All 73 legs retain at least one unknown mandatory cell, so
`HG-09-mandatory-leg-unresolved` is open.

Supported state stores and deployments (unchanged, catalog-pinned): SQLite WAL
under three one-host deployment profiles, and PostgreSQL 16 as the required
first-class backend for the maintained AWS single-leader Landscape deployment.
Multi-replica scheduling remains outside the claim.

Compiler handoff rule: a future `CompiledPipeline` binds the catalog ID and
digest recorded in the current dated `assessment.json` and may execute only
when runtime assembly reports a compatible state-engine contract.

Do not reuse historical denominators. The v1 68-leg result and the v2 73-leg
2,040-cell result answer different contracts than v3's 7,010 executable cells.

## Authority and precedence

Use this order when documents disagree:

1. Current source and freshly executed tests establish observed behavior.
2. [Completeness criteria](completeness-criteria.md) define the claim bar.
3. [Current architecture](architecture.md) describes the maintained model and
   known durable seams.
4. The [v3 proof catalog](proof-catalog/v3/catalog.json) defines the finite leg,
   dimension, case, and hard-gate universe.
5. The current dated `assessment.json` binds evidence and findings to one code
   baseline.
6. Filigree owns live work status, assignment, priority, and dependencies.
7. Older dated assessments preserve only their baseline-bound conclusions.

No other document is canonical for current state-engine status. In particular,
`docs/architecture/token-scheduler-state-engine.md` is a deprecated pointer.

## Start here

- [Architecture](architecture.md) — token scheduler, sink-effect, barrier,
  fencing, read-model, and transaction-boundary model.
- [Proof matrix](proof-matrix.md) — human-readable current result and open proof
  themes. The JSON catalog and dated manifest remain the machine authorities.
- [Completeness criteria](completeness-criteria.md) — status vocabulary,
  dimensions, hard gates, and completion tiers.
- [Assessment program](assessment-program.md) — exact reproducible procedure
  for full, delta, and historical rerun assessments.
- [Assessment framework](assessment-framework.md) — evidence and
  classification rules used while interpreting results.
- [Proof catalog](proof-catalog/README.md) — schema, stable IDs, and promotion
  rules.
- [Assessment history](assessments/README.md) — immutable baseline records and
  current pointer policy.

## Directory model

```text
docs/architecture/state_engine/
├── README.md
├── architecture.md
├── proof-matrix.md
├── completeness-criteria.md
├── assessment-program.md
├── assessment-framework.md
├── proof-catalog/
│   ├── README.md
│   ├── v1/catalog.json         # frozen historical contract
│   ├── v2/catalog.json         # frozen historical contract
│   └── v3/                     # current contract, schemas, and selector manifest
├── templates/
│   ├── assessment-readme.md
│   ├── verification-run.md
│   └── review-record.md
└── assessments/
    ├── README.md
    └── YYYY-MM-DD-HHMM/
        ├── README.md
        ├── assessment.json
        ├── evidence.md
        ├── review.md
        ├── nodes/              # retained exact node IDs when needed
        └── artifacts/          # retained JUnit/stdout/stderr when executed
```

Future assessments stay small. Add raw artifacts only when they preserve a
material fact that Git, a command vector, or an output digest cannot recover.
Do not add remediation checklists; create or update Filigree issues instead.

## When to reassess

Run a full assessment when any of these change:

- the state or subtype vocabulary;
- a scheduler, sink-effect, barrier, fencing, or read-model contract;
- transaction boundaries or restart choreography;
- supported database, worker, plugin, or deployment profiles;
- the proof catalog, hard gates, or completion semantics.

Run a delta assessment for a bounded implementation or evidence change. A
delta may update named cells but cannot declare the whole engine complete.

Update this page only after the new assessment passes its direct validation and
independent review. Never silently rewrite an older assessment to describe new
code.

## Assessment history

| Date | Baseline | Mode | Verdict | Notes |
| --- | --- | --- | --- | --- |
| 2026-08-15 | `2b4b04a8a` | First full v3 assessment | Not complete | All three local SQLite lanes green (745 nodes); 489 of 7,010 cells promoted with per-cell attribution (417 pass, 72 partial); live PostgreSQL-16 AWS and provider lanes unexecuted with named owners. |
| 2026-08-12 | `9f78d2b2a` | Task 5 SQLite delta assessment (v2) | Not complete | Promotes four reporter-bound SQLite single-process cells for queue rollback, TS-02 entry, and F-11 guard refusal; required PostgreSQL 16 AWS and follower cells remain unknown. |
| 2026-08-12 | `af79b3404` | Full v2 pinning assessment | Not complete | Pins 73 current legs, PostgreSQL 16 AWS single-leader support, fresh structural/tracker context, focused current observations, and live cohort ownership without inheriting v1 passes. |
| 2026-07-18 | `3c782ac3c` | Full contract refresh and source-ingress evidence | Not complete | Adds the exact source `COMPLETED` witness to TS-02/PB-01, records strict pre-fix reconciliation, and attaches 13 fresh focused checks without promoting the broader legs or hard gates. |
| 2026-07-18 | `422415009` | Full framework reset and conservative evidence import | Not complete | Introduces the 68-leg v1 catalog, explicit coordination state, sink-effect architecture, reproducibility contract, and reviewed authority model. |
| 2026-07-15 | `0dcd61ac` | Seed assessment | Not complete | Historical 18-leg Wave 1 result; useful evidence, obsolete denominator and blocker list. |
