# ELSPETH Design System

A design system for **ELSPETH** — *Extensible Layered Secure Pipeline Engine for Transformation and Handling* — reconstructed from the product's own frontend source so design agents can build on-brand interfaces, mocks, and assets.

> **Sources used to build this system**
> - GitHub: **https://github.com/johnm-dta/elspeth** (the `johnm-dta/elspeth` repository)
>   - Design tokens & CSS lifted from `src/elspeth/web/frontend/src/styles/` (`tokens.css`, `base.css`, `shared.css`, `animations.css`, `themes.css`) and per-area component CSS under `src/elspeth/web/frontend/src/components/**`.
>   - Layout & component structure read from `src/elspeth/web/frontend/src/App.tsx`, `LoginPage.tsx`, and the chat / sidebar / catalog component trees.
> - A test deployment of the Web Composer (credentials supplied by the project owner) was referenced for UX behaviour.
>
> If you have access, explore the repository directly — especially the `web/frontend` tree — to build richer, more accurate ELSPETH designs than this distilled system can capture.

---

## What ELSPETH is

ELSPETH is a **high-assurance pipeline substrate for consequential workflows** — systems where a wrong output can cause operational, legal, safety, financial, or security harm. It is positioned for sensitive, regulated, transactional, medical, security, or defence-adjacent work where every step must be reviewable and auditable.

It offers **two authoring surfaces over one runtime assurance model**:

1. **YAML Operator Path** — hand-edited, version-controlled YAML for operators who read graph edges and runtime evidence directly.
2. **Web Composer** — an authenticated React app where a knowledge worker builds a pipeline conversationally; an LLM tool loop mutates pipeline state through audited tools, contracts, validation, and preflight checks rather than emitting unchecked config text.

Both surfaces target the same primitives, plugin contracts, runtime assembly, graph validation, **Landscape** audit trail, and run-accounting model. Validation and audit are core product properties, not after-the-fact diagnostics.

**The mental model is Sense → Decide → Act:**
- **Sense** (Sources) — load data: CSV, JSON, text/blob, Azure Blob, Dataverse, database, Chroma.
- **Decide** (Transforms + pure-config Gates) — map, coerce, classify, run LLM queries, evaluate threshold gates, compute batch analytics.
- **Act** (Sinks) — route rows to outputs: files, database rows, review queues, alerts.

The six pipeline component types — **Source, Transform, Gate, Sink, Aggregation, Coalesce** — are the load-bearing vocabulary of the entire UI, each with its own functional colour.

### The product this system designs for

The primary surface is the **Web Composer**: a three-column operator console.
- A **navy header** (navigation family) carries the `ELSPETH` wordmark, the session switcher, and the user menu.
- A **teal chat panel** (workspace family) is the conversation with the LLM composer — message bubbles, tool-call cards, a composing indicator, and the chat input.
- A **warm-neutral side rail** (inspection family) holds a graph mini-view, validation banner, audit-readiness panel, and a completion bar (Save-for-review / Run / Copy YAML).
- A **plugin catalog drawer** slides in from the right to browse Sources / Transforms / Sinks with their audit characteristics.

---

## CONTENT FUNDAMENTALS

How ELSPETH writes. The voice is that of an **assurance engineer**: precise, calm, evidence-led, and never hype.

**Tone & vibe.** Forensic and trustworthy. Copy reads like it was written by people who expect to be audited. Claims are hedged honestly ("design intentions, not release commitments"). The product never oversells — it lists where to "Consider Alternatives" (Spark, dbt, Flink) plainly. The vibe is *operator-grade infrastructure*, not consumer SaaS.

**Person.** Mostly impersonal / system-voice. The UI addresses the user as **"you"** in guidance ("Use it when you want to build, inspect, validate…", "Choose the authoring path that matches how you want to work"). The system describes its own actions in third person ("the composer builds through tools"). Avoid "we"; avoid first-person singular entirely.

**Casing.** Sentence case for everything in the UI — headings, buttons, labels ("Sign in to ELSPETH", "Save for review", "Copy YAML", "Configure API keys"). The single deliberate exception is the brand wordmark **ELSPETH**, always all-caps. Component-type and status badges are **UPPERCASE** by CSS transform (SOURCE, TRANSFORM, RUNNING, COMPLETED) — but authored in normal case in the data.

**Terminology — use the product's exact words:**
- Components are *Sources, Transforms, Gates, Sinks, Aggregations, Coalesce points* — not "nodes" loosely, not "steps".
- The audit trail is **Landscape**. A processed unit is a **token** (row lineage). Outcomes are **completed / completed_with_failures / failed / empty / cancelled**.
- "Pure-config gates", "preflight", "declaration-trust", "run accounting", "provenance", "quarantine", "trust boundary", "fingerprint" are all load-bearing terms — reuse them.
- The three-tier **Data Trust Model** (Tier 1 full trust / Tier 2 elevated / Tier 3 zero trust) is a signature concept.

**Sentence shape.** Short, declarative, often imperative for actions. Comments and docs favour explaining *why* a decision was made ("shifted cyan-ward to break the proximity to --color-success"). Error and validation copy is specific and operator-actionable, never "Something went wrong" — e.g. *"Backend unavailable — Cannot connect to the ELSPETH server. Check that the backend is running."*

**Emoji.** **None** in product UI. Do not use emoji. The product instead uses a small set of **unicode glyphs as functional marks**: ⚙ (settings), ⚠ (warning / completed-with-failures), ∅ (empty result), and stroke-icon SVGs for everything else. A warning is signalled by a glyph + colour, not a 🟠.

**Examples to imitate:**
- Heading: "Sign in to ELSPETH"
- Button: "Save for review", "Run", "Copy YAML", "Retry connection"
- Banner: "Service unavailable: The composer cannot reach a usable LLM right now."
- Eyebrow/label: "PLUGIN CATALOG", "AUDIT READINESS", micro-labels uppercased and tracked.
- Status: "completed_with_failures" (snake_case in data, rendered as a badge).

---

## VISUAL FOUNDATIONS

The aesthetic is **DTA / AGDS-inspired** (Australian Digital Transformation Agency / Australian Government Design System lineage): institutional, calm, high-contrast, accessibility-first. It is a **dark-default** product with a fully specified light theme.

### The three-family colour model (the single most important visual idea)
Surfaces are deliberately split into three colour families so a screen reads as **paper-on-a-desk** instead of one flat slab:
- **Workspace family — Pacific teal** (`--color-bg #0f2d35`, `--color-surface #122f37`, elevated `#1a3d47`): active editing surfaces, the chat panel.
- **Navigation family — Foundation navy** (`--color-surface-nav #0a1d2e`): the header chrome.
- **Inspection family — warm neutral** (`--color-surface-inspector #2a2826`, paper `#332f2c`): the right rail and modal "paper" panels. This warm neutral against the cool teal/navy is the signature contrast.

### Colour
- **Accent** is a muted forest green (`--color-accent #1a7a52`); the **primary button** is a deeper green (`#16664a`). Links and info use **cyan** (`#61daff` dark / `#176d8a` light).
- **Semantics:** success = teal `#14b0ae`, error = coral-red `#e85653`, warning = amber `#e38444`, info = cyan `#61daff`. Each ships a 10–14% alpha background and a ~30% border for inline banners.
- **The six component-type colours are a fixed vocabulary:** Source aqua-green `#4db89a`, Transform amber `#e8a030`, Gate purple `#c390f9`, Sink orange-red `#e07040`, Aggregation cyan `#61daff`, Coalesce cyan-teal `#18c2c0`. Each has a pre-composited opaque background + 30% border for badges and graph nodes.
- Imagery is **not** a feature of this product — it is a data/operator tool. No hero photography, no stock illustration. The "imagery" is the **pipeline graph** itself (React Flow canvas with a faint dot grid).

### Typography
- **Inter** for all body/UI text (the AGDS Foundation typeface).
- **JetBrains Mono** for the **audit/forensic register**: the `ELSPETH` wordmark (700, uppercase, `letter-spacing 0.18em`), code, YAML, content hashes, and the component-type badges. The mono face deliberately reads as "audit evidence".
- The scale is **fine and dense** (10/11/12/13/15/16/18/22/32px) — this is an information-dense operator console, not a marketing page. Body is 16px; chrome hints drop to 11px; badges to 10–12px.
- Line heights: tight 1.3, snug 1.35 (chat bubbles), normal 1.5, relaxed 1.7 (code).

### Spacing, radius, shape
- Spacing scale: **2, 4, 8, 12, 16, 24, 32px**. Dense but rhythmic.
- Radii: **4 / 6 / 8 / 12px** plus pill (9999px). Buttons are 6px; cards 8px; modals 8px; badges 4px. Nothing is heavily rounded — corners are crisp and institutional.
- Cards: a subtle 1px teal-tinted border (`--color-border` ≈ 12% alpha) on `--color-surface`, radius 8px. Elevation is expressed by **borders and a slightly lighter surface**, not big shadows. The few shadows that exist are soft and low (`0 8px 32px rgba(0,0,0,0.25)` on modals; `0 2px 8px` on floating pills).

### Borders, shadows, transparency
- **Borders do the heavy lifting**, not shadows. Teal-tinted translucent borders (`rgba(143,200,200,0.12)` → `0.25` strong) separate surfaces.
- **Left-edge accents** are a recurring motif: the assistant chat bubble has a 2px left border; tool-call cards have a 4px coloured left border (amber pending → teal committed → red rejected); pending proposals use a **dashed** left accent.
- **Transparency & alpha** are used constantly for semantic backgrounds and bubble fills (10–18% alpha over the surface). Blur is **not** a signature — this is a flat, opaque, high-contrast system, not glassmorphism.

### Motion
- Restrained and functional. Transitions are **100ms / 150ms / 250ms ease**. Only four keyframe animations exist: a staggered three-dot **composing bounce**, an indeterminate **progress stripe**, a **pulse-dot** (cancel-pending), and a button **spinner**.
- **Hover** states lighten the surface (`--color-surface-hover` ≈ 4% white) and strengthen the border — never a transform/scale. **Focus** is a 2px solid ring in `--color-focus-ring` (white on dark, near-black on light) with 2px offset. There is no press/shrink animation.
- Everything respects `prefers-reduced-motion: reduce` (animations drop to a static dimmed state) and `prefers-contrast: more` / `forced-colors: active`.

### Layout rules
- Full-viewport app shell: a fixed 40px navy header, then a CSS-grid main area `"chat siderail"`. The side rail is a fixed 320px (min 240px) inspection column.
- The login is a centred 360px card on `--color-bg`.
- Drawers (plugin catalog) slide from the right at `min(440px, 100%−24px)` over a 30%-black backdrop.
- WCAG is structural: 44px AAA touch targets for canvas-area buttons, 36px AA compact controls for chrome rows, visible focus rings, and tokenised disabled states (never `opacity:0.4`).

---

## ICONOGRAPHY

ELSPETH uses **inline stroke SVG icons**, not an icon font, not raster PNGs, and not emoji.

- **Style:** thin-line, geometric, `stroke: currentColor`, `stroke-width ≈ 1.8`, `stroke-linecap: round`, `stroke-linejoin: round`, `fill: none`. This matches the **Lucide / Feather** family precisely (the chat-input icons in the source are hand-authored in exactly this style). Icons inherit text colour and sit at ~20px in controls.
- **Recommendation for new work:** use **Lucide** (https://lucide.dev) from CDN as the canonical icon set — its stroke weight and round joins are an exact match. This system documents that substitution; the product's own icons are bespoke but stylistically identical to Lucide. See `assets/icons/` for a small set copied in this style and `guidelines/iconography.html` for the specimen.
- **Functional unicode glyphs** are used sparingly as status marks where an icon would be overkill: `⚙` settings, `⚠` warning / completed-with-failures, `∅` empty result, `⊘`/`×` close. Keep these monochrome and inherit colour.
- **No emoji.** No coloured/3D icon sets. No duotone. Icons never carry their own brand colour — they take the colour of their context (muted by default, semantic when in a banner).
- **Logo / wordmark:** ELSPETH ships **no graphical logo** — the brand mark *is* the typographic wordmark: `ELSPETH` in JetBrains Mono 700, uppercase, `letter-spacing: 0.18em`. Reproduce it as text, never as an image. See `assets/logo/`.

---

## Index / manifest

Root files:
- `styles.css` — global entry point (link this). Imports everything below.
- `tokens/` — `fonts.css`, `colors.css`, `typography.css`, `layout.css`, `base.css` (reset), `primitives.css` (shared classes).
- `assets/` — `logo/` (wordmark specimen + usage), `icons/` (stroke-SVG set in the ELSPETH style).
- `guidelines/` — foundation specimen cards (Colors, Type, Spacing, Brand, Iconography) that populate the Design System tab.
- `components/` — reusable React primitives (see below).
- `ui_kits/composer/` — the Web Composer recreation (login + composer shell + catalog).
- `ui_kits/website/` — a public-facing marketing **site** for ELSPETH (net-new; the product has no site today, only the GitHub README). A few short pages — Home, Authoring, Assurance, Use cases, Get started — sharing `site.css` + `site.js`, built entirely from the brand foundations, with a working light/dark toggle.
- `SKILL.md` — Agent-Skill front-matter wrapper so this system can be used in Claude Code.

**Components** (`window.ELSPETHDesignSystem_85edbb.*`): `Button`, `TypeBadge`, `StatusBadge`, `Input`, `Textarea`, `Card`, `Tabs`, `AlertBanner`, `ChatBubble`, `PluginCard`, `Toolbar`, `WordMark`.

**UI kits:**
- `ui_kits/composer/` — `index.html` (interactive: login → composer → run), plus `LoginScreen`, `ComposerShell`, `ChatPanel`, `SideRail`, `CatalogDrawer`, `PipelineGraph` JSX.
- `ui_kits/website/` — a multi-page marketing **site**: `index.html` (Home), `authoring.html`, `assurance.html`, `use-cases.html`, `get-started.html`, plus shared `site.css` and `site.js`. Static HTML on the token system; no bundle dependency; light/dark toggle in the nav.

> **Note:** the website is a *new* surface designed in-brand, not a recreation — ELSPETH ships no public site, so this proposes one rather than copying an existing design.

---

## Caveats & substitutions
- **Fonts:** Inter and JetBrains Mono are loaded from Google Fonts (exactly as the product does). No local font binaries are shipped.
- **Icons:** the product's icons are bespoke inline SVGs; this system supplies a small matching set and recommends **Lucide** as the closest CDN-available equivalent. Flag any place a specific product glyph is needed.
- This system is **distilled from the frontend source**, not an exhaustive export. The repository remains the source of truth — explore `web/frontend` for anything not captured here.
