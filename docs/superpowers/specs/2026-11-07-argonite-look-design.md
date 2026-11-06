# ARGOnite: Elspeth's composer adopts ARGO's look and feel

Status: design, approved in conversation 2026-11-07. Branch `feat/argonite-look-20261107`,
worktree `.claude/worktrees/argonite-look-20261107`, based on `release/0.8.10-assembly`
at `c900eebd9c`.

Supersedes the direction (not the analysis) of `ARGONITE_ANALYSIS_AND_PLAN.md` in the ARGO
checkout (`dta-au` ARGO repository root, untracked), which proposed a selectable, colour-only skin.

## Intent

Elspeth's composer works, but looks like developer art. ARGO's front end for the same Elspeth
backend is enjoyable to use. The difference is mostly *feel* rather than features: depth and
soft shadows, panes with their own title bars, pill-shaped controls, a recessed sliding
toggle, icon buttons with tooltips, a warmer palette and display typography.

Elspeth is the **reference UI**: it deliberately exposes every feature. So we adopt ARGO's
feel, not ARGO's simplicity.

Why now: a demonstration to the parent agency, late in the week of 9 November or early in the
week of 16 November 2026, on Elspeth 0.8.10. This is a hedge in case ARGO's own work is not
ready. The pitch: "this is the reference architecture; it deliberately implements all
features" and it looks good. The name ARGOnite is a deliberate nod to the ARGO team. It may
ship as a celebratory part of 0.8.10 if .10 settles in time; otherwise it lands after.

The mockup the user approved: claude.ai artifact `JCT2gATUupfVwtGrszUzAR`
("Elspeth: ARGOnite look"), which shows today and ARGOnite side by side in dark and light.

## Goals

1. ARGOnite becomes Elspeth's look in both light and dark modes. There is no skin switcher.
2. Material: two-layer soft shadows on panes, cards and nodes; recessed "track" wells; raised
   pills; larger radii (panes 20px, cards and bubbles 16px, nodes 14px, controls pill).
3. Palette: ARGO's DTA palette. Light is a pale green canvas `#eaf5ef` with white surfaces and
   navy headings; dark is a deep green-black canvas `#141d1b` with navy surfaces `#182d3f`. Both
   use DTA green `#29b480` for primary and selection, and DTA yellow `#fad673` sparingly (the
   wordmark and attention badges).
4. Typography: Bricolage Grotesque for headings, pane titles and the wordmark; Instrument Sans
   for UI and body text; JetBrains Mono stays for code, checksums and badges where mono carries
   meaning. All are self-hosted via `@fontsource` (OFL-1.1, the same pattern as Inter).
5. Chrome:
   - The conversation and pipeline panes become rounded cards, each with a tinted title bar
     carrying the pane's name and pane-local tools.
   - The artifact tabs become a segmented pill tab strip in a recessed track.
   - The checksum controls are regrouped into one row: the hash in a recessed pill, plus a
     labelled segmented pill of actions (see the review decisions).
   - "Hide quarantine and discard routes" becomes a labelled switch (see the review
     decisions).
   - The wiring disclosure becomes a pane footer row with a count pill.
   - The composer input becomes one rounded well with Send inside it.
   - Run and Send become raised filled pills. **Run stays red** (`variant="danger"`). That was
     a deliberate operator decision on 2026-08-15, because Run is the one verb with egress and
     spend (`ExecuteButton.tsx`). The mockup's green Run was a mockup error, not a design
     change. Send is the green primary.
   - The wordmark keeps JetBrains Mono, the audit register it owns (`header.css`). The nod is
     the navy chip with a yellow wordmark.
6. All of this applies through tokens where possible, with targeted CSS and small markup
   changes where it doesn't.

## Non-goals

- Removing, hiding or merging any control. **Rule:** every control present today is present
  after the change, and nothing is more than one click further away.
- A skin or theme switcher, a runtime appearance registry, or cross-tab skin sync. The original
  plan's runtime work is deferred, not deleted.
- ARGO's navigation, sidebar, page band layout, folder cards or 3:2 pane grid. Elspeth's layout
  geometry, scroll owners, subgrid action bar and responsive behaviour stay.
- Backend, API, preferences, session, polling or workflow changes.
- Rebranding: the ELSPETH wordmark text stays. Only its colour and font may change.
- The marketing website's mirrored tokens (`website/tokens/`) follow later, separately.

## Anti-goals (failure even if it "works")

- Any control missing or unreachable, especially checksum Copy, Match and Paste, quarantine and
  discard visibility, Wiring, Approvals, Spec, YAML or Checks.
- The classification banner restyled. Protective-marking colours are theme-invariant and pinned.
- Graph node-type colours (source, transform, gate, sink, aggregation, coalesce, row union,
  queue, collector) collapsing into the brand accent or losing distinguishability.
- Contrast regressions: text 4.5:1, large text 3:1, and control/focus boundaries 3:1, in both
  modes, including tinted semantic surfaces.
- Focus visibility lost on pill controls, or forced-colours and high-contrast overrides
  defeated by new selectors.
- Bulk-regenerating test pins or snapshots to make the gates pass.

## Constraints

- **The CSS contract tests are part of the design.** `primitiveGeometry`, `surfaceElevation`,
  `workspaceChrome`, `overlayChrome`, `buttonCascade`, `classNames`, `colorContrast`,
  `tokenContracts`, `tokenReferences` and others parse the stylesheets and pin values. Each
  changed pin is updated deliberately, with a one-line reason in the test, and the invariant it
  protects is preserved. For example, `surfaceElevation` requires elevation ordering and
  shadow-geometry parity between themes. New shadow roles are added as new tiers rather than by
  repurposing popover, dropdown or modal.
- `classNames.test.ts` fails on any emitted class without a stylesheet rule. New classes ship
  with their CSS in the same commit.
- The pane collapse controls stay in the workspace action bar (`workspaceChrome` pins them at
  the 44px register). Title bars carry titles and pane-local tools only.
- The light `--color-bg` is load-bearing for the link-over-bubble contrast gate (see the
  `tokens.css` comment). Palette changes must re-satisfy that gate, not delete it.
- `themes.css` (high contrast and forced colours) is imported last and must keep winning. New
  selectors use `:where()` or match the existing specificity, with no `!important`.
- Fonts are self-hosted only (CSP `font-src 'self'`). No Google Fonts at runtime.
- Commits on this branch keep green tests green (Elspeth `AGENTS.md`). No commit may turn a
  passing test red.
- **Licensing (working assumption, user decision 2026-11-07):** ARGO's palette values and
  styling recipes are reused and released under MIT, on the expectation that the ARGO owners
  (a fellow government team) grant that. If they require another licence, the borrowed material
  takes that licence instead. The borrowed values carry a source comment crediting ARGO
  (`dta-au` ARGO client, `index.css` DTA palette, and the `ViewToggle` and
  `ElspethWorkflowEditor` recipes), so the material is easy to find and relicense. The fonts
  (Bricolage Grotesque, Instrument Sans) are SIL OFL-1.1 via `@fontsource`, independent of ARGO,
  and are handled exactly as Inter already is.

## Approaches considered

| | Approach | Trade-off | Decision |
|---|---|---|---|
| A | Selectable ARGOnite skin behind a two-axis appearance runtime (the original plan) | About a day of runtime plumbing before anything visible. The structural chrome (title bars, tab track, icon checksum row) is markup and can't be cleanly scoped to a skin without per-skin component branches. | Deferred |
| B | ARGOnite becomes Elspeth's own look; no switcher | Fastest visible result; one look to test. Changes the default for every user, so landing is a release decision. | **Chosen** |
| C | Shared structural upgrade, with ARGOnite as a selectable palette and typography skin on top | Gives a "flip the skin" demo moment, but adds switch plumbing and doubles the contrast and visual matrix. | Not chosen. Can be added after B if wanted. |

The user chose B after seeing the mockup and said they are "not wedded to our programmer art if
this is objectively better".

## Delivery shape (decided)

The work is split so that only the slow part waits for 0.8.10 to stabilise:

- **Now (mechanical):** tokens and palette in both modes, fonts, material tokens, the
  button/pill/track primitives, the chrome markup and CSS changes, and updated unit-level
  contract tests. All of it is verified with `vitest`, `typecheck`, `lint` and `lint:css`.
- **After .10 settles:** re-baseline the visual snapshots (reviewed image by image, never in
  bulk), run the Playwright workspace geometry, accessibility, reflow and visual specs on an
  isolated harness, and do a states pass in a real browser.
- Go/no-go is the working day before the demo. If not ready, demo plain 0.8.10.

## UX review findings and resulting decisions

An independent UX review of the mockup (2026-11-07) computed every contrast pair from the
theme values. Nothing was rendered. The review is committed beside this document as
`2026-11-07-argonite-ux-review.md`. Its scratch paths, including the contrast script, are
session-local. Its findings change the mockup as follows.
Each decision below is a design call; "(review)" marks one taken directly from the review, and
"(call)" marks one the builder made where the review offered options.

**Colour and boundary fixes (must-fix):**

- Buttons and icon buttons take ARGO's own base rule `border: 1px solid var(--line-strong)`
  (light `#7a9487` 3.27:1 on white; dark `#5f8a7d` 3.65:1 on surface). The mockup's pale
  `--line` borders failed 1.4.11. The "lift" drop-shadow is dropped from icon buttons; the
  border carries the boundary. (review)
- Light header controls sit on a dark tint `rgba(0,0,0,0.20)` over the green header (white
  text 6.65:1, muted 5.17:1), not a white tint (3.53:1, fail). Dark mode keeps the 10% white
  tint (6.91:1). (review option b, chosen as the "pressed into the band" feel) (call)
- The wordmark keeps ARGO's real brand-mark idiom: a yellow `#fad673` wordmark on a navy
  `#1e3c50` chip (8.22:1), in both modes. ARGO never puts yellow directly on its green header.
  (review 4.1, navy-chip option) (call)
- Graph edges: light `#5f7a6e` (3.89:1 on the well); dark `#5f8a7d` passes as is. Node borders:
  light `#6b8a7c` (3.15:1), dark `#5f8a7d` (4.20:1). Shadows are never the only edge. (review)
- Selected node is a solid 2px ring: light `#156048` (7.50:1), dark `#29b480` (4.86:1). No
  translucent ring. (review)
- Badges: dark keeps Elspeth's existing opaque, pre-composited badge backgrounds. The mockup's
  16% tints regressed source, gate and sink. Sink text is lifted to about `#ea8a62`. Light badge
  text is darkened slightly to clear 4.5:1 (source about `#1f6f5a`, transform `#875c12`, gate
  `#7a4fab`, sink `#a04a29`). All nine badge types, not just the four in the mockup, are tuned
  until `colorContrast.test.ts` and `statusBadgeAccessibility.test.ts` pass. (review)
- Error and danger text keeps Elspeth's error tokens (dark `#f08e8b`). ARGO's dark `--danger`
  is 3.72:1 on the surface and fails. (review)
- Light muted text stays at about `#5b6267`, slightly darker than ARGO's `#61686d`, for
  headroom. The dark field stays darker than ARGO's (`#223b4e`, not `#374c5d`). Both are
  deliberate departures in the accessible direction. (review)

**Controls (decisions):**

- Checksum actions become one segmented pill with three short visible labels: "Copy", "Match
  clipboard" and "Paste". The accessible names stay "Copy checksum", "Match clipboard" and
  "Paste checksum", so the visible label is contained in the name. This is chosen over icon-only
  because the demo audience is seeing checksum verification for the first time, and two
  clipboard glyphs are indistinguishable at 16px. It needs no new tooltip component. (review 2.2)
  (call)
- "What this checksum covers" stays the existing inline disclosure, restyled, not a new
  popover. This avoids a new undesigned state. (call)
- The quarantine and discard control becomes a switch: a real `<input type="checkbox"
  role="switch">` with a visible label, "Show quarantine and discard routes". Checked means
  shown. This follows ARGO's own `.switch` pattern in `ElspethWorkflowEditor.module.css`. Its
  thumb contrasts in both states: on is a navy thumb on green (5.64:1); off is a
  `line-strong` track with a white, bordered thumb. Tests that reference the old
  "Hide/Show …" button name are updated deliberately. (review 2.3, 3.6)
- The Checks tab badge becomes a 20px-high pill with `min-width: 20px` that can hold a count
  ("25"). Its fill is semantic: pass, warning, or fail using Elspeth error tokens, plus a
  running variant that is safe under reduced motion. (review 2.1, 3.2)
- Pane title bars are 46px (ARGO's `min-height`). The pipeline pane's "Pipeline" title is
  visually hidden when the pane is too narrow for title, tab track and catalog together. It stays
  the region's accessible name. (review 2.1, 4.5) (call)

**States the build must design (review section 3):**

- Must-fix:
  - Focus-visible on every pill uses Elspeth's focus-ring tokens with `outline-offset: 2px`,
    and filled pills get a double ring (`0 0 0 2px surface, 0 0 0 4px focus-ring`).
  - Disabled Run is drawn with an inset fill, muted text, a `line-strong` border and no
    shadow, not ARGO's opacity 0.6. The run-gate reason beside Run uses `text` colour, not
    muted, when it is blocking.
  - Checks failed and warning states, and the validation banner, use Elspeth's error and
    warning tokens.
  - Forced colours: the active tab, switch, nodes, icon buttons, the graph well and badges get
    `CanvasText`/`Highlight` borders in `themes.css`, because every shadow-carried affordance
    vanishes there.
- Should-fix: hover (the `inset` fill), the "Awaiting your decision" card (ARGO's warning
  pairs), the collapsed conversation pane, Wiring expanded, empty and pending states.
- Later: reduced-motion polish for slide transitions, and the pressed/active tab.

## Open items left to the builder

- The exact tuned hex values that satisfy every contrast gate. Start from ARGO's values and
  the review's proposals, and adjust lightness only, keeping hue.
- Per-component spacing inside title bars, within the existing height tokens.
- Where the tab-track title collapse threshold sits. Measure it on a real 1280×720 build in the
  later browser pass.
