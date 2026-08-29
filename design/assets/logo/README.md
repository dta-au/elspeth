# ELSPETH logo

ELSPETH has **no graphical logo**. The brand mark is the typographic **wordmark**:

```
ELSPETH
```

- **Typeface:** JetBrains Mono, weight **700**
- **Case:** UPPERCASE
- **Tracking:** `letter-spacing: 0.18em` (≈ 5.76px at 32px)
- **Colour:** `--color-text` (#dff0ee) on a navy (`--color-surface-nav` #0a1d2e) or teal chrome; never on photography.

**Always reproduce it as live text** (so it inherits theme colour and stays crisp), e.g.:

```html
<span style="font-family: var(--font-mono); font-weight: 700;
             text-transform: uppercase; letter-spacing: 0.18em;
             color: var(--color-text);">ELSPETH</span>
```

`wordmark.svg` is provided only as a static fallback (e.g. favicon, social card). Prefer the text form in product and on the web. Do not recolour, outline, add a graphical icon, or alter the tracking.
