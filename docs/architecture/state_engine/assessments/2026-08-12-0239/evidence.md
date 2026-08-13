# Executed Evidence

Evidence was executed on 2026-08-12 AEST in
`/home/john/elspeth/.claude/worktrees/state-engine-assessment-2026-08-12`, a
clean detached worktree at `af79b34040f5ce5fd989aa0d42a1b80ad8366829`.
The local environment used Python 3.13.1, pytest 9.0.3, SQLite 3.47.1, and
SQLAlchemy 2.0.45. No full test suite was run.

## Focused current results

| Vector | Exact selectors | Result | Honest classification |
| --- | --- | --- | --- |
| EV-OBS-01 | `tests/unit/core/landscape/test_scheduler_fencing.py` and `tests/unit/core/landscape/test_scheduler_pending_sink_claim.py` | 46 passed; one expected `env_files` warning; 10.41 s | Current narrow SQLite observation; no v2 cell promotion |
| EV-OBS-02 | `tests/integration/pipeline/test_builtin_sink_effect_recovery.py` and `tests/unit/core/landscape/test_scheduler_repository_complete_barrier.py` | 39 passed; one expected `env_files` warning; 6.70 s | Current narrow SQLite observation; no v2 cell promotion |
| EV-OBS-03 | The eight literal TS-07–TS-10 selectors listed below | 30 passed; one expected `env_files` warning; 2.28 s | Current narrow disposition observation; no v2 cell promotion |
| EV-OBS-04 | The ten literal source/resume selectors listed below | 13 passed; one expected `env_files` warning; 11.97 s | Current narrow source/resume observation; no v2 cell promotion |
| EV-OBS-PG-01 | `tests/testcontainer/core/test_scheduler_lease_eviction_postgres.py` and `tests/testcontainer/core/test_run_coordination_release_postgres.py`, with `-m testcontainer` | 4 passed on `postgres:16-alpine`; 9.53 s | Selected first-class PostgreSQL paths; not the complete PB-11 case matrix |

EV-OBS-01 through EV-OBS-04 used
`.venv/bin/python -m pytest -p no:dotenv -q -n 0`. EV-OBS-PG-01 used
`.venv/bin/python -m pytest -q -n 0 -m testcontainer`.

EV-OBS-03 literal selectors:

- `test_normal_dispositions_refuse_reclaimed_sink_redrive_without_mutation`;
- `test_transform_disposition_truth_table_commits_exact_row_event_and_branch_loss`;
- `test_transform_disposition_truth_table_refuses_stale_owner_without_mutation`;
- `test_transform_disposition_truth_table_refuses_departed_member_without_mutation`;
- `test_transform_disposition_truth_table_rolls_back_when_event_insert_fails`;
- `test_branch_loss_failure_rolls_back_disposition_row_and_event`;
- `test_mark_blocked_refuses_missing_release_key_without_mutation`;
- `test_mark_pending_sink_rejects_incomplete_bundle_without_mutation`.

All eight are in
`tests/unit/core/landscape/test_scheduler_events.py`. EV-OBS-04 literal
selectors are the nine `TestProcessRowNoTransforms` methods named below plus
the final E2E selector:

- `test_records_source_node_state`;
- `test_fenced_ingest_commits_source_completion_before_return`;
- `test_fenced_ingest_rolls_source_completion_back_with_scheduler_failure`;
- `test_source_completion_reconciliation_rejects_conflicting_state`;
- `test_source_completion_reconciliation_rejects_duplicate_claim_witness`;
- `test_source_completion_reconciliation_rejects_mismatched_work_item_witness`;
- `test_source_completion_reconciliation_rejects_later_attempt`;
- `test_source_completion_reconciliation_rejects_malformed_existing_state`;
- `test_source_completion_reconciliation_rejects_source_impossible_metadata`;
- `tests/e2e/recovery/test_concurrent_resume.py::TestMidClaimCrashResume::test_ts02_source_completion_gap_reconciles_once_before_plugin_execution`.

The exact full argv arrays, environment, JUnit counts, exit codes, artifact
paths, and SHA-256 digests for all six observations are retained in
`artifacts/observation-index.json`. Raw outputs whose captured bytes contain
trailing whitespace are stored with deterministic `gzip -n -9`; the index
records both the stored-file digest and the decompressed raw-byte digest.

These observations are deliberately absent from `assessment.json.evidence`.
The v2 validator permits promotion only when the trusted profile reporter
binds each exact node to a case and execution profile in retained JUnit,
profile, and node-index artifacts. The selected production/repository tests do
not yet contain that probe. Recording green stdout as proof would manufacture
attribution the run did not establish.

## Python 3.14 failed observation

Plain `uv sync --frozen --all-extras` selected CPython 3.14.3 because the
project declares `requires-python = ">=3.12"`. On that clean environment,
EV-OBS-01 passed 46 checks, but EV-OBS-02 produced 9 failures and 30 passes.
Every failure occurred while building the Runtime-VAL manifest:
`_normalize_dependency_value` raised `FrameworkBugError` for
`elspeth.contracts.errors:__classdict__[value]` of type
`builtins:member_descriptor` (representative candidate:
`PassThroughContractViolation.divergence_set`). Recreating the local
environment explicitly with Python 3.13.1 made EV-OBS-02 pass 39/39.

This is tracked unclaimed as `elspeth-61350c4744`, blocks the lifecycle cohort
and final completion step, and remains a failed supported-runtime observation.
The Python 3.13 baseline is the reproducible assessment environment; it does
not erase or downgrade the 3.14 defect.

## Structural and temporal capture

`loomweave worktree analyze <assessment-worktree> --no-incremental` created
isolated store `wt-be4cafe43983c9793bbee6fcb29c125233f6f86bd2aa0fb26127a4614031e3bf`.
Run `1aa9de74-7763-44a5-98e6-07cc88ff0d66` persisted status `completed` at the
exact baseline commit with 71,628 entities, 152,366 edges, zero dropped edges,
and a complete source/classifier walk. It also retained seven warnings: two
call-resolution timeouts, three Pyright restarts, one poisoned-frame fallback,
and one 2,305-site reference cap. The CLI continued into post-analysis
embedding work; after 45 minutes it was interrupted once (exit 130) under the
authorized wall-time rule and was not retried. No `project_status_get` response
was retained, so the package reports the completed exact-commit run rather
than claiming that tool's `staleness` field or semantic-search completeness.

Warpline 1.3.0 was then invoked once against the same detached worktree:

- full snapshot 1 is `SKIPPED`, with `source_version: no_index`, 0 entities,
  and 0 edges because Warpline could not consume Loomweave's isolated store;
- `changed` for
  `3c782ac3c7efb0550495be38f75800eddffa639a..af79b34040f5ce5fd989aa0d42a1b80ad8366829`
  returns an empty local set, which is non-authoritative because this worktree's
  Warpline store has no usable graph/change history;
- `reverify` returns `NO_SNAPSHOT`, `graph_fresh: false`, no graph reference,
  and unavailable risk verification.

The snapshot response also emits `enrichment.edges: "skipped"`. Project
guidance defines the enrichment vocabulary as only `present`, `absent`, or
`unavailable`, so `skipped` is retained verbatim as a non-conforming tool
result and treated as `unavailable` for this assessment. It is not accepted as
a fourth project state or as evidence that edges are absent.

The retained artifact hashes are:

| Artifact | SHA-256 |
| --- | --- |
| `artifacts/loomweave-analysis.json` | `3389a3b140a027b8df91ef42ed20b584b4d7e722f8fe0812f76adeecb628e679` |
| `artifacts/warpline-snapshot.json` | `592c956f186932cf20df5803c0f2d970ca4b4df452d497b09020252e3255009d` |
| `artifacts/warpline-changed.json` | `401bddd3c63f192c2ae9da12d6d622935e807523ddd24963d87c8ae8b7dcf9e3` |
| `artifacts/warpline-reverify.json` | `0599d868447698ba2d807cdbdd10d2c800ef294b9c394639214538ba7092a2f8` |

No absence or downstream-unreachability conclusion is drawn from this
warning-bearing Loomweave edge surface or Warpline's SKIPPED/NO_SNAPSHOT
results. Current source, executable tests, and the conservative unknown cells
remain authoritative.

## Filigree capture

Filigree 3.1.0 JSON was captured after creating and wiring the v2 plan. Each
retained envelope reports `has_more: false`:

| Query | Items | Artifact SHA-256 |
| --- | ---: | --- |
| `filigree search '[state engine]' --json` | 18 | `e30835e472c5bab7b79838e9d1d61ed245bceab989750ae08561a631934f4685` |
| `filigree ready --json` | 832 | `0e4d46b66235a0e6c3a958804cb7b588ae8086e4d9268ec7c041f2e418b4ee3b` |
| `filigree blocked --json` | 43 | `e1515d5a56f462443d6c68be92d72aaca1d8bba6ed581462b40d3ba0785c87e1` |

In addition, `artifacts/filigree-show-records.ndjson` retains exact
`filigree show --json` records for the milestone, all three phases, all eight
steps, the six linked pre-existing issues, and the Python 3.14 bug (19 records;
SHA-256
`859e6ad7fe719e3fdfc7b1c4be8d6ec60dbc3e98f3d5805613290c94840d1727`).
Those records make parent/child relationships, `blocks`/`blocked_by` edges,
assignees, readiness, and close anchors reconstructible from the dated
package. They were captured at 2026-08-12T03:33:21+10:00, after the three
list/search responses above and before this package commit.

The phase-0 catalog step `elspeth-4833bc0dc1` is completed against the Task 1
and Task 2 commits; `elspeth-af65f78095` is ready and will close only after the
assessment package has a commit to cite. The three queries and later exact
record capture were sequential and Filigree is live mutable state, so this is
a complete capture of each response rather than a cross-command atomic
snapshot. The exact records establish that the six retained pre-existing
issues and the Python 3.14 bug were open and unclaimed at record-capture time;
the list/search envelopes preserve the post-wiring ready and blocked views at
their own earlier capture time. No implementation issue was claimed.

## Limits

- No v1 pass is inherited and no v2 proof cell is promoted.
- PostgreSQL 16 is required; four selected checks do not establish all five
  PB-11 semantic cases or every applicable dimension.
- No provider credentials were available or captured; real-provider plugin
  acceptance remains unknown rather than mocked.
- No multi-replica scheduling claim is made.
- The full CI-equivalent test suite was intentionally deferred to the final
  pre-merge gate under the user's wall-time constraint.
