# Lifecycle foundations fix1 scoped rereview

Reviewed R1/R2 against the original review, the parent observation-policy
ruling at `progress.md:244`, the appended fix1 implementation report, the
complete fix delta, and both amended files in full. Candidate remains
`/workspace/elspeth/.claude/worktrees/composer-remediation-lifecycle-foundations`
at base/HEAD `f9c23c59c63bbd3099029e995a778bade594f6c8`.

Verified artifact hashes:

- Fix delta: `61e27c6c0dc7292bdc313ba6fb098ef791a50d65bd1ae1d7b2d0a2669fab6232`.
- Complete final diff: `ac17c62a32342cda4c2c46a6bd6481a4d72ab4bd006aafc4e2ad0cf0fe524ef5`.

Read-only blob comparisons matched all six final files and their HEAD bases
to the complete final artifact. The fix1 before/after freeze manifest bytes
are equal. Read the final focused log and explicit exit receipt: 57 passed,
3 deselected in 7.25s, exit 0. Scoped Ruff/format/mypy/contracts receipts are
all 0. No tests or scans were rerun by this reviewer; no source/index/commit
changes, installations, delegation, or Page writes were performed. This report
is the only intended write.

## Original findings

**R1: ADDRESSED.** `_release()` now requires observation, eviction, and zero
remaining awaiters. `_delivered()` no longer removes a record that `_inflight`
can expose. The success/failure producer regression performs two immediate
zero-budget retries in the completed-but-not-evicted window, checks identical
results, and verifies custody remains available for the original waiter.
Pending awaiters also remain visible to a seal snapshot.

**R2: ADDRESSED under the parent ruling.** Ordinary failure envelopes retain
only their class for reporting, are reported once when abandoned, and are
released after eviction and waiter settlement. They are not converted into
cleanup exceptions. Completed actual task failures outside an active join are
similarly reported and released. For tasks the join still owns, `join_owned`
prevents duplicate reporting and the retained snapshot preserves genuine task
failure/cancellation identities until settlement. The two sequential producer
cases exercise 25 abandoned workers each, assert empty registries during normal
service, check exact class-only reports, and use weak references to verify task
collection. Releasing the test's own timeout receipt is appropriate: its
traceback is caller custody, not coordinator history.

The implementation remains small: one release condition, one observation path,
and the existing join snapshot. It introduces no general task framework or
historical failure archive. The changed tests also pin late successful delivery
of a failure envelope, no duplicate reporting to patient waiters, and original
genuine task/caller cancellation identities through repeated cancellation.

## New breakage in this fix

### R3 — Important: abandoned-outcome reporting can corrupt MCP stdout

**Location:** `src/elspeth/web/execution/runtime_preflight.py:20` and
`:191`–193.

The new `structlog.get_logger()` uses structlog's default output when the host
has not configured logging. In the installed dependency, `_config.py:58`
selects `PrintLoggerFactory()`; `_output.py:55`–56 defaults its file to stdout,
and its `error` method uses `print(..., flush=True)`. The default processors
include a console renderer, so this emits a diagnostic line, not JSON-RPC.

This is an existing consumer condition, not a proposed supervisor feature:
`composer_mcp/server.py:1162` (`main`) and its `run_server` path do not configure
structlog or redirect diagnostics to stderr. The public `elspeth-composer`
entry point delegates directly to that main. The MCP SDK's `stdio_server`
wraps `sys.stdout.buffer` and writes protocol messages there; it does not
redirect ordinary prints. Existing standalone hosts of `create_server` also
need not initialize Web logging. The server itself currently uses stdlib
`logging.getLogger(__name__)`, whose unconfigured fallback is stderr.

**Trigger:** a standalone stdio host has a preflight request time out or cancel,
then its retained worker fails. `_release()` emits either new error event to
stdout. The existing `create_server` API accepts an asynchronous preflight
callable and a per-request timeout, so a worker completing with failure after
that budget is a concrete supported scenario independent of the deferred
public shared-executor owner repair.

**Impact:** the client receives a non-protocol line in its JSON-RPC stream,
causing a transport parse error and potentially losing the session. It also
breaks stdout cleanliness for any other unconfigured coordinator host. The
`capture_logs()` tests validate event contents but replace normal structlog
dispatch, so they cannot detect this routing error.

**Remedy:** use a small reporting sink with a stderr-safe unconfigured default
while preserving the existing configured Web logging behavior. A stdlib logger
is a natural local option because ELSPETH's configured logging already routes
stdlib records through its structlog formatter. Keep class-only evidence and
the current observation/release policy. Do not require global Web setup from
this transport-neutral coordinator or defer correctness to the future
supervisor. Add a fresh-child producer control without pytest/Web logging
configuration that abandons a failing coordinator worker and checks stdout is
empty and the diagnostic appears once on stderr. No new task framework or
public lifecycle integration is necessary for this repair.

This is a source-grounded finding: the coordinator, current MCP startup,
installed structlog defaults, and SDK stdio routing were inspected. The
reviewer did not run a reproduction.

## Considered but declined behaviors

- Re-raising ordinary abandoned envelopes at join: explicitly superseded by
  the parent ruling; reporting and release are the approved behavior.
- Replaying completed actual failures that were reported before seal:
  explicitly superseded by the same ruling; they are completed history.
- Retaining genuine failures in the single sealed join result for replay:
  finite custody of that join's obligations, not an accumulating service-life
  archive; no new memory finding.
- Callers retaining their own timeout exception tracebacks: caller ownership
  is expected and does not invalidate the coordinator release regression.
- Rerunning unchanged real watchdog/Uvicorn long cases: outside this fix-only
  verification; their original focused evidence remains applicable to the
  unchanged files. Broad gates and Landscape proofs remain parent-owned.
- Full portable MCP supervision and native OS qualification: still deferred;
  R3 concerns a newly introduced logging side effect in an existing consumer,
  so it is not deferred with those features.

## Verdicts

**Specification compliance: changes required.** R1 and R2 are addressed and
the revised observation policy is implemented correctly, but R3 violates the
existing stdio consumer's protocol boundary.

**Code quality: changes required.** The custody and memory fix is sound and
appropriately simple. Repair the reporting destination and add the fresh-child
routing regression before dependent implementation. No other new findings or
out-of-scope defects were identified in this scoped rereview.
