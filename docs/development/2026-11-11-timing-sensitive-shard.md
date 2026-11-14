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

## Defects to investigate rather than mark (first pass; see the follow-up section below)

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

## Follow-up: D1-D5 investigated (same day, later)

Method: systematic debugging, no fix without a proven cause. Investigation by
Sol, reviewed by Astra, measured and checked by the lead. Lane evidence (not
tracked): `.claude/lanes/timing-shard/d1/`, `d3/`, `instr/`.

**Corrections to the first pass.** "This id was never load-bound" and the
first-pass framing that all of `test_healthy_child_fails_closed_when_supervisor_is_killed`
was a load victim were wrong: see D2. And the D3 commit message ties the
reason-recording to the first-upload 404 in #360; the investigation did not
establish that this was that request's cause, only that the reason was being
discarded (the tests inject reasons).

### D1 (child stuck in "closing landscape") - NOT RESOLVED; mechanism proven at short durations

- Evidence: `LandscapeDB.close()` is `engine.dispose()`; on the last SQLite
  connection the DBAPI close checkpoints and fsyncs the WAL. Standalone (no
  harness), 12 closes each: idle median 9.5 ms, max 11.3 ms; with a bounded
  fsync writer median 68 ms, p90 152 ms, max 513 ms. In 27 samples taken
  inside timed closes, 24 were `State: D`, 23 of those in syscall 74 (fsync);
  `wchan` was `jbd2_log_wait_commit` (11), `folio_wait_bit_common` (8),
  `rq_qos_wait`, `__wait_on_buffer`, `wait_transaction_locked`. In 8 of 8
  kill-during-close trials the child was in D with SIGKILL pending
  (`SigPnd`/`ShdPnd` = 0x100) and exited -9 after 14-231 ms. One fsync write
  under a single bounded writer took 20.4 s (host `full avg10` reached 35%).
- Hypotheses killed: another connection's lock does not make close consume
  `busy_timeout` (close took under 1 ms with a competing `BEGIN IMMEDIATE`;
  SQLite's close takes the checkpoint lock without a busy handler); pool or
  thread joins (dispose does not join; no heartbeat thread present); interpreter
  exit after close (target return to exit 147-357 ms); harness release protocol.
- Not reproduced: a 15 s close, or a child alive 2 s after SIGKILL. Worst
  measured natural close 513 ms. So the historical stall is not explained;
  what is proven is that the ext4 journal commit wait can park a closing
  process in D state, that SIGKILL then waits, and that one fsync here can take
  20 s under pressure.
- Product or harness: the close path's fsync is SQLite's documented WAL
  behaviour, not an incorrect shutdown contract, and the harness joins already
  wait on the exit sentinel. No product change is justified without a
  durability/journal decision, which is not taken here. Measurements are local
  ext4; EFS/NFS behaviour (SQLite WAL is documented as unsupported on network
  filesystems) was not measured.
- Done: the next occurrence names itself. `ProcessDiagnostics.snapshot` and the
  `could not clean up child` failure now carry `State`, `SigPnd`/`ShdPnd`,
  `wchan`, `syscall` and per-thread states (tests with mutation controls).
- Options for a ruling, none implemented: (a) leave; the timing job plus the new
  evidence is enough to learn more. (b) Test-only: put the recovery tests'
  database directory on tmpfs so their SIGKILL-survival checks do not depend on
  disk fsync latency; this changes what the tests exercise (no real-disk
  checkpoint) and Docker's default `/dev/shm` is 64 MB. (c) Any change to
  checkpoint/synchronous settings on production paths: not recommended on this
  evidence; would need crash/reopen proof and an explicit decision.
- Owed: the same measurement on a slow or network volume if the AWS/Azure
  targets matter for this question.

### D2 (`early-stop-abort` hung past 15 s once) - NOT RESOLVED; a different race found and fixed

- Reproduction attempts: the 8-id function 30 times (240 ids) at natural load,
  the whole file 3 times, and the id 20 times under a bounded single-writer fsync
  load (two 80 s batches, 376 MB and 141 MB written, pressure guard stopped each
  at 15-18% `full avg10`): no hang. The original occurred in a whole-file run
  while other test processes were active.
- Instrumentation added so a repeat identifies itself: child timeline events
  (`child-started`, `imports-done`, `connected`), probe alarm diagnostics (owner
  state, child `/proc` block, all thread stacks), and `finish()` turning a
  timeout into an assertion that carries them and kills the Popen-owned
  supervisor group and the direct children found at timeout. Not a general
  process-tree guarantee: a runner that outlives its supervisor and was not a
  direct child at that moment is not covered (Astra, accepted as a limit).
  Hypotheses H1 (import/connect stall past the 12 s alarm), H2 (supervisor race)
  and H3 (leftover state) remain untested by evidence.
- Found by the whole-file runs (2 of 3 failed, at load 2-3, `full avg10` 0):
  `test_healthy_child_fails_closed_when_supervisor_is_killed` tested
  `state_file.exists()` and then read it; init can reap the zombie in between
  (`ProcessLookupError`/`FileNotFoundError`). 7 failures in 30 serial runs on an
  idle host before the fix, 0 in 60 after. This is one of the ids the issue
  attributed to load; it was not load-bound. Fix: one reader helper that reports
  a vanished entry as `None`; test for the helper. The same pattern was in a
  control added earlier the same day and is fixed too. Astra's search for
  other `/proc` exists-then-read sites found none.
- Astra also found incomplete-publication races (a reader treats a file's
  existence as readiness and parses it at once): fixed for the portable
  supervisor probe (`event()` now publishes by atomic replace, with a test).
  Still owed, not fixed here: `tests/helpers/composer_parent_death_probe.py`
  (`ready.json`) and `tests/unit/cicd/test_ci_proof_progress.py` (PID files).

### D3 (404 for a lost session-operation fence drops its reason) - RESOLVED (diagnostic loss)

- Where it was discarded: `session_operation_handlers.py` (shared handler,
  `_exc` unused) and the fork route's local 404 for a missing reservation. The
  detached composer-operation projector is deliberately pure; the other sites
  rethrow, settle privately or already carry the reason into private audit
  facts (table in the lane report).
- Fix: one structlog warning at each of the two HTTP mapping points with only
  `reason` (closed enum), HTTP method and the matched route template; response
  bytes unchanged for every reason. Emission failures are contained like the
  sibling handlers (the 404 survives a broken log backend; Tier-1 errors still
  escape). Tests: every reason, byte-exact body, exact fields, leak controls
  (session id, query token, user header absent from log and response), no raw
  path when no route matched, failing backend, Tier-1 passthrough; removing the
  call fails 7 tests. The registrar source pin in the mutation-fencing gate was
  updated for the reviewed change; the canonical `reserve_admission_on_actual_sources`
  proof was re-run on the final source (see commands).
- Limits (Astra): the production processor chain adds the middleware's
  `request_id` to every record, and that value can be client-supplied; it is the
  existing correlation field and the same one the sibling handlers log, so it is
  kept, but "ids are never logged" in the commit message is too strong. The
  helper assumes an HTTP request (a websocket scope would fail on `.method`; no
  current path raises fence loss there). Whether routine refusal logging fits
  `.claude/skills/logging-telemetry-policy/SKILL.md` (logs only when audit and
  telemetry cannot fire) is a policy ambiguity: the web layer already logs
  bounded HTTP diagnostics as a recovery channel, but the skill has no explicit
  exemption. Needs a one-line ruling or an operator-telemetry metric instead.
- The tests that hit the original 404 were not reproduced; whether their
  cause was a lease lapse remains unproven. Trust-tier lint (keyless, touched
  files): handler 0 to 0, fork route 5 to 5. No schema/SQL/persistence/lock
  change; no testcontainer run owed.

### D4 (2 s join after SIGKILL in `harness.py`) - NO CHANGE JUSTIFIED

`multiprocessing.Process.join(timeout)` already waits on the exit sentinel, so
there is no wrong-condition wait to replace. The longest kill-to-exit measured
(231 ms, child in D with SIGKILL pending) is inside the 2 s budget, so no
measurement supports a larger bound, and none was made. The failure message now
carries the child's kernel state, which is what a future change needs.

### D5 (solo shard still fails on a loaded host) - MITIGATED only as far as D1/D4

Measured reruns of the full 119-id selection, serial: first run, load 11-17:
845.9 s, 1 failed (`...web_hosted_leader_plus_same_host_cli_followers...`,
child blocked in the heartbeat wait after a 2 s spawn/import), stalls of 30 and
51 s in ids that take 3-9 s; rerun after the fixes, host load about 1:
753.2 s wall, 733.8 s summed, 118 passed, 2 skipped (one by design when the
state-engine profile reporter is absent, one a collection skip), 0 failed. The
second run says the ids pass on a quiet host; it does not show robustness under
contention, which D1 shows still produces multi-second fsync stalls. No
host-level machinery was added.

### Commands run (exit codes)

Focused set after the final edits (handlers, supervisor file, recovery harness
file, process diagnostics, fork test): exit 0, 124 passed in 79 s. 60 serial
runs of the repaired race: 0 failures. Collect-only proof re-run at 060cddd51: 74,024 before, 73,905 after, 119 in
`-m timing_sensitive`; `before - after` equals that list exactly, 0 in both,
0 new. Architecture gate `test_required_work_reserve_admission_on_actual_sources`
on the final source: exit 0, 293 s. Ruff check and format: clean on touched files.
