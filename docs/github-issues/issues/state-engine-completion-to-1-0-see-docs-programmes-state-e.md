---
title: State engine completion to 1.0
labels: [area/engine, type/task]
---

Tracking issue for the remaining contract-closure work on ELSPETH's durable state engine ahead of 1.0. The detail lives in `docs/programmes/state-engine-1.0/`; this issue exists so there is one place to find it.

## Where this lives

The state engine is the part of the runtime that owns durable execution state: run lifecycle, work-item scheduling and leasing, token lineage, and the audit tables behind them. The code is under `src/elspeth/core/landscape/`, with `scheduler/`, `execution/` and `data_flow/` holding most of what these cohorts touch. The architecture, the completeness bar it is measured against, and the current verdict are all in `docs/architecture/state_engine/README.md`, which is the right first read.

## Scope

Six contract-closure cohorts, plus a final assessment and the gates maintained alongside them:

- queue, source, transform and gate contracts;
- lease coordination and the read model;
- aggregation coalesce and row union;
- sink-effect publication and repair;
- lifecycle abandonment follower and plugin contracts;
- the final full assessment.

## Where the detail lives

- `docs/programmes/state-engine-1.0/README.md` — the programme's scope, its documents of record, and all six cohorts.
- `docs/programmes/state-engine-1.0/tracker-rows.json` — the seven work items this one replaces, retained so nothing was lost in the consolidation.

## Documents of record

- `docs/plans/2026-08-11-state-engine-pinning-and-completion.md`. Every cohort cites this by task number, so it is the map between the list above and the actual work.
- `docs/architecture/state_engine/` — architecture, completeness criteria, assessment framework, proof matrix, proof catalog, and the assessments themselves.
- `docs/plans/2026-08-15-state-engine-six-issue-disposition.md` and `docs/plans/2026-08-17-state-engine-local-residual-split.md`, both listed in the programme README.

## Size

This is a programme, not a task, and it is not something to pick up cold. The published assessment records the verdict as not complete. The largest gaps are verification profiles that have never been executed, rather than code known to be broken. Anyone starting here should read the architecture README first and then agree a single cohort with the maintainer; taking the whole thing on is not a realistic unit of work.

## Note

The programme previously ran as an epic plus six step issues. Those seven were consolidated into this one pointer after their content was captured in the folder above.
