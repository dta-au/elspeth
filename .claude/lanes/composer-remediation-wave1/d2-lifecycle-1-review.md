# Lifecycle foundations slice 1 review

Reviewed the frozen candidate in
`/workspace/elspeth/.claude/worktrees/composer-remediation-lifecycle-foundations`
against `d2-lifecycle-1-foundations-brief.md` and the relevant approved portable
lifecycle design. Base and HEAD are
`f9c23c59c63bbd3099029e995a778bade594f6c8`. Review artifact SHA256 is
`090f1758577b194354e3521979e48610a8501eb6d8131b418026a33e90c7ee6f`.
I read the complete 692-line artifact and all six changed files, the unchanged
recovery tests, the nominal watchdog implementations, and relevant production
Web/MCP preflight consumers and worker shutdown implementation. Read-only
`git hash-object`/`git rev-parse HEAD:<path>` comparisons matched all six
candidate and base blob identities recorded in the diff.

No tests, scans, installations, agents, commits, source changes, or Page writes
were performed during this review. This report is the only intended write.
The existing focused log and explicit exit receipt show 52 passed in 55.14s,
exit 0; the existing scoped Ruff, mypy, and contracts exit receipts are 0.
The documented 16 generated pytest additions do not establish source drift:
the freeze report distinguishes them from the 6205 unchanged tracked/source/test
entries. These are implementer receipts, not a new reviewer test run.

## Strengths

- The semantic owner API is small and nominal. Recovery no longer evaluates
  Unix signal constants; the Linux owner still routes through its existing
  pidfd SIGTERM/SIGKILL method. The nominal test owner was deliberately updated.
- Acknowledgement still precedes graceful termination, and `_begin` retains
  the first reason and single escalation task. The forced-termination helper
  preserves the original supervision exception and a distinct force failure.
- Admission sealing uses the existing lock and retains a single join task.
  The unavailable outcome stays inside the existing failure envelope. Worker
  cancellation identity and previously consumed caller cancellation are
  handled deliberately instead of relying on shield's replacement exception.
- Tests drive the real coordinator and recovery producer. The real executor
  child has an explicit fresh lifecycle and an outer timeout; Linux watchdog
  and Uvicorn subprocess controls remain in the recorded focused selection.

## Findings

### R1 — Important: completed-task delivery breaks subsequent same-key admission

**Location:** `src/elspeth/web/execution/runtime_preflight.py:170`–172;
the failing lookup is at line 121, with `_inflight` eviction at lines 113–118.

`_delivered()` immediately removes the task's `_retained` entry, even when the
task is still present in `_inflight`. Task completion and execution of its
queued done callback are separate event-loop steps. In that interval,
`asyncio.shield(done_task)` and `wait_for(..., timeout=0.0)` can finish
synchronously. The first same-key caller gets the completed result and removes
the ownership record; a second caller before `_evict` runs indexes the missing
record and raises `KeyError` instead of receiving the same result.

This is directly adjacent to an existing producer control:
`test_exhausted_budget_still_joins_a_completed_worker` deliberately enters the
done-but-not-evicted interval. Repeat its immediate
`assert await coordinator.run(key, worker, timeout=0.0) is expected` once more,
with no intervening yield. The first assertion deletes `_retained[task]`; the
second reaches line 121 while `_inflight[key]` still contains that task.
Alternatively, a worker can wake two retry callers with an Event immediately
before returning; those callers are queued before its completion callbacks.
This reasoning is from the source and asyncio scheduling contract, not a claim
that the reviewer executed the regression.

**Impact:** concurrent/repeated callers can receive an internal `KeyError`
instead of the successful or ordinary failed preflight outcome. This regresses
the existing single-flight contract even for callers that never seal.

**Remedy:** keep admission lookup and retained ownership consistent. A record
must remain available while `_inflight` can expose its task; release it only
after eviction and the existing waiter obligations are settled, unless its
undelivered failure still needs observation. Centralizing that small release
condition is preferable to tolerating a missing record at line 121. Include
success and ordinary-failure versions of the two-immediate-retry producer test.
The same release condition should preserve records with pending awaiters for
the seal snapshot; success observation currently also removes records before
their awaiter count reaches zero.

### R2 — Important: abandoned ordinary failures accumulate for the process lifetime

**Location:** `src/elspeth/web/execution/runtime_preflight.py:155`–168 and
198–208. Actual long-lived owner: `src/elspeth/web/app.py:2299`–2309;
actual timeout-producing caller: `src/elspeth/web/composer/composer_preflight.py:243`–248.

Whenever all callers time out or cancel and the worker later raises an
ordinary `Exception`, `_observe()` stores its original exception and leaves the
completed task in `_retained`. No later same-key retry can discharge that
record after eviction: it starts a different task. There is no bound, draining
observer, or completed-failure release path. The real Web coordinator is a
singleton, and this slice deliberately leaves its lifecycle integration
unchanged; it has no production `seal_and_join()` call. Consequently every such
failure remains reachable for the entire application lifetime. Even eventual
sealing does not bound retention during normal service.

**Trigger:** repeated requests exhaust their remaining preflight budgets or
disconnect, and the retained workers subsequently fail validation, plugin
construction, or admission. Repeating that otherwise ordinary request pattern
adds one completed task, exception, and traceback graph per request. Worker
pool capacity does not bound the historical collection because these workers
have already finished and released their capacity.

**Impact:** memory grows with the lifetime total of abandoned failures rather
than active work. Tracebacks can retain request state and validation/plugin
locals. This introduces a production leak in an existing caller that never
seals, despite the old eviction mechanism explicitly preventing lifetime
retention. The implementation report acknowledges retention until lifecycle
end, but that does not make it bounded for a long-running server.

**Remedy:** establish a bounded, explicit observation policy for abandoned
ordinary failures. For example, report such an outcome once through an owned
error sink after its last waiter settles, then release the completed task;
retain only the cleanup outcome the lifecycle actually needs, with a documented
bounded overflow policy if multiple originals must be kept. Do not silently
drop failures or treat an already delivered envelope as cleanup failure. A
small sequential producer test should demonstrate that many abandoned,
completed failures are observed and do not leave an ever-growing task/traceback
registry. This can be fixed within the coordinator; it does not require adding
the deferred Web/MCP lifecycle integration or a general task framework.

## Considered but declined behaviors

- Public MCP supervision/stdio absent-owner repair: explicitly deferred to
  later serial slices; this candidate does not claim to fix it.
- Native Windows/macOS/BSD qualification: outside this Linux review; the
  semantic fake proves dispatch behavior, not native process semantics.
- A new coordinator-enforced worker deadline: intentionally omitted. The
  coordinator must await admitted work; independent supervision and actual
  executor shutdown own termination and thread settlement.
- Arbitrary cancellation of private join tasks by host-wide task sweeps:
  not an exposed producer operation, and the approved owner must not use such
  sweeps. No speculative finding is raised from private-state interference.
- Automatic forced termination after a graceful request itself raises:
  no new regression is established here; the grace is already acknowledged,
  the exception remains visible, and the independent deadline remains armed.
- External subclasses implementing only the former abstract signal method:
  the approved semantic interface intentionally changes that implementation
  contract, and the report identifies it; owned implementations were updated.
- A separate premature-join finding solely from a missing success record:
  the release invariant is addressed under R1. I did not execute a scheduling
  proof of a distinct externally observable early return and am not inflating
  it into another independent finding.
- Broad/static corpus gates and the three long Landscape proofs: parent-owned
  pending verification, not silently claimed green or treated as a new code
  defect. No signature/binding refresh is requested by this review.

## Verdicts

**Specification compliance: changes required.** The semantic termination
foundation satisfies the scoped design on inspection, and most cancellation,
failure-observation, and sealing behavior is implemented. R1 violates the
preserved single-flight/error contract; R2 leaves custody unbounded in the
actual long-lived existing owner.

**Code quality: changes required.** The implementation stays focused and its
producer tests are useful, but ownership removal must follow a consistent
release invariant and historical abandoned failures need a bounded observation
policy. Resolve R1 and R2 with focused regressions and rereview before the
dependent supervisor/integration slice. No critical or minor-only findings.
