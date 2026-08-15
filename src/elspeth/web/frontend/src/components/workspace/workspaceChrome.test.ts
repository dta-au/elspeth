import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Structural contracts for the workspace chrome — the pane seam, the bottom
// action bar and the inspector drawer — from the 2026-08-14 professionalisation
// review.
//
// These are deliberately NOT "the file contains this string" tests. Each one
// pins a relationship that two or more declarations have to agree on, because
// every defect in this batch was two values that were CHOSEN together and then
// drifted apart: the drawer's width against the strip the pane reserved for it,
// the bar's outer gap against the gaps inside its groups, the authoring-side
// band against the action bar it has to meet across the divider. A single
// declaration read back as text would have passed while all three were broken.
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

describe("workspace inspector drawer (elspeth-4e54200207)", () => {
  it("reserves the drawer's strip from the same measure the drawer is drawn at", () => {
    // The drawer used to be an absolutely-positioned overlay spanning the full
    // workspace height with no scrim and nothing on the artifact side
    // reserving for it, so it sliced the primary action's box mid-word and hid
    // "Focus Graph" entirely — over controls that stayed live and clickable.
    // The reserve is only correct while it is the SAME measure as the drawer,
    // so read both out of the stylesheet and compare rather than pinning 512px
    // twice.
    expect(declaration(".workspace-inspector", "width")).toBe(
      "min(var(--workspace-inspector-width), 100%)",
    );

    const reserve = rules.filter(
      (rule) =>
        rule.selector.includes(".workspace-inspector:not([hidden])") &&
        rule.selector.endsWith(".workspace-artifact-pane"),
    );
    expect(reserve).toHaveLength(1);
    expect(reserve[0].declarations).toContain(
      "padding-right: var(--workspace-inspector-width);",
    );

    // Narrow mode moves the slot to the full width and the drawer becomes the
    // view, so the reserve must not apply there.
    expect(reserve[0].selector).toContain(':not([data-layout-mode="narrow"])');
  });

  it("compresses both drawer bands with the shared workspace band token", () => {
    // elspeth-7bd392c0dc: the bands were hard-built from 8px padding plus a
    // 36px control, so the density regime compressed the artifact toolbar
    // beside them to 45px while they stayed at 53. Same construction as
    // .artifact-workspace-toolbar: height from the token, no vertical padding.
    const bands = ".workspace-inspector-header, .workspace-inspector-tabs";
    expect(declaration(bands, "min-height")).toBe(
      declaration(".artifact-workspace-toolbar", "min-height"),
    );
    expect(declaration(bands, "padding")).toBe(
      declaration(".artifact-workspace-toolbar", "padding"),
    );
  });

  it("keeps the inspector title on the type scale with no UA margins (elspeth-3e23bed130)", () => {
    // The title was the app's only bare <h2> — UA 24px with 19.92px block
    // margins, inflating the header band to 93px against the 45px artifact
    // toolbar one seam away. The band's height must stay owned by
    // --size-workspace-band, so the title contributes no margins and sits on
    // the product type scale (the .graph-modal-header h2 recipe, common.css).
    expect(declaration(".workspace-inspector-header h2", "margin")).toBe("0");
    expect(declaration(".workspace-inspector-header h2", "font-size")).toBe(
      "var(--font-size-lg)",
    );
    expect(declaration(".workspace-inspector-header h2", "line-height")).toBe(
      "var(--line-height-heading)",
    );
  });
});

describe("workspace action bar rhythm (elspeth-fca731fb28)", () => {
  // Every gap in the bar has to move together. Lowering only the bar's own gap
  // left the group BOUNDARIES at 4px while the gaps INSIDE the status and
  // completion groups stayed at 8px, so the eye bonded "Audit N issues" to
  // "Save for review" at exactly the viewport where the bar is most crowded.
  const gapBearingSelectors = [
    ".workspace-action-bar",
    ".workspace-action-bar .completion-bar",
    ".workspace-status-controls",
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
  it("isolates Run at the right edge from content-sized members", () => {
    // The equal-width co-equal contract retired 2026-08-15 (operator
    // decision): Run pipeline sits alone at the bar's right edge, registered
    // with the toolbar's Focus graph button through the shared
    // --artifact-gutter inline inset, while Save/Import cluster left. The
    // group is the bar's filler and the auto margin on the Run button is
    // what spends the free space as the isolating void. Members size to
    // content (no grow), so the 2560 slab overshoot the old cap existed for
    // cannot recur — which is also why the cap itself is gone.
    const group = ".workspace-action-bar .completion-bar";
    expect(declaration(group, "flex")).toBe("1 1 auto");
    expect(declaration(`${group} > *`, "flex")).toBe("0 0 auto");
    expect(
      declaration(`${group} .side-rail-execute-btn`, "margin-inline-start"),
    ).toBe("auto");
    expect(ruleFor(group)).not.toContain("max-width");
  });

  it("keeps the bar's composition stable when the middle group is absent", () => {
    // space-between would change the arrangement depending on whether guided
    // mode rendered a completion bar. flex-start keeps status and the
    // Save/Import cluster as one left-anchored run; the completion group's
    // own grow + Run's auto margin (above) are what hold the right edge, and
    // they do it identically whether or not the status cluster renders.
    expect(declaration(".workspace-action-bar", "justify-content")).toBe(
      "flex-start",
    );
  });

  it("tints settled status-chip verdicts with the matching semantic pair", () => {
    // Recut 2026-08-15: success/warning/error chips carry the banner-family
    // alpha background beside their border so the good/bad verdict reads at
    // a glance (text stays the information carrier, WCAG 1.4.1). busy and
    // neutral deliberately stay untinted — in-flight and not-yet-checked are
    // neither good nor bad. The pair must come from the SAME semantic family
    // or the chip's border and fill would disagree about the verdict.
    const chip = ".workspace-status-control";
    expect(declaration(`${chip}[data-tone="success"]`, "background-color")).toBe(
      "var(--color-success-bg, transparent)",
    );
    expect(declaration(`${chip}[data-tone="success"]`, "border-color")).toContain(
      "--color-success",
    );
    expect(declaration(`${chip}[data-tone="warning"]`, "background-color")).toBe(
      "var(--color-warning-bg, transparent)",
    );
    expect(declaration(`${chip}[data-tone="error"]`, "background-color")).toBe(
      "var(--color-error-bg, transparent)",
    );
    expect(ruleFor(`${chip}[data-tone="busy"]`)).not.toContain("background");
  });

  it("centers the members instead of stretching them to the bar's height", () => {
    // elspeth-929fc5d4a7: .completion-bar carries align-items: stretch for its
    // vertical rail layout, which in this row context stretched each button to
    // the bar's full height — a 98px slab whenever the bar wrapped.
    expect(
      declaration(".workspace-action-bar .completion-bar", "align-items"),
    ).toBe("center");
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

  it("puts the drawer's slot no lower than the panel controls it must cover", () => {
    // The slot is the LAST child of .composer-workspace, so an equal rung is
    // resolved in its favour by tree order; a LOWER rung is the filed bug.
    expect(declaration(".workspace-inspector-slot", "z-index")).toBe(
      "var(--z-panel-controls)",
    );
  });
});

describe("workspace bottom edge (elspeth-215c989bed)", () => {
  it("builds the authoring-side band from the same values as the action bar", () => {
    // The action bar's rule and fill are a child of the artifact grid column,
    // so the bottom of the app was a half-drawn line. The mirrored band must
    // be exactly as tall as the action bar: inset + tallest control + inset.
    const band = ".workspace-authoring-pane::before";
    /* The band's height is no longer a constant that can be checked against the
       bar's constant, because the bar is no longer constant: it grows a line
       whenever .completion-bar carries ExecuteButton's block-reason text, and
       the band then missed it by 27px (elspeth-97db9c22e5). What a source-string
       test can still pin is the SHAPE of the agreement — the measured height
       published by ComposerWorkspace wins, and the token formula survives as the
       fallback for the paint before the observer fires, so it must still be the
       bar's ungated height rather than some other value. The equality itself is
       a rendered fact now and is asserted as one, in
       composer-workspace-geometry.spec.ts. */
    expect(declaration(band, "height")).toBe(
      "var(--workspace-action-bar-height, calc(2 * var(--workspace-bar-inset) + var(--size-control)))",
    );
    expect(declaration(band, "border-top")).toBe(
      declaration(".workspace-action-bar", "border-top"),
    );
    expect(declaration(band, "background")).toBe(
      declaration(".workspace-action-bar", "background"),
    );

    // The control has to paint above the band it sits on; tree order only
    // decides that while the control is itself positioned.
    expect(declaration(".workspace-collapse-control", "position")).toBe(
      "relative",
    );
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

  it("keeps the inspector bands on the same inline inset as the toolbar", () => {
    // Already pinned pairwise by the band-height test above; restated on the
    // gutter axis so a band that leaves the gutter is named by THIS failure.
    expect(
      declaration(
        ".workspace-inspector-header, .workspace-inspector-tabs",
        "padding",
      ),
    ).toBe(declaration(".artifact-workspace-toolbar", "padding"));
  });
});
