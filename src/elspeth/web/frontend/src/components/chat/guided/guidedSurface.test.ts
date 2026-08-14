import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

// Guided-surface invariants from the 2026-08-14 professionalisation review.
//
// These are deliberately NOT declaration-existence tests. Each one either
// resolves a CASCADE question (does the winning rule for this element actually
// carry the declaration?) or an AGREEMENT one (do two values that must be
// chosen together really match?) — the two failure modes the review kept
// finding, where a stylesheet held a correct-looking declaration that never
// reached the element, or that silently contradicted its neighbour.
//
// cwd-relative per the tokenReferences.test.ts idiom; vitest runs from the
// frontend root.
const guidedCss = readFileSync("src/components/chat/guided/guided.css", "utf8");
const chatCss = readFileSync("src/components/chat/chat.css", "utf8");
const sharedCss = readFileSync("src/styles/shared.css", "utf8");
const tokensCss = readFileSync("src/styles/tokens.css", "utf8");

function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

interface Rule {
  /** Comma-separated selector list, split and trimmed. */
  selectors: string[];
  /** Raw declaration block. */
  declarations: string;
  /** Position in source order — a larger index wins an equal-specificity tie. */
  index: number;
}

/**
 * Flat rule list in source order. Rules nested in @media are captured as their
 * own entries (the at-rule prelude never matches, because a declaration block
 * containing `{` cannot satisfy `[^{}]*`) — the technique buttonCascade.test.ts
 * and primitiveGeometry.test.ts both use.
 */
function parseRules(css: string): Rule[] {
  const rules: Rule[] = [];
  for (const match of stripComments(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    rules.push({
      selectors: match[1].split(",").map((selector) => selector.trim()),
      declarations: match[2],
      index: rules.length,
    });
  }
  return rules;
}

const guidedRules = parseRules(guidedCss);

function declaredValue(rule: Rule, property: string): string | null {
  const declaration = new RegExp(
    `(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`,
    "s",
  ).exec(rule.declarations);
  return declaration ? declaration[1].trim().replace(/\s+/g, " ") : null;
}

function valueIn(css: string, selector: string, property: string): string | null {
  let winner: string | null = null;
  for (const rule of parseRules(css)) {
    if (!rule.selectors.includes(selector)) continue;
    const value = declaredValue(rule, property);
    if (value !== null) winner = value;
  }
  return winner;
}

/**
 * Class-level specificity (the "B" of (A,B,C)) for the flat selectors this file
 * asserts on: `:where(...)` contributes zero, `:not(<simple>)` contributes its
 * argument. Same computation as styles/buttonCascade.test.ts.
 */
function classLevelSpecificity(selector: string): number {
  let s = selector.replace(/:where\((?:[^()]|\([^()]*\))*\)/g, "");
  s = s.replace(/:not\(([^)]*)\)/g, "$1");
  const classes = s.match(/\.[\w-]+/g) ?? [];
  const attributes = s.match(/\[[^\]]+\]/g) ?? [];
  const pseudoClasses = s.match(/(?<!:):[\w-]+/g) ?? [];
  return classes.length + attributes.length + pseudoClasses.length;
}

/** Rules (with the matching selector) whose selector list satisfies `predicate`. */
function rulesMatching(
  predicate: (selector: string) => boolean,
): { rule: Rule; selector: string }[] {
  const found: { rule: Rule; selector: string }[] = [];
  for (const rule of guidedRules) {
    const selector = rule.selectors.find(predicate);
    if (selector !== undefined) found.push({ rule, selector });
  }
  return found;
}

const tokensRoot = (() => {
  const root = /:root\s*\{([\s\S]*?)\n\}/.exec(stripComments(tokensCss));
  if (root === null) throw new Error("tokens.css has no :root block");
  return root[1];
})();

/** The :root value of a design token, e.g. "--space-md" -> "12px". */
function rootValue(name: string): string {
  const declared = new RegExp(`\\n\\s*${name}\\s*:\\s*([^;]+);`).exec(tokensRoot);
  expect(declared, `${name} is not declared in tokens.css :root`).not.toBeNull();
  return declared![1].trim();
}

/**
 * Substitute every `var(--token)` in a declared value with its :root value, so
 * two declarations can be compared for what they RENDER rather than for how
 * they happen to be spelled (one site may name the token, another the literal).
 */
function resolveTokens(value: string): string {
  return value.replace(/var\((--[\w-]+)\)/g, (_, name: string) => rootValue(name));
}

/** px value of a `var(--token)` reference, resolved from tokens.css :root. */
function tokenPx(reference: string): number {
  const resolved = resolveTokens(reference.trim());
  const px = /^(\d+(?:\.\d+)?)px$/.exec(resolved);
  expect(px, `"${reference}" does not resolve to a px token (got ${resolved})`).not.toBeNull();
  return Number(px![1]);
}

describe("guided decision eyebrow typography (elspeth-aa1e0f4a96)", () => {
  // The eyebrow is a <p> INSIDE .guided-current-decision-copy, so the sibling
  // group rule `.guided-current-decision-copy p` (0,1,1) beat a bare
  // `.guided-current-decision-eyebrow` (0,1,0) on every property it declares.
  // A font-size written on the bare class is therefore silently overridden —
  // which is why this asserts the cascade, not the declaration.
  const eyebrowRules = rulesMatching((selector) =>
    selector.includes("guided-current-decision-eyebrow"),
  );
  const paragraphGroup = ".guided-current-decision-copy p";

  it("gives the eyebrow rule enough specificity to beat the sibling <p> group rule", () => {
    const sizing = eyebrowRules.filter(
      ({ rule }) => declaredValue(rule, "font-size") !== null,
    );
    expect(
      sizing.length,
      "exactly one rule in guided.css should size the eyebrow",
    ).toBe(1);

    const groupRule = guidedRules.find((rule) =>
      rule.selectors.includes(paragraphGroup),
    );
    expect(groupRule, `no rule found for ${paragraphGroup}`).toBeDefined();
    expect(declaredValue(groupRule!, "font-size")).not.toBeNull();

    // The group rule is later in source order, so a tie loses. The eyebrow rule
    // must win on specificity outright.
    expect(sizing[0].rule.index).toBeLessThan(groupRule!.index);
    expect(
      classLevelSpecificity(sizing[0].selector),
      `"${sizing[0].selector}" must out-specify "${paragraphGroup}"`,
    ).toBeGreaterThan(classLevelSpecificity(paragraphGroup));
  });

  it("puts the eyebrow on the same uppercase micro-label idiom as the stage divider", () => {
    // .bubble-system--stage is the other uppercase micro-label on this very
    // surface — the two can sit ~114px apart — and every uppercase micro-label
    // in the frontend uses the same rung. Assert they AGREE rather than
    // asserting a literal, so a future retune moves both or fails here.
    const eyebrow = eyebrowRules.find(
      ({ rule }) => declaredValue(rule, "font-size") !== null,
    )!.rule;
    for (const property of ["font-size", "font-weight", "letter-spacing"]) {
      // Resolved, not literal: the divider spells its weight as 700 and the
      // eyebrow names the token, and it is the rendered value that must agree.
      expect(
        resolveTokens(declaredValue(eyebrow, property)!),
        `eyebrow ${property} must match .bubble-system--stage`,
      ).toBe(
        resolveTokens(valueIn(guidedCss, ".bubble-system--stage", property)!),
      );
    }
    expect(declaredValue(eyebrow, "font-size")).toBe("var(--font-size-xs)");
  });
});

describe("guided option chip states (elspeth-bf28ffcdfa, elspeth-64c97a2d72)", () => {
  const hoverRules = rulesMatching(
    (selector) =>
      selector.startsWith(".guided-chip-btn") && selector.includes(":hover"),
  );

  it("guards every chip hover against the disabled state, at zero specificity cost", () => {
    expect(hoverRules.length).toBeGreaterThan(0);
    for (const { selector } of hoverRules) {
      expect(selector, `${selector} must not light up a dead control`).toContain(
        ":where(:not(:disabled))",
      );
      expect(selector).toContain(':where(:not([aria-disabled="true"]))');
    }
  });

  it("keeps the base chip hover below the pressed chip hover", () => {
    // Same constraint shared.css pins for .btn: the guards live inside
    // :where() so the base hover cannot outrank the variant hover and repaint
    // a selected chip.
    const base = hoverRules.find(({ selector }) =>
      selector.startsWith(".guided-chip-btn:hover"),
    );
    const pressed = hoverRules.find(({ selector }) =>
      selector.startsWith('.guided-chip-btn[aria-pressed="true"]:hover'),
    );
    expect(base, "no base .guided-chip-btn hover rule").toBeDefined();
    expect(pressed, "no pressed .guided-chip-btn hover rule").toBeDefined();
    expect(classLevelSpecificity(base!.selector)).toBeLessThan(
      classLevelSpecificity(pressed!.selector),
    );
  });

  it("layers the hover tint over the chip's own opaque fill instead of replacing it", () => {
    // --color-highlight is translucent. Assigning it to background-color threw
    // away --color-surface-elevated and composited the tint against whatever
    // card sat behind the button — a 1.06:1 move in dark, and the OPPOSITE
    // direction in light. The base fill must survive the hover.
    const base = guidedRules.find((rule) =>
      rule.selectors.includes(".guided-chip-btn"),
    );
    expect(declaredValue(base!, "background-color")).toBe(
      "var(--color-surface-elevated)",
    );

    const hover = hoverRules.find(({ selector }) =>
      selector.startsWith(".guided-chip-btn:hover"),
    )!.rule;
    expect(
      declaredValue(hover, "background-color"),
      "the hover must not replace the chip's opaque fill",
    ).toBeNull();
    expect(declaredValue(hover, "background-image")).toContain(
      "var(--color-highlight)",
    );
  });

  it("strengthens the hover boundary rather than re-declaring the resting one", () => {
    // The old hover named --color-chip-border, the SAME token the base rule
    // sets: a literal no-op, so the house "lighten the surface AND strengthen
    // the border" contract had no border half at all.
    const base = guidedRules.find((rule) =>
      rule.selectors.includes(".guided-chip-btn"),
    )!;
    const hover = hoverRules.find(({ selector }) =>
      selector.startsWith(".guided-chip-btn:hover"),
    )!.rule;
    const restingBorder = declaredValue(base, "border")!;
    const hoverBorder = declaredValue(hover, "border-color");
    const hoverRing = declaredValue(hover, "box-shadow");
    const borderIsANoOp =
      hoverBorder !== null && restingBorder.includes(hoverBorder);
    expect(
      !borderIsANoOp || hoverRing !== null,
      "the hover changes no boundary property the resting rule does not already set",
    ).toBe(true);
  });

  it("dims a disabled chip on the same token pair .btn:disabled uses", () => {
    const disabled = guidedRules.find((rule) =>
      rule.selectors.includes(".guided-chip-btn:disabled"),
    );
    expect(disabled, "no .guided-chip-btn:disabled rule").toBeDefined();
    expect(disabled!.selectors).toContain('.guided-chip-btn[aria-disabled="true"]');
    for (const property of ["background-color", "color", "border-color", "cursor"]) {
      expect(
        declaredValue(disabled!, property),
        `disabled chip ${property} must match .btn:disabled`,
      ).toBe(valueIn(sharedCss, ".btn:disabled", property));
    }
  });
});

describe("guided decision card geometry", () => {
  it("rounds the decision card like its peer cards, not like a button (elspeth-cad0862ed7)", () => {
    // Peers in the same scroller: .ack-stack (chat.css) above it and
    // .guided-completion in the terminal state.
    const decision = valueIn(guidedCss, ".guided-current-decision", "border-radius");
    expect(decision).toBe(
      valueIn(guidedCss, ".guided-completion", "border-radius"),
    );
    expect(decision).toBe(valueIn(chatCss, ".ack-stack", "border-radius"));
  });

  it("centres the footer rule between the last option and the Explain button (elspeth-74db8a22e6)", () => {
    // The gap ABOVE the hairline is .guided-turn's own bottom padding plus any
    // footer margin-top; the gap BELOW is the footer's padding-top. Measured at
    // 24px / 8px before the fix. Assert the arithmetic, not the literals.
    const footer = guidedRules.find((rule) =>
      rule.selectors.includes(".guided-current-decision-footer"),
    )!;
    const turnPadding = valueIn(guidedCss, ".guided-turn", "padding")!;
    const turnPaddingBlock = tokenPx(turnPadding.split(" ")[0]);
    const footerMargin = declaredValue(footer, "margin-top");
    const above = turnPaddingBlock + (footerMargin === null ? 0 : tokenPx(footerMargin));
    const below = tokenPx(declaredValue(footer, "padding-top")!);
    expect(above, `rule sits ${above}px below the options and ${below}px above the button`).toBe(below);
  });

  it("binds an option's hint to the option above it (elspeth-802d658c79)", () => {
    // The hint used to sit ~6px under its own option and ~10px above the next
    // one — almost equidistant — so a hinted row read as detached from the
    // button it describes.
    const itemGap = tokenPx(valueIn(guidedCss, ".guided-chip-item", "gap")!);
    const rowGap = tokenPx(
      valueIn(guidedCss, ".guided-chip-group", "gap")!.split(" ")[0],
    );
    expect(rowGap).toBeGreaterThan(itemGap * 2);
  });
});

describe("guided motion and type tokens (elspeth-616a236fc3, elspeth-8dc37146d0)", () => {
  it("spends no raw duration in any transition or animation declaration", () => {
    // guided.css carried 20 raw `0.1s ease` declarations and consumed ZERO
    // motion tokens across the largest authoring surface in the product, so a
    // change to the motion scale silently skipped it. Every transition/
    // animation declaration in the file is walked (not just the ones whose
    // property is spelled `transition`), and any bare time literal fails.
    const offenders: string[] = [];
    for (const rule of guidedRules) {
      for (const declaration of rule.declarations.split(";")) {
        const parsed = /^\s*((?:transition|animation)[\w-]*)\s*:\s*(.+)$/s.exec(
          declaration,
        );
        if (parsed === null) continue;
        if (/\d+(\.\d+)?m?s\b/.test(parsed[2])) {
          offenders.push(`${rule.selectors[0]} { ${parsed[1]}: ${parsed[2].trim()} }`);
        }
      }
    }
    expect(offenders, "raw durations bypass the motion tokens").toEqual([]);
  });

  it("resolves the schema textarea to the shared mono family", () => {
    // The hardcoded stack that used to live here omitted JetBrains Mono, so the
    // field rendered in a different typeface from the mono label above it.
    expect(valueIn(guidedCss, ".guided-schema-textarea", "font-family")).toBe(
      "var(--font-mono, monospace)",
    );
  });
});

describe("guided scroller's marked top boundary (elspeth-3797c79446)", () => {
  // Content scrolling under the step-indicator band used to be clipped dead
  // through its letterforms — a half-sliced decision heading read as a
  // rendering fault, not as "there is more above". The scroller masks its
  // top edge so glyphs fade out as they approach the band.

  it("fades the scroll container's top edge", () => {
    const mask = valueIn(guidedCss, ".guided-authoring-scroll", "mask-image");
    expect(mask, "the guided scroller must mask its top edge").not.toBeNull();
    expect(mask).toMatch(/^linear-gradient\(to bottom,\s*transparent/);
  });

  it("keeps the fade ramp inside the scroller's own top padding", () => {
    // AGREEMENT constraint: the ramp must be no longer than the padding
    // above the first card, or content at scrollTop 0 — with nothing above
    // it to scroll to — would render partially faded.
    const mask = valueIn(guidedCss, ".guided-authoring-scroll", "mask-image")!;
    const ramp = /(?:#000|black)\s+(var\(--[\w-]+\)|\d+(?:\.\d+)?px)\s*\)/.exec(
      mask,
    );
    expect(ramp, `mask ramp length not extractable from: ${mask}`).not.toBeNull();
    const paddingBlock = valueIn(
      guidedCss,
      ".guided-authoring-scroll",
      "padding",
    )!.split(/\s+/)[0];
    expect(tokenPx(ramp![1])).toBeLessThanOrEqual(tokenPx(paddingBlock));
  });
});
