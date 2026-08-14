import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Arithmetic invariants for the message-bubble action overlays
// (elspeth-e6db9519d2, elspeth-8174010851, elspeth-eb3a875f4d).
//
// The copy overlay (and, on user turns, the edit overlay) is absolutely
// positioned inside the SAME element that carries the message text —
// MessageBubble.tsx puts `bubble`, `message-bubble-content` and
// `bubble-action-overlay` on one box — so an absolutely-positioned child
// contributes nothing to the parent's width. Three separate values therefore
// have to be chosen together:
//
//   * the overlay's own width (--size-control-compact),
//   * how far the edit overlay is inset from the right edge, and
//   * how much right padding the text column reserves.
//
// Get any pair out of step and the control paints on top of the message's own
// first line (it did: 29px into the prose on assistant bubbles, ~72px on user
// bubbles), or the bubble shrink-to-fits below the overlays' combined width
// and the edit button resolves to a NEGATIVE left edge and floats onto the
// canvas beside the turn.
//
// These are deliberately not declaration-existence tests: each one resolves
// the winning declaration out of the barrel, substitutes the shipped token
// values, and checks the inequality that has to hold. A future edit that
// changes any single value in isolation fails here.
//
// Barrel-loaded in cascade order, exactly like styles/primitiveGeometry.test.ts.
// Import paths are resolved against the BARREL's directory, not this file's —
// cwd-relative per the tokenReferences.test.ts idiom; vitest runs from the
// frontend root.
const barrelDir = "src/styles";
const stylesheetBarrel = readFileSync(join(barrelDir, "index.css"), "utf8");
const appCss = Array.from(
  stylesheetBarrel.matchAll(/@import\s+"(?<path>[^"]+)";/g),
)
  .map((match) => {
    const importPath = match.groups?.path;
    if (importPath === undefined) {
      throw new Error("styles/index.css import regex produced no path");
    }
    return readFileSync(join(barrelDir, importPath), "utf8");
  })
  .join("\n");

function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

interface Rule {
  selectors: string[];
  declarations: string;
}

const rules: Rule[] = Array.from(
  stripComments(appCss).matchAll(/([^{}]+)\{([^{}]*)\}/g),
).map((match) => ({
  selectors: match[1].split(",").map((selector) => selector.trim()),
  declarations: match[2],
}));

/**
 * EVERY value declared for `property` by a rule listing `selector` verbatim,
 * in barrel order. @media-nested rules are flattened into the same list (the
 * parse captures them as their own entries), so a state that only exists
 * inside a media query is inspected too.
 */
function declaredValues(selector: string, property: string): string[] {
  const found: string[] = [];
  for (const rule of rules) {
    if (!rule.selectors.includes(selector)) continue;
    const match = new RegExp(`(?:^|;)\\s*${property}\\s*:([^;]+)`).exec(
      rule.declarations,
    );
    if (match) found.push(match[1].trim());
  }
  return found;
}

/**
 * The LAST value declared for `property` by a rule listing `selector`
 * verbatim, in barrel order — i.e. which declaration actually reaches the
 * element among the equal-specificity rules that name it.
 */
function declaredValue(selector: string, property: string): string {
  const found = declaredValues(selector, property);
  if (found.length === 0) {
    throw new Error(`No ${property} declared for ${selector}`);
  }
  return found[found.length - 1];
}

const tokensCss = stripComments(readFileSync("src/styles/tokens.css", "utf8"));

function tokenPx(name: string): number {
  const match = new RegExp(`${name}:\\s*(-?[\\d.]+)px\\s*;`).exec(tokensCss);
  if (!match) {
    throw new Error(`No px value for ${name} in tokens.css`);
  }
  return Number.parseFloat(match[1]);
}

/**
 * Resolve a length written as a bare px value or as a calc() over px tokens,
 * `+` and `*`. Deliberately narrow: anything richer throws rather than
 * silently resolving to a number that is not what the browser computes.
 */
function px(value: string): number {
  const substituted = value
    .replace(/^calc\(([\s\S]*)\)$/, "$1")
    .replace(/var\(\s*(--[\w-]+)\s*\)/g, (_, name: string) => `${tokenPx(name)}`)
    .trim();
  if (!/^[\d\s.+*]+$/.test(substituted)) {
    throw new Error(`Unsupported length expression: ${value}`);
  }
  return substituted
    .split("+")
    .map((term) =>
      term
        .split("*")
        .map((factor) => Number.parseFloat(factor.trim()))
        .reduce((a, b) => a * b, 1),
    )
    .reduce((a, b) => a + b, 0);
}

/** Inline-axis padding from `.bubble`'s two-value padding shorthand. */
function bubbleInlinePadding(): number {
  const shorthand = declaredValue(".bubble", "padding").split(/\s+/);
  expect(shorthand, ".bubble's padding shorthand must stay two-valued").toHaveLength(2);
  return px(shorthand[1]);
}

const overlayWidth = px(declaredValue(".bubble-action-overlay", "min-width"));
const copyInset = px(declaredValue(".bubble-action-overlay--copy", "right"));
const editInset = px(declaredValue(".bubble-action-overlay--edit", "right"));
const assistantGutter = px(declaredValue(".message-bubble-content", "padding-right"));
const userGutter = px(declaredValue(".message-bubble-content--user", "padding-right"));

describe("bubble action overlays never paint on the message text (elspeth-e6db9519d2)", () => {
  it("adopts one control size for the overlay, agreeing with the .bubble-copy-btn rule", () => {
    // The two rules carry the same classes at equal specificity, so the later
    // one wins every property they share. Before the fix .bubble-action-overlay
    // declared a 44px literal while .bubble-copy-btn's surviving comment
    // described a 36x36 target the control never rendered at.
    expect(overlayWidth).toBe(tokenPx("--size-control-compact"));
    expect(px(declaredValue(".bubble-action-overlay", "min-height"))).toBe(
      tokenPx("--size-control-compact"),
    );
    expect(px(declaredValue(".bubble-copy-btn", "min-width"))).toBe(overlayWidth);
    expect(px(declaredValue(".bubble-edit-btn", "min-height"))).toBe(overlayWidth);
  });

  it("reserves a full gutter beyond the copy overlay on every bubble", () => {
    expect(
      assistantGutter - (copyInset + overlayWidth),
      "the text column must clear the copy overlay by a real gutter, not overlap it",
    ).toBeGreaterThanOrEqual(tokenPx("--space-lg"));
  });

  it("reserves a full gutter beyond BOTH overlays on user bubbles", () => {
    // User turns are the only ones that render the edit overlay
    // (MessageBubble.tsx guards it on isUser), and it is the outer of the two.
    expect(
      userGutter - (editInset + overlayWidth),
      "user turns render copy AND edit; the text column must clear the outer one",
    ).toBeGreaterThanOrEqual(tokenPx("--space-lg"));
  });

  it("keeps the edit overlay clear of the copy overlay it sits beside", () => {
    expect(
      editInset - (copyInset + overlayWidth),
      "the edit overlay must start outside the copy overlay's box",
    ).toBeGreaterThanOrEqual(0);
  });
});

describe("the overlay's own state surface (elspeth-8174010851)", () => {
  // These two invariants used to be pinned in MessageBubble.test.tsx as raw
  // strings against chat.css — "opacity: 0.3;" and "min-width: 44px;". Both
  // pinned the LITERAL rather than the property it was chosen for, so both
  // locked in values their own tickets file as defects. Restated here, in the
  // stylesheet's own lane, as the constraints that actually have to hold.

  it("stays legible at rest, not merely present", () => {
    // The rule's intent — "visibly discoverable before hover or focus" — was
    // contradicted by its own value: at 0.3 over --color-text-muted the glyph
    // read as a smudge on the prose, and a control invisible until hover is
    // undiscoverable. Every declared resting value is checked, including the
    // @media (hover: none) branch, which has no hover to raise it with.
    const declared = declaredValues(".bubble-copy-btn", "opacity");
    expect(declared.length).toBeGreaterThan(0);
    for (const value of declared) {
      expect(
        Number.parseFloat(value),
        `resting opacity ${value} is below the legibility floor`,
      ).toBeGreaterThanOrEqual(0.5);
    }
    // The resting chip is what lets the glyph stay quiet without disappearing.
    expect(declaredValue(".bubble-action-overlay", "background-color")).toContain(
      "var(--color-surface-hover)",
    );
  });

  it("holds a footprint a one-character confirmation cannot outgrow", () => {
    // MessageBubble swaps the copy glyph for a single success mark on click
    // (elspeth-091695b241) precisely because the overlay is absolutely
    // positioned at right:0: anything wider than the control's floor can only
    // grow LEFTWARD, over the prose the user just copied. The argument rests
    // on there BEING a floor, not on it being any particular number — one
    // glyph at --font-size-xs plus --space-1-5 padding either side is far
    // inside the compact control rung.
    const glyphBox = tokenPx("--font-size-xs") + 2 * tokenPx("--space-1-5");
    expect(overlayWidth).toBeGreaterThan(glyphBox);
    expect(declaredValue(".bubble-action-overlay", "position")).toBe("absolute");
  });
});

describe("a short bubble cannot push an overlay off its own box (elspeth-eb3a875f4d)", () => {
  // .bubble declares max-width but no min-width, is a shrink-to-fit flex item,
  // and absolutely-positioned children add nothing to intrinsic width — so a
  // bubble containing "ok" once resolved the edit overlay's left edge to -38px
  // and the control floated over the canvas with no visible owner. Padding DOES
  // contribute, so the reserved gutters are what now floor the bubble's width.
  it("floors the user bubble wide enough to contain the outer overlay", () => {
    const intrinsicFloor = bubbleInlinePadding() + userGutter;
    expect(
      intrinsicFloor - (editInset + overlayWidth),
      "padding alone must span both overlays, whatever the message says",
    ).toBeGreaterThanOrEqual(0);
  });

  it("floors the assistant bubble wide enough to contain the copy overlay", () => {
    const intrinsicFloor = bubbleInlinePadding() + assistantGutter;
    expect(intrinsicFloor - (copyInset + overlayWidth)).toBeGreaterThanOrEqual(0);
  });
});

describe("one type size across both speakers (elspeth-b6e82c12bb)", () => {
  it("binds the markdown path's size to the bubble instead of common.css", () => {
    // .markdown-body is a DESCENDANT of .bubble, so common.css's
    // `.markdown-body { font-size: var(--font-size-sm) }` governed the
    // assistant's prose regardless of what .message-bubble-content set on the
    // bubble box — the operator rendered 16px against the composer's 13px.
    expect(declaredValue(".bubble .markdown-body", "font-size")).toBe("inherit");
  });

  it("sets that one size on the bubble itself", () => {
    expect(declaredValue(".message-bubble-content", "font-size")).toBe(
      "var(--font-size-sm)",
    );
  });

  it("keeps the markdown heading ladder above the bubble's body size", () => {
    // The reason the harmonisation went DOWN to 13px: --font-size-base (16px)
    // is also the h2 step, so raising bubble prose to it would have put h2 at
    // body size with h3 (15px) and h4 (13px) below — trading a size divergence
    // for a heading-hierarchy collapse.
    const body = tokenPx("--font-size-sm");
    for (const heading of ["--font-size-md", "--font-size-base", "--font-size-lg"]) {
      expect(tokenPx(heading), `${heading} must stay above the bubble body size`)
        .toBeGreaterThan(body);
    }
  });
});

describe("one gutter across the shared band row (elspeth-dfc207f341)", () => {
  // .chat-panel-header and .artifact-workspace-toolbar are ONE horizontal
  // band across the pane divider. With --space-lg on the authoring side and
  // --space-sm on the artifact side, the divider sat off-centre in its own
  // row: 16px of clearance on its left, 8px on its right, measured at 1280.
  // Agreement with the toolbar, not a pinned literal — and agreement on what
  // the two sides RENDER, not on spelling: the toolbar names the gutter
  // through workspace.css's --artifact-gutter indirection while the header
  // names the spacing token directly.

  /** Resolve var() chains against every custom-property declaration in the
   *  barrel (tokens.css :root plus component-scoped properties such as
   *  --artifact-gutter). Cycles or unknown names throw rather than pass. */
  function resolveVars(value: string, depth = 0): string {
    if (depth > 8) throw new Error(`var() chain too deep for: ${value}`);
    const reference = /var\(\s*(--[\w-]+)\s*\)/.exec(value);
    if (reference === null) return value.trim();
    const declaration = new RegExp(
      `(?:^|[;{\\s])${reference[1]}\\s*:\\s*([^;}]+)`,
    ).exec(stripComments(appCss));
    if (declaration === null) {
      throw new Error(`${reference[1]} is not declared anywhere in the barrel`);
    }
    return resolveVars(
      value.replace(reference[0], declaration[1].trim()),
      depth + 1,
    );
  }

  it("gives both band halves the same rendered inline gutter", () => {
    const headerInline = declaredValue(".chat-panel-header", "padding").split(
      /\s+(?![^(]*\))/,
    )[1];
    const toolbarInline = declaredValue(
      ".artifact-workspace-toolbar",
      "padding",
    ).split(/\s+(?![^(]*\))/)[1];
    expect(resolveVars(headerInline)).toBe(resolveVars(toolbarInline));
  });
});

describe("one rule per in-bubble seam (elspeth-4dc660d56f)", () => {
  // The sources-created group is the audit-bearing part of a message. In a
  // sources-ONLY turn nothing else draws the seam, so the group must carry
  // the same border-top as its sibling .message-tools — the rule's own
  // comment promised that parallel for months while the declaration did not
  // exist, and the group abutted prose with 12px of undifferentiated gap.
  // When BOTH groups render, MessageBubble.tsx paints the
  // .message-group-separator ruler between them, and the group's own border
  // must come OFF or the one seam carries two rules.

  it("gives the sources group the same seam .message-tools draws", () => {
    // Equality with the sibling, not a pinned literal: if the divider weight
    // is ever retuned, both groups move together or this fails.
    expect(declaredValue(".message-sources-created", "border-top")).toBe(
      declaredValue(".message-tools", "border-top"),
    );
  });

  it("keeps the ruler's weight equal to the group seams it substitutes for", () => {
    expect(declaredValue(".message-group-separator", "border-top")).toBe(
      declaredValue(".message-tools", "border-top"),
    );
  });

  it("strips the group's own border when the ruler owns the seam", () => {
    // The elspeth-4dc660d56f exclusivity: ruler + border-top in the same
    // seam is a double rule. The adjacent-sibling rule already zeroes the
    // margin/padding pair; the border must be zeroed with them.
    const suppressed = declaredValue(
      ".message-group-separator + .message-sources-created",
      "border-top",
    );
    expect(px(suppressed)).toBe(0);
    expect(
      px(
        declaredValue(
          ".message-group-separator + .message-sources-created",
          "margin-top",
        ),
      ),
    ).toBe(0);
  });
});
