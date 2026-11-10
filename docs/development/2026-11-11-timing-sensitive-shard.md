# Timing-sensitive solo shard (issue #360): candidate search and result

Branch `ci/timing-sensitive-shard`, six commits on `f6a0c502f`. Measured
2026-11-11 on a shared host (load average 11-17 on 24 cores, I/O pressure
present throughout). The path differs from the one the brief named because
`docs/reviews` is gitignored.

## What landed

- Marker `timing_sensitive`, excluded from the default `-m` in `addopts`.
- CI job `timing-sensitive` per Python version: serial (`-n 0`), `needs: [test]`
  with `always()` and the fork-PR guard. Its `-m` replaces addopts' and is pinned
  by test to the addopts expression with only `not timing_sensitive` reversed.
- `scripts/ci_test_shards.py` partition `timing`: manifest records the marker
  from `iter_markers` on actual items and the call outcome of every test. The
  Combined coverage validator refuses empty, overlapping, collect-only,
  all-skipped or wrongly attributed evidence and folds 3.13 timing coverage in.
  CI Success and Combined coverage require the job.
- `scripts/full-suite-gate.sh --stages timing`; AGENTS.md, CONTRIBUTING.md and
  three guides say a green `pytest tests/` does not cover these tests.
- No `concurrency` group: GitHub cancels pending runs in a group, which would
  silently drop a required job.

## Selection proof (final commit, collect-only, `-n 0`)

| selection | ids |
|---|---|
| before (explicit `-m` equal to the old addopts expression) | 73,982 |
| after (bare default) | 73,863 |
| `-m timing_sensitive` | 119 |

`before - after` is 119 ids and `diff` of that list against the `-m
timing_sensitive` list is empty. In both: 0. In `after` but not `before`: 0.
Control: `test_portable_supervisor.py::test_real_owned_completion_and_status[normal]`
(unmarked) is in `after` only; a marked id is in `timing` only.

Solo shard cost: the first 97 ids ran serially in 845.9 s wall, 821.9 s summed
test time (1 failed, 2 skipped, see below); the 22 ids added afterwards ran in
54.5 s (22 passed). About 14.6 minutes, 374 s of it two deployment-profile
tests. This job could not be run in CI from here, and `actionlint` is not
installed: the workflow was parsed as YAML and pinned by contract tests only.

## Structural causes, how they were searched, members

Mechanism common to the evidence: a fixed wall-clock bound (15 s readiness, 2 s
join after SIGKILL, 1-5 s elapsed assertions, 30 s planner deadline) sits on a
path whose latency depends on shared I/O and scheduling: spawn plus interpreter
import plus SQLite open/WAL, process teardown, fsync. A process in
uninterruptible I/O (D state) does not die on SIGKILL until the I/O completes,
which fits "alive 2 s after SIGKILL"; this was not verified (the diagnostics do
not record `/proc/<pid>/stat`).

1. **Real child processes behind fixed readiness/join bounds.** Search: files
   matching spawn/kill patterns (83 files) intersected with fixed-wait patterns
   (60 files); controls: three known members matched, `test_canonical.py` and
   `test_config.py` did not. Then measured. Serial runs reproduced the issue's
   failure class at load 12: `could not clean up child at process seam
   resume-completed` (`test_process_death_reopens_same_sink_effect_and_publishes_once[after_inspection]`,
   31 s) and stalls of 19-51 s in ids that take 3-9 s (duplicate-admission,
   aggregation, `[before_reservation]`). Marked: whole functions of the recovery
   harness users (sink-effect matrices, duplicate admission, barrier, aggregation,
   lifecycle, registered-process authority, depth-5, state-engine harness,
   deployment profiles), the long-plugin-lease pair (real 2 s lease), the two
   composer_mcp files' timing-bound ids. Left alone: template-worker tests
   (root-fixed by a ready handshake, S3b), power-automate and row-union recovery
   (15 passed, 35 s, no stall), most of `portable_supervisor` and
   `standalone_lifecycle` (88 of 89 passed serially in 190 s, ids 0.3-2 s; the
   maintainer asked for the cheap ids to stay), testcontainer files (own job).
2. **Asserted elapsed-time bounds.** First pass keyed on variable names and
   missed tests (Astra found four); the second pass is an AST scan that follows
   clock reads through assignments (ad-hoc scripts, not committed; control: known positive
   `28 <= elapsed <= 35`, known negatives excluded by hand: timestamp-ordering
   checks such as `before <= ts <= after`, which no stall can break). 22 members
   marked in two passes. Residual risk: the scan does not see bounds computed
   through helpers.
3. **Lease/heartbeat expiry.** Real leases of seconds exist only in
   `test_long_plugin_lease_recovery` (marked). The 30 s session-operation lease
   upload tests (`shareable_reviews`) are not marked: they fail through a product
   behaviour (below).
4. **fsync/WAL latency.** `grep fsync` finds only tests that count mocked calls;
   none asserts on fsync latency. Not a direct class; it acts through class 1.
5. **Sleeps as synchronisation.** 43 sleeps of at least 0.05 s inside test bodies,
   mostly poll loops. The four that wait out a real 1-4 s deadline
   (`test_scheduler_deadline_completion`, `test_composer_operation_authority`,
   `test_composer_adoption_recovery_native`, `test_composer_operation_lifecycle`)
   sleep longer than needed, so a stall helps rather than hurts; left.
6. **In-process hang guards of 2 s or less** (`wait_for`, `Event.wait`; 172 sites
   in 60 files). They only fail when the loop stalls for seconds; marking them
   would bloat the shard. Left; `test_auto_title` is marked because the issue
   names it with a failure (Astra argued for unmarking it as an in-process guard;
   rejected on that evidence).
7. **Planner wall-clock budget.** `test_pipeline_planner.py` runs 177 planner
   calls under a real 30 s deadline (`timeout_seconds`, already raised once in
   #354) while the scripted protocols take 2-3.7 s idle; the two failing ids do
   real SQLite custody. Marked per the issue. A test-side fix is available and
   better: `test_planner_strict_dialect.py` already freezes
   `_planner_deadline_time`; doing that in `_plan()` removes the class. Not done:
   it touches 177 call sites in a 7,000-line file.

## Defects to investigate rather than mark

- **D1. Recovery child stuck "closing landscape".** `LandscapeDB.close()` is
  `engine.dispose()`; on the last SQLite connection that checkpoints and fsyncs
  the WAL. Hypothesis: D-state I/O in that path under host I/O pressure. Not
  reproduced. Next step: make `ProcessDiagnostics.snapshot` record
  `/proc/<pid>/stat` state and `wchan`.
- **D2. `early-stop-abort` hung past 15 s once** (supervisor probe
  `communicate(timeout=15)` expired; normal run 3 s) in a serial run of the
  portable-supervisor file at load 13; 0 failures in 96 runs of the same id group
  alone. Could be contention or a supervisor race; not explained.
- **D3. The 404 for a lost session-operation fence** drops the reason
  (`session_operation_handlers.py`); logging it is the fix the issue proposes.
- **D4. Cleanup ladder.** 2 s joins after terminate/kill in
  `harness.py` are short for a process in D state. Not changed (no bound loosened
  here); fixed instead was the masking below.
- **D5. Solo job is not isolated from the rest of the host.** The serial local
  run still failed one deployment-profile id and stalled others at load 15
  (`leader did not produce follower-claimable work`, child blocked in the
  heartbeat wait after a 2 s spawn/import). The job runs after the shards but
  concurrently with other jobs (landscape proofs, other runs). It reduces the
  contention; it does not remove it. Whether that failure is load or product is
  open.

Fixed in this branch: `wait_until_ready` called `close()` after taking its
evidence snapshot and a failed cleanup replaced the readiness error, destroying
the snapshot (seen in the `after_inspection` failure above). The readiness error
now survives with the cleanup failure as a note. Negative control: with the old
code the new test fails.

## Adversarial review (Astra) and disposition

1. Missed wall-clock-bounded tests: accepted, verified in code; 12 functions
   (22 ids) marked, search rebuilt.
2. Remaining `portable_supervisor` cases: partly accepted (the eight deadline
   faults marked); the cheap completion/authentication cases stay, per the
   issue's own guidance and the 88/89 serial pass.
3. `test_auto_title` marker: rejected (the issue names it with a failure).
4. Partition completeness against a `-k` narrowing: rejected as out of design;
   ordinary shards have the same property and the workflow command is pinned.
5. All-skipped or collect-only timing evidence could qualify: accepted, fixed
   (outcome recording and validator checks, with negative controls including a
   real-subprocess all-skipped run).
6. CI wiring and docs: none found after searching.

## Own review

Read the full diff as a reviewer. Found: a setup-phase skip (a fixture
`importorskip`) leaves a marked id without a call outcome, so the validator
refuses the evidence; CI installs all extras so this should not occur, and a
loud failure is the safer side, but it is stricter than the ordinary shards.
Local `--stages timing` uses a bare `-m
timing_sensitive`, which would also select a timing test that carried `slow`.

## Not verified

The CI job itself (never run), `actionlint`, the full default suite and the
Testcontainer suite, coverage floors with timing coverage included, 3.12.
