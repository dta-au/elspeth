# R63. Comments still describe the removed Workflow-tab approvals table

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Frontend workspace, inspector and stores |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-06#3, fe-10#4, fe-08#2 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/components/inspector/approvalRows.ts:65-67`, `workspace/workspace.css:333-335`, `CatalogButton.tsx:5-12` ("view toggle") and, on an untouched line, `ChatPanel.tsx:3503-3505`.
- **Wrong:** Since 040b89986, ApprovalsView is the only caller. It is still inside an ErrorBoundary, so behaviour is correct.
- **Fix:** Reword the comments to name the Approvals tab and the current toolbar.
- **Sources:** fe-06#3, fe-10#4, fe-08#2.


## Source findings and verification

### fe-06#3: Comments still describe the removed Workflow-tab approvals table

- **Reported at:** `src/elspeth/web/frontend/src/components/inspector/approvalRows.ts:66`; reviewer severity low; category stale-comment; diff-anchored True.
- **Summary:** The approvalRows docstring says it is 'shared by the Workflow tab's Approvals table and the Approvals tab ... both callers sit inside an ErrorBoundary', but since 040b89986 ApprovalsView is the only caller. The workspace.css comment and the CatalogButton docstring ('beside a tablist and a view toggle') also describe UI that no longer exists.
- **Failure scenario:** A maintainer changing the fail-closed throw in approvalRows looks for a second caller or ErrorBoundary that doesn't exist, or reads workspace.css:333-335 and thinks the Workflow tab still renders the approvals disclosure it restyles against.
- **Evidence:** approvalRows.ts:66-68 vs the grep of callers: only ApprovalsView.tsx:20 plus the test. workspace.css:333-335 (added in the window) says 'the Workflow tab's approvals table ... drops the disclosure's own top rule'. CatalogButton.tsx:5-12 says 'view toggle', but the toolbar at ArtifactWorkspace.tsx:569-590 has only History and Plugin catalog. Related, on an untouched line: ChatPanel.tsx:3503 'The graph's Approvals table owns resolved history'.
- **Suggested fix:** Rewrite the three comments to describe the single Approvals-tab consumer and the current toolbar.
- **Verifier (trace):** upheld, confidence high, severity low. I confirmed the finding at pinned commit 74c0ce0db, and it is diff-anchored. approvalRows.ts was created in the window at 6a50bab15. Its docstring (lines 65-67) says the function is "shared by the Workflow tab's Approvals table and the Approvals tab" and that "both callers sit inside an ErrorBoundary". Commit 040b89986 ("remove duplicate approvals disclosure from workflow") also landed in the window. After it, ApprovalsView.tsx:20 is the only production caller; the other call sites are tests in GraphApprovals.test.tsx. There is still an ErrorBoundary (ArtifactWorkspace.tsx:629-634 wraps every active tab), so the fail-closed throw is still contained. Only the "two callers" wording is wrong, so this is a stale comment, not a behaviour bug. The workspace.css comment at lines 333-335 still calls this "the Workflow tab's approvals table" and mentions dropping "the disclosure's own top rule". The Workflow tab no longer renders it, and GraphApprovals.tsx:4 now calls the table "The dedicated Approvals tab's table". The CatalogButton.tsx docstring (lines 5-12) mentions "a tablist and a view toggle", but the toolbar at ArtifactWorkspace.tsx:569-590 has only History and CatalogButton, and the file has no toggle. That file was touched in the window (4811e63e0, 29f9af13b). I found no ruling that would explain the wording. Severity stays low: it misleads maintainers but has no runtime effect.

### fe-10#4: Window-added comments describe a Workflow-tab approvals table that was later deleted

- **Reported at:** `src/elspeth/web/frontend/src/components/workspace/workspace.css:333`; reviewer severity low; category stale-comment; diff-anchored True.
- **Summary:** workspace.css:333-335 ('the Workflow tab's approvals table … drops the disclosure's own top rule') and approvalRows.ts:65-67 ('shared by the Workflow tab's Approvals table and the Approvals tab … both callers') were written in the window. 040b89986 then removed the Workflow-tab disclosure, leaving ApprovalsView as the only caller. ChatPanel.tsx:3503-3505, untouched, still justifies hiding the chat approvals echo with 'The graph's Approvals table owns resolved history'.
- **Failure scenario:** A maintainer trusts approvalRows' two-caller contract or the ChatPanel gating rationale and assumes the Workflow tab still shows approval history. For example, they keep or relax the compositionState!==null chat-echo gate on that false premise.
- **Evidence:** git show 040b89986 removes GraphApprovals and the GraphView render site; `grep approvalRows` shows only ApprovalsView as a caller.
- **Suggested fix:** Reword the three comments to name the Approvals tab as the only surface.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this. The two comments were added in the window, and a later commit in the same window deleted the Workflow-tab approvals disclosure they describe. 6a50bab15 and 543ed1d68 (2026-09-21 03:28 and 03:47) are both ancestors of 040b89986 (22:37 the same day). 040b89986 removed the GraphApprovals render and the approvedInterpretations selector from GraphView. Only the GraphOutputs ErrorBoundary is left under `compositionState &&`. At the pinned commit, ApprovalsView is the only production caller of approvalRows; the other callers are in GraphApprovals.test.tsx. So approvalRows.ts:65-67 ("shared by the Workflow tab's Approvals table and the Approvals tab ... both callers") is false. workspace.css:333-335 names a "Workflow tab's approvals table" and a "disclosure" top rule, and neither exists in the Workflow tab any more. The ChatPanel.tsx:3503-3505 comment ("The graph's Approvals table owns resolved history") was not changed in the window, and it now names a surface that no longer exists. The code itself is still correct: the `compositionState === null` gate still hands resolved history to the Approvals tab, which ArtifactWorkspace.tsx:143 renders. The only harm is a misleading rationale for maintainers, so severity stays low. As a stale-comment finding it is valid.

### fe-08#2: approvalRows docstring names a Workflow-tab caller that no longer exists

- **Reported at:** `src/elspeth/web/frontend/src/components/inspector/approvalRows.ts:65`; reviewer severity low; category stale-comment; diff-anchored True.
- **Summary:** The docstring says the helper is shared by the Workflow tab's Approvals table and the Approvals tab, and that both callers sit inside an ErrorBoundary. After 040b89986 removed GraphApprovals from GraphView, ApprovalsView.tsx:20 is the only production caller.
- **Failure scenario:** A maintainer relying on this fail-closed contract looks for a second ErrorBoundary-wrapped caller in GraphView, or reasons about where a thrown malformed-approval error surfaces, and finds nothing there. No runtime defect: the comment gets the call graph wrong. The same staleness appears in workspace.css:333, also added in the window, and in ChatPanel.tsx:3503, an untouched line.
- **Evidence:** approvalRows.ts:65-67 at 74c0ce0db. grep over src/ finds approvalRows called only from components/workspace/ApprovalsView.tsx:20 and GraphApprovals.test.tsx. The 040b89986 diff removes the GraphApprovals ErrorBoundary block from GraphView.tsx.
- **Suggested fix:** Reword to say the helper serves the Approvals tab (ApprovalsView), whose caller wraps it in an ErrorBoundary. Update the comments at workspace.css:333 and ChatPanel.tsx:3503 to name the Approvals tab.
- **Verifier (trace):** upheld, confidence high, severity low. Confirmed at 74c0ce0db. 6a50bab15 (commit #111 in the window) added the docstring at approvalRows.ts:65-67, which says the helper is shared by the Workflow tab's Approvals table and the Approvals tab and that both callers sit inside an ErrorBoundary. A later commit in the same window, 040b89986 (#86), removed GraphApprovals, its ErrorBoundary("Approvals table") and the selectApprovedInterpretations wiring from GraphView.tsx. Only one production caller is left. That caller is wrapped in a boundary, so the fail-closed behaviour still holds; only the description of who calls the helper is wrong. This has no runtime effect, so it stays at low severity as a stale comment.
