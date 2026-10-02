# R38. The newest-started refresh fence discards a successful older snapshot when the newer refresh fails

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Interpretation events |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-11#2 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/stores/interpretationEventsStore.ts:275-282`, `:291` and `:344`.
- **Wrong:** A response is dropped whenever a later request has started, and nothing reconciles the state when that later request fails. **Scenario:** a compose completion starts refresh #1, and the auto-validate success starts refresh #2 (`executionStore.ts:484`). #1 lands late and is dropped, then #2 fails. The newly minted pending card never renders. On activation, `reviewEventsLoaded` stays false.
- **Fix:** Use a per-session applied sequence, and drop a response only when a newer one has already been applied.
- **Sources:** fe-11#2.
- **Verifier notes:** This is a regression, because last-write-wins used to apply #1. In production only `refreshAll` is affected.


## Source findings and verification

### fe-11#2: Newest-started refresh fence discards successful older snapshots

- **Reported at:** `src/elspeth/web/frontend/src/stores/interpretationEventsStore.ts:344`; reviewer severity low; category race; diff-anchored True.
- **Summary:** refreshAll/refreshPending drop a response whenever a later refresh for the same session has started, even if that later refresh then fails. No snapshot is applied, and pending review cards (the fix affordance for a blocked Execute) stay missing until another refresh.
- **Failure scenario:** The compose completion fires refreshAll#1 (void). A successful auto-validate fires refreshAll#2 (executionStore.ts:485). #1 resolves after #2 starts and is dropped. #2 rejects on a network error or 5xx. The newly minted pending card never renders, and on first activation ChatPanel's reviewEventsLoaded stays false. Guided (retiring) respondGuided awaits refreshAll and can re-enable submit before the cards land.
- **Evidence:** `if (state.refreshRequestBySession[sessionId] !== request) return state;` at :291 and :344. The request is set at the start (:276-282, :306-312) and never reconciled on failure.
- **Suggested fix:** Use a per-session monotonic sequence and drop a response only when a newer one has already applied (appliedSeqBySession). Keep the terminal-id filtering for resurrection.
- **Verifier (trace):** upheld, confidence medium, severity low. I traced the code at 74c0ce0db and the defect is real. The fence came in with 7d981d6cf, inside the review window. refreshAll and refreshPending write their request Symbol before the GET (interpretationEventsStore.ts:275-282, 309-315). The apply step drops the response if that Symbol has been replaced (:291, :344). Nothing clears or reconciles the Symbol when a later request rejects, and nothing retries. So if an older request succeeds after a newer one has started, and the newer one then fails, no snapshot is applied.

The only way a pending card gets into the store is a snapshot. addPendingEvent (:543) has no production callers, and a grep over non-test src finds none. So a card minted by that turn stays invisible until the next refresh. Before the window the last write won, so the older successful snapshot would have been applied; this is a regression.

The overlap the finding describes happens in production. Compose completion fires refreshAll without awaiting it (sessionStore.ts:2476, 2850, 910/1010/1067 via refreshInterpretationEventsForSession). The version bump also starts the auto-validate loop (subscriptions.ts:583), and a successful validate fires a second refreshAll without awaiting it (executionStore.ts:484, which predates the window). Session activation gives the same pairing: selectSession fires refreshAll at sessionStore.ts:2064, then auto-validate. In that case pendingBySession[sid] stays undefined, so reviewEventsLoaded stays false (ChatPanel.tsx:778-780).

The fence is a recorded design choice: docs/reviews/2026-09-22-composer-feedback-repairs.md:8 says "Per-session request ownership orders both interpretation snapshot endpoints". That note only covers responses arriving out of order. It says nothing about the newer request failing, so no ruling covers this gap.

Severity stays low because two things must coincide. The older GET has to outlast a full validate POST round-trip, since the second refresh starts only after validate resolves. Then the newer GET has to fail. The next compose, validate or resolve refreshes the store and recovers it.

One small correction to the finding: refreshPending has no production callers (grep shows only tests), so in practice only refreshAll can hit this. The guided respondGuided await path (sessionStore.ts:3965, 3735) is in the retiring guided lane.
