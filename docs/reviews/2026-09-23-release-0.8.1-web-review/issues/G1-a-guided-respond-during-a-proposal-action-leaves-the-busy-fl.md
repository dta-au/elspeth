# G1. A guided respond during a proposal action leaves the busy flag stuck

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Guided lane (retiring) |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-12#3 |

## Finding

- **Location:** `frontend/src/stores/sessionStore.ts:2510-2518,2575-2583,3368`
- **Finding and suggested disposition:** 7d981d6cf changed the accept/reject `finally` to clear `proposalActionPendingIds` only when `isCurrent()`, which also checks the guided generation. `respondGuided`, `startGuided`, `seedGuided` and `chatGuided` advance that generation without clearing the flag. Accept and Reject can then stay disabled until the session is re-selected. The fix is local to the freeform store: clear the pending id whenever `activeSessionId` still matches, and keep the generation check only on publication writes.
- **Sources:** fe-12#3.

## Source findings and verification

### fe-12#3: Guided respond during proposal action strands busy flag

- **Reported at:** `src/elspeth/web/frontend/src/stores/sessionStore.ts:2510`; reviewer severity low; category regression; diff-anchored True.
- **Summary:** The accept and reject isCurrent() checks now also fence on guidedPublicationGeneration (2416-2419, 2522-2526). Their finally blocks (2510-2518, 2575-2583) clear proposalActionPendingIds only when isCurrent() is true. respondGuided (3368), startGuided, seedGuided and chatGuided advance the generation within the same session without clearing proposalActionPendingIds, so the busy id is never removed. Before the window, the finally checked only the session id.
- **Failure scenario:** In a guided session that shows a pending proposal in DecisionPanel, the user confirms Reject and answers a guided turn before the reject POST settles. respondGuided advances the generation, so the reject's error or receipt handling and its pending cleanup are all skipped. If the proposal is still pending (for example, the reject returned a lease 409), its Accept and Reject stay disabled={isBusy} until the session is re-selected, and no error is shown.
- **Evidence:** sessionStore.ts:3368 advances the generation unconditionally on each respond. The resets of proposalActionPendingIds are only at 1802, 1872, 1938, 2080, 2115, 2142 and 3025 (activation paths). DecisionPanel.tsx:329-330.
- **Suggested fix:** In the finally, clear the pending id whenever get().activeSessionId === activeSessionId, and keep the generation fence only on the publication writes. This is a local fix in the freeform store and needs no investment in the guided lane.
- **Verifier (trace):** upheld, confidence medium, severity low. I traced the claimed path at 74c0ce0db and could not refute it. Both proposal actions capture guidedPublicationGeneration. Their finally blocks now remove the busy id only when isCurrent() is true, and isCurrent() requires the generation to be unchanged. Before the window, the finally checked only the session id (commit 7d981d6cf changed `get().activeSessionId === activeSessionId` to `isCurrent()`). advanceGuidedPublicationGeneration() runs in respondGuided (3368), startGuided (3142), seedGuided (3185), chatGuided's cold-start branch (4161) and reset (4721). None of those paths clears proposalActionPendingIds. The only resets are on session activation or selection (1802, 1872, 1938, 2080, 2115, 2142, 3025). So an in-flight accept or reject that settles after a same-session guided action leaves its id in proposalActionPendingIds for good. It also skips the proposal merge and the error it would have shown, so the proposal can keep rendering as pending with both buttons disabled until the user re-selects the session.

The path is reachable. Guided sessions do carry pending composition proposals: the Step-3 and guided-full proposals, whose anchors guided_proposal_rebase.py moves. The guided branches of ChatPanel render the same DecisionPanel ({decisionPanel} at ChatPanel.tsx:2706 and 3267). The GuidedTurn widget's onSubmit calls respondGuided (ChatPanel.tsx:2999) with no gate on proposalActionPendingIds.

The severity stays low for three reasons. The race needs the user to start a guided action inside the latency window of a proposal POST. It only affects the guided lane, which is being retired. Re-selecting the session recovers it. No automatic effect advances the generation after an accept or reject: the startGuided call at ChatPanel.tsx:996 sits inside a user retry handler.
