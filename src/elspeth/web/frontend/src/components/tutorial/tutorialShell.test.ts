import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Constraint tests for the first-run tutorial shell, from the 2026-08-14
// professionalisation review (elspeth-0c11a9cf90, elspeth-4603f4a432,
// elspeth-28bb719b47, elspeth-4da8113ac3, elspeth-651ef08d53,
// elspeth-8dfa1fd709, elspeth-58d45762d8, elspeth-e2d19ae400).
//
// The tutorial shell was written before the shared primitives and never
// migrated, so its failures were all of one kind: a hand-rolled construction
// that LOOKED like the system's and silently diverged from it. None of them are
// caught by asserting that a declaration exists — each one below either resolves
// a CASCADE question (which declaration reaches the element) or checks a
// RELATIONSHIP that two values chosen together have to satisfy, so a future edit
// that moves one value in isolation reddens here.
//
// Barrel-loaded in cascade order, exactly like styles/primitiveGeometry.test.ts
// and chat/chatBubbleGutter.test.ts — the tutorial's rules and the primitives'
// live in different files, and half of what is asserted here is agreement
// ACROSS that boundary. Import paths resolve against the BARREL's directory,
// not this file's; cwd-relative per the tokenReferences.test.ts idiom (vitest
// runs from the frontend root).
const barrelDir = "src/styles";
const stylesheetBarrel = readFileSync(join(barrelDir, "index.css"), "utf8");
const appCss = Array.from(stylesheetBarrel.matchAll(/@import\s+"(?<path>[^"]+)";/g))
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

/**
 * Flat rule list in barrel order. Rules nested in @media / @keyframes are
 * captured as their own entries (an at-rule prelude never matches, because a
 * block containing `{` cannot satisfy `[^{}]*`), so a state that exists only
 * inside a media query is inspected too.
 */
function parseRules(css: string): Rule[] {
  return Array.from(stripComments(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)).map((match) => ({
    selectors: match[1].split(",").map((selector) => selector.trim()),
    declarations: match[2],
  }));
}

const rules = parseRules(appCss);

/** EVERY value declared for `property` by a rule listing `selector` verbatim. */
function declaredValues(selector: string, property: string): string[] {
  const found: string[] = [];
  for (const rule of rules) {
    if (!rule.selectors.includes(selector)) continue;
    const match = new RegExp(`(?:^|;)\\s*${property}\\s*:([^;]+)`).exec(rule.declarations);
    if (match) found.push(match[1].trim());
  }
  return found;
}

/**
 * The LAST value declared for `property` by a rule listing `selector` verbatim,
 * in barrel order — i.e. which declaration actually reaches the element among
 * the equal-specificity rules that name it, not merely whether the text exists.
 */
function declaredValue(selector: string, property: string): string {
  const found = declaredValues(selector, property);
  if (found.length === 0) {
    throw new Error(`No ${property} declared for ${selector}`);
  }
  return found[found.length - 1];
}

const tokensCss = stripComments(readFileSync(join(barrelDir, "tokens.css"), "utf8"));

function tokenPx(name: string): number {
  const match = new RegExp(`${name}:\\s*(-?[\\d.]+)px\\s*;`).exec(tokensCss);
  if (!match) {
    throw new Error(`No px value for ${name} in tokens.css`);
  }
  return Number.parseFloat(match[1]);
}

/** Resolve a length written as a bare px value or as a single px token. */
function px(value: string): number {
  const token = /^var\(\s*(--[\w-]+)\s*\)$/.exec(value.trim());
  if (token) return tokenPx(token[1]);
  const literal = /^(-?[\d.]+)px$/.exec(value.trim());
  if (literal) return Number.parseFloat(literal[1]);
  throw new Error(`Unsupported length expression: ${value}`);
}

/** The single custom property a value references, e.g. out of a border shorthand. */
function tokenIn(value: string): string {
  const found = Array.from(value.matchAll(/var\(\s*(--[\w-]+)\s*\)/g)).map((m) => m[1]);
  if (found.length !== 1) {
    throw new Error(`Expected exactly one custom property in "${value}", found ${found.length}`);
  }
  return found[0];
}

describe("the progress row spans the card it reports on (elspeth-0c11a9cf90)", () => {
  // .tutorial-shell is `align-items: center`, so an unwidthed .tutorial-progress
  // shrink-to-fits and centres beside the 920px card instead of over it — and
  // .tutorial-exit-button's `margin-left: auto` then has no free space to push
  // against, so a PERSISTENT chrome control changes position turn by turn.
  const bookendProgress = ".tutorial-shell:not(.tutorial-shell--guided) .tutorial-progress";

  it("adopts the card's own width expression, not a width of its own", () => {
    expect(
      declaredValue(bookendProgress, "width"),
      "the tracker must span the card it labels — move both widths together or neither",
    ).toBe(declaredValue(".tutorial-turn", "width"));
  });

  it("gives the right-anchored exit control free space to push against", () => {
    // Both halves of the mechanism, so neither can be removed on its own.
    expect(declaredValue(".tutorial-exit-button", "margin-left")).toBe("auto");
    expect(declaredValues(bookendProgress, "width")).toHaveLength(1);
  });

  it("leaves the guided step's full-bleed band uncapped", () => {
    // The guided override (tutorial.css `.tutorial-shell--guided
    // .tutorial-progress`) sets flex-shrink and padding but no width, and the
    // guided shell is `align-items: stretch` — it is the one step that already
    // right-anchored correctly. An unscoped width on the base selector would
    // reach it and cap it at the card width.
    expect(
      declaredValues(".tutorial-progress", "width"),
      "a width on the base selector reaches the guided step too — keep it :not()-scoped",
    ).toEqual([]);
    expect(declaredValues(".tutorial-shell--guided .tutorial-progress", "width")).toEqual([]);
  });
});

describe("the tutorial's link control stays in step with .link-button (elspeth-4603f4a432)", () => {
  // styles/shared.css documents `.link-button` as having replaced
  // `.tutorial-link-button`, but six tutorial call sites still name the older
  // class. Until they are migrated the two must not diverge — before this,
  // .tutorial-link-button had no :hover rule anywhere (six secondary actions
  // dead on hover) and no border-radius (a square focus ring).
  it("shapes its focus ring like the primitive's", () => {
    expect(declaredValue(".tutorial-link-button", "border-radius")).toBe(
      declaredValue(".link-button", "border-radius"),
    );
  });

  it("carries the primitive's hover cue, and only on hover", () => {
    expect(declaredValue(".tutorial-link-button:hover", "text-decoration-thickness")).toBe(
      declaredValue(".link-button:hover", "text-decoration-thickness"),
    );
    // A hover cue that is also the resting value is not a cue.
    expect(declaredValues(".tutorial-link-button", "text-decoration-thickness")).toEqual([]);
  });

  it("keeps the hit area the primitive deliberately does not floor", () => {
    // `.link-button` is padding:0 with no min-height; a naive class swap at the
    // six call sites would shrink every one of them.
    expect(px(declaredValue(".tutorial-link-button", "min-height"))).toBe(
      tokenPx("--size-control"),
    );
  });
});

describe("chrome-row controls sit on the compact rung (elspeth-4603f4a432, elspeth-28bb719b47)", () => {
  it("drops the exit control to the chrome rung inside the dense progress band", () => {
    const exit = px(declaredValue(".tutorial-exit-button", "min-height"));
    expect(exit).toBe(tokenPx("--size-control-compact"));
    expect(
      exit,
      "the exit control steps DOWN off the canvas floor — that is the point of the rule",
    ).toBeLessThan(px(declaredValue(".tutorial-link-button", "min-height")));
    // Still a real target: WCAG 2.5.8 (AA) is the floor the compact rung trades
    // 2.5.5 (AAA) for, and tokens.css sanctions that trade for chrome rows.
    expect(exit).toBeGreaterThanOrEqual(24);
  });

  it("puts the hash copy control on a declared rung instead of below both", () => {
    // It declared no height at all and rendered ~30px — below --size-control
    // AND --size-control-compact, i.e. on no rung the system declares.
    expect(px(declaredValue(".tutorial-hash-copy", "min-height"))).toBe(
      tokenPx("--size-control-compact"),
    );
    expect(declaredValue(".tutorial-hash-copy", "border-radius")).toBe(
      declaredValue(".btn-compact", "border-radius"),
    );
  });

  it("changes the border as well as the fill on hover, over a transition", () => {
    // The defect: hover swapped the fill only, with no transition, so the
    // control felt less responsive than every other button in the product.
    const restBorder = tokenIn(declaredValue(".tutorial-hash-copy", "border"));
    const hoverBorder = tokenIn(declaredValue(".tutorial-hash-copy:hover", "border-color"));
    expect(hoverBorder, "hover must move the border, not only the fill").not.toBe(restBorder);
    expect(declaredValue(".tutorial-hash-copy:hover", "background-color")).not.toBe(
      declaredValue(".tutorial-hash-copy", "background-color"),
    );
    const transition = declaredValue(".tutorial-hash-copy", "transition");
    for (const property of ["background-color", "border-color"]) {
      expect(transition, `${property} changes on hover, so it must transition`).toContain(
        property,
      );
    }
  });
});

describe("boxed notes move one way off the card (elspeth-4da8113ac3)", () => {
  it("lifts the teaching callout onto the same surface as its sibling notes", () => {
    // --color-surface-input is byte-identical to --color-bg in the dark theme,
    // so the callout RECESSED below the card and read as a disabled text field
    // — while every sibling boxed note in the file lifted. On Turn 4 the two
    // idioms stacked and moved in opposite directions.
    const sibling = declaredValue(".tutorial-layer", "background");
    expect(declaredValue(".tutorial-callout", "background")).toBe(sibling);
    expect(declaredValue(".tutorial-audit-list div", "background")).toBe(sibling);
    expect(declaredValue(".tutorial-mode-fieldset label", "background")).toBe(sibling);
    expect(
      declaredValue(".tutorial-callout", "background"),
      "a note painted the card's own surface has no boxed-note idiom left",
    ).not.toBe(declaredValue(".tutorial-turn", "background"));
  });
});

describe("the first-run card's elevation tracks the theme (elspeth-651ef08d53)", () => {
  it("resolves its shadow through a token rather than a raw value", () => {
    const shadow = declaredValue(".tutorial-turn", "box-shadow");
    expect(
      shadow,
      "a literal rgba() shadow cannot be theme-paired and is invisible to the token system",
    ).toMatch(/^var\(\s*--shadow-[\w-]+\s*\)$/);
  });

  it("uses a shadow token the light theme actually re-points", () => {
    // The point of tokenising: the raw value shipped dark-theme strength
    // against the light ground. A token that only the dark block defines would
    // reproduce exactly that.
    const name = tokenIn(declaredValue(".tutorial-turn", "box-shadow"));
    const lightBlock = /\[data-theme="light"\]\s*\{([\s\S]*?)\n\}/.exec(tokensCss);
    expect(lightBlock, "could not find the light token block in tokens.css").not.toBeNull();
    expect(lightBlock![1]).toContain(`${name}:`);
  });
});

describe("the disclosure frame agrees with the AlertBanner it decorates (elspeth-8dfa1fd709)", () => {
  // The frame was an unconditional `border: 1px solid var(--color-info-border)`
  // at (0,2,0), outranking the primitive's own tone rules at (0,1,0) — so the
  // welcome screen's error-tone banner (AlertBanner's DEFAULT tone, which
  // `startDisabledReason` renders through) shipped red text on
  // --color-error-bg inside a cyan frame with an info glyph.
  it("frames the primitive's default tone in the primitive's default colour", () => {
    expect(tokenIn(declaredValue(".tutorial-disclosure.alert-banner", "border"))).toBe(
      tokenIn(declaredValue(".alert-banner", "border-bottom")),
    );
  });

  it.each(["info", "warning", "success"])(
    "re-points the frame for the %s tone exactly as the primitive does",
    (tone) => {
      // Total by construction: AlertBanner.tsx emits error (no modifier) plus
      // these three, and each one is checked against the primitive's own
      // mapping — so a tone can never fall back to another tone's frame.
      expect(
        tokenIn(declaredValue(`.tutorial-disclosure.alert-banner--${tone}`, "border-color")),
      ).toBe(tokenIn(declaredValue(`.alert-banner--${tone}`, "border-bottom-color")));
    },
  );
});

describe("no glyph is grown out of CSS (elspeth-58d45762d8)", () => {
  it("keeps every content: string in the tutorial stylesheet empty", () => {
    // The ⓘ injected by `.tutorial-disclosure.alert-banner::before` was the only
    // non-empty CSS content: string in the whole stylesheet corpus. Being
    // CSS-injected it was invisible to anyone reading the component and could
    // not vary with the banner's tone. The rendering-only `content: ""` on
    // ::after pseudo-elements is what remains.
    const declared = parseRules(readFileSync("src/components/tutorial/tutorial.css", "utf8"))
      .flatMap((rule) =>
        Array.from(rule.declarations.matchAll(/(?:^|;)\s*content\s*:([^;]+)/g)).map((match) =>
          match[1].trim(),
        ),
      );
    expect(declared.every((value) => value === '""')).toBe(true);
  });
});

describe("the layer micro-labels read as labels (elspeth-e2d19ae400)", () => {
  it("sizes the uppercase label on the micro-label rung, not at body size", () => {
    // With no font-size it inherited 16px, so SENSE/DECIDE/ACT out-shouted the
    // 22px h2 on the very first screen, with tracking calibrated for 12-13px.
    const label = px(declaredValue(".tutorial-layer strong", "font-size"));
    expect(label).toBe(tokenPx("--font-size-xs"));
    expect(label, "12px is the declared type floor").toBeGreaterThanOrEqual(12);
    expect(
      label,
      "a micro-label must sit below the body text it labels",
    ).toBeLessThan(tokenPx("--font-size-base"));
  });

  it("separates the label from its explanatory line tonally", () => {
    expect(
      declaredValue(".tutorial-layer span", "color"),
      "sharing one colour collapses the label/body hierarchy the markup declares",
    ).not.toBe(declaredValue(".tutorial-layer strong", "color"));
  });
});
