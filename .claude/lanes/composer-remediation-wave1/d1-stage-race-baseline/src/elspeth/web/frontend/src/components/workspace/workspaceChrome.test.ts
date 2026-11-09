import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Structural contracts for the workspace chrome — the pane seam, the bottom
// action bar — from the 2026-08-14 professionalisation
// review.
//
// These are deliberately NOT "the file contains this string" tests. Each one
// pins a relationship that two or more declarations have to agree on, because
// every defect in this batch was two values that were CHOSEN together and then
// drifted apart: the drawer's width against the strip the pane reserved for it,
// the bar's outer gap against the gaps inside its groups, the authoring-side
// band against the action bar it has to meet across the divider. A single
// declaration read back as text would have passed while the relationships were broken.
//
// cwd-relative per the tokenReferences.test.ts idiom; vitest runs from the
// frontend root.
const css = readFileSync(
  join(process.cwd(), "src/components/workspace/workspace.css"),
  "utf8",
);
const cssWithoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");

interface Rule {
  selector: string;
  declarations: string;
}

/**
 * Flat rule list. Rules nested in @media are captured as their own entries —
 * the at-rule prelude never matches, because its body contains braces.
 */
const rules: Rule[] = [
  ...cssWithoutComments.matchAll(/([^{}]+)\{([^{}]*)\}/g),
].map((match) => ({
  selector: match[1].replace(/\s+/g, " ").trim(),
  declarations: match[2],
}));

function ruleFor(selector: string): string {
  const found = rules.filter((rule) => rule.selector === selector);
  if (found.length === 0) {
    throw new Error(`No rule for ${selector}`);
  }
  return found.map((rule) => rule.declarations).join("");
}

/**
 * The winning value of `property` for `selector`. A selector may appear more
 * than once (a base rule plus a density-regime override); exactly one of those
 * rules is expected to carry the property, so an accidental second declaration
 * — the shape that makes a cascade question ambiguous — fails here rather than
 * being silently resolved.
 */
function declaration(selector: string, property: string): string {
  const declarations = rules
    .filter((rule) => rule.selector === selector)
    .flatMap((rule) => [
      ...rule.declarations.matchAll(
        new RegExp(`(?:^|;)\\s*${property}:\\s*([^;]+);`, "g"),
      ),
    ])
    .map((match) => match[1].replace(/\s+/g, " ").trim());

  if (declarations.length !== 1) {
    throw new Error(
      `Expected exactly one ${property} for ${selector}, found ${declarations.length}`,
    );
  }
  return declarations[0];
}

describe("workspace action bar rhythm (elspeth-fca731fb28)", () => {
  // Every gap in the bar has to move together. Lowering only the bar's own gap
  // left the group BOUNDARIES at 4px while the gaps INSIDE the status and
  // completion groups stayed at 8px, so the eye bonded "Audit N issues" to
  // "Share inspect link" at exactly the viewport where the bar is most crowded.
  const gapBearingSelectors = [
    ".workspace-action-bar",
    ".workspace-action-bar .completion-bar",
  ];

  it.each(gapBearingSelectors)("drives %s's gap from one property", (selector) => {
    expect(declaration(selector, "gap")).toBe("var(--workspace-bar-gap)");
  });

  it("tightens every gap and standoff together in the density regime", () => {
    const densityBlock = /@media \(min-width: 961px\) and \(max-height: 800px\) \{([\s\S]*)\n\}/.exec(
      cssWithoutComments,
    );
    expect(densityBlock).not.toBeNull();

    // The regime must retune the shared properties, not individual rules —
    // an override that reaches only .workspace-action-bar is the defect.
    expect(densityBlock![1]).toContain("--workspace-bar-gap: var(--space-xs);");
    expect(densityBlock![1]).toContain(
      "--workspace-bar-inset: var(--space-xs);",
    );
    expect(densityBlock![1]).not.toContain(".workspace-action-bar");
    expect(densityBlock![1]).not.toContain(".workspace-collapse-control");
  });

  it("gives both pane toggles the shared icon chrome at the 44px register", () => {
    // Recut 2026-08-15: the toggles render variant="bare", so
    // .workspace-pane-toggle is their ONLY chrome. It must carry the full
    // --size-control register on BOTH axes (the collapse control's
    // registration contract needs the height; icon-only needs the width for
    // the 44px target the geometry e2e hit-tests), and it must NOT recreate
    // the .btn surface fill that made the old control read as a sixth
    // action-bar button — transparent ground, hover-only wash.
    const toggle = ".workspace-pane-toggle";
    expect(declaration(toggle, "min-width")).toBe("var(--size-control)");
    expect(declaration(toggle, "min-height")).toBe("var(--size-control)");
    expect(declaration(toggle, "background-color")).toBe("transparent");
    // The border is the toggle's ONLY resting boundary (transparent fill), so
    // WCAG 1.4.11 applies to it directly: --color-border-strong is ~1.7:1 on
    // --color-surface in both themes (2026-08-15 a11y audit), which is why
    // the toggles borrow --color-input-border — the token M04 created for
    // exactly this defect on form inputs. colorContrast.test.ts proves the
    // token clears 3:1; this pin proves the toggles actually use it.
    expect(declaration(toggle, "border")).toBe(
      "1px solid var(--color-input-border)",
    );
    expect(
      declaration(`${toggle}:hover:where(:not(:disabled))`, "background-color"),
    ).toBe("var(--color-surface-hover)");
  });

  it("takes the bar's padding and the collapse control's standoff from one inset", () => {
    // The two rows meet across the pane divider and must share one bottom edge
    // and one optical center — a BLOCK-axis contract: both sides express the
    // standoff as --workspace-bar-inset, so it no longer depends on a 16px
    // browser root. The bar's INLINE inset is the artifact column's shared
    // --artifact-gutter (elspeth-87195dda2c); the collapse control lives in
    // the authoring column, which keeps its own --space-sm inline margin.
    expect(declaration(".workspace-action-bar", "padding")).toBe(
      "var(--workspace-bar-inset) var(--artifact-gutter)",
    );
    expect(declaration(".workspace-collapse-control", "margin")).toBe(
      "var(--workspace-bar-inset) var(--space-sm)",
    );
  });
});

describe("workspace completion group (recut 2026-08-15, supersedes elspeth-c6fd722d2f)", () => {
  const group = ".workspace-action-bar .completion-bar";

  it("isolates Run at the right edge from content-sized members", () => {
    // The equal-width co-equal contract retired 2026-08-15 (operator
    // decision): Run pipeline sits alone at the bar's right edge, registered
    // with the toolbar's Focus graph button through the shared
    // --artifact-gutter inline inset, while Save/Import cluster left. The
    // group is the bar's filler and its flexible third column is the void
    // that isolates Run — with the reason line in it when Run is gated,
    // empty when it is not — so Run is pinned to the FOURTH column rather
    // than auto-placed, or the ungated state would seat it beside Import.
    // Members are auto tracks at their content width (justify-items: start),
    // so the 2560 slab overshoot the old cap existed for cannot recur — which
    // is also why the cap itself is gone.
    expect(declaration(group, "display")).toBe("grid");
    expect(declaration(group, "grid-template-columns")).toBe("auto auto 1fr auto");
    expect(declaration(group, "justify-items")).toBe("start");
    expect(declaration(`${group} .side-rail-execute-btn`, "grid-column")).toBe("4");
    expect(declaration(`${group} .side-rail-execute-reason`, "grid-column")).toBe("3");
    expect(declaration(`${group} > *`, "width")).toBe("auto");
    expect(ruleFor(group)).not.toContain("max-width");
    // No auto margins survive from the flex row: the column is the void.
    for (const rule of rules.filter((candidate) => candidate.selector.startsWith(group))) {
      expect(rule.declarations).not.toMatch(/margin[a-z-]*:\s*auto/);
    }
  });

  it("keeps the reason beside Run and lets it absorb the squeeze (elspeth-b4e88f0f8c)", () => {
    // A wrapping flex row broke lines by each item's MAX-content, so the whole
    // group moved under the chips before the reason line had yielded a pixel
    // and no measure cap could change that. In a one-row grid the reason and
    // Run cannot part, the reason's 1fr column is the only thing that shrinks,
    // and the group's minimum size — buttons plus the reason's 16ch floor —
    // is what the OUTER bar reads when it decides whether the group fits
    // beside the chips, because flex-basis 0 presents the automatic minimum
    // rather than the max-content. Basis auto (the previous value) wrapped the
    // group at 1280 unconditionally.
    expect(declaration(group, "flex")).toBe("1 1 0");
    expect(ruleFor(group)).not.toMatch(/(?:^|;)\s*min-width:/);
    const reason = `${group} .side-rail-execute-reason`;
    expect(declaration(reason, "min-width")).toBe("16ch");
    expect(declaration(reason, "max-width")).toBe("44ch");
    expect(declaration(reason, "justify-self")).toBe("end");
    expect(declaration(reason, "text-align")).toBe("end");
    // The reason-to-Run gap is one grid gap plus one, keyed on the adjacency
    // ExecuteButton's DOM order guarantees (the <p> precedes the button).
    expect(
      declaration(`${reason} + .side-rail-execute-btn`, "margin-inline-start"),
    ).toBe("var(--workspace-bar-gap)");
  });

  it("keeps the bar's composition stable when the middle group is absent", () => {
    // space-between would change the arrangement depending on whether a
    // completion bar rendered. flex-start keeps status and the
    // Save/Import cluster as one left-anchored run; the completion group's
    // own grow and its flexible column (above) are what hold the right edge,
    // and they do it identically whether or not the status cluster renders.
    expect(declaration(".workspace-action-bar", "justify-content")).toBe(
      "flex-start",
    );
  });

  it("tints settled Checks-badge verdicts with the matching semantic pair", () => {
    // Recut 2026-08-15, relocated with the chips-to-Checks-tab move: the
    // settled verdict surface is now the Checks tab's glyph badge. Each tone
    // pairs the banner-family alpha background with the SAME family's text
    // colour, so fill and glyph cannot disagree about the verdict (the
    // glyph — count, checkmark, or bang — stays the information carrier,
    // WCAG 1.4.1). busy and neutral deliberately stay dots on the base
    // badge rule: in-flight and not-yet-checked are neither good nor bad.
    const glyph = ".artifact-tab-badge--glyph";
    expect(declaration(`${glyph}[data-tone="success"]`, "background-color")).toBe(
      "var(--color-success-bg)",
    );
    expect(declaration(`${glyph}[data-tone="success"]`, "color")).toBe(
      "var(--color-success)",
    );
    expect(declaration(`${glyph}[data-tone="warning"]`, "background-color")).toBe(
      "var(--color-warning-bg)",
    );
    expect(declaration(`${glyph}[data-tone="warning"]`, "color")).toBe(
      "var(--color-warning)",
    );
    expect(declaration(`${glyph}[data-tone="error"]`, "background-color")).toBe(
      "var(--color-error-bg)",
    );
    expect(declaration(`${glyph}[data-tone="error"]`, "color")).toBe(
      "var(--color-error)",
    );
    expect(cssWithoutComments).not.toContain(`${glyph}[data-tone="busy"]`);
    expect(cssWithoutComments).not.toContain(`${glyph}[data-tone="neutral"]`);
  });

  it("centers the members instead of stretching them to the bar's height", () => {
    // elspeth-929fc5d4a7: .completion-bar carries align-items: stretch for its
    // vertical rail layout, which in this row context stretched each button to
    // the bar's full height — a 98px slab whenever the bar wrapped. And since
    // the reason line can make the group taller than a control row, the bar
    // must centre its groups too (elspeth-b4e88f0f8c): the buttons sit at
    // (H - 44) / 2 inside the group, and only a centred status group lands
    // at the same offset. flex-start was right while the group's buttons sat
    // at ITS top (elspeth-15e8cff7a5); it would now misregister the chips by
    // the same half-difference from the other side.
    expect(declaration(group, "align-items")).toBe("center");
    expect(declaration(".workspace-action-bar", "align-items")).toBe("center");
  });
});

describe("workspace stacking order (elspeth-cf4b08e271)", () => {
  it("declares no raw z-index integers", () => {
    // The file's five literals (2/3/3/4/5) were written against a scale whose
    // lowest rung is 10, so the token scale could not reach them — and the
    // literal 4 put the open drawer BELOW .graph-a11y-list's
    // var(--z-panel-controls), which painted straight through it.
    const values = [
      ...cssWithoutComments.matchAll(/z-index:\s*([^;]+);/g),
    ].map((match) => match[1].trim());

    expect(values.length).toBeGreaterThan(0);
    expect(values.filter((value) => !/^var\(--z-[\w-]+\)$/.test(value))).toEqual(
      [],
    );
  });

});

describe("workspace bottom edge (elspeth-215c989bed, elspeth-9c94a58500)", () => {
  // The bottom rule of the application was drawn by two elements in two grid
  // columns — the action bar in the artifact column and a mirrored band on the
  // authoring pane — and every way of making them the same height failed in
  // turn: a token formula (elspeth-215c989bed) that encoded "the bar is one
  // control row" and broke when the block-reason line made it 87px
  // (elspeth-97db9c22e5), then a measured height that a ResizeObserver kept
  // publishing. The restructure retires the seam itself: ONE grid item spans
  // both columns and paints the rule and the fill once, and its height is its
  // content because it is a content-sized grid row. These pins are the shape
  // of that guarantee as it reads in the stylesheet; the rendered facts — the
  // rule spanning the full width, the collapse control registered with the
  // bar's controls through a wrap — are asserted in
  // composer-workspace-geometry.spec.ts.
  const slot = ".workspace-action-bar-slot";

  it("paints the rule and the fill once, on one item spanning both columns", () => {
    expect(declaration(slot, "grid-column")).toBe("1 / -1");
    expect(declaration(slot, "grid-template-columns")).toBe("subgrid");
    expect(declaration(slot, "border-top")).toBe(
      "1px solid var(--color-border, currentcolor)",
    );
    expect(declaration(slot, "background")).toBe("var(--color-surface, Canvas)");

    // The bar itself must NOT draw them too, or the bottom edge is back to
    // having two owners — the exact shape being retired.
    expect(ruleFor(".workspace-action-bar")).not.toMatch(
      /(?:^|;)\s*border[a-z-]*:/,
    );
    expect(ruleFor(".workspace-action-bar")).not.toMatch(
      /(?:^|;)\s*background[a-z-]*:/,
    );

    // And nothing may draw a second band on the authoring side again.
    expect(rules.some((rule) => rule.selector.includes(".workspace-authoring-pane::"))).toBe(
      false,
    );
  });

  it("gives the bar row no height of its own — it is content-sized, in every layout", () => {
    // No height, no min-height, no measured custom property: the row is the
    // grid's `auto` track and is exactly as tall as the taller of its two
    // cells. A height on the wrapper, however derived, is the seam coming
    // back.
    expect(ruleFor(slot)).not.toMatch(/(?:^|;)\s*(?:min-|max-)?height:/);
    expect(cssWithoutComments).not.toContain("--workspace-action-bar-height");

    // Desktop: panes row, then the bar row. Narrow: tabs, view, then the bar
    // row — placed on the third track explicitly.
    expect(declaration(".composer-workspace", "grid-template-rows")).toBe(
      "minmax(0, 1fr) auto",
    );
    expect(declaration(slot, "grid-row")).toBe("2");
    expect(
      declaration('.composer-workspace[data-layout-mode="narrow"]', "grid-template-rows"),
    ).toBe("auto minmax(0, 1fr) auto");
    expect(
      declaration(`.composer-workspace[data-layout-mode="narrow"] ${slot}`, "grid-row"),
    ).toBe("3");
  });

  it("seats the collapse control in the row's authoring cell, centred like the bar's controls", () => {
    // The « control is a child of the bar row now (it had to be: in narrow
    // Compose view the action bar is hidden and the control alone gives the
    // row its height). Column 1 is the authoring cell. Its block alignment
    // must be the SAME as the bar's cross-axis alignment: the bar centres its
    // groups against a reason-tall completion group (elspeth-b4e88f0f8c), so
    // a start-aligned control would sit above the buttons by half the
    // difference — the seam misregistration again, one cell over.
    expect(declaration(".workspace-collapse-control", "grid-column")).toBe("1");
    expect(declaration(".workspace-collapse-control", "align-self")).toBe("center");
    expect(declaration(".workspace-action-bar", "align-items")).toBe("center");
    expect(declaration(".workspace-action-bar-main", "grid-column")).toBe("2");
  });

  it("applies the standoff exactly once on each side of the seam", () => {
    // The ticket's own warning: if the wrapper ALSO carried the inset, the
    // density regime's --workspace-bar-inset override would tighten it twice.
    // The bar's padding and the control's margin are the only two carriers
    // (both pinned to the token in the rhythm describe above); neither the
    // wrapper nor the artifact cell has any, so no rule pads it.
    expect(ruleFor(slot)).not.toMatch(/(?:^|;)\s*padding[a-z-]*:/);
    expect(
      rules
        .filter((rule) => rule.selector.split(",").some((s) => s.trim().endsWith(".workspace-action-bar-main")))
        .map((rule) => rule.declarations)
        .join(""),
    ).not.toMatch(/(?:^|;)\s*padding[a-z-]*:/);
  });
});

describe("workspace pane separator (elspeth-ffb96f0b95)", () => {
  it("draws the seam as a real one-pixel box on a whole device pixel", () => {
    // It was a border on a ZERO-width box offset by half a pixel, so it
    // rendered blurred at 1x. The hit area is 24px, so left: 50% lands the
    // line on a whole pixel.
    expect(declaration(".workspace-separator::before", "width")).toBe("1px");
    expect(declaration(".workspace-separator::before", "left")).toBe("50%");
    expect(ruleFor(".workspace-separator::before")).not.toMatch(
      /(?:^|;)\s*border[a-z-]*:/,
    );
  });

  it("gives the seam and its grip a contrast-justified token, not the hairline", () => {
    // --color-border is 12% alpha, ~1.1:1 against the surfaces either side.
    // This is an interactive control, so WCAG 1.4.11 applies and the token
    // tokens.css justifies for exactly this case is the right one.
    for (const selector of [
      ".workspace-separator::before",
      ".workspace-separator::after",
    ]) {
      expect(declaration(selector, "background")).toBe(
        "var(--color-input-border)",
      );
    }
  });

  it("carries hover and active states for both the seam and the grip", () => {
    // Seven rules matched this control before and not one was a state
    // selector: a 24px col-resize hit area whose only feedback was the cursor.
    const stateRules = rules.filter(
      (rule) =>
        rule.selector.startsWith(".workspace-separator:hover") ||
        rule.selector.includes(", .workspace-separator:active"),
    );
    expect(stateRules).toHaveLength(2);
    for (const rule of stateRules) {
      expect(rule.selector).toContain(":hover");
      expect(rule.selector).toContain(":active");
      expect(rule.declarations).toContain(
        "background: var(--color-text-secondary);",
      );
    }
  });
});

describe("artifact column gutter (elspeth-87195dda2c)", () => {
  // With the authoring pane collapsed, the artifact column IS the page, and
  // its surfaces used to crowd the viewport edge on three different left
  // margins (strip ~10, tabs ~14, panel content ~8). The fix is one shared
  // property; these assertions pin that every gutter-bearing inset READS it,
  // so retuning the gutter moves all of them together and none can drift
  // back to a private literal.
  it("defines the gutter once on the workspace root", () => {
    expect(declaration(".composer-workspace", "--artifact-gutter")).toBe(
      "var(--space-lg)",
    );
  });

  it.each([
    [".artifact-workspace-toolbar", "padding", "0 var(--artifact-gutter)"],
    [
      ".workspace-action-bar",
      "padding",
      "var(--workspace-bar-inset) var(--artifact-gutter)",
    ],
    [
      ".artifact-workspace-panel .inline-run-results",
      "padding-inline",
      "var(--artifact-gutter)",
    ],
    [".pipeline-spec-view", "padding", "var(--space-md) var(--artifact-gutter)"],
  ])("%s takes its inline inset from the gutter", (selector, property, expected) => {
    expect(declaration(selector, property)).toBe(expected);
  });

});
