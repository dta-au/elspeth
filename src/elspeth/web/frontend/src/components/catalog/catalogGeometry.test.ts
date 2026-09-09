import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Geometry and register invariants for the catalog drawer, from the
// 2026-08-14 professionalisation review (elspeth-56e9c8e3b2,
// elspeth-05bb15df70, elspeth-6ee56bb5f5, elspeth-c33d029f96,
// elspeth-112aa6c484, elspeth-453d9756be, elspeth-64ca50785d,
// elspeth-806c5f79ec, elspeth-c4111235e9, elspeth-9ae8ee9ac2).
//
// These are deliberately NOT declaration-existence tests. Each one either
// resolves a CASCADE question (which declaration reaches the element), an
// ARITHMETIC one (do two values that were chosen together still agree), or —
// for the divider reset — a REACH one, by running the shipped selector against
// the DOM CatalogDrawer actually renders. Asserting that a rule body contains
// a literal would pin the literal rather than the constraint it was chosen
// for, and would be blind to later rules and @media branches.
//
// Barrel-loaded in cascade order, exactly like styles/primitiveGeometry.test.ts
// and chat/chatBubbleGutter.test.ts. Import paths resolve against the BARREL's
// directory; cwd-relative per the tokenReferences.test.ts idiom (vitest runs
// from the frontend root).
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
 * in barrel order. @media-nested rules are captured as their own entries, so a
 * branch that only exists inside a media query is inspected too.
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
 * The LAST value declared for `property` by a rule listing `selector` verbatim
 * — i.e. which declaration reaches the element among the equal-specificity
 * rules that name it.
 */
function declaredValue(selector: string, property: string): string {
  const found = declaredValues(selector, property);
  if (found.length === 0) {
    throw new Error(`No ${property} declared for ${selector}`);
  }
  return found[found.length - 1];
}

/**
 * px value of every custom property the barrel defines as a bare px length.
 * Component-local properties (e.g. --catalog-search-clear-size, declared on
 * .catalog-search-container) resolve here too, so a length written against one
 * is checked, not skipped. A name defined at two DIFFERENT px values throws
 * rather than silently resolving to whichever came last.
 */
const pxTokens = new Map<string, Set<number>>();
for (const match of stripComments(appCss).matchAll(
  /(--[\w-]+)\s*:\s*(-?[\d.]+)px\s*;/g,
)) {
  const [, name, value] = match;
  const seen = pxTokens.get(name) ?? new Set<number>();
  seen.add(Number.parseFloat(value));
  pxTokens.set(name, seen);
}

function tokenPx(name: string): number {
  const values = pxTokens.get(name);
  if (values === undefined) {
    throw new Error(`No px value for ${name} in the stylesheet barrel`);
  }
  if (values.size > 1) {
    // e.g. --size-workspace-band, which a density media query overrides.
    // Resolving one of several branches would be a guess, so refuse.
    throw new Error(
      `${name} is defined at ${[...values].join("px, ")}px — it has no single value`,
    );
  }
  return [...values][0];
}

/** Resolve a length written as a bare px value or a var() over a px token. */
function px(value: string): number {
  const substituted = value
    .replace(/var\(\s*(--[\w-]+)\s*\)/g, (_, name: string) => `${tokenPx(name)}`)
    .replace(/px/g, "")
    .trim();
  if (!/^-?[\d.]+$/.test(substituted)) {
    throw new Error(`Unsupported length expression: ${value}`);
  }
  return Number.parseFloat(substituted);
}

describe("filter strip is one control band (elspeth-05bb15df70)", () => {
  it("clears filters at the same height as the chips it clears", () => {
    // .filter-chip-clear shipped at --size-control-compact (36px) inside a
    // 28px chip band: the only control in the strip 8px taller than
    // everything around it. Compares the two SHIPPED values rather than
    // pinning 28px, so a legitimate retune of the band moves both together
    // or fails here.
    expect(px(declaredValue(".filter-chip-clear", "min-height"))).toBe(
      px(declaredValue(".filter-chip", "min-height")),
    );
  });

  it("keeps the whole band above the WCAG 2.5.8 AA target floor", () => {
    for (const selector of [".filter-chip", ".filter-chip-clear"]) {
      expect(px(declaredValue(selector, "min-height"))).toBeGreaterThanOrEqual(24);
    }
  });
});

describe("catalog search gutter clears its own control (elspeth-c4111235e9)", () => {
  // The input's right padding exists ONLY to keep its text out from under the
  // clear button overlaid on it. Both were bare 28px literals in two rules, so
  // resizing the control decoupled them silently.
  const inlineEnd = declaredValue(".catalog-search-input", "padding").split(/\s+/)[1];

  it("takes the gutter and the control's box from one declaration", () => {
    expect(px(inlineEnd)).toBe(px(declaredValue(".catalog-search-clear", "min-width")));
    expect(px(inlineEnd)).toBe(px(declaredValue(".catalog-search-clear", "min-height")));
  });

  it("resolves the shared size against a property the control inherits", () => {
    // .catalog-search-clear and .catalog-search-input are both children of
    // .catalog-search-container, which is where the property is declared. If
    // it moved off a common ancestor the var() would resolve to nothing and
    // min-width would silently drop below the 24x24 hit-target floor.
    expect(declaredValue(".catalog-search-container", "--catalog-search-clear-size")).toBe("28px");
    expect(px(declaredValue(".catalog-search-clear", "min-width"))).toBeGreaterThanOrEqual(24);
  });
});

describe("discriminated-schema variants sit in the card register (elspeth-56e9c8e3b2)", () => {
  it("gives the variant label the card's type size, not the body inherit", () => {
    // The three variant class names were used by PluginCard.tsx and defined
    // NOWHERE, so the labels inherited body/16px inside a 12px card.
    const label = px(declaredValue(".plugin-card-variant-label", "font-size"));
    expect(label).toBe(px(declaredValue(".plugin-card-desc", "font-size")));
    expect(
      label,
      "a label larger than the card's own title register is the defect",
    ).toBeLessThan(px(declaredValue(".plugin-card-name", "font-size")));
    expect(label).toBeLessThan(tokenPx("--font-size-base"));
  });

  it("separates the variant blocks from each other and from the hint", () => {
    expect(px(declaredValue(".plugin-card-variants", "gap"))).toBeGreaterThan(0);
    // The hint's old margin-bottom stacked on top of the grid gap once the
    // container became a grid; the gap is now the single spacing decision.
    expect(declaredValues(".plugin-card-variants-hint", "margin-bottom")).toEqual([]);
  });
});

describe("plugin-card detail sections are built the same way (elspeth-56e9c8e3b2)", () => {
  it("spaces label from body by the section grid, in BOTH sections", () => {
    // .plugin-card-example was a class name with no rule behind it and its
    // spacing lived on the <pre>'s own margin instead — the same shape as the
    // variant classes, one section over.
    const sectionRules = rules.filter((rule) =>
      rule.selectors.includes(".plugin-card-example"),
    );
    expect(sectionRules).toHaveLength(1);
    expect(sectionRules[0].selectors).toEqual([
      ".plugin-card-prose-section",
      ".plugin-card-example",
    ]);
    expect(px(declaredValue(".plugin-card-example", "gap"))).toBeGreaterThan(0);
    expect(
      declaredValue(".plugin-card-example-code", "margin"),
      "a margin on the <pre> would stack a second, different gap on the grid's",
    ).toBe("0");
  });
});

describe("catalog count badge keeps the house badge silhouette (elspeth-6ee56bb5f5)", () => {
  it("rounds at the same radius as the shared badge primitives", () => {
    const count = declaredValue(".catalog-tab-count", "border-radius");
    expect(count).toBe(declaredValue(".type-badge", "border-radius"));
    expect(px(count)).toBeLessThan(tokenPx("--radius-lg"));
  });
});

describe("catalog list states share one slot geometry (elspeth-c33d029f96)", () => {
  // Loading / empty / load-failure re-laid-out the same slot on every
  // transition: two paddings and three alignments between them.
  it("puts every state on the shared .empty-state register", () => {
    expect(declaredValue(".catalog-status-message", "padding")).toBe(
      declaredValue(".empty-state", "padding"),
    );
    expect(declaredValue(".catalog-status-message", "font-size")).toBe(
      declaredValue(".empty-state", "font-size"),
    );
    expect(px(declaredValue(".catalog-status-message", "font-size"))).toBeGreaterThanOrEqual(
      tokenPx("--font-size-xs"),
    );
  });

  it("lets no modifier move the slot", () => {
    // The error and empty modifiers must not redeclare geometry — only
    // colour. A modifier that re-set padding or alignment would put the
    // layout jump straight back.
    for (const modifier of [
      ".catalog-status-message--error",
      ".catalog-status-message--center",
    ]) {
      expect(declaredValues(modifier, "padding")).toEqual([]);
      expect(declaredValues(modifier, "font-size")).toEqual([]);
    }
    // --center may only restate the base's alignment, never contradict it.
    for (const value of declaredValues(".catalog-status-message--center", "text-align")) {
      expect(value).toBe(declaredValue(".catalog-status-message", "text-align"));
    }
  });
});

describe("plugin list ends without a dangling divider (elspeth-112aa6c484)", () => {
  // A REACH test, not an existence one: each card is wrapped in its own
  // role="listitem", so the obvious `.plugin-card:last-child` matches EVERY
  // card and would delete all the dividers. Run the shipped selector against
  // the DOM CatalogDrawer renders and count what it hits.
  function mountList(cardCount: number): HTMLElement[] {
    document.body.innerHTML = "";
    const list = document.createElement("div");
    list.setAttribute("role", "list");
    list.className = "catalog-plugin-list";
    const cards: HTMLElement[] = [];
    for (let i = 0; i < cardCount; i += 1) {
      const item = document.createElement("div");
      item.setAttribute("role", "listitem");
      const card = document.createElement("div");
      card.className = "plugin-card";
      item.appendChild(card);
      list.appendChild(item);
      cards.push(card);
    }
    document.body.appendChild(list);
    return cards;
  }

  const resetSelectors = rules
    .filter(
      (rule) =>
        /(?:^|;)\s*border-bottom\s*:\s*none/.test(rule.declarations) &&
        rule.selectors.some((selector) => selector.includes(".plugin-card")),
    )
    .flatMap((rule) => rule.selectors)
    .filter((selector) => selector.includes(".plugin-card"));

  it("declares exactly one divider reset for the card list", () => {
    expect(resetSelectors).toHaveLength(1);
    expect(declaredValue(".plugin-card", "border-bottom")).toBe(
      "1px solid var(--color-border)",
    );
  });

  it("resets the LAST card's divider and no other", () => {
    const cards = mountList(3);
    const matched = [...document.querySelectorAll<HTMLElement>(resetSelectors[0])];
    expect(
      matched,
      "the naive .plugin-card:last-child form matches every card through the " +
        'role="listitem" wrapper and would delete all three dividers',
    ).toEqual([cards[2]]);
  });

  it("still resets the only card when the list holds one", () => {
    const cards = mountList(1);
    expect([...document.querySelectorAll<HTMLElement>(resetSelectors[0])]).toEqual([cards[0]]);
  });
});

describe("the drawer's type slot has one treatment (elspeth-64ca50785d)", () => {
  it("styles the plugin cards' kind label and the inline entry's badge from one rule", () => {
    const slotRules = rules.filter((rule) =>
      rule.selectors.some(
        (selector) =>
          selector === ".plugin-card-kind" ||
          selector === ".inline-chat-source-entry-badge",
      ),
    );
    expect(
      slotRules,
      "two rules means the two slots can drift apart again",
    ).toHaveLength(1);
    expect(slotRules[0].selectors).toEqual([
      ".plugin-card-kind",
      ".inline-chat-source-entry-badge",
    ]);
    // Both slots must be BORDERED — the original defect was a bordered badge
    // next to bare text in the same position.
    expect(slotRules[0].declarations).toMatch(/border\s*:\s*1px solid/);
  });
});

describe("catalog type weights resolve to faces Inter ships (elspeth-806c5f79ec)", () => {
  // Derived from the @fontsource imports rather than hardcoded: fonts.css is
  // loaded by main.tsx, not the styles barrel.
  const interWeights = new Set(
    Array.from(
      readFileSync(join(barrelDir, "fonts.css"), "utf8").matchAll(
        /@fontsource\/inter\/latin-(\d+)\.css/g,
      ),
    ).map((match) => Number.parseInt(match[1], 10)),
  );

  function weight(selector: string): number {
    const declared = declaredValue(selector, "font-weight");
    const name = /var\(\s*(--[\w-]+)\s*\)/.exec(declared);
    if (name === null) return Number.parseInt(declared, 10);
    const definition = new RegExp(`${name[1]}\\s*:\\s*(\\d+)\\s*;`).exec(
      stripComments(appCss),
    );
    if (definition === null) throw new Error(`No numeric value for ${name[1]}`);
    return Number.parseInt(definition[1], 10);
  }

  it("keeps the audit chips BELOW the card title they sit under", () => {
    // The chips shipped at 650. Inter has no 650 face, so font matching
    // resolved it to 700 — exactly as bold as .plugin-card-name — and the
    // hierarchy the value was chosen to create never rendered.
    expect(interWeights.size).toBeGreaterThan(0);
    expect(interWeights.has(weight(".audit-icon"))).toBe(true);
    expect(weight(".audit-icon")).toBeLessThan(weight(".plugin-card-name"));
  });

  it("takes every catalog weight from the tokens, so none can go off-face", () => {
    // Scoped to catalog.css, the file this lane owns — a raw literal anywhere
    // else in the barrel is that file's lane to close. A token reference
    // cannot name a weight Inter does not ship, because every
    // --font-weight-* token resolves to one of these faces.
    const catalogRules = Array.from(
      stripComments(readFileSync("src/components/catalog/catalog.css", "utf8")).matchAll(
        /([^{}]+)\{([^{}]*)\}/g,
      ),
    ).map((match) => ({
      selectors: match[1].split(",").map((selector) => selector.trim()),
      declarations: match[2],
    }));
    expect(catalogRules.length).toBeGreaterThan(0);

    const offScale: string[] = [];
    for (const rule of catalogRules) {
      const declared = /(?:^|;)\s*font-weight\s*:\s*([^;]+)/.exec(rule.declarations);
      if (declared === null) continue;
      const value = declared[1].trim();
      const name = /^var\(\s*(--font-weight-[\w-]+)\s*\)$/.exec(value);
      if (name === null || !interWeights.has(weight(rule.selectors[0]))) {
        offScale.push(`${rule.selectors.join(", ")} → ${value}`);
      }
    }
    expect(
      offScale,
      "catalog.css must take font-weight from --font-weight-*; a raw literal " +
        "can name a face Inter does not ship (650 resolved UP to 700)",
    ).toEqual([]);
  });
});

describe("catalog chrome spacing and elevation", () => {
  it("guarantees a gutter on the longest shortcut rows (elspeth-453d9756be)", () => {
    // space-between distributes FREE space; on the longest rows there is none
    // left, so without a gap the keycap chip meets its description.
    expect(declaredValue(".shortcuts-list-item", "justify-content")).toBe("space-between");
    expect(px(declaredValue(".shortcuts-list-item", "gap"))).toBeGreaterThan(0);
  });

  it("lifts the blocking drawer off the surface it blocks (elspeth-9ae8ee9ac2)", () => {
    // aria-modal="true" + useFocusTrap, but no elevation at all: flush
    // against the page it blocks, separated only by a hairline.
    expect(declaredValue(".catalog-drawer", "box-shadow")).toBe("var(--shadow-modal)");
  });
});
