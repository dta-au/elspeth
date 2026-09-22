---
title: State engine completion to 1.0
labels: [area/engine, type/task]
---

Tracking issue for the state engine's remaining contract-closure work ahead of 1.0. The detail lives in `docs/programmes/state-engine-1.0/`; this issue is the single pointer to it.

## Scope

Six contract-closure cohorts, plus a final assessment and the gates that are maintained alongside them:

- queue, source, transform and gate contracts;
- lease coordination and the read model;
- aggregation coalesce and row union;
- sink-effect publication and repair;
- lifecycle abandonment follower and plugin contracts;
- the final full assessment.

## Where the detail lives

- `docs/programmes/state-engine-1.0/README.md` — the programme's scope, its documents of record, and all six cohorts.
- `docs/programmes/state-engine-1.0/tracker-rows.json` — the seven container rows this issue replaces, captured verbatim.

## Documents of record

- `docs/plans/2026-08-11-state-engine-pinning-and-completion.md`, which every cohort cites by task number.
- `docs/architecture/state_engine/` — architecture, completeness criteria, assessment framework, proof matrix, proof catalog and the assessments themselves.
- `docs/plans/2026-08-15-state-engine-six-issue-disposition.md` and `docs/plans/2026-08-17-state-engine-local-residual-split.md`, both listed in the programme README.

## Why it is one issue

The programme previously ran as an epic plus six step issues. Those seven rows were consolidated into this single issue pointing at the folder, after their content was captured there.
