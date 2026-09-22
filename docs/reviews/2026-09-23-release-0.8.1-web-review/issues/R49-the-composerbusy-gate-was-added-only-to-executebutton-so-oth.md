# R49. The `composerBusy` gate was added only to ExecuteButton, so other Run surfaces become silent dead controls

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Execution and run controls |
| Review line | seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-08-execution#3 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx:582-588,643-658`, `CommandPalette.tsx:117-130`, `App.tsx:551-563` and `InlineRunResults.tsx:380-400`.
- **Wrong:** Ctrl+E, the palette and the InlineRunResults "Run pipeline" button dispatch `REQUEST_RUN_EVENT`, which `handleRunClick` drops while the composer is busy. This breaks InlineRunResults' own rule against silent dead controls.
- **Fix:** Share one admissibility predicate that includes `composerBusy` and `isRunBlocked`.
- **Sources:** seam-08-execution#3.
- **Verifier notes:** The sticky bar shows "Wait for the composer…", and the backend leases return 409.


## Source findings and verification

### seam-08-execution#3: composerBusy gate added only to the REQUEST_RUN_EVENT owner, so other Run surfaces become silent dead controls

- **Reported at:** `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx:585`; reviewer severity low; category half-wired; diff-anchored True.
- **Summary:** 5623ceb6d added !composerBusy to ExecuteButton's canExecute. CommandPalette 'Execute pipeline' (CommandPalette.tsx:120-125), Ctrl+E (App.tsx:551-563) and the InlineRunResults 'Run pipeline' button (InlineRunResults.tsx:389) still gate only on execution_ready. They dispatch REQUEST_RUN_EVENT, which handleRunClick drops on !canExecute. This breaks InlineRunResults' own rule against silent dead controls.
- **Failure scenario:** While the composer is thinking with execution_ready true, the Run tab shows an enabled 'Run pipeline' button (or the palette shows Execute enabled). Clicking does nothing and that surface gives no reason. The backend is consistent: exclusive COMPOSE/EXECUTE leases give a 409 'Session operation is already active'.
- **Evidence:** ExecuteButton.tsx:582-588 and handleRunClick `if (!canExecute ...) return`; InlineRunResults.tsx:380-400 comment and executionReady-only gate; CommandPalette.tsx:120-125; App.tsx:551-563.
- **Suggested fix:** Share one admissibility predicate (including composerBusy and isRunBlocked) across every REQUEST_RUN_EVENT dispatcher.
- **Verifier (trace):** upheld, confidence high, severity low. I confirmed this at the pinned commit 74c0ce0db. Commit 5623ceb6d added `!composerBusy` only to ExecuteButton's `canExecute`, at ExecuteButton.tsx:582-588. `handleRunClick` at :643-645 returns early when `!canExecute`, and it is also the handler for REQUEST_RUN_EVENT (:653-658), so a dispatched event is silently dropped while the composer is busy. Three other dispatchers still gate only on `execution_ready`, `isExecuting` and `progress`, and none checks `composerBusy`:
- the palette's "Execute pipeline" `enabled` predicate (CommandPalette.tsx:117-130);
- Ctrl+E (App.tsx:551-563);
- InlineRunResults' empty-state "Run pipeline" button (InlineRunResults.tsx:251-253 and 389-398).

The state the finding needs can really occur. When a compose turn starts, sessionStore.ts:2178-2186 sets `isComposing: true` without calling `clearValidation()`. Validation is cleared only after the response arrives with a changed version (:2219-2223). So during a follow-up turn on a pipeline that already passed validation, `execution_ready` stays true.

The InlineRunResults comment at :383-388 states the rule this breaks: the button is rendered only when the run is admissible, because otherwise it "would be a silent dead control". No commit after 5623ceb6d in the window changes these gates; 29f9af13b touches App.tsx only for a JSX type import. I found no recorded ruling in docs/agents/recent-code-hints.md that makes this deliberate.

The impact is small, so severity stays low. The sticky action bar in the same pane shows "Wait for the composer to finish thinking." (RUN_BLOCK_REASON_TEXT.composing), and the backend's exclusive leases turn any attempt into a 409 rather than a harmful run. The same partial mirroring already existed for `isRunBlocked` (pending interpretations) before this window. The new gate widens that existing gap; it did not create the pattern.
