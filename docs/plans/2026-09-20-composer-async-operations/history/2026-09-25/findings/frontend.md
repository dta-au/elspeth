# Frontend seam: freeform send/recompose -> async operations

Measured on `release/0.8.1` tip `d479eb2b4` (main checkout, `src/` clean).
All paths are relative to `src/elspeth/web/frontend/src/` unless prefixed.
Spec: `docs/specs/2026-09-16-composer-async-operations-design.md` (freeform only;
guided routes stay synchronous and unchanged).

## 0. Corrections to the brief's framing (read first)

1. **`guidedOperationRetry.ts` uses `window.sessionStorage`, not localStorage**
   (`stores/guidedOperationRetry.ts:53-59`, `sessionStorageOrNull()`). Per-tab,
   survives reload in the same tab, invisible to a new tab.
2. **It persists a fingerprint only, never the request body.** Descriptor shape
   `{kind, sessionId, requestFingerprint, operationId, createdAt}` (`:11-17`);
   fingerprint is a 4-lane FNV-1a hash (`:196-213`); the body lives only in page
   memory (`:42-51` comment, `liveOperationIds` set at `:51`). Tests pin this as an
   invariant: `stores/guidedOperationRetry.test.ts:89` "stores only a fingerprint,
   never the raw request identity", `:147` "reuses one session fork operation
   without storing edited content", `:169` "...without storing the message".
   Spec §2 requires the SPA to persist "that ID and the strict request body
   locally until it observes a terminal result" and §5 requires reload-resume.
   **The existing module cannot be reused as-is — it is a shape to copy, not a
   store to extend.** Storing the freeform body is a deliberate reversal of that
   module's privacy invariant and needs to be stated in the plan.
3. **Reload orphans descriptors by design and `selectSession` sweeps them:**
   `clearOrphanedGuidedRetriesForSession` (`guidedOperationRetry.ts:352-366`)
   is called from `selectSession` at `stores/sessionStore.ts:1946`. For async
   freeform a persisted id after reload is exactly what must be resumed, so the
   freeform descriptor must NOT be subject to that sweep.
4. **Size bound mismatch:** `MAX_STORAGE_BYTES = 8192` (`guidedOperationRetry.ts:37`)
   vs `SendMessageRequest.content: str = pydantic.Field(min_length=1, max_length=65536)`
   (`src/elspeth/web/sessions/schemas.py:154`). A body-bearing envelope needs a
   different bound (and a defined behaviour when storage refuses the write).
5. **`recompose` sends no body today** (`api/client.ts:917-927`: POST with
   `Content-Type: application/json` header and no `body`), and the route takes no
   body model (`src/elspeth/web/sessions/routes/composer/compose.py:90-98`).
   "DTOs gain a required `operation_id`" means recompose gains a JSON body for
   the first time.
6. `guidedOperationRetry` is not guided-only: `session_fork` (freeform fork,
   `sessionStore.ts:3016`) and `state_revert` (`sessionStore.ts:4653`) use it. Do
   not delete or rename it while "removing guided"; the fork/revert kinds stay.

## 1. `api/client.ts` — the two freeform functions and error decoding

### Signatures (current)

```ts
// api/client.ts:896-913
export async function sendMessage(
  sessionId: string,
  content: string,
  stateId?: string,
  signal?: AbortSignal,
): Promise<MessageWithStateResponse>
// body: { content: string; state_id?: string }  (state_id only when truthy, :902-905)
// POST /api/sessions/${sessionId}/messages, headers authHeaders("application/json"), signal passed through

// api/client.ts:917-927
export async function recompose(
  sessionId: string,
  signal?: AbortSignal,
): Promise<MessageWithStateResponse>
// POST /api/sessions/${sessionId}/recompose, NO body
```

- Both return `parseResponse<MessageWithStateResponse>(response)` — an unchecked
  cast (`:912`, `:926`). There is **no decoder for `MessageWithStateResponse`,
  `ChatMessage`, or `CompositionProposal`** (grep for
  `decode(ChatMessage|CompositionProposal|MessageWithState)` returned nothing;
  control: the same grep form finds `decodeCompositionState` at
  `api/guidedDecoder.ts:2030`). `decodeCompositionState(value, path)` is
  exported and already used by `fetchCompositionState` (`client.ts:1186-1197`),
  so a poll-result decoder can reuse it for `state`.
- Transport: `authFetch` (`api/authSession.ts:14-23`) is a thin `fetch` wrapper
  that records the credential per Response (WeakMap) for the 401 logout guard.
  **No timeout** on either compose call; the only AbortSignal source is the
  caller (`useComposer` via `runComposeWithTimeout`). The only per-call timeout
  idiom in the client is `fetchSystemStatus` → `signal: AbortSignal.timeout(5000)`
  (`client.ts:601-607`) — the natural model for "AbortController only bounds
  individual short HTTP calls" (spec §5).
- Progress poll fetch: `fetchComposerProgress(sessionId)` (`client.ts:778-789`),
  GET `/api/sessions/${sessionId}/composer-progress`, no signal, no decoder.
  Messages fetch: `fetchMessages(sessionId)` (`client.ts:751-756`), no signal.

### Error decoding (`parseResponse`, `client.ts:243-490`)

- Throws a plain `ApiError` object (not an Error subclass) on `!response.ok`.
- 401 interceptor: `response.status === 401 && options.logoutOnUnauthorized !== false`
  → `useAuthStore.logout()` if `responseOwnsCredential` (`:247-256`).
- Field extraction reads from BOTH `body` and `body.detail` when detail is an
  object (`nestedDetail`, `:283-287`): `error_type` from
  `["error_type","error_code","refusal","code","kind"]` (`:289-292`), `current_state`,
  `storage_quota` (413 only), `sources`, `request_id`, `failure_code`, `reason`,
  `recovery_text`, `timeout_seconds` (finite >0 only, `:320-330`), `retry_after`,
  `component_id`, `plugin_id`, `snapshot_fingerprint`, `provider_detail`,
  `provider_status_code`, `fanout_guard`, `secret_guard`, `validation_errors`,
  `errors` (+ FastAPI `detail[]` fallback), `detail`, `partial_state`,
  `failed_turn`, `partial_state_save_failed`, `partial_state_save_error`.
- Only header read: `X-ELSPETH-Plugin-Snapshot` (`:482-484`, via
  `optionalResponseHeader` `:171-182`); consumed only by the plugin catalog
  (`stores/pluginCatalogStore.ts:231`), not by compose errors.
- `ApiError` type: `types/index.ts:1166-1227`.

**Implication for the terminal envelope (spec §2: `error` = `http_status`,
`error_type`, public response body):** the body-parsing half of `parseResponse`
must be extracted into a pure `(status, body) -> ApiError` function and reused
for the poll envelope, **without** the 401-logout side effect (an envelope is
not a credential rejection). client.recovery tests below are the pinning suite
for field parity (top-level and nested FastAPI forms).

### New client surface the plan must add (none exists today)

`grep -n "/operations" api/client.ts` → nothing. Needed: submit (202 ack
decode: `{operation_id, kind, status, poll_after_ms}`), poll
(`GET /api/sessions/{sid}/operations/{oid}` → `{kind, status, cancel_requested,
result|error}`), cancel (`POST .../operations/{oid}/cancel`, 202 with
`cancel_requested` or the terminal row). Closest existing decoder precedent for a
closed status union: `decodeGuidedStartOperationReconciliation`
(`api/guidedDecoder.ts:2297-2340`, exact-record + closed switch) and
`reconcileGuidedStartOperation` (`client.ts:1034-1049`).

Two decoding consequences the plan must state explicitly:
- A terminal **error** envelope arrives inside a **200** poll body, so
  `parseResponse`'s `!response.ok` branch never runs for it. Routing the poll
  through `parseResponse` alone would treat a failed operation as a success
  payload; the poll decoder must branch on `status`/`error` and call the
  extracted `(status, body) -> ApiError` function itself.
- Spec §2: an initial poll 404 "is not evidence that an in-flight POST cannot
  still commit". The poll client therefore wants the 404→`null` shape used by
  `fetchCompositionState` (`client.ts:1193-1195`), not a thrown ApiError that the
  store would render as a failure.

## 2. `types` — DTOs

```ts
// types/index.ts:336-340
export interface MessageWithStateResponse {
  message: ChatMessage;
  state: CompositionState | null;
  proposals: CompositionProposal[];
}
```
Re-exported from `types/api.ts:23`.

```ts
// types/index.ts:115-136 (ChatMessage, fields the reducers mutate)
  local_status?: "pending" | "failed";
  local_error?: string;
  local_failure_code?: string;   // e.g. "policy_blocked"; cleared wherever local_status is cleared
```

- `CompositionProposal`: `types/index.ts:318-334`.
- `ComposerProgressSnapshot`: `types/index.ts:395-413`, including
  `inflight_requests?: number` (`:412`) documented as "the post-abort resync's
  only settlement signal".
- `ComposerProgressReason` union (`types/index.ts:366-387`) includes
  `"client_cancelled"`.
- `ApiError`: `types/index.ts:1166-1227`. `failure_code` doc says "Closed
  guided-operation failure code" (`:1180-1183`) — freeform reuses it for
  `policy_blocked`.
- `SystemStatus.composer_timeout_seconds?: number` (`types/index.ts:1276`).
- `types/api.generated.ts` is referenced by the `generate-types` script but the
  hand-written re-export at `types/api.ts:1-5` says generation is not in use.

## 3. `stores/sessionStore.ts` — `sendMessage` (freeform send)

Store interface: `sendMessage: (content: string, signal?: AbortSignal) => Promise<void>` (`:1502`).
Implementation `:2174-2419`.

### Pre-flight (synchronous, before any await)
- `if (!activeSessionId) return;` (`:2176`).
- Admission gate `if (isComposing) return;` (`:2184`, elspeth-3f38ebb1b5).
- Captures `recoveryStartedCompositionVersion = compositionState?.version ?? null`
  (`:2185-2186`) and `baselineMessageIds` (`:2187`).
- **Optimistic user row** (`:2189-2197`): `id: \`local-${crypto.randomUUID()}\``,
  `role: "user"`, `content`, `tool_calls: null`, `created_at: now`,
  `local_status: "pending"`.
- `set` (`:2199-2207`): `isComposing: true`, `error: null`, `composerProgress: null`,
  `lastComposeChangedPipeline: null`, messages += optimistic.
- Starts both pollers and keeps their generations:
  `progressPollGeneration = startComposerProgressPolling(sid)` (`:2208-2209`),
  `inflightPollGeneration = startInflightMessagesPolling(sid)` (`:2210-2211`).

### Request
- `stateId = get().compositionState?.id` (`:2214`) is read **at send time from
  live store state** and rides the body as `state_id`. Under the spec's body
  hash this must be captured once and persisted with the operation; a replay
  that rebuilt it from a changed store would 409 (spec §2 mismatch).

### SUCCESS reducer — every side effect, in order
1. Session guard: `if (get().activeSessionId !== activeSessionId) return;` (`:2216-2218`).
   **Note:** this early return skips clearing `isComposing` (see §9 latent issue).
2. `await get().loadInflightMessages(sid, inflightPollGeneration)` (`:2226`) —
   canonical DB sync; drops the optimistic `local-*` row once a same
   role+content canonical row exists (`:2770-2779`).
3. Second session guard (`:2229-2231`).
4. `const { message, state } = result; const proposals = result.proposals ?? [];` (`:2232-2233`).
5. In one `set((s) => …)` (`:2234-2287`):
   - `versionChanged = newVersion !== null && newVersion !== previousVersion` (`:2235-2238`);
   - **validation reset:** `if (versionChanged) getExecutionStore().clearValidation();` (`:2242-2244`) — runs inside the set callback, before compositionState updates (R4-H3);
   - `newState = state ?? s.compositionState` (`:2247`) — a null `state` keeps the old one;
   - **selection:** `nodeStillExists` check; spread `{ selectedNodeId: null }` if the node vanished (`:2248-2250`, `:2285`);
   - **messages:** clear `local_status/local_error/local_failure_code` on the optimistic id; append `message` only if its id is not already present (`:2259-2271`);
   - `compositionState: newState` (`:2275`);
   - `lastComposeChangedPipeline: versionChanged` (`:2279`) — drives "Pipeline updated" vs "Response ready" badge;
   - **proposals:** `compositionProposals: mergeCompositionProposals(s.compositionProposals, proposals)` (`:2280-2283`; merge at `:1084-1098` never downgrades a non-pending to pending, and is a no-op for an empty list);
   - `isComposing: false` (`:2284`).
   - Does NOT clear `error` here (it was cleared at start, `:2201`).
6. Fire-and-forget: `useBlobStore.getState().loadBlobs(sid)` (`:2291`).
7. `void get().loadSessions()` (`:2297`) — picks up the server auto-title.
8. `void refreshInterpretationEventsForSession(sid)` (`:2302`) — invariant documented at `:724-751`; omitting it deadlocks freeform review cards.
9. `finally` (`:2407-2418`): stop both pollers by generation, then — only if this turn still owns the progress poller — one-shot `loadComposerProgress(sid, { ownerGeneration })`.

### ERROR path (`:2303-2406`)
Copy selection (`:2304-2334`):

| Condition | Copy |
|---|---|
| `isComposeAbort(err)` (abort DOMException or raw reason string, `:396-407`) | `composeAbortMessage(signal)` → `COMPOSE_CANCELLED_MESSAGE` if reason is `compose_user_cancel`, else `COMPOSE_TIMEOUT_MESSAGE` (`:181-184`, `:435-439`) |
| `status 422 && error_type "convergence"` | `formatConvergenceError` (`:208-225`): budget copy unless `reason === "convergence_wall_clock_timeout"`; names `timeout_seconds` only when present; saved-draft sentence only when `partial_state != null`; appends `recovery_text` |
| `502 && "llm_unavailable"` | `formatLlmUnavailableError` (`:694-696`) + provider diagnostic (`provider_detail`, `provider_status_code`, `formatProviderDiagnostic` `:683-692`) |
| `502 && "llm_auth_error"` | `formatLlmAuthError` (`:698-700`) + diagnostic |
| `error_type "audit_integrity_error"` (any status) | `formatAuditIntegrityError` (`:708-718`), names `request_id` when present |
| else | `apiErr.detail ?? "Failed to send message. Please try again."` |

State patch (`:2335-2382`):
- `auditIntegrityRefusal` (`:2339-2340`): the optimistic row is **un-failed** (status cleared) because the user row is committed before any audit raise (F-4b).
- otherwise optimistic row → `local_status: "failed"`, `local_error: errorMessage`, `local_failure_code` = `apiErr.failure_code` when a string (`:2346-2349`).
- `recoveryPatch`: `{ recoveryError, recoveryStartedCompositionVersion }` when `isComposerRecoveryError(apiErr)` = `partial_state != null && failed_turn != null` (`types/recovery.ts:22-26`) (`:2350-2355`).
- `partialStatePatch = convergencePartialStatePatch(apiErr, selectedNodeId)` (`:2361-2363`; helper `:240-260`): for `error_type === "convergence"` with a `partial_state`, sets `compositionState: partial`, `recoveryStartedCompositionVersion: partial.version`, and clears selection if needed. Applied after `recoveryPatch`.
- Session guard before set (`:2356-2358`).
- `isComposing: false`, `error: errorMessage`.

Post-error reconciliation:
- abort → `resyncAfterAbortedComposeTurn(sid, progressGen, inflightGen, preTurnVersion)` (`:2386-2395`; helper `:845-925`).
- `isAmbiguousComposeNetworkFailure(err)` (TypeError / `name === "NetworkError"` / `status === 0`, `:420-429`) → `resyncAfterAmbiguousComposeFailure(...)` (`:2396-2405`; helper `:939-1025`). This is today's "lost response" path: it waits for quiescence, reloads messages/state/proposals, and if it finds new durable evidence un-fails the row with "Your request was saved, but its response could not be confirmed…" (`:1016`). It never replays.

HTTP-status handling that is NOT special-cased for freeform today: 404, 409, 429,
503, generic 500. They render `detail`. 429's `retry_after` is parsed
(`client.ts:332-336`) but freeform ignores it (only `preferencesStore` uses it,
per the `ApiError.retry_after` doc at `types/index.ts:1210-1214`).

## 4. `stores/sessionStore.ts` — `retryMessage` (the ONLY caller of recompose)

Interface `retryMessage: (messageId: string, signal?: AbortSignal) => Promise<void>` (`:1538`).
Implementation `:2820-3007`. There is no other `api.recompose` caller
(grep `api\.recompose|api\.sendMessage` non-test: only `sessionStore.ts:2215`
and `:2853`).

Differences from `sendMessage` that a "one shared reducer" plan can get wrong:

| Aspect | sendMessage | retryMessage |
|---|---|---|
| Target row | new optimistic `local-*` row | existing persisted user row `messageId` flipped to `local_status: "pending"` (`:2835-2842`); refuses unless role is user (`:2829`) |
| Body | `{content, state_id?}` | none today |
| Success: `loadBlobs` | `:2291` | `:2915` |
| Success: `loadSessions` | `:2297` | **absent** (grep over `:2820-3008` finds none; the same grep finds `:2297`) |
| Success: interpretation refresh | `:2302` | `:2919` |
| Error: `audit_integrity_error` arm | `:2327`, un-fail at `:2339-2340` | **absent** — retry marks the row failed even for an audit-integrity refusal (`:2926-2932`, `:2960-2970`) |
| Error: copy precedence | 422-convergence, 502 unavailable, 502 auth, audit | 502 unavailable, 502 auth, 422 convergence (`:2926-2932`) |
| Ambiguous resync content | optimistic content | `message.content` of the retried row (`:2985-2993`) |

Everything else (versionChanged → `clearValidation`, selection clear,
`lastComposeChangedPipeline`, `mergeCompositionProposals`, `isComposing: false`,
recovery patch, partial-state patch, abort resync, finally poller stop +
one-shot progress load `:2995-3006`) mirrors sendMessage.

## 5. Stop button, client deadline, post-abort reconciliation

### Wiring
- `hooks/useComposer.ts:19-78`. `activeControllerRef` (`:30`);
  `sendMessage(content)` and `retryMessage(messageId)` pre-check
  `isComposing` then call `runComposeWithTimeout(activeControllerRef, composeTimeoutReady, runner)` (`:38-63`).
  `cancelComposition = () => activeControllerRef.current?.abort(COMPOSE_USER_CANCEL_ABORT_REASON)` (`:65-67`).
- `config/composer.ts:98-123` `runComposeWithTimeout`: returns without running if
  `!ready`; installs a fresh `AbortController` on the ref; `setTimeout(() =>
  controller.abort(COMPOSE_TIMEOUT_ABORT_REASON), getComposeTimeoutMs())`;
  clears timer and (identity-checked) ref in `finally`.
- **Shared with guided:** `ChatPanel.tsx:842-857` `sendGuidedChat` uses the same
  `runComposeWithTimeout` with its own `guidedChatControllerRef`, and
  `cancelGuidedChat` (`ChatPanel.tsx:944-946`) aborts it. Changing
  `runComposeWithTimeout` changes guided; the freeform cutover should stop
  *calling* it for freeform rather than altering it.
- Freeform UI: `ChatPanel.tsx:1003-1010` destructures `useComposer()`;
  `ChatInput onSend={handleSend} disabled={isComposing} onCancel={isComposing ? cancelComposition : undefined}` (`ChatPanel.tsx:3661-3664`).
  `ChatInput.tsx:642-650` renders the Stop button (`aria-label="Stop composing"`)
  only when `disabled && onCancel`.
- Other freeform senders: `handleSend` (`ChatPanel.tsx:2274-2283`), suggestion
  prompts (`:2068-2072`), paste-as-source (`:2105`), `:2145`, blob "use as input"
  (`:2297`), retry via `MessageBubble onRetry={turn.kind === "user" ? retryMessage : undefined}` (`:3512`),
  and `components/sidebar/SideRailValidationBanner.tsx:32` (`useComposer()`).
  All go through `useComposer`; nothing else calls the store actions directly
  (grep over non-test files for `useComposer\(|\.sendMessage\(|\.retryMessage\(`).

### Client compose deadline + grace (`config/composer.ts`)
- `COMPOSE_CLIENT_GRACE_MS = 25_000` (`:28`), `DEFAULT_COMPOSE_TIMEOUT_MS = 270_000 + grace` (`:29`).
- `getComposeTimeoutMs()` (`:45-47`); `applyServerComposerTimeout(seconds)` sets
  `round(seconds*1000) + grace`, ignoring non-finite/≤0 (`:60-72`).
- Boot latch: `App.tsx:401-411` applies `status.composer_timeout_seconds` and
  sets `sessionStore.setComposeTimeoutReady(true)` (setter `sessionStore.ts:1741-1743`);
  stuck-state branch `App.tsx:412-432`.
- Abort reasons: `COMPOSE_TIMEOUT_ABORT_REASON = "compose_timeout"`,
  `COMPOSE_USER_CANCEL_ABORT_REASON = "compose_user_cancel"` (`:128-129`).
- The header comment (`:1-27`) states the invariant "client ceiling sits ABOVE
  the backend wall clock plus grace so the 422 arrives first" and that Stop/
  timeout now stops server work because the server cancels on disconnect. Both
  premises change under async: the POST no longer carries the turn.

### Post-abort reconciliation (elspeth-06a23adfcc)
- `waitForCancelledComposeToSettle(sessionId, ownerGeneration)` (`:798-826`):
  loop every `ABORT_RESYNC_SETTLE_POLL_MS = 500` (`:759`) on
  `fetchComposerProgress(...).inflight_requests ?? 0`; exits on 0, on session
  change, on `composerProgressPollGeneration !== ownerGeneration`, or on fetch
  error. **No wall-clock bound, by design** (`:753-759`).
- `resyncAfterAbortedComposeTurn` (`:845-925`): wait → `loadInflightMessages` with
  the turn's inflight generation → GET state + proposals → `clearValidation` on
  version change, selection clear, replace proposals wholesale
  (`proposals ?? s.compositionProposals`), refine the banner with
  `stoppedComposeOutcomeMessage` (`:455-480`, counts saved versions) → loadBlobs,
  loadSessions, interpretation refresh.
- `resyncAfterAbortedGuidedTurn` (`:1037-1082`) uses the same wait for guided —
  must keep working unchanged.
- The server side of `inflight_requests` is a durable
  `composer_inflight_requests` row registered via
  `src/elspeth/web/coordination/composer_progress_authority.py:158` and mounted
  through `_track_compose_inflight` (`compose.py:68`, `:97`). **If the async
  worker does not register such a row, freeform turns stop contributing to the
  count once the dependency leaves the two routes, so the settle wait would
  return immediately and resync pre-turn state.** Spec §2 names the operation
  row as the sole settlement authority; freeform abort/ambiguous resync must be
  re-keyed onto the terminal poll, not on `inflight_requests`.

## 6. The two pollers (module-global, generation-fenced)

- Progress: `COMPOSER_PROGRESS_POLL_INTERVAL_MS = 1500` (`:172`). State
  `composerProgressPollTimer/SessionId/SeenNonTerminal/Generation` and read/applied
  tickets (`:529-561`). `startComposerProgressPolling(sid)` (`:2681-2695`) bumps
  generation, sets `composerProgress: null`, fires an immediate read with
  `discardStaleTerminal: true`, then `setInterval`. `stopComposerProgressPolling(sid?, gen?)`
  (`:2697-2711`). `loadComposerProgress(sid?, {discardStaleTerminal?, ownerGeneration?})`
  (`:2611-2679`) — interval-tick vs owner-read fencing, ticket ordering, stale
  terminal discard, `set({ composerProgress: phase === "idle" ? null : progress })`.
  Errors swallowed ("advisory").
- In-flight messages: `INFLIGHT_MESSAGES_POLL_INTERVAL_MS = 1500` (`:665`),
  `inflightMessagesLatestClaimBySession` map (`:673`) survives a stop so an
  owning turn's explicit sync still lands after A→B→A. `loadInflightMessages`
  (`:2713-2789`) fetches all messages, applies ownership + ticket fences, keeps
  `local-*` optimistic rows whose role+content is not yet in the fresh list
  (`:2770-2779`). `start…` `:2789-2802`, `stop…` `:2804-2818`.
- Both are also claimed by guided `chatGuided` (`sessionStore.ts:4469-4470`; also `:3483`),
  which is why the generation, not `isComposing`, is the mode-agnostic
  supersession signal (`:790-795`).
- Progress correlation contract in the view: "Freeform progress.request_id is
  the persisted user-message id" (`ChatPanel.tsx:1249-1270`); retirement of the
  terminal indicator depends on it. **The worker must keep publishing progress
  with `request_id` = user-message id, not the operation id.**
- `shouldShowComposerProgress = isComposing || (terminal && !retired)`
  (`ChatPanel.tsx:1292-1295`); `findActiveComposerMessage` picks the newest
  `local_status === "pending"` user row (`ChatPanel.tsx:3883-3897`).

Spec §2/§5: pollers "remain for UX but stop when the operation is terminal" and
"cannot declare success, failure, or quiescence". Today the pollers are stopped
in the action's `finally`; under async that `finally` must be reached only after
the operation poll reports terminal (not on 202).

## 7. Session switch and page reload (today)

- `selectSession(id)` (`:1936-2123`): `clearValidation()`, clears both poll
  timers (`:1939-1940`), sweeps orphan guided retries (`:1946`), advances the
  publication generation, and resets `isComposing: false`, `composerProgress:
  null`, `messages: []`, etc. (`:1949-1967`). It **does not abort** the
  in-flight POST. On A→B→A the old promise resolves while A is active again and
  its success reducer applies (the owner-generation sync is designed for this,
  `:2735-2742`); meanwhile `isComposing` is false so Send is enabled on A while
  the server turn is still running.
- Other `isComposing: false` reset sites: initial state `:1719`, `archiveSession`
  active branch `:1896`, `selectSession` 404 branch `:2104`,
  `resetForTutorialSession` `:2139`, `unbindMissingSession` `:2166`, fork `:3101`,
  `:3143`. `createSession` (`:1794-1868`) **does not** reset `isComposing`.
- `reset()` (`:4793-4808`, called on logout from `stores/authStore.ts:111`)
  clears pollers and `clearAllGuidedRetries()`.
- Reload: nothing is persisted for freeform; `isComposing` is in-memory only.
  `useAutoResumeSession` (`hooks/useAutoResumeSession.ts:20-51`) or the hash
  router (`hooks/useHashRouter.ts:193`) calls `selectSession`, which loads
  messages/state/proposals. The browser drops the POST on unload and the server
  cancels via `_cancel_on_client_disconnect`. There is no reattach.
- **Spec gap to resolve:** the spec defines only
  `GET /api/sessions/{sid}/operations/{oid}` — no per-session "active operation"
  lookup. Reattach after reload / session switch / a second tab is possible only
  when the browser still holds the id (sessionStorage is per-tab; a new tab has
  none). "A second action for the same session remains disabled while a job is
  nonterminal" therefore needs either a list/active endpoint or an accepted
  limitation, and on reload must be derived from the persisted descriptor, not
  from `isComposing`.

## 8. `guidedOperationRetry.ts` in full (pattern to copy)

Storage:
- Key `GUIDED_RETRY_STORAGE_KEY = "elspeth_guided_operation_retries_v2"` (`:30`),
  envelope `{"schema":"guided-operation-retries.v2","descriptors":[...]}` (`:31`, `:85-87`),
  **in `window.sessionStorage`** (`:53-59`).
- Validation on read: exact kind set, canonical session UUID
  (`SESSION_UUID_PATTERN`, `:33`), SHA-256-shaped fingerprint (`:34`), **v4**
  operation UUID (`UUID_PATTERN` `:32` requires version nibble 4 and variant 8-b —
  i.e. `crypto.randomUUID()` output), finite createdAt (`:61-83`). Any malformed
  envelope → drop all and remove the key (`:141-150`).
- Bounds: age ≤ 24 h (`:35`), ≤ 16 descriptors (`:36`), ≤ 8192 bytes, oldest
  evicted first (`:93-108`).
- Storage failure: in-memory `fallbackDescriptors` becomes authoritative
  (`fallbackAuthoritative`, `:39-40`, `:112-123`, `:185-193`). Clearing writes an
  empty tombstone AND removes the key, both attempted independently
  (`:161-183`).

Lifecycle:
- `acquireGuidedRetry(kind, sessionId, requestIdentity)` (`:242-285`): canonical
  JSON of `{schema:"guided-operation-request-fingerprint.v2", kind, requestIdentity}`
  (sorted keys, plain records only, finite numbers, `:215-240`) → fingerprint.
  Same kind+session+fingerprint → re-adopt the stored `operationId` and mark it
  live (same-action recovery, `:259-267`). Same kind+session, different
  fingerprint → `{status:"conflict", existing}` (`:268-270`). Else mint
  `crypto.randomUUID()`, persist, mark live.
- `isGuidedRetryReplayable(handle)` → in `liveOperationIds` (`:290-292`).
- `clearGuidedRetry(handle)` (`:294-307`), `findGuidedRetry(kind, sid)` (`:309-317`),
  `clearGuidedRetriesForSession(kind, sid)` (`:319-335`), `clearAllGuidedRetries()`
  (`:337-340`), `clearOrphanedGuidedRetriesForSession(sid)` (`:352-366`).
- `isAmbiguousGuidedRetryFailure(error)` (`:368-383`): TypeError, AbortError,
  TimeoutError, and 5xx are ambiguous (keep custody) EXCEPT
  `guided_operation_terminal_failure` and `server_invariant_violated`.
- Store-side conflict handling: `reconcileOrphanedGuidedRetry` (`sessionStore.ts:320-349`),
  `reconcileGuidedRetryConflict` (`:359-369`), conflict copy
  `GUIDED_RETRY_KIND_LABELS` (`:284-293`) + `guidedRetryConflictState` (`:295-307`).
- Canonical usage shape (state revert, freeform, `sessionStore.ts:4650-4714`):
  acquire → on conflict reconcile → on surviving conflict set copy and return →
  call API with `retry.operationId` → on success `clearGuidedRetry` → on error
  clear unless ambiguous. Guided chat equivalent: `:4432-4490`.
- Callers: `session_fork` `:3016`, `guided_start` `:3268`, `:4264`,
  `guided_convert` `:3326`, `guided_respond` `:3421` and
  `components/tutorial/TutorialGuidedShell.tsx:422`, `guided_reenter` `:4079`,
  `guided_chat` `:4432`, `state_revert` `:4653`.

Tests (`stores/guidedOperationRetry.test.ts`, `beforeEach` clears
`window.sessionStorage` and calls `clearAllGuidedRetries`, `:44-49`):
`:51` expires after 24 h (fake timers); `:63` caps count and bytes; `:74` drops
malformed storage; `:89` fingerprint only; `:97`, `:105` convert/plan custody;
`:114` finds guided-start custody after prompt is gone; `:126` ignores retired v1
key; `:147` fork without content; `:158` rehydrates after `vi.resetModules()`
(module reload); `:169` chat without message; `:180`, `:193` canonical key order;
`:211` rejects non-plain objects; `:217` clears one kind for a session; `:247`
conflict preserves prior custody; `:262` independent kinds; `:271`, `:283`,
`:302`, `:317`, `:333`, `:348` storage-failure fallbacks; `:366` rejects bad
session ids; `:375` ambiguity classifier.

What freeform needs that this module does not provide: persisted body (bounded
at ≥ 64 KiB content + state_id), reload-survivable replay custody (no orphan
sweep for freeform kinds), a by-session lookup to reattach, and an operation
status (queued/running/terminal) rather than an ambiguous/cleared binary.

## 9. Latent issues observed by reading (not measured)

- `createSession` does not reset `isComposing` (`:1794-1868`, no
  `isComposing` key; grep of `isComposing: false` lists no line in that range),
  and sendMessage/retryMessage return early on session change without clearing
  it (`:2216-2218`, `:2854-2856`). By reading, a compose in flight on A followed
  by "new session" leaves `isComposing: true` on the new session until another
  reset. Not confirmed by a test run.
- retryMessage has no `audit_integrity_error` arm (see §4); an audit-integrity
  refusal on retry marks the row failed.

## 10. Test harness and files that will change

Commands (`package.json`): `npm run typecheck` = `tsc -p tsconfig.app.json --noEmit && tsc -p tsconfig.oidc.json --noEmit`;
`npm test` = `vitest run`; `npm run lint` = eslint over `src` + listed e2e files;
`npm run lint:css` = stylelint; `npm run build` = `tsc -p tsconfig.app.json --noEmit && vite build`.
CI (`.github/workflows/ci.yaml`) runs only `npm run typecheck` (`:1320`) and
`npm test -- --run` (`:1326`) in the `frontend-unit` job, plus Playwright
`npm run test:e2e` (`:1267`). Instrument: `grep -nE "npm run (lint|build|lint:css|typecheck)"`
returns only the typecheck line — so lint and build are local-only gates.
`tsconfig.app.json` includes all of `src` (tests included); `tsconfig.test.json`
adds vitest globals for `src/**/*.test.*`.

Vitest config: `vite.config.ts:28-36` — `globals: true`, `environment: "jsdom"`,
setup `./src/test/setup.ts` and `./src/test/a11y/setup.ts`, include
`src/**/*.test.{ts,tsx}` and `tests/e2e/harness/**/*.test.ts`.
Store reset helper: `test/store-helpers.ts` `resetStore(store)` calls
`state.reset()` when defined.

### Client tests (real client over a fetch spy)
- `api/client.recovery.test.ts` — `vi.spyOn(globalThis, "fetch")` with hand-built
  `{ok,status,statusText,json}` objects (`:27-35`). Tests that call `sendMessage`
  and assert thrown ApiError fields: `:63` "preserves top-level composer recovery
  fields on ApiError", `:107` "preserves nested FastAPI recovery fields including
  null transcript_url", `:153` "preserves the convergence taxonomy and elapsed
  budget from a timeout 422 (R2-F9)", `:197` "drops a non-numeric
  timeout_seconds rather than rendering it", `:223` "keeps existing provider
  validation and fanout error parsing". These become envelope-decoder parity
  tests (same bodies, delivered as a poll `error`).
- `api/client.guided.test.ts` — same fetch-spy strategy (header comment `:4-11`);
  its "retry-safe mutations" block (`:1117-1215`) is the template for asserting
  `operation_id` in request bodies. Guided tests must not change.

### Store tests (`@/api/client` module-mocked)
- `stores/sessionStore.test.ts` — factory mock `:24-80+` (includes `sendMessage`,
  `recompose`); `beforeEach` `:241-277` does `vi.resetAllMocks()`,
  `window.sessionStorage.clear()`, `clearAllGuidedRetries()`, `resetStore`.
  Affected describes: "sendMessage optimistic insert" `:529` (tests `:530`–`:2924`,
  e.g. `:568` "clears local_status on successful response", `:595` "records whether
  the turn mutated the pipeline", `:657`/`:695` interpretation refresh after
  send/recompose, `:761`–`:873` convergence copy, `:908` audit-integrity F-4b,
  `:953`–`:1009` abort-reason mapping, `:1034`–`:1720` abort resync and settle-wait
  (`:1253` "waits for the cancelled turn's terminal progress before resyncing",
  `:1320` "keeps waiting past any wall-clock budget…"), `:1739` "reconciles a lost
  POST response from saved messages and state without resending", `:1791` "keeps
  the retry affordance when a lost POST response has no durable evidence",
  `:1875` "drops stale sendMessage responses after the active session changes",
  `:1939` / `:2010` poller lifetime, inflight/progress poll fences `:2157`, `:2569`);
  "freeform compose admission gate" `:2837` (`:2860`, `:2885`, `:2924`);
  "retryMessage abort handling" `:3832` (`:3833`, `:3906`, `:3939`, `:3980`, `:4014`).
- `stores/sessionStore.guided.test.ts` — mock factory lists `sendMessage`/`recompose`;
  must only gain new keys, no behavioural edits (guided unchanged).

### Hook / component tests
- `hooks/useComposer.test.ts` — replaces `sendMessage` on the store via
  `useSessionStore.setState` and watches the AbortSignal. Tests: `:26`, `:43`, `:54`
  ceiling derivation; `:63` "reads the ceiling at call time so a boot-applied
  server value governs the abort"; `:92` distinct abort reasons; `:96`/`:109`/`:119`
  readiness gate; `:137` "keeps Stop bound to the active compose when a raced
  entry is refused (elspeth-3f38ebb1b5)" — the only freeform Stop test.
- `config/composer.test.ts` — `:24`–`:48` ceiling derivation, `:61` describe
  "runComposeWithTimeout — the shared freeform/guided send primitive" (`:70`, `:83`).
  Shared with guided; keep.
- `components/chat/ChatPanel.test.tsx` — mocks `@/hooks/useComposer` wholesale
  (`:65-67`, `mockReturnValue` per test), so it has **no component-level
  freeform Stop test** to adapt; guided Stop tests (`:5614`) use `cancelGuidedChat`.
  Only a11y covers the button: `test/a11y/components.a11y.test.tsx:1472`
  (`getByRole("button", { name: "Stop composing" })`).
- `App.test.tsx:1608` "opens the recovery panel for recovery-shaped send
  failures", `:1635` "applies and discards recovery locally without compose
  retries" (spy `api.sendMessage` rejections; `:1662-1663` assert
  `sendMessage` called once and `recompose` never).
- `test/inlineSourceIntegration.test.tsx:430`, `:513` — spy on `sendMessage`
  returning `MessageWithStateResponse`.
- `components/sidebar/SideRailValidationBanner.test.tsx:335-390`,
  `stores/subscriptions.test.ts:245-354` — replace the store `sendMessage` with a
  `vi.fn`; unaffected if the store action signature holds.

### Module-mock factories that must gain the new client exports
Instrument: `grep -rlnE 'vi\.mock\(\s*["'"'"'](@/api/client|(\.\./)+api/client|\./api/client|\./client)["'"'"']'`
over `*.test.ts[x]` → 47 files; of those, 8 list `sendMessage:`/`recompose:` keys
explicitly. Controls: positive `App.test.tsx` (uses the relative `./api/client`
form, missed by an `@/`-only first pass) and `stores/sessionStore.test.ts`;
negative `api/client.interpretation.test.ts` (mocks the module, no send key).
The 8: `App.test.tsx`, `components/chat/ChatPanel.guidedStartRecovery.test.tsx`,
`components/chat/ChatPanel.test.tsx`, `components/common/CommandPalette.test.tsx`,
`components/common/commandRegister.test.tsx`, `stores/sessionStore.guided.test.ts`,
`stores/sessionStore.test.ts`, `test/inlineSourceIntegration.test.tsx`.
Factory mocks throw on access to an undeclared export, so any module that
imports the new functions at render/selection time needs them declared.

### Playwright e2e
- `tests/e2e/composer-proposals.spec.ts:184-200` fulfils `POST /messages` with a
  synchronous `MessageWithStateResponse` (test `:268` "explicit approve tool call
  is visible before commit") — must become 202 + poll.
- `tests/e2e/composer-proposals.spec.ts:163`, `tests/e2e/guided-collector.spec.ts:547`,
  `tests/e2e/tutorial.spec.ts:652` mock `composer-progress`.
- `tests/e2e/harness/classify.ts:42` documents `compose` as
  "POST /api/sessions/{id}/messages — the compose (turn 2)" for the staging
  tutorial classifier; the tutorial uses guided routes, so this comment may be
  stale — check before editing.
