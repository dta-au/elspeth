import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

// Cascade / arithmetic gates for the sidebar.css rules retuned by the
// 2026-08-14 professionalisation review (elspeth-eaa8c0dc73,
// elspeth-51358d56a3, elspeth-a422aa467c).
//
// These are deliberately NOT declaration-existence tests. A test asserting
// that some stylesheet contains the TEXT `var(--color-focus-ring)` proves
// nothing about which rule wins for the element, and the three defects fixed
// here were all "a correct-looking declaration on the wrong side of a
// comparison". Each assertion below either resolves the LAST declaration in
// barrel order for a selector (the one that reaches the element among rules
// that tie on specificity), or compares two token values that must be chosen
// together.
//
// The barrel is read the way styles/primitiveGeometry.test.ts and
// styles/buttonCascade.test.ts read it, so rule order here is real cascade
// order. cwd-relative per the tokenReferences.test.ts idiom — vitest runs
// from the frontend root.
const stylesheetBarrel = readFileSync("src/styles/index.css", "utf8");
const appCss = Array.from(
  stylesheetBarrel.matchAll(/@import\s+"(?<path>[^"]+)";/g),
)
  .map((match) => {
    const importPath = match.groups?.path;
    if (importPath === undefined) {
      throw new Error("styles/index.css import regex produced no path");
    }
    return readFileSync(resolve("src/styles", importPath), "utf8");
  })
  .join("\n");

function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

interface Rule {
  selectors: string[];
  declarations: string;
}

/** Flat rule list in barrel order (@media blocks land as their own entries). */
const appRules: Rule[] = Array.from(
  stripComments(appCss).matchAll(/([^{}]+)\{([^{}]*)\}/g),
).map((match) => ({
  selectors: match[1].split(",").map((selector) => selector.trim()),
  declarations: match[2],
}));

/**
 * The LAST value declared for `property` by a rule listing `selector`
 * verbatim, in barrel order — i.e. the declaration that reaches the element
 * among the equal-specificity rules for that selector.
 */
function winningValue(selector: string, property: string): string | null {
  let winner: string | null = null;
  for (const rule of appRules) {
    if (!rule.selectors.includes(selector)) continue;
    for (const declaration of rule.declarations.matchAll(
      /([\w-]+)\s*:\s*([^;]+)/g,
    )) {
      if (declaration[1].trim() === property) {
        winner = declaration[2].trim();
      }
    }
  }
  return winner;
}

/** Numeric px value of a token declared in tokens.css. */
function tokenPx(name: string): number {
  const declared = appCss.match(
    new RegExp(`${name}\\s*:\\s*(\\d+(?:\\.\\d+)?)px`),
  );
  if (declared === null) {
    throw new Error(`${name} is not declared as a px value in the barrel`);
  }
  return Number(declared[1]);
}

const sidebarCss = stripComments(
  readFileSync("src/components/sidebar/sidebar.css", "utf8"),
);

describe("sidebar focus rings (elspeth-eaa8c0dc73)", () => {
  // --color-accent (#1a7a52) on the menu surface (#122f37) computes to
  // 2.65:1 — below the WCAG 1.4.11 3:1 non-text floor — while the sibling
  // control in the same two-item popover got the ~14:1 --color-focus-ring.
  // --color-info was a third ring colour again.
  it.each([
    [".side-rail-catalog-btn:focus-visible", "2px"],
    [".side-rail-suggestion-header:focus-visible", "-2px"],
  ])("%s rings with the focus-ring token", (selector, offset) => {
    expect(winningValue(selector, "outline")).toBe(
      "2px solid var(--color-focus-ring)",
    );
    // Each rule keeps its own offset: the catalog button rings outside, the
    // clipped suggestion header rings inside.
    expect(winningValue(selector, "outline-offset")).toBe(offset);
  });

  it("uses no other colour for any focus ring declared in sidebar.css", () => {
    const ringColours = Array.from(
      sidebarCss.matchAll(/:focus-visible\s*\{([^{}]*)\}/g),
    )
      .flatMap((block) => Array.from(block[1].matchAll(/outline:\s*([^;]+)/g)))
      .map((declaration) => declaration[1].trim());

    expect(ringColours.length).toBeGreaterThan(0);
    for (const ring of ringColours) {
      expect(ring).toContain("var(--color-focus-ring)");
    }
  });
});

describe("import-yaml form measure (elspeth-51358d56a3)", () => {
  // The frame is .yaml-modal { position: fixed; inset: 32px }, so at a 1920px
  // viewport the sheet is 1856px wide. Without a cap the three-field paste
  // form got an 1856px monospace textarea and stranded Cancel/Import ~1800px
  // from the field they act on.
  const frameInset = 32;
  const frameWidthAt1920 = 1920 - 2 * frameInset;

  it("caps the form well inside its full-bleed frame and centres it", () => {
    const maxWidth = winningValue(".import-yaml-body", "max-width");
    expect(maxWidth).not.toBeNull();
    const cap = Number(/^(\d+)px$/.exec(maxWidth!)?.[1]);
    expect(Number.isFinite(cap)).toBe(true);
    expect(cap).toBeLessThan(frameWidthAt1920 / 2);
    expect(winningValue(".import-yaml-body", "margin-inline")).toBe("auto");
  });

  it("leaves .yaml-modal itself full-bleed for the wide YAML viewer", () => {
    expect(winningValue(".yaml-modal", "max-width")).toBeNull();
    expect(winningValue(".yaml-modal", "inset")).toBe(`${frameInset}px`);
  });
});

describe("run-disclosure type scale (elspeth-a422aa467c)", () => {
  // The confirm dialog's lead copy is --font-size-base. The disclosure body
  // sat at --font-size-sm, skipping --font-size-md entirely — a two-rung drop
  // that read as fine print beside the sentence introducing it.
  const scale = ["--font-size-xs", "--font-size-sm", "--font-size-md", "--font-size-base"];

  function rungOf(selector: string): number {
    const value = winningValue(selector, "font-size");
    const token = /^var\((--[\w-]+)\)$/.exec(value ?? "")?.[1];
    const rung = scale.indexOf(token ?? "");
    expect(
      rung,
      `${selector} resolves font-size to ${String(value)}, which is not on the ` +
        `sampled scale ${scale.join(" < ")}`,
    ).toBeGreaterThanOrEqual(0);
    return rung;
  }

  it("keeps the scale monotonic, so a rung comparison means something", () => {
    const sizes = scale.map(tokenPx);
    expect(sizes).toEqual([...sizes].sort((a, b) => a - b));
  });

  it.each([".run-disclosure-summary", ".run-disclosure-opt-out"])(
    "%s sits exactly one rung below the dialog's lead copy",
    (selector) => {
      expect(rungOf(".confirm-dialog-message") - rungOf(selector)).toBe(1);
    },
  );
});
