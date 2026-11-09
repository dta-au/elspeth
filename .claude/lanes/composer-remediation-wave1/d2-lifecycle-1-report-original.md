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
