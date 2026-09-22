# R20. E2E specs click or assert a Fullscreen control that is not rendered on an empty pipeline

| | |
|---|---|
| Severity | medium |
| Status | confirmed |
| Area | — |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-10#1 |

## Finding

- **Status and severity:** confirmed, **medium**. The defect is in the e2e gate; users are not affected.
- **Location:**
  - `src/elspeth/web/frontend/tests/e2e/modal-flow.spec.ts:107`.
  - `composer-workspace-geometry.spec.ts:296` together with `workspace-assertions.ts:170`.
  - Product side: `GraphView.tsx:2099-2108` returns the empty state before `<Controls>`, and `GraphView.tsx:2232` holds the ControlButton.
- **What is wrong:** The window removed the always-rendered toolbar "Focus graph" button (4811e63e0, a7e54492e). The only remaining openers are the React Flow "Fullscreen" ControlButton, which needs nodes, and the one in GuidedGraphPane, which needs a guided projection. Two specs were renamed to "Fullscreen" but still expect it on empty sessions. 4811e63e0's message says the specs "were renamed to match but were not run".
- **Failure scenario:**
  - (A) The modal-flow test "explicit Focus Graph action opens the Graph modal" creates a bare session. `getByRole('button', {name: 'Fullscreen'}).click()` times out.
  - (B) In the empty-freeform geometry scenario, `expectControlReachable(composer.focusGraph())` fails at all five desktop viewports.
  - CI runs `npm run test:e2e` (`ci.yaml:1268`). Retries cannot pass these tests, because the control is never rendered.
- **Suggested fix:** In modal-flow, seed a composition before clicking, and add a separate assertion that an empty pipeline has no Fullscreen control. In the geometry specs, add a fullscreen capability flag that is false for empty-freeform and assert `toHaveCount(0)`. The alternative is to restore an opener that exists in every graph state, as 4811e63e0 originally intended. Either way, choose deliberately.
- **Sources:** fe-10#1.
- **Verifier notes:** Frontend e2e specs were outside the bundle scope. This finding was reached from the product-side change.



## Source findings and verification

### fe-10#1: E2E specs click or assert a Fullscreen control that is not rendered on an empty pipeline

- **Reported at:** `src/elspeth/web/frontend/tests/e2e/modal-flow.spec.ts:107`; reviewer severity medium; category test-regression; diff-anchored True.
- **Summary:** This is an e2e gate regression (a half-wired change), not a user-facing bug. The window removed the always-rendered toolbar "Focus graph" button (ArtifactWorkspace.tsx:569-590). The only remaining openers are GraphView's React Flow ControlButton "Fullscreen" (GraphView.tsx:2232), which exists only when the graph has nodes, and GuidedGraphPane's button, which needs a guided projection. Two e2e consumers were retargeted to "Fullscreen" in the window but still expect it on empty sessions.
- **Failure scenario:** (A) modal-flow.spec.ts, test 'explicit Focus Graph action opens the Graph modal': it creates a fresh session with a bare POST /api/sessions and seeds no composition, so GraphView renders the 'No pipeline to visualise.' empty state. getByRole('button',{name:'Fullscreen'}).click() then times out on every attempt. (B) composer-workspace-geometry.spec.ts:296 calls expectPrimaryControlsInViewport, which runs expectControlReachable(composer.focusGraph()) unconditionally (workspace-assertions.ts:170); that helper begins with toBeVisible(). The empty-freeform scenario seeds compositionState=null and first asserts that the empty-state text IS visible (spec:76-81), so the Fullscreen assertion fails at all 5 desktop viewports. Both specs are collected by CI's `npm run test:e2e`, and retries cannot pass them.
- **Evidence:** 4811e63e0's message: 'stays reachable in every graph state … E2E specs are renamed to match but were not run.' a7e54492e's message: 'There is no control while the pipeline is empty.' A grep for the literal "Fullscreen" in src/**/*.tsx (tests excluded) finds only GuidedGraphPane.tsx:28 and GraphView.tsx:2233. GraphView.tsx:2099-2108 returns the empty state before <Controls>. workspace-fixtures.ts:636-638 seeds null for empty-freeform. helpers/api.ts:50-61 createSession seeds nothing.
- **Suggested fix:** modal-flow: seed a composition before clicking Fullscreen, and add a separate assertion that an empty pipeline has no Fullscreen control. Geometry: add a fullscreen capability flag that is false for empty-freeform and assert toHaveCount(0) there. The alternative is to restore an opener that exists in every graph state, as 4811e63e0 intended; choose one deliberately.
- **Verifier (trace):** upheld, confidence high, severity medium. I could not refute this. I checked every guard and branch on the path, and at the pinned commit neither spec can find a Fullscreen control on an empty pipeline. Before the window, the toolbar's "Focus graph" button was always rendered (7c986dc97 ArtifactWorkspace.tsx:586-588, with no condition). The window removed it and moved the opener into the graph canvas controls. GraphView returns the empty state before it renders <Controls>, so an empty pipeline has no Fullscreen button. The only other Fullscreen button is in GuidedGraphPane, which appears only when a guided projection exists. The empty-freeform scenario rules that branch out: it asserts that the empty-state text is visible, and GraphView shows that text only when no guided pane is drawn. modal-flow's fresh session has no composition and no guided turn. The specs changed in the window (4811e63e0, a7e54492e, 29f9af13b) only swapped the button name from "Focus graph" to "Fullscreen". They did not add a seeded composition or a condition. CI runs these specs: ci.yaml:1268 runs `npm run test:e2e`, and playwright.config.ts testIgnore excludes only setup, page-objects, helpers, *.test.ts and *.staging.spec.ts. CI's retries (2) cannot help, because the button is never rendered. It is a broken e2e gate, not a user-facing bug, so medium stays.
