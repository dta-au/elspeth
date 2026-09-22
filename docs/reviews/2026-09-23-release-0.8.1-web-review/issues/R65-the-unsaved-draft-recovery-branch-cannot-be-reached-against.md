# R65. The unsaved-draft recovery branch cannot be reached against the current backend

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Frontend workspace, inspector and stores |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-09#2 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/components/recovery/RecoveryPanel.tsx:49-55,138-143` and `types/recovery.ts:22-26`.
- **Wrong:** The server sends `partial_state` only after a successful save (`_helpers.py:3199/3357/3606`). When the save fails, the body has no `partial_state`, so the panel never opens. The new "not saved on the server" alert and the conditional Apply are therefore dead code. The panel also calls `.trim()` without the `typeof` guard that the store uses.
- **Fix:** Delete the branch and its test, or route bodies with `partial_state_save_failed` and no `partial_state` to an explanation.
- **Sources:** fe-09#2.


## Source findings and verification

### fe-09#2: Unsaved-draft recovery branch is unreachable against the current backend

- **Reported at:** `src/elspeth/web/frontend/src/components/recovery/RecoveryPanel.tsx:49`; reviewer severity low; category dead-code; diff-anchored True.
- **Summary:** draftWasSaved is always true while the panel is mounted, so the new 'not saved on the server' alert, the Discard-focus arm and the conditional Apply button never run. The backend only sends partial_state after save_composition_state succeeds, with the persisted UUID id. When the save fails it sends partial_state_save_failed with no partial_state, and isComposerRecoveryError then returns false, so the panel never opens.
- **Failure scenario:** A composer turn fails and the partial-state save raises SQLAlchemyError. The 422 body carries partial_state_save_failed=true, partial_state_save_error and failed_turn, but no partial_state (_helpers.py:3214/3222, 3380/3381, 3616/3617). isComposerRecoveryError (types/recovery.ts:22-26) returns false, so the recovery panel never shows its new explanation and the user gets the generic chat error. The repair doc's item 8 (docs/reviews/2026-09-22-chat-card-repairs.md) and RecoveryPanel.test.tsx:105-110 exercise a response shape the server never sends. The panel also calls partial_state.id.trim() without the typeof check the store guard uses, so the defensive case it exists for would throw during render.
- **Evidence:** _helpers.py:3199/3357/3606 set partial_state only after a successful save via _recovery_partial_state_response (the persisted record, UUID id). The recovery-error type guard requires partial_state != null. sessionStore.applyRecoveredState :3105-3114 has the same guard but predates the window (git log -L: 23e3a2be9, 2026-06-22).
- **Suggested fix:** Either delete the unreachable branch and its test, or make it reachable: route partial_state_save_failed bodies that have no partial_state to an explanation in the chat error or panel, and use a typeof check before .trim() as the store does.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute the finding. The unsaved-draft branch that commit 07faf477e added to RecoveryPanel is unreachable with any response body the server can send. There are only three places that write `partial_state` into a response body. Each writes it inside the `try`, after `save_composition_state` succeeds, so the id is always the saved record's id. The `except SQLAlchemyError` arm sets only `partial_state_save_failed` and `partial_state_save_error`. So no body carries both `partial_state` and `partial_state_save_failed=true`. `CompositionStateResponse.id` is a non-empty string built from the saved record. A body without `partial_state` fails `isComposerRecoveryError`, so neither the store nor `useRecoveryPanel` opens the panel. That means `draftWasSaved` is always true while the panel is mounted. As a result, the alert at :138-143, the Discard-focus arm at :55 and the conditional Apply button never fire in practice. RecoveryPanel.test.tsx:97-110 injects shapes the server never produces (`partial_state` together with `saveFailed=true`, and a blank id). The `.trim()` sub-claim is also correct. The panel omits the `typeof recoveredState.id !== "string"` check that `sessionStore.applyRecoveredState` has at :3107, so a malformed id would throw during render instead of reaching the defensive branch. That only matters for bodies the server does not send. Severity stays low because nothing is corrupted or blocked; the new code is only dead. The practical effect is that when the save fails, the user sees the generic chat error rather than the new in-dialog explanation that the repair doc's item 8 promises.
