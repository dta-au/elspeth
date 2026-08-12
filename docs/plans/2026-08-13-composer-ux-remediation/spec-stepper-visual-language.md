# Guided-stepper visual language spec — compact-width sequence states

Filigree step: `elspeth-a208542679`
Scope: the 5-step guided workflow strip (Source → Output → Transforms → Wire → Ready) rendered by
`GuidedWorkflowStepper` (`src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx:3084-3118`),
styled in `src/elspeth/web/frontend/src/components/chat/guided/guided.css` (base block lines 24-92,
compact overrides lines 179-202). Decisions and values only — no CSS diffs.

Sources read: `styles/tokens.css` (both theme blocks), `styles/themes.css`, `styles/base.css`
(body paints `--color-bg`; `.chat-panel` and the workspace authoring pane paint nothing, so the
strip's backdrop **is `--color-bg`**: `#0f2d35` dark / `#f4f8f9` light), `chat/guided/guided.css`,
`chat/guided/stepLabels.ts`, `workspace/workspace.css` (`--authoring-pane-width: 360px`), and the
elspeth-design skill (palette/type/spacing guidance). Screenshot
`composer-1920x1080-workspace-empty.png` confirms the defect: five bordered rectangles that read as
input fields.

## Why the current strip fails (the arithmetic of the defect)

With `.guided-workflow-index` hidden, the three states differ only by:

| Signal | Dark ratio | Light ratio |
|---|---|---|
| upcoming bg `--color-surface` vs backdrop `--color-bg` | 1.03:1 | 1.07:1 |
| complete/current bg `--color-surface-elevated` vs upcoming bg | 1.21:1 | 1.00:1 (both `#ffffff`) |
| border `--color-border` (composited `#1e4047` / `#d9e0e1`) vs border-strong (`#2f545a` / `#bbc5c8`) | 1.75 vs 1.30 | 1.65 vs 1.25 |

Every state-bearing difference is below 2:1 — far under the 3:1 the states owe WCAG 1.4.11 — and
the one surviving shape (a bordered, backgrounded rounded rectangle) is this design system's
*input* idiom. In light theme surface and elevated are both white, so complete vs upcoming is
carried by a border-alpha shift of 0.13 alone.

---

## 1. Rulings on the five design questions

**Dot vs numeral vs checkmark for complete → checkmark.**
A dot is the tutorial-progress vocabulary (`.tutorial-progress-dot`) and carries no semantics; a
numeral is this design's "not yet visited" mark (see below), so keeping it on completed steps says
nothing changed. The check is the universal "settled" mark, needs no locale, and fits the brand's
functional-glyph doctrine. Render it as an inline stroke SVG (Lucide `check`, stroke 1.8, round
caps, `currentColor`) at 12px, `aria-hidden="true"` — not the unicode ✓, whose baseline and weight
drift across platforms at 12px.

**Fill vs border for current → border (2px ring), numeral inside.**
Two reasons. (a) Token arithmetic: a filled "you are here" disc needs a fill that clears 3:1
against the backdrop AND carries a 4.5:1 numeral, in both themes. No existing pair does it:
`--color-accent` fill fails the boundary in dark (2.73:1); `--color-chip-border` fill passes the
boundary (4.97:1) but white text on it is 2.92:1 and no dark-navy *text* token exists to put on it.
(b) System semantics: in this UI a solid accent fill means *committed/selected* (the pressed chip's
outlined → tinted → filled ladder in guided.css). Giving the fill to **complete** (the committed
step) and the emphasized open ring to **current** (the step still being worked) reuses that ladder
instead of contradicting it. Current's strength comes from the 2px ring weight, the bold numeral,
and the semibold full-strength label — three redundant cues, none color-alone.

**Whether upcoming steps need any container → no container; they keep the indicator circle only.**
The bordered rectangle dies for **all** states — it is what made the strip read as inputs. Upcoming
steps still render their circle (1px hairline ring + muted numeral), because five always-visible
nodes on one rail are the sequence signal; an upcoming step reduced to bare gray text would read as
a label, not a position. The container's job (grouping indicator+label) is done by proximity.

**Connector vs no connector → connector.**
A continuous 1px horizontal rail behind the five indicators is the strongest available "this is a
sequence" cue and costs zero column width. It is uniform `--color-border-strong` in all segments —
deliberately **not** tinted on the traversed portion: state lives in the nodes, and an untinted
rail is decorative (exempt from 1.4.11, same doctrine as the canvas dot grid note in tokens.css
lines 168-175). The rail never touches the circles (small clear gap or masked by the indicator).

**Step title wrap → allowed, two lines max, indicators stay on the rail.**
Layout becomes indicator-above-label (stacked, centered) instead of the current side-by-side row.
Side-by-side at 360px gives a 64px cell where 24px indicator + gap leaves ~36px for "Transforms" —
guaranteed wrap into raggedness. Stacked, the label gets the full 64px. Labels keep
`overflow-wrap: break-word`, center-aligned, `line-height` 1.2; the indicator is the first element
and top-aligned in every cell, so a wrapped label deepens the band but never bends the rail.

## 2. State table

Geometry (all states): indicator = 20px (1.25rem) circle, centered; label centered beneath, gap
`--space-xs` (4px); step cells remain `grid-template-columns: repeat(5, minmax(0,1fr))` with the
existing 2px compact gap; band padding unchanged (`--space-xs --space-lg 0`). Numerals and glyph
at `--font-size-xs` (12px); labels at `--font-size-xs` (12px, the compact override), Inter.
No container box, no background, no border on the step cell itself, any state.

| State | Container | Indicator | Indicator border | Glyph / numeral | Label |
|---|---|---|---|---|---|
| **Complete** | none | disc filled `--color-accent` (dark `#1a7a52`, light `#156048`) | 1px `--color-chip-border` (dark `#3fa980`, light `#156048` — aliases accent) | Lucide check, 12px, `--color-text-inverse` (`#ffffff` both themes), aria-hidden | `--color-text-secondary` (dark `#a8d0d0`, light `#3a5a64`), `--font-weight-regular` |
| **Current** | none | transparent (backdrop shows through) | **2px** `--color-chip-border` (dark `#3fa980`, light `#156048`) | numeral, 12px, `--font-weight-bold`, `--color-text` (dark `#dff0ee`, light `#0f2d35`) | `--color-text` (dark `#dff0ee`, light `#0f2d35`), `--font-weight-semibold` |
| **Upcoming** | none | transparent | 1px `--color-input-border` (dark `#608a8a`, light `#6c919d`) | numeral, 12px, `--font-weight-regular`, `--color-text-muted` (dark `#93b3b3`, light `#426069`) | `--color-text-muted` (dark `#93b3b3`, light `#426069`), `--font-weight-regular` |
| Rail (all) | — | 1px horizontal line between adjacent indicators, `--color-border-strong` (composited: dark `#2f545a`, light `#bbc5c8`), decorative | — | — | — |

The hidden-index rule (`.chat-panel--guided .guided-workflow-index { display: none; }`,
guided.css:190-192) is retired: the index element becomes the indicator circle in every state and
is never hidden at any width. The `--complete` / `--current` container recolors (guided.css:61-71)
are retired with the box.

## 3. Contrast verification

Method: WCAG relative luminance from the token hex values above; translucent tokens composited over
their actual backdrop before computing. Backdrop is `--color-bg` in both themes (see Sources).
Ratio = (L₁+0.05)/(L₂+0.05). Sample arithmetic, dark current ring: L(`#3fa980`)=0.3038,
L(`#0f2d35`)=0.0212 → (0.3538/0.0712) = **4.97:1**. All values below computed the same way
(script-verified, not eyeballed).

State-bearing indicators → SC 1.4.11 (≥3:1). Label/numeral text at 12px → SC 1.4.3 (≥4.5:1).

| Pair | Dark | Light | SC | Verdict |
|---|---|---|---|---|
| Complete disc boundary: `--color-chip-border` vs `--color-bg` | `#3fa980`/`#0f2d35` **4.97:1** | `#156048`/`#f4f8f9` **7.02:1** | 1.4.11 | PASS / PASS |
| Complete check: `--color-text-inverse` vs `--color-accent` fill | `#ffffff`/`#1a7a52` **5.32:1** | `#ffffff`/`#156048` **7.50:1** | 1.4.11 (glyph) | PASS / PASS |
| (info) complete fill vs backdrop | `#1a7a52`/`#0f2d35` 2.73:1 | 7.02:1 | — | boundary duty carried by the 1px chip-border ring in dark; fill alone is not the state signal |
| Current ring: `--color-chip-border` vs `--color-bg` | **4.97:1** | **7.02:1** | 1.4.11 | PASS / PASS |
| Current numeral: `--color-text` vs `--color-bg` | `#dff0ee` **12.33:1** | `#0f2d35` **13.57:1** | 1.4.3 | PASS / PASS |
| Current label: `--color-text` vs `--color-bg` | **12.33:1** | **13.57:1** | 1.4.3 | PASS / PASS |
| Upcoming ring: `--color-input-border` vs `--color-bg` | `#608a8a` **3.80:1** | `#6c919d` **3.18:1** | 1.4.11 | PASS / PASS (thin margin light — see Caveats) |
| Upcoming numeral: `--color-text-muted` vs `--color-bg` | `#93b3b3` **6.45:1** | `#426069` **6.32:1** | 1.4.3 | PASS / PASS |
| Upcoming label: `--color-text-muted` vs `--color-bg` | **6.45:1** | **6.32:1** | 1.4.3 | PASS / PASS |
| Complete label: `--color-text-secondary` vs `--color-bg` | `#a8d0d0` **8.71:1** | `#3a5a64` **6.95:1** | 1.4.3 | PASS / PASS |
| Rail: `--color-border-strong` composited vs `--color-bg` | 1.75:1 | 1.65:1 | exempt | decorative, non-state-bearing (canvas-grid precedent, tokens.css:168-175) |

Rejected pairings (why the table is what it is): dark `--color-accent` as ring/boundary = 2.73:1
FAIL; dark `--color-btn-primary-bg` fill = 2.10:1 FAIL; white numeral on a `--color-chip-border`
fill = 2.92:1 FAIL. These three failures force: chip-border for rings, accent (not chip-border)
for the complete fill, and no filled treatment for current.

**No new color tokens are required.** Every state meets its SC from the shipped palette in both
themes. (`--color-chip-border` is documented in tokens.css as the accent variant that exists
precisely to clear 1.4.11 on dark surfaces; the stepper becomes its second consumer. If that
semantic stretch is unwanted, the alternative is a new alias token — e.g.
`--color-progress-indicator`, dark `#3fa980` / light `#156048`, same hexes — a naming decision
only, flagged for the developer; no new color values either way.)

## 4. Behaviour notes

**Wrap.** Labels center under their indicator and may wrap to two lines ("Transforms" at 12px Inter
is ~60px vs a 64px cell at 360px — one line usually, two under user font-size overrides). The
label vocabulary is a closed list (`stepLabels.ts` + the completed-surface "Review"/"Validation"
variants, longest 10 chars), so pathological lengths cannot occur. Indicators top-align so a
wrapped label in one cell never bends the rail. Band height goes from ~26px to ~39px (20px
indicator + 4px gap + one 15px label line); the `max-height: 760px` tightening rules apply
unchanged to band padding.

**Width envelope.** At 360px pane width: 328px content − 8px gaps = 64px/cell. At 400px: 72px.
At 440px: 80px. The design is identical across the envelope — nothing is hidden or swapped at any
width; the ≤640px *viewport* media query (2-column fallback for true mobile) is out of scope and
unchanged.

**Overflow.** `minmax(0, 1fr)` columns are retained; the strip can never force horizontal scroll.

**Reduced motion.** No animation is used — none is needed; state changes are instant repaints on
step advance. The existing blanket `@media (prefers-reduced-motion: reduce)` rule for `guided-*`
(guided.css end block) keeps covering the element; there is nothing new for it to silence.

**Forced colors / high contrast.** `prefers-contrast: more` needs nothing: all pairings already
sit ≥3.18:1 and the token overrides in themes.css only push them higher. `forced-colors: active`
needs the same treatment the tutorial dots got (themes.css:72-99): indicator rings `1px solid
CanvasText`, complete fill `Highlight` (check `HighlightText`), current ring `Highlight` at 2px —
flagged as a themes.css addition, decision only.

**Aria semantics** (markup is `nav > ol > li`, ChatPanel.tsx:3093-3115):
- Keep `aria-current="step"` on the current `li` (already present, correct token).
- The indicator (numeral or check SVG) is `aria-hidden="true"` — position is already conveyed by
  list order; a bare "3" adds noise, and the check is purely visual.
- Add a visually-hidden state suffix inside each non-current `li`: "`, completed`" and
  "`, not started`" (sentence-case copy per voice rules). Current needs none — `aria-current`
  announces it. This is required because with the numeral hidden from AT, complete vs upcoming is
  otherwise a purely visual distinction.
- Drop the redundant `aria-label="Guided workflow"` on the `ol` — the `nav` already carries
  "Guided workflow progress"; two nested near-identical labels are SR noise. (The nav label should
  not embed dynamic "step 2 of 5" — the list itself conveys that.)

## 5. Why this reads as progress

The perceptual mechanism being restored is **a path with positions on it**, expressed through four
redundant channels the boxes destroyed:

1. **A rail.** Five nodes threaded on one continuous line is the pre-attentive signature of a
   sequence; a row of equal rectangles is the signature of a form. The connector is what makes the
   eye read "stations on a route" before reading a single word.
2. **A gradient of visual weight that encodes time.** Solid filled disc (done) → heavy open ring
   (here) → hairline ring (ahead). Past is heaviest and most "settled", future is faintest; the
   asymmetry points in the direction of travel. The old design's states were symmetric rectangles
   — no direction, no "you are here".
3. **Glyph semantics.** Check = finished, numeral = position not yet reached. The indices also
   restore the ordinal reading ("2", "3", "4" visible ahead of you) that the responsive recode
   removed entirely.
4. **Label weight echo.** The current label is the only semibold, full-contrast text in the band,
   so the "you are here" signal survives even squinting / small sizes, and never rests on hue
   alone — weight, ring thickness, and glyph all agree.

Killing the per-step border/background also removes the false affordance: nothing in the band any
longer shares the rectangle+border+fill grammar of `.guided-schema-input` / the chip buttons, so
nothing invites a click it cannot honor.

## 6. SME protocol

**Confidence: high** on token arithmetic and state design (ratios computed from the shipped hex
values against the verified `--color-bg` backdrop; markup and current CSS read in full).
**Medium** on the exact rendered width of "Transforms" at 12px (font-metric estimate, not a
browser measurement — the two-line wrap allowance absorbs the uncertainty).

**Risk:**
- *Low:* `--color-chip-border` gains a second consumer (stepper indicators). If chip styling is
  ever retuned for chips specifically, the stepper inherits it silently — mitigated by the flagged
  alias-token option in §3.
- *Low:* band grows ~13px taller; on <760px-tall viewports the existing padding tightenings apply,
  but if vertical budget is contested the indicator can drop to 18px without any contrast
  consequence (contrast is color-pair, not size, dependent; 1.4.11 has no minimum size here).
- *Low:* `completionSurface.test.ts` and any snapshot/contrast gates that pin `.guided-workflow-*`
  will need updating alongside the implementation (`colorContrast.test.ts` parses `:root` /
  `[data-theme="light"]` top-level blocks — untouched by this spec since no token values change).

**Information gaps:**
- Not verified in a browser (read-only, no-browser constraint): sub-pixel rendering of a 1px ring
  on a 20px circle at non-integer zoom, and real Inter metrics at 12px.
- The completed surface (`.chat-panel--completed`) reuses this stepper with the fifth step current
  and relabeled ("Review"/"Validation"); this spec assumes identical treatment there — the padding
  override (guided.css:121-123) is compatible, but that surface was not screenshot-verified.
- Whether any e2e/a11y gate asserts the *presence* of the hidden-index rule was not exhaustively
  searched; `completionSurface.test.ts` references `guided-workflow` and must be re-read at
  implementation time.

**Caveats:**
- The light-theme upcoming ring (`#6c919d` on `#f4f8f9`, 3.18:1) passes with ~6% margin. It is the
  designated "boundary that must clear 3:1" token and already carries this duty on inputs; if the
  page background is ever lightened past `#f4f8f9`, this is the first pairing to re-check.
- The dark complete-disc *fill* is 2.73:1 against the backdrop; compliance rests on the 1px
  chip-border ring plus the 5.32:1 white check. Implementations must not drop the ring "since the
  fill is visible anyway".
- The rail is deliberately below 3:1 (decorative). If a future revision makes rail segments encode
  state (e.g. tinting traversed segments), they become state-bearing and inherit the 3:1 duty —
  use `--color-chip-border` if so.
