# Related polling investigation

Investigated release commit `fe01ad94b254598c3b870d032720ef3d85403c6e`
using `superpowers:systematic-debugging`: trace the boundary, compare a working
path, state a hypothesis, and reproduce with controlled interleavings before
recommending a correction. This is an investigation, not an implementation.
Production code was not changed.

## 1. A database-error WebSocket closure can silently stop run updates

**Priority: P2. Confirmed transport behavior; component-lifetime impact traced
in source.**

The durable run poller closes with code `1011` after a SQLAlchemy error
(`src/elspeth/web/execution/routes.py:2154`). The frontend's `1011` branch
sets `closed` and returns without reconnecting or invoking `onDisconnected`
(`src/elspeth/web/frontend/src/api/websocket.ts:204`). A premature `1000`
closure has the same notification gap.

REST recovery belongs to the three-second interval in `InlineRunResults`
(`src/elspeth/web/frontend/src/components/execution/InlineRunResults.tsx:295`).
That component unmounts when the Run tab is not selected. The execution store
can therefore retain running progress and `wsDisconnected=false`, leaving the
live indicator stale until another refresh or Run-tab visit. The existing
comment at `src/elspeth/web/frontend/src/stores/executionStore.ts:886`
acknowledges the off-tab fallback gap.

The actual WebSocket module, transpiled unchanged and supplied controlled socket
and timer boundaries, produced:

```text
1006: onConnected, onDisconnected; one reconnect timer
1011: onConnected only; zero reconnect timers
1000: onConnected only; zero reconnect timers
4004: onConnected, onRunUnavailable; zero reconnect timers
```

The `1006` and `4004` controls distinguish recovery from intentional terminal
refusal. Reproduction: `/tmp/polling-stream-repro.mjs`, exit 0. Five existing
backend disconnect/durable-progress tests also passed; they do not cover this
frontend recovery gap. No live-browser or Azure reproduction was performed.

**Correction direction:** give active-run recovery an owner that survives tab
changes and notify the store about premature closure. Preserve terminal
authorization refusals and distinguish transient errors from integrity failures
before deciding to reconnect.

## 2. Retired Composer progress requests can overwrite newer state

**Priority: P2. Confirmed with the real frontend store.**

`loadComposerProgress` checks only the active session after awaiting HTTP
(`src/elspeth/web/frontend/src/stores/sessionStore.ts:2586`). The poller has a
generation, but this read does not capture or validate it. Stopping the interval
does not stop a request already in flight.

Controlled reproductions showed:

- An old `using_tools` response arriving after stop and the explicit terminal
  refresh replaces the completed snapshot.
- An old response from one turn replaces a newer turn's progress on the same
  session after the poller restarts.
- A response for a genuinely different active session is correctly discarded
  (control).

The adjacent `loadInflightMessages` provides the working lifetime/generation
pattern; its stopped-poller control correctly preserves final messages.

**Correction direction:** bind interval reads and explicit post-settlement
reads to their owning turn, and reject responses from retired owners.

## 3. Overlapping polls can apply responses in reverse order

**Priority: P2. Confirmed with the real frontend store.**

Both interval callbacks launch async reads without waiting for the previous
read (`sessionStore.ts:2628` and `:2728`). Generation checks alone do not order
responses from the same generation.

With controlled deferred responses and fake timers, the second response updated
progress first, then the delayed first response replaced it with older progress.
The equivalent message test added an assistant message, then erased it when an
earlier empty response arrived. The probes also observed another request being
launched while the first remained pending. Backend load impact was not measured.

Findings 2 and 3 have different conditions: retiring a poller versus overlapping
reads within its lifetime. The six frontend reproduction/control tests all
passed by asserting these observed behaviors, not by asserting correctness:

```text
vitest run --config /tmp/polling-audit.vitest.config.mjs
1 test file, 6 tests passed; exit 0
```

Probe source: `/tmp/polling-audit.test.ts`; log: `/tmp/polling-audit.log`.

**Correction direction:** serialize periodic reads or enforce response ordering,
including interactions with explicit refreshes. Retain the existing ownership
checks rather than replacing them with response ordering alone.

## 4. Access changes between polling checks become HTTP 500

**Priority: P2. Confirmed on PostgreSQL. Authorization remains fail closed.**

Authentication and initial ownership can succeed before revocation or archival
commits. The progress authority correctly checks the new committed state and
raises `PermissionError`, but the routes do not translate that denial:

- `src/elspeth/web/sessions/routes/composer/state.py:531`
- `src/elspeth/web/sessions/routes/sessions.py:875`
- `src/elspeth/web/coordination/composer_progress_authority.py:402`

The production `OSError` handler rethrows this errno-less exception
(`src/elspeth/web/app.py:2039`). The result is an internal-server response for
an expected concurrent access change, not a data leak or another deadlock.

A SQLAlchemy event barrier committed the change immediately before the
authority's first identity read, after the earlier route check. It used real
PostgreSQL, SessionService, authority/registry, and production routes; an
authentication override represented the already-authenticated identity.

```text
progress, unchanged access: HTTP 200
progress, identity disabled: HTTP 500, identity PermissionError
progress, session archived: HTTP 500, ownership PermissionError
active list, unchanged access: HTTP 200
active list, identity disabled: HTTP 500, identity PermissionError
5 reproduction/control tests passed; exit 0
```

Probe: `/tmp/test_polling_permission_race.py`.
Log: `/tmp/polling-permission-race-final.log`.
This uses the ordinary FastAPI error boundary plus inspection of production
exception registration; it is not full deployment-stack acceptance.

**Correction direction:** translate the specific authority denial to the
appropriate non-disclosing access response at the route boundary. Keep the
fresh authorization check and preserve unexpected database-error telemetry.

## 5. Cancellation can discard queued request cleanup

**Priority: P2. Confirmed on PostgreSQL; requires cancellation while cleanup is
queued behind occupied workers.**

`DatabaseComposerProgressRegistry.finish_request` delegates directly to
`run_sync_in_worker` (`composer_progress_authority.py:499`). Ordinary worker
submissions are cancelled if the caller abandons them before execution
(`src/elspeth/web/async_workers.py:263`). The lifecycle dependency awaits this
cleanup without shielding it (`src/elspeth/web/sessions/routes/_helpers.py:2638`).
Consequently, cancellation at this boundary can discard the request's lease
deletion altogether. The heartbeat has already been stopped.

The working comparison is `start_request`'s cancellation handling: it shields
and joins admission and exact-token cleanup, including repeated cancellation.
Ordinary worker cancellation is intentional; cleanup uses that policy where it
needs a stronger lifetime guarantee.

The real PostgreSQL probe admitted a lease, occupied a one-thread test executor,
queued `finish_request`, then cancelled its caller. After releasing the worker,
the real progress read still counted one request. The unchanged control counted
zero. Explicit real cleanup cleared the remaining token:

```text
cancel=False: durable_poll_inflight=0
cancel=True: durable_poll_inflight=1
explicit cleanup afterward: durable_poll_inflight=0
2 reproduction/control tests passed; exit 0
```

Only worker capacity was constrained; the authority, registry, lease, and reads
were real. Probe: `/tmp/test_polling_cleanup_pg.py`; log:
`/tmp/polling-cleanup-pg.log`. The earlier executor-only probe also distinguished
one cleanup invocation from zero. Its first attempt hung in the diagnostic's
own default-executor shutdown; replacing that probe-only shutdown with a direct
join resolved it. That instrument issue is not an application finding.

This can delay post-abort reconciliation while progress still reports live
work. The count's expiry filter bounds stale lease visibility; the probe did not
wait for expiry or reproduce a complete browser cancellation flow.

**Correction direction:** preserve and observe exact-token teardown across
caller cancellation, using the existing admission-cleanup pattern. Keep
ordinary abandoned work cancellable and retain lease expiry as crash recovery.

## Scope and remaining hypotheses

- No residual identity/session lock inversion was found in the inspected
  durable run-progress reader. It uses ordinary reads in a repeatable-read
  snapshot. This is a bounded inspection, not a global proof.
- Active-session progress uses a count query per owned session; durable run
  polling also validates event history. Their load/pool impact was not measured,
  so neither is reported as a confirmed performance defect.
- Post-abort reconciliation polls serially while the server reports inflight
  work. It has no overall timeout and exits on zero, owner/session change, or
  fetch error. Finding 5 confirms stale durable inflight state; its end-to-end
  browser delay was not measured.
- Existing tracker issue `elspeth-90f453d7b2` describes an older message-poller
  lifetime defect. Its stopped-message case is now a passing control; it does
  not establish that the progress-generation and same-generation ordering
  defects above are fixed.
- The code-map refresh was unavailable/failed; a fresh refresh was requested.
  Findings rely on current source and executable probes, not stale map output.
- No broad suite, live Azure acceptance, production edit, merge, or deployment
  was performed for this investigation. Front Door's 240-second origin timeout
  remains separate.
