import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

// Surface-family and elevation invariants (elspeth-9e1a4935b0, elspeth-b30aa49831,
// elspeth-44dc95153e).
//
// The 2026-08-14 professionalisation review found --color-surface and
// --color-surface-elevated BYTE-IDENTICAL (#ffffff) in the light theme, which
// made every elevation step that relies on a lighter surface a no-op, and found
// the three-family role palette collapsed there — the warm "inspection" family
// sat 0.04 L* from the cool workspace ground.
//
// colorContrast.test.ts gates LEGIBILITY (text on surfaces). Nothing gated the
// surfaces' relationships to EACH OTHER, so a palette could pass every contrast
// assertion while rendering as one flat slab. These tests compute L* and hue
// from the shipped hexes rather than asserting that a declaration appears, so
// they fail on a value regression and not merely on a deleted line.
//
// Loaded through the styles/index.css barrel exactly like colorContrast.test.ts
// so a token moved out of the runtime cascade fails here too.
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

type Theme = "dark" | "light";
const THEMES: Theme[] = ["dark", "light"];

function themeBlock(theme: Theme): string {
  const pattern =
    theme === "dark" ? /^:root\s*\{([\s\S]*?)\n\}/m : /\[data-theme="light"\]\s*\{([\s\S]*?)\n\}/;
  const match = pattern.exec(appCss);
  if (!match) {
    throw new Error(`Could not find the ${theme} token block in styles/tokens.css`);
  }
  return match[1];
}

function token(theme: Theme, name: string): string {
  const match = new RegExp(`${name}:\\s*([^;]+);`).exec(themeBlock(theme));
  if (!match) {
    throw new Error(`Could not find ${name} in the ${theme} token block`);
  }
  return match[1].trim();
}

function hex(theme: Theme, name: string): string {
  const value = token(theme, name);
  if (!/^#[0-9a-fA-F]{6}$/.test(value)) {
    throw new Error(`Expected ${name} (${theme}) to be a six-digit hex, got ${value}`);
  }
  return value.toLowerCase();
}

function channels(value: string): { r: number; g: number; b: number } {
  return {
    r: Number.parseInt(value.slice(1, 3), 16),
    g: Number.parseInt(value.slice(3, 5), 16),
    b: Number.parseInt(value.slice(5, 7), 16),
  };
}

function toLinear(channel: number): number {
  const normalized = channel / 255;
  return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
}

function luminance(value: string): number {
  const { r, g, b } = channels(value);
  return 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b);
}

// CIE L* — perceptual lightness, so "how far apart do these surfaces read"
// is measured on a perceptual axis rather than on raw channel deltas.
function lightness(value: string): number {
  const y = luminance(value);
  return y <= 0.008856 ? y * 903.3 : 116 * Math.cbrt(y) - 16;
}

// Warm-vs-cool along the red/blue axis. The role palette separates its
// inspection family from its workspace family by HUE, not by lightness — in
// the dark theme they sit within 0.5 L* of each other and are told apart
// entirely by this number.
function warmth(value: string): number {
  const { r, b } = channels(value);
  return r - b;
}

describe("surface elevation is a real step in both themes (elspeth-9e1a4935b0)", () => {
  it("never lets --color-surface and --color-surface-elevated be the same colour", () => {
    for (const theme of THEMES) {
      expect(
        hex(theme, "--color-surface-elevated"),
        `--color-surface-elevated must differ from --color-surface (${theme}); ` +
          "identical values make every lighter-surface elevation step a no-op",
      ).not.toBe(hex(theme, "--color-surface"));
    }
  });

  it("keeps elevated perceptibly lighter than surface, and surface lighter than the ground", () => {
    for (const theme of THEMES) {
      const ground = lightness(hex(theme, "--color-bg"));
      const surface = lightness(hex(theme, "--color-surface"));
      const elevated = lightness(hex(theme, "--color-surface-elevated"));

      expect(
        elevated - surface,
        `elevated must sit at least 1 L* above surface (${theme})`,
      ).toBeGreaterThanOrEqual(1);
      expect(
        surface - ground,
        `surface must sit above the page ground (${theme})`,
      ).toBeGreaterThan(0);
    }
  });
});

describe("the three-family role palette stays separated (elspeth-9e1a4935b0)", () => {
  it("gives the inspection family real warmth against the workspace ground", () => {
    // Light theme before the fix: --color-surface-inspector #faf7f3 was
    // r-b = 7 against a ground of r-b = -5, a 12-point swing, and sat 0.04 L*
    // from that ground — so the family that exists to read as paper-on-desk
    // was indistinguishable from the desk. The dark theme's swing is 42.
    for (const theme of THEMES) {
      const ground = warmth(hex(theme, "--color-bg"));
      for (const family of ["--color-surface-inspector", "--color-surface-paper"]) {
        expect(
          warmth(hex(theme, family)) - ground,
          `${family} must read warmer than --color-bg (${theme})`,
        ).toBeGreaterThanOrEqual(15);
      }
    }
  });

  it("separates the navigation family from the workspace ground on lightness", () => {
    for (const theme of THEMES) {
      expect(
        Math.abs(lightness(hex(theme, "--color-surface-nav")) - lightness(hex(theme, "--color-bg"))),
        `--color-surface-nav must be a real step off --color-bg (${theme})`,
      ).toBeGreaterThanOrEqual(3);
    }
  });

  it("keeps every surface-family token distinct within a theme", () => {
    const family = [
      "--color-bg",
      "--color-surface",
      "--color-surface-nav",
      "--color-surface-inspector",
      "--color-surface-paper",
    ];
    for (const theme of THEMES) {
      const values = family.map((name) => hex(theme, name));
      expect(new Set(values).size, `duplicate surface-family values (${theme})`).toBe(
        family.length,
      );
    }
  });
});

describe("light-theme elevation shadows (elspeth-b30aa49831)", () => {
  const TIERS = ["--shadow-popover", "--shadow-dropdown", "--shadow-modal"];

  it("overrides all three shadow tiers onto the tinted-navy base in the light theme", () => {
    // Before the fix only --shadow-dropdown was overridden, so every dialog
    // (including .explain-dialog on cream --color-surface-paper) and every
    // tooltip dropped a pure-black shadow on a light surface, at up to the
    // 0.25 alpha of the heaviest tier.
    for (const tier of TIERS) {
      const value = token("light", tier);
      expect(value, `${tier} must not keep a pure-black base in the light theme`).not.toMatch(
        /rgba\(\s*0\s*,\s*0\s*,\s*0\s*,/,
      );
      expect(value, `${tier} must use the tinted-navy base in the light theme`).toMatch(
        /rgba\(\s*15\s*,\s*45\s*,\s*53\s*,/,
      );
    }
  });

  it("keeps the light tiers ordered and lighter than their dark counterparts", () => {
    const alphaOf = (value: string): number => {
      const match = /rgba\([^)]*?,\s*(0?\.\d+|[01])\s*\)/.exec(value);
      if (!match) {
        throw new Error(`Could not read an alpha out of ${value}`);
      }
      return Number.parseFloat(match[1]);
    };

    const light = TIERS.map((tier) => alphaOf(token("light", tier)));
    const dark = TIERS.map((tier) => alphaOf(token("dark", tier)));

    // popover < dropdown < modal — the tier ordering must survive the swap.
    expect(light[0]).toBeLessThan(light[1]);
    expect(light[1]).toBeLessThan(light[2]);
    for (const [index, tier] of TIERS.entries()) {
      expect(light[index], `${tier} should be lighter in the light theme`).toBeLessThan(
        dark[index],
      );
    }
  });

  it("changes only the shadow colour, not the geometry, between themes", () => {
    const geometryOf = (value: string): string => value.slice(0, value.indexOf("rgba")).trim();
    for (const tier of TIERS) {
      expect(geometryOf(token("light", tier)), `${tier} geometry must match the dark tier`).toBe(
        geometryOf(token("dark", tier)),
      );
    }
  });
});

describe("forced-colors tutorial progress states (elspeth-44dc95153e)", () => {
  function forcedColorsBlock(): string {
    // Anchor on the at-rule's OPENING BRACE. themes.css names
    // "@media (forced-colors: active)" in its file-header comment first, so a
    // bare indexOf lands in the prose and brace-matches the unrelated
    // prefers-contrast block that follows it.
    const opening = /@media\s*\(forced-colors:\s*active\)\s*\{/.exec(appCss);
    expect(opening, "styles/themes.css must define a forced-colors block").not.toBeNull();
    const start = opening!.index;
    // Brace-match rather than slicing on a formatting sentinel, so a rule
    // added at the end of the block is still inspected.
    let depth = 0;
    for (let index = start + opening![0].length - 1; index < appCss.length; index += 1) {
      if (appCss[index] === "{") depth += 1;
      if (appCss[index] === "}") {
        depth -= 1;
        if (depth === 0) return appCss.slice(start, index + 1);
      }
    }
    throw new Error("Unbalanced braces in the forced-colors block");
  }

  function declaration(block: string, selector: string): string {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = new RegExp(`${escaped}\\s*\\{([^}]*)\\}`).exec(block);
    if (!match) {
      throw new Error(`No forced-colors rule for ${selector}`);
    }
    return match[1];
  }

  it("gives completed, current and upcoming steps three distinguishable appearances", () => {
    // Completed and upcoming rendered identically under Windows High Contrast
    // because only --active had a rule, reintroducing the exact defect the
    // --complete class split was written to fix (tutorial.css:658).
    const block = forcedColorsBlock();
    const complete = /background:\s*([^;]+);/.exec(
      declaration(block, ".tutorial-progress-dot--complete"),
    )?.[1];
    const active = /background:\s*([^;]+);/.exec(
      declaration(block, ".tutorial-progress-dot--active"),
    )?.[1];

    expect(complete, "--complete needs its own forced-colors background").toBeDefined();
    expect(active, "--active needs its own forced-colors background").toBeDefined();
    // Upcoming dots inherit Canvas from the base .tutorial-progress-dot rule,
    // so complete and active only have to differ from each other and from it.
    expect(complete).not.toBe(active);
    expect(complete).not.toMatch(/^Canvas$/);
    expect(declaration(block, ".tutorial-progress-dot")).toContain("CanvasText");
  });

  it("lets the current step win over the completed step if a dot ever carries both", () => {
    // The two classes are mutually exclusive today
    // (HelloWorldTutorial.tsx:386-392 is a ternary), but they have equal
    // specificity, so source order is the only tie-break. --active must come
    // LAST — matching tutorial.css, where --complete (:659) precedes
    // --active (:667) — or a dot gaining both would render the CURRENT step
    // as already completed, the very confusion this block exists to remove.
    const block = forcedColorsBlock();
    expect(block.indexOf(".tutorial-progress-dot--active")).toBeGreaterThan(
      block.indexOf(".tutorial-progress-dot--complete"),
    );
  });
});
