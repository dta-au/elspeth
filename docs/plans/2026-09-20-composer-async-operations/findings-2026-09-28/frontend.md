# Frontend seam re-survey: the SPA after guided removal

Measured on `release/0.8.1` at `1effedab2e0af7e09a5e8b30c66bc46ceaa130d5` (`git rev-parse HEAD` printed
exactly that; `git diff --stat 1effedab2 HEAD -- src/elspeth/web/frontend` is empty, so no anchor moved).
Read-only. Paths are relative to `src/elspeth/web/frontend/src/` unless prefixed.

Inputs: spec `docs/specs/2026-09-16-composer-async-operations-design.md` (its guided text at `:7-11`,
`:41-52` and `:205-217` is stale after `7001600fe`); `panel-2026-09-28/RULINGS.md` (rulings 1-5);
`panel-2026-09-28/RECOMMENDATION.md` (B′); `contract.md` (Frontend at `:423-447`); old
`findings/frontend.md` (tip `d479eb2b4`, used only as a map).

Commits that touched this seam's files between `d479eb2b4` and the tip:
`04c11a713`, `8630db9b8` (ingress receipts, epoch 69), `6202d545d`, `01d96af9a` (tutorial Build moved
to freeform), `7001600fe` (guided removed), `f3d37c1a7`.

---

## 1. CURRENT FACTS

### 1.1 Wire client (`api/client.ts`, 1856 lines)

```ts
// api/client.ts:952-972
export async function sendMessage(
  sessionId: string,
  content: string,
  clientRequestId: string,
  stateId?: string | null,
  signal?: AbortSignal,
): Promise<MessageWithStateResponse>
// body: { content, client_request_id, state_id? }; state_id is set whenever stateId !== undefined (:963-965)

// api/client.ts:977-989
export async function recompose(
  sessionId: string,
  expectedUserMessageId: string,
  signal?: AbortSignal,
): Promise<MessageWithStateResponse>
// body: { expected_user_message_id }  (:985). No state_id. No client id.
```

- Both return `parseResponse<MessageWithStateResponse>` as an unchecked cast (`:971`, `:988`).
- **`state_id` is sent as an explicit `null`, not omitted, when the session has no state.** The store
  always passes a defined value, `string | null` (`stores/sessionStore.ts:1499-1501`: `?? null`), and
  the client includes the key whenever `stateId !== undefined` (`client.ts:963-965`).
- Backend wire truth: `SendMessageRequest(_RequestModel)` with `content: str (1..65536)`,
  `state_id: UUID | None = None`, `client_request_id: UUID` (`src/elspeth/web/sessions/schemas.py:143-159`).
  `RecomposeRequest(_RequestModel)` with only `expected_user_message_id: UUID` (`schemas.py:162-165`).
  The transcript row DTO exposes `client_request_id: str | None` (`schemas.py:216`).
- `parseResponse<T>(response, options)` (`client.ts:290`) throws a plain `ApiError`. The 401 logout runs
  on `status === 401 && options.logoutOnUnauthorized !== false` (`:294-303`). It extracts from both
  `body` and a nested object `body.detail`. The fields include `error_type` (`:340-343`),
  `client_request_id` and `user_message_id` (`:364-365`, emitted at `:518-519`), `failure_code`
  (`:366`), `reason`, `recovery_text`, `timeout_seconds` (finite and >0 only), `retry_after`,
  `partial_state`, `failed_turn`, provider fields, and `validation_errors`/`errors`.
- `ApiError` (`types/index.ts:1165`) carries `client_request_id?` (`:1179`), `user_message_id?`
  (`:1180`) and `failure_code?` (`:1181`).
- `ChatMessage` (`types/index.ts:121-148`) carries `client_request_id?: string | null` (`:132`, "Server
  receipt identity for an accepted user send"), `local_status` (`:133`), `local_requested_state_id`
  (`:136`), `local_accepted_user_message_id` (`:138`) and `local_failure_code` (`:143`).
- `fetchComposerProgress` (`client.ts:834-844`) and `fetchMessages` (`:807-812`) take no signal and use
  no decoder. The only per-call timeout idiom is `fetchSystemStatus` with `AbortSignal.timeout(5000)`
  (`:663`). Grep `AbortSignal.timeout` over `api/client.ts`: 1 hit, which is the positive control.
- `fetchCompositionState` maps 404 to `null` (`:1059-1070`), the 404-tolerant shape a poll client can copy.
- **No `/operations` client exists.** Grep `/operations` over `api/client.ts`: 0 hits. The negative
  result is controlled by the same grep form finding `/state/revert` (`:1093`).

### 1.2 `sessionStore.sendMessage` (`stores/sessionStore.ts:1480-1761`)

Signature: `sendMessage: (content: string, signal?: AbortSignal, retryLocalMessageId?: string) => Promise<void>`
(`:1103`). The third parameter is new since `d479eb2b4`.

- Pre-flight: `if (!activeSessionId) return` (`:1482`); synchronous admission gate `if (isComposing) return`
  (`:1490`); captures `recoveryStartedCompositionVersion` (`:1491-1492`) and `baselineMessageIds` (`:1493`).
- **Client id minting and replay** (`:1495-1513`):
  - A local-row retry (`retryLocalMessageId`) looks up a `local-*` row. If that row is missing or has no
    `client_request_id`, it returns silently (`:1495-1498`).
  - `stateId = retriedIntent ? retriedIntent.local_requested_state_id ?? null : get().compositionState?.id ?? null`
    (`:1499-1501`). A retry therefore replays the **original** `state_id` and does not re-read the head.
  - `clientRequestId = retriedIntent?.client_request_id ?? crypto.randomUUID()` (`:1502`).
  - The optimistic row has id `local-${clientRequestId}`, carries `client_request_id` and
    `local_requested_state_id`, and has `local_status: "pending"` (`:1503-1513`).
- `set` (`:1515-1527`): `isComposing: true`, `error: null`, `composerProgress: null`,
  `lastComposeChangedPipeline: null`, and the row is appended or re-pended.
- Both pollers start and return generations (`:1528-1531`).
- Request: `api.sendMessage(activeSessionId, optimisticMessage.content, clientRequestId, stateId, signal)` (`:1534`).
- The **success reducer** (`:1535-1617`):
  1. Claim fence `freeformComposeClaimIsCurrent(sid, inflightGen)` (`:1535-1537`; helper `:451-456`). It
     returns early **without clearing `isComposing`**.
  2. `await loadInflightMessages(sid, inflightGen)` (`:1545`), then the claim fence again (`:1548-1550`).
  3. One `set` (`:1553-1607`):
     - clear validation when the version changed (`:1561-1563`);
     - `newState = state ?? s.compositionState` (`:1566`);
     - clear the selection if the selected node vanished;
     - clear `local_status/local_error/local_failure_code` on the optimistic id and append `message` only
       if it is new (`:1578-1591`);
     - `lastComposeChangedPipeline: versionChanged` (`:1599`);
     - `mergeCompositionProposals` (`:1600-1603`; merge at `:868-882`);
     - `isComposing: false` (`:1604`).
  4. `loadBlobs` (`:1610`), `loadSessions` (`:1616`), `refreshInterpretationEventsForSession` (`:1617`).
- The **error reducer** (`:1618-1747`):
  - **The 409 `message_already_accepted` arm** (`:1619-1645`) matches `status 409`, `error_type`,
    `client_request_id === clientRequestId` and a string `user_message_id`. It stamps
    `local_accepted_user_message_id`, clears `isComposing` and calls `reconcileAcceptedSend(...)`.
  - The copy ladder:
    - an abort maps to `composeAbortMessage(signal)` (`:1652-1653`);
    - 422 `convergence` (`:1657`);
    - 502 `llm_unavailable` (`:1659-1663`);
    - 502 `llm_auth_error` (`:1664-1668`);
    - `audit_integrity_error` (`:1669-1670`);
    - otherwise `apiErr.detail ?? "Failed to send message. Please try again."` (`:1671-1674`).
  - `auditIntegrityRefusal` un-fails the row (`:1681-1682`, `:1709-1715`).
  - **`localFailureCode` sets `"message_idempotency_conflict"` for that error type** (`:1683-1688`).
    Otherwise it takes `apiErr.failure_code`.
  - The recovery patch (`:1689-1694`) is followed by the claim fence (`:1695-1697`).
  - `convergencePartialStatePatch` (`:1700-1702`; helper `:116-136`).
  - The set (`:1703-1726`) matches the row by id **or** by `client_request_id` (`:1707-1708`).
  - An abort calls `resyncAfterAbortedComposeTurn` (`:1727-1735`). An ambiguous network failure calls
    `resyncAfterAmbiguousComposeFailure(..., content, clientRequestId)` (`:1736-1746`).
- `finally` (`:1748-1760`) stops both pollers by generation, then makes a one-shot owner-generation
  progress read.

### 1.3 `sessionStore.retryMessage` (`:2173-2380`)

Signature: `retryMessage: (messageId: string, signal?: AbortSignal) => Promise<void>` (`:1139`). It has
three arms:

1. **Accepted-receipt refresh** (`:2183-2197`): when the row has both `client_request_id` and
   `local_accepted_user_message_id`, it starts the pollers and calls `reconcileAcceptedSend` without
   composing.
2. **Local-row replay** (`:2198-2202`): a `local-*` row re-enters `sendMessage(content, signal, message.id)`.
   It reuses the same `client_request_id` and `local_requested_state_id`.
3. **Canonical recompose** (`:2203-2379`):
   - The row is marked `local_status: "pending"` (`:2205-2216`) and both pollers start (`:2217-2220`).
   - It calls `api.recompose(activeSessionId, messageId, signal)` (`:2226`).
   - The success reducer (`:2227-2289`) mirrors send, **except that it does not call `loadSessions`**.
     Grep over `:2222-2290` finds none; the same grep finds `:1616`.
   - The error copy ladder (`:2292-2304`) runs 502 unavailable, then 502 auth, then 422 convergence, then
     `detail`. **It has no `audit_integrity_error` arm**, so an audit-integrity refusal on retry marks the
     row failed.
   - `localFailureCode` maps `"recompose_user_message_mismatch"` (`:2309-2314`). The backend producer is
     `src/elspeth/web/sessions/routes/composer/compose.py:162`.
   - The abort and ambiguous resyncs are at `:2346-2366`. The ambiguous resync receives **no**
     `clientRequestId` (`:2357-2365`), so it falls back to content, assistant and state evidence
     (`:712-714`).

### 1.4 Transcript-matching recovery that ruling 1 deletes: its actual span at the tip

The panel cites `sessionStore.ts:676-803`. At the tip, the recovery keyed on `client_request_id` covers
all of the following:

| Site | Lines | What it does |
|---|---|---|
| `resyncAfterAmbiguousComposeFailure` accepted-row probe | `:674-685` | finds `user && client_request_id === id` in fresh messages → `reconcileAcceptedSend` |
| same, durable-evidence rule | `:697-714` | with an id, **only** a matching user row counts as evidence |
| `reconcileAcceptedSend` (whole function) | `:756-865` | stops pollers, four-surface read, requires the canonical row with the same `client_request_id` (`:787-793`), decides "deliberate retry" vs "saved" copy, or on failure stamps `local_accepted_user_message_id` |
| `sendMessage` 409 `message_already_accepted` arm | `:1619-1645` | the route's 409 → reconcile |
| `sendMessage` `message_idempotency_conflict` → `local_failure_code` | `:1683-1688` | terminal, Retry suppressed |
| `sendMessage` error set: match by `client_request_id` | `:1707-1708` | |
| `retryMessage` accepted-receipt arm | `:2183-2197` | refresh-only retry |
| `loadInflightMessages` carry-forward of `local_accepted_user_message_id` | `:2122` | **delete**; this is the only recovery field in the carry-forward |
| *(re-key, do not delete)* `loadInflightMessages` optimistic survivor filter | `:2103-2110` | dedup, not recovery: drops a `local-*` row once a canonical user row with the same `client_request_id` exists |
| *(re-key, do not delete)* `loadInflightMessages` carry-forward of `local_requested_state_id` and failed status | `:2111-2121`, `:2123-2131` | dedup and replay custody, keyed by `client_request_id` |
| `MessageBubble` Retry suppression | `components/chat/MessageBubble.tsx:283` | list includes `message_idempotency_conflict` and `recompose_user_message_mismatch` |

Instrument: `grep -rn client_request_id` over non-test `.ts/.tsx` returned 22 lines in 3 files
(`api/client.ts`, `stores/sessionStore.ts`, `types/index.ts`). The `sessionStore.ts` hits are exactly
`:676,703,790,803,855,1498,1502,1508,1623,1631,1708,2107,2108,2112,2115,2183,2189,2199`.
- Positive control: the same grep hits `sessionStore.ts:1623`, the known `message_already_accepted` arm.
- Negative control: `tests/e2e` has 0 hits for `client_request_id|message_already_accepted|message_idempotency_conflict`.

### 1.5 Module-global pollers and settle wait

- Progress poller (`:48`, `COMPOSER_PROGRESS_POLL_INTERVAL_MS = 1500`):
  - state at `:317-349`;
  - `loadComposerProgress` (`:1950-2009`): the ownership fence has interval and owner modes, plus a
    ticket order and the stale-terminal discard;
  - `startComposerProgressPolling` (`:2011-2025`) and `stopComposerProgressPolling(sid?, gen?)` (`:2027-2041`).
- Inflight poller (`:438`, 1500 ms):
  - `inflightMessagesLatestClaimBySession` (`:446`);
  - `freeformComposeClaimIsCurrent` (`:451-456`);
  - `loadInflightMessages` (`:2043-2140`) returns `ChatMessage[] | null`;
  - start and stop at `:2142-2171`.
- `waitForCancelledComposeToSettle(sessionId, ownerGeneration)` (`:521-546`):
  - loops every `ABORT_RESYNC_SETTLE_POLL_MS = 500` (`:515`) on `fetchComposerProgress(...).inflight_requests ?? 0`;
  - exits on 0, on a session change, on a generation change, or on a fetch error;
  - has no wall-clock bound, by design (`:509-514`).
- `resyncAfterAbortedComposeTurn` (`:568-640`):
  - waits, then syncs inflight, then reads state and proposals;
  - clears validation on a version change;
  - `stoppedComposeOutcomeMessage` (`:285-306`) refines the banner;
  - finishes with blobs, sessions and interpretation.
- **Callers of the settle wait at the tip:** `:577` (abort resync) and `:667` (ambiguous resync) only.
  - Positive control: the same grep finds the definition at `:521`.
  - There is no guided caller any more. Grep for
    `resyncAfterAbortedGuidedTurn|sendGuidedChat|cancelGuidedChat|TutorialGuidedShell` over `src` and
    `tests` returned 0 lines. Grep for `stale_compose_state` over `src` and `tests` also returned 0; its
    positive control is the backend `app.py:1512`.

### 1.6 `sessionOperationRetry.ts` (renamed from `guidedOperationRetry.ts` in `7001600fe`)

- Kinds `"state_revert" | "session_fork"` only (`:1`). Descriptor
  `{kind, sessionId, requestFingerprint, operationId, createdAt}` (`:3-9`).
- Storage and bounds:
  - key `SESSION_OPERATION_RETRY_STORAGE_KEY = "elspeth_session_operation_retries_v1"` (`:22`);
  - schema `session-operation-retries.v1` (`:23`);
  - **`window.sessionStorage`** (`:45-51`);
  - v4 UUID operation ids (`:24`);
  - 24 h age, 16 descriptors, `MAX_STORAGE_BYTES = 8192` (`:27-29`).
- It stores a **fingerprint only**, using 4-lane FNV-1a (`:181-198`) over the canonical JSON
  `{schema:"session-operation-request-fingerprint.v1", kind, requestIdentity}` (`:235-237`). The body
  lives only in page memory, and the `liveOperationIds` set is at `:43`.
- API:
  - `acquireSessionOperationRetry` (`:227-270`), where the same fingerprint re-adopts the descriptor and
    re-arms liveness, and a different fingerprint for the same kind and session is a conflict;
  - `isSessionOperationRetryReplayable` (`:275-277`);
  - `clearSessionOperationRetry` (`:279-292`);
  - `findSessionOperationRetry` (`:294-302`);
  - `clearSessionOperationRetriesForSession` (`:304-320`);
  - `clearAllSessionOperationRetries` (`:322-325`);
  - `clearOrphanedSessionOperationRetriesForSession` (`:337-351`), which sweeps every non-live
    descriptor for the session;
  - `isAmbiguousSessionOperationRetryFailure` (`:353-368`). TypeError, Abort and Timeout, and 5xx are
    ambiguous, except `session_operation_terminal_failure` and `server_invariant_violated`.
- **Production callers (sessionStore only):**
  - imports `:16-37`;
  - conflict copy `:141-156`;
  - reconcile helpers `:168-213`;
  - orphan sweep in `selectSession` `:1362`;
  - `session_fork` acquire `:2388`;
  - `state_revert` acquire `:2587`;
  - `clearAllSessionOperationRetries()` in `reset` `:2704`.

  Instrument: grep `sessionOperationRetry|SessionOperationRetr` over non-test `.ts/.tsx` lists only
  `stores/sessionStore.ts` outside the module itself. Test users are `sessionOperationRetry.test.ts`,
  `sessionStore.test.ts`, `authStore.test.ts` and `api/client.composition-state.test.ts`.
- Guided residue: grep `guidedOperationRetry|GuidedRetry|guided_operation` over `src`: 0 hits.

### 1.7 Stop path, deadline and boot latch

- `hooks/useComposer.ts:19-69`:
  - `activeControllerRef` (`:27`);
  - `sendMessage`/`retryMessage` pre-check `isComposing`, then call
    `runComposeWithTimeout(activeControllerRef, composeTimeoutReady, runner)` (`:29-54`);
  - `cancelComposition = () => activeControllerRef.current?.abort(COMPOSE_USER_CANCEL_ABORT_REASON)` (`:56-58`).
- `config/composer.ts` (80 lines):
  - `COMPOSE_CLIENT_GRACE_MS = 25_000` (`:1`) and `DEFAULT_COMPOSE_TIMEOUT_MS = 270_000 + grace` (`:2`);
  - `getComposeTimeoutMs` (`:15-17`);
  - `applyServerComposerTimeout` (`:30-42`);
  - `resetComposeTimeoutForTests` (`:44-47`);
  - `runComposeWithTimeout` (`:49-74`);
  - abort reasons `compose_timeout` and `compose_user_cancel` (`:79-80`).
- **`runComposeWithTimeout` has exactly one non-test caller, `hooks/useComposer.ts:31`.** Grep over
  non-test files hit only `config/composer.ts` (definition) and `hooks/useComposer.ts`. The guided
  caller `ChatPanel.sendGuidedChat` is gone; `sendGuidedChat` has 0 hits.
- **`useComposer()` callers (non-test):**
  - `components/chat/ChatPanel.tsx:317`;
  - `components/tutorial/TutorialFreeformShell.tsx:51`;
  - `components/sidebar/SideRailValidationBanner.tsx:32`.

  ChatPanel's send surfaces are at `:988`, `:994`, `:1021`, `:1061`, `:1071` and `:1092`, and Retry is
  wired as `MessageBubble onRetry={turn.kind === "user" ? retryMessage : undefined}` (`:1321`). No
  component calls the store `sendMessage`/`retryMessage` directly. Grep `\.sendMessage\(|\.retryMessage\(`
  over non-test files finds only `sessionStore.ts:2200` (retryMessage re-entering send) and
  `TutorialFreeformShell.tsx:112` (`composer.sendMessage`, via the hook).
- Stop UI:
  - `ChatInput disabled={isComposing} onCancel={isComposing ? cancelComposition : undefined}`
    (`ChatPanel.tsx:1469-1472`);
  - the button renders only when `disabled && onCancel` (`ChatInput.tsx:547-551`, `aria-label="Stop composing"`).
- `App.tsx:391-423`:
  - applies `status.composer_timeout_seconds` through `applyServerComposerTimeout`;
  - latches `setComposeTimeoutReady(true)` (setter `sessionStore.ts:1194-1196`);
  - otherwise flags `setComposerTimeoutUnavailable(true)` once.
  - Re-auth triggers `checkHealth` on the false-to-true transition (`App.tsx:597-603`).
  - `SystemStatus.composer_timeout_seconds?` is at `types/index.ts:1277`.
- `composeTimeoutReady` gates these send affordances:
  - `ChatInput.tsx:234-242`;
  - `SideRailValidationBanner.tsx:39-43`;
  - `ChatPanel.tsx:369,982` (decision Apply);
  - `TutorialFreeformShell.tsx:52,109,161`.

### 1.8 Proposal Accept/Reject controls (ruling-5 guard site)

- `ProposalItem` (`components/chat/DecisionPanel.tsx:334-354`):
  - Accept `disabled={isBusy || isStale}` (`:349`);
  - Reject `disabled={isBusy}` (`:350`).
  - **Neither is gated on `isComposing`.**
- `DecisionPanel` receives `isComposing` (`:69`, `:159`) but uses it only for suggestion Apply
  (`:180-181`, `:205`, `:280`).
- Reject goes through a `ConfirmDialog` (`:311-322`).
- The sole wiring is `ChatPanel.tsx:1142,1152-1153`. The store actions are `acceptProposal`
  (`sessionStore.ts:1781-1883`) and `rejectProposal` (`:1885-1948`). Neither checks `isComposing`.
- Instrument: grep `acceptProposal|rejectProposal|acceptCompositionProposal|rejectCompositionProposal`
  over non-test files lists only `client.ts:913,930`, `ChatPanel.tsx:354-355,1152-1153` and the store.
  There is no second Accept surface; the tutorial has none either.
- Other head-moving controls **not** gated on `isComposing`:
  - `HeaderVersionSelector` revert (`components/header/HeaderVersionSelector.tsx:36,349,534`,
    `disabled={!canRevertSelected}`);
  - interpretation resolve (`stores/interpretationEventsStore.ts:429`).
  - `forkFromMessage` itself **sets** `isComposing: true` (`sessionStore.ts:2405`).

### 1.9 Tutorial (freeform since `01d96af9a`)

- `TutorialFreeformShell.tsx` (179 lines):
  - `useComposer()` (`:51`);
  - `hasUserMessage = messages.some(role==="user")` (`:71`);
  - `pendingProposal` (`:72`).
- **Send** is `onSendBrief` (`:107-113`). It is guarded by `sentRef.current || sampleUrls === null ||
  hasUserMessage || isComposing || !composeTimeoutReady || activeSessionId !== sessionId`, then calls
  `composer.sendMessage(tutorialBrief(sampleUrls))` (`:112`).
- The button renders only when `sampleUrls !== null && !hasUserMessage && !sent` (`:157-165`).
- **Reload-does-not-resubmit** rests on two things:
  - server transcript state: `hasUserMessage`, after `selectSession` in the mount effect (`:74-105`);
  - component-local `sent`/`sentRef` (`:67-69`), which do not survive a reload.

  Pinned by `TutorialFreeformShell.test.tsx:89` "resumes an existing freeform transcript without
  resending the brief" and `:67` "submits one complete brief…".
- **Build/Continue gate:**
  - `readyToCheck = active && compositionStateLoaded && hasUserMessage && compositionState !== null &&
    !isComposing && !pendingProposal && proposalActionPendingIds.length === 0 && pendingReviewCount === 0`
    (`:143-145`);
  - `onContinue` re-checks the same things after `getTutorialReadiness` and state-id equality (`:115-141`).
- `tutorialDeparture.ts:4-15`: `assertLoadedFreeformSession` throws when
  `isComposing || proposalActionPendingIds.length > 0`.
- Resume identity comes from server preferences (`HelloWorldTutorial.tsx:36-61`,
  `resumeTutorialState({ sessionId: prefs.tutorialSessionId, … })`), not from browser storage.

### 1.10 Session switch, reload and resets

- `selectSession` (`:1357-1431`) does the following:
  - clears validation and both poll timers (`:1358-1360`);
  - runs the orphan sweep (`:1362`);
  - advances the publication generation;
  - sets `isComposing: false` (`:1378`);
  - loads messages, state, proposals and preferences.

  It does not abort an in-flight POST.
- Other `isComposing: false` sites:
  - initial state `:1173`;
  - archive `:1318`;
  - the 404 branch `:1417`;
  - `resetForTutorialSession` `:1447`;
  - `unbindMissingSession` `:1473`;
  - fork `:2471`, `:2496`;
  - accepted reconcile `:838`, `:851`.

  **`createSession` (`:1247-1289`) still does not reset `isComposing`**; its set block has no such key.
- `reset()` (`:2701-2715`) clears the timers and all retry descriptors, then spreads `initialState`.
- Nothing persists freeform in-flight state across a reload. `isComposing` is memory-only.

### 1.11 Progress correlation in the view

`ChatPanel.tsx:526-562`: "Freeform progress.request_id is the persisted user-message id". Retirement of
the terminal indicator binds to `turn.kind === "user" && turn.id === requestId`.
`shouldShowComposerProgress = isComposing || (terminal && !retired)` (`:563-566`).
`findActiveComposerMessage` picks the newest pending user row (`:1513-1527`).

### 1.12 Tests

**Harness at the tip.** These are unchanged from old §10:
- `package.json` scripts: `test` = `vitest run` (`:20`), `typecheck` (`:15`), `lint` (`:17`),
  `build` (`:13`), `test:e2e` = `playwright test` (`:22`).
- `vite.config.ts:28-34`: `environment: "jsdom"`, setup files `./src/test/setup.ts` and
  `./src/test/a11y/setup.ts`, include `src/**/*.test.{ts,tsx}` and `tests/e2e/harness/**/*.test.ts`.
- CI `.github/workflows/ci.yaml` runs `npm run typecheck` (`:1320`), `npm test -- --run` (`:1326`) and
  `npm run test:e2e` (`:1267`). Lint and build run only locally, as the comment at `:1290` says.

**Mock factories.** The instrument was
`grep -rlnE "vi\.mock\(\s*[\"'](@/api/client|(\.\./)+api/client|\./api/client|\./client)[\"']"` over
`src/**/*.test.ts[x]`.
- Result: **45 files**.
- Of these, **5 factories explicitly key `sendMessage`/`recompose`**:
  - `App.test.tsx:249` (`./api/client` form; keys `:265-266`);
  - `components/common/CommandPalette.test.tsx:53` (`:59-60`);
  - `components/common/commandRegister.test.tsx:44` (`:50-51`);
  - `stores/sessionStore.test.ts:24` (`:32-33`);
  - `test/inlineSourceIntegration.test.tsx:158` (`../api/client` form; keys `:175-176`, inside the factory).
- Controls: the positive is `App.test.tsx` (relative form); the negative is
  `api/client.interpretation.test.ts`, which mocks the module and has 0 `sendMessage|recompose` hits.
- `components/chat/ChatPanel.test.tsx:58` uses `importOriginal` spread, so it needs no new keys. Its
  `sendMessage: vi.fn()` hits (`:273`, …) are `useComposer` `mockReturnValue`s.
- `TutorialFreeformShell.integration.test.tsx:14` is a bare automock, `vi.mock("@/api/client")`.
- The tutorial tests stub the **store** `sendMessage` (`TutorialFreeformShell.test.tsx:39`,
  `.integration.test.tsx:49`).

**`stores/sessionStore.test.ts` (4870 lines).**
- `describe("freeform send identity")` `:239-492` is the ruling-1 block. It holds these tests:
  - `:240` accepted 409 refresh-only;
  - `:273` slow accepted-receipt snapshot;
  - `:303` ambiguous accepted;
  - `:380` receipt kept on reconcile failure;
  - `:406` deliberate recompose after acceptance;
  - `:450` names the canonical user;
  - `:463` conflicting reuse is terminal.
- Others:
  - `:1932` "reconciles a lost POST response…" and `:1988` "keeps the retry affordance…";
  - `:2513` "does not publish an old accepted receipt over a newer A turn after A->B->A";
  - admission gate `:3252`, with `:3275`, `:3300` and `:3339`;
  - `retryMessage abort handling` `:4236`;
  - abort/settle `:1230-1913`.
- Grep for identity fields (`message_already_accepted|message_idempotency_conflict|local_accepted_user_message_id|client_request_id`)
  over this file: 38 lines.

**Other test files.**
- `api/client.recovery.test.ts:38` "sends an exact ingress identity and parses the canonical acceptance
  receipt" and `:56` "binds recompose to the expected last conversational user". Parity tests at
  `:63-269`.
- `components/chat/MessageBubble.test.tsx:152` `it.each([... "message_idempotency_conflict",
  "recompose_user_message_mismatch"])`.
- `hooks/useComposer.test.ts`:
  - `:17` describe "compose timeout ceiling";
  - `:63` call-time ceiling;
  - `:96`/`:109`/`:119` readiness gate;
  - `:137` "keeps Stop bound to the active compose when a raced entry is refused".
- `config/composer.test.ts:61` describe is "runComposeWithTimeout — the composer send primitive". The
  old describe name ending in "freeform/guided" has gone.
- Stop is clicked in `ChatInput.test.tsx:287` and `TutorialFreeformShell.integration.test.tsx:59`
  ("lets Stop abort the initial brief's request", which asserts `signal.reason ===
  COMPOSE_USER_CANCEL_ABORT_REASON`). The old a11y Stop test (`test/a11y/components.a11y.test.tsx:1472`)
  now returns 0 hits.

**Playwright.** Grep over `tests/e2e`:
- `composer-proposals.spec.ts:172-201` fulfils `POST /messages` synchronously with
  `MessageWithStateResponse`. The test is `:256` "explicit approve tool call is visible before commit",
  and it clicks Accept at `:274`. It also has `GET /messages` `:167`, `composer-progress` `:151`, and
  status `composer_timeout_seconds: 180` `:101`.
- `tutorial.spec.ts:113-128` handles `POST /messages` by pushing `"freeform-compose"`, then fulfils a
  synchronous `{message, state, proposals: []}`. The test is `:197` "Welcome → freeform Build → explicit
  Run → Audit → Graduation keeps one session", asserting at `:223`. It also has `composer-progress`
  `:149` and status `:62`.
- `helpers/workspace-fixtures.ts:426` (GET messages) and `:478,501` (status).
- Harness classifiers treat `POST /messages` as the freeform compose:
  - `tests/e2e/harness/transition-ledger.ts:29`;
  - `classify.ts:42`;
  - `tutorial-reliability.staging.spec.ts:220-235`.
- `tests/e2e/guided-collector.spec.ts` no longer exists (`ls`: No such file).

---

## 2. DELTA vs old findings (`d479eb2b4`) and contract/T14

### Moved or renamed

- `guidedOperationRetry.ts` became `sessionOperationRetry.ts`:
  - storage key `elspeth_guided_operation_retries_v2` → `elspeth_session_operation_retries_v1`;
  - schema v2 → v1;
  - classifier string `guided_operation_terminal_failure` → `session_operation_terminal_failure`.
  - The old corrections 1-4 still hold: sessionStorage, fingerprint only, the orphan sweep from
    `selectSession` (old `:1946` → `:1362`), and the 8192-byte bound.
- `sessionStore.ts` shrank from about 4800 to 2716 lines, so every old line number is stale:
  - `sendMessage` `:2174-2419` → `:1480-1761`;
  - `retryMessage` `:2820-3007` → `:2173-2380`;
  - settle wait `:798-826` → `:521-546`;
  - pollers `:2611-2818` → `:1950-2171`.
- `client.ts` `sendMessage` `:896-913` → `:952-972`; `recompose` `:917-927` → `:977-989`;
  `parseResponse` `:243` → `:290`.

### Vanished

- Every guided surface the old findings and T14 cited:
  - `ChatPanel.sendGuidedChat` and `cancelGuidedChat`;
  - `resyncAfterAbortedGuidedTurn`;
  - guided `chatGuided` poller claims;
  - `sessionStore.guided.test.ts`;
  - `ChatPanel.guidedStartRecovery.test.tsx`;
  - `TutorialGuidedShell.tsx`;
  - `tests/e2e/guided-collector.spec.ts`;
  - the 6 guided retry kinds.
- **`runComposeWithTimeout` is no longer shared.** Its one caller is freeform `useComposer`. Contract
  `:444-445` and T14 `:34`, `:83-87`, `:146-148`, `:5160` ("guided keeps it, with its ceiling read from
  `composer_sync_timeout_seconds`, M6") have no consumer.
- The a11y Stop test at old `components.a11y.test.tsx:1472`.
- Mock-factory inventory: old **47 files / 8 keyed** → now **45 / 5 keyed**. The drops are
  `ChatPanel.guidedStartRecovery.test.tsx` and `sessionStore.guided.test.ts`, which are deleted, and
  `ChatPanel.test.tsx`, which now spreads `importOriginal`.

### Reversed

- Old §0.5, "`recompose` sends no body", is false now. Recompose sends `{expected_user_message_id}`
  (`client.ts:985`; `schemas.py:162-165`).
  - Contract `:161` and T14 `:159` ("recompose body `{operation_id}`") and the chair's fact-correction 5
    agree that it exists.
  - Ruling 5 adds a recompose `state_id` to it.

### New since `d479eb2b4`; absent from the contract, T14 and the spec

- The whole `client_request_id` ingress identity in the SPA: minting and replay, the 409
  accepted/conflict arms, `reconcileAcceptedSend`, the `local_*` identity fields, and MessageBubble
  suppression (§1.4).
- Grep `client_request_id|message_already_accepted|reconcileAcceptedSend` over `contract.md` and `T14.md`:
  0 and 0.
- `sendMessage` has a third parameter, `retryLocalMessageId`. The local-row retry deliberately replays
  the original id and `state_id`.
- **The `state_id` null-vs-absent split.** Live behaviour sends `state_id: null` when there is no state
  (§1.1). T14 plans the opposite: "omits state_id (never null) when the session has no composition
  state" (`T14.md:1905-1914`).
- The freeform tutorial (`TutorialFreeformShell`) is now an ordinary `useComposer` caller.
- `f3d37c1a7` ("Serialize Composer review and validation mutations") touched only `api/client.ts`,
  `api/authSession.ts` and `api/client.interpretation.test.ts` in the frontend (`git show --stat`). It
  changed no send, recompose or store path.

### Unchanged latent issues from the old §9

- `createSession` does not reset `isComposing` (`:1247-1289`).
- `retryMessage` has no `audit_integrity_error` arm (`:2296-2303`).
- The claim-fence early returns (`:1535-1537`, `:1548-1550`) leave `isComposing` set.

---

## 3. IMPLICATIONS for the plan under the rulings

1. **Ruling 1's SPA deletion set is the §1.4 table, not `:676-803`.**
   - T14 must delete `reconcileAcceptedSend` (`:756-865`), the `:674-685` probe, the accepted and
     conflict arms at `:1619-1645` and `:1683-1688`, and the retry accepted arm `:2183-2197`.
   - It must delete `local_accepted_user_message_id` (`types/index.ts:138`) and `ApiError.client_request_id`
     and `user_message_id` (`:1179-1180`, `client.ts:364-365,518-519`).
   - It must remove `message_idempotency_conflict` from `MessageBubble.tsx:283`.
   - The test block `sessionStore.test.ts:239-492`, plus `:1932`, `:1988`, `:2513`,
     `client.recovery.test.ts:38`, and the `MessageBubble.test.tsx:152` row, are removed or rewritten.
   - **Keep one thing but re-key it:** the `loadInflightMessages` optimistic dedup (`:2103-2121`,
     `:2123-2131`; only `:2122` is deleted) is not recovery. The optimistic row must still yield to its canonical row, which under async the
     worker inserts later. Under "one wire name" it matches on the job id. This requires the transcript
     row to carry that id (today `ChatMessageResponse.client_request_id`, `schemas.py:216`), renamed in
     the same epoch cut. **Open question for the backend seam:** does the renamed ingress key still ride
     `GET /messages`? The panel says ingress is joined onto every chat row (`service.py:4759-4764`).
2. **The operation id is today's `client_request_id` minting point** (`sessionStore.ts:1502`), and
   today's local-row replay (`:1495-1502`, `:2198-2201`) already has the spec §2 shape: "a retry uses
   the exact same ID and body". The custody module replaces the in-memory `local-*` row as the body
   holder so that a reload can resume. `retriedIntent.local_requested_state_id` becomes the custody
   body's `state_id`.
3. **Ruling 5 needs a separate resend path.** A `stale_compose_state` refusal is terminal for its
   operation id. Retrying that row with the same `(id, body)` would replay the stored terminal 409
   forever.
   - "Keeps the body in custody so the user can resend it" therefore means minting a **new** id with a
     **freshly read** head `state_id` (or the content restored to the input) for this failure class.
   - The row needs a distinct `local_failure_code` (for example `stale_compose_state`), and MessageBubble
     must either route its Retry to "resend" or suppress Retry.
   - Today `stale_compose_state` has no SPA arm. It renders through the generic `detail` fallback
     (`sessionStore.ts:1672-1673`), and the body at `src/elspeth/web/app.py:1502-1516` gives the text
     "The session changed while the compose turn was running." That fallback is "today's stale copy".
4. **Ruling 5 SPA guard is new work.** Proposal Accept and Reject need
   `disabled={isBusy || isStale || <same-tab send queued>}` at `DecisionPanel.tsx:349-350`. The store
   actions (`:1781`, `:1885`) should refuse programmatically in the same way that the admission gate
   refuses a second send (`:1490`).
   - Today `isComposing` is the only same-tab signal. Under async it must stay true from custody
     acquire until terminal, which includes the queued phase.
   - Revert (`HeaderVersionSelector.tsx:534`) and interpretation resolve are also head-moving and
     ungated (open question 2).
4a. **Ruling 5 turns the `selectSession` load window into a false stale refusal.**
   - `selectSession` sets `compositionState: null` and `compositionStateLoaded: false` (`:1369-1371`)
     before its fetches land.
   - No send surface gates on `compositionStateLoaded`. Grep over `ChatPanel.tsx`, `ChatInput.tsx`,
     `SideRailValidationBanner.tsx` and `useComposer.ts` returned 0 lines. The positive control is
     `TutorialFreeformShell.tsx:54,143`, which does gate on it.
   - The store reads `get().compositionState?.id ?? null` at send time (`:1501`). A send typed in that
     window therefore carries `state_id: null`. Today that is harmless, because the route composes
     against the actual head regardless (`src/elspeth/web/sessions/routes/messages.py:192-218`: `body.state_id` is only provenance, and
     `compose_base_state_id` is the actual head, `:218`).
   - Under ruling 5, null or absent means "no state", and the worker compares it with the head. A
     session that has a head would get a false `stale_compose_state` 409 on every such send.
   - The SPA must hold Send until `compositionStateLoaded`, or the store must refuse. This is also why
     open question 1 (null vs absent) matters for the loaded-but-empty case.
   - Relatedly, a head moved by a sibling tab now produces a refusal by design. That is the ruling
     working as intended, and the plan should say so.
5. **`runComposeWithTimeout` should be deleted, not preserved.**
   - At cutover `useComposer` stops calling it, which leaves zero callers.
   - The T14 steps that keep it "for guided" and the `composer_sync_timeout_seconds` SPA plumbing
     (`T14.md:83-87,146-148,171,194,211,3660-3750,5088`) are dead scope.
   - `applyServerComposerTimeout` and `composeTimeoutReady` still gate four send affordances (§1.7). The
     plan must decide whether the latch survives: the freeform deadline now comes from the poll body's
     `deadline_remaining_ms`, per contract `:445-447`.
6. **Stop and the settle wait.**
   - `cancelComposition` (`useComposer.ts:56-58`) must target the cancel endpoint.
   - `waitForCancelledComposeToSettle`'s two callers (`:577`, `:667`) are both freeform. The
     `inflight_requests` settle loop can be retired for freeform, with no guided dependant left.
   - Contract D1 (`contract.md:71`) keeps `inflight_requests` populated by the worker's CRL, but the SPA
     must stop using it as settlement.
7. **The tutorial reload guard breaks in the queued window.** The worker inserts the user row at start,
   not admission. This is a ruling fact, not a frontend measurement: `RULINGS.md:8` says ingress is
   "written by the worker in the same transaction as the user row", and ruling 5's check runs "before
   the user row is inserted" (`RULINGS.md:25`). So `hasUserMessage` stays false while a job is queued.
   - After a reload `sent` and `sentRef` reset and `isComposing` is false (`selectSession:1378`), so
     "Send tutorial brief" reappears (`TutorialFreeformShell.tsx:157-165`).
   - The same-tab fix is to restore custody before render: contract `:441-442`,
     `resumeComposerOperation` on `selectSession`/boot, which must set `isComposing`. The shell's
     `onSendBrief` guard and the render condition must also consult it.
   - Cross-tab there is no custody, so a click sends a new id, gets 409 `composer_operation_active` and
     attaches (contract D8 `:78`). That is acceptable, but it needs a test.
   - The Continue gate (`:143-145`) relies on `!isComposing` too. A running job after a reload with a
     non-null interim head must hold Build.
8. **The tutorial Stop test must move.** `TutorialFreeformShell.integration.test.tsx:44-65` asserts an
   AbortSignal reason. Under async Stop is a cancel POST, so this test is rewritten, not preserved.
9. **Mock and e2e churn is smaller than T14 lists.**
   - The 5 keyed factories (§1.12) need the new exports.
   - Two Playwright specs return synchronous `POST /messages` bodies: `composer-proposals.spec.ts:172-201`
     and `tutorial.spec.ts:113-128`. They need a 202 plus an operations GET mock.
   - `composer-proposals.spec.ts:274` clicks Accept after the reply. Under the ruling-5 guard Accept
     must be enabled once the op is terminal, which is a good positive control for the guard.
   - The harness classifiers (`transition-ledger.ts:29`, `classify.ts:42`, `tutorial-reliability.staging.spec.ts:235`)
     count the POST as the compose. That count still works (one POST per action), but the POST no
     longer carries the turn.
10. **The custody-module design point still stands, with new names.** `sessionOperationRetry` is
    fingerprint-only, uses an 8192-byte bound, and sweeps orphans in `selectSession:1362`. The body-bearing
    freeform custody (contract `:434-437`) is a separate module. Its "reverses the fingerprint-only
    invariant" note must cite `sessionOperationRetry.ts:34-43,181-198`, not `guidedOperationRetry`.

---

## 4. Open questions

1. **Null vs absent `state_id`.** Ruling 5 says "absent means no state" and binds `state_id` into the
   request hash. The SPA sends explicit `null` today (`client.ts:963-965`, `sessionStore.ts:1501`), and
   T14 planned omission. Under B′'s "`None` materialisation", do `null` and absent produce the same hash?
   The codec seam must answer, and the SPA must pick one, pinned by a test.
2. **The breadth of the ruling-5 guard.** It names proposal Accept/Reject only. Revert
   (`HeaderVersionSelector.tsx:349`), interpretation resolve (`interpretationEventsStore.ts:429`) and fork
   also move the head through a SOL while a same-tab send is queued, and each would convert the queued
   send into a terminal stale refusal. Should the guard cover them too? Fork already borrows `isComposing`
   (`sessionStore.ts:2405`).
3. **Resend UX for a stale refusal.** Should it be a new id with a re-read head, sent automatically on
   Retry? Or should the content go back to the input with Retry suppressed? The owner or the UX seam
   decides. Either way the `local_failure_code` vocabulary grows.
4. **The transcript join key after the rename.** Does `ChatMessageResponse` keep a per-row client id, now
   `operation_id`, for the optimistic dedup in §3.1? If not, the dedup falls back to role and content
   matching, which the current code deliberately abandoned.
5. **The `composeTimeoutReady` latch after cutover.** Should it be kept as a boot-readiness gate for the
   four send affordances, or removed together with `runComposeWithTimeout` and `applyServerComposerTimeout`?
   If it is removed, `App.tsx:391-423` and the `composer_timeout_seconds` status field lose their SPA
   consumer.
6. **The recompose operation's transcript anchor.** Recompose jobs have no ingress row, according to the
   panel's FK note. After a reload mid-recompose, custody holds `{operation_id, expected_user_message_id,
   state_id}`. Confirm that the poll `result` is enough to run the recompose success reducer, which
   differs from send's: no `loadSessions` (§1.3).
7. **The latent `isComposing` leaks (§2 Unchanged).** Under async, `isComposing` gains a queued phase and
   a reload-restore path. Should the plan fix `createSession` and the claim-fence early returns
   (`:1535-1537`) in the same cut, or file them separately? The scope-discipline rules require asking.
