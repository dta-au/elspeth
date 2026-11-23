# ARGOnite design review (Elspeth composer workspace)

Path placeholders: `<argo>` is the ARGO repository root, `<elspeth>` this repository root, `<elspeth-frontend>` is `src/elspeth/web/frontend/`, and `<review-scratch>` the reviewer's session-local scratch directory (not retained).

Scope: the mockup at `<review-scratch>/argonite-canvas/project/Main.dc.html`, both `argonite-dark` and `argonite-light`, reviewed against ARGO's real tokens (`<argo>/cto_ai_automation.client/src/index.css`, `CSS/ViewToggle.module.css`, `Elspeth/CSS/ElspethWorkflowEditor.module.css`), Elspeth's `tokens.css`, and the two Playwright snapshots of today's UI.

How this was done: the canvas runtime is not available, so nothing was rendered. Every contrast number below is computed (WCAG 2.x relative luminance, alpha pre-composited over the stated ground) from the inline styles and the `themes` object. Layout and density points are inferred from the pixel values in the markup and the 1280x720 / 1920x1080 snapshots; they are not measured on a live page. Floors used: 4.5:1 text, 3:1 large text (>=18.66px bold or >=24px), 3:1 control/focus boundaries and meaningful graphics.

Constraint check (all pass in the mockup as drawn): checksum copy / match / paste survive as icon buttons at the same click depth; "What this checksum covers" survives as an (i) button; hide-routes survives as a switch; Wiring, Plugin catalog, Share inspect link, Run and the collapse control are all present; collapse stays in the bottom action bar; the OFFICIAL banner is byte-identical to today's; node-type badges keep the four Elspeth hues.

Ranking: **M** = must-fix before the demo, **S** = should-fix (do if the 2-3 days allow), **L** = later.

---

## 1. Computable accessibility defects

### 1.1 argonite-light

| # | Pair | Ratio | Floor | Verdict |
|---|------|-------|-------|---------|
| L1 | Header controls: white text on `nav-ctl` (16% white over `#288264`) -- "Complaints triage", "v7 · Draft" | **3.53** | 4.5 | **FAIL (M)** -- the mailbox and account icons on the same fill are non-text and pass 3:1 at 3.53; "JM" on `primary` is 5.64; the mailbox count `#1e3c50` on `#fad673` is 8.22 |
| L2 | Model chip: `nav-muted #c8ebd7` on `nav-ctl` over `#288264` | **2.74** | 4.5 | **FAIL (M)** |
| L3 | Graph edges `#7a9487` on well `#dcefe4` | **2.73** | 3.0 | **FAIL (M)** |
| L4 | Icon-button borders (`--line #cde3d6`) on white | **1.35** | 3.0 | **FAIL (M)** -- the border is the only boundary of the checksum, upload and more buttons |
| L5 | `btn-border #b4cfc0` on white (collapse, Share inspect link) | **1.66** | 3.0 | FAIL (S) -- today-light's `rgba(15,45,53,.25)` is 1.65, so this is inherited, not a regression |
| L6 | Switch thumb: white on `#29b480` | **2.65** | 3.0 | **FAIL (M)** -- and the thumb is the state indicator |
| L7 | Selected-node ring `rgba(41,180,128,.4)` vs white node / vs well | **1.46 / 1.37** | 3.0 | **FAIL (M)** -- today-light uses solid `#156048` (7.50 / 6.26) |
| L8 | Node borders `#cde3d6` on well `#dcefe4` | **1.13** | 3.0 | FAIL (S) -- the node's shadow carries the edge; shadow is not a boundary under forced colours or on a washed-out projector |
| L9 | Wordmark `#fad673` on `#288264` (20px/800 = large text) | 3.34 | 3.0 large | passes narrowly; see 4.1 -- ARGO never puts yellow on green |
| L10 | Primary pill `#29b480` vs page `#eaf5ef` (Run boundary) / vs field `#e5f3eb` (Send) | 2.37 / 2.31 | 3.0 | FAIL (S) -- the pill is a filled control, text inside passes, so this is 1.4.11-marginal; a 1px `#1b7655` border fixes it |
| L11 | `muted #5b6267` on surface / pane-head / well / track / field / page | 6.20 / 5.61 / 5.17 / **4.59** / 5.42 / 5.55 | 4.5 | passes everywhere, thinnest on the tab track |
| L12 | Badge text on badge bg: SRC 4.28, TR 4.40, GATE 4.39, SINK 4.36 | ~4.3-4.4 | 4.5 (11px) | **marginal (S)** -- clears Elspeth's 3:1 badge gate, misses AA small-text by ~0.1 |
| L13 | Heading `#1e3c50` on pane-head / surface | 10.46 / 11.56 | 4.5 | pass |
| L14 | `on-primary #13293a` on `#29b480` (Run/Send) | 5.64 | 4.5 | pass |
| L15 | Active tab `#13293a` on white pill | 14.94 | 4.5 | pass; pill vs track boundary is 1.35 (shadow-carried, see 3.12) |
| L16 | Checks tick: white on `#1b7a3d` | 5.39 | 4.5 | pass |
| L17 | OK banner border `#a9dbb9` vs white / vs its own bg | 1.55 / 1.32 | -- | decorative; fine as long as the icon and text carry the state |

**Fixes (light):**

- L1/L2 (M): the 16% white fill lightens the green and kills the contrast. Either (a) drop the fill and keep the controls as text + chevron on the green (white 4.70, `#c8ebd7` 3.65 -> change the model chip to white at 13px/500 or move it out of the header), or (b) use a dark tint `rgba(0,0,0,0.20)` behind the controls: white text 6.65, `#c8ebd7` 5.17. (b) also reads as "pressed into the band", which is the ARGO feel. Or (c) put the chips on a `#1e3c50` navy pill: white 11.56, `#c8ebd7` 8.98 -- this is literally what ARGO does with its band (see 4.1).
- L3 (M): `edge: #5f7a6e` (3.89 on the well). Keep arrowheads at the same colour. `#61786c` gives 3.97 if you want a touch warmer.
- L4 (M): give icon buttons `border: 1px solid var(--line-strong)` (`#7a9487`, 3.27 on white). That is ARGO's own default `button` rule (`index.css` ~line 577), so this is also a fidelity fix.
- L5 (S): same token swap for `btn-border` -> `#7a9487` (3.27). Today fails too, so it is not a demo blocker, but it is a one-token change.
- L6 (M): thumb `#13293a` on the on-track (5.64), or keep white and add `border: 1px solid #13293a`. Off state: track `#7a9487` (3.27 on white) with a white thumb (3.27 on the track).
- L7 (M): drop the translucent ring; use `box-shadow: 0 0 0 2px #156048, <node shadow>` (7.50 vs node, 6.26 vs well). Dark: solid `#29b480` (4.86 vs node) instead of 45% alpha (2.12).
- L8 (S): `node-border: #6b8a7c` (3.15 on the well). If you want the lighter look, accept it but add a forced-colours border rule (3.12).
- L12 (S): darken each badge text slightly: SRC `#1f6f5a` (4.92 on `#dcebe9`), TR `#875c12` (5.00 on `#f3ecdc`), GATE `#7a4fab` (4.93 on `#ece8f5`), SINK `#a04a29` (4.93 on `#f5e6e0`). Computed with `<review-scratch>/contrast.py`.
- L11: no change required. `#4f565b` would give 6.75 / 5.53 / 6.22 (pane-head / track / well) if you want headroom, but it is optional.

### 1.2 argonite-dark

| # | Pair | Ratio | Floor | Verdict |
|---|------|-------|-------|---------|
| D1 | Badge text over 16% tints on node `#1c3447`: SRC **4.00**, TR 4.45, GATE **4.06**, SINK **3.35** | -- | 4.5 (11px) | **FAIL (M)** -- today-dark's opaque bgs give 4.54 / 5.10 / 4.65 / 3.82. The mockup replaced Elspeth's pre-composited backgrounds with alpha tints and regressed all four. (SINK is below 4.5 today as well; tokens.css gates badges at 3:1.) |
| D2 | Icon-button borders `--line #244a42` on surface `#182d3f` | **1.44** | 3.0 | **FAIL (M)** |
| D3 | `btn-border #2f5a50` on surface | **1.82** | 3.0 | FAIL (S) -- today-dark's `rgba(143,200,200,.25)` on `#1a3d47` is 1.68, inherited |
| D4 | Node border `#2c5a50` on well `#0c2420` | **2.08** | 3.0 | FAIL (S) -- shadow-carried |
| D5 | Selected-node ring 45% alpha vs node | **2.12** | 3.0 | **FAIL (M)** |
| D6 | Tab track `#244a42` vs pane-head `#11282e` | 1.56 | -- | the recess is shadow-carried; acceptable, but see 3.12 |
| D7 | Edges `#5f8a7d` on well `#0c2420` | 4.20 | 3.0 | pass |
| D8 | `muted #a9b9c4` on surface / pane-head / well / node / track / field / page | 7.01 / 7.63 / 8.09 / 6.39 / 4.89 / 5.78 / 8.53 | 4.5 | pass |
| D9 | Wordmark `#fad673` on `#0f4636` | 7.66 | 3.0 | pass |
| D10 | Header text on `nav-ctl` (10% white over `#0f4636`): `#e8eef2` 6.91, `#a8e3c4` 5.56 | -- | 4.5 | pass |
| D11 | `on-primary #13293a` on `#29b480` | 5.64 | 4.5 | pass |
| D12 | Checks tick `#13293a` on `#63d58f` | 8.14 | 4.5 | pass |
| D13 | Active tab `#141d1b` on `#e8eef2`; pill vs track | 14.69; 8.41 | 4.5; 3.0 | pass |
| D14 | Danger (ARGO `#db5853`) on surface / on `danger-soft` over surface | 3.72 / **3.06** | 4.5 | **FAIL if used for text (M when the Checks-failed state is built)** -- Elspeth's own `#f08e8b` exists for exactly this reason |
| D15 | Text on `--field #223b4e` | 9.95 | 4.5 | pass (ARGO's real dark field is `#374c5d`, see 4.4) |

**Fixes (dark):**

- D1 (M): keep Elspeth's opaque badge backgrounds (`#184244`, `#303e34`, `#2a3c52`, `#2e3737`) under the pill radius, or pre-composite the 16% tints over `#1c3447` and then lift the text. The cheapest correct option is the former: it is one token per badge and is already gated by `colorContrast.test.ts`. Also lift SINK text to `#ea8a62` (4.84 on `#2e3737`; `#e8825a` is 4.52, too thin).
- D2/D3 (M/S): `border: 1px solid var(--line-strong)` (`#5f8a7d`): 3.65 on surface, 3.32 on raised.
- D4 (S): `node-border: #5f8a7d` (4.20 on the well).
- D5 (M): solid `#29b480` 2px ring (4.86 vs node).
- D14: when building Checks-failed / validation banners, use Elspeth's `--color-error #f08e8b` (not ARGO's `--danger`) for dark text-on-tint. Light can use ARGO's `#c23431` (5.48 on white, 4.78 on `#fbeceb`).

### 1.3 Focus rings (both modes)

The mockup defines no focus style at all. ARGO's `:focus-visible { outline: 2px solid var(--action) }` resolves to roughly `#1b7655` (light) / `#8edac0` (dark). Against the primary `#29b480` pill those are **2.11 / 1.63** -- a focused Run or Send button has no visible ring. Elspeth's own rings (`#0f2d35` light, `#ffffff` dark) are 5.48 / 2.65 against the pill directly, but with `outline-offset: 2px` the ring sits on the surrounding surface (14.51 / 14.13), which is where WCAG assesses it. **Fix (M):** keep Elspeth's focus-ring tokens, always with `outline-offset: 2px`, and on pills use a double ring (`box-shadow: 0 0 0 2px var(--surface), 0 0 0 4px var(--color-focus-ring)`) so it survives on any ground.

---

## 2. Does the tactile treatment help or hurt at this density?

**2.1 Recessed six-tab track (S).** Today's tabs are flat text with a soft active fill; the track makes the active tab unmistakable (pill vs track 8.41 dark) and is the single most "ARGO" element, so keep it. Two risks:

- Width. Six pills at 14-16px side padding plus the Checks badge, an 18px "Pipeline" title, 18px gaps and a ~150px "Plugin catalog" pill total roughly 780-800px. At 1280x720 the artifact pane is ~850px wide (1280 - 400 conversation - 28 gaps). That is ~50px slack; any longer tab label, a 2-digit Checks count, or a slightly narrower window wraps the header. Fix: let the title collapse to an icon below ~1366px, or drop "Pipeline" from the title bar (the pane is unambiguous) and keep only the track and the catalog button.
- The Checks badge. The real checks-open snapshot shows "Checks 25", not a tick. An 18px circle cannot hold two digits at 11px; design it as a 20px-high pill with `min-width: 20px; padding: 0 6px` and a fail colour (see 3.3).

Inactive tab text at `muted` on the track is the thinnest text pair in the whole design (4.59 light, 4.89 dark). Acceptable, but do not go lighter.

**2.2 Icon-only checksum buttons (S, borderline M for a demo).** Today's labelled "Copy checksum / Match clipboard / Paste checksum" are self-explanatory, which matters because the demo audience is a parent agency seeing checksum verification for the first time. Icon-only drops them to `title` tooltips: no label on touch, nothing until hover, and "Match clipboard" and "Paste checksum" are both clipboard glyphs that differ by a tick vs an arrow at 16px. Recommended compromise that keeps the pill look and saves nearly the same vertical space: a single segmented pill with three labelled segments ("Copy", "Match clipboard", "Paste") at 13px/600, or keep "Copy" labelled and icon-only for the two rarer actions. Either way, the build must render real tooltips (not just `title`) that are keyboard-reachable, because `title` is not announced reliably and never appears on focus in Chrome.

The (i) replacing the inline "What this checksum covers" disclosure is the same click depth, but turns inline text into a popover -- a new state that needs a designed surface (3.9).

**2.3 Switch instead of the "Hide quarantine and discard routes" button (S).** A switch is the right control for a persistent, binary, immediately-applied view preference, which this is, so the semantics are fine. The problem is polarity. Today's control is phrased as the action "Hide ..."; the mockup's label is the noun "Quarantine & discard routes" with `aria-checked="true"`, which must mean "shown". That inversion is exactly how a build wires it backwards. Fix: label it with the state verb, "Show quarantine & discard routes", keep `aria-checked` = shown, and make sure the off state is drawn (3.6). Also: ARGO already has a switch in this exact pane (`ElspethWorkflowEditor.module.css` `.switch*`, used for Auto-apply), so reuse its markup pattern (real `<input type=checkbox role=switch>` with `:focus-visible` on the slider) rather than the mockup's `<button role=switch>`.

**2.4 Shadows and depth at this density (S).** Two things work: the 14px page gutter with floating panes gives the workspace a clear two-column read, and the inset well separates the graph from the chrome better than today's flat teal. Two things do not: in dark mode the black shadows over `#141d1b` are nearly invisible, so separation there comes from the surface step and the gutter, not the shadow -- do not let the build rely on shadow for pane edges, node edges or the active tab (see D4, D6). And five stacked shadow idioms (card, pill, node, lift, well) on a dense screen tend to read as noise, especially at 1280x720. Keep `sh-card`, `well-sh`, and `sh-pill` on the active tab and primary buttons; drop `lift` from the three checksum icon buttons and the collapse control and use the `line-strong` border instead (which you need anyway for L4/D2).

**2.5 Information hierarchy across the two panes (S).** The new pane title bars ("Conversation · 12 turns", "Pipeline") add ~108px of chrome (50 + 58). The icon checksum row saves ~70px versus today's three-row checksum block, so vertically the trade is roughly a wash, not a gain. The bigger hierarchy issue is the artifact pane: title bar (58) + checksum row (54) + well margin (12) before the graph starts, versus today's 52 + ~110. Fine at 1080p; at 720p the graph gets ~430px, which is enough for the demo pipeline but will crop a five-node graph with labels. Recommendation: make the checksum row collapsible into the title bar at narrow heights, or let the shield icon + hash pill alone sit in the title bar on the right with the three actions in its overflow -- still one click, still visible.

Readability of the action bar: "Checks passed · 5 nodes · ready to run" is the only run-gate status text and it is `muted` 13px. Today's UI shows the gate text ("Fix the validation errors shown in the Checks tab before running") right next to Run. Keep that behaviour and render it in `text`, not `muted`, when it is a blocker.

**2.6 Typography (L).** Bricolage Grotesque at 18px/700 for two pane titles and the wordmark is fine. Instrument Sans at 15px body is slightly larger than ARGO's 14px and is better for a dense UI. Load the fonts self-hosted (ARGO does, `index.css` `@font-face` -> `./assets/fonts/*.woff2`); the mockup pulls from Google Fonts, which an OFFICIAL-marked deployment likely cannot reach and which would silently fall back to system-ui on the demo machine.

---

## 3. States the build must design (none are in the mockup)

| # | State | Why it will look wrong by default | Design to specify | Rank |
|---|-------|-----------------------------------|-------------------|------|
| 3.1 | **Disabled Run** (real: checks-open snapshot shows Run gated) | ARGO's `button:disabled { opacity: .6 }` on a saturated green pill still reads as pressable; computed light text-on-pill at 60% over the page is 3.21, so it is both "clickable-looking" and under 4.5 | `background: var(--inset); color: var(--muted); box-shadow: none; border: 1px solid var(--line-strong)`; muted on inset = 5.17 light / 8.09 dark. Keep the gate message beside it in `text` colour | M |
| 3.2 | **Checks tab: failed / warning / running** with counts | Only the tick is drawn; the 18px circle cannot hold "25" | 20px pill, `min-width 20px`, danger fill with white text (light `#c23431` 5.48; dark use Elspeth `#f08e8b` on a dark tint), warning amber, and a running spinner variant that respects reduced-motion | M |
| 3.3 | **Validation-failed banner / Checks list** | ARGO `--danger` on dark surface is 3.72 (D14) | Use Elspeth's error tokens; keep the banner's 1px border + icon so it survives forced colours | M |
| 3.4 | **"Awaiting your decision" card** (real, in the 1280 snapshot) | Mockup has no warning surface; ARGO has `.decisions` with `border: 1px solid var(--warning)` | Light `#b34800` on `#fbeadd` 4.66; dark `#f0a064` on surface 6.67 -- both pass, reuse them | S |
| 3.5 | **Focus-visible on every pill** | Not defined (1.3) | Elspeth ring tokens + `outline-offset: 2px`, double ring on filled pills | M |
| 3.6 | **Switch off** | Only "on" drawn; white thumb on green already fails (L6) | Off: track `line-strong`, thumb white with 1px `line-strong` border; on: thumb navy on green | M |
| 3.7 | **Hover** | ARGO: `button:hover { background: var(--inset) }`; icon buttons here are white with `lift` | Specify hover = `inset` fill, no shadow change; tab hover = `heading` colour on transparent (ViewToggle pattern) | S |
| 3.8 | **Collapsed conversation pane** | Control exists in the bar, state undrawn | ARGO already has `.paneCollapsed` (2.75rem rail, vertical label); copy it, keep the collapse control in the bar per the layout tests | S |
| 3.9 | **"What this checksum covers" popover** | Replaced inline disclosure with (i) | A raised card (`sh-raised`), dismiss on Esc/outside, `aria-expanded` on the (i), focus returned to the button | S |
| 3.10 | **Wiring expanded** | Only the closed 44px bar is drawn | Expanded list height cap (~35% of pane) with its own scroll; chevron rotates; respect reduced motion | S |
| 3.11 | **Empty / pending** | No "composer is thinking" or empty-graph state | Streaming bubble (ARGO `.working` fade, reduced-motion safe); empty well with a centred muted hint, not a blank inset | S |
| 3.12 | **Forced colours (Windows High Contrast) and `prefers-contrast: more`** | Every shadow-carried affordance disappears: active tab pill, recessed track, switch state, node elevation, icon-button boundaries, the inset well. Elspeth already has a `themes.css` with `forced-colors` blocks (`<elspeth-frontend>/src/styles/themes.css`) | Add rules: active tab `border: 2px solid Highlight` or `background: Highlight; color: HighlightText`; switch thumb/track `border: 1px solid ButtonText`; nodes and icon buttons `border: 1px solid CanvasText`; well `outline: 1px solid CanvasText`; badges keep a border so type is distinguishable without colour | M (the agency may well be on Windows HC) |
| 3.13 | **Reduced motion** | Pill slide and switch transition | `@media (prefers-reduced-motion: reduce)` -> no transform transitions (ARGO already does this for `.working`) | L |
| 3.14 | **Pressed / active and keyboard-activated tab** | Not drawn | 1px darker fill, no transform; the track must not re-layout on selection (pill width changes with label width -- use a measured slider like ViewToggle or accept the snap) | L |

---

## 4. Where the mockup departs from ARGO

**4.1 Yellow wordmark on the green header (S).** In ARGO's `dta` palette `--dta-yellow` is `--on-band-muted`, `--band-accent` and `--brand-mark-foreground`; every one of those sits on `--band` = `--dta-navy #1e3c50` (8.22:1). The green `--header #288264` carries `--on-header` white and `--on-header-muted #c8ebd7`. A grep of `Layout/`, `CSS/`, `Components/` and `Elspeth/` for `band-accent`, `dta-yellow`, `on-band-muted` and `brand-mark` finds them only in `PageBand.module.css`, `Tutorial.module.css`, `RunnerProgress.module.css` (all on the navy band) and the `SidebarLayout.tsx` brand mark (yellow stroke on `--brand-mark-background` = navy); no rule combines them with `var(--header)`. ARGO does not paint yellow on the green. The mockup's `brand: #fad673` on `nav: #288264` (3.34) is therefore both the thinnest pairing in the header and not ARGO's look. Fix: white wordmark on the green (4.70) or, if the yellow matters for recognition, a navy chip behind the wordmark exactly as ARGO's brand mark does (favicon in `App.tsx` lines 73-77: yellow on navy). Dark mode is fine either way (7.66).

**4.2 Button borders (M, already counted above).** ARGO's base `button` is `border: 1px solid var(--line-strong); background: var(--surface); color: var(--heading)`. The mockup uses `--line` and a new `--btn-border` that are 1.4-1.8:1. Restoring `line-strong` fixes L4/L5/D2/D3 and makes it more faithful.

**4.3 Light muted (minor).** ARGO `dta --muted` is `#61686d`; the mockup uses `#5b6267`. The mockup is slightly darker and passes more comfortably (5.66 vs 6.20 on white), so keep the mockup value; just note it is not ARGO's.

**4.4 Dark field colour (minor).** ARGO dark `--field` is `color-mix(in srgb, var(--raised) 88%, var(--white))` = roughly `#374c5d`; the mockup uses `#223b4e`. ARGO's is lighter and muted-on-field is only 4.43 there, so the mockup's darker field is the better choice (5.78). Keep it, and do not "fix" it toward ARGO.

**4.5 Pane header height (minor).** ARGO `.paneHeader` is `min-height: 46px`; the mockup uses 50px (conversation) and 58px (pipeline). 46-50px is enough; reclaiming 8-12px helps 720p.

**4.6 Switch anatomy (S).** ARGO's switch (`.switchSlider`, 3rem x 22px, off = `--muted`, on = `--hue`, thumb = `--inset`) is not what the mockup draws (40x22, white thumb, inset `track` shadow). Reuse ARGO's switch styles -- it already has `:focus-visible` and `:disabled` states.

**4.7 Things that are faithful (no action):** `surface`, `raised`, `inset`, `line`, `line-strong`, `ink/text`, `heading`, dark `muted`, `nav`, `nav-muted`, `primary = --hue`, `ok`, `pill` / `on-pill` in both modes, all five shadow strings, the `track` inset, and `pane-head` (which is exactly `color-mix(inset 55%, surface)` in both modes) match `index.css`. Font families and weights match the `@font-face` declarations.

---

## 5. Prioritised to-do for the 2-3 day build

Must-fix, token-level (about half a day together):
1. L1/L2 header control fills in light (dark tint or navy chips).
2. L4/D2 icon-button borders -> `line-strong`.
3. L3 light edge colour -> `#5f7a6e`.
4. D1 restore opaque badge backgrounds in dark.
5. L6/3.6 switch thumb and off state; label as "Show ...".
6. L7/D5 solid selected-node ring.

Must-fix, component state work (another half to full day):
7. 1.3/3.5 focus rings with offset on every pill.
8. 3.1 disabled Run; 3.2/3.3 Checks failed badge and banner using Elspeth error tokens.
9. 3.12 forced-colours rules for tab, switch, nodes, icon buttons, well.

Should-fix (another half to full day):
- 2.2 labelled checksum segments or at least a labelled Copy + real tooltips.
- 2.1 title-bar overflow at 1280 and the 2-digit Checks badge.
- 4.1 wordmark colour.
- L8/D4 node borders; L10 primary pill boundary; L12 light badge text.
- 3.4, 3.7, 3.8, 3.9, 3.10, 3.11 states.
- Self-host the fonts.

Later: 3.13, 3.14, 2.6 sizing, 4.5 header height.

## 6. Validation gaps

- Nothing was rendered; the overflow estimate in 2.1 and the vertical budget in 2.5 should be checked on a real 1280x720 build.
- Badge text fixes in L12/D1 were computed for the four hues the mockup shows; Elspeth has nine badge types (aggregation, coalesce, row-union, queue, collector) that the mockup does not draw and whose tints need the same treatment.
- Elspeth's `colorContrast.test.ts` and `statusBadgeAccessibility.test.ts` parse `tokens.css` top-level selectors; whichever way the ARGOnite tokens are introduced, those tests should run against them before the demo, which would catch D1 and D14 automatically.
- Contrast script used for every number above: `<review-scratch>/contrast.py` (run with `python3 -I`).
