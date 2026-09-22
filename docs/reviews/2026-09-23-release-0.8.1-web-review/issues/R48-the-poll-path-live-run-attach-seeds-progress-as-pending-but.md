# R48. The poll-path live-run attach seeds progress as "pending", but the Run gates check only for "running"

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Execution and run controls |
| Review line | seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-08-execution#2 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/stores/executionStore.ts:960-990`, `ExecuteButton.tsx:587,595` and `CommandPalette.tsx:124`.
- **Wrong:** For a run started elsewhere, Run stays enabled during the backend's pending phase (`service.py:3460-3487`), and a click returns 409 `run_already_active`.
- **Fix:** Gate on the progress being live (not `isTerminalRunStatus`).
- **Sources:** seam-08-execution#2.
- **Verifier notes:** This is not a regression. The gate mismatch predates the window, and the poll attach shortens the period in which Run is wrongly enabled. The 409 is handled with clear copy.


## Source findings and verification

### seam-08-execution#2: Poll-path live-run attach seeds progress 'pending', and Run gates only check 'running'

- **Reported at:** `src/elspeth/web/frontend/src/stores/executionStore.ts:985`; reviewer severity medium; category ui-backend-mismatch; diff-anchored True.
- **Summary:** attachLiveRunIfUnattached now runs from the 3s loadRuns poll (line 921) and seeds progress.status from liveRun.status, which can be 'pending'. ExecuteButton canExecute (ExecuteButton.tsx:587), primaryRunBlockReason and CommandPalette (line 124) block only on status === 'running'. For a run started elsewhere, Run stays enabled during the backend's pending phase.
- **Failure scenario:** Another tab, an agent or the API starts a run. The backend creates it as pending and flips it to running only at service.py:3485, after settings, YAML and graph/plugin build. This tab's Run-tab poll attaches with status 'pending' while execution_ready is true, so Run is enabled with no block reason. The click POSTs /execute and gets a 409 (run_already_active or a session-operation conflict).
- **Evidence:** executionStore.ts:960-990 (new attach, `status: liveRun.status`); ExecuteButton.tsx:587 `progress?.status !== "running"`; ExecuteButton primaryRunBlockReason progressRunning is `=== "running"`; service.py:3480-3487 is the pending→running transition.
- **Suggested fix:** Gate Run on progress being live (not isTerminalRunStatus) instead of === 'running'.
- **Verifier (trace):** upheld, confidence high, severity low. The mechanics check out. At 74c0ce0db, attachLiveRunIfUnattached (executionStore.ts:960-989) seeds `progress.status: liveRun.status`, and that value can be "pending". The loadRuns poll (line 921) now calls it. The Run gates only test "running": ExecuteButton.tsx:587 and :595, and CommandPalette.tsx:124. So while the backend still has the run pending (service.py:3460-3487 builds the graph and preflights sinks before update_run_status("running")), Run is enabled with no block reason. I can't refute that. But the finding overstates it in two ways. (1) It is not a regression from this window. At base 7c986dc97, rehydrateActiveRun already seeded `status: liveRun.status` with pending included (base executionStore.ts:958-977), and ExecuteButton already gated on `!== "running"` (base :580, :588). Also, before 60baa12ad a run started from another tab, an agent or the API was never attached by the poll, so Run stayed enabled for that run's whole pending AND running life. The change shortens how long Run is wrongly enabled; it does not create the problem. diff_anchored=true is only loosely true: the gate mismatch predates the window. (2) The result is harmless. execute() handles a 409 with error_type run_already_active by showing "A run is already in progress for this pipeline." (executionStore.ts:617-624). The backend refuses the second run, so no duplicate run and no audit problem. Once the backend flips to running and the WebSocket or poll updates progress, the gate closes. What remains is a small UI inconsistency: ArtifactWorkspace.tsx:265 treats pending as live and the Run gates do not. It is real, but low severity, not medium.
