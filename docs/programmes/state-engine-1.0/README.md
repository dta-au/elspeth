# State engine — completion to 1.0

Six contract-closure cohorts plus the final assessment and the maintained gates.

One GitHub issue points here. This folder is the detail: the programme's scope as it stood in
the legacy issue tracker on 2026-09-23, captured before those rows were closed. Tool names have since been generalized;
the original wording is preserved in the local tool-retirement archive.

## Why this is a folder and not seven issues

GitHub has no `milestone` / `phase` / `step` issue type. Operator ruling 2026-09-23 (John):
*"for the epics, a single ticket pointing to a folder with the details"*. So the container rows
below were closed in legacy issue tracker and their content lives here. The tracker rows are preserved
verbatim in `tracker-rows.json` alongside this file — nothing was summarised away.

## Documents of record

- `docs/plans/2026-08-11-state-engine-pinning-and-completion.md` — the plan every step below cites by task number
- `docs/architecture/state_engine/` — architecture.md, completeness-criteria.md, assessment-framework.md, proof-matrix.md, proof-catalog/, assessments/
- `docs/plans/2026-08-15-state-engine-six-issue-disposition.md`
- `docs/plans/2026-08-17-state-engine-local-residual-split.md`

## Status

The epic is already the flattened form of a cancelled milestone/phase/step tree (elspeth-4b3d734e3a). It absorbed four proof-cohort steps, the final assessment step, and all 13 open '[state engine]' tasks and bugs.

## A note on the `elspeth-…` identifiers

The identifiers below are rows from **legacy issue tracker**, the internal tracker ELSPETH used
before moving to GitHub Issues. They are opaque local ids: they name nothing outside that
tracker, and the tracker is being retired.

They are kept here deliberately, because this folder exists to preserve the provenance of
work whose tracker rows were closed — the id is the link between a closed row and the
content that replaced it. They should **not** appear in GitHub issue bodies, where they
would read as a dangling reference to a system a reader cannot see;
`docs/github-issues/check_issues.py` blocks them there for that reason.

## Scope

Closed container rows: `elspeth-1040aa2143` (epic) and 6 children.

### 1. Close queue source transform and gate contracts

*was `elspeth-d262ace360` (step, P1, filed 2026-08-11)*

Task 5 in docs/plans/2026-08-11-state-engine-pinning-and-completion.md. Owns TS-00–02, TS-07–10, PB-01–03, F-01/02/11. Exit: production-boundary queue/source/transform/gate evidence covers rollback, restart, refusal, and every required execution profile. Existing dependencies are retained context, not duplicate owners.

### 2. Close lease coordination process and read-model contracts

*was `elspeth-eefd990b46` (step, P1, filed 2026-08-11)*

Tasks 4, 6, and 7 in docs/plans/2026-08-11-state-engine-pinning-and-completion.md. Owns TS-03–06, AUX-01/02/06/07, RC-01–07, PB-11, RM-01–14, F-04/06/07/10/12. Exit: independent-process and read-model/refusal evidence covers every required cell, and PB-11 separately executes DB-server-time, row-lock-order, transaction-isolation, schema-admission-migration, and ambiguous-connection-loss cases across every required dimension on real PostgreSQL 16 for the AWS single-leader profile.

### 3. Close aggregation coalesce and row-union contracts

*was `elspeth-cc0b256aca` (step, P1, filed 2026-08-11)*

Task 8 in docs/plans/2026-08-11-state-engine-pinning-and-completion.md. Owns TS-15–18, AUX-03–05, PB-04/05/10, F-03/05/09/13. Exit: aggregation, coalesce, and row-union arrival/loss/timeout/release/late-arrival/restart matrices pass across every required profile.

### 4. Close sink-effect publication and repair contracts

*was `elspeth-f227dd8d2f` (step, P1, filed 2026-08-11)*

Task 9 in docs/plans/2026-08-11-state-engine-pinning-and-completion.md. Owns TS-11–14, PB-06/07, F-08. Exit: effect publication, ambiguous connection loss, restart/repair, real external-provider, and supported-profile evidence establishes exactly one effective publication.

### 5. Close lifecycle abandonment follower and plugin contracts

*was `elspeth-67be892457` (step, P1, filed 2026-08-11)*

Tasks 10 and 11 in docs/plans/2026-08-11-state-engine-pinning-and-completion.md. Owns TS-19, PB-08/09, F-14. Exit: ABANDONED finalization/accounting/refusal, follower lifecycle, every first-party plugin, real-provider where required, and every supported profile pass; Python 3.14 Runtime-VAL blocker elspeth-61350c4744 is closed.

### 6. Run the final full assessment and install maintained gates

*was `elspeth-f89d82e925` (step, P1, filed 2026-08-11)*

Task 12 in docs/plans/2026-08-11-state-engine-pinning-and-completion.md. Owns final frozen full assessment, maintained CI/release gates, and compiler handoff. Exit: all five cohorts and Python 3.14 blocker close; every required v2 cell is pass or catalog-approved not_applicable; all hard gates close; final full suite and required trust/release gates run once at the frozen commit.
