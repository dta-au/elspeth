# ARGOnite Look (mechanical half) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ARGO's look and feel Elspeth's own composer look in both modes, keeping every
control, with all unit-level contract gates green. Visual snapshot re-baselining and the
Playwright browser pass are deferred until 0.8.10 settles.

**Architecture:** Change values through `tokens.css` first: palette, new role tokens, material
tokens and fonts. Then restyle the shared primitives (`.btn` family, a new switch). Then apply
targeted CSS and small markup changes per surface: header, workspace panes, graph, chat and the
action bar. Finally add the forced-colours fallbacks to `themes.css`. Every pinned value that
changes is updated in its test with a one-line reason; no gate is deleted.

**Tech Stack:** React 18, TypeScript, Vite, plain CSS with custom properties, Vitest with
jsdom, and React Testing Library. Fonts come from `@fontsource`.

**Spec:** `docs/superpowers/specs/2026-11-07-argonite-look-design.md` (read it first, including
"UX review findings and resulting decisions"). Full review:
`docs/superpowers/specs/2026-11-07-argonite-ux-review.md`.

All paths below are relative to `src/elspeth/web/frontend/` unless they start with `docs/`.
Work in the worktree `.claude/worktrees/argonite-look-20261107` on branch
`feat/argonite-look-20261107`. **Never** run `npm run build` outside this worktree; its
`postbuild` prunes assets.

## Global Constraints

- Every control present today stays present, at the same or fewer clicks. No control is
  removed, merged away, or hidden behind a new disclosure.
- Classification tokens (`--color-classification-*`) are unchanged byte for byte.
- All nine graph badge types stay mutually distinct and pass the existing badge gates in both
  modes.
- Contrast floors: text 4.5:1, large text 3:1, and control/focus boundaries 3:1, in both
  modes. Shadows never carry a boundary alone.
- **Run pipeline stays `variant="danger"` (red).** This was a deliberate operator decision on
  2026-08-15 (`components/sidebar/ExecuteButton.tsx:737`). The mockup's green Run was wrong. Run
  gets the ARGOnite pill shape and shadow, not a new colour.
- The ELSPETH wordmark text and its JetBrains Mono face stay; mono is the audit register it
  owns (`components/header/header.css:58`). The ARGOnite nod is a navy `#1e3c50` chip with a
  yellow `#fad673` wordmark.
- Pane collapse controls stay in the workspace action bar.
- No `!important`, no ID selectors. New selectors match existing specificity or use `:where()`.
  `themes.css` stays imported last.
- Fonts are self-hosted via `@fontsource` only. No runtime Google Fonts.
- Borrowed ARGO values carry the comment
  `/* ARGOnite: from ARGO (dta-au) client index.css — reuse assumed MIT, see design doc */`.
  Put it once per block, not per line.
- Each commit keeps every previously passing test green (Elspeth `AGENTS.md`). Before each
  commit, run `npx vitest run <the task's test files>`; at the task's end, run `npx vitest run`.
- A changed pin in a test gets a one-line comment naming the reason and `ARGOnite`, e.g.
  `// ARGOnite: nav is DTA green; body text on nav is now --color-on-nav (design doc §UX review)`.
- Run `bash scripts/branch-safety-check.sh --intent commit` (from the repo root) before each
  commit; FAIL blocks. The venv WARN is expected for frontend-only work.

## Review Focus

1. **Windows High Contrast / forced colours.** The active tab, switch state, node edges, icon
   button edges and badges must stay visible when shadows and backgrounds are stripped. Task 9
   adds `themes.css` rules, and `forcedColors.test.ts` asserts each selector carries a
   system-colour border.
2. **A disabled Run must not look runnable.** The pill shape and shadow must not survive on
   `.btn:disabled`. Task 4's cascade test asserts that the disabled rule removes `box-shadow`
   and outranks `.btn-danger`.
3. **Keyboard focus on filled pills** (green Send, red Run, the white active tab). Task 4
   asserts the double-ring focus recipe on `.btn-primary` and `.btn-danger` and
   `outline-offset: 2px` on the base.
4. **A two-digit Checks count** ("Checks 25"). Task 6 asserts the glyph badge is a pill with
   `min-width` and horizontal padding, not a fixed circle.
5. **Quarantine switch polarity.** Checked must mean the routes are shown, after any number of
   toggles. Task 7's RTL test toggles twice and asserts the `checked` state and the route
   visibility each time.

---

### Task 1: Fonts and typography tokens

**Files:**
- Modify: `package.json`, `package-lock.json` (add `@fontsource/bricolage-grotesque` and
  `@fontsource/instrument-sans`, both `^5.3.0`, under `dependencies` next to `@fontsource/inter`)
- Modify: `src/styles/fonts.css`
- Modify: `src/styles/tokens.css` (the typography block, around line 214)
- Modify: `src/styles/base.css` (the headings rule, if it sets a family)
- Test: create `src/styles/fonts.test.ts`

**Interfaces:**
- Produces: `--font-sans` (Instrument Sans first, then the existing fallback stack);
  `--font-display: "Bricolage Grotesque", "Instrument Sans", <existing sans fallback>`;
  `--font-mono` unchanged.

- [ ] **Step 1: Write the failing test** `fonts.test.ts`. Read `fonts.css` and `tokens.css` as
  text, the same way `colorContrast.test.ts` loads its CSS, and assert:
  - `fonts.css` contains `@import "@fontsource/instrument-sans/latin-{400,500,600,700}.css"`
    and `@import "@fontsource/bricolage-grotesque/latin-{600,700,800}.css"`.
  - The Inter and JetBrains Mono imports are still present. Inter stays as a fallback face.
  - `--font-sans` in `:root` starts with `"Instrument Sans"`.
  - `--font-display` is defined and starts with `"Bricolage Grotesque"`.
  - No `fonts.googleapis.com` string appears in any file under `src/`.
- [ ] **Step 2:** Run `npx vitest run src/styles/fonts.test.ts`. Expected: FAIL (missing
  imports and token).
- [ ] **Step 3:** `npm install @fontsource/bricolage-grotesque@^5.3.0 @fontsource/instrument-sans@^5.3.0`.
  Add the imports to `fonts.css` and update its header comment (families, weights, OFL-1.1).
  Set the tokens. Apply `font-family: var(--font-display)` to `h1–h4` in `base.css`. If any
  third-party notice file lists `@fontsource/inter` (search the repo, excluding
  `node_modules`), add the two new packages there.
- [ ] **Step 4:** Run `npx vitest run src/styles`. Expected: PASS. Then run
  `npm run typecheck && npm run lint:css`, which must be clean.
- [ ] **Step 5:** Commit with `feat(frontend): ARGOnite fonts — Instrument Sans UI, Bricolage display`.

### Task 2: ARGOnite role and material tokens

**Files:**
- Modify: `src/styles/tokens.css` (add to both `:root` and `[data-theme="light"]`)
- Modify: `src/styles/tokens.ts` (export only the roles graph code reads: edge, node border,
  selected ring)
- Test: `src/styles/surfaceElevation.test.ts`, `src/styles/tokenContracts.test.ts`
  (add cases), `src/styles/tokenReferences.test.ts` (must stay green)

**Interfaces:**
- Produces these custom properties, defined in both blocks. Values are dark / light (from
  the spec and the review):

| Token | Dark | Light |
|---|---|---|
| `--color-brand-navy` | `#1e3c50` | `#1e3c50` |
| `--color-brand-yellow` | `#fad673` | `#fad673` |
| `--color-on-nav` | `#e8eef2` | `#ffffff` |
| `--color-on-nav-muted` | `#a8e3c4` | `#c8ebd7` |
| `--color-nav-control` | `rgba(255,255,255,0.10)` | `rgba(0,0,0,0.20)` |
| `--color-pane-header` | `#11282e` | `#ecf6f0` |
| `--color-well` | `#0c2420` | `#dcefe4` |
| `--color-track` | `#244a42` | `#cde3d6` |
| `--color-pill` | `#e8eef2` | `#ffffff` |
| `--color-on-pill` | `#141d1b` | `#13293a` |
| `--color-graph-edge` | `#5f8a7d` | `#5f7a6e` |
| `--color-node-border` | `#5f8a7d` | `#6b8a7c` |
| `--color-btn-primary-text` | `#13293a` | `#13293a` |
| `--shadow-card` | `0 1px 3px rgba(0,0,0,0.4), 0 8px 24px rgba(0,0,0,0.45)` | `0 1px 3px rgba(16,40,36,0.1), 0 8px 24px rgba(16,40,36,0.13)` |
| `--shadow-pill` | `0 1px 2px rgba(0,0,0,0.4), 0 4px 10px rgba(0,0,0,0.3)` | `0 1px 2px rgba(16,40,36,0.2), 0 4px 10px rgba(16,40,36,0.16)` |
| `--shadow-track` | `inset 0 2px 5px rgba(0,0,0,0.7), inset 0 -1px 0 rgba(255,255,255,0.05)` | `inset 0 2px 3px rgba(16,40,36,0.22), inset 0 -1px 0 rgba(255,255,255,0.7)` |

- Shape tokens in `:root` only: `--radius-pane: 20px; --radius-card: 16px; --radius-node: 14px;`
  and `--size-pane-header: 46px; --space-pane-gutter: 14px;`.

- [ ] **Step 1: Write failing tests.**
  - In `surfaceElevation.test.ts`, add "ARGOnite material tiers change only colour between
    themes": `--shadow-card`, `--shadow-pill` and `--shadow-track` have identical offsets and
    blurs in both blocks. Reuse the file's existing geometry-parse helper.
  - In `tokenContracts.test.ts`, add "ARGOnite role tokens are theme-paired": every token in the
    table above is declared in both blocks, and the three radii are strictly ascending
    (node < card < pane).
- [ ] **Step 2:** Run `npx vitest run src/styles/surfaceElevation.test.ts src/styles/tokenContracts.test.ts`.
  Expected: FAIL.
- [ ] **Step 3:** Add the tokens with the ARGO credit comment.
- [ ] **Step 4:** Run `npx vitest run src/styles`. Expected: PASS, with `tokenReferences`
  still green.
- [ ] **Step 5:** Commit with `feat(frontend): ARGOnite role and material tokens`.

### Task 3: Palette swap, both modes

**Files:**
- Modify: `src/styles/tokens.css` (existing colour values in `:root` and `[data-theme="light"]`)
- Test: `src/styles/colorContrast.test.ts`, `src/styles/surfaceElevation.test.ts`,
  `src/styles/statusBadgeAccessibility.test.ts`, `src/styles/tokenContracts.test.ts` (as they
  stand; update pins only where noted)

**Interfaces:**
- Consumes: Task 2 tokens.
- Produces: the ARGOnite values of the existing `--color-*` tokens. Starting values follow;
  tune **lightness only** until every gate passes.
  - **Dark:**
    - `bg #141d1b`, `surface #182d3f`, `surface-elevated #1c3447`, `surface-raised #1c3447`
    - `surface-input #223b4e`, `surface-nav #0f4636`, `surface-nav-raised #13553f`
    - `text #e8eef2`, `text-secondary #c3d0d8`, `text-muted #a9b9c4`
    - `border #244a42`, `border-strong #5f8a7d`, `input-border #5f8a7d`
    - `bubble-user rgba(41,180,128,0.16)` with border `rgba(41,180,128,0.38)`; `bubble-assistant #1c3447` with border `#244a42`
    - `accent #29b480`, `btn-primary-bg #29b480`, `btn-primary-bg-hover` lighter (e.g. `#3cc391`)
    - `highlight rgba(41,180,128,0.14)`, `selected-ring #29b480`
    - `canvas-grid rgba(95,138,125,0.35)`, `badge-sink #ea8a62`
    - Every other badge foreground and background, semantic, status, link, danger and
      classification value: unchanged unless a gate fails.
  - **Light:**
    - `bg #eaf5ef`, `surface #f9fcfa`, `surface-elevated #ffffff`, `surface-raised #f1f8f4`
    - `surface-input #ffffff`, `surface-nav #288264`, `surface-nav-raised #23735a`
    - `text #414141` (body ink; `--color-heading` is not introduced, so headings use `--color-text` unless a heading rule opts into `--color-brand-navy`), `text-secondary #4f565b`, `text-muted #5b6267`
    - `border #cde3d6`, `border-strong #7a9487`, `input-border #7a9487`
    - `bubble-user #dcefe4` with border `#b6dcc6`; `bubble-assistant #ffffff` with border `#cde3d6`
    - `accent #156048`, `btn-primary-bg #29b480`, `btn-primary-bg-hover` darker (e.g. `#22a172`)
    - `selected-ring #156048`, `canvas-grid rgba(122,148,135,0.35)`
    - Badge text: `source #1f6f5a`, `transform #875c12`, `gate #7a4fab`, `sink #a04a29`; others
      as they are unless a gate fails
    - `link`: deepen as needed, because the bubble-link gate is re-evaluated against the new bg

- [ ] **Step 1:** Change the values, keeping all existing comments that still hold. Rewrite the
  ones that become false, such as the frozen-`#f4f8f9` note and the "navy navigation" family
  descriptions, so they describe the new values and why.
- [ ] **Step 2:** Run `npx vitest run src/styles`. Expected: some FAILs. For each failure,
  either retune the value (preferred) or, where the test pins a superseded role, update it
  deliberately. The anticipated deliberate update: "keeps body text at AA on the navy
  navigation surface in both themes" now asserts `--color-on-nav` and `--color-on-nav-muted` on
  `--color-surface-nav`, because the header is now a green band with light text. Its name and
  reason comment are updated.
- [ ] **Step 3:** Update "keeps primary button text at WCAG AA contrast in both themes" and
  `tokenContracts` "keeps inverse button text at WCAG AA on the hover fills" so the primary
  pair is `--color-btn-primary-text` on `--color-btn-primary-bg` and on its hover. Danger keeps
  `--color-text-inverse`.
- [ ] **Step 4:** Run `npx vitest run`. Expected: PASS everywhere. Component tests that assert
  colours are fixed by token reference, never by hex.
- [ ] **Step 5:** Commit with `feat(frontend): ARGOnite palette in dark and light`.

### Task 4: Button, focus and switch primitives

**Files:**
- Modify: `src/styles/shared.css` (`.btn`, `.btn-compact`, `.btn-primary`, `.btn-danger`, ghost, disabled)
- Modify: `src/styles/base.css` (global `:focus-visible`, if defined there)
- Create the `.switch` primitive CSS in `src/styles/shared.css`
- Test: `src/styles/buttonCascade.test.ts`, `src/styles/primitiveGeometry.test.ts`,
  `src/styles/colorContrast.test.ts` (disabled section), and a new `src/styles/switchPrimitive.test.ts`

**Interfaces:**
- Produces: `.btn*` with `border-radius: var(--radius-pill)` and
  `border: 1px solid var(--color-border-strong)`, hovering to `var(--color-well)` fill.
  `.btn-primary` uses `color: var(--color-btn-primary-text)`. Filled `.btn-primary` and
  `.btn-danger` get `box-shadow: var(--shadow-pill)`. Disabled has `box-shadow: none`,
  `background: var(--color-well)`, `color: var(--color-text-muted)` and a `border-strong`
  border.
- Focus: `:focus-visible { outline: 2px solid var(--color-focus-ring); outline-offset: 2px }`.
  Filled pills add `box-shadow: 0 0 0 2px var(--color-surface), 0 0 0 4px var(--color-focus-ring)`
  on `:focus-visible`.
- The switch markup contract for later tasks:
  `<label class="switch"><input type="checkbox" role="switch" class="switch-input" /><span class="switch-track" aria-hidden="true"><span class="switch-thumb"></span></span><span class="switch-label">…</span></label>`.
  - Off: the track is `--color-border-strong`; the thumb is `--color-pill` with a
    `border-strong` border.
  - On: the track is `--color-btn-primary-bg`; the thumb is `--color-btn-primary-text`.
  - Focus ring on `.switch-input:focus-visible + .switch-track`. The input is visually hidden
    but focusable.
  - Hit target ≥ `--size-control-compact`.

- [ ] **Step 1: Write failing tests.**
  - `buttonCascade`: "disabled filled buttons drop the pill shadow and outrank variants". Parse
    `.btn:disabled` and assert `box-shadow: none` with specificity ≥ `.btn-danger`.
  - `colorContrast` "verifies .btn:disabled uses the token pair rather than opacity dimming":
    extend it to the new well/muted pair at ≥4.5 in both themes.
  - `switchPrimitive.test.ts`: the on-thumb on the on-track ≥3:1 and the off-thumb on the
    off-track ≥3:1 in both themes (computed with the existing contrast helper); a
    `:focus-visible` rule exists for `.switch-input`; the track's min-height is
    ≥ `--size-control-compact` or the label row provides it.
  - Add "filled pills keep a visible focus ring" (Review Focus 3): `.btn-primary:focus-visible`
    and `.btn-danger:focus-visible` carry the double-ring `box-shadow`, and the base rule has
    `outline-offset: 2px`.
- [ ] **Step 2:** Run `npx vitest run src/styles`. Expected: FAIL on the new cases.
- [ ] **Step 3:** Implement. Keep every existing `:where()` hover guard exactly; that
  structure is pinned. Keep control heights on their existing rungs; `primitiveGeometry`
  must not need changes.
- [ ] **Step 4:** Run `npx vitest run`. Expected: PASS.
- [ ] **Step 5:** Commit with `feat(frontend): ARGOnite pill buttons, focus rings and switch primitive`.

### Task 5: Header band and wordmark chip

**Files:**
- Modify: `src/components/header/header.css` (`.app-header` around line 223, the header
  controls and separators)
- Modify: `src/components/common/AppHeader.tsx` (wrap `<WordMark>` in a span with class
  `app-header-brand`)
- Test: `src/components/common/AppHeader.test.tsx`, `src/components/header/headerMenuElevation.test.ts`,
  `src/styles/classNames.test.ts`

**Interfaces:**
- Consumes: `--color-surface-nav`, `--color-on-nav`, `--color-on-nav-muted`,
  `--color-nav-control`, `--color-brand-navy` and `--color-brand-yellow`.
- Produces: `.app-header-brand` (navy chip: `--radius-md`, horizontal padding `--space-sm`)
  with the wordmark coloured `--color-brand-yellow`. Pass this through `WordMark`'s `style`
  prop or a class; `WordMark` keeps the mono font.

- [ ] **Step 1: Write a failing test** in `AppHeader.test.tsx`: the wordmark renders inside an
  element with class `app-header-brand`, and its text is still "ELSPETH".
- [ ] **Step 2:** Run `npx vitest run src/components/common/AppHeader.test.tsx`. Expected: FAIL.
- [ ] **Step 3:** Implement. Header buttons (session switcher, version selector, mailbox, user
  menu) sit on `--color-nav-control` with `--color-on-nav` text, pill radius and no border.
  The model chip uses `--color-on-nav-muted`. Add the `.app-header-brand` rule in the same
  commit (`classNames` gate).
- [ ] **Step 4:** Run `npx vitest run src/components/common src/components/header src/components/sessions src/styles`.
  Expected: PASS.
- [ ] **Step 5:** Commit with `feat(frontend): ARGOnite header band and wordmark chip`.

### Task 6: Workspace panes, title bars and tab track

**Files:**
- Modify: `src/components/workspace/workspace.css` (`.composer-workspace`, `.workspace-authoring-pane`,
  `.workspace-artifact-pane`, `.artifact-workspace-toolbar`, the tab rules,
  `.artifact-tab-badge--glyph`)
- Modify: `src/components/workspace/ComposerWorkspace.tsx` (title bar above
  `.workspace-authoring-body`)
- Modify: `src/components/workspace/ArtifactWorkspace.tsx` (title inside
  `.artifact-workspace-toolbar`, before the tablist)
- Test: `src/components/workspace/workspaceChrome.test.ts`,
  `src/components/workspace/ComposerWorkspace.test.tsx`,
  `src/components/workspace/ArtifactWorkspace.test.tsx`, `src/styles/classNames.test.ts`

**Interfaces:**
- Produces:
  - `.workspace-pane-header`, a `<header>` of height `--size-pane-header` with background
    `--color-pane-header` and a bottom border, containing
    `<h2 class="workspace-pane-title">Conversation</h2>` (`--font-display`, `--font-size-lg`,
    700).
  - The artifact toolbar becomes the pipeline pane's title bar, with
    `<h2 class="workspace-pane-title workspace-pane-title--collapsible">Pipeline</h2>`.
    Below a container width of 900px it becomes visually hidden (reuse the existing
    `visually-hidden` recipe inside a `@container` rule).
  - Panes are cards: `border-radius: var(--radius-pane)`, `box-shadow: var(--shadow-card)`, a
    1px `--color-border` edge, and `--space-pane-gutter` between and around them.
  - The tablist sits in a track (`--color-track`, `--shadow-track`, pill radius, 4px inset).
    `[aria-selected="true"]` tabs are `--color-pill` / `--color-on-pill` with
    `--shadow-pill`. Inactive tabs use `--color-text-muted`.
  - The glyph badge becomes a pill: `min-height: 20px; min-width: 20px; padding: 0 6px;
    border-radius: var(--radius-pill)`, filled with the tone colour and contrasting text.

- [ ] **Step 1: Write failing tests.**
  - `ComposerWorkspace.test.tsx`: a heading named "Conversation" is rendered in the authoring
    pane, and the collapse and restore controls are still inside the action bar slot. Assert
    with the existing queries for those controls.
  - `ArtifactWorkspace.test.tsx`: a heading named "Pipeline" precedes the
    `tablist` "Pipeline artifacts"; every existing tab name, including "Checks: …", is
    unchanged.
  - `workspaceChrome.test.ts`: "the Checks glyph badge can hold a two-digit count" (Review
    Focus 4) asserts `min-width` and non-zero horizontal padding on `.artifact-tab-badge--glyph`.
    Also "the active tab is a raised pill on a recessed track" asserts the track uses
    `--shadow-track` and the selected tab uses `--shadow-pill`.
- [ ] **Step 2:** Run `npx vitest run src/components/workspace`. Expected: FAIL on the new
  cases.
- [ ] **Step 3:** Implement. Keep scroll owners, the grid and subgrid, the separator and the
  action-bar geometry. Existing `workspaceChrome` pins should hold. If a pin breaks because the
  gutter moves an edge, update it with an `ARGOnite` reason, keeping its invariant (e.g. the
  "bottom edge painted once" test).
- [ ] **Step 4:** Run `npx vitest run`. Expected: PASS.
- [ ] **Step 5:** Commit with `feat(frontend): ARGOnite pane cards, title bars and tab track`.

### Task 7: Graph well, nodes, checksum row, quarantine switch and wiring footer

**Files:**
- Modify: `src/components/inspector/inspector.css` (graph shell, React Flow node styles,
  controls panel, `.graph-checksum*`, `.graph-view-toolbar`, the wiring `details`)
- Modify: `src/components/inspector/GraphChecksum.tsx:116-160`
- Modify: `src/components/inspector/GraphView.tsx:2196-2200` (quarantine control)
- Modify: `src/components/inspector/GraphOutputs.tsx:116` (count pill in `<summary>`)
- Test: `src/components/inspector/GraphChecksum.test.tsx`, `GraphView*.test.tsx` (the files
  that assert the quarantine button), `GraphOutputs.test.tsx`,
  `src/components/inspector/reactFlowThemeScope.test.tsx`, `src/styles/classNames.test.ts`

**Interfaces:**
- Consumes: Task 4 `.switch` markup contract, and `--color-well`, `--color-graph-edge`,
  `--color-node-border`, `--color-selected-ring`, `--radius-node` and `--shadow-card`.
- Produces:
  - Checksum: `.graph-checksum` becomes one row holding the title, a `.graph-checksum-value`
    styled as a recessed pill (`--color-well`, `--shadow-track`, mono), and
    `.graph-checksum-actions` as a segmented pill group. The three `Button`s get visible text
    "Copy", "Match clipboard" and "Paste", with `aria-label`s "Copy checksum",
    "Match clipboard" and "Paste checksum". The full 64-character hash stays in the DOM. The
    pill truncates visually with `text-overflow: ellipsis`, and the full value stays
    selectable.
  - Quarantine: replace the `Button aria-pressed` with the switch: visible label "Show
    quarantine and discard routes", `checked={showFailureRoutes}`, and `onChange` toggles.
  - Graph: the canvas sits in a well (`--color-well`, `--radius-card`, `--shadow-track`, a
    12px/14px margin). Nodes get `--radius-node`, a `--color-node-border` border and
    `--shadow-card`. The selected node gets a solid 2px `--color-selected-ring` ring. Edges and
    arrowheads use `--color-graph-edge`. The React Flow controls become a rounded pill group
    with `--shadow-pill`.
  - Wiring: `<summary>` shows "Wiring" plus `<span class="graph-outputs-count">{rows.length}</span>`.
    The accessible text must still contain "Wiring" and the count; drop the parentheses
    visually only if tests allow, otherwise keep "Wiring (n)".

- [ ] **Step 1: Write failing tests.**
  - `GraphChecksum.test.tsx`: the buttons are found by role and name ("Copy checksum",
    "Match clipboard", "Paste checksum"), and their visible text is "Copy" / "Match
    clipboard" / "Paste" (`toHaveTextContent`). The existing copy/match/paste behaviour tests
    are unchanged.
  - The GraphView test asserting the quarantine control (Review Focus 5):
    `getByRole("switch", { name: "Show quarantine and discard routes" })` is checked iff the
    routes are rendered. Toggle twice and assert both states. Remove the old "Hide/Show"
    button-name assertions in the same commit, with an `ARGOnite` reason comment.
  - `reactFlowThemeScope.test.tsx`: the edge stroke and selected ring reference the new tokens
    (follow the file's existing assertion style; it explains why it asserts by token
    reference).
- [ ] **Step 2:** Run `npx vitest run src/components/inspector`. Expected: FAIL on the new
  cases.
- [ ] **Step 3:** Implement. Update `tokens.ts` exports if graph code reads colours through it.
- [ ] **Step 4:** Run `npx vitest run`. Expected: PASS.
- [ ] **Step 5:** Commit with `feat(frontend): ARGOnite graph well, checksum row, routes switch, wiring footer`.

### Task 8: Chat bubbles, composer well and action bar

**Files:**
- Modify: `src/components/chat/chat.css` (`.message-bubble-content*` around line 1248;
  `.chat-input*` from line 562)
- Modify: `src/components/workspace/workspace.css` (`.workspace-action-bar`, the completion bar
  members, `.side-rail-execute-reason`)
- Modify: `src/components/sidebar/sidebar.css` (`.side-rail-execute-btn`, if styled there)
- Test: `src/components/chat/chatBubbleGutter.test.ts`, `ChatInput.test.tsx`,
  `src/components/workspace/workspaceChrome.test.ts`, `ExecuteButton.test.tsx`

**Interfaces:**
- Consumes: Task 4 primitives.
- Produces:
  - Bubbles get `--radius-card`. The assistant bubble keeps a 1px border; the shadow is
    decoration only.
  - `.chat-input-row` becomes the composer well: `--radius-card`, `--color-surface-input`,
    `--shadow-track` and a `--color-input-border` border. The textarea inside is borderless and
    transparent. The send button is a `.btn-primary` pill inside the well; `.chat-input-icon-btn`
    are 36px circles with `border-strong` borders. Keep the DOM order and the
    `role="group"` label.
  - Action bar: transparent on the canvas, with no top rule beyond the existing "bottom edge
    painted once" contract. Buttons are pills. Run keeps `variant="danger"` and gains
    `--shadow-pill`. `.side-rail-execute-reason` uses `--color-text` when the reason is a block
    (`role="alert"`) and stays muted for advisory text.

- [ ] **Step 1: Write failing tests.**
  - `workspaceChrome.test.ts`: "a blocking run reason reads at body-text colour" asserts the
    `.side-rail-execute-reason` rule (without `--advisory`) uses `--color-text`.
  - `ExecuteButton.test.tsx`: an assertion that Run still renders with the danger variant
    class. Add it if absent; it guards the operator decision.
- [ ] **Step 2:** Run the four test files. Expected: FAIL on the new cases.
- [ ] **Step 3:** Implement. `chatBubbleGutter` pins must hold; if a radius change trips a
  gutter calculation, retune the padding, not the pin.
- [ ] **Step 4:** Run `npx vitest run`. Expected: PASS.
- [ ] **Step 5:** Commit with `feat(frontend): ARGOnite chat bubbles, composer well and action bar`.

### Task 9: Forced colours and reduced motion

**Files:**
- Modify: `src/styles/themes.css` (the existing `@media (forced-colors: active)` and
  `prefers-reduced-motion` blocks)
- Test: create `src/styles/forcedColors.test.ts`, or extend the "forced-colors accessibility
  fallbacks" describe in `colorContrast.test.ts:551` if its helpers fit

**Interfaces:**
- Consumes: the class names from Tasks 4–8.

- [ ] **Step 1: Write the failing test** (Review Focus 1). Inside `forced-colors: active`:
  - `[role="tab"][aria-selected="true"]` in the artifact tablist has a border or outline using
    `Highlight`.
  - `.switch-track`, `.switch-thumb`, the React Flow node selector used in Task 7,
    `.chat-input-icon-btn`, `.graph-checksum-actions .btn` and the graph well each carry a
    `1px solid` border or outline in `CanvasText` or `ButtonText`.
  - Badge classes keep a border.

  Inside `prefers-reduced-motion: reduce`, the switch and tab transitions are `none`.
- [ ] **Step 2:** Run `npx vitest run src/styles`. Expected: FAIL.
- [ ] **Step 3:** Implement in `themes.css` with selectors no more specific than the rules they
  override.
- [ ] **Step 4:** Run `npx vitest run`. Expected: PASS.
- [ ] **Step 5:** Commit with `feat(frontend): ARGOnite forced-colours and reduced-motion fallbacks`.

### Task 10: Whole-tree verification and hand-off

**Files:**
- Modify: `docs/superpowers/specs/2026-11-07-argonite-look-design.md`: add a "Status" note
  listing what is done and what is deferred.

- [ ] **Step 1:** In the worktree frontend, run `npm test`, `npm run typecheck`,
  `npm run typecheck:workspace-e2e`, `npm run lint` and `npm run lint:css`. Expected: all exit 0.
  Capture full output to `.claude/lanes/argonite-20261107/verify.log` (gitignored).
- [ ] **Step 2:** Run `npm run build` in the worktree only. Expected: exit 0; the built
  `index.html` still references `theme-init.js` before the module entry.
- [ ] **Step 3:** Run `npx playwright test tests/e2e/composer-workspace.visual.spec.ts --list`
  only, to confirm the specs still compile. Do **not** run or update snapshots; that is the
  deferred half.
- [ ] **Step 4:** Add the status note to the design doc:
  - Done: Tasks 1–9.
  - Deferred, to run once 0.8.10 settles: snapshot re-baseline (reviewed image by image, never
    `--update-snapshots` in bulk); Playwright geometry, accessibility, reflow, keyboard and
    visual specs on an isolated harness; real-browser checks of 1280×720 title-bar overflow,
    hover states, the collapsed pane, the expanded Wiring list, and empty/pending states.
- [ ] **Step 5:** Commit with `docs: ARGOnite mechanical half complete; browser pass deferred`.
