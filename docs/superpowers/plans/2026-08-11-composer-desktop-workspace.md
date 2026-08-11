# Composer Desktop Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the chat-dominated Web Composer shell with a persistent, accessible artifact-first workspace that is fully operable from 1280×720 through 2560×1280 while preserving existing narrow reflow behavior.

**Architecture:** A common `ComposerWorkspace` will own authoring-pane geometry, a persistent Graph/Spec/YAML/Run artifact area, a single validation/audit/history inspector, and the existing completion actions. `ChatPanel` remains the sole owner of freeform and guided conversation semantics; the workspace only projects existing authoritative stores and components. Layout preferences are browser-local UI state, while pipeline, validation, audit, execution, guided, proposal, and custody semantics remain unchanged.

**Tech Stack:** React 18.3, TypeScript, Zustand, CSS Grid/Flexbox, React Flow, Vitest/Testing Library/jest-axe, Playwright/Chromium, Python/FastAPI test backend.

**Approved design:** `docs/superpowers/specs/2026-08-11-composer-desktop-workspace-design.md`

**Prerequisites:**

- Execute only in `/home/john/elspeth/.claude/worktrees/composer-1080p-wqhd-plan` on `codex/composer-1080p-wqhd-plan`. Never switch, edit, merge, or run dependency setup in `/home/john/elspeth` while engine fixes are landing there.
- The worktree directory is already ignored by `.gitignore`. Its `.venv` and frontend `node_modules` are real worktree-local directories, not symlinks.
- Before every execution session, run `git status --short --branch` in both the worktree and main checkout. Preserve all unrelated main-checkout changes.
- Verify the interpreter before trusting Python or Playwright: `.venv/bin/python -c 'import elspeth; print(elspeth.__file__)'` must print a path below this worktree.
- If dependencies must be refreshed, run `uv sync --frozen --all-extras` from the worktree root and `npm ci` from `src/elspeth/web/frontend`. Never share or symlink the main checkout's `.venv` or `node_modules`.
- Read `docs/agents/recent-code-hints.md` before source edits. Do not use Loomweave from the linked worktree; use live file inspection there.
- Before the first source edit, capture the current key-free trust-tier corpus with the exact command below. Exit 0 or the documented fail-closed exit 1 is admissible; any other exit blocks implementation. Keep these ignored, task-owned artifacts until Task 11 compares and removes them. Do not acquire signing keys or edit judge signatures.

```bash
mkdir -p .codex/composer-1080p
set +e
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  .venv/bin/elspeth-lints check --rules all --root src/elspeth --format json \
  > .codex/composer-1080p/trust-tier-before.raw.json \
  2> .codex/composer-1080p/trust-tier-before.stderr
TRUST_TIER_BEFORE_EXIT=$?
set -e
test "$TRUST_TIER_BEFORE_EXIT" -eq 0 || test "$TRUST_TIER_BEFORE_EXIT" -eq 1
jq -S 'sort_by(.)' .codex/composer-1080p/trust-tier-before.raw.json \
  > .codex/composer-1080p/trust-tier-before.json
```
- The focused baseline is currently green: `composer-reflow.spec.ts` has 2 passing tests at 320×256 and 375×667.
- Unless a step explicitly says otherwise, run `npm`, `npx`, and frontend test commands from `src/elspeth/web/frontend`; run Python, Git, trust-tier, and Wardline commands from the worktree root.

---

## Invariants for Every Task

- Keep `ComposerWorkspace` mounted across freeform/guided mode changes. Do not key it or `ChatPanel` by mode; doing so loses drafts, scroll position, focus ownership, and in-flight guided request custody.
- Mount only one live `GraphView`, one live `YamlView`, and one `InlineRunResults` owner in the normal Composer workspace. The graph modal may mount its second `GraphView` only while focus mode is open.
- Conditionally mount only the active artifact panel. Do not keep inactive panels mounted with `hidden`: a zero-sized hidden Graph is unsafe to initialize, YAML mount fetches and mutates the export binding, and Run mount starts history polling. Test request counts, poll cleanup, and session-switch remount behavior.
- Keep `AcknowledgementLiveRegion` outside the changing guided scroller and keep the current guided decision inside that scroller.
- Keep `isGuidedBuildActive()` as run-admission authority. An active guided build and the tutorial must not acquire ordinary Run/Save actions.
- Do not move or alter backend/API, guided state-machine, proposal, mutation, acknowledgement, audit, custody, validation, or execution logic.
- Use rendered browser geometry as responsive proof. Vitest runs with `css: false`; CSS source-string assertions are not adequate acceptance evidence.
- Stage and commit only the files named by the current task.

## Task 1: Add Browser-Local Workspace Pane State

**Files:**

- Create: `src/elspeth/web/frontend/src/components/workspace/workspaceTypes.ts`
- Create: `src/elspeth/web/frontend/src/components/workspace/useWorkspacePaneState.ts`
- Create: `src/elspeth/web/frontend/src/components/workspace/useWorkspacePaneState.test.ts`

- [ ] **Step 1: Write the failing pane-state tests**

Cover missing, valid, corrupt, wrong-version, storage-read failure, and storage-write failure cases; width bounds; compact default; clamp-without-overwriting-preference; collapse persistence; tab fallback; and session reset.

```ts
import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import {
  clampAuthoringWidth,
  nearestAvailableArtifactTab,
  paneBoundsForWidth,
  readStoredWorkspaceLayout,
  useWorkspacePaneState,
  WORKSPACE_LAYOUT_STORAGE_KEY,
} from "./useWorkspacePaneState";

describe("workspace pane state", () => {
  beforeEach(() => localStorage.clear());

  it("defaults compact desktops to 360px and standard desktops to 420px", () => {
    expect(paneBoundsForWidth(1280).defaultWidth).toBe(360);
    expect(paneBoundsForWidth(1536).defaultWidth).toBe(420);
  });

  it("preserves a preferred width while clamping the effective width", () => {
    localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE_KEY,
      JSON.stringify({ version: 1, preferredAuthoringWidth: 620, authoringCollapsed: false }),
    );
    const { result, rerender } = renderHook(
      ({ width }) => useWorkspacePaneState({ workspaceWidth: width, sessionId: "s1" }),
      { initialProps: { width: 1600 } },
    );
    expect(result.current.effectiveAuthoringWidth).toBe(620);
    rerender({ width: 1100 });
    expect(result.current.preferredAuthoringWidth).toBe(620);
    expect(result.current.effectiveAuthoringWidth).toBe(460);
  });

  it("rejects corrupt local data without throwing", () => {
    localStorage.setItem(WORKSPACE_LAYOUT_STORAGE_KEY, "not-json");
    expect(readStoredWorkspaceLayout(localStorage)).toBeNull();
  });

  it("falls back to graph when a selected tab becomes unavailable", () => {
    expect(nearestAvailableArtifactTab("yaml", ["graph", "run"])).toBe("graph");
  });
});
```

- [ ] **Step 2: Run the tests and verify RED**

Run from `src/elspeth/web/frontend`:

```bash
npm test -- src/components/workspace/useWorkspacePaneState.test.ts
```

Expected: FAIL because the workspace state modules do not exist.

- [ ] **Step 3: Implement the typed state and persistence contract**

Use this public shape:

```ts
// workspaceTypes.ts
export const ARTIFACT_TABS = ["graph", "spec", "yaml", "run"] as const;
export type ArtifactTab = (typeof ARTIFACT_TABS)[number];
export type InspectorTab = "validation" | "audit" | "history";

export interface PaneBounds {
  min: number;
  max: number;
  defaultWidth: number;
  resizable: boolean;
}

export interface StoredWorkspaceLayoutV1 {
  version: 1;
  preferredAuthoringWidth: number;
  authoringCollapsed: boolean;
}
```

```ts
// useWorkspacePaneState.ts — constants and pure boundary functions
export const WORKSPACE_LAYOUT_STORAGE_KEY = "elspeth_composer_workspace_layout_v1";
export const AUTHORING_MIN = 360;
export const AUTHORING_MAX = 640;
export const ARTIFACT_MIN = 640;
export const STANDARD_DESKTOP_MIN = 1536;

export function paneBoundsForWidth(workspaceWidth: number): PaneBounds {
  const max = Math.max(
    AUTHORING_MIN,
    Math.min(AUTHORING_MAX, workspaceWidth - ARTIFACT_MIN),
  );
  return {
    min: AUTHORING_MIN,
    max,
    defaultWidth: workspaceWidth < STANDARD_DESKTOP_MIN ? AUTHORING_MIN : 420,
    resizable: workspaceWidth >= AUTHORING_MIN + ARTIFACT_MIN && max > AUTHORING_MIN,
  };
}

export function clampAuthoringWidth(preferred: number, bounds: PaneBounds): number {
  return Math.min(bounds.max, Math.max(bounds.min, preferred));
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

export function readStoredWorkspaceLayout(storage: Storage): StoredWorkspaceLayoutV1 | null {
  try {
    const raw = storage.getItem(WORKSPACE_LAYOUT_STORAGE_KEY);
    if (raw === null) return null;
    const value: unknown = JSON.parse(raw);
    if (
      !isPlainRecord(value) ||
      !Object.keys(value).every((key) =>
        ["version", "preferredAuthoringWidth", "authoringCollapsed"].includes(key),
      ) ||
      value.version !== 1 ||
      typeof value.preferredAuthoringWidth !== "number" ||
      !Number.isFinite(value.preferredAuthoringWidth) ||
      value.preferredAuthoringWidth < AUTHORING_MIN ||
      value.preferredAuthoringWidth > AUTHORING_MAX ||
      typeof value.authoringCollapsed !== "boolean"
    ) return null;
    return {
      version: 1,
      preferredAuthoringWidth: value.preferredAuthoringWidth,
      authoringCollapsed: value.authoringCollapsed,
    };
  } catch {
    return null;
  }
}
```

`useWorkspacePaneState()` must:

- initialize preferred width/collapse from validated storage or the current viewport default;
- retain preferred width separately from effective clamped width;
- persist only after `commitResize(finalWidth)` or a collapse/restore action;
- catch storage failures silently and keep the in-memory state;
- keep active artifact and inspector tabs ephemeral;
- reset the artifact tab to Graph and close the inspector when `sessionId` changes;
- choose Graph when Spec/YAML becomes unavailable;
- expose `resizeTransient`, `commitResize`, `setAuthoringCollapsed`, `selectArtifactTab`, `openInspector`, and `closeInspector`.
- treat an initial `ResizeObserver` width of zero as not measured: do not replace the stored/default preference until the first positive width arrives.
- validate storage as untrusted input: reject null, arrays, invalid/prototype-bearing shapes, wrong versions, non-finite/out-of-range widths, and wrong booleans; always construct a fresh owned value.

- [ ] **Step 4: Run the focused tests and verify GREEN**

```bash
npm test -- src/components/workspace/useWorkspacePaneState.test.ts
```

Expected: all pane-state tests pass.

- [ ] **Step 5: Commit the state slice**

```bash
git add src/elspeth/web/frontend/src/components/workspace/workspaceTypes.ts \
  src/elspeth/web/frontend/src/components/workspace/useWorkspacePaneState.ts \
  src/elspeth/web/frontend/src/components/workspace/useWorkspacePaneState.test.ts
git diff --cached --name-only
git commit -m "feat(web): add Composer workspace pane state"
```

**Definition of Done:** browser-local state is validated, failure-safe, unit-tested, and independent of pipeline/audit persistence.

## Task 2: Build the Accessible Two-Pane Workspace Shell

**Files:**

- Create: `src/elspeth/web/frontend/src/components/workspace/WorkspaceSeparator.tsx`
- Create: `src/elspeth/web/frontend/src/components/workspace/WorkspaceSeparator.test.tsx`
- Create: `src/elspeth/web/frontend/src/components/workspace/WorkspacePaneContext.tsx`
- Create: `src/elspeth/web/frontend/src/components/workspace/WorkspacePaneContext.test.tsx`
- Create: `src/elspeth/web/frontend/src/components/workspace/ComposerWorkspace.tsx`
- Create: `src/elspeth/web/frontend/src/components/workspace/ComposerWorkspace.test.tsx`
- Create: `src/elspeth/web/frontend/src/components/workspace/workspace.css`
- Modify: `src/elspeth/web/frontend/src/styles/index.css`

- [ ] **Step 1: Write failing separator and shell tests**

Assert DOM order, one named authoring pane, one artifact pane, collapse/restore, narrow-view switching, independent error boundaries, ResizeObserver clamping, and the complete separator keyboard contract.

```tsx
it("resizes with arrows, Shift, Home, and End", async () => {
  const user = userEvent.setup();
  const onResize = vi.fn();
  const onResizeEnd = vi.fn();
  render(
    <WorkspaceSeparator
      value={420}
      min={360}
      max={640}
      disabled={false}
      onResize={onResize}
      onResizeEnd={onResizeEnd}
    />,
  );
  const separator = screen.getByRole("separator", { name: "Resize authoring pane" });
  expect(separator).toHaveAttribute("aria-orientation", "vertical");
  expect(separator).toHaveAttribute("aria-valuenow", "420");
  await user.type(separator, "{ArrowRight}");
  expect(onResize).toHaveBeenLastCalledWith(436);
  await user.keyboard("{Shift>}{ArrowRight}{/Shift}");
  expect(onResize).toHaveBeenLastCalledWith(468);
  await user.keyboard("{Home}");
  expect(onResize).toHaveBeenLastCalledWith(360);
  await user.keyboard("{End}");
  expect(onResize).toHaveBeenLastCalledWith(640);
  expect(onResizeEnd.mock.calls).toEqual([[436], [468], [360], [640]]);
});
```

- [ ] **Step 2: Verify RED**

```bash
npm test -- src/components/workspace/WorkspaceSeparator.test.tsx src/components/workspace/ComposerWorkspace.test.tsx
```

Expected: FAIL because the components do not exist.

- [ ] **Step 3: Implement `WorkspaceSeparator`**

Render a focusable `role="separator"` with `aria-orientation="vertical"`, `aria-valuemin`, `aria-valuemax`, and `aria-valuenow`. Use 16px Arrow steps, 48px Shift+Arrow steps, Home/End bounds, pointer capture, and `requestAnimationFrame`-bounded pointer moves. Define `onResizeEnd(finalWidth: number)` and pass the same final, clamped pixel value on keyboard completion, pointer-up, and pointer-cancel; do not depend on an asynchronously committed React state value. The visible seam is 1px; its absolute hit target is at least 24×24px and does not consume the 640px artifact minimum.

- [ ] **Step 4: Implement `ComposerWorkspace`**

Use this public interface:

```tsx
interface ComposerWorkspaceProps {
  authoring: React.ReactNode;
  artifact: React.ReactNode;
  inspector: React.ReactNode;
  actionBar: React.ReactNode;
  authoringStatus?: React.ReactNode;
  collapsedStatus?: { text: string; tone: "neutral" | "busy" | "error" };
}
```

`ComposerWorkspace` owns the `ResizeObserver`, `useWorkspacePaneState`, separator, collapse/restore control, and the narrow Compose/Pipeline view state. Publish the hook result through `WorkspacePaneContext`; `ArtifactWorkspace`, `WorkspaceInspector`, and `WorkspaceActionBar` consume that single controller rather than creating independent tab/inspector state. The context must throw a named developer error when consumed outside `ComposerWorkspace`, and its tests pin that failure plus identity-stable action access.

Render the semantic DOM in this exact order: mobile view tablist, authoring pane, separator, artifact pane (artifact content followed by action bar), inspector. Keep the existing authoring `ErrorBoundary` here. Tasks 3 and 4 place boundaries around only the active artifact body and inspector body respectively, so tab navigation, status controls, action bar, inspector tabs, and Close remain outside a failing child. Key those body boundaries by session plus active tab. Name the controls `Collapse authoring pane`, `Restore authoring pane`, `Compose`, and `Pipeline`. At `<960px`, the Compose/Pipeline controls use the complete roving ARIA tab pattern and the separator is unfocusable.

When collapsed, keep the authoring subtree mounted so requests/drafts/custody survive, but make its container `inert`, `aria-hidden="true"`, and visually absent so it exposes neither duplicate live regions nor hidden focus targets. Render `collapsedStatus.text` in the visible collapsed affordance, include it in the restore control's accessible description, and make that projection the sole announcement owner while collapsed. Test neutral, freeform/guided in-progress, error, and new-message/acknowledgement updates; no hidden authoring descendant may receive focus, and collapse must not abort an in-flight operation.

Use CSS custom property `--authoring-pane-width` on the workspace root. The desktop grid is `var(--authoring-pane-width) minmax(0, 1fr)` with the separator overlaid on the seam; the state bounds reserve the 640px artifact minimum whenever the workspace is at least 1000px wide. Between 960px and 999px, use the approved compact fallback of 360px authoring plus the remaining artifact width without document overflow. Below 960px, show one main view at a time, default to Compose, hide the separator, and keep both view-tab controls reachable. Pin the inclusive behavior at widths 959, 960, 961, 999, and 1000 in unit tests. At every five desktop acceptance viewport, the active artifact content area must retain at least 420px height.

- [ ] **Step 5: Import workspace CSS**

Add `@import "../components/workspace/workspace.css";` in `src/styles/index.css` before chat/guided overrides and before `themes.css`.

- [ ] **Step 6: Verify GREEN**

```bash
npm test -- src/components/workspace/WorkspaceSeparator.test.tsx \
  src/components/workspace/WorkspacePaneContext.test.tsx \
  src/components/workspace/ComposerWorkspace.test.tsx
npm run typecheck
npm run lint:css
```

Expected: focused tests, TypeScript, and Stylelint pass.

- [ ] **Step 7: Commit the shell slice**

```bash
git add src/elspeth/web/frontend/src/components/workspace/WorkspaceSeparator.tsx \
  src/elspeth/web/frontend/src/components/workspace/WorkspaceSeparator.test.tsx \
  src/elspeth/web/frontend/src/components/workspace/WorkspacePaneContext.tsx \
  src/elspeth/web/frontend/src/components/workspace/WorkspacePaneContext.test.tsx \
  src/elspeth/web/frontend/src/components/workspace/ComposerWorkspace.tsx \
  src/elspeth/web/frontend/src/components/workspace/ComposerWorkspace.test.tsx \
  src/elspeth/web/frontend/src/components/workspace/workspace.css \
  src/elspeth/web/frontend/src/styles/index.css
git diff --cached --name-only
git commit -m "feat(web): add accessible Composer workspace shell"
```

**Definition of Done:** the common shell has bounded pane geometry, keyboard/pointer resize, collapse/restore, narrow switching, and pane-level failure isolation.

## Task 3: Add Persistent Graph, Spec, YAML, and Run Artifacts

**Files:**

- Create: `src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.tsx`
- Create: `src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.test.tsx`
- Create: `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx`
- Create: `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/execution/InlineRunResults.tsx`
- Modify: `src/elspeth/web/frontend/src/components/execution/InlineRunResults.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/inspector/YamlView.tsx`
- Modify: `src/elspeth/web/frontend/src/components/inspector/YamlView.test.tsx`
- Modify: `src/elspeth/web/frontend/src/lib/composer-events.ts`

- [ ] **Step 1: Write failing artifact tests**

Test the ARIA tab relationships, roving focus, Arrow/Home/End navigation, Graph default/empty state, Spec and YAML availability, Run empty state, fallback announcement/focus, Focus Graph action, and tab-selection events.

Use a shared `renderArtifactWorkspace()` test helper that mounts the real `ComposerWorkspace` provider around `ArtifactWorkspace`. Assert the Graph tab starts with `aria-selected="true"` and `aria-controls="artifact-panel-graph"`; clicking Spec moves roving focus/selection and exposes the correspondingly labelled tabpanel. For focus mode, attach a one-shot `OPEN_GRAPH_MODAL_EVENT` listener, dispatch an artifact request, then assert Graph has focus before the modal event count becomes one.

- [ ] **Step 2: Verify RED**

```bash
npm test -- src/components/workspace/ArtifactWorkspace.test.tsx \
  src/components/workspace/PipelineSpecView.test.tsx
```

Expected: FAIL because the artifact workspace and request event do not exist.

- [ ] **Step 3: Add the artifact request event**

```ts
// composer-events.ts
import type { ArtifactTab } from "@/components/workspace/workspaceTypes";

export const REQUEST_ARTIFACT_VIEW_EVENT = "elspeth:request-artifact-view";
export interface RequestArtifactViewDetail {
  tab: ArtifactTab;
  focusMode: boolean;
  sessionId: string | null;
}
```

`ArtifactWorkspace` consumes active-tab state/actions from `WorkspacePaneContext`; it must not create a second tab controller. It listens for this event, selects and focuses the requested tab, then queues `OPEN_GRAPH_MODAL_EVENT` only when `{tab: "graph", focusMode: true}`. YAML focus mode is the persistent YAML tab; there must not be a second live `YamlView` owner. Until Task 7 removes the last guided-completion sender, also treat `OPEN_YAML_MODAL_EVENT` as a compatibility request to select/focus YAML. Pin this adapter in a test so Task 5 can delete the modal without creating a dead action.

- [ ] **Step 4: Implement the four artifact tabs**

Use `GraphView`, `YamlView`, and `InlineRunResults` directly. Add `showEmptyState?: boolean` to `InlineRunResults`; when true and no run exists, render `<p className="artifact-empty">No runs yet.</p>` instead of returning null.

`PipelineSpecView` must render authoritative state without deriving business rules. Build its rows deterministically from the existing wire types:

```ts
const sourceRows = Object.entries(state.sources)
  .sort(([left], [right]) => left.localeCompare(right))
  .map(([id, source]) => ({
    id,
    kind: "source" as const,
    plugin: source.plugin,
    routing: {
      on_success: source.on_success ?? null,
      on_validation_failure: source.on_validation_failure ?? null,
    },
    options: source.options,
  }));

const nodeRows = state.nodes.map((node) => ({
  id: node.id,
  kind: node.node_type,
  plugin: node.plugin,
  routing: {
    input: node.input,
    on_success: node.on_success,
    on_error: node.on_error,
    routes: node.routes ?? null,
    fork_to: node.fork_to ?? null,
  },
  options: node.options,
}));

const outputRows = state.outputs.map((output) => ({
  id: output.name,
  kind: "output" as const,
  plugin: output.plugin,
  routing: { on_write_failure: output.on_write_failure ?? null },
  options: output.options,
}));
```

Render a named Sources, Nodes, and Outputs section from those rows; each card shows `id`, `kind`, `plugin`, the non-null routing fields, and `JSON.stringify(options, null, 2)` in a focusable, labelled code region. Render the composition metadata heading/description above the sections and the existing `PipelineGloss` beneath it. Do not copy private graph validation or configuration mapping logic.

The Graph panel always exists and uses `GraphView`'s existing empty state. Spec/YAML tabs are disabled without composition content. Run is always available.

Keep the artifact tablist, Focus Graph control, and action-bar slot outside the active-panel `ErrorBoundary`. Key only that body boundary by `sessionId` and active artifact tab, and test that a throwing Graph/Spec/YAML/Run body leaves all tabs, inspector status buttons, and primary actions usable.

- [ ] **Step 5: Move YAML export context into the persistent surface**

Move the unconditional deployment-scope note and conditional blob-binding note from `ExportYamlModal.tsx` into `YamlView` above `YamlDisplay`. Preserve their exact evidence-based wording and tests. This makes the YAML tab the single copy/download/export owner.

- [ ] **Step 6: Define the navigation cutover contract**

Do not migrate App, hash-router, command-palette, or export senders in this task: the workspace receiver is not mounted in App until Task 5, and every committed slice must remain operable. Unit-test the new receiver and its temporary YAML compatibility adapter here. Task 5 atomically migrates all normal senders when it mounts the receiver.

- [ ] **Step 7: Verify GREEN**

```bash
npm test -- src/components/workspace/ArtifactWorkspace.test.tsx \
  src/components/workspace/PipelineSpecView.test.tsx \
  src/components/execution/InlineRunResults.test.tsx \
  src/components/inspector/YamlView.test.tsx
npm run typecheck
```

Expected: all focused tests and TypeScript pass.

- [ ] **Step 8: Commit the artifact slice**

```bash
git add src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.tsx \
  src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.test.tsx \
  src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx \
  src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.test.tsx \
  src/elspeth/web/frontend/src/components/execution/InlineRunResults.tsx \
  src/elspeth/web/frontend/src/components/execution/InlineRunResults.test.tsx \
  src/elspeth/web/frontend/src/components/inspector/YamlView.tsx \
  src/elspeth/web/frontend/src/components/inspector/YamlView.test.tsx \
  src/elspeth/web/frontend/src/lib/composer-events.ts
git diff --cached --name-only
git commit -m "feat(web): add persistent Composer artifact tabs"
```

**Definition of Done:** Graph, Spec, YAML, and Run have one authoritative persistent owner with accessible tab navigation and a tested request receiver ready for the atomic App cutover.

## Task 4: Add the Inspector and Sticky Action Bar

**Files:**

- Create: `src/elspeth/web/frontend/src/components/workspace/WorkspaceInspector.tsx`
- Create: `src/elspeth/web/frontend/src/components/workspace/WorkspaceInspector.test.tsx`
- Create: `src/elspeth/web/frontend/src/components/workspace/WorkspaceActionBar.tsx`
- Create: `src/elspeth/web/frontend/src/components/workspace/WorkspaceActionBar.test.tsx`
- Create: `src/elspeth/web/frontend/src/components/workspace/workspaceStatus.ts`
- Create: `src/elspeth/web/frontend/src/components/workspace/workspaceStatus.test.ts`
- Modify: `src/elspeth/web/frontend/src/components/sidebar/SideRailValidationBanner.tsx`
- Modify: `src/elspeth/web/frontend/src/components/sidebar/SideRailValidationBanner.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/audit/ReadinessRowDetail.tsx`
- Modify: `src/elspeth/web/frontend/src/components/audit/ReadinessRowDetail.test.tsx`

- [ ] **Step 1: Write failing inspector/action tests**

Cover status labels, validation/audit/history tabs, exact invoker focus restoration, Escape/close, disconnected-invoker fallback, inspector persistence without visibility, overlay geometry class below 1536px, secondary menu keyboard behavior, and the active-guided/tutorial omission of completion actions.

- [ ] **Step 2: Verify RED**

```bash
npm test -- src/components/workspace/WorkspaceInspector.test.tsx \
  src/components/workspace/WorkspaceActionBar.test.tsx \
  src/components/workspace/workspaceStatus.test.ts
```

Expected: FAIL because these modules do not exist.

- [ ] **Step 3: Implement status-only projections**

`workspaceStatus.ts` may derive only visible labels/counts:

- Validation: `Not checked`, `Passed`, `{n} warnings`, or `{n} errors` from `validationResult`.
- Audit: `Checking`, `Ready`, or `{n} issues` only when the cached snapshot matches both the active session ID and current `compositionState.version`; otherwise `Checking`. Reuse the same freshness predicate as `AuditReadinessPanel` so an earlier-version snapshot can never project `Ready`.
- Each status object includes text, tone, and an accessible label. It must not compute run/completion readiness.

- [ ] **Step 4: Implement an always-mounted inspector**

Render one `WorkspaceInspector` instance at all times. Consume inspector state, `openInspector`, `closeInspector`, and exact-invoker focus restoration from `WorkspacePaneContext`; do not create component-local open/tab state. When closed, set `hidden` on its `<aside>` but keep the component mounted so `AuditReadinessPanel` continues its snapshot-loading and validation-projection effects. Keep the inspector heading, tablist, and Close control outside the body boundaries. Mount Validation and Audit panels once in separate keyed `ErrorBoundary` instances and switch their labelled tabpanels with `hidden`; mount `GuidedHistory` only when guided history exists, in its own boundary. Test that a throwing body leaves its tabs and Close operable.

Use `role="tablist"` and the same roving-tab implementation as the artifact tabs. At widths below 1536px apply `workspace-inspector--overlay`; otherwise keep it inside the artifact workspace. It must never alter the authoring/artifact grid tracks.

Store the exact opening button in a ref. On close/Escape, call `focus({preventScroll: true})` if it is still connected; otherwise focus the status-control group. Do not apply the modal-only `useFocusTrap` to the inline inspector.

- [ ] **Step 5: Implement the sticky action bar**

`WorkspaceActionBar` consumes `openInspector` from `WorkspacePaneContext` and receives this explicit capability object:

```ts
interface WorkspaceActionCapabilities {
  completion: boolean;
  importYaml: boolean;
  catalog: boolean;
}
```

It renders:

- Validation and Audit status buttons that open the matching inspector tab.
- Existing `CompletionBar` only when `capabilities.completion` is true.
- A `More actions` menu containing the existing `ImportYamlButton` and/or `CatalogButton` only when their individual capability is true; omit the menu when neither is allowed.

The action bar is sticky inside the artifact workspace, stays outside the artifact scroller, and does not recreate any enablement/readiness rules.

Pin the capability matrix in tests: ordinary freeform and terminal guided use the existing App admission rules and allow Import/Catalog; active guided allows none of these three because the old side rail was deliberately absent and Import can terminate/reset guided state; tutorial allows none. Status/inspector buttons remain available in every mode.

- [ ] **Step 6: Route validation/audit component navigation through Graph**

Add an optional callback to `SideRailValidationBanner` and `ReadinessRowDetail`:

```ts
interface ComponentNavigationProps {
  onSelectComponent?: (componentId: string) => void;
}
```

The workspace callback selects the node in `useSessionStore`, selects/focuses the Graph tab, and does not automatically open the graph modal. Preserve the old modal-event fallback for shared/legacy call sites until Task 5 removes the normal side rail.

- [ ] **Step 7: Verify GREEN**

```bash
npm test -- src/components/workspace/WorkspaceInspector.test.tsx \
  src/components/workspace/WorkspaceActionBar.test.tsx \
  src/components/workspace/workspaceStatus.test.ts \
  src/components/sidebar/SideRailValidationBanner.test.tsx \
  src/components/audit/ReadinessRowDetail.test.tsx
npm run typecheck
npm run lint
npm run lint:css
```

Expected: focused tests and static checks pass.

- [ ] **Step 8: Commit the inspector/action slice**

```bash
git add src/elspeth/web/frontend/src/components/workspace/WorkspaceInspector.tsx \
  src/elspeth/web/frontend/src/components/workspace/WorkspaceInspector.test.tsx \
  src/elspeth/web/frontend/src/components/workspace/WorkspaceActionBar.tsx \
  src/elspeth/web/frontend/src/components/workspace/WorkspaceActionBar.test.tsx \
  src/elspeth/web/frontend/src/components/workspace/workspaceStatus.ts \
  src/elspeth/web/frontend/src/components/workspace/workspaceStatus.test.ts \
  src/elspeth/web/frontend/src/components/sidebar/SideRailValidationBanner.tsx \
  src/elspeth/web/frontend/src/components/sidebar/SideRailValidationBanner.test.tsx \
  src/elspeth/web/frontend/src/components/audit/ReadinessRowDetail.tsx \
  src/elspeth/web/frontend/src/components/audit/ReadinessRowDetail.test.tsx
git diff --cached --name-only
git commit -m "feat(web): add Composer inspector and action bar"
```

**Definition of Done:** audit/validation/history share one accessible inspector, primary actions stay visible, and business-rule owners remain unchanged.

## Task 5: Integrate the Common Workspace in the Main Composer

**Files:**

- Modify: `src/elspeth/web/frontend/src/App.tsx`
- Modify: `src/elspeth/web/frontend/src/App.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/composer/CompletionBar.tsx`
- Modify: `src/elspeth/web/frontend/src/components/composer/CompletionBar.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/sidebar/ExportYamlButton.tsx`
- Modify: `src/elspeth/web/frontend/src/components/sidebar/ExportYamlButton.test.tsx`
- Modify: `src/elspeth/web/frontend/src/styles/shared.css`
- Modify: `src/elspeth/web/frontend/src/test/a11y/components.a11y.test.tsx`
- Modify: `src/elspeth/web/frontend/src/test/inlineSourceIntegration.test.tsx`
- Modify: `src/elspeth/web/frontend/tests/e2e/modal-flow.spec.ts`
- Create: `src/elspeth/web/frontend/src/components/workspace/useCollapsedAuthoringStatus.ts`
- Create: `src/elspeth/web/frontend/src/components/workspace/useCollapsedAuthoringStatus.test.ts`
- Modify: `src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx`
- Modify: `src/elspeth/web/frontend/src/components/chat/ChatPanel.test.tsx`
- Modify: `src/elspeth/web/frontend/src/hooks/useHashRouter.ts`
- Modify: `src/elspeth/web/frontend/src/hooks/useHashRouter.test.ts`
- Modify: `src/elspeth/web/frontend/src/components/common/CommandPalette.tsx`
- Modify: `src/elspeth/web/frontend/src/components/common/CommandPalette.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/common/ShortcutsHelp.tsx`
- Modify: `src/elspeth/web/frontend/src/components/common/ShortcutsHelp.test.tsx`
- Create: `src/elspeth/web/frontend/tests/e2e/composer-workspace-geometry.spec.ts`
- Modify: `src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.tsx`
- Modify: `src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.test.tsx`
- Delete: `src/elspeth/web/frontend/src/components/common/Layout.tsx`
- Delete: `src/elspeth/web/frontend/src/components/common/Layout.test.tsx`
- Delete: `src/elspeth/web/frontend/src/components/sidebar/SideRail.tsx`
- Delete: `src/elspeth/web/frontend/src/components/sidebar/SideRail.test.tsx`
- Delete: `src/elspeth/web/frontend/src/components/sidebar/ExportYamlModal.tsx`
- Delete: `src/elspeth/web/frontend/src/components/sidebar/ExportYamlModal.test.tsx`

- [ ] **Step 1: Write the App integration tests and browser tracer first**

Replace the `Layout` mock with `ComposerWorkspace`, assert a single stable workspace in freeform/active-guided/completed-guided states, and preserve the existing `runAdmissionAvailable` assertions. Add an explicit test that active guided renders status controls but no CompletionBar, Import, Catalog, or `REQUEST_RUN_EVENT` owner.

Create the first two live-browser contract tests in `composer-workspace-geometry.spec.ts` at 1280×720 and 1536×760. Each test creates a real session through `createSession()`, seeds a populated composition into that returned ID, navigates to `/#/${session.id}`, and asserts named authoring/artifact panes, artifact width at least 640px, active-panel content height at least 420px, reachable action controls, and no document horizontal overflow. Run these against the old shell and observe RED before changing App.

- [ ] **Step 2: Verify RED**

```bash
npm test -- src/App.test.tsx src/components/composer/CompletionBar.test.tsx src/components/sidebar/ExportYamlButton.test.tsx

WORKTREE=/home/john/elspeth/.claude/worktrees/composer-1080p-wqhd-plan
PYTHONPATH="$WORKTREE/src" PLAYWRIGHT_BACKEND_PORT=8460 PLAYWRIGHT_FRONTEND_PORT=5180 \
  npx playwright test composer-workspace-geometry.spec.ts --project=chromium
```

Expected: unit integration tests and both geometry tracers FAIL because App still assembles `Layout` and `SideRail`.

- [ ] **Step 3: Replace App's shell assembly**

At the current `App.tsx` main Composer branch, render:

```tsx
<div className="app-main" role="main">
  <ComposerWorkspace
    authoring={<ChatPanel onOpenSecrets={openSecrets} />}
    authoringStatus={<DefaultModeChangedBanner />}
    collapsedStatus={collapsedAuthoringStatus}
    artifact={<ArtifactWorkspace />}
    inspector={<WorkspaceInspector />}
    actionBar={
      <WorkspaceActionBar
        capabilities={workspaceActionCapabilities}
      />
    }
  />
</div>
```

Derive `workspaceActionCapabilities` once in App: `{completion: runAdmissionAvailable, importYaml: !guidedBuildActive, catalog: !guidedBuildActive}`. Preserve any existing non-guided gates within the owned buttons themselves. Do not branch the workspace itself on mode.

Build `collapsedAuthoringStatus` with a presentation-only hook over the existing session `isComposing`, `guidedChatPending`, `guidedResponsePending`, and `error` fields, plus `usePendingAcknowledgements` and message sequence/count. Priority is error, then any freeform/guided operation in progress, then new messages or acknowledgements received while collapsed, then neutral `Authoring pane collapsed`. Reset the unread baseline when the pane is restored or the active session changes. Test collapse during both guided-chat submission and guided-decision response. Do not reproduce guided transition, acknowledgement, or request-custody rules in this hook.

- [ ] **Step 4: Cut navigation over atomically**

Migrate `useHashRouter`, App shortcuts, CommandPalette, Export YAML, and shortcut help to `REQUEST_ARTIFACT_VIEW_EVENT`; every dispatch includes the intended session ID. Restore Spec/Run hash actions, and continue canonicalizing the hash after dispatch. If a Spec/YAML request arrives before that session's composition content is loaded, retain one pending intent keyed to that session and fulfill it only after the matching active session has loaded content; discard it on a different session or superseding request. Preserve the current fresh-load YAML behavior and add the same test for Spec.

For CommandPalette, close the palette first, then queue the artifact request in a microtask so the palette focus trap restores before the workspace focuses the requested tab. Unit-test final focus, not only event delivery. Keep `Focus graph` as the sole explicit full-screen action.

- [ ] **Step 5: Make Export YAML select the YAML artifact**

Keep the user-facing `Export YAML` button and all readiness/content gates, but dispatch `REQUEST_ARTIFACT_VIEW_EVENT` with `{tab: "yaml", focusMode: false, sessionId: activeSessionId}`. Remove the normal `ExportYamlModal` host from App; YAML copy/download and scope notes now live in the persistent tab. Rewrite the CompletionBar integration test to render inside `ComposerWorkspace` and assert YAML-tab selection. Remove the deleted modal mocks/imports from `inlineSourceIntegration.test.tsx` and the component accessibility inventory. Keep the temporary `OPEN_YAML_MODAL_EVENT` compatibility listener described in Task 3 until Task 7 migrates guided completion.

- [ ] **Step 6: Retire the old shell only after App tests pass**

Delete `Layout`, `SideRail`, and their tests. Remove `.app-layout`, `.layout-chat`, `.layout-siderail`, `.side-rail`, and fixed `--siderail-width` shell rules only when no live consumer remains. Keep `GraphMiniView`, because `SharedInspectView` still uses it.

At the same cutover, remove all three `InlineRunResults` mounts from `ChatPanel` (freeform, active guided, and completed guided) and their layout-only assertions. The Run artifact becomes the only normal Composer owner atomically; add a request-count/poll-cleanup regression proving one history request stream when Run is selected and zero after switching away or changing session. Tasks 6 and 7 remove the remaining guided/completion ambient surfaces but must not defer this polling-owner migration.

Update `modal-flow.spec.ts` so shortcut and deep-link checks expect Graph/YAML tab selection; retain a separate browser assertion that the explicit `Focus graph` action opens the graph modal.

- [ ] **Step 7: Verify GREEN**

```bash
npm test -- src/App.test.tsx \
  src/components/composer/CompletionBar.test.tsx \
  src/components/sidebar/ExportYamlButton.test.tsx \
  src/components/workspace/useCollapsedAuthoringStatus.test.ts \
  src/components/chat/ChatPanel.test.tsx \
  src/hooks/useHashRouter.test.ts \
  src/components/common/CommandPalette.test.tsx \
  src/components/common/ShortcutsHelp.test.tsx \
  src/components/workspace
npm run typecheck
npm run lint
npm run lint:css
npm run build

WORKTREE=/home/john/elspeth/.claude/worktrees/composer-1080p-wqhd-plan
PYTHONPATH="$WORKTREE/src" PLAYWRIGHT_BACKEND_PORT=8460 PLAYWRIGHT_FRONTEND_PORT=5180 \
  npx playwright test composer-workspace-geometry.spec.ts modal-flow.spec.ts --project=chromium
```

Expected: all focused tests and frontend static/build checks pass.

- [ ] **Step 8: Commit the integration slice**

```bash
git add src/elspeth/web/frontend/src/App.tsx \
  src/elspeth/web/frontend/src/App.test.tsx \
  src/elspeth/web/frontend/src/components/composer/CompletionBar.tsx \
  src/elspeth/web/frontend/src/components/composer/CompletionBar.test.tsx \
  src/elspeth/web/frontend/src/components/sidebar/ExportYamlButton.tsx \
  src/elspeth/web/frontend/src/components/sidebar/ExportYamlButton.test.tsx \
  src/elspeth/web/frontend/src/styles/shared.css \
  src/elspeth/web/frontend/src/test/a11y/components.a11y.test.tsx \
  src/elspeth/web/frontend/src/test/inlineSourceIntegration.test.tsx \
  src/elspeth/web/frontend/tests/e2e/modal-flow.spec.ts \
  src/elspeth/web/frontend/src/components/workspace/useCollapsedAuthoringStatus.ts \
  src/elspeth/web/frontend/src/components/workspace/useCollapsedAuthoringStatus.test.ts \
  src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx \
  src/elspeth/web/frontend/src/components/chat/ChatPanel.test.tsx \
  src/elspeth/web/frontend/src/hooks/useHashRouter.ts \
  src/elspeth/web/frontend/src/hooks/useHashRouter.test.ts \
  src/elspeth/web/frontend/src/components/common/CommandPalette.tsx \
  src/elspeth/web/frontend/src/components/common/CommandPalette.test.tsx \
  src/elspeth/web/frontend/src/components/common/ShortcutsHelp.tsx \
  src/elspeth/web/frontend/src/components/common/ShortcutsHelp.test.tsx \
  src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.tsx \
  src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.test.tsx \
  src/elspeth/web/frontend/tests/e2e/composer-workspace-geometry.spec.ts \
  src/elspeth/web/frontend/src/components/common/Layout.tsx \
  src/elspeth/web/frontend/src/components/common/Layout.test.tsx \
  src/elspeth/web/frontend/src/components/sidebar/SideRail.tsx \
  src/elspeth/web/frontend/src/components/sidebar/SideRail.test.tsx \
  src/elspeth/web/frontend/src/components/sidebar/ExportYamlModal.tsx \
  src/elspeth/web/frontend/src/components/sidebar/ExportYamlModal.test.tsx
git diff --cached --name-only
git commit -m "refactor(web): replace Composer side rail with workspace"
```

**Definition of Done:** the normal Composer uses one persistent common workspace and the obsolete shell/export modal are gone without changing action admission.

## Task 6: Converge Active Guided Authoring on the Common Shell

**Files:**

- Modify: `src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx`
- Modify: `src/elspeth/web/frontend/src/components/chat/ChatPanel.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/chat/guided/guided.css`
- Modify: `src/elspeth/web/frontend/src/components/chat/guided/PipelineValidationSummary.tsx`
- Modify: `src/elspeth/web/frontend/src/test/a11y/components.a11y.test.tsx`
- Modify: `src/elspeth/web/frontend/tests/e2e/composer-guided.spec.ts`

- [ ] **Step 1: Write the guided convergence regressions**

Assert that active guided uses the same outer workspace, the authoring pane preserves this order, and the old complementary `Pipeline summary` rail is absent:

```tsx
expect(within(authoringPane).getByRole("log", { name: "Step chat history" })).toBeVisible();
expect(within(authoringPane).getByRole("log", { name: "Guided wizard step" })).toBeVisible();
expect(screen.getByRole("tab", { name: "Graph" })).toBeVisible();
expect(screen.queryByRole("complementary", { name: "Pipeline summary" })).toBeNull();
```

Keep existing draft retention, send/retry, focus, pending, acknowledgement, and state-machine tests unchanged except for the renamed scroller selector.

- [ ] **Step 2: Verify RED**

```bash
npm test -- src/components/chat/ChatPanel.test.tsx -t "guided workspace|guided pending|guided scroll"
```

Expected: the new no-rail/common-workspace assertions fail.

- [ ] **Step 3: Flatten only the guided render frame**

In the active guided branch:

- retain the header, stepper, error banner, `AcknowledgementLiveRegion`, transcript, acknowledgement stack, current decision, pending strip, and docked composer;
- preserve order `live region outside scroller -> transcript -> acknowledgements -> decision -> pending strip -> composer outside scroller`;
- remove `GraphMiniView`, `PipelineGloss`, `PipelineValidationSummary`, and `GuidedHistory` mounts from `ChatPanel` (all `InlineRunResults` owners were already removed atomically in Task 5);
- remove `.guided-workspace` and `.guided-workspace-rail` wrappers;
- rename the scroller to `.guided-authoring-scroll` while preserving its ref, `onScroll`, `role="group"`, `aria-label="Conversation"`, and `tabIndex={0}`.

Do not move selectors, callbacks, async actions, retry logic, drafts, or focus effects out of `ChatPanel`.

- [ ] **Step 4: Move guided ambient content to common surfaces**

Project `GuidedHistory` from the current guided session inside `WorkspaceInspector`'s History tab. Use the common Graph/Spec tabs for pipeline state and the common Run tab for run evidence. For active tutorial validation, allow the Inspector Validation tab to render the existing `PipelineValidationSummary isTutorial` instead of duplicating its derivation.

- [ ] **Step 5: Remove obsolete guided rail CSS**

Delete the guided grid/rail block currently owning the fixed 320px rail. Keep one authoring scroller with `min-height: 0`, `overflow-y: auto`, bottom anchoring, inset focus ring, and a non-shrinking composer. Make the stepper compact enough for a 360px authoring pane and short viewport.

- [ ] **Step 6: Verify component and rendered guided behavior**

```bash
npm test -- src/components/chat/ChatPanel.test.tsx \
  src/test/a11y/components.a11y.test.tsx

WORKTREE=/home/john/elspeth/.claude/worktrees/composer-1080p-wqhd-plan
PYTHONPATH="$WORKTREE/src" PLAYWRIGHT_BACKEND_PORT=8461 PLAYWRIGHT_FRONTEND_PORT=5181 \
  npm run test:e2e -- composer-guided.spec.ts --project=chromium
```

Expected: guided component/a11y tests and the live guided browser spec pass.

- [ ] **Step 7: Commit the guided slice**

```bash
git add src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx \
  src/elspeth/web/frontend/src/components/chat/ChatPanel.test.tsx \
  src/elspeth/web/frontend/src/components/chat/guided/guided.css \
  src/elspeth/web/frontend/src/components/chat/guided/PipelineValidationSummary.tsx \
  src/elspeth/web/frontend/src/test/a11y/components.a11y.test.tsx \
  src/elspeth/web/frontend/tests/e2e/composer-guided.spec.ts
git diff --cached --name-only
git commit -m "refactor(web): converge guided authoring on workspace"
```

**Definition of Done:** guided authoring shares the persistent artifact workspace without changing guided protocol, custody, pending, acknowledgement, or run-admission behavior.

## Task 7: Converge Completion and Tutorial Surfaces

**Files:**

- Modify: `src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx`
- Modify: `src/elspeth/web/frontend/src/components/chat/ChatPanel.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/chat/guided/CompletionSummary.tsx`
- Modify: `src/elspeth/web/frontend/src/components/chat/guided/CompletionSummary.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/tutorial/TutorialGuidedShell.tsx`
- Modify: `src/elspeth/web/frontend/src/components/tutorial/TutorialGuidedShell.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/tutorial/tutorial.css`
- Modify: `src/elspeth/web/frontend/src/test/a11y/components.a11y.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.tsx`
- Modify: `src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.test.tsx`
- Modify: `src/elspeth/web/frontend/src/lib/composer-events.ts`

- [ ] **Step 1: Write completion/tutorial tests first**

Assert that completion retains its readiness-derived heading, acknowledgement owner, and exact `exitToFreeform()` action, but no longer duplicates YAML, validation, or run surfaces. Assert that tutorial supplies the same workspace shell with normal completion actions omitted.

- [ ] **Step 2: Verify RED**

```bash
npm test -- src/components/chat/guided/CompletionSummary.test.tsx \
  src/components/tutorial/TutorialGuidedShell.test.tsx \
  src/components/chat/ChatPanel.test.tsx -t "completed"
```

Expected: duplicate completion YAML/validation/run assertions fail against the new contract.

- [ ] **Step 3: Simplify guided completion content**

Keep `GuidedWorkflowStepper`, `AcknowledgementLiveRegion`, `AcknowledgementStack`, completion outcome heading, and non-tutorial `Open freeform editor`. Remove the inline YAML preview, Export YAML, Validate Pipeline, and tutorial validation summary from the completed `ChatPanel` branch; Task 5 already removed its run-results mount. The common YAML, Inspector Validation, and Run tabs are their single owners.

`Open freeform editor` must continue calling the existing async `exitToFreeform()` state transition; it is not ordinary tab navigation.

Change any surviving guided-completion YAML action to dispatch `REQUEST_ARTIFACT_VIEW_EVENT`, then remove the temporary `OPEN_YAML_MODAL_EVENT` compatibility listener, its constant, and its tests. Verify with `rg -n 'OPEN_YAML_MODAL_EVENT|ExportYamlModal' src tests/e2e` that no production/test import or sender remains; descriptive historical comments may be reworded instead of retaining the retired identifier.

- [ ] **Step 4: Wrap tutorial authoring in the common workspace**

Inside `TutorialGuidedShell`, keep start/reset/sample URL/pending-review/handoff logic unchanged. Wrap the existing real `<ChatPanel isTutorial lockedChatPrompt={lockedChatPrompt} />` with `ComposerWorkspace`/artifact/inspector surfaces configured with `{completion: false, importYaml: false, catalog: false}`. Do not mount a normal `CompletionBar` in the tutorial.

- [ ] **Step 5: Adapt the tutorial height chain**

Replace references to `.guided-workspace-scroll` with the common workspace/authoring chain. Preserve `flex: 1`, `min-height: 0`, fixed header bands, internal authoring scroll, and the completed-panel fallback. Do not restore document scrolling for routine desktop guided work.

- [ ] **Step 6: Verify GREEN**

```bash
npm test -- src/components/chat/guided/CompletionSummary.test.tsx \
  src/components/tutorial/TutorialGuidedShell.test.tsx \
  src/components/chat/ChatPanel.test.tsx \
  src/test/a11y/components.a11y.test.tsx
npm run typecheck
npm run lint:css
```

Expected: completion, tutorial, ChatPanel, a11y, TypeScript, and CSS tests pass.

- [ ] **Step 7: Commit the completion/tutorial slice**

```bash
git add src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx \
  src/elspeth/web/frontend/src/components/chat/ChatPanel.test.tsx \
  src/elspeth/web/frontend/src/components/chat/guided/CompletionSummary.tsx \
  src/elspeth/web/frontend/src/components/chat/guided/CompletionSummary.test.tsx \
  src/elspeth/web/frontend/src/components/tutorial/TutorialGuidedShell.tsx \
  src/elspeth/web/frontend/src/components/tutorial/TutorialGuidedShell.test.tsx \
  src/elspeth/web/frontend/src/components/tutorial/tutorial.css \
  src/elspeth/web/frontend/src/test/a11y/components.a11y.test.tsx \
  src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.tsx \
  src/elspeth/web/frontend/src/components/workspace/ArtifactWorkspace.test.tsx \
  src/elspeth/web/frontend/src/lib/composer-events.ts
git diff --cached --name-only
git commit -m "refactor(web): unify Composer completion and tutorial workspace"
```

**Definition of Done:** freeform, active guided, completed guided, and tutorial use one outer workspace without duplicate artifact/action owners.

## Task 8: Harden Notices, Dialogs, and Short-Height Behavior

**Files:**

- Create: `src/elspeth/web/frontend/src/components/common/AppNoticeCenter.tsx`
- Create: `src/elspeth/web/frontend/src/components/common/AppNoticeCenter.test.tsx`
- Create: `src/elspeth/web/frontend/src/components/common/ConfirmDialog.test.tsx`
- Modify: `src/elspeth/web/frontend/src/App.tsx`
- Modify: `src/elspeth/web/frontend/src/App.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/common/ConfirmDialog.tsx`
- Modify: `src/elspeth/web/frontend/src/components/composer/SaveForReviewDialog.tsx`
- Modify: `src/elspeth/web/frontend/src/components/composer/SaveForReviewDialog.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/chat/chat.css`
- Modify: `src/elspeth/web/frontend/src/components/header/header.css`
- Modify: `src/elspeth/web/frontend/src/components/composer/composer.css`
- Modify: `src/elspeth/web/frontend/src/styles/shared.css`

- [ ] **Step 1: Write failing notice/dialog/height tests**

Test notice priority, additional-notice disclosure, preserved alert/status roles/actions, tall dialog body scrolling, sticky title/footer, focus restoration, and desktop textarea max height.

Priority is explicit and stable:

1. Backend unavailable
2. Preferences error
3. Redirect toast
4. Stale build
5. Composer unavailable

- [ ] **Step 2: Verify RED**

```bash
npm test -- src/components/common/AppNoticeCenter.test.tsx \
  src/components/common/ConfirmDialog.test.tsx \
  src/components/composer/SaveForReviewDialog.test.tsx \
  src/App.test.tsx
```

Expected: FAIL because notices are still independently stacked and ConfirmDialog has no bounded body.

- [ ] **Step 3: Implement `AppNoticeCenter`**

Pass a typed array of notices from App. Show only the highest-priority notice in the bounded banner row. When more exist, render a named `N more notices` button that opens a viewport-bounded popover overlay with each notice's original text and action. The popover uses `max-height: calc(100dvh - 32px)`, one internal scroller, Escape/outside-click close, and exact invoker focus restoration; it never participates in the workspace height calculation. Preserve the primary notice's `alert` versus `status` semantics. If a hidden additional notice is assertive, announce one concise polite/alert summary that additional urgent notices are available; do not duplicate every message in live regions. Add a 1280×720 geometry test with several long notices proving the workspace height is unchanged and every notice/action is reachable in the popover.

- [ ] **Step 4: Bound ConfirmDialog and other Composer dialogs**

Change `ConfirmDialog` to a three-region flex frame:

```tsx
<div
  ref={dialogRef}
  className="confirm-dialog"
  role="alertdialog"
  aria-modal="true"
  aria-labelledby="confirm-dialog-title"
  aria-describedby="confirm-dialog-message"
>
  <header className="confirm-dialog-header">
    <h2 id="confirm-dialog-title">{title}</h2>
  </header>
  <div className="confirm-dialog-body">
    <p id="confirm-dialog-message">{message}</p>
    {children}
  </div>
  <footer className="confirm-dialog-actions">
    <button type="button" className="btn confirm-dialog-btn" onClick={onCancel}>
      {cancelLabel}
    </button>
    <button
      type="button"
      className={`${confirmBtnClass} confirm-dialog-btn confirm-dialog-confirm-btn`}
      onClick={onConfirm}
    >
      {confirmLabel}
    </button>
  </footer>
</div>
```

Use `max-height: calc(100dvh - 32px)`, `overflow: hidden` on the frame, `overflow-y: auto; min-height: 0` on the body, and non-shrinking header/footer. Change the already-bounded Save-for-review dialog from `100vh` to `100dvh`. Keep Graph and Import YAML modal bodies as their existing single scrollers.

- [ ] **Step 5: Bound desktop composer growth and compact height**

Outside the existing ≤760px rule, give `.chat-input-textarea` `max-height: min(28dvh, 240px); overflow-y: auto`. Add height-aware compact spacing for workspace/header/tab/action bands at `@media (max-height: 800px)`. Preserve `.app-main` as defensive exceptional-height scroll owner, but routine acceptance viewports must use pane scrollers.

- [ ] **Step 6: Verify GREEN**

```bash
npm test -- src/components/common/AppNoticeCenter.test.tsx \
  src/components/common/ConfirmDialog.test.tsx \
  src/components/composer/SaveForReviewDialog.test.tsx \
  src/App.test.tsx
npm run typecheck
npm run lint
npm run lint:css
```

Expected: notice, dialog, App, and static tests pass.

- [ ] **Step 7: Commit the height-hardening slice**

```bash
git add src/elspeth/web/frontend/src/components/common/AppNoticeCenter.tsx \
  src/elspeth/web/frontend/src/components/common/AppNoticeCenter.test.tsx \
  src/elspeth/web/frontend/src/components/common/ConfirmDialog.tsx \
  src/elspeth/web/frontend/src/components/common/ConfirmDialog.test.tsx \
  src/elspeth/web/frontend/src/App.tsx \
  src/elspeth/web/frontend/src/App.test.tsx \
  src/elspeth/web/frontend/src/components/composer/SaveForReviewDialog.tsx \
  src/elspeth/web/frontend/src/components/composer/SaveForReviewDialog.test.tsx \
  src/elspeth/web/frontend/src/components/chat/chat.css \
  src/elspeth/web/frontend/src/components/header/header.css \
  src/elspeth/web/frontend/src/components/composer/composer.css \
  src/elspeth/web/frontend/src/styles/shared.css
git diff --cached --name-only
git commit -m "fix(web): harden Composer short-height surfaces"
```

**Definition of Done:** notices cannot consume the workspace, dialogs stay operable within `dvh`, and the composer cannot grow over primary content.

## Task 9: Add Deterministic Desktop Geometry Acceptance

**Files:**

- Create: `src/elspeth/web/frontend/tests/e2e/helpers/workspace-fixtures.ts`
- Create: `src/elspeth/web/frontend/tests/e2e/helpers/workspace-assertions.ts`
- Modify: `src/elspeth/web/frontend/tests/e2e/composer-workspace-geometry.spec.ts`
- Modify: `src/elspeth/web/frontend/tests/e2e/page-objects/composer-page.ts`
- Modify: `src/elspeth/web/frontend/tests/e2e/composer-reflow.spec.ts`
- Create: `src/elspeth/web/frontend/tsconfig.workspace-e2e.json`
- Modify: `src/elspeth/web/frontend/eslint.config.js`
- Modify: `src/elspeth/web/frontend/package.json`

- [ ] **Step 1: Build deterministic scenario fixtures**

Generalize the route fixture patterns from `composer-proposals.spec.ts`, `tutorial.spec.ts`, `empty-run-discard-warning.spec.ts`, and `composer-guided.spec.ts`. Provide fixed IDs, timestamps, text, and authoritative response shapes for:

- empty freeform;
- populated pipeline with long transcript;
- active guided decision;
- validation/audit issues;
- pending acknowledgement/blocker;
- active/completed run;
- multiple notices;
- tall confirmation dialog.

Use `createSession()` and the existing live `seedCompositionState()` endpoint for canonical composition state. `installWorkspaceScenario()` returns the server-created session ID and every test navigates to `/#/${sessionId}`; never hard-code a fake session ID around a partially live API. Use route fixtures only for transcript/guided/run/notice/dialog state because no backend seed route exists for those. Do not add a backend endpoint.

- [ ] **Step 2: Add semantic page-object locators**

Add navigation/interaction only—no assertions—for workspace, authoring pane, separator, artifact region/tabs/panel, inspector, action bar, collapse/restore, and Compose/Pipeline narrow view tabs.

- [ ] **Step 3: Write geometry helpers and acceptance tests**

```ts
const DESKTOP_VIEWPORTS = [
  { width: 1920, height: 900 },
  { width: 1536, height: 760 },
  { width: 1280, height: 720 },
  { width: 2048, height: 1050 },
  { width: 2560, height: 1280 },
] as const;

for (const viewport of DESKTOP_VIEWPORTS) {
  test(`populated workspace is operable at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const sessionId = await installWorkspaceScenario(page, "populated-long-transcript");
    await page.goto(`/#/${sessionId}`);
    await expect(page.getByLabel("Authoring pane")).toBeVisible();
    await expect(page.getByLabel("Pipeline artifact")).toBeVisible();
    await expect.poll(() => boxWidth(page.getByLabel("Pipeline artifact"))).toBeGreaterThanOrEqual(640);
    await expect.poll(() => boxHeight(page.getByRole("tabpanel"))).toBeGreaterThanOrEqual(420);
    await expectNoDocumentHorizontalOverflow(page);
    await expectPrimaryControlsInViewport(page);
    await expectIntendedPaneScrollers(page);
  });
}
```

Run this exact scenario matrix at every viewport in `DESKTOP_VIEWPORTS`:

| Scenario | Required additional assertions |
| --- | --- |
| Empty freeform | actionable Graph empty state; actions and composer reachable |
| Populated long transcript | both pane minima; transcript is the sole authoring scroller |
| Active guided decision | stable outer geometry; Step chat history and Guided wizard step visible; Import/Catalog/Completion absent |
| Validation/audit issues with inspector | inspector overlay does not change both main pane widths; inspector body is its sole scroller |
| Pending acknowledgement/blocker | acknowledgement remains reachable; collapsed projection announces new activity |
| Active and completed run | one Run history request stream; sticky action bar does not cover focus |
| Multiple notices | one primary banner row and accessible additional-notice disclosure |
| Tall confirmation dialog | bounded frame/body scroller; title/actions visible; exact invoker focus return |

For resize, assert minimum/default/maximum geometry at each viewport. At 1280px, dragging to the current maximum must still leave the artifact at 640px; at widths below 1000px assert the approved 360px-plus-remainder fallback and zero horizontal overflow. `expectIntendedPaneScrollers()` must inspect computed overflow plus `scrollHeight > clientHeight` and fail for any routine scroller other than transcript, active artifact body, and open inspector body. `expectPrimaryControlsInViewport()` must focus each sticky/collapse/tab control and assert its bounding box is inside the viewport and not intersected by sticky regions.

- [ ] **Step 4: Preserve narrow reflow**

Keep the existing 320×256 and 375×667 conversation/input assertions. Add proof that the Compose/Pipeline switcher is reachable, no horizontal document overflow exists, and mobile resizing is disabled.

- [ ] **Step 5: Run the browser acceptance**

Before Playwright, add a targeted E2E TypeScript/lint gate. `tsconfig.workspace-e2e.json` extends `tsconfig.e2e.json` but overrides `include` to the new workspace specs/helpers and modified page object, avoiding the unrelated existing `staging-tutorial-driver.mjs` declaration defect. Add `typecheck:workspace-e2e` to `package.json`, and add the same workspace file globs to ESLint's configured files and `lint` script.

```bash
WORKTREE=/home/john/elspeth/.claude/worktrees/composer-1080p-wqhd-plan
cd "$WORKTREE/src/elspeth/web/frontend"
npm run typecheck:workspace-e2e
npm run lint
PYTHONPATH="$WORKTREE/src" PLAYWRIGHT_BACKEND_PORT=8462 PLAYWRIGHT_FRONTEND_PORT=5182 \
  npx playwright test composer-workspace-geometry.spec.ts composer-reflow.spec.ts --project=chromium
```

Expected: every scenario in the eight-row matrix passes at all five desktop viewports, and both narrow reflow viewports pass.

- [ ] **Step 6: Commit geometry acceptance**

```bash
git add src/elspeth/web/frontend/tests/e2e/helpers/workspace-fixtures.ts \
  src/elspeth/web/frontend/tests/e2e/helpers/workspace-assertions.ts \
  src/elspeth/web/frontend/tests/e2e/composer-workspace-geometry.spec.ts \
  src/elspeth/web/frontend/tests/e2e/composer-reflow.spec.ts \
  src/elspeth/web/frontend/tests/e2e/page-objects/composer-page.ts \
  src/elspeth/web/frontend/tsconfig.workspace-e2e.json \
  src/elspeth/web/frontend/eslint.config.js \
  src/elspeth/web/frontend/package.json
git diff --cached --name-only
git commit -m "test(web): enforce Composer desktop geometry"
```

**Definition of Done:** rendered browser tests enforce the five desktop viewport contract, pane minima, scroll ownership, control reachability, and narrow regressions.

## Task 10: Add Real-Browser Accessibility and Visual Baselines

**Files:**

- Modify: `src/elspeth/web/frontend/package.json`
- Modify: `src/elspeth/web/frontend/package-lock.json`
- Create: `src/elspeth/web/frontend/tests/e2e/composer-workspace-accessibility.spec.ts`
- Create: `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts`
- Create: `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots/*.png`
- Modify: `src/elspeth/web/frontend/src/test/a11y/components.a11y.test.tsx`

- [ ] **Step 1: Add the real-browser axe dependency**

From the frontend directory:

```bash
npm install --save-dev --save-exact @axe-core/playwright@4.11.0
```

Expected: `package.json` and `package-lock.json` add one pinned dev dependency; do not run `npm audit fix` as part of this feature.

- [ ] **Step 2: Write keyboard/focus/browser accessibility tests**

Cover DOM/visual order, tab roving, separator ARIA and keyboard operations, collapse/restore, inspector/dialog focus return, sticky controls not covering focus, busy/error names, and an AxeBuilder WCAG 2.2 AA scan of stable populated and inspector-open states.

```ts
const results = await new AxeBuilder({ page })
  .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
  .analyze();
expect(results.violations).toEqual([]);
```

- [ ] **Step 3: Verify accessibility tests**

```bash
WORKTREE=/home/john/elspeth/.claude/worktrees/composer-1080p-wqhd-plan
PYTHONPATH="$WORKTREE/src" PLAYWRIGHT_BACKEND_PORT=8463 PLAYWRIGHT_FRONTEND_PORT=5183 \
  npx playwright test composer-workspace-accessibility.spec.ts --project=chromium
```

Expected: keyboard/focus and Axe browser tests pass.

- [ ] **Step 4: Add only the seven approved visual baselines**

Force dark theme through localStorage before navigation, emulate reduced motion, wait for `document.fonts.ready`, use fixed fixture IDs/timestamps, and wait for the expected React Flow node count. Use `animations: "disabled"` and `caret: "hide"`.

Capture:

- populated freeform at 1920×900, 1536×760, 1280×720, and 2560×1280;
- active guided decision at 1536×760;
- inspector open at 1280×720;
- tall dialog at 1280×720.

```ts
await expect(page.getByTestId("composer-workspace")).toHaveScreenshot(
  `${scenario}-${viewport.width}x${viewport.height}.png`,
  { animations: "disabled", caret: "hide" },
);
```

Use the workspace-element screenshot for the six workspace states. `ConfirmDialog` is an App-root sibling, so capture the tall-dialog baseline with `expect(page).toHaveScreenshot("tall-dialog-1280x720.png", { animations: "disabled", caret: "hide" })` after asserting its dialog/body geometry; otherwise the modal would be omitted.

- [ ] **Step 5: Generate and immediately review baselines**

```bash
WORKTREE=/home/john/elspeth/.claude/worktrees/composer-1080p-wqhd-plan
PYTHONPATH="$WORKTREE/src" PLAYWRIGHT_BACKEND_PORT=8464 PLAYWRIGHT_FRONTEND_PORT=5184 \
  npx playwright test composer-workspace.visual.spec.ts --project=chromium --update-snapshots
PYTHONPATH="$WORKTREE/src" PLAYWRIGHT_BACKEND_PORT=8464 PLAYWRIGHT_FRONTEND_PORT=5184 \
  npx playwright test composer-workspace.visual.spec.ts --project=chromium
```

Expected: exactly seven baseline images are created and the immediate non-update rerun passes.

- [ ] **Step 6: Verify component a11y and commit**

```bash
npm test -- src/test/a11y/components.a11y.test.tsx
git add src/elspeth/web/frontend/package.json \
  src/elspeth/web/frontend/package-lock.json \
  src/elspeth/web/frontend/tests/e2e/composer-workspace-accessibility.spec.ts \
  src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts \
  src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots \
  src/elspeth/web/frontend/src/test/a11y/components.a11y.test.tsx
git diff --cached --name-only
git commit -m "test(web): add Composer accessibility and visual gates"
```

**Definition of Done:** keyboard/focus behavior is browser-proven, Axe finds no target violations, and seven stable visual baselines cover the approved states.

## Task 11: Update User Documentation and Run Full Project Gates

**Files:**

- Modify: `docs/guides/user-manual.md`
- Modify: `docs/guides/sharing-pipelines.md`
- Modify: `docs/agents/recent-code-hints.md` only if implementation discovers a new whole-tree/test trap

- [ ] **Step 1: Update the user-facing workspace description**

Document:

- resizable/collapsible authoring pane;
- Graph/Spec/YAML/Run tabs;
- Validation/Audit/History inspector;
- narrow Compose/Pipeline switcher;
- Graph/YAML shortcuts selecting tabs;
- Focus Graph as the optional full-screen enhancement.

Replace the stale statement in `docs/guides/sharing-pipelines.md` that `CompletionBar` is mounted in the side rail; it is now mounted in the artifact action bar. Do not change runtime/API or sharing semantics.

- [ ] **Step 2: Run the focused frontend suite**

From `src/elspeth/web/frontend`:

```bash
npm test
npm run typecheck
npm run typecheck:workspace-e2e
npm run lint
npm run lint:css
npm run build
```

Expected: all commands exit 0.

- [ ] **Step 3: Run the full local Playwright suite from the worktree**

```bash
WORKTREE=/home/john/elspeth/.claude/worktrees/composer-1080p-wqhd-plan
PYTHONPATH="$WORKTREE/src" PLAYWRIGHT_BACKEND_PORT=8465 PLAYWRIGHT_FRONTEND_PORT=5185 npm run test:e2e
```

Expected: all default Chromium Playwright specs pass.

- [ ] **Step 4: Run the Python CI-equivalent suite**

From the worktree root:

```bash
.venv/bin/python -m pytest tests/
```

Expected: the full suite reaches its normal passing summary. Do not substitute a scoped run for this gate.

- [ ] **Step 5: Compare the key-free trust-tier corpus**

```bash
set +e
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  .venv/bin/elspeth-lints check --rules all --root src/elspeth --format json \
  > .codex/composer-1080p/trust-tier-after.raw.json \
  2> .codex/composer-1080p/trust-tier-after.stderr
TRUST_TIER_AFTER_EXIT=$?
set -e
test "$TRUST_TIER_AFTER_EXIT" -eq 0 || test "$TRUST_TIER_AFTER_EXIT" -eq 1
jq -S 'sort_by(.)' .codex/composer-1080p/trust-tier-after.raw.json \
  > .codex/composer-1080p/trust-tier-after.json
cmp .codex/composer-1080p/trust-tier-before.json \
  .codex/composer-1080p/trust-tier-after.json
```

Expected: the key-sorted, finding-sorted JSON corpus is byte-identical. Inspect stderr only for runner diagnostics; if the JSON differs, use a structured JSON diff to identify every added/removed binding and resolve frontend-caused drift before continuing. After a successful comparison, remove only the six task-owned `trust-tier-before*`/`trust-tier-after*` files below `.codex/composer-1080p/`. Never sign or edit signatures.

- [ ] **Step 6: Run Wardline because localStorage is an external-input boundary**

```bash
WARDLINE_TMP_DIR="$(mktemp -d /tmp/elspeth-composer-wardline.XXXXXX)"
wardline scan . --fail-on ERROR --fail-on-inert \
  --trust-pack scripts.wardline_pack --allow-custom-packs --local-only \
  --format jsonl --output "$WARDLINE_TMP_DIR/findings.jsonl"
wc -l "$WARDLINE_TMP_DIR/findings.jsonl"
```

Expected: exit 0. Inspect the task-owned JSONL artifact and report its location in the handoff; it is outside the repository and does not dirty either checkout. Remove that exact `mktemp` directory only after inspection if cleanup is desired.

- [ ] **Step 7: Commit documentation and any justified code-hint update**

```bash
git add docs/guides/user-manual.md docs/guides/sharing-pipelines.md
if ! git diff --quiet -- docs/agents/recent-code-hints.md; then
  git add docs/agents/recent-code-hints.md
fi
git diff --cached --name-only
git commit -m "docs: describe the Composer desktop workspace"
```

- [ ] **Step 8: Verify the isolated handoff**

```bash
git status --short --branch
git log --oneline --decorate -12
git diff release/0.7.2...HEAD --stat
```

Expected: the worktree branch contains only the approved workspace plan/implementation/docs commits and is clean. The main checkout remains on its existing branch with its concurrent engine fixes untouched.

**Definition of Done:** documentation matches the implemented workspace, all frontend/browser/Python/static/trust-boundary gates are evidenced, and the isolated branch is clean and ready for an explicitly coordinated integration after engine fixes land.

## Integration Boundary

Do not merge this branch into the main checkout during implementation. When the engine-fix landing window is complete, inspect both worktrees, identify any overlapping frontend changes, update this worktree branch against the user-named integration tip, rerun Tasks 9–11 on the integrated result, and only then request permission for the final local merge. Never discard or overwrite concurrent main-checkout changes to make the integration easier.
