import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Stylesheet invariants for the audit-readiness chrome
// (elspeth-7fb1633b86, elspeth-459950731c, elspeth-ac51d2dac2).
//
// These are deliberately NOT declaration-existence tests. Each one resolves
// the winning declaration out of the cascade barrel, substitutes the shipped
// token values, and checks a relationship that has to hold — an equality
// between two surfaces, or a family/box constraint. Pinning the literals
// instead ("padding: 2px 4px;") would lock in the very numbers a later
// retune is allowed to change, and would be blind to any @media branch.
//
// Barrel-loaded in cascade order, exactly like styles/primitiveGeometry.test.ts
// and chat/chatBubbleGutter.test.ts. Import paths resolve against the BARREL's
// directory, cwd-relative per the tokenReferences.test.ts idiom; vitest runs
// from the frontend root.
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
 * in barrel order. @media-nested rules flatten into the same list (the parse
 * captures them as their own entries), so a branch that only exists inside a
 * media query is inspected too.
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

/** The LAST value declared for `property` by a rule naming `selector`. */
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

/** Resolve a bare px length or a var() over a px token. */
function px(value: string): number {
  const substituted = value
    .replace(/var\(\s*(--[\w-]+)\s*\)/g, (_, name: string) => `${tokenPx(name)}`)
    .trim();
  const parsed = Number.parseFloat(substituted);
  if (Number.isNaN(parsed) || !/^-?[\d.]+(px)?$/.test(substituted)) {
    throw new Error(`Unsupported length expression: ${value}`);
  }
  return parsed;
}

/** The inline-axis term of a one- or two-value padding shorthand. */
function inlinePadding(selector: string): number {
  const shorthand = declaredValue(selector, "padding").split(/\s+/);
  expect(
    shorthand.length,
    `${selector}'s padding shorthand must stay one- or two-valued`,
  ).toBeLessThanOrEqual(2);
  return px(shorthand[shorthand.length - 1]);
}

describe("the gate chip is one box in two states (elspeth-7fb1633b86)", () => {
  // "Blocks run" and "Advisory" are the two values of ONE binary field. They
  // shipped as different-sized boxes: the border and its 2px of geometry lived
  // on `--blocks` alone and `--advisory` had no rule anywhere in the tree, so
  // the pair rendered as a bordered chip beside a bare run of muted text.

  const base = ".audit-readiness-row-gate";
  const blocks = ".audit-readiness-row-gate--blocks";
  const advisory = ".audit-readiness-row-gate--advisory";

  it("declares the box once, on the base rule, so both variants inherit it", () => {
    // The load-bearing assertion: neither variant may re-declare a property
    // that changes its outer box. A variant that sets its own padding or
    // border-width is exactly the regression this ticket describes.
    for (const variant of [blocks, advisory]) {
      for (const boxProperty of ["padding", "border", "border-width"]) {
        expect(
          declaredValues(variant, boxProperty),
          `${variant} must not re-declare ${boxProperty} — the box belongs to ${base}`,
        ).toEqual([]);
      }
    }
    expect(declaredValues(base, "border").length).toBeGreaterThan(0);
    expect(declaredValues(base, "padding").length).toBeGreaterThan(0);
  });

  it("gives both variants an edge, so neither reads as loose text", () => {
    // Before: only --blocks had a border-color, so an advisory row's
    // classification floated in the label with no mark of its own.
    //
    // This is STRICTER than the ticket, which asked only that the two
    // variants share a box — a transparent border on the base rule would
    // satisfy that while leaving the advisory value as bare muted text. The
    // extra constraint is a design call: four of six rows are advisory on a
    // typical snapshot, and a panel where some classifications are chips and
    // the rest are loose words does not read as one binary field. Relax this
    // assertion, not the box assertion above, if that call is revisited.
    for (const variant of [blocks, advisory]) {
      expect(
        declaredValue(variant, "border-color"),
        `${variant} must colour the shared edge`,
      ).toMatch(/^var\(--color-/);
    }
    expect(
      declaredValue(blocks, "border-color"),
      "the gating variant must be the louder of the two",
    ).not.toBe(declaredValue(advisory, "border-color"));
  });

  it("gives the rounded corner real geometry to act on", () => {
    // A 1px border drawn hard against the text's own line box left the radius
    // with nothing to round. The chip's vertical padding must at least reach
    // the corner radius, or the curve is clipped by the glyphs.
    const [blockPadding] = declaredValue(base, "padding").split(/\s+/);
    const borderWidth = px(declaredValue(base, "border").split(/\s+/)[0]);
    expect(px(blockPadding)).toBeGreaterThan(0);
    expect(px(blockPadding) + borderWidth).toBeGreaterThanOrEqual(
      tokenPx("--radius-sm") / 2,
    );
  });

  it("stays inside the chip rung it borrows from .status-badge", () => {
    // The house chip (shared.css .status-badge) is the reference box. This one
    // is deliberately quieter, so it may be tighter, but never LOOSER — a
    // third chip size is the eleventh rung the system does not want.
    expect(inlinePadding(base)).toBeLessThanOrEqual(inlinePadding(".status-badge"));
    expect(declaredValue(base, "font-size")).toBe(
      declaredValue(".status-badge", "font-size"),
    );
  });
});

describe("the readiness-row drawer has one left edge (elspeth-459950731c)", () => {
  // `.readiness-row-detail-findings` / `-raw` / `-raw-text` shipped as class
  // names with no rule anywhere, so the findings <ul> kept the UA
  // `padding-inline-start: 40px` and its disc markers ON TOP of each item's
  // own --space-md inset: finding text started 52px into a 420px drawer while
  // the summary above it started at 12px, and the <details> sat flush at 0.

  it("defines every class the drawer renders", () => {
    for (const selector of [
      ".readiness-row-detail-findings",
      ".readiness-row-detail-raw",
      ".readiness-row-detail-raw-text",
    ]) {
      expect(
        rules.some((rule) => rule.selectors.includes(selector)),
        `${selector} is rendered by ReadinessRowDetail.tsx but styled nowhere`,
      ).toBe(true);
    }
  });

  it("aligns the findings' text edge to the summary's, whatever that inset becomes", () => {
    // The constraint, not the 12px literal: a finding <li> carries the
    // -body class, so its own inset already matches the summary. The list
    // wrapper must therefore contribute ZERO — any inline padding it keeps
    // (including the 40px the UA supplies when the rule is absent) pushes
    // the findings off the drawer's single left edge.
    expect(inlinePadding(".readiness-row-detail-findings")).toBe(0);
    expect(inlinePadding(".readiness-row-detail-raw")).toBe(
      inlinePadding(".readiness-row-detail-summary"),
    );
  });

  it("drops the UA disc markers the product uses nowhere else", () => {
    expect(declaredValue(".readiness-row-detail-findings", "list-style")).toBe("none");
    // The sibling list in the same drawer already resets the same way; if one
    // is bulleted and the other is not, the drawer reads as pasted-in HTML.
    expect(declaredValue(".readiness-row-detail-components-list", "list-style")).toBe(
      "none",
    );
  });
});

describe("the Explain dialog sets prose as prose (elspeth-ac51d2dac2)", () => {
  const narrative = ".explain-dialog-narrative";

  it("drops the monospace face the producer's output never justified", () => {
    // explain.py emits hyphen-bulleted English sentences — no code, no
    // hashes, no column alignment. base.css sets a bare <pre> mono, so this
    // rule has to restate the family to win.
    expect(declaredValue(narrative, "font-family")).toBe("var(--font-sans)");
    expect(declaredValues(narrative, "font-family")).not.toContain("var(--font-mono)");
  });

  it("caps the measure inside a readable prose band", () => {
    // The dialog is `inset: 32px` — full-bleed — so an uncapped column ran to
    // roughly 150 characters, about twice the comfortable measure, and the
    // eye lost its line on every return sweep.
    //
    // The band, not a literal and not another lane's number: an earlier draft
    // asserted equality with .message-bubble-content's cap, which would have
    // reddened this lane the moment the chat lane retuned its bubbles. What
    // has to hold is that the cap exists, is expressed in ch (so it tracks
    // the font rather than the viewport), and lands in the readable range —
    // Bringhurst's 66 sits mid-band; the shipped value is the 74ch the chat
    // bubbles independently settled on.
    const cap = declaredValue(narrative, "max-width");
    expect(cap, "the measure must be capped in ch, not px or %").toMatch(/^\d+ch$/);
    const columns = Number.parseInt(cap, 10);
    expect(columns).toBeGreaterThanOrEqual(45);
    expect(columns).toBeLessThanOrEqual(80);
  });

  it("keeps the narrative at or above the drawer's body size", () => {
    // Prose the user is expected to READ, in a modal that fills the viewport.
    expect(tokenPx(declaredValue(narrative, "font-size").replace(/var\(|\)/g, ""))).
      toBeGreaterThanOrEqual(
        tokenPx(
          declaredValue(".readiness-row-detail-summary", "font-size").replace(
            /var\(|\)/g,
            "",
          ),
        ),
      );
  });
});
