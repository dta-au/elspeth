# R37. The inline-receipt preservation code and the `addPendingEvent` guard protect paths that have no production caller

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Interpretation events |
| Review line | frontend, seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-07-interpretation#3, fe-11#3 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/stores/interpretationEventsStore.ts:291-294`, `:377-383` and `:543-553`, and the docs at `:140-145` and `:190-198`.
- **Wrong:** Neither `addPendingEvent` nor `refreshPending` has ever had a non-test caller (`git log -S` returns only 1c23037c9). The preservation branch cannot run, and the docstrings describe a compose-loop inline delivery that does not exist.
- **Fix:** Wire the compose response into `addPendingEvent`, or delete the dead paths and correct the docs.
- **Sources:** seam-07-interpretation#3, fe-11#3.
- **Verifier notes:** The comment "exists for inline-add from the compose-loop response path" predates the window. The window added the guard and the loops beneath it.


## Source findings and verification

### seam-07-interpretation#3: Inline-compose-receipt preservation and addPendingEvent guard defend a path with no production caller

- **Reported at:** `src/elspeth/web/frontend/src/stores/interpretationEventsStore.ts:379`; reviewer severity low; category dead-code; diff-anchored True.
- **Summary:** The window added loops in refreshPending (291-294) and refreshAll (379-383) that preserve 'inline compose receipts received after the GET began', plus an opted-out/terminal guard in addPendingEvent (543-553), whose comment says it 'exists for inline-add from the compose-loop response path'. Neither addPendingEvent nor refreshPending has a non-test caller, at the window base or at the tip.
- **Failure scenario:** No runtime misbehaviour. In production, pendingBySession changes between a refreshAll start and its landing only through resolveEvent (removes), optOut (clears) or a newer refreshAll (whose token supersedes). The preservation branch therefore never fires, and the comments describe a delivery path that does not exist. A maintainer who relies on them could assume compose responses push review cards inline when they only arrive via refreshAll.
- **Evidence:** git grep 'addPendingEvent|refreshPending' over src/elspeth/web/frontend/src at 7c986dc97 and 74c0ce0db: only the store and four test files match. All production refreshes are refreshAll: sessionStore.ts:736/2064/3056/3735, executionStore.ts:485.
- **Suggested fix:** Either wire the compose response into addPendingEvent or delete the preservation branches and correct the comments.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this. I traced the code at the pinned commit and it matches the finding. addPendingEvent and refreshPending have no production caller, either at the window base or at the tip. Every production refresh goes through refreshAll. In production, pendingBySession[sid] changes between the start and the landing of a refreshAll in only four ways, and none of them can trigger the "preserve inline compose receipts" branch (`pendingAtStart[id] !== event`). So the new preservation loops (292-294, 381-383) and the new optedOut/terminal guard in addPendingEvent (547-552) only defend a path that tests exercise. One correction to the finding: the comment "exists for inline-add from the compose-loop response path" was already there at the base (7c986dc97:453-455). The window added the guard and the preservation loops underneath it, and wrote a new comment at 379-380 that describes the same delivery path, which does not exist. Nothing misbehaves at runtime, so the severity stays low.

### fe-11#3: New ordering logic protects addPendingEvent/refreshPending, which have no production callers; docs are wrong

- **Reported at:** `src/elspeth/web/frontend/src/stores/interpretationEventsStore.ts:543`; reviewer severity low; category dead-code; diff-anchored True.
- **Summary:** The window added the pendingAtStart merge and guards in addPendingEvent to keep inline compose receipts through older reads. Neither method has ever had a production caller, so the branch cannot run. The addPendingEvent doc ('used when an event arrives via the compose-loop response') and the refreshPending doc ('overwrites') are false.
- **Failure scenario:** A maintainer trusts the docs and the repair note ('Inline events survive older reads'), assumes compose responses push cards inline, and removes or relies on the refreshAll call in refreshInterpretationEventsForSession. Removing it brings back the freeform review-card deadlock.
- **Evidence:** grep for addPendingEvent/refreshPending outside the store hits only *.test.ts(x) files. `git log -S 'addPendingEvent(' -- src/elspeth/web/frontend/src ':!*.test.ts' ':!*.test.tsx'` returns only 1c23037c9 (the commit that created the store). Merge branch at :290-293 and :377-382, docs at :140-145 and :190-198.
- **Suggested fix:** Either wire addPendingEvent to the compose response, or delete addPendingEvent/refreshPending and the pendingAtStart merge, and fix the docstrings.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this finding. addPendingEvent and refreshPending have no production callers. The pendingAtStart merge in refreshAll is also dead in production. That loop only runs when pendingBySession holds an event object that differs from the snapshot taken when this refresh started. The only production code that writes pendingBySession is refreshAll itself, resolveEvent (which only removes entries), optOut (which clears), and the auth reset. Two refreshAll calls cannot interleave to trigger it: the per-session ownership token discards the older response. So nothing adds a new event object while the owning refresh is in flight. The docstrings are out of date. addPendingEvent says it is 'used when an event arrives via the compose-loop response', but nothing calls it. refreshPending says it 'overwrites' the pending map, but after this change it merges local events, drops terminal IDs and respects opt-out. The repair note's claim 'Inline events survive older reads' is true only under test. The failure scenario (a maintainer trusts the docs and removes a refreshAll call) is speculative. What the evidence supports is dead code plus stale docs, so the severity stays low.
