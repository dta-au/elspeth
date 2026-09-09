import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

// Elevation invariants for the header's floating surfaces (elspeth-4d38a73632).
//
// The session switcher shipped with no box-shadow at all — the only floating
// surface in the app that did not lift off the page, opening over the teal
// workspace where the separation matters most — while both header menus wrote
// their stacking layer as a bare literal (100 / 50) instead of reading the
// z-scale.
//
// The gate is structural rather than value-pinning: find every floating
// surface in this stylesheet by its own `position: absolute`, and require each
// to compose the elevation and z-index SCALES. A retuned --shadow-dropdown or
// a menu moved between documented layers stays green; a new menu that forgets
// to lift, or one that reintroduces a literal, does not.
//
// Scoped to header.css deliberately: the same divergence exists in chat.css,
// workspace.css and inspector.css, which this lane does not own. Widening the
// scan is the follow-up, not a reason to leave the header ungated.
//
// cwd-relative read per the tokenReferences.test.ts idiom; vitest runs from
// the frontend root.
const headerCss = readFileSync("src/components/header/header.css", "utf8");
const stylesheetBarrel = readFileSync("src/styles/index.css", "utf8");

function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

interface Rule {
  selectors: string[];
  declarations: string;
}

const rules: Rule[] = Array.from(
  stripComments(headerCss).matchAll(/([^{}]+)\{([^{}]*)\}/g),
).map((match) => ({
  selectors: match[1].split(",").map((selector) => selector.trim()),
  declarations: match[2],
}));

function declaredValue(rule: Rule, property: string): string | undefined {
  const match = new RegExp(`(?:^|;)\\s*${property}\\s*:([^;]+)`).exec(
    rule.declarations,
  );
  return match ? match[1].trim() : undefined;
}

/** Rules that float a surface out of flow — the ones that owe the reader an
 *  elevation cue and a documented stacking layer. */
const floatingRules = rules.filter(
  (rule) => declaredValue(rule, "position") === "absolute",
);

describe("header floating surfaces (elspeth-4d38a73632)", () => {
  it("finds the floating surfaces it claims to inspect", () => {
    expect(stylesheetBarrel).toContain(
      '@import "../components/header/header.css";',
    );
    const selectors = floatingRules.flatMap((rule) => rule.selectors);
    // The switcher's floating surface is the POPOVER wrapper, not the menu:
    // elspeth-8c7ac37d49 moved the filter strip and menu inside one
    // absolutely-positioned box hung off the trigger, so the header's height
    // contract holds while the switcher is open. The menu itself is an
    // in-flow flex child of that popover and owes no elevation of its own.
    expect(selectors).toContain(".header-session-switcher-popover");
    expect(selectors).toContain(".user-menu-list");
  });

  it("lifts every floating surface off the page from the elevation scale", () => {
    const flat = floatingRules
      .filter((rule) => {
        const shadow = declaredValue(rule, "box-shadow");
        return shadow === undefined || !/var\(--shadow-[\w-]+\)/.test(shadow);
      })
      .flatMap((rule) => rule.selectors);

    expect(
      flat,
      "floating header surfaces with no box-shadow from the elevation scale — " +
        "a menu with no shadow cannot be told from the page it opens over",
    ).toEqual([]);
  });

  it("takes every floating surface's stacking layer from the z-scale", () => {
    const literalLayers = floatingRules
      .filter((rule) => {
        const zIndex = declaredValue(rule, "z-index");
        return zIndex !== undefined && !/^var\(--z-[\w-]+\)$/.test(zIndex);
      })
      .flatMap((rule) => rule.selectors);

    expect(
      literalLayers,
      "floating header surfaces whose z-index bypasses the documented scale",
    ).toEqual([]);
  });

  // z-index is deliberately NOT compared here: the two menus sit on different
  // documented tiers, and moving one is a system-wide call that also binds the
  // chat, workspace and inspector menus. The scale test above is what holds —
  // the layer must be NAMED, whichever tier it names.
  it("gives both header menus the same corner and trigger offset", () => {
    const menus = [".header-session-switcher-popover", ".user-menu-list"].map(
      (selector) => {
        const rule = floatingRules.find((candidate) =>
          candidate.selectors.includes(selector),
        );
        if (rule === undefined) {
          throw new Error(`${selector} no longer declares position: absolute`);
        }
        return rule;
      },
    );

    for (const property of ["border-radius", "top"]) {
      const values = menus.map((rule) => declaredValue(rule, property));
      expect(values[0], `${property} differs between the two header menus`).toBe(
        values[1],
      );
      expect(values[0]).toBeDefined();
    }
  });
});
