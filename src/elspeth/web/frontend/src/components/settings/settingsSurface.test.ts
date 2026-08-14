import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Stylesheet invariants for the settings surfaces — the secrets modal, the
// dev-admin user dialog and the composer-preferences panel
// (elspeth-03f43bdef0, elspeth-b9871d3648, elspeth-c5efcaf102,
// elspeth-e977b66dba, elspeth-035c98081b, elspeth-2580a7b094,
// elspeth-38ffb9aff3, elspeth-ca94961ead).
//
// Every assertion here resolves the WINNING declaration out of the shipped
// barrel and checks the constraint the value was chosen for — a token
// identity, or an arithmetic relationship between two values that have to be
// picked together. None of them pins a literal: a lane that retunes
// --size-control or --space-lg moves both sides of the comparison and stays
// green, while a lane that changes one value in isolation fails.
//
// Barrel-loaded in cascade order, following the idiom in
// components/chat/chatBubbleGutter.test.ts. Import paths are resolved against
// the BARREL's directory; vitest runs from the frontend root.
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

/** EVERY value declared for `property` by a rule listing `selector` verbatim,
 *  in barrel order — @media-nested rules are captured as their own entries so
 *  a branch that only exists inside a media query is inspected too. */
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

/** The declaration that actually reaches the element among the
 *  equal-specificity rules naming `selector`. */
function declaredValue(selector: string, property: string): string {
  const found = declaredValues(selector, property);
  if (found.length === 0) {
    throw new Error(`No ${property} declared for ${selector}`);
  }
  return found[found.length - 1];
}

function hasRule(selector: string): boolean {
  return rules.some((rule) => rule.selectors.includes(selector));
}

const tokensCss = stripComments(readFileSync("src/styles/tokens.css", "utf8"));

function tokenPx(name: string): number {
  const match = new RegExp(`${name}:\\s*(-?[\\d.]+)px\\s*;`).exec(tokensCss);
  if (!match) {
    throw new Error(`No px value for ${name} in tokens.css`);
  }
  return Number.parseFloat(match[1]);
}

/** Every px value the spacing scale declares. A gap is "on the scale" when it
 *  is one of these — the point of the finding, rather than any one value. */
const spacingScale = new Set(
  Array.from(tokensCss.matchAll(/--space-[\w-]+:\s*(\d+(?:\.\d+)?)px\s*;/g)).map(
    (match) => Number.parseFloat(match[1]),
  ),
);

/** Resolve a length written as a bare px value or as a var() over a px token. */
function px(value: string): number {
  const substituted = value
    .replace(/var\(\s*(--[\w-]+)\s*\)/g, (_, name: string) => `${tokenPx(name)}`)
    .trim();
  if (!/^-?[\d.]+(px)?$/.test(substituted)) {
    throw new Error(`Unsupported length expression: ${value}`);
  }
  return Number.parseFloat(substituted);
}

describe("secrets inventory rows keep one trailing edge (elspeth-ca94961ead)", () => {
  it("reserves exactly the delete control's width on every row", () => {
    // Server- and org-scoped secrets render no delete button. The reserved
    // slot and the button it holds have to be chosen together: a slot
    // narrower than the button pushes the badge on deletable rows, a wider
    // one pushes it on read-only rows. Either way the badge column zig-zags.
    const slotBasis = px(declaredValue(".secrets-list-action", "flex").split(/\s+/)[2]);
    expect(slotBasis).toBe(px(declaredValue(".secrets-delete-btn", "min-width")));
    expect(slotBasis).toBe(tokenPx("--size-control"));
  });
});

describe("the destructive row action has a state surface (elspeth-e977b66dba)", () => {
  it("gives every interactive control in the secrets panel a hover background", () => {
    // The delete "×" was the one control in this file with no :hover at all,
    // while the harmless close button beside it lightened — least feedback
    // exactly where a mis-click is least recoverable.
    for (const selector of [".secrets-panel-close", ".secrets-delete-btn"]) {
      expect(
        hasRule(`${selector}:hover`),
        `${selector} ships without a hover state`,
      ).toBe(true);
      expect(
        declaredValues(`${selector}:hover`, "background").length +
          declaredValues(`${selector}:hover`, "background-color").length,
      ).toBeGreaterThan(0);
    }
  });

  it("transitions that hover on the shared control timing", () => {
    expect(declaredValue(".secrets-delete-btn", "transition")).toContain(
      "var(--transition-fast)",
    );
  });
});

describe("settings region boundaries are on the spacing scale (elspeth-035c98081b)", () => {
  const boundary = ".secrets-panel-section ~ .secrets-panel-section";

  it("separates every region the same way, with a rule and scale-value air", () => {
    const marginTop = px(declaredValue(boundary, "margin-top"));
    const paddingTop = px(declaredValue(boundary, "padding-top"));
    expect(
      spacingScale.has(marginTop),
      `${marginTop}px is not a value on the --space-* scale`,
    ).toBe(true);
    expect(
      paddingTop,
      "air above and below the boundary rule must match, or the rule reads as belonging to one side",
    ).toBe(marginTop);
    expect(declaredValue(boundary, "border-top")).toContain("var(--color-border)");
  });

  it("gives the footnote boundary the same treatment as the section boundary", () => {
    const footnote = ".secrets-panel-body > .secrets-footnote";
    expect(px(declaredValue(footnote, "margin-top"))).toBe(
      px(declaredValue(boundary, "margin-top")),
    );
    expect(declaredValue(footnote, "border-top")).toBe(
      declaredValue(boundary, "border-top"),
    );
  });
});

describe("bordered settings cards are not sharper than the dialog framing them (elspeth-38ffb9aff3)", () => {
  it("rounds every bordered content card at the card radius, not the badge radius", () => {
    for (const selector of [
      ".secrets-list-item",
      ".user-admin-password-banner",
      ".user-admin-create-form",
    ]) {
      const radius = px(declaredValue(selector, "border-radius"));
      expect(radius, `${selector} still carries the badge radius`).toBe(
        tokenPx("--radius-lg"),
      );
      expect(radius).toBeGreaterThan(tokenPx("--radius-sm"));
    }
    // The badge itself stays at the badge radius — the finding is about
    // cards adopting the badge step, not about flattening the two together.
    expect(px(declaredValue(".secrets-scope-badge", "border-radius"))).toBe(
      tokenPx("--radius-sm"),
    );
  });
});

describe("the composer-preferences fieldsets carry real chrome (elspeth-03f43bdef0, elspeth-c5efcaf102)", () => {
  it("strips the user-agent fieldset border and padding", () => {
    // With no class at all these rendered Chrome's 2px groove bevel on
    // --color-surface. Assert the reset, not any particular border value.
    expect(px(declaredValue(".composer-preferences-fieldset", "border"))).toBe(0);
    expect(px(declaredValue(".composer-preferences-fieldset", "padding"))).toBe(0);
  });

  it("puts the option gap on the scale and separates groups more than options", () => {
    // Sibling radios were held apart by the browser's ~5px default, so
    // "System" "Light" "Dark" read as one sentence. The relationship — a
    // group's own options sit closer together than two groups do — is what
    // makes the grouping legible, and it is what this pins.
    const optionGap = px(declaredValue(".composer-preferences-fieldset", "gap"));
    expect(spacingScale.has(optionGap), `${optionGap}px is off the scale`).toBe(
      true,
    );
    expect(optionGap).toBeGreaterThan(0);

    const groupGap = px(
      declaredValue(
        ".composer-preferences-fieldset ~ .composer-preferences-fieldset",
        "margin-top",
      ),
    );
    expect(spacingScale.has(groupGap), `${groupGap}px is off the scale`).toBe(true);
    expect(
      groupGap,
      "two option groups must be further apart than two options inside one group",
    ).toBeGreaterThan(optionGap);
  });

  it("labels its groups the way the sibling form in this dialog family does", () => {
    for (const property of ["font-size", "font-weight", "text-transform"]) {
      expect(declaredValue(".composer-preferences-legend", property)).toBe(
        declaredValue(".user-admin-create-form legend", property),
      );
    }
  });
});

describe("failure messages carry an error affordance (elspeth-b9871d3648)", () => {
  // .composer-preferences-error had three call sites and no rule: two
  // role="alert" messages rendered as ordinary body text and the third
  // carried a one-off inline style.
  for (const selector of [".composer-preferences-error", ".secrets-form-error"]) {
    it(`${selector} renders as an error surface, not as body text`, () => {
      expect(declaredValue(selector, "background-color")).toBe(
        "var(--color-error-bg)",
      );
      expect(declaredValue(selector, "border")).toContain(
        "var(--color-error-border)",
      );
      expect(declaredValue(selector, "color")).toBe("var(--color-error)");
      expect(px(declaredValue(selector, "border-radius"))).toBeGreaterThan(0);
    });
  }

  it("keeps the two names one affordance", () => {
    // Same failure, same rendering — whichever settings surface reports it.
    for (const property of [
      "background-color",
      "color",
      "border",
      "border-radius",
      "padding",
      "margin",
      "font-size",
    ]) {
      expect(declaredValue(".secrets-form-error", property)).toBe(
        declaredValue(".composer-preferences-error", property),
      );
    }
  });
});

describe("the settings forms compose the shared input primitive (elspeth-2580a7b094)", () => {
  it("no longer forks .input as .secrets-form-input", () => {
    expect(
      hasRule(".secrets-form-input"),
      "the fork is back; .input's height floor cannot reach the settings forms through it",
    ).toBe(false);
    expect(hasRule(".secrets-form-label")).toBe(false);
  });

  it("keeps the create row's field and its button on one control rung", () => {
    // .user-admin-create-fields is align-items: flex-end, so a button shorter
    // than the fields shows the difference as a step at the top edge.
    expect(px(declaredValue(".input", "min-height"))).toBe(
      px(declaredValue(".btn", "min-height")),
    );
  });
});
