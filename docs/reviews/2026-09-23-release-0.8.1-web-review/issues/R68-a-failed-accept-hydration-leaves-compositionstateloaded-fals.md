# R68. A failed accept hydration leaves `compositionStateLoaded` false permanently

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Frontend workspace, inspector and stores |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-12#1 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/stores/sessionStore.ts:2443-2460,2475` and `1365-1374`.
- **Wrong:** After a committed accept, the state is reset to null and not loaded. If hydration fails, no later publisher sets the flag back to true. GraphMiniView then shows "Loading pipeline…" over a populated pipeline, `#/{id}/yaml` and `/spec` deep links never fire, and the interpretation refresh is skipped.
- **Fix:** Have every publisher of an authoritative non-null state set `compositionStateLoaded: true`. Move the refresh before the hydration `try`, and update the doc comment.
- **Sources:** fe-12#1.
- **Verifier notes:** The error banner tells the user to reload, and re-selecting the session recovers.


## Source findings and verification

### fe-12#1: Failed accept hydration leaves compositionStateLoaded false permanently

- **Reported at:** `src/elspeth/web/frontend/src/stores/sessionStore.ts:2455`; reviewer severity medium; category correctness; diff-anchored True.
- **Summary:** After a committed accept, acceptProposal sets compositionState=null and compositionStateLoaded=false (2443-2447). If either hydration GET fails it returns (2455-2460), and no later publication path sets compositionStateLoaded back to true: sendMessage, recompose, applyResolvedInterpretation and the guided paths all skip it. Before this window, selectSession's failure arm (2090-2099) deliberately set it true so consumers would not wait forever. The early return also skips refreshInterpretationEventsForSession, and the doc comment at 1365-1374 is now false.
- **Failure scenario:** The user accepts a proposal and the POST commits. fetchCompositionState then fails transiently (a 503 or a replica restart). The user keeps chatting. sendMessage publishes version N+1, but compositionStateLoaded stays false. GraphMiniView shows 'Loading pipeline…' over a populated pipeline, #/{id}/yaml and #/{id}/spec deep links never pass the useHashRouter gate, and interpretation reviews created by the accept (which can gate execution) do not appear until the session is re-selected.
- **Evidence:** sessionStore.ts:2443-2447 sets null/false. 2455-2460 is the catch that returns without settling the flag. The only setters of compositionStateLoaded=true are 1798, 2000, 2098, 2467 and 3021 (grep). The sendMessage return object (~2257-2270) omits the flag. GraphMiniView.tsx:47-60 and useHashRouter.ts:125-165 gate on it. selectSession 2090-2099 has the comment 'Marking loaded lets deferred consumers ... resolve ... instead of waiting forever'.
- **Suggested fix:** Keep null/false on failure, because loaded=true with a null state would let ImportYamlModal.tsx:965 skip its replace confirmation. Have every path that publishes an authoritative non-null compositionState also set compositionStateLoaded: true. Move the interpretation refresh before the hydration try, or into a finally gated on isCurrent(). Update the doc comment.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this. I traced the path at 74c0ce0db and it is reachable as described. Commit 7d981d6cf or 07faf477e changed acceptProposal in this window. After a committed POST it now sets compositionState=null and compositionStateLoaded=false (sessionStore.ts:2443-2447). If either hydration GET fails, the inner catch (2455-2460) sets an error banner and returns. That leaves the flag false, and it also skips refreshInterpretationEventsForSession (2475).

Every later publisher of compositionState writes the state without setting the flag. That includes sendMessage (2253-2265), which publishes newState without compositionStateLoaded, and the setters at lines 339, 579, 899, 996, 1061, 2833, 3122, 3162, 3277, 3636, 3759, 3989, 4027, 4118, 4420, 4446, 4620 and 4653. Only createSession (1798), selectSession (2000, 2098), the accept success path (2467) and forkFromMessage (3021) set it true. So the flag stays false until the session is re-selected, created or forked.

The consumers gate on the flag. GraphMiniView.tsx:47-56 renders 'Loading pipeline…' whenever !compositionStateLoaded, even over a non-null published state. The subscriber in useHashRouter.ts:129/148 returns early while the flag is false, so the #/{id}/yaml and #/{id}/spec intents never dispatch. The flag's doc comment at sessionStore.ts:1365-1374 says it is true once the selectSession fetch settled, or once the session was created or forked. The accept path now breaks that. This is a regression: before the window, acceptProposal never touched the flag, and a hydration failure left the earlier (true) value in place.

I lowered the severity to low. It needs a transient GET failure after a committed accept. The error banner at 2458 tells the user to 'Reload the session to refresh it', and re-selecting the session through selectSession fixes everything. No data or audit is lost; the problem is a stuck UI and dead deep links until that manual reload.
