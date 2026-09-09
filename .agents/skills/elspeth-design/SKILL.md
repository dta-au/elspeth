---
name: elspeth-design
description: Use this skill to generate well-branded interfaces and assets for ELSPETH, either for production or throwaway prototypes/mocks/etc. Contains essential design guidelines, colors, type, fonts, assets, and UI kit components for prototyping.
user-invocable: true
---

The ELSPETH design system is a product asset and lives at the repository's
top-level `design/` directory, not inside this skill. Every path below is
relative to the repository root.

Read `design/readme.md` first, then explore the other files under `design/`.

If creating visual artifacts (slides, mocks, throwaway prototypes, etc), copy assets out of `design/` and create static HTML files for the user to view. If working on production code, you can copy assets and read the rules there to become an expert in designing with this brand.

If the user invokes this skill without any other guidance, ask them what they want to build or design, ask some questions, and act as an expert designer who outputs HTML artifacts _or_ production code, depending on the need.

## What's in `design/`
- `design/styles.css` — the single global entry point. Link it and you get every token, font, and shared primitive class. It `@import`s `design/tokens/` (`fonts`, `colors`, `typography`, `layout`, `base`, `primitives`).
- `design/tokens/` — CSS custom properties. The colour system is a DTA/AGDS three-family model (workspace teal / navigation navy / inspection warm-neutral), dark-default with a full `[data-theme="light"]` theme.
- `design/components/` — React primitives (`Button`, `TypeBadge`, `StatusBadge`, `Card`, `AlertBanner`, `Tabs`, `Input`, `Textarea`, `WordMark`, `ChatBubble`, `PluginCard`). Each ships a `.d.ts` and `.prompt.md`.
- `design/ui_kits/composer/` — an interactive recreation of the ELSPETH Web Composer (login → conversational pipeline build → validate → run), composing the primitives above.
- `design/ui_kits/website/` — the marketing-site kit. The canonical public site is the top-level `website/` tree, which keeps its own tokens copy; do not merge the two.
- `design/guidelines/` — foundation specimen cards (Colors, Type, Spacing, Brand, Iconography).
- `design/assets/logo/` — the wordmark (there is no graphical logo; reproduce ELSPETH as live text).
- `design/_ds_bundle.js` + `design/_ds_manifest.json` — the compiled component bundle and its manifest; `design/_adherence.oxlintrc.json` — the brand-adherence lint config.

## Brand essentials (read `design/readme.md` for the full guide)
- **Wordmark:** `ELSPETH` in JetBrains Mono 700, uppercase, `letter-spacing: 0.18em`. Always live text.
- **Type:** Inter for UI/body; JetBrains Mono for the audit/forensic register (wordmark, code, YAML, hashes, type badges).
- **Voice:** precise, calm, evidence-led; sentence case; "you" for guidance, system-voice otherwise; **no emoji** (functional unicode glyphs ⚙ ⚠ ∅ only).
- **Icons:** Lucide (stroke 1.8, round joins, `currentColor`) — the closest match to the product's bespoke inline SVGs.
- **Shape:** crisp institutional corners (4/6/8/12px), borders do the work not big shadows, left-edge accents are a recurring motif. Restrained 100–250ms motion; hover lightens surface, focus is a 2px ring.
- **The six pipeline component types** (Source / Transform / Gate / Sink / Aggregation / Coalesce) each have a fixed colour — keep that vocabulary.

## Using the components
In an HTML file, link `design/styles.css`, load `design/_ds_bundle.js`, then read components from the namespace:
```html
<link rel="stylesheet" href="design/styles.css" />
<script src="design/_ds_bundle.js"></script>
<script>const { Button, TypeBadge, ChatBubble } = window.ELSPETHDesignSystem_85edbb;</script>
```
(Confirm the exact namespace by checking `design/_ds_manifest.json`.) For throwaway mocks, prefer copying the token CSS and the class names from `design/tokens/primitives.css` directly into a static HTML file.

Source of truth: the product frontend at **https://github.com/dta-au/elspeth** (`src/elspeth/web/frontend`).
