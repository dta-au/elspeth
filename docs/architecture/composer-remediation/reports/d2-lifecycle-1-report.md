# Lifecycle foundations slice 1

Implementation is frozen for parent/Astra review in
`/workspace/elspeth/.claude/worktrees/composer-remediation-lifecycle-foundations`.
Base/HEAD remains `f9c23c59c63bbd3099029e995a778bade594f6c8` (no commits).
The final focused run exited 0: **52 passed in 55.14s**. This report covers
shared foundations only. Public stdio preview's absent lifecycle owner remains
OPEN until the later MCP integration slice; no public MCP startup was changed.

## Changed paths and behavior

* `src/elspeth/web/process_watchdog.py`: nominal `ProcessWatchdogControl` now
  requires `request_graceful_termination()` and `force_termination()`. The real
  Linux owner maps these to its existing pidfd SIGTERM/SIGKILL implementation.
  `request_signal` remains on the Linux implementation for compatibility and
  its internal bootstrap path, rather than on the portable abstract contract.
* `src/elspeth/web/process_recovery.py`: its three signal-dependent sites use
  semantic operations. Acknowledgement still precedes graceful termination;
  first reason, exactly-once escalation, required-generation observations and
  monitor behavior are retained. If forced termination itself fails, the
  original supervision failure and distinct termination failure are raised
  together. Successful force still propagates the exact original failure.
* `src/elspeth/web/execution/runtime_preflight.py`: `seal_and_join()` closes
  admission under the existing admission lock, shares one retained join, and
  waits for actual preflight tasks plus their existing run awaiters. Later
  calls (including the same key) receive the existing `RuntimePreflightFailure`
  envelope containing owned `RuntimePreflightUnavailable`; no third result
  variant or consumer migration was introduced.
* `tests/fixtures/process_watchdog.py`: implements both semantic operations
  while preserving existing exact Linux signal evidence.
* `tests/unit/web/test_process_watchdog.py`: semantic-owner producer tests
  reject any Unix signal dispatch; ack/monitor failure identities and force
  failure aggregation are covered.
* `tests/unit/web/execution/test_runtime_preflight_coordinator.py`: actual
  producer tests cover seal/refusal, idempotent empty/repeated close, timeout
  custody, failure/eviction races, multiple awaiters, actual task abort and
  cancellation, consumed earlier cancellation, repeated join cancellation,
  and a fresh child with explicit real executor ownership and a blocking
  real worker.

All six changed files were read in full after the final edits/formatting.
`test_process_recovery.py` was read and run unchanged. No path-authority,
executor algorithm, Web lifespan, launcher, configuration, metadata/signature,
or suppression edits were made. All currently owned nominal watchdog
implementations were inspected and updated. An external subclass of this
abstract owner must implement the two new semantic operations.

## Failure and cancellation custody

Task outcomes are retrieved exactly once by the coordinator's completion
observer, independently of key eviction. Successful and delivered outcomes
are released. Undelivered failures remain retained for lifecycle join,
including failures completing before seal. Existing awaiters settle before
join decides whether a failure was delivered. An ordinary
`RuntimePreflightFailure` returned to any caller is therefore not converted to
a second cleanup error; one cancelled waiter does not duplicate a result
delivered to a patient waiter. A per-caller timeout envelope is not delivery
of the still-running worker's later outcome.

Ruling: abandoned ordinary failure envelopes are observed at task completion
and their original exception is raised by lifecycle join if no caller received
them. Actual uncaptured task exceptions and task cancellation use the same
custody, preserving original identities. This avoids clean completion over a
lost failure. Cost: an abandoned failure and its traceback remain retained
until the coordinator lifecycle ends; successful/delivered tasks do not.

Join shields its retained task through repeated caller cancellation, retains
the first caller cancellation, finishes owned work, then propagates that
original cancellation alongside distinct undelivered failures when necessary.
Worker cancellation is distinguished from fresh caller cancellation by the
change in the caller's cancellation count, so an already-consumed lifecycle
cancellation does not replace a subsequent worker cancellation. No all-task
sweep, unrelated task cancellation, early admission release or new executor
was added. Repeated join replays its retained failure outcome without
resubmitting work.

## Commands and receipts

All commands ran from the worktree above. Every pytest invocation exported
`PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src`, supplied matching
`-o pythonpath="$PWD/src $PWD/elspeth-lints/src"`, and used `-n 0`.
The live import probe resolved both `elspeth.__file__` and
`elspeth_lints.__file__` inside this worktree.
Each name below has the lane-private `.log` and explicit `.exit` receipt
under prefix `d2-lifecycle-1-`.

| Receipt | Invocation/selection | Exit and observation |
| --- | --- | --- |
| `red` | `pytest tests/unit/web/test_process_watchdog.py tests/unit/web/execution/test_runtime_preflight_coordinator.py -k 'semantic_owner_operations or worker_cancellation_preserves_original_identity or seal_'` | 1; 10 failed. Beyond missing seal API, actual recovery used prohibited Unix dispatch and actual worker cancellation lost identity. |
| `green-initial` | Same two files, `-k 'not armed_channel_loss'` | 0; 40 passed, 1 deselected. |
| `force-red` | Watchdog file, `-k failed_force_preserves_original` | 1; 2 failed because force failure masked the original supervision failure. |
| `green-edges` | Same two files, `-k 'not armed_channel_loss'` | 0; 45 passed, 1 deselected. |
| `consumed-red` | Coordinator file, `-k consumed_caller` | 1; worker cancellation identity was replaced after a previously consumed caller cancellation. |
| `green-final-edges` | Same two files, `-k 'not armed_channel_loss'` | 0; 46 passed, 1 deselected. |
| `focused` | `pytest tests/unit/web/test_process_watchdog.py tests/unit/web/test_process_recovery.py tests/unit/web/execution/test_runtime_preflight_coordinator.py --basetemp="$PWD/.claude/lifecycle-foundations-final-pytest" --junitxml=/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-lifecycle-1-focused.xml` | 0; 52 passed in 55.14s. Includes real Linux 30-second SIGKILL deadline, Uvicorn SIGTERM teardown, and fresh real-worker child. |
| `ruff` | `python -m ruff check` all six changed paths | 0; all checks passed. |
| `format` | `python -m ruff format --check` all six changed paths | 0; six files already formatted. |
| `mypy` | `python -m mypy --cache-dir "$PWD/.claude/lifecycle-foundations-mypy" src/elspeth/web/process_watchdog.py src/elspeth/web/process_recovery.py src/elspeth/web/execution/runtime_preflight.py` | 0; no issues in three source files; only the configured unused-override note. |
| `contracts` | `python -m scripts.check_contracts` | 0; all contract checks/census match; no rebind needed. |
| `diff-check` | `git diff --check` | 0. |

The final child output/exit evidence is preserved at
`d2-lifecycle-1-final-pytest/test_seal_joins_real_worker_in0/worker-child.log`
and `worker-child.exit` (exit 0, `real worker and coordinator joined`).
Other final pytest child artifacts and host logs are in that same
`d2-lifecycle-1-final-pytest/` directory; intermediate artifacts are in
`d2-lifecycle-1-edge-pytest/`.

## Freeze and review package

Used the existing `d2-checks/freeze_tree.py`, which reads tracked symlinks
as link payloads without following directory symlinks and also includes
untracked/ignored source and test inputs excluding caches.

The raw all-input manifests honestly differ: `freeze-before` has 6211 inputs
and `freeze-after` has 6227. The exact 16 additions are listed in
`d2-lifecycle-1-freeze-result.json`; all are generated files/symlinks under
`.claude/lifecycle-foundations-final-pytest/`. Those temporary pytest directories
were unexpectedly untracked rather than ignored. Both owned pytest artifact
directories were moved into the canonical lane after preserving the raw
manifests, leaving only the six intended modified paths in git status.

Comparison included **every git-tracked path plus every source/test input**
from the complete manifests. The identical 6205 entries include 12 symlinks;
HEAD, membership, modes and content hashes match before/after. A comparator
positive control changing the runtime-preflight entry was detected.
`d2-lifecycle-1-freeze-result.json` records `frozen: true` for that scope.
The final clean candidate's entire 6205-entry manifest was separately checked
against the pre-test entries and matches them exactly.

Final clean manifest: `d2-lifecycle-1-freeze-final.json`; its canonical manifest
hash is `290e6a24c3aba8ab5d31b9e88f869e630a02f01455bd288d4bbc0c8f37bb490f`.
Raw before/after/final logs and exit receipts are preserved alongside manifests.

Frozen review diff: `d2-lifecycle-1-review.diff` (28530 bytes), SHA256
`090f1758577b194354e3521979e48610a8501eb6d8131b418026a33e90c7ee6f`.
It contains the complete tracked diff against HEAD; untracked-file enumeration
found zero remaining new files. The package generator would append binary
no-index patches for any new file. Parent should review the diff and candidate
at the final freeze; no further source/test edits are planned.

## Open verification and platform limits

Parent owns Astra review, integration and all broad/static-corpus gates.
No broad pytest, all-rules scan, installs, commits or pushes were run.
The three long Landscape proofs and their exact binding checks remain pending
with the parent; notably the recovery-cell proof is
`test_required_work_reserve_admission_on_actual_sources`. Its directly pinned
`ProcessRecovery.__init__`, `_begin` and `required_generation_expired` bodies
were left unchanged, but this is not a claim that the proof passed. No
signature or blanket binding refresh was attempted; report any gate-specific
binding delta against the frozen candidate during parent checks.

Linux evidence covers the real existing Web watchdog/process controls and
shared semantic interface behavior. The semantic fake is not native Windows
or macOS/BSD qualification. This slice adds no platform restriction and does
not claim that the new portable MCP owner exists yet. Native platform and
public stdio lifecycle acceptance remain for the later serial slices.

## Fix round 1/5 — R1 and R2

The sections above preserve the original reviewed candidate and its evidence.
This fix record supersedes its abandoned-failure retention ruling and the
corresponding custody description. Parent's explicit ruling in `progress.md`
requires observation and release, rather than an archive of historical errors.
Original report bytes are also saved as `d2-lifecycle-1-report-original.md`;
the original `d2-lifecycle-1-review.diff` is unchanged at its recorded SHA256.
Pre-fix copies of the two amended files are under
`d2-lifecycle-1-original-snapshot/`.

R1 is locally reproduced and fixed: the completed-task test now makes two
immediate zero-budget same-key calls with no intervening yield, parametrized
for success and ordinary failure. It also verifies that exposed admission
still has a retained record while an original waiter remains. One centralized
release condition now requires observation, eviction, and zero remaining
awaiters. Delivery alone cannot remove the record. This keeps `_inflight`
lookup, pending waiter custody and the seal snapshot consistent.

R2 is locally reproduced and fixed: abandoned ordinary failure envelopes are
reported exactly once after eviction and waiter settlement, with only the
exception class in a small owned `structlog` error event. No exception value,
traceback, request key or user content is passed to the logger. This observes
the loss of the result-delivery channel where there is no caller or pipeline
run to record the failure; it does not add a parallel record of run outcomes.
The reporting policy follows the parent ruling and was checked against the
repository logging policy. The production docstring pins this distinction.

Ordinary `RuntimePreflightFailure` values remain ordinary results even if a
caller left; they are never promoted to artificial cleanup errors. Completed
abandoned actual task failures outside a join are also reported once and
released. A seal atomically takes custody of its current unfinished task and
awaiter obligations; genuine undelivered failures/cancellations of those
owned tasks retain their original identities in the join outcome. Delivered
failures are neither duplicated in logs nor raised again at join. There is no
historical task/traceback registry or new task framework. The earlier ruling
to retain every abandoned ordinary exception until close has been withdrawn.

The sequential producer regression runs 25 abandoned failures for each of
cancellation and timeout, asserting empty admission/retention registries after
each completion, exactly 25 class-only reports per scenario and collection of
every worker task through weak references. This proves release during normal
service before seal. Other controls cover late immediate retries before
eviction, completion before/after seal, a cancelled and a patient waiter,
ordinary failure during a repeatedly cancelled join, real task abort and
task cancellation during an owned join, completed task errors reported before
seal, and the existing fresh real-worker lifecycle child.

Only `src/elspeth/web/execution/runtime_preflight.py` and
`tests/unit/web/execution/test_runtime_preflight_coordinator.py` changed in this
round. Both final files were read in full; the final test-only timeout-receipt
release was then inspected separately. Semantic termination and all four
other files from the original review are unchanged.

### Fix1 verification receipts

All receipts below are in the canonical lane with prefix
`d2-lifecycle-1-fix1-`. Each named test/check has an explicit `.exit` receipt.
All pytest commands again used both worktree import roots, matching
`-o pythonpath`, and `-n 0`. All fix1 temporary pytest output is directly under
the canonical lane, so no generated worktree inputs needed to be removed or
filtered from this round's freeze.

| Receipt | Covering command/observation | Exit |
| --- | --- | --- |
| `r1-red` | Coordinator file, `-k exhausted_budget_still_joins`: both immediate-retry scenarios fail with `KeyError`. | 1 |
| `r1-green` | Same command: 2 passed, 19 deselected. | 0 |
| `r2-red` | Coordinator file, `-k 'many_abandoned or seal_observes_failure or completed_abandoned_actual or join_preserves_owned or late_waiter or seal_retains_repeated'`: 8 failed, 4 passed, 15 deselected; reproduces archive/incorrect-cleanup policy. | 1 |
| `coordinator-green` | Complete coordinator file before adding the timeout variant: 27 passed in 2.42s. | 0 |
| `focused-initial` | Final scoped command below, first timeout-variant run: 56 passed, 1 failed, 3 deselected. The failing weakref assertion was test-owned timeout-envelope retention, not coordinator registry retention. | 1 |
| `focused` | Final scoped command below: **57 passed, 3 deselected in 7.25s**; includes the fresh real-worker child, immediate retries, 50 sequential abandoned workers and semantic watchdog/recovery controls. | 0 |
| `ruff` | `python -m ruff check` on the two amended paths. An initial B023 finding on loop closure variables was corrected with explicit bindings; final check passes without suppression. | 0 |
| `format` | `python -m ruff format --check` on the two amended paths: both formatted. | 0 |
| `mypy` | `python -m mypy --cache-dir "$PWD/.claude/lifecycle-foundations-mypy" src/elspeth/web/execution/runtime_preflight.py`: no issues; configured unused-override note only. | 0 |
| `contracts` | `python -m scripts.check_contracts`: all checks/census match; no binding/config change. | 0 |

The timeout-variant failure was diagnosed independently: the coordinator
registry was empty, while a caller-held timeout exception traceback kept its
`run` locals and worker task alive. Releasing that caller-owned result made
the task collectible. The test now drops its own timeout receipt before
measuring coordinator retention; no production code was changed for this
diagnosis. The failed focused log, XML and freeze manifests remain preserved
with `-initial` names.

Final exact scoped command, from the worktree:

```bash
PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src .venv/bin/python -m pytest \
  tests/unit/web/test_process_watchdog.py tests/unit/web/test_process_recovery.py \
  tests/unit/web/execution/test_runtime_preflight_coordinator.py \
  -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" \
  --basetemp=/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-lifecycle-1-fix1-final-pytest \
  -k 'not real_watchdog_armed_channel_loss and not fatal_worker_exits_uvicorn_process' \
  --junitxml=/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-lifecycle-1-fix1-focused.xml
```

The three long subprocess cases were not rerun because their watchdog/recovery
source is unchanged; their original 52-test cohort evidence remains above.
No broad tests, all-rules scans or long Landscape proofs ran in this round.

### Fix1 frozen artifacts and remaining handoff

The complete `fix1-freeze-before.json` and `fix1-freeze-after.json` bytes match,
including all 6205 tracked/source/test inputs, their membership/modes and
symlink payloads. Canonical manifest SHA256:
`a38a76152c8f73641f2f48f2e560fd0e27500634f57499e20d2dc56e7d34904b`.
Comparison to the original reviewed manifest proves exactly the two amended
paths changed; no other production/test file or new file was added.

* Fix delta relative to original review: `d2-lifecycle-1-fix1.diff`, 16507 bytes,
  SHA256 `61e27c6c0dc7292bdc313ba6fb098ef791a50d65bd1ae1d7b2d0a2669fab6232`.
  Its reverse applicability to the final candidate was checked without writes.
* Complete final diff against unchanged HEAD: `d2-lifecycle-1-review-fix1.diff`,
  37369 bytes, SHA256
  `ac17c62a32342cda4c2c46a6bd6481a4d72ab4bd006aafc4e2ad0cf0fe524ef5`.
* Original review diff/report snapshot remain preserved. The final fresh
  real-worker child log/exit are under
  `d2-lifecycle-1-fix1-final-pytest/test_seal_joins_real_worker_in0/`.

R1/R2 are ready for parent-owned Astra scoped rereview. This is fix round 1/5,
with no commit, integration or new permission request. Broad/static corpus
gates, exact Landscape proof bindings, native platform qualification and the
public stdio missing-owner repair remain pending exactly as before.

## Fix round 2/5 — R3 stdout-safe abandoned-outcome reporting

The fix1 scoped review marked R1 and R2 ADDRESSED and identified R3:
unconfigured structlog routes the new diagnostic to stdout, corrupting an
existing stdio consumer's protocol stream. This record supersedes only the
fix1 sink choice; its release/observation policy and ownership algorithm are
unchanged. The complete fix1 report is preserved as
`d2-lifecycle-1-report-fix1.md`, and pre-edit copies of both amended files are
under `d2-lifecycle-1-fix1-snapshot/`. Original and fix1 diff artifacts remain
byte-identical at their previously recorded hashes.

The coordinator now uses `logging.getLogger(__name__)`, matching the current
MCP server's stdlib logging convention. Its unconfigured fallback is stderr;
when a host configures ELSPETH logging, the same stdlib record goes through
that host's existing handlers and `ProcessorFormatter`. No global logging
setup, handler installation, Web initialization or supervisor dependency was
added. Each error message contains only its fixed event tag and exception
class. The class is rendered in the message, so the unconfigured fallback
retains the diagnostic and configured Web JSON retains its event/level format.
No exception value, traceback, request key or private worker content is emitted.

The new fresh-child producer uses only asyncio and production coordinator
imports in its unconfigured path: no pytest imports, Web startup or logging
configuration. It admits a real coordinator worker, cancels its caller,
releases the worker into either ordinary failure or actual task abort, waits
for completion/observation, then checks release at seal. Both unconfigured
branches assert empty stdout and exactly one literal class-only stderr line.
The same child with explicit `configure_logging(json_output=True)` verifies
configured Web formatting remains one valid JSON error record with the event
and class, and no stderr output. All four children assert private exception
content is absent and save stdout/stderr/exit receipts under the lane-private
pytest basetemp.

Existing in-process observation checks now use pytest's stdlib `caplog` sink
and inspect real coordinator records, rather than replacing structlog dispatch.
The complete coordinator file still covers R1/R2 races, bounded release through
50 sequential abandoned workers, exact one-report behavior, actual task and
caller cancellation identity, no duplicate delivered failure, and the fresh
real executor worker. Both amended files were read in full after the fix.

### Fix2 verification receipts

All named receipts below have lane-private `.log` and explicit `.exit` files
with prefix `d2-lifecycle-1-fix2-`. Every pytest invocation exported both
worktree import roots, supplied matching `-o pythonpath`, and used `-n 0`.

| Receipt | Covering command/result | Exit |
| --- | --- | --- |
| `red` | Coordinator file, `-k fresh_child_reporting`: 2 failed, 2 passed, 28 deselected in 6.09s. Both unconfigured children emit diagnostic bytes to stdout on fix1; configured JSON controls pass. | 1 |
| `green` | Complete coordinator file: 32 passed in 9.52s after the sink repair. | 0 |
| `focused` | Exact final command below: **48 passed in 9.76s**, comprising complete coordinator and core logging controls. | 0 |
| `ruff` | `python -m ruff check src/elspeth/web/execution/runtime_preflight.py tests/unit/web/execution/test_runtime_preflight_coordinator.py`: all checks pass. | 0 |
| `format` | `python -m ruff format --check` on those two paths: both formatted. | 0 |
| `mypy` | `python -m mypy --cache-dir "$PWD/.claude/lifecycle-foundations-mypy" src/elspeth/web/execution/runtime_preflight.py`: no issues; configured unused-override note only. | 0 |
| `contracts` | `python -m scripts.check_contracts`: all checks/census match; no configuration or binding refresh. | 0 |

Final exact scoped command, from the worktree:

```bash
PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src .venv/bin/python -m pytest \
  tests/unit/web/execution/test_runtime_preflight_coordinator.py \
  tests/unit/core/test_logging.py -n 0 \
  -o pythonpath="$PWD/src $PWD/elspeth-lints/src" \
  --basetemp=/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-lifecycle-1-fix2-final-pytest \
  --junitxml=/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-lifecycle-1-fix2-focused.xml
```

No unchanged long watchdog/Uvicorn cases or broad/static corpus gates were
rerun. Their source remains unchanged, and the original/fix1 evidence remains
preserved. This round fixes the new reporting side effect in an existing
consumer; it does not implement the later public MCP lifecycle owner.

### Fix2 frozen review artifacts

The complete fix2 before/after manifest bytes match: all 6205 tracked/source/test
inputs, including symlink payloads and new-file membership, are unchanged
through final verification. Canonical manifest SHA256:
`adf50234d227498f699f19624c38e89f7457125a959023a5117dc3a7b7e764b3`.
Comparison to the fix1 manifest confirms exactly the coordinator source and
its test file changed; no additional files changed or were added.

* Delta against fix1: `d2-lifecycle-1-fix2.diff`, 13607 bytes, SHA256
  `94c8743e7791497acfc1fd77287148bd9a00c248b49d072d252ae74663725f65`.
  Reverse applicability to the final candidate was checked without writes.
* Complete final diff against unchanged HEAD:
  `d2-lifecycle-1-review-fix2.diff`, 41235 bytes, SHA256
  `689df4bf37d4aa7c7cfeab32be67b4bad1546397d2579541f3607189d71004a7`.

R3 is locally reproduced and repaired, ready for parent/Astra scoped rereview.
R1/R2 custody and bounded reporting remain in the passing coordinator cohort.
No new ruling, approval request, metadata/suppression change, commit or push
was made. Exact Landscape proof bindings, broad gates, native platform
qualification and public stdio missing-owner acceptance remain parent-owned
or deferred to their approved later slices.
