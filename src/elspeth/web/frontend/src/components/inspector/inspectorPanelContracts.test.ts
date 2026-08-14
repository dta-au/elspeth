import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Inspector-surface contracts pinned out of the shipped barrel
// (elspeth-b486ab259c, elspeth-859eae4480, elspeth-37cc8b5310,
// elspeth-6c3223c76e, elspeth-1f4b60a492).
//
// Each block resolves the WINNING declaration out of the barrel and checks
// the constraint that has to hold — never a raw-literal pin, per the
// chatBubbleGutter.test.ts idiom. vitest runs with `css: false`, so a
// rendered getComputedStyle probe would return "" for everything; what can
// be measured honestly is which declarations the stylesheets actually make
// and whether the values keep the invariants the tickets restored.
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
 * The LAST value declared for `property` by a rule listing `selector`
 * verbatim, in barrel order — i.e. the declaration that reaches the element
 * among equal-specificity rules naming it.
 */
function declaredValue(selector: string, property: string): string {
  const found: string[] = [];
  for (const rule of rules) {
    if (!rule.selectors.includes(selector)) continue;
    const match = new RegExp(`(?:^|;)\\s*${property}\\s*:([^;]+)`).exec(
      rule.declarations,
    );
    if (match) found.push(match[1].trim());
  }
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

describe("the YamlDisplay flex chain reaches the scroller (elspeth-b486ab259c)", () => {
  // `.yaml-view-display` used to be emitted with NO definition, so this
  // in-between box was a content-height block, `.yaml-view-content`'s
  // overflow rule was inert, and the scroll fell through to the workspace
  // panel — taking the Copy/Download toolbar and both export-scope notes
  // with it. Every link below is load-bearing: drop one and the chain
  // silently reverts to panel scroll.
  it("keeps every link of the chain: view → display → content", () => {
    expect(declaredValue(".yaml-view", "display")).toBe("flex");
    expect(declaredValue(".yaml-view", "flex-direction")).toBe("column");

    expect(declaredValue(".yaml-view-display", "display")).toBe("flex");
    expect(declaredValue(".yaml-view-display", "flex-direction")).toBe("column");
    expect(declaredValue(".yaml-view-display", "flex")).toBe("1");
    expect(declaredValue(".yaml-view-display", "min-height")).toBe("0");

    expect(declaredValue(".yaml-view-content", "flex")).toBe("1");
    expect(declaredValue(".yaml-view-content", "min-height")).toBe("0");
    expect(declaredValue(".yaml-view-content", "overflow")).toBe("auto");
  });

  it("pins the toolbar as a non-scrolling band", () => {
    expect(declaredValue(".yaml-view-toolbar", "flex-shrink")).toBe("0");
  });
});

describe("the canvas edge label respects the 12px type floor (elspeth-859eae4480)", () => {
  it("declares a size for the edge text at or above the floor", () => {
    // @xyflow's unthemed default is 10px; the theming block must keep an
    // explicit size. :root-prefixed selector so it beats the vendor's
    // bare-class rule on specificity, not bundle order.
    const declared = declaredValue(":root .react-flow__edge-text", "font-size");
    const match = /^var\(\s*(--[\w-]+)\s*\)$/.exec(declared);
    expect(match, `edge-text font-size must come from a token, got ${declared}`)
      .not.toBeNull();
    expect(tokenPx(match![1])).toBeGreaterThanOrEqual(12);
  });
});

describe("graph panel corners stay concentric with the node cards (elspeth-37cc8b5310)", () => {
  it("rounds the config panel at the card rank", () => {
    // The node cards read --radius-lg (GraphView.tsx node style); a panel
    // rounding SHARPER than its cards inverts the outer-≥-inner concentric
    // relationship. Same token on both surfaces, so a retune moves together.
    expect(declaredValue(".graph-config-panel", "border-radius")).toBe(
      "var(--radius-lg)",
    );
  });

  it("keeps the node card on the same token, not a raw literal", () => {
    const graphViewSource = readFileSync(
      "src/components/inspector/GraphView.tsx",
      "utf8",
    );
    // The literal 8 was invisible to the radius system — a retune of the
    // card rank could never reach the graph. Inline style is the only place
    // React Flow accepts node geometry, so the consumption check is a source
    // check, like reactFlowThemeScope.test.tsx's vendor-consumption pins.
    expect(graphViewSource).toContain('borderRadius: "var(--radius-lg)"');
    expect(graphViewSource).not.toMatch(/borderRadius:\s*\d/);
  });
});

describe("floating graph surfaces resolve elevation through the tiers (elspeth-6c3223c76e)", () => {
  it("keeps the a11y list on the popover tier", () => {
    // Mini floating panel = --shadow-popover's own definition. The raw
    // 0 4px 16px rgba(0,0,0,0.3) it carried was heavier than any named tier
    // and never softened in the light theme.
    expect(declaredValue(".graph-a11y-list:focus-within", "box-shadow")).toBe(
      "var(--shadow-popover)",
    );
  });
});

describe("the empty canvas centres its message in the void (elspeth-1f4b60a492)", () => {
  it("stretches the empty-state box to the panel and stacks lead over hint", () => {
    // The shared .empty-state register centres within its own box; without a
    // height the box hugged its single line at the top edge of a ~950px
    // panel. The base register supplies the centring; the graph modifier
    // supplies the height.
    expect(declaredValue(".empty-state", "align-items")).toBe("center");
    expect(declaredValue(".empty-state", "justify-content")).toBe("center");
    expect(declaredValue(".graph-view-empty", "height")).toBe("100%");
    expect(declaredValue(".graph-view-empty", "flex-direction")).toBe("column");
  });

  it("leads with the xl register over a muted guidance line", () => {
    expect(declaredValue(".graph-view-empty-title", "font-size")).toBe(
      "var(--font-size-xl)",
    );
    expect(declaredValue(".graph-view-empty-title", "color")).toBe(
      "var(--color-text)",
    );
    // The hint inherits .empty-state's muted colour — it must not override it.
    expect(declaredValue(".empty-state", "color")).toBe(
      "var(--color-text-muted)",
    );
  });
});
