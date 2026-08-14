import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

// Geometry invariants for the shared primitives in styles/shared.css,
// styles/common.css and styles/animations.css, from the 2026-08-14
// professionalisation review.
//
// These are deliberately NOT declaration-existence tests. Each one either
// resolves a CASCADE question (does the winning rule for this element carry the
// declaration?) or an ARITHMETIC one (do two values that must be chosen
// together actually agree?) — the two failure modes the review kept finding,
// where a stylesheet contained a correct-looking declaration that never reached
// the element or that contradicted a sibling value.
//
// Loaded through the styles/index.css barrel in barrel order, exactly like
// colorContrast.test.ts / buttonCascade.test.ts, so rule ORDER in the array
// below is real cascade order.
const stylesheetBarrel = readFileSync("src/styles/index.css", "utf8");
const appCss = [
  ...Array.from(stylesheetBarrel.matchAll(/@import\s+"(?<path>[^"]+)";/g)).map((match) => {
    const importPath = match.groups?.path;
    if (importPath === undefined) {
      throw new Error("styles/index.css import regex produced no path");
    }
    return readFileSync(fileURLToPath(new URL(importPath, import.meta.url)), "utf8");
  }),
].join("\n");

// cwd-relative per the tokenReferences.test.ts idiom; vitest runs from the
// frontend root.
const commonCss = readFileSync("src/styles/common.css", "utf8");

function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

interface Rule {
  /** Comma-separated selector list, split and trimmed. */
  selectors: string[];
  /** Raw declaration block. */
  declarations: string;
  /** Position in barrel order — a larger index wins an equal-specificity tie. */
  index: number;
}

/**
 * Flat rule list in barrel order. Rules nested in @media / @keyframes are
 * captured as their own entries (the at-rule prelude never matches, because a
 * declaration block containing `{` cannot satisfy `[^{}]*`) — the same
 * technique buttonCascade.test.ts uses.
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

const appRules = parseRules(appCss);

/** Every rule whose selector list contains `selector` as a whole selector. */
function rulesFor(selector: string): Rule[] {
  return appRules.filter((rule) => rule.selectors.includes(selector));
}

/**
 * The LAST value declared for `property` by a rule listing `selector` verbatim,
 * in barrel order. Those rules all tie on specificity, so among them the last
 * one wins — which is why this answers "which declaration reaches the element",
 * not merely "does this text appear in a stylesheet".
 *
 * Scope limit, deliberately not papered over: it compares selector STRINGS, so
 * a more specific descendant rule (`.confirm-dialog .confirm-dialog-header`) or
 * a second class on the same element (`.plugin-card-disclosure`) is invisible
 * to it and could override the value it reports. No such rule exists for the
 * selectors asserted below today; where one does exist across a file boundary
 * the fact is stated at the assertion instead of being silently absorbed.
 */
function winningValue(selector: string, property: string): string | null {
  let winner: string | null = null;
  for (const rule of rulesFor(selector)) {
    const declaration = new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`).exec(
      rule.declarations,
    );
    if (declaration) winner = declaration[1].trim();
  }
  return winner;
}

function firstIndexOf(selector: string): number {
  const rules = rulesFor(selector);
  if (rules.length === 0) throw new Error(`No rule found for selector ${selector}`);
  return rules[0].index;
}

describe("control-height rungs (elspeth-04d8ef4f3b, elspeth-39bd9d68b3)", () => {
  it("binds .btn-small to the compact rung and lets it WIN the tie with .btn", () => {
    // .btn-small carried font-size and padding but no min-height, so all 14
    // call sites — every one of which also carries .btn, hand-written or via
    // ui/Button (which emits "btn" whenever compact={false}) — rendered a 12px
    // label inside .btn's 44px box. Both selectors are (0,1,0), so the fix only
    // works if .btn-small is declared AFTER .btn; assert the order, not just
    // the declaration.
    //
    // Reach, measured: 17 call sites carry .btn-small and all 17 also carry
    // .btn, so 14 move from 44px to 36px here. The other three
    // (.plugin-card-detail-toggle, .plugin-card-disclosure,
    // .inline-chat-source-entry-try) keep a bespoke `min-height: 30px` at
    // catalog.css:160-162 and :598 — a LATER barrel import at equal
    // specificity — so they still ship 30px until the catalog lane deletes
    // those literals. This assertion covers styles/shared.css only.
    expect(winningValue(".btn-small", "min-height")).toBe("var(--size-control-compact)");
    expect(
      firstIndexOf(".btn-small"),
      ".btn-small must stay BELOW .btn in styles/shared.css — the two selectors " +
        "tie at (0,1,0) and only source order resolves them",
    ).toBeGreaterThan(firstIndexOf(".btn"));
  });

  it("binds .input to the same rung as the .btn that completes its form", () => {
    // Without a min-height, .input's height was content-derived (16px x 1.4 +
    // 8px + 8px + 2px = 40.4px) against the 44px .btn beneath it.
    expect(winningValue(".input", "min-height")).toBe("var(--size-control)");
    expect(winningValue(".btn", "min-height")).toBe("var(--size-control)");
  });

  it("keeps both modal close buttons on a tokenised control rung", () => {
    for (const selector of [".graph-modal-close", ".yaml-modal-close"]) {
      expect(winningValue(selector, "min-height")).toBe("var(--size-control-compact)");
      expect(winningValue(selector, "min-width")).toBe("var(--size-control-compact)");
    }
  });
});

describe("overlay headers carry a bottom rule (elspeth-329ca624a8)", () => {
  // Half the app's dialog and drawer headers had no separation between title
  // and body; the three that did copy-pasted the recipe byte-for-byte. The
  // check that matters is REACH: does a rule that actually matches this class
  // carry the rule? A rule that matches nothing is the silent-failure shape.
  it.each([".graph-modal-header", ".yaml-modal-header", ".confirm-dialog-header"])(
    "%s is reached by a rule declaring a border-bottom",
    (selector) => {
      expect(winningValue(selector, "border-bottom")).toBe("1px solid var(--color-border)");
    },
  );

  it("declares the shared modal-header recipe exactly once in common.css", () => {
    // Re-splitting the grouped rule back into per-modal copies is the
    // regression this guards; the two headers must keep sharing one block.
    const commonRules = parseRules(commonCss);
    const headerRules = commonRules.filter((rule) =>
      rule.selectors.some(
        (selector) =>
          selector === ".graph-modal-header" || selector === ".yaml-modal-header",
      ),
    );
    expect(headerRules).toHaveLength(1);
    expect(headerRules[0].selectors).toEqual([".graph-modal-header", ".yaml-modal-header"]);
  });
});

describe("confirm dialog brackets its scroll owner (elspeth-8013e385df)", () => {
  it("puts a rule and air at BOTH ends of the scrolling body", () => {
    // The last body ink row measured y=634 against the button band's border at
    // y=635. The only air was .confirm-dialog-message's bottom margin, which
    // lives inside the scroller and scrolls away.
    expect(winningValue(".confirm-dialog-header", "border-bottom")).not.toBeNull();
    expect(winningValue(".confirm-dialog-actions", "border-top")).toBe(
      "1px solid var(--color-border)",
    );
    expect(winningValue(".confirm-dialog-actions", "padding-top")).toBe("var(--space-lg)");
    expect(
      winningValue(".confirm-dialog-body", "padding-bottom"),
      "the bottom air must sit on the SCROLL OWNER so it survives to the end of the scroll",
    ).toBe("var(--space-md)");
  });
});

describe("validation banner keeps one indent system (elspeth-d1edd9bef8)", () => {
  it("lands the pass and fail bullet columns in the same place", () => {
    // The banner occupies one slot, so pass and fail must not shift the text
    // block. The fail list previously indented 28px against the pass list's
    // --space-xl (24px).
    const passList = winningValue(".validation-banner-checks", "padding");
    const failList = winningValue(".validation-banner-fail-list", "padding");
    expect(passList).toBe("0 0 0 var(--space-xl)");
    expect(failList).toBe("0 0 0 var(--space-xl)");
  });

  it("takes the fail title's inset from .validation-banner alone", () => {
    // Its own 8px/12px padding stacked on top of the banner's, putting the
    // fail title 12px right and 8px down of the pass summary.
    expect(winningValue(".validation-banner-fail-title", "padding")).toBeNull();
    expect(winningValue(".validation-banner-fail-title", "padding-left")).toBeNull();
  });
});

describe("indeterminate progress stripe (elspeth-14523219ee, elspeth-58903688f2)", () => {
  function px(value: string | null): number {
    const match = /(-?\d+(?:\.\d+)?)px/.exec(value ?? "");
    if (!match) throw new Error(`Expected a px value, got ${value}`);
    return Number.parseFloat(match[1]);
  }

  it("advances exactly one gradient period per iteration", () => {
    // The keyframe distance and the background-size must stay equal or the
    // loop visibly jumps at the wrap.
    const period = px(winningValue(".progress-bar-stripe", "background-size"));
    const keyframeEnd = appRules.filter((rule) => rule.selectors.includes("100%"));
    const travel = keyframeEnd
      .map((rule) => /background-position:\s*(-?\d+(?:\.\d+)?)px/.exec(rule.declarations))
      .filter((match): match is RegExpExecArray => match !== null)
      .map((match) => Number.parseFloat(match[1]));
    expect(travel, "the progress-stripe keyframe must translate by one period").toContain(
      period,
    );
  });

  it("chooses the gradient period WITH the track height, not independently", () => {
    // At 40px against a 8px track the hatching rendered as chunky slabs — the
    // defect was the ratio, not the motion. Cap it at 2x so the bar reads as
    // hatching.
    const period = px(winningValue(".progress-bar-stripe", "background-size"));
    const trackHeight = px(winningValue(".progress-bar", "height"));
    expect(period).toBeLessThanOrEqual(trackHeight * 2);
  });

  it("declares the track height that actually ships, so import order cannot decide it", () => {
    // ProgressView.tsx renders className="progress-bar progress-bar-outer";
    // .progress-bar-outer (execution.css) is a later barrel import at equal
    // specificity, so it shipped. Both must now agree.
    expect(px(winningValue(".progress-bar", "height"))).toBe(
      px(winningValue(".progress-bar-outer", "height")),
    );
  });
});

describe("centred placeholder recipes (elspeth-4b8e6273bf)", () => {
  it("ships one padding value across all three placeholder surfaces", () => {
    const paddings = [".empty-state", ".command-palette-empty", ".yaml-loading"].map(
      (selector) => winningValue(selector, "padding"),
    );
    expect(new Set(paddings).size, `expected one padding, got ${paddings.join(" / ")}`).toBe(
      1,
    );
    expect(paddings[0]).toBe("var(--space-2xl)");
  });
});

describe("shared modal surfaces (elspeth-03492be50b, elspeth-93afabcde8)", () => {
  it("rounds every modal at the modal radius, not the button radius", () => {
    for (const selector of [".graph-modal", ".yaml-modal", ".confirm-dialog"]) {
      expect(winningValue(selector, "border-radius")).toBe("var(--radius-lg)");
    }
  });

  it("does not frame a --color-bg canvas in the warm paper family", () => {
    // .graph-modal-body renders <GraphView />, whose React Flow canvas paints
    // --color-bg. .yaml-modal already migrated off paper for this exact clash.
    expect(winningValue(".graph-modal", "background-color")).toBe(
      "var(--color-surface-elevated)",
    );
  });
});

describe("primary notice row (elspeth-81f714e210)", () => {
  it("leaves the contained control's focus ring unclipped", () => {
    // The action is flex-centred, so symmetric padding cannot move it: a 36px
    // control's ring needs 36 + 2x(2px offset + 2px width) = 44px against a
    // 43px padding box (44px row - 1px .alert-banner border). Removing the
    // PAINT clip is what uncuts it; height/max-height still bound the layout.
    const rule = rulesFor(".app-notice-primary.alert-banner");
    expect(rule).toHaveLength(1);
    expect(rule[0].declarations).not.toMatch(/(?:^|;)\s*overflow\s*:/);
    expect(winningValue(".app-notice-primary.alert-banner", "height")).toBe(
      "var(--size-control)",
    );
    expect(winningValue(".app-notice-primary.alert-banner", "max-height")).toBe(
      "var(--size-control)",
    );
  });

  it("gives the content box room for the 36px control it holds", () => {
    // 44px row - 1px border - 2 x --space-2xs (2px) = 39px >= 36px. The old
    // --space-xs left 35px.
    expect(winningValue(".app-notice-primary.alert-banner", "padding-block")).toBe(
      "var(--space-2xs)",
    );
  });
});
