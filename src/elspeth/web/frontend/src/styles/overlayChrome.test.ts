import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Constraint gates for the blocking-overlay chrome from the 2026-08-14
// professionalisation review (elspeth-e6fcd8d703 dialog primitive,
// elspeth-9ae8ee9ac2 scrim, elspeth-1c4687ff67 notice overlay,
// elspeth-44491d3ea1 shortcuts hierarchy).
//
// These deliberately pin RELATIONSHIPS and token CONSUMPTION, not literals:
// every backdrop must consume the one scrim token (whatever its value), the
// dialog primitive must stay grouped with a live consumer so it can never
// decay into a dead rule, and the shortcuts subheadings must resolve BELOW
// the dialog title they sit under. A sibling retuning any token value stays
// green; an edit that breaks the constraint fails here.
//
// Barrel-loaded in cascade order, exactly like styles/primitiveGeometry.test.ts.
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

/** Every rule whose selector list contains `selector` as a whole selector. */
function rulesFor(selector: string): Rule[] {
  return rules.filter((rule) => rule.selectors.includes(selector));
}

/**
 * The LAST value declared for `property` by a rule listing `selector`
 * verbatim, in barrel order — the declaration that reaches the element among
 * the equal-specificity rules that name it.
 */
function winningValue(selector: string, property: string): string | null {
  let winner: string | null = null;
  for (const rule of rulesFor(selector)) {
    const match = new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`).exec(
      rule.declarations,
    );
    if (match) winner = match[1].trim();
  }
  return winner;
}

const tokensCss = stripComments(readFileSync("src/styles/tokens.css", "utf8"));

/** A token's first (root-block) numeric value, px or unitless. */
function tokenNumber(name: string): number {
  const match = new RegExp(`${name}:\\s*(-?[\\d.]+)(?:px)?\\s*;`).exec(tokensCss);
  if (!match) {
    throw new Error(`No numeric value for ${name} in tokens.css`);
  }
  return Number.parseFloat(match[1]);
}

/** Resolve a bare number/px value or a single-token var() to a number. */
function resolveNumber(value: string): number {
  const varMatch = /^var\(\s*(--[\w-]+)\s*\)$/.exec(value);
  if (varMatch) return tokenNumber(varMatch[1]);
  const numMatch = /^(-?[\d.]+)(?:px)?$/.exec(value);
  if (!numMatch) {
    throw new Error(`Unsupported numeric expression: ${value}`);
  }
  return Number.parseFloat(numMatch[1]);
}

describe("one page-dimming veil behind every blocking overlay (elspeth-e6fcd8d703, elspeth-9ae8ee9ac2)", () => {
  // The review counted eleven hand-copied scrims across three alphas
  // (0.3 / 0.45 / 0.5), so "blocked" dimmed three different amounts depending
  // on which overlay was open. Every stylesheet-authored backdrop must now
  // consume the token; the token's own value is tokens.css's decision.
  it("points every *-backdrop background at var(--color-scrim)", () => {
    const backdropSelectors = new Set<string>();
    for (const rule of rules) {
      for (const selector of rule.selectors) {
        if (/^\.[\w-]+-backdrop$/.test(selector)) backdropSelectors.add(selector);
      }
    }
    // Inertness guard: the eight shipped backdrop families (dialog primitive,
    // confirm, catalog, palette, graph, yaml, recovery, save-for-review,
    // explain) must actually be seen — an empty scan proves nothing.
    expect(backdropSelectors.size).toBeGreaterThanOrEqual(8);
    for (const selector of backdropSelectors) {
      const background = winningValue(selector, "background-color");
      if (background === null) continue; // positioning-only helper, no veil
      expect(
        background,
        `${selector} must consume the shared scrim token, not a literal`,
      ).toBe("var(--color-scrim)");
    }
    // At least the known veils must have carried a background at all.
    expect(
      winningValue(".confirm-dialog-backdrop", "background-color"),
    ).toBe("var(--color-scrim)");
    expect(winningValue(".catalog-backdrop", "background-color")).toBe(
      "var(--color-scrim)",
    );
  });
});

describe("the app-dialog primitive (elspeth-e6fcd8d703)", () => {
  it("consumes the dialog band and the modal chrome tiers", () => {
    expect(winningValue(".app-dialog", "z-index")).toBe("var(--z-dialog)");
    expect(winningValue(".app-dialog", "border-radius")).toBe("var(--radius-lg)");
    expect(winningValue(".app-dialog", "box-shadow")).toBe("var(--shadow-modal)");
    expect(winningValue(".app-dialog-backdrop", "z-index")).toBe(
      "var(--z-dialog-backdrop)",
    );
    expect(winningValue(".app-dialog-backdrop", "background-color")).toBe(
      "var(--color-scrim)",
    );
  });

  it("stays grouped with a live consumer, so it cannot decay into a dead rule", () => {
    // A promoted class no TSX applies is the Theme-2 inverse defect
    // (elspeth-09a1a87051's trap). The primitive's names are therefore
    // REQUIRED to share their rules with selectors that ship today:
    // .confirm-dialog (ConfirmDialog + ShortcutsHelp) and the graph/yaml
    // modal close buttons. Splitting the group into a lone .app-dialog rule
    // fails here until the split brings its own consumer.
    for (const [primitive, consumer] of [
      [".app-dialog", ".confirm-dialog"],
      [".app-dialog-backdrop", ".confirm-dialog-backdrop"],
      [".dialog-close", ".graph-modal-close"],
    ]) {
      const grouped = rulesFor(primitive);
      expect(grouped.length, `${primitive} must have a rule`).toBeGreaterThan(0);
      for (const rule of grouped) {
        expect(
          rule.selectors,
          `${primitive} must stay grouped with shipping consumer ${consumer}`,
        ).toContain(consumer);
      }
    }
  });

  it("keeps the settings-dialog closures to per-dialog decisions only", () => {
    // The three settings dialogs (SecretsPanel, UserAdminDialog,
    // ComposerPreferencesPanel) compose .app-dialog in their TSX; their
    // closures may decide width and content scale, nothing more.
    // Re-declaring chrome in a closure forks the primitive the moment a
    // token moves — the exact pathology their inline style objects had.
    for (const selector of [".settings-dialog", ".settings-dialog-wide"]) {
      expect(
        rulesFor(selector).length,
        `${selector} must have a rule`,
      ).toBeGreaterThan(0);
      for (const property of [
        "border-radius",
        "box-shadow",
        "z-index",
        "background-color",
        "max-height",
        "position",
      ]) {
        expect(
          winningValue(selector, property),
          `${selector} must leave ${property} to the .app-dialog primitive`,
        ).toBeNull();
      }
    }
  });

  it("resolves the explain-dialog close onto the shared dialog-close recipe", () => {
    // .explain-dialog-close cannot be renamed — the class is ExplainDialog's
    // focus-trap initial-focus selector — so it collapses by grouping:
    // common.css imports after audit.css, and the group's declarations must
    // be the ones that win over the older hand-rolled copy (which sat at the
    // badge radius with no font).
    for (const property of [
      "border-radius",
      "min-width",
      "min-height",
      "font-family",
      "font-size",
    ]) {
      expect(
        winningValue(".explain-dialog-close", property),
        `.explain-dialog-close must resolve ${property} to the .dialog-close recipe`,
      ).toBe(winningValue(".dialog-close", property));
    }
  });

  it("rounds every CSS-authored blocking frame at the modal radius", () => {
    // The review shipped three radii across the seven frames; graph/yaml took
    // the correction under elspeth-03492be50b, explain and save-for-review
    // under elspeth-e6fcd8d703. The remaining offenders WERE the three
    // inline-styled settings dialogs, which no stylesheet could reach —
    // they now compose .app-dialog directly.
    for (const selector of [
      ".confirm-dialog",
      ".command-palette",
      ".recovery-panel",
      ".graph-modal",
      ".yaml-modal",
      ".explain-dialog",
      ".save-for-review-dialog",
    ]) {
      expect(
        winningValue(selector, "border-radius"),
        `${selector} must round at the modal radius`,
      ).toBe("var(--radius-lg)");
    }
  });
});

describe("the notice bar overlays the app instead of displacing it (elspeth-1c4687ff67)", () => {
  it("keeps the notice center out of normal flow", () => {
    // In flow before the header, a notice's arrival shifted the ENTIRE
    // viewport down by the bar's height and snapped it back on dismiss.
    expect(winningValue(".app-notice-center", "position")).toBe("fixed");
  });

  it("reserves the strip's band at the top of the app shell", () => {
    // The strips are fixed at top: 0 and one --size-control tall, so without a
    // matching reserve on .app-root the bar covers the header: every header
    // control sat inside its band and elementFromPoint returned the bar, so
    // none of them could be clicked while a notice was up. Seating the strips
    // below the header instead was measured and rejected — it moved the
    // occlusion onto the artifact tab strip. The reserve must stay the STRIP's
    // height and stay unconditional; making it conditional on a notice being
    // mounted reintroduces the arrival/dismiss shift elspeth-1c4687ff67 removed.
    expect(winningValue(".app-notice-center", "top")).toBe("0");
    expect(
      winningValue(".app-notice-center", "height"),
      "the wrapper takes its height from the strip it holds",
    ).toBeNull();
    expect(winningValue(".app-root", "padding-top")).toBe("var(--size-control)");
    expect(
      winningValue(".app-notice-primary.alert-banner", "height"),
      "the reserve is only correct while the strip is exactly that tall",
    ).toBe("var(--size-control)");
  });

  it("stacks it on a tokenised band above page chrome and below dialogs", () => {
    const zIndex = winningValue(".app-notice-center", "z-index");
    expect(zIndex).toMatch(/^var\(--z-[\w-]+\)$/);
    const band = tokenNumber(/^var\((--z-[\w-]+)\)$/.exec(zIndex ?? "")![1]);
    expect(band, "must clear the catalog drawer").toBeGreaterThan(
      tokenNumber("--z-catalog"),
    );
    expect(band, "must not cover a modal dialog's backdrop").toBeLessThan(
      tokenNumber("--z-dialog-backdrop"),
    );
  });

  it("centres the primary row's content instead of parking it at the bar's edges", () => {
    expect(
      winningValue(".app-notice-primary.alert-banner", "justify-content"),
    ).toBe("center");
  });
});

describe("shortcuts subheadings sit BELOW the dialog title (elspeth-44491d3ea1)", () => {
  // With no rule behind the class, the four <h3>s fell to the UA default —
  // 18.72px/700 against the dialog's 18px/600 <h2>: a measured hierarchy
  // inversion. The constraint is the ORDERING, not any particular size.
  it("resolves the subheading strictly smaller than the dialog title", () => {
    const subheading = winningValue(".shortcuts-subheading", "font-size");
    const title = winningValue(".confirm-dialog-title", "font-size");
    expect(subheading).not.toBeNull();
    expect(title).not.toBeNull();
    expect(resolveNumber(subheading!)).toBeLessThan(resolveNumber(title!));
  });

  it("resolves the subheading no heavier than the dialog title", () => {
    const subheading = winningValue(".shortcuts-subheading", "font-weight");
    const title = winningValue(".confirm-dialog-title", "font-weight");
    expect(subheading).not.toBeNull();
    expect(title).not.toBeNull();
    expect(resolveNumber(subheading!)).toBeLessThanOrEqual(resolveNumber(title!));
  });

  it("draws the subheading margins from the spacing scale, not the UA default", () => {
    const margin = winningValue(".shortcuts-subheading", "margin");
    expect(margin).not.toBeNull();
    for (const component of margin!.split(/\s+/)) {
      expect(
        component,
        `margin component ${component} must be 0 or a --space token`,
      ).toMatch(/^(?:0|var\(--space-[\w-]+\))$/);
    }
  });
});
