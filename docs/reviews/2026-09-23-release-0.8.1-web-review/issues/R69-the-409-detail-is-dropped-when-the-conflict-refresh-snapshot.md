# R69. The 409 detail is dropped when the conflict-refresh snapshot is superseded

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Frontend workspace, inspector and stores |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-12#2 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/stores/sessionStore.ts:604-619`.
- **Wrong:** The error is published only while the refresh's own snapshot is current. A concurrent successful accept or reject on another proposal starts a newer snapshot, so the 409 fails silently.
- **Fix:** Set the error whenever `isCurrent()`, and keep the snapshot guard on the proposal-list writes only. Assert the error in the existing test.
- **Sources:** fe-12#2.


## Source findings and verification

### fe-12#2: 409 detail dropped when conflict refresh snapshot is superseded

- **Reported at:** `src/elspeth/web/frontend/src/stores/sessionStore.ts:604`; reviewer severity low; category error-handling; diff-anchored True.
- **Summary:** reconcileProposalConflict publishes the 409's error detail only while its own list snapshot is still current (604, 613, 614-619). acceptProposal and rejectProposal clear error at dispatch. So if any newer proposal-list read starts before the conflict refresh returns, the user gets no message.
- **Failure scenario:** Pending proposals A and B are both visible. Accept on A returns 409 'Session operation is already active'. While its refresh GET is in flight, the user confirms Reject on B, which succeeds and starts a newer snapshot. A's refresh resolves, snapshot.isCurrent() is false, and the function returns. error stays null and A's Accept re-enables silently, so the accept did nothing and the user is not told.
- **Evidence:** sessionStore.ts:604 `if (!isCurrent() || !snapshot.isCurrent()) return;`. The error is set only at 613, or at 614-619 under the same guard, and is cleared at 2424-2429. The window's test 'does not let an older conflict refresh retire a proposal found by a newer read' runs exactly this sequence (409 'Busy', then a newer loadCompositionProposals) and never asserts error. Lease 409: session_operation_handlers.py:28-30. DecisionPanel.tsx:274-275 and 329-330 disable buttons per proposal only.
- **Suggested fix:** Set the error from error.detail whenever isCurrent() is true, whether or not the snapshot is still current. Keep the snapshot guard only on the compositionProposals and staleProposalIds writes. Assert error in the existing test.
- **Verifier (trace):** upheld, confidence medium, severity low. I could not refute this. The code at 74c0ce0db behaves as the finding says. reconcileProposalConflict, which is new in this window (commits 7d981d6cf and 07faf477e), sets error.detail only when the refresh's own snapshot is still current. The success branch returns early at sessionStore.ts:604, and the catch branch at 614-619 has the same guard. The 409 detail describes the action that failed, not the refresh read, yet it is dropped whenever any newer proposal snapshot has started. No recorded ruling covers this, and the commit message gives no rationale.

The path is reachable, but only through a narrow timing race. At the pinned tree, beginProposalSnapshot is called from four places: loadCompositionProposals (2403), accept success (2450), reject success (2548) and the conflict function itself (601). Outside tests, loadCompositionProposals is called only from the 422 branch of accept (2492). advanceGuidedPublicationGeneration also bumps the sequence, but it breaks isCurrent() as well, so it is not the snapshot-only case.

So the error is lost only when three things happen in order. First, another proposal's accept or reject POST succeeds. Second, it starts its refresh snapshot. Third, A's conflict refresh GET is still in flight at that point.

The most natural trigger is proposal B's Accept holding the session lease. The user clicks Accept on A, which returns 409 "Session operation is already active". B's POST then completes and begins its snapshot before A's GET resolves. DecisionPanel.tsx:274-275 and 329-330 disable buttons per proposal only, so A can be clicked while B is busy.

When this happens, both dispatches have already cleared error (2424-2429 for accept, 2522-2527 for reject). B's success path sets no error, and A's refresh returns at 604. A stays pending and not stale, so its Accept button re-enables with no message. That is the reported outcome.

The window is roughly one GET round-trip, and the user can simply click again, so low severity stands. The test at sessionStore.test.ts:2774 exercises this exact sequence and does not assert error, which matches the finding.
