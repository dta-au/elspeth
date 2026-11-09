# adjacent02.17 slice 1 — semantic termination and preflight drain

Serial implementation brief, prepared 2026-11-09; no implementation performed
by this brief. Parent supplies the current worktree/base after preceding work.
Read that worktree's `AGENTS.md`, `CONTRIBUTING.md` whole-tree gates and canonical
lane `d2-portable-lifecycle-design.md`. User approved portable supervision;
parent accepted the authenticated loopback seam. No agents, commits, pushes,
providers, migrations or broad suites. Parent owns broad/static corpus gates.

## Outcome and boundary

Deliver reviewable shared foundations without changing public MCP startup or
claiming its missing-owner defect fixed. Keep existing Web Linux recovery
behavior identical; give its owned watchdog interface platform-neutral
termination operations. Add explicit seal/join ownership to the actual
`RuntimePreflightCoordinator`. Existing callers that never seal retain current
single-flight, timeout, cancellation and error behavior.

Primary files: `src/elspeth/web/process_watchdog.py`, `process_recovery.py`,
`execution/runtime_preflight.py`, `tests/fixtures/process_watchdog.py`, focused
Web watchdog/recovery/coordinator tests. Inspect current files before editing;
line numbers in earlier maps are evidence, not edit coordinates. No MCP
launcher, new process owner, D2 path code or Web lifespan rewrite in this slice.

## Implement

1. Add owned semantic operations for graceful termination and forced termination
   to `ProcessWatchdogControl`. Migrate the three `ProcessRecovery` signal calls
   to them so portable recovery never evaluates a missing Windows SIGKILL.
   Linux `ProcessWatchdog` still uses its existing pidfd SIGTERM/SIGKILL
   operations; retain `request_signal` as a compatibility/internal helper if
   appropriate. Update all nominal owned implementations/fixtures deliberately.
   Do not use runtime structural dispatch, platform no-ops or broad catches.
2. Preserve acknowledgement-before-graceful-termination, exactly-once
   escalation, forced termination after supervision failure, first recovery
   reason/deadline and original cancellation/failure retention. Do not change
   the required-generation witness/submission/quarantine algorithm.
3. Add one coordinator seal-and-join API with a clearly documented closed state.
   Seal atomically against new `run` admissions; new calls after seal receive an
   explicit owned unavailable outcome. Existing in-flight tasks/awaiters stay
   owned until actual completion. Waiter timeout/cancellation never evicts or
   releases unfinished work early. Repeated close/join is safe and does not
   restart work. Retain cancellation while completing the join, then propagate
   it with distinct actual cleanup failures as appropriate.
4. Observe actual retained task outcomes, including completion racing eviction.
   Distinguish existing `RuntimePreflightFailure` result values from actual
   task/join failures: do not silently turn an ordinary already-delivered
   preflight result into a second cleanup error or model response. Document how
   abandoned failures are observed. Never sweep `asyncio.all_tasks`, cancel
   unrelated host work, or inspect `_inflight` from transport code.

## Focused acceptance

| Contract | Required evidence |
|---|---|
| Linux adapter | Real Linux watchdog subprocess still arms then sends existing SIGTERM/SIGKILL behavior; existing exact signal/order tests remain valid |
| Semantic portability | Owned fake that supports semantic operations independently of Unix signal symbols works through actual `ProcessRecovery`; no `sys.platform` mock claimed as Windows execution |
| Idempotence/failure | Repeated recovery and failure during normal shutdown retain first outcome; ack failure/monitor failure forces termination; repeated caller cancellation retains actual ack/join |
| Producer custody | Actual `RuntimePreflightCoordinator.run` creates retained task; two same-key callers produce one worker; one caller timeout leaves worker and admission alive |
| Seal race | Work admitted before seal finishes and is joined; work attempted after seal never starts; close with no work and repeated close behave deliberately |
| Actual worker join | Isolated fresh child with an explicit real test lifecycle submits a blocking real worker, cancels/times out caller, then releases worker; close cannot finish before actual worker/task completion |
| Failure observation | Worker failure after caller leaves is observed; task cancellation, task failure and close cancellation retain truthful identities without duplicate delivery |

Use existing homes `tests/unit/web/test_process_watchdog.py`,
`test_process_recovery.py`, and
`tests/unit/web/execution/test_runtime_preflight_coordinator.py`. New tests must
drive actual recovery/coordinator producers; a fake that itself implements
seal/join cannot prove the production behavior. A fake watchdog is appropriate
for precise ordering, with existing real watchdog children retained as control.

Capture meaningful pre-fix red for cancellation/seal behavior and retained
existing behavior controls, then focused green. A missing new method/import is
not by itself semantic regression evidence. Retain the known fresh public
stdio absent-owner failure as an explicitly unresolved acceptance item.
Use lane-private logs with command and exit code, real worktree import
provenance and `-n 0` for subprocess/race tests. Run scoped Ruff/mypy and only
affected whole-tree contract gates needed for changed interfaces; ask parent to
coordinate any expensive/shared-host checks. No full suite from this slice.

## Handoff gate

Write `d2-lifecycle-1-report.md` and `d2-lifecycle-1-review.diff` in this canonical
lane (diff includes new files). Report API/behavior choices, changed paths,
exact red/green commands/exits, ordinary-failure versus cleanup-failure handling,
reviewed complete changed files and platform limits. Freeze edits for parent
review before slice 2. No public owner is exposed at this point, and public
stdio preview remains unfixed.
