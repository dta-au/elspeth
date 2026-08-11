# Composer Desktop Workspace Design

Date: 2026-08-11
Status: Proposed — artifact-first direction approved; detailed design pending review

## Problem

The Web Composer technically reflows at small widths, but its desktop layout is
not useful on the screens where it is expected to do serious authoring work.
At a 1920×947 CSS viewport, the current shell gives 1600px to chat and fixes the
side rail at 320px, while the persistent graph is only about 303×98px. Most of
the working area becomes empty transcript space, useful pipeline context is
compressed into stacked cards, and the graph becomes practical only after
opening a full-screen modal.

The problem gets worse when a 1080p display is used with browser chrome,
operating-system scaling, or a short browser window. The shell, chat transcript,
composer, side rail, expanded audit/validation cards, banners, dialogs, and
guided-mode rail all compete for vertical space through several independent
scroll and clipping rules. Controls can remain technically present while the
workflow becomes hard to understand and operate.

The redesign must make the Composer genuinely supportable across common
desktop conditions from 1080p through WQHD. “Supportable” means that the user
can see and manipulate the pipeline artifact, converse with the assistant,
inspect readiness and audit evidence, and reach primary actions without using a
special modal or fighting nested scroll containers.

## Design Goals

1. Make the pipeline artifact the primary persistent workspace, not a side-rail
   thumbnail.
2. Keep chat available as an authoring control surface without allowing it to
   monopolize the viewport.
3. Preserve access to validation, audit, run evidence, and primary actions at
   every supported desktop viewport.
4. Give each pane one predictable scroll region and eliminate routine
   page-within-panel scrolling.
5. Use one outer workspace model for freeform and guided composition.
6. Preserve existing backend, pipeline, audit, mutation, and execution
   semantics.
7. Make the responsive contract executable through rendered-browser geometry,
   accessibility, and visual regression tests.

## Supported Viewport Contract

The implementation will treat CSS viewport size—not monitor marketing
resolution—as the acceptance boundary. The following matrix represents common
1080p and WQHD usage after browser chrome and display scaling:

| Class | Acceptance viewport | Typical condition |
| --- | ---: | --- |
| 1080p, 100% | 1920×900 | Full-screen browser with browser chrome |
| 1080p, 125% | 1536×760 | OS/browser scaling with chrome |
| 1080p, 150% / compact | 1280×720 | High scaling or constrained desktop window |
| WQHD, 125% | 2048×1050 | Scaled WQHD with chrome |
| WQHD, 100% | 2560×1280 | Full-screen WQHD with chrome |

The matrix is a minimum support contract, not a list of breakpoint-specific
mockups. Layout behavior must remain continuous between these sizes.

The existing narrow reflow contract at 375×667 and 320×256 remains in force and
must not regress, but a broader mobile redesign is outside this work.

## Approved Experience

### Artifact-first workspace

The Composer becomes a two-pane desktop workspace with an optional inspector:

```text
┌──────────────────────────────── application header ────────────────────────────────┐
│ session / mode / status                                             global actions │
├────────────── resizable authoring pane ─┬──────────── artifact workspace ──────────┤
│ Chat / guided decision                  │ Graph | Spec | YAML | Run                │
│                                        │                                            │
│ transcript or current guided step       │ active artifact view                       │
│                                        │                                            │
│                                        │                                            │
│ pinned composer / guided actions        │                              [Inspector]   │
└─────────────────────────────────────────┴────────────────────────────────────────────┘
```

- The authoring pane defaults to a compact but comfortable width and is
  resizable between bounded limits.
- The artifact workspace consumes the remaining width and has an explicit
  minimum usable size.
- The authoring pane can collapse to a persistent reopen affordance.
- The inspector opens over, or within, the artifact workspace instead of
  permanently creating a third narrow column.
- At wider WQHD sizes the same geometry may leave more room around the active
  artifact; it does not introduce a separate dockable-layout system.

The default desktop width is 420px. The user may resize the pane from 360px to
640px while the artifact workspace retains at least 640px. On viewport changes,
the saved width is clamped to the currently valid range. If the viewport cannot
support both minima, compact mode uses a 360px authoring pane and the remaining
space for the artifact. At every desktop acceptance viewport, the active
artifact panel retains at least 420px of content height after its own tabs and
actions. Explicit collapse is always available.

These values are starting contracts to verify during implementation. They may
be tuned by a small amount when rendered content proves a better value, but the
artifact minimum and all acceptance viewports must remain green.

### Artifact tabs

The persistent artifact workspace provides four tabs:

- **Graph** — the existing pipeline graph at useful canvas size, selected by
  default when a pipeline exists.
- **Spec** — the structured pipeline summary and plugin/configuration details.
- **YAML** — the existing YAML view, including its current copy/download
  behavior and permission rules.
- **Run** — current or most recent run state and `InlineRunResults` content.

When no pipeline exists, Graph remains the default and shows an actionable
empty state that explains that the pipeline will appear as the conversation
creates it. The area must not look like a broken blank canvas.

The existing full-screen graph remains available as “Focus graph.” It becomes
an enhancement for detailed inspection, not the only way to see a usable
graph. Existing graph and YAML deep-link/shortcut behavior should select and
focus the corresponding workspace tab before invoking any full-screen mode.

### Authoring pane

Freeform mode keeps the transcript and composer together:

- The transcript is the pane’s single scrolling body.
- The composer is pinned to the bottom inside the pane.
- The textarea has a viewport-relative maximum height at every desktop size;
  it scrolls internally after reaching that bound.
- Streaming, acknowledgements, and error recovery remain visible above the
  composer without permanently consuming most of the pane.
- Collapsing chat does not stop an in-flight operation. The reopen affordance
  exposes busy/error/unread state without relying on color alone.

The saved pane width and collapsed state are local interface preferences. They
are not pipeline state, are not included in audit evidence, and do not alter
server-side session semantics. Missing or corrupt preference data fails open to
the default width and expanded state.

### Guided-mode convergence

Guided composition uses the same outer workspace rather than maintaining a
second fixed 320px artifact rail:

- The current question, explanation, and guided choices occupy the authoring
  pane.
- Guided progress appears compactly in the authoring header or workspace action
  bar.
- The same Graph, Spec, YAML, and Run tabs stay visible in the artifact
  workspace as the proposal evolves.
- Completion actions and run evidence use the common action bar and Run tab.
- Exiting to freeform changes authoring content, not the surrounding geometry.

This convergence is visual and structural only. It must not change the guided
state machine, proposal lifecycle, acknowledgement rules, or custody semantics.

### Inspector and status

Audit and validation stop competing as permanently expanded side-rail cards.
Their summaries become compact status controls in the workspace action bar.
Opening either control reveals a single Inspector surface with tabs for:

- Validation
- Audit
- Decisions/history when that information is present

Only one inspector body is visible at a time. It has one scroll region, a clear
close action, and a stable accessible name. At the compact acceptance viewport
it overlays the artifact area and is dismissible; it never squeezes both main
panes below their minimum widths.

Status summaries retain severity, counts, and readiness wording. No state may
be communicated by color alone. An inspector rendering failure is isolated so
chat and the artifact workspace remain usable.

### Primary actions

Run, Save/Export, and other state-dependent primary actions live in a compact,
sticky workspace action bar. They remain reachable without scrolling the
transcript, graph, or inspector. Action enablement and labels continue to come
from existing mutation, validation, execution, and readiness state.

Import, catalog browsing, and other lower-frequency actions move to a clearly
labeled secondary menu or to the relevant empty/artifact view. This reduces
permanent chrome without hiding the actions behind chat history.

### Banners and dialogs

The shell currently allows several banners to stack above the workspace. The
redesign presents the highest-priority active banner inline and groups other
notices behind a status/notification control. This keeps critical states
visible while bounding header growth.

Composer dialogs use a viewport-bounded frame:

- maximum height `calc(100dvh - 32px)`;
- one scrolling dialog body;
- title and action footer remain visible;
- focus is trapped, restored to the invoker, and never obscured by sticky
  workspace controls.

The exact notice-priority policy remains the existing application policy unless
multiple simultaneous banners expose an ambiguity during implementation. That
ambiguity must be resolved explicitly rather than by silently dropping a
notice.

## Responsive Behavior

### Standard desktop: 1536px and wider

- Both main panes are visible by default.
- The authoring pane uses the saved, clamped width.
- The artifact workspace owns the remaining width.
- The inspector overlays or occupies space inside the artifact workspace.
- Labels remain visible on primary actions unless actual rendered geometry
  requires the compact label treatment.

### Compact desktop: 1280px to 1535px

- The authoring pane defaults/clamps to 360px.
- The artifact toolbar uses compact labels or icons with accessible names.
- The inspector is an overlay/drawer over the artifact workspace.
- Secondary actions move into their menu.
- The graph stays persistent and usable; it is not replaced by a thumbnail.

### Short height

Height-aware compact behavior is based on available CSS viewport height, not
width alone:

- workspace header and tab/action bars use compact spacing;
- non-critical explanatory copy may collapse behind disclosure controls;
- transcript, artifact, and inspector bodies scroll independently within their
  pane;
- composer and primary actions remain pinned and visible;
- dialogs remain bounded to the viewport;
- a routine workflow does not require scrolling an outer page to find controls.

The outer application may retain a defensive short-viewport fallback for
exceptionally small heights, but it is not the normal scroll owner for the five
desktop acceptance viewports.

### Narrow reflow

Below the existing desktop-collapse threshold, the Composer may continue to
stack its regions. The new shared workspace components must preserve the
existing no-horizontal-overflow and control-reachability guarantees at 375×667
and 320×256. Mobile pane resizing is disabled; users switch between authoring
and artifact views through the tab/reflow affordance.

## Scrolling and Geometry Rules

The layout follows these invariants:

1. The application shell fills the dynamic viewport (`dvh`) below any visible
   notice/header region.
2. The authoring pane, artifact workspace, and inspector use `min-height: 0`
   and `min-width: 0` at the correct grid/flex boundaries.
3. The chat transcript is the only routine vertical scroller in the authoring
   pane.
4. The active artifact view is the only routine content scroller in the
   artifact workspace. Graph panning/zooming remains graph interaction, not
   page scrolling.
5. The inspector body is the only vertical scroller in the inspector.
6. Sticky composers, action bars, and dialog footers do not cover focused
   content; scroll padding accounts for their occupied space.
7. No supported viewport has document-level horizontal overflow.
8. Resizing never makes the primary action bar, collapse/reopen control, or
   active artifact tab unreachable.

## Pane Resize and Collapse Interaction

The divider between authoring and artifact panes is a semantic separator:

- `role="separator"` with vertical orientation and current/min/max values;
- pointer drag support with pointer capture;
- Left/Right Arrow keyboard adjustment;
- Shift+Arrow for a larger step;
- Home/End to move to the current minimum/maximum;
- a nearby named button to collapse or restore the authoring pane;
- a visible focus indicator and minimum 24×24px interactive hit area.

Resize updates are animation-frame bounded to avoid excessive rendering. Width
is persisted after the interaction settles, not on every pointer event. A
window resize recalculates and clamps the valid width without destroying the
user’s stored preference.

## Accessibility

- Visual and DOM reading order remain authoring pane, separator, artifact
  workspace, then inspector when open.
- Tab selection uses the ARIA tab pattern with keyboard navigation and a
  labelled tab panel.
- Collapsed panes have a stable, keyboard-reachable restore control.
- Inspector and dialog focus return to the exact invoking control on close.
- Streaming/status announcements remain polite and do not repeat on every
  render.
- Readiness, error, warning, and busy states include text or accessible labels,
  not color alone.
- Resize, collapse, tab selection, graph focus mode, inspector, and all primary
  actions are keyboard operable.
- Existing 400% zoom/reflow behavior is retained; the desktop matrix adds
  explicit scaled-display coverage rather than treating monitor resolution as
  proof.

## Failure Handling

- Artifact rendering is wrapped at the pane boundary. A graph/spec/YAML/run
  rendering failure shows a recoverable error in that tab without taking down
  chat.
- Inspector rendering failure does not block authoring or primary actions.
- Corrupt or unavailable local layout preferences reset to defaults without a
  server request or audit event.
- If an artifact tab becomes unavailable because session state changes, focus
  moves to the nearest available tab and the user receives a concise status
  message.
- If the viewport changes during pointer resize, the active width is clamped
  immediately and the separator remains reachable.
- Existing session, stream, mutation, validation, and execution errors retain
  their current recovery semantics and audit behavior.

## Component Boundaries

The implementation should introduce a small workspace composition layer rather
than continue extending `App.tsx` and `SideRail.tsx`:

- `ComposerWorkspace` owns pane geometry and composes the authoring, artifact,
  inspector, and action surfaces.
- `WorkspacePaneState` (hook/reducer) owns collapse, width clamping, active
  artifact tab, and local preference parsing/persistence.
- `ArtifactWorkspace` owns Graph/Spec/YAML/Run tab presentation while reusing
  existing graph, YAML, pipeline summary, and run-results components.
- `WorkspaceInspector` owns validation/audit/history tabs while reusing current
  detail components and data.
- `WorkspaceActionBar` presents current readiness/status/actions without
  recalculating their business rules.
- Freeform and guided authoring components supply content to the common
  authoring pane.

These are responsibility boundaries, not required filenames. The implementation
plan must confirm the live component seams before naming exact files.

No new backend endpoint, response field, persistence table, pipeline model, or
audit record is required. If implementation reveals that a frontend surface
cannot be built from existing authoritative state, that is a design exception
to review rather than permission to duplicate business logic in the client.

## Verification Contract

### Browser geometry

Playwright covers all five desktop acceptance viewports for representative
states:

- freeform with no pipeline;
- freeform with a populated pipeline and long transcript;
- guided workflow with an active decision;
- expanded validation and audit details;
- pending acknowledgement or blocking warning;
- active/completed run evidence;
- stacked-notice input reduced to the consolidated banner treatment;
- confirmation dialog with content tall enough to scroll.

Rendered assertions verify:

- authoring and artifact panes are visible or intentionally collapsed;
- artifact width remains at or above 640px;
- the active artifact panel retains at least 420px of content height;
- primary actions and pane controls are inside the viewport;
- sticky regions do not overlap focused content;
- no document-level horizontal overflow exists;
- each pane exposes no more than its intended routine vertical scroller;
- the compact inspector does not shrink both main panes;
- tab, resize, collapse, inspector, and dialog interactions work by keyboard.

The existing 375×667 and 320×256 cases remain in the reflow suite.

### Visual regression

Stable seeded states receive a deliberately small screenshot matrix rather than
snapshotting every state at every size:

- populated freeform at 1920×900, 1536×760, 1280×720, and 2560×1280;
- guided active decision at 1536×760;
- inspector open at 1280×720;
- tall dialog at 1280×720.

Snapshots mask or seed timestamps, identifiers, and streaming content. Visual
baselines supplement geometry assertions; they are not the only responsive
gate.

### Unit and component tests

- width clamping at every acceptance viewport;
- corrupt, missing, and out-of-range preference restoration;
- collapse/restore and viewport-resize reducer transitions;
- keyboard separator behavior and ARIA values;
- artifact-tab availability and fallback focus;
- inspector open/close focus restoration;
- freeform/guided use of the common workspace shell;
- error isolation between authoring, artifact, and inspector panes.

CSS source-string checks may guard a narrow structural convention but cannot be
the primary proof. The responsive contract is rendered and measured in a real
browser.

### Project gates

Implementation verification includes focused frontend unit and Playwright
tests, frontend lint/typecheck/build, the full `pytest tests/` run, the
trust-tier finding-corpus comparison required by project policy, and Wardline
when a change touches an external-input boundary. Pure layout changes are not
expected to alter trust boundaries or judge-signature bindings.

## Delivery Slices

The implementation plan should use independently reviewable vertical slices:

1. **Acceptance harness** — add seeded states and failing rendered geometry
   tests for the five viewport contract.
2. **Workspace shell** — add common two-pane geometry, separator interaction,
   collapse, clamping, and local preference behavior.
3. **Persistent artifact** — move existing graph/spec/YAML/run views into the
   artifact tabs and demote full-screen graph to focus mode.
4. **Inspector and actions** — consolidate validation/audit details and primary
   actions; retire the permanent side-rail layout.
5. **Guided convergence** — render guided authoring in the common workspace and
   remove the separate fixed-width guided artifact rail.
6. **Height hardening** — bound composer growth, notices, dialogs, and short
   viewport behavior; eliminate unintended nested scroll owners.
7. **Accessibility and visual proof** — complete keyboard/focus coverage,
   screenshot baselines, narrow-reflow regression, cleanup, and documentation.

Each slice should keep current Composer behavior operable and should not require
an all-at-once backend/frontend cutover.

## Out of Scope

- A general-purpose dockable or arbitrarily rearrangeable panel system.
- Mobile information-architecture redesign beyond preserving current reflow.
- Direct graph editing or a new graph-rendering engine.
- Backend/API/database changes, new audit events, or changes to mutation,
  validation, custody, acknowledgement, or execution semantics.
- A new application-wide design system or brand refresh.
- Redesigning shared read-only inspection pages unless a reused workspace
  component makes a strictly compatible improvement trivial.
- Removing full-screen graph, YAML download, current keyboard shortcuts, or
  existing accessibility support.

## Success Criteria

The redesign is complete when all five desktop acceptance viewports allow a
keyboard or pointer user to:

1. author or continue a freeform/guided conversation;
2. inspect a persistent, useful pipeline artifact;
3. open validation and audit details;
4. reach the correct primary actions and run evidence;
5. complete the workflow without document-level horizontal overflow, hidden
   controls, modal-only graph access, or routine nested scrolling.

The result should feel like an authoring workspace whose assistant and artifact
cooperate, not a chat page with a diagnostic sidebar.
