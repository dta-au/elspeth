# R66. The Graph-to-Workflow and Focus graph-to-Fullscreen renames were not carried into the training docs

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Frontend workspace, inspector and stores |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-10#3 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.tsx:46`, `docs/guides/composer-training-one-hour.md:238-240`, the slide HTML (`:286`, `:394`), and `docs/guides/user-manual.md`.
- **Wrong:** Trainers are told to use a "Graph" tab and a "Focus graph" button. Neither exists any more.
- **Fix:** Update to the Workflow tab and the canvas Fullscreen control.
- **Sources:** fe-10#3.
- **Verifier notes:** The Ctrl+Shift+G "full-screen" claim was already wrong before the window (`focusMode:false`). Losing one-click full-screen from the other tabs is a deliberate design change.


## Source findings and verification

### fe-10#3: Graph→Workflow and Focus graph→Fullscreen rename not carried into training docs

- **Reported at:** `src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.tsx:46`; reviewer severity low; category stale-docs; diff-anchored True.
- **Summary:** The tab is now labelled "Workflow" and the Focus graph button is gone. The training guide and slides, which bfda1fb16 edited in this same window, still direct users to a `Graph` tab and a `Focus graph` button, and say Ctrl+Shift+G gives a full-screen view. It does not: CommandPalette dispatches focusMode:false.
- **Failure scenario:** A trainer following Slide 12 (composer-training-one-hour.md:238-240, slides html :286/:394) tells learners to click 'Graph' and 'Focus graph'. Neither control exists, and Ctrl+Shift+G only switches tabs. The rename also removed the one-click full-screen route from the Spec, YAML, Checks and Run tabs.
- **Evidence:** docs/guides/composer-training-one-hour.md:240 reads '`Focus graph` (or `Ctrl+Shift+G`) for a full-screen view'; CommandPalette.tsx:170-185 sends focusMode:false. Stale test comments: modal-flow.spec.ts:365, workspace-assertions.ts:162, workspaceChrome.test.ts:248.
- **Suggested fix:** Update the guide and slides to the Workflow tab and the Fullscreen canvas control (under fit view), and fix the Ctrl+Shift+G claim.
- **Verifier (trace):** upheld, confidence high, severity low. I confirmed the finding at 74c0ce0db, with two corrections. The core defect holds: in this window, 6a50bab15 renamed the visible tab from "Graph" to "Workflow", and 4811e63e0 and a7e54492e replaced the toolbar's "Focus graph" button with a "Fullscreen" control in the graph canvas. The training guide and slides were not updated to match. bfda1fb16 touched the guide in the same window, but only its tutorial-prompt line. Correction 1: the Ctrl+Shift+G claim is wrong, but that inaccuracy is older than the window. At 7c986dc97 the shortcut already sent focusMode:false (base App.tsx:495), so the guide was already wrong on that point before the window. Correction 2: losing one-click full-screen from the Spec, YAML, Checks and Run tabs is a deliberate design change in the window's commits ("Fullscreen ... on the Workflow tab"), not a defect. The finding also missed a stale spot: docs/guides/user-manual.md still describes "Graph" and "Focus Graph". The stale docs mislead trainers but do not stop anyone using the product, so severity stays low.
