# State Engine

This directory is the sole canonical entrypoint for Elspeth's durable state
engine architecture, completeness bar, proof inventory, assessment procedure,
current verdict, and historical assessments.

## Current verdict

| Field | Current value |
| --- | --- |
| Assessment | [2026-08-12 02:39 AEST](assessments/2026-08-12-0239/README.md) |
| Code baseline | `codex/state-engine-completion-plan` at `af79b34040f5ce5fd989aa0d42a1b80ad8366829` |
| Catalog | `elspeth-state-engine-v2`, schema 1, 73 legs, explicit semantic cases and required execution profiles |
| Verdict | **Not complete** |
| Current observations | 128 focused SQLite checks and 4 PostgreSQL 16 testcontainer checks pass on Python 3.13.1; no v2 cell is promoted without reporter-bound case/profile evidence |
| Primary gaps | Independent-process recovery, complete read-model/refusal truth tables, barrier families, external sink ambiguity, `ABANDONED`, first-party plugin lifecycles, and complete PostgreSQL semantics |

This assessment pins the current contract rather than inheriting historical
passes. All 73 v2 legs are `unknown`: each retains at least one mandatory cell
without current reporter-bound evidence. The v2 contract adds `ABANDONED`, row
union, RM-14, F-14, current first-party plugin inventory, and PostgreSQL 16 as
a required first-class backend for the maintained AWS single-leader Landscape
profile. Multi-replica scheduling remains outside that claim.

Do not reuse either historical denominator. The v1 68-leg result and the older
18-leg seed answer different contracts.

## Authority and precedence

Use this order when documents disagree:

1. Current source and freshly executed tests establish observed behavior.
2. [Completeness criteria](completeness-criteria.md) define the claim bar.
3. [Current architecture](architecture.md) describes the maintained model and
   known durable seams.
4. The [v2 proof catalog](proof-catalog/v2/catalog.json) defines the finite leg,
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
│   └── v2/catalog.json         # current contract
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
| 2026-08-12 | `af79b3404` | Full v2 pinning assessment | Not complete | Pins 73 current legs, PostgreSQL 16 AWS single-leader support, fresh structural/tracker context, focused current observations, and live cohort ownership without inheriting v1 passes. |
| 2026-07-18 | `3c782ac3c` | Full contract refresh and source-ingress evidence | Not complete | Adds the exact source `COMPLETED` witness to TS-02/PB-01, records strict pre-fix reconciliation, and attaches 13 fresh focused checks without promoting the broader legs or hard gates. |
| 2026-07-18 | `422415009` | Full framework reset and conservative evidence import | Not complete | Introduces the 68-leg v1 catalog, explicit coordination state, sink-effect architecture, reproducibility contract, and reviewed authority model. |
| 2026-07-15 | `0dcd61ac` | Seed assessment | Not complete | Historical 18-leg Wave 1 result; useful evidence, obsolete denominator and blocker list. |
