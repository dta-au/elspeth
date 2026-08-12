# Chrome Conformance Inventory — ELSPETH Pipeline Composer frontend

Filigree step: elspeth-be21168a80 · Read-only sweep, 2026-08-13 · Method: scripted
extraction of all 217 interactive elements (`<button>`, `<summary>`, `role=tab/menuitem`)
across `src/elspeth/web/frontend/src/components/**/*.tsx`, paired against every rule
block in all 23 CSS files, then manual verification of every non-`.btn` hit.
All paths below are relative to `/home/john/elspeth/src/elspeth/web/frontend/src/`
unless prefixed `tests/`.

**Population:** 217 interactive elements; 65 compose the `.btn` system directly;
of the remaining 152, most carry a complete bespoke skin (the guided-turn family,
chat-input family, catalog chips, header triggers are all fully skinned with
hover + focus). The genuinely unskinned population is concentrated in ONE
subsystem: the shared-workspace chrome (`components/workspace/*`), plus a small
tail of stray controls. There is no global `button {}` reset in `styles/base.css`,
so a classless button renders full UA default chrome — **UA system font included**
(body font does not inherit into buttons), which is why these controls read as
foreign next to tokenized siblings, not merely "flatter".

A global focus ring exists (`styles/base.css:83-86` `:focus-visible { outline: 2px
solid var(--color-focus-ring) }`; the only `outline: none` in the tree is the
correct `:focus:not(:focus-visible)` at base.css:89). **No WCAG 2.4.7 failures
were found anywhere** — every control below inherits the global ring. The
"focus ok?" column therefore records whether the control has a *deliberate*
treatment vs. riding the global default.

---

## 1. Inventory table

### Tier 1 — unskinned (structure-only or no CSS at all); the jank mechanism

| # | Control | CSS | TSX | Screens | Sits beside | Remedy | Height-shift risk | Test collisions | focus-visible |
|---|---------|-----|-----|---------|-------------|--------|-------------------|-----------------|---------------|
| 1 | Status chips "Validation" / "Audit" | `.workspace-status-control` workspace.css:134-148 (min-height + gap only; `data-tone` rules set border-**color** onto the UA border) | WorkspaceActionBar.tsx:43-53 | Main composer, bottom action bar (always) | Three co-equal `.btn` CompletionBar buttons (44px) + "More actions" | **(a)** compose `.btn-compact` in TSX; keep `.workspace-status-control` for the data-tone border-color overrides (they outrank `.btn-compact` on specificity, (0,2,0) vs (0,1,0)) | None — 36px floor already set; row height is driven by the 44px `.btn` siblings | WorkspaceActionBar.test.tsx:107 pins the CSS **file** regex `.workspace-status-control{…min-height:var(--size-control-compact)}` — additive-safe; keep the min-height line | global ring only — ok |
| 2 | "More actions" trigger | `.workspace-more-actions > button` workspace.css:154-156 (min-height only) | WorkspaceActionBar.tsx:199-209 | Main composer, bottom action bar | Status chips, CompletionBar | **(a)** `.btn-compact` in TSX | None (36px floor already) | WorkspaceActionBar.test.tsx:110 pins the descendant rule regex — keep the rule as a floor; additive-safe | global ring — ok |
| 3 | Artifact tabs Graph/Spec/YAML/Run | `.artifact-workspace-toolbar button` workspace.css:79-81 (min-height only) | ArtifactWorkspace.tsx:319-337 (`role=tab`, no className) | Main composer, artifact toolbar (always) | "Focus Graph" (also bare), directly under the skinned chat header across the split | **(b)** new `.artifact-tab` (spec in §3) | None — 36px floor already sets toolbar height | ArtifactWorkspace.test.tsx:192 pins `.artifact-workspace-toolbar button{…min-height…}` — **keep that rule** as the floor and add `.artifact-tab` on top; do not delete it | global ring — ok |
| 4 | "Focus Graph" | same bare-button rule, workspace.css:79-81 | ArtifactWorkspace.tsx:339-341 | Artifact toolbar | The artifact tabs | **(a)** `.btn-compact` in TSX (it is a command, not a tab) | None | none beyond #3's kept rule | global ring — ok |
| 5 | "Collapse authoring pane" | `.workspace-collapse-control` workspace.css:173-176 (align/flex only; min-height appears **only** inside the max-height≤800 media block at :423-426) | ComposerWorkspace.tsx:333-341 | Main composer, bottom of authoring pane (always) | Authoring status line above, action bar below | **(a)** `.btn-compact` in TSX + border-top separator per prior finding #2 | **YES (flagged)** — at viewport heights >800px it currently has no min-height, so UA height ≈ 24-26px; `.btn-compact` raises it to 36px and the authoring scroll region loses ~10px. Absorbed by the flex scroller; acceptable, but visible in golden diffs | none — no test pins this class | global ring — ok |
| 6 | "Restore authoring pane" | none (classless) | ComposerWorkspace.tsx:351-359 | Only when authoring collapsed, inside `.workspace-collapsed-affordance` | Collapsed-status text | **(a)** `.btn-compact` in TSX | Low — affordance box (padding 0.5rem, bordered) grows a few px; it floats, nothing reflows | none | global ring — ok |
| 7 | Narrow view tabs "Compose"/"Pipeline" | `.workspace-view-tabs > [role=tab]` gets min-width/height 44 **only** in the narrow-mode rule workspace.css:371-376 | ComposerWorkspace.tsx:280-305 (`role=tab`, classless) | Narrow layout only (workspace width < 960px, ComposerWorkspace.tsx:64) | Each other, above the single pane | **(b)** `.artifact-tab`; keep the existing 44px narrow floor rule (touch target) | None — 44px floor rule already sets row height in the only mode where they render | none | global ring — ok |
| 8 | Inspector "Close" | none (classless) | WorkspaceInspector.tsx:203-210 | Workspace inspector overlay (node config / run outputs / audit) | `h2 "Inspector"` in a padded header row | **(a)** `.btn-compact` | **YES (flagged)** — header row is padding 8px + content; UA button ≈ 24-26px → row ≈ 40px today; 36px control makes it ≈ 52px (which *matches* the artifact toolbar — desirable, but moves golden `inspector-open-1280x720.png`) | none | global ring — ok |
| 9 | Inspector tabs (Config/Outputs/Audit…) | none (classless, `role=tab`) | WorkspaceInspector.tsx:219-241 | Workspace inspector overlay | Each other in `.workspace-inspector-tabs` row | **(b)** `.artifact-tab` | Same as #8 — tabs row grows to 52px; flag for golden regen | none | global ring — ok |
| 10 | Run-history "Retry" | none (classless) | InlineRunResults.tsx:339-344 | Run tab, "Run history unavailable" empty state | Prose; panel toolbar above uses `.btn-compact` | **(a)** `.btn-compact` | None — empty-state layout, nothing pinned | none | global ring — ok |
| 11 | "Download full output" | `.narrative-results-download` has **no rule** in composer.css (checked: zero hits) | NarrativeResults.tsx:354-360 | Run tab, narrative results block | Prose paragraph | **(c)** `.link-button` (it is a download link in prose, not a command button) — or `.btn-compact` if the team prefers buttons for downloads | None as link-button | has `data-testid="narrative-results-download-link"` — tests select by testid/role, class is free | global ring — ok |

### Tier 2 — styled-in-TSX (inline style objects; invisible to any CSS sweep or lint)

| # | Control | Where | Screens | Remedy | Notes |
|---|---------|-------|---------|--------|-------|
| 12 | "Sign in" / "Create an account" link buttons | LoginPage.tsx:225-234 (`linkButtonStyle` object), used at :683-690, :754-761 | Login screen | **(c)** `.link-button` | Skin itself is fine (link idiom incl. underline offset); the defect is the mechanism — inline styles outrank stylesheets and dodge token audits |
| 13 | "download for full file" | RunOutputsPanel.tsx:521-533 (inline style object) | Inspector run-outputs preview, truncated state | **(c)** `.link-button` | Same mechanism defect; sits in prose beside `.btn-compact` Preview/Download buttons (:424-444) which are fine |

### Tier 3 — skinned but incomplete (polish list; NOT the jank mechanism)

These all have padding+border/background+radius+font. Missing pieces noted.
All ride the global focus ring (ok). Remedy **(d) keep, patch the named gap**
unless stated.

| # | Control | CSS | Gap | Beside |
|---|---------|-----|-----|--------|
| 14 | `.chat-panel-error-dismiss` (×) | chat.css:877-889 (+ guided.css:583 position override) | no radius, no hover treatment | error banner text; ChatPanel.tsx:2508/2756/3130 |
| 15 | `.chat-input-send-btn` | chat.css:537-553 | no `:hover` on the enabled state (accent bg is static) | cancel/icon buttons which do have hover |
| 16 | `.structured-preview-toggle` | ui/structured-preview.css:22-38 | no hover (has `aria-pressed` state) | preview header |
| 17 | `.secrets-panel-close` (×) | settings.css:22-36 | no hover | settings/preferences panel headers (SecretsPanel.tsx:223, ComposerPreferencesPanel.tsx:238) |
| 18 | `.graph-config-close` (×) | inspector.css:327-336 | no hover, no font-size (32px, below the 36px family norm) | graph node-config panel header |
| 19 | `.graph-modal-close` / `.yaml-modal-close` | styles/common.css:462-470, :514-522 | no hover, no explicit padding/font (min-w/h only) | modal headers |
| 20 | `.filter-chip-clear` | catalog.css:519-530 | no hover/focus enhancement | filter chips (which have both) |
| 21 | `.alert-banner-action` | header.css:30-41 | no hover | app notice banner message |
| 22 | `.tutorial-link-button` | tutorial.css:191-201 | no radius (focus ring is rectangle-around-text — fine), no hover; fold into **(c)** `.link-button` adoption when convenient | tutorial prose |
| 23 | `.message-retry-btn` | chat.css:1065-1074 | no hover | failed-message row |
| 24 | `.header-session-switcher-item`/`-action`, `.user-menu-action` | header.css:218-233, :341-363 | no radius (full-bleed menu rows — **intentional (d), keep**) | dropdown menus |
| 25 | `.audit-readiness-summary`, `.audit-readiness-row-btn` | audit.css:62-…, :185-… | no radius (full-width row expanders — **intentional (d), keep**) | audit panel rows |

### Tier 4 — intentionally minimal, no action (d)

- **Disclosure `<summary>` elements** (`.tool-call-details summary` chat.css:1486;
  `.inline-source-created-turn-audit summary` chat.css:1221; `.wire-stage__raw >
  summary` guided.css:314; `.pipeline-validation-summary-raw`; ReadinessRowDetail
  raw-text) — native disclosure triangles in card bodies; base.css:93-97 gives
  them a dedicated focus ring + radius. Correct as-is.
- **Link-style buttons already on the shared idiom**: `.validation-banner-summary-btn`,
  `.validation-banner-collapse-btn`, `.validation-banner-component-btn`
  (shared.css:616-729), `.ack-stack-opt-out-link`, `.ack-card-view-toggle`,
  `.composing-details-toggle`, `.wire-stage__blocker-link` — deliberate
  text-affordance register inside prose/banners. Optionally migrate to
  `.link-button` over time; not jank.
- **CommandPalette results** — `<li role="option" class="command-palette-item">`
  (CommandPalette.tsx:380-…; common.css:command-palette-item) — listbox pattern,
  selection painted via `-selected`, keyboard focus stays in the input
  (which has a proper ring, common.css:276-280). Correct as-is.
- **`.bubble-copy-btn`/`.bubble-edit-btn`** (chat.css:132-162) — reveal-on-hover
  icon buttons with focus-visible reveal and `hover:none` fallback. Correct.
- **`.workspace-separator`** (workspace.css:246-280) — invisible 24px resize
  grip with its own inset focus ring. Correct.
- **`.graph-a11y-list button`** (inspector.css:211-242) — SR/keyboard overlay,
  fully skinned. Correct.

### Drive-by defects found during the sweep (not chrome-skin, but log them)

- **`.btn-secondary` is referenced but defined nowhere** — RecoveryPanel.tsx:136,
  :160 and RecoveryDiff.tsx:301 emit `btn btn-secondary`; shared.css defines
  primary/danger/ghost/small/compact only. Harmless today (degrades to base
  `.btn`) but it is a phantom variant; either add it or drop the class token.
- **CatalogButton reuse inside the More-actions menu** — the menu
  (WorkspaceActionBar.tsx:210-228) renders `ImportYamlButton` (`.btn`, 44px) and
  `CatalogButton` (`.side-rail-catalog-btn`, a two-column side-rail grid with its
  own margins, sidebar.css:130-150). Two different design systems inside one
  12rem menu. Not blocking; consider a `.menu-action` treatment when the menu
  grows.

---

## 2. Header height decision

**Measured today (default, viewport height > 800px):**

| Half | Rule | Computation | Height |
|------|------|-------------|--------|
| Chat | `.chat-panel-header` chat.css:638-648: `padding: var(--space-sm) var(--space-lg)` (8px block) + tallest child `.mode-switch-btn` chat.css:731-745 `min-height: var(--size-control)` (44px) | 8+44+8 (+1 border) | **60px (61 outer)** |
| Artifact | `.artifact-workspace-toolbar` workspace.css:64-72: `padding: 0.5rem` + `.artifact-workspace-toolbar button` :79-81 `min-height: var(--size-control-compact)` (36px) | 8+36+8 (+1 border) | **52px (53 outer)** |

**Recommendation: canonicalize on 52px — token pair `--size-control-compact`
(content) + `--space-sm` (padding-block).** Rationale: (i) `.btn-compact` is the
system's designated chrome-row size ("use for buttons inside surfaces shorter
than 44px", shared.css:184-187), and every control this inventory adds to these
rows is `.btn-compact`/36px; (ii) at the 1080p primary target the vertical
budget goes to the graph/chat, not chrome — raising the toolbar to 60px spends
8px × 2 rows for nothing; (iii) 36px still clears WCAG 2.5.8 AA (24px floor;
the comment above `.mode-switch-btn` cites 2.5.8, not the 44px AAA 2.5.5, so no
documented contract is broken).

**Exact changes:**

- **Chat side (the only functional change):** chat.css:740
  `.mode-switch-btn { min-height: var(--size-control); }` →
  `min-height: var(--size-control-compact);`. Everything else in the header
  (model chip, authority chip, title) is already shorter. Header computes
  8+36+8 = 52px. ModeSwitchButton renders only in the two body headers
  (ChatPanel + guided), so the change is scoped exactly to header rows.
- **Artifact side:** no height change. Housekeeping only: workspace.css:70
  `padding: 0.5rem` → `padding: var(--space-sm)` so both halves name the same
  token (0.5rem == 8px == --space-sm; zero visual delta).
- **Optional belt-and-braces:** give both `.chat-panel-header` and
  `.artifact-workspace-toolbar` an explicit
  `min-height: calc(var(--size-control-compact) + 2 * var(--space-sm));` so the
  row height survives a future child shrinking. Not required for the fix.

**Compaction media queries — verdict: keep both, they converge after the change.**

- chat.css:475 `@media (min-width: 761px) and (max-height: 800px)` drops
  `.chat-panel-header` padding-block to `--space-xs` → 4+36+4 = **44px**.
- workspace.css:416 `@media (min-width: 961px) and (max-height: 800px)` drops
  toolbar padding to `0.25rem 0.5rem` → 4+36+4 = **44px**.

Today these compact to 52px vs 44px (still mismatched!); after the
mode-switch change both halves compact identically to 44px. The differing
min-width guards (761 vs 961) guard different panes and are fine.
**Breakpoint move: not needed for 1080p.** A maximized 1080p window has
~930-1010px innerHeight, comfortably above 800, so compaction correctly never
fires at the primary target; it exists for 768p laptops and is doing its job.
Only note: two of the four visual-spec golden viewports (1536×760, 1280×720)
are *inside* the compaction regime, so goldens exercise both modes — good.

---

## 3. New shared classes

### `.artifact-tab` — role=tab chrome (remedy (b); shared.css, next to `.tab-strip`)

Not a toggle button: selected state must read as "current view", not "pressed".
The existing `.tab-strip-tab` (underline idiom, shared.css:330-357) was
considered and rejected for this spot: the artifact toolbar is a bordered
toolbar row also carrying "Focus Graph", and an underline tab wants to sit
flush on the container's border-bottom, which this row's padding prevents.
Segmented-control idiom fits the toolbar. Tokens only:

```
.artifact-tab                    display:inline-flex; align-items:center; justify-content:center;
                                 gap: var(--space-xs);
                                 min-height: var(--size-control-compact);
                                 padding: var(--space-2xs) var(--space-sm);
                                 border: 1px solid transparent;      /* reserve the box — no width jump on select */
                                 border-radius: var(--radius-md);
                                 background: transparent;
                                 color: var(--color-text-secondary);
                                 font-family: var(--font-sans);
                                 font-size: var(--font-size-sm);
                                 font-weight: var(--font-weight-medium);
                                 cursor: pointer;
                                 transition: background-color var(--transition-fast), border-color var(--transition-fast), color var(--transition-fast);
.artifact-tab:hover:where(:not(:disabled)):where(:not([aria-selected="true"]))
                                 background: var(--color-surface-hover); color: var(--color-text);
.artifact-tab[aria-selected="true"]
                                 background: var(--color-surface-elevated);
                                 border-color: var(--color-border);
                                 color: var(--color-text);
.artifact-tab:disabled           color: var(--color-text-muted); cursor: not-allowed;
/* focus: global base.css ring — declare nothing, stay consistent */
```

Cascade note: keep the hover guards inside `:where()` (project convention,
buttonCascade.test.ts rationale) so `aria-selected` always wins over hover.
Adopters: artifact tabs (#3), inspector tabs (#9), narrow view tabs (#7 — keep
the existing 44px narrow floor rule at workspace.css:371-376, it simply
overrides the min-height in narrow mode for touch).

### `.link-button` — button-that-reads-as-link (remedy (c); shared.css)

The pattern currently exists five+ times as three mechanisms (class, class,
inline-TSX). One definition, tokens only:

```
.link-button   border: 0; background: transparent; padding: 0;
               color: var(--color-link); font: inherit; cursor: pointer;
               text-decoration: underline; text-underline-offset: 3px;
               border-radius: var(--radius-sm);   /* focus-ring shape only */
.link-button:hover   text-decoration-thickness: 2px;
```

(Verified: `--color-link` exists in both themes, tokens.css:138/:401;
`--color-link-hover` does NOT exist — so the hover cue is underline thickness,
not a color shift. Do not invent a new color token for this.)
Adopters now: LoginPage (#12, delete `linkButtonStyle`), RunOutputsPanel (#13,
delete the inline object), NarrativeResults (#11). Adopters later (no rush):
`.tutorial-link-button`, `.ack-stack-opt-out-link`, `.wire-stage__blocker-link`.

### Not proposed

A `.btn-icon`/close-button consolidation (Tier-3 family #17-19) was considered
and deferred: the family is already visually consistent (36px, bordered,
radius-sm) and consolidation is churn without a user-visible win. Patch the
missing hovers in place.

---

## 4. Ordered landing plan

One PR, four commits, **one golden-screenshot regeneration at the end** (the
visual spec will diff on every commit in this plan; regenerating per-commit
wastes cycles and hides which change moved what — regenerate once, eyeball the
diff against this inventory).

1. **Commit 1 — workspace chrome skin (the jank fix; pure additive).**
   TSX: add `btn-compact` to #1, #2, #4, #5, #6, #8, #10. CSS: add
   `.artifact-tab` to shared.css; TSX: adopt on #3, #7, #9.
   KEEP `.workspace-status-control`, `.workspace-more-actions > button`,
   `.artifact-workspace-toolbar button` rules untouched (test-pinned floors;
   also the data-tone border-color overrides live on #1's class).
   Check: the workspace.css:423-426 compaction override for
   `.workspace-collapse-control` becomes redundant once the control is
   `.btn-compact` (36px everywhere) — delete the redundant block *in this
   commit* so the compaction story stays readable.
   Tests: WorkspaceActionBar.test.tsx / ArtifactWorkspace.test.tsx regexes stay
   green (file-content regexes, all additive). CompletionBar.test.tsx untouched
   (buttons never shrink; chips rise to 36px skinned — they already had the
   36px floor).
2. **Commit 2 — header height 60→52.** chat.css:740 min-height token swap +
   workspace.css:70 `0.5rem`→`var(--space-sm)` housekeeping (+ optional
   explicit min-heights, §2). No test pins `.mode-switch-btn` height
   (verified: only ModeSwitchButton.test.tsx:64, a confirm-card DOM assert;
   ChatInput.test.tsx:554-558 pins are inside the ≤760px phone block, untouched).
3. **Commit 3 — `.link-button` + stray controls.** Add class to shared.css;
   adopt at #11, #12, #13; delete `linkButtonStyle` and the RunOutputsPanel
   inline object. Also the `.btn-secondary` decision (add the variant or edit
   3 call sites to plain `.btn`) — tiny, but it touches shared.css so it
   batches here.
4. **Commit 4 — Tier-3 hover/focus polish.** Add `:hover` treatments for
   #14-#21, #23 (one-liners, `--color-surface-hover` / border-strong idiom).
   Zero layout movement; safe to batch.
5. **Golden regen + acceptance.** Regenerate composer-workspace.visual.spec.ts
   goldens (4 viewports; 1536×760 and 1280×720 exercise the compaction regime).
   Acceptance per the prior review: screenshot diff at 1920×1080 AND 1920×930,
   both themes — the unit tests cannot see any of this (they assert substrings,
   which is how the jank shipped green).

Do NOT batch into this PR: authoring-pane width 360→420+ (prior finding #4) —
it collides with composer-workspace-geometry.spec.ts:205-206 (`toBe(360)` /
`toBe(620)` exact-pixel pins) and with WorkspaceSeparator resize bounds; that
is its own change with its own test updates.

### Test-safety summary (per pinned test, verified by reading them)

| Test | Pins | Verdict |
|------|------|---------|
| WorkspaceActionBar.test.tsx:107,110 | CSS-source regex: min-height lines in `.workspace-status-control` and `.workspace-more-actions > button` | additive-safe — keep both rules and their min-height lines |
| ArtifactWorkspace.test.tsx:172-192 | `.artifact-workspace*` flex/overflow structure + bare-button min-height rule | additive-safe — keep the bare-button rule as floor under `.artifact-tab` |
| CompletionBar.test.tsx:109-124 | `.workspace-action-bar .completion-bar` row overrides; three co-equal buttons | OUT OF SCOPE — chips rise, buttons never shrink; untouched |
| buttonCascade.test.ts | specificity of `.btn`/`.btn-compact` hover vs variant hovers | untouched — `.artifact-tab` uses the same `:where()` guard convention and doesn't start with `.btn` |
| colorContrast.test.ts:306-323 | `--size-control: 44px`, `--size-control-compact` ≥ AA, `.btn`/`.btn-compact` compose the tokens | untouched — no token values change |
| AppHeader.test.tsx:23-26 | app-header 36px+1 height + trigger heights | untouched — app header not in scope |
| ChatInput.test.tsx:549-560 | ≤760px phone compaction block contents | untouched |
| ComposerWorkspace.test.tsx:159-160 | narrow-mode 420px workspace floor | untouched |
| tests/e2e/composer-workspace-geometry.spec.ts | :205-206 pane widths 360/620 @980×720 (NOT touched by this plan); :300-306 app-header control bounds ≥36 (unaffected); :343 workspace height stable ±1 across menu open (unaffected — same-skin before/after) | no collisions from THIS plan; the pane-width finding collides and stays separate |
| tests/e2e/composer-workspace.visual.spec.ts | golden screenshots, 4 viewports | WILL diff on every commit — regenerate once at the end, review against this inventory |

---

## 5. SME protocol

**Confidence: HIGH** on the inventory's coverage and every file:line cited
(each was read, not inferred; the scripted sweep enumerated all 217 interactive
sites and every non-`.btn` site was manually resolved — including the ~60 the
regex could not parse). HIGH on the test-safety table (every pinned test read).
MODERATE on two judgment calls: (i) the `.artifact-tab` segmented-control idiom
vs. reusing `.tab-strip-tab` — a defensible aesthetic choice, reviewable at
implementation; (ii) the claim that no component test selects these controls by
class (spot-checked the workspace tests, which use roles/testids; a stray
`querySelector('.workspace-status-control')` elsewhere would surface instantly
as a green-stays-green anyway since classes are added, not removed).

**Risk:** LOW for commits 1, 3, 4 (additive CSS/class changes behind
substring-pinned tests). MODERATE for commit 2 (header height): the 44→36px
mode-switch button is a real hit-target reduction — still AA (2.5.8, 24px
floor) but below the AAA 44px the `--size-control` comment block champions for
canvas-area actions; if the team treats AAA as binding for this control, the
alternative is canonicalizing both headers at 60px (artifact side: buttons →
`--size-control`, toolbar keeps 8px padding), at the cost of 8px of vertical
budget and diverging from the `.btn-compact` chrome-row doctrine.

**Information gaps:** (1) No browser in this session — all heights are computed
from the cascade, not measured; the 60/52 numbers match the prior live-capture
review's, corroborating the method, but the commit-2 result should be verified
against the 1920×1080 screenshot in acceptance. (2) Dark theme unverified
(prior review's caveat stands; `.artifact-tab` uses only theme-paired tokens so
risk is contained). (3) The `styled-in-TSX` sweep
covered `style={{...}}` on interactive elements found by the tag scan; a
component composing styles through a helper could evade it — none were seen in
the directories reviewed.

**Caveats:** The guided stepper numerals (guided.css:190-192, prior finding #6)
and the chat textarea/pane-width items (#4, #5) are real but are NOT chrome
conformance and are excluded here per the brief's scope. CompletionBar's three
co-equal buttons are product intent (commented in the test) and were treated as
immovable. The `.btn-secondary` phantom variant and the CatalogButton-in-menu
observation are logged as drive-bys, not scoped into the landing plan's
acceptance.
