import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Chrome contracts for the header session switcher (elspeth-8c7ac37d49).
//
// The defect class this file gates: HeaderSessionSwitcher.tsx once emitted
// five class names (`-controls`, `-filter`, `-show-archived`,
// `-archive-error`, `-rename-error`) that NO stylesheet defined, so the
// filter strip rendered as white UA controls bursting the 41px header to
// ~60px, and both role="alert" strips rendered as bare body prose. A class
// name is a cross-file reference with no compiler: nothing but a test notices
// when the referent is missing. This is the component-scoped analogue of
// styles/tokenReferences.test.ts (which does the same for custom
// properties) — every class the component emits must be NAMED by at least
// one selector in the shipped barrel.
//
// The truth half then pins the constraints that make the defined classes
// actually cure the defect, chatBubbleGutter.test.ts-style (winning
// declaration resolved out of the barrel, compared against tokens — never a
// raw literal pin):
//   * the popover and the archive alert are out-of-flow (position: absolute),
//     which is the entire height contract — in-flow content inside the
//     wrapper is what grew the header;
//   * both failure strips carry the product's error affordance (the error
//     token trio), because a role="alert" styled as body prose is the worst
//     form of the original finding;
//   * the filter controls sit on the compact control rung, not UA-default
//     geometry.
//
// Barrel-loaded in cascade order; import paths resolved against the barrel's
// directory, cwd-relative per the tokenReferences.test.ts idiom (vitest runs
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

const componentSource = readFileSync(
  "src/components/sessions/HeaderSessionSwitcher.tsx",
  "utf8",
);

// Every class token the component emits through a static className string.
// Derived from the component source so a NEW emitted class with no stylesheet
// definition fails here the day it is written, not in a later visual review.
const emittedClasses = Array.from(
  new Set(
    Array.from(componentSource.matchAll(/className="([^"]+)"/g)).flatMap(
      (match) => match[1].split(/\s+/).filter((token) => token.length > 0),
    ),
  ),
);

function classIsDefined(className: string): boolean {
  const probe = new RegExp(`\\.${className}(?![\\w-])`);
  return rules.some((rule) =>
    rule.selectors.some((selector) => probe.test(selector)),
  );
}

describe("every class HeaderSessionSwitcher emits is defined in the barrel (elspeth-8c7ac37d49)", () => {
  it("emits the classes this gate exists for", () => {
    // The five originally-undefined names plus the popover surface: if a
    // refactor renames them the definition check below must follow the NEW
    // names, so pin that the derivation still sees this family at all.
    for (const expected of [
      "header-session-switcher-popover",
      "header-session-switcher-controls",
      "header-session-switcher-filter",
      "header-session-switcher-show-archived",
      "header-session-switcher-archive-error",
      "header-session-switcher-rename-error",
    ]) {
      expect(emittedClasses, `component no longer emits .${expected}`).toContain(
        expected,
      );
    }
  });

  it("finds a defining selector for every emitted class", () => {
    for (const className of emittedClasses) {
      expect(
        classIsDefined(className),
        `.${className} is emitted by HeaderSessionSwitcher.tsx but no stylesheet in the barrel names it`,
      ).toBe(true);
    }
  });
});

describe("the header height contract: floating surfaces are out of flow", () => {
  it("hangs the popover off the trigger instead of growing the header", () => {
    // In-flow content inside the wrapper is what burst the 41px header to
    // ~60px and shoved the separator + model chip 314px right. Out-of-flow
    // plus the overlay layer is the contract; the wrapper anchors it.
    expect(declaredValue(".header-session-switcher-popover", "position")).toBe(
      "absolute",
    );
    expect(declaredValue(".header-session-switcher-popover", "z-index")).toBe(
      "var(--z-overlay)",
    );
    expect(declaredValue(".header-session-switcher", "position")).toBe(
      "relative",
    );
  });

  it("floats the archive alert, which renders while the menu is closed", () => {
    expect(
      declaredValue(".header-session-switcher-archive-error", "position"),
    ).toBe("absolute");
    expect(
      declaredValue(".header-session-switcher-archive-error", "z-index"),
    ).toBe("var(--z-overlay)");
  });
});

describe("the failure strips carry the error affordance, not body prose", () => {
  for (const selector of [
    ".header-session-switcher-archive-error",
    ".header-session-switcher-rename-error",
  ]) {
    it(`${selector} resolves text and frame through the error tokens`, () => {
      expect(declaredValue(selector, "color")).toBe("var(--color-error)");
      expect(declaredValue(selector, "border")).toContain(
        "var(--color-error-border)",
      );
    });
  }

  it("layers the in-menu rename strip's wash over an opaque surface", () => {
    // This assertion used to pin background-color === var(--color-error-bg) —
    // pinning the FAILING composition as the contract: the bare tint sits on
    // the popover's nav-raised ground, which the contrast gate's tint matrix
    // does not enumerate, and measured 4.06:1 in the light theme. The strip
    // now uses the same layered recipe as the archive strip below, so both
    // pins state the same constraint: opaque ground, tint rides on top.
    expect(
      declaredValue(".header-session-switcher-rename-error", "background-color"),
    ).toBe("var(--color-surface)");
    expect(
      declaredValue(".header-session-switcher-rename-error", "background-image"),
    ).toContain("var(--color-error-bg)");
  });

  it("layers the floating archive strip's wash over an opaque surface", () => {
    // --color-error-bg is a low-alpha wash and this box floats over arbitrary
    // workspace content, so the tint rides background-image on top of an
    // opaque background-color — dropping either half regresses legibility.
    expect(
      declaredValue(".header-session-switcher-archive-error", "background-color"),
    ).toBe("var(--color-surface)");
    expect(
      declaredValue(".header-session-switcher-archive-error", "background-image"),
    ).toContain("var(--color-error-bg)");
  });
});

describe("the filter strip sits on the compact control rung, not UA defaults", () => {
  it("gives the filter field the control floor and the input border pair", () => {
    expect(declaredValue(".header-session-switcher-filter", "min-height")).toBe(
      "var(--size-control-compact)",
    );
    expect(declaredValue(".header-session-switcher-filter", "border")).toContain(
      "var(--color-input-border)",
    );
    expect(
      declaredValue(".header-session-switcher-filter", "background-color"),
    ).toBe("var(--color-surface-elevated)");
  });

  it("gives the show-archived toggle the same floor and the accent checkbox", () => {
    expect(
      declaredValue(".header-session-switcher-show-archived", "min-height"),
    ).toBe("var(--size-control-compact)");
    expect(
      declaredValue(".header-session-switcher-show-archived input", "accent-color"),
    ).toBe("var(--color-accent)");
  });
});
