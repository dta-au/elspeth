---
title: Move long composer turns off the synchronous request so no proxy can cut a compose
labels: [area/composer, area/deployment, type/task]
---

A composer turn can run for minutes inside a single buffered HTTP request that puts zero
bytes on the wire until it finishes. Any intermediary on the path that closes idle
connections — a CDN edge, a corporate inspecting proxy, a load balancer — cuts it first, and
the user sees a generic send failure for work the server may have completed.

## What happens

The browser posts a message and waits. The server holds the request open for the whole
provider loop and writes nothing to the socket, so an intermediary counting idle time closes
it. Two instances have been measured: a CDN edge cutting at 125 s, and a TLS-inspecting
corporate proxy cutting at roughly 60 s. The browser application then shows
"Failed to send message. Please try again."
(`src/elspeth/web/frontend/src/stores/sessionStore.ts:2310` on send, `:2864` on
retry/recompose) while the turn may still be running at the origin.

The timeout belongs to a hop the deployment cannot always enumerate — a proxy installed on
the client's own machine is invisible to the server — so no declared request ceiling can be
guaranteed correct.

## Where the work starts

The composer's HTTP surface is `src/elspeth/web/sessions/routes/`; the progress contract the
browser already polls is `src/elspeth/web/composer/progress.py`; the browser state store is
`src/elspeth/web/frontend/src/stores/sessionStore.ts`. Three routes carry the same buffered
long-turn shape and the same `_track_compose_inflight` dependency, so a fix covering one will
be re-argued twice:

| Route | Anchor |
|---|---|
| `POST /{session_id}/messages` | `src/elspeth/web/sessions/routes/messages.py:112` |
| `POST /{session_id}/recompose` | `src/elspeth/web/sessions/routes/composer/compose.py:87` |
| `POST /{session_id}/guided/plan` | `src/elspeth/web/sessions/routes/composer/guided_plan.py:333` |

## Why not a keepalive heartbeat

This has been tried and abandoned; the reason is recorded so it is not rebuilt. The prototype
relocated the handler body into a child task and streamed whitespace heartbeats from the
parent, carrying the real outcome in a deferred body envelope. It broke five tests under
`tests/unit/web/sessions/`, structurally rather than by accident.

`_track_compose_inflight` captures `asyncio.current_task()` as the request owner
(`src/elspeth/web/sessions/routes/_helpers.py:2496`) and its renewal loop can cancel that
task; the test file states this dependency explicitly
(`tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py:11`). The guided plan route
reads `asyncio.current_task().cancelling()` to tell a client disconnect from a server fault —
in a closure nested inside the handler (`guided_plan.py:758`) and in a terminal-publish helper
the route awaits (`:249`). Both run in whatever task is serving the request.

`Task.cancelling()` is a per-task counter, not a property of the request. Move the handler
into a child and cancel the parent, and the child's counter reads 0 while an ancestor tears
it down — so every `cancelling() > 0` branch silently takes the wrong path. No exception, no
type error, just a cancellation reclassified.

**Constraint for any design:** a wrapper must not change which task the handler's own
`current_task()` returns. That rules out "handler in a child, heartbeat in the parent" at the
route layer and at the ASGI (server interface) layer alike. A heartbeat also commits the HTTP
status before the outcome is known and pins a minutes-long connection to one instance, against
the no-affinity routing direction recorded in
`docs/plans/2026-09-13-kubernetes-and-identity/K7-no-affinity-routing.md`.

## Proposed shape: accept with 202, report through the existing poll

Return `202 Accepted` as soon as the request is admitted, run the turn in a server-owned task,
and let the browser's existing polling carry the outcome. Measured on `release/0.8.1` at
`8f4384936`, most of the machinery already exists:

- The browser already polls twice per turn — `startComposerProgressPolling` and
  `startInflightMessagesPolling` both start on send (`sessionStore.ts:2188`, `:2190`), and
  `loadInflightMessages` (`:2653`) already merges server-authoritative messages with local
  optimistic ones mid-turn.
- The progress contract already carries a richer failure taxonomy than the HTTP status:
  `convergence_progress_event` (`progress.py:339`) publishes `phase="failed"` with `reason` in
  {`convergence_wall_clock_timeout`, `convergence_discovery_budget`,
  `convergence_composition_budget`} plus `headline`, `evidence` and `likely_next`;
  `phase="cancelled"` / `reason="client_cancelled"` lives at `progress.py:406-412`.
- `ComposerProgressSnapshot.inflight_requests` (`progress.py:57-70`) is already the browser's
  only post-abort settlement signal, because phase alone cannot distinguish
  aborted-but-running from quiescent.
- `ComposerRequestLease` (`progress.py:48-54`) and `renew_request` (`progress.py:106`) already
  exist, as does durable multi-instance progress authority.

Admission-time failures keep their real status because they fire before the 202; turn-time
outcomes move to the progress contract. Nothing wraps the handler, so the `current_task()`
problem does not arise.

## What has to be decided first

1. **Cancel-on-disconnect becomes lease expiry.** With no socket, "the client went away"
   becomes "the lease was not renewed"; TTL versus poll interval is undecided.
2. **Reason coverage.** `convergence_*` and `client_cancelled` have progress reasons today.
   Whether `llm_unavailable`, `llm_auth_error`, `audit_integrity_error` and `policy_blocked`
   do must be measured, not assumed.
3. **Idempotency.** A retried POST after a lost 202 must not start a second turn; the
   per-session compose lock plus `inflight_requests` is the basis.
4. **Browser discriminators** move from HTTP status to progress `reason`, and the two failure
   fallbacks above converge on one path.

## Done looks like

- A turn that outlasts any idle timeout on the path still reports its real outcome, including
  when the connection is closed mid-turn. The test for this closes the connection deliberately
  rather than waiting on a real network.
- Every one of the 16 `raise HTTPException` sites in `messages.py` maps to either a pre-202
  status or a named terminal progress reason, pinned by a test so a new raise site cannot be
  added without a mapping.
- A duplicate POST for the same turn does not start a second compose.
- No route wraps the handler in a way that changes which task `current_task()` returns.

## Size

Large, and design-first — do not start with code. Item 2 above is a good self-contained first
task for someone new: enumerate the raise sites in `messages.py`, map each to a progress
reason or an admission-time status, and publish the table. That answer is needed before the
rest can be designed, and produces something another person can act on.
