# New Composer UI — initial handover pack

This pack is for a team building a new user interface against ELSPETH's
existing backend. It reflects the current `release/0.8.1` checkout. It gives
a starting route through the product, the main wire contracts, and the cases
that need care. Build a thin slice, bring concrete gaps back to the ELSPETH
team, and we will improve the backend contract while continuing to own the
engine, Composer, validation, execution, and audit behavior.

## The shape of the integration

The UI is an authenticated client of the HTTP API and the run-progress
WebSocket. The backend owns pipeline authoring by the LLM, graph validation,
execution admission, run accounting, and audit evidence. The UI presents
those results, collects user decisions, and handles uncertain or delayed
responses. It must not construct a pipeline in place of the Composer planner
or give the tutorial a separate authoring path; both are [product
invariants](../../AGENTS.md#composer-invariants-non-negotiable).

A replacement SPA can be served from the same origin as the API. A separately
hosted browser UI needs configured [CORS
origins](../../src/elspeth/web/config.py) and an agreed authentication/SSO
return path. The current SPA uses relative URLs, browser `localStorage` for
its Bearer token, and the page's host for WebSocket connections; these are
[current-client assumptions](../../src/elspeth/web/frontend/src/api/client.ts),
not requirements of the backend protocol.

The backend has a live FastAPI `/openapi.json` document. Its version is
currently the app's `0.1.0`, and the repository's `generate-types` script has
no committed generated output. The [application API seam
design](../specs/2026-09-08-application-api-seam-design.md) describes a
versioned, snapshotted contract, but that work is not yet present. Treat the
current routes and response models as the source of truth while we help turn
the useful parts of that design into a dependable consumer contract.

## Build a first vertical slice

1. **Connect and sign in.** Read `GET /api/auth/config`, use the deployment's
   login flow, and send `Authorization: Bearer <access_token>` on protected
   HTTP requests. Handle `401` as an authentication event. If hosting on a
   second origin, agree the CORS and SSO configuration with us early.
2. **Create or select a session.** Use `POST /api/sessions` and
   `GET /api/sessions`. Load its messages, current composition state, pending
   proposals, and Composer preferences when selected.
3. **Send one freeform turn.** Generate a stable `client_request_id` UUID and
   send `{content, client_request_id, state_id?}` to
   `POST /api/sessions/{sid}/messages`. Render the returned `message`,
   `state`, and `proposals`. A null `state` means the turn created no new
   graph version. Poll `/composer-progress` for status while the request is
   live; its text is a safe status summary, not model reasoning.
4. **Show and validate the graph.** Render the server's composition state.
   Call `POST /api/sessions/{sid}/validate?state_id={state_id}` and use
   `readiness.execution_ready` and its structured blockers for the Run
   affordance. The backend rechecks readiness at launch.
5. **Run and observe.** `POST /api/sessions/{sid}/execute` returns `202` and
   `{run_id}`. Mint a one-use WebSocket ticket, stream progress, and load the
   run list/results. Reloading the page should recover a live run from
   `GET /api/sessions/{sid}/runs` and attach to it again.

That slice proves authentication, a Composer request, state projection,
validation, launch, and recovery across a page reload. Add guided authoring,
proposal decisions, catalog and blob workflows, interpretation review,
approvals, diagnostics, and output downloads as separate slices. The existing
[UI client](../../src/elspeth/web/frontend/src/api/client.ts), [guided
decoder](../../src/elspeth/web/frontend/src/api/guidedDecoder.ts), and
[run-stream helper](../../src/elspeth/web/frontend/src/api/websocket.ts) are
useful executable examples. They are coupled to this browser app and should
not be treated as a published SDK.

## API map for the UI

`sid`, `state_id`, and `run_id` are server-side UUIDs. Session and run reads
check ownership. The [Composer tool API](../reference/composer-tools.md) is
the planner's interface, not an alternative UI authoring route.

| UI concern | Main API | Response or decision to retain |
| --- | --- | --- |
| Sessions and conversation | `GET/POST /api/sessions`; `GET/POST /api/sessions/{sid}/messages` | Session ID, message IDs, `client_request_id`, and the optional returned state and proposals. |
| Freeform retry and status | `POST /api/sessions/{sid}/recompose`; `GET /api/sessions/{sid}/composer-progress` | Retry only the selected persisted user message; use progress `phase`, `reason`, and `inflight_requests`. |
| State and proposals | `GET /api/sessions/{sid}/state`; `GET /api/sessions/{sid}/proposals?status=pending`; `POST .../accept` or `.../reject` | State `id` and `version`; proposal status and committed state. |
| Guided flow | `GET /api/sessions/{sid}/guided`; `POST .../guided/start`, `/respond`, `/chat`, `/convert`, `/reenter` | Replace the whole guided response view: `guided_session`, `next_turn`, `terminal`, `composition_state`. |
| Validation and launch | `POST /api/sessions/{sid}/validate`; `POST /api/sessions/{sid}/execute` | `readiness.execution_ready`, blockers, guard challenges, then `run_id`. |
| Run status and history | `GET /api/sessions/{sid}/runs`; `GET /api/runs/{run_id}`; `POST .../cancel`; `GET .../results` | Session-list rows and run-status/results rows have distinct shapes. Results require a terminal run. |
| Live run stream | `POST /api/runs/{run_id}/ws-ticket`; `WS /ws/runs/{run_id}?ticket=...&after_sequence=...` | A fresh one-use ticket per connection, durable sequence cursor, and terminal event. |
| Investigation and artifacts | `GET /api/runs/{run_id}/diagnostics`, `/outputs`, `/outputs/{artifact_id}/preview`, `/content` | Diagnostics are bounded; outputs list the full artifact manifest. Download bytes with Bearer auth. |

The [session routes](../../src/elspeth/web/sessions/routes/__init__.py) and
[schemas](../../src/elspeth/web/sessions/schemas.py), plus the [execution
routes](../../src/elspeth/web/execution/routes.py) and
[schemas](../../src/elspeth/web/execution/schemas.py), define the current
wire shapes. JSON refusals carry a `detail`; many have a structured
`error_type`, sometimes nested in `detail`. Branch on the structured value,
not wording in an error message.

## Things to watch for

| Situation | Client behavior | Where we can help |
| --- | --- | --- |
| A send times out, the tab closes, or the user presses Stop | The server may already have accepted the message. Keep the same `client_request_id`, refresh messages/state/proposals, and use `/recompose` only for the persisted user row that needs a retry. A client abort is not proof that server work stopped. | Make ambiguous outcomes easier to reconcile and provide concrete response fixtures. |
| A guided turn has an uncertain response | Reuse its `operation_id` for that exact action, keep the server's `turn_token`, and refresh the guided view. Guided start also has `/guided/start/{operation_id}/reconcile`. Do not advance a local wizard optimistically. | Clarify or extend reconciliation responses if a real case cannot be resolved. |
| The user switches sessions or an older request finishes late | Fence response publication by active session, request identity, and state version. Clear validation after a new composition version. | Expose an authoritative field when the client otherwise has to infer freshness. |
| Composer work is still in flight when the user presses Run | Keep Run unavailable until the compose request settles and the resulting state has been validated. A prior validation belongs to its earlier state version. | Supply a clearer server status or admission response if the UI cannot identify this condition reliably. |
| Run admission refuses a launch | Handle `409 run_already_active`, `422` validation/readiness blockers, and `428` secret or fan-out guard challenges. If both guards fire, resend all previously confirmed acknowledgement tokens on the next attempt. | Improve structured error shapes and resolve backend admission gaps. |
| The run stream disconnects or the page reloads | A ticket is single use; obtain another before reconnecting. Resume with `after_sequence`, deduplicate by `event_sequence`, and recover terminal status through REST. `error` events are nonterminal. Close codes `4001` and `4004` end retries; `4503` is retryable. | Supply protocol fixtures and repair stream or recovery defects. |
| The UI needs graph meaning or a missing field | Render the server-authored state and ask for a backend projection or API field. The current frontend contains some topology interpretation in `graphTopology.ts`; copying it would create another domain authority. | Add or correct server projections and parity fixtures. |
| A response does not match its client type | Validate the wire at the boundary and report the endpoint plus a redacted example. Today `getRunStatus` and `getRunResults` in the current client are typed as session-list `Run` even though their server shapes differ. | Correct the types, publish reliable schemas, and add contract checks. |

The existing [session store](../../src/elspeth/web/frontend/src/stores/sessionStore.ts)
and [execution store](../../src/elspeth/web/frontend/src/stores/executionStore.ts)
show why these cases matter. They contain retry custody, stale-response
fences, guard handling, WebSocket recovery, and run reconciliation. A new UI
can start smaller while preserving the same server authority.

## How we can work together

The UI team can own its interaction design, rendering, and client state. We
will keep ownership of backend behavior and help remove blockers encountered
in the vertical slices: missing projections, unclear errors, inconsistent
response types, authentication/origin setup, contract fixtures, and genuine
backend defects. A useful blocker report is one endpoint and request shape,
the redacted response or missing field, the user action it prevents, and the
expected UI behavior. That gives us a concrete backend change to make without
asking the UI to reconstruct engine rules.

We can strengthen the handoff incrementally: first fix the known run-response
typing mismatch, then publish checked response fixtures and a stable API
contract for the routes the new UI uses. The API seam design is a direction,
not a prerequisite to starting the first slice.

As a planning guide, a simple demonstrator is a matter of days; a dependable
core with retries and run recovery is likely weeks; broad parity with the
current guided and review workflows is a larger, multi-slice effort. Those
are estimates for one experienced frontend engineer, not a delivery promise.
