import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

// Constraint gates for the token scales added by the 2026-08-14 polish pass
// (elspeth-3b57d9025b hover direction, elspeth-9ae8ee9ac2 scrim,
// elspeth-7ac472c22c letter-spacing, elspeth-7f63055e06 icon sizes,
// elspeth-ec5ba7ba21 line-height rungs).
//
// These deliberately pin RELATIONSHIPS, not values: hover direction is a
// luminance inequality, the scrim is "shares the theme's shadow ink and dims
// within the shipped band", the scales are unit + strict monotonicity. A
// sibling retuning any single value stays green as long as the constraint
// still holds; an edit that breaks the constraint (a hover that darkens, a
// px tracking rung, a scale rung out of order) fails here.
//
// cwd-relative path per the established idiom (tokenReferences.test.ts,
// colorContrast.test.ts; vitest runs from the frontend root).
const tokensCss = readFileSync("src/styles/tokens.css", "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

function extractBlock(pattern: RegExp, blockName: string): string {
  const match = pattern.exec(tokensCss);
  if (!match) {
    throw new Error(`Could not find ${blockName} token block in styles/tokens.css`);
  }
  return match[1];
}

const rootBlock = extractBlock(/^:root\s*\{([\s\S]*?)\n\}/m, "root");
const lightBlock = extractBlock(
  /\[data-theme="light"\]\s*\{([\s\S]*?)\n\}/,
  "light theme",
);

function rawToken(block: string, blockName: string, tokenName: string): string {
  const match = new RegExp(`${tokenName}:\\s*([^;]+);`).exec(block);
  if (!match) {
    throw new Error(`Could not find ${tokenName} in ${blockName} token block`);
  }
  return match[1].trim();
}

const rootToken = (name: string) => rawToken(rootBlock, "root", name);
const lightToken = (name: string) => rawToken(lightBlock, "light theme", name);

function hexToRgb(value: string): [number, number, number] {
  const match = /^#([0-9a-fA-F]{6})$/.exec(value);
  if (!match) {
    throw new Error(`Expected 6-digit hex colour, got ${value}`);
  }
  const hex = match[1];
  return [
    Number.parseInt(hex.slice(0, 2), 16),
    Number.parseInt(hex.slice(2, 4), 16),
    Number.parseInt(hex.slice(4, 6), 16),
  ];
}

function channelToLinear(value: number): number {
  const normalized = value / 255;
  return normalized <= 0.04045
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const [r, g, b] = hexToRgb(hex);
  return (
    0.2126 * channelToLinear(r) +
    0.7152 * channelToLinear(g) +
    0.0722 * channelToLinear(b)
  );
}

function contrastRatio(hexA: string, hexB: string): number {
  const [lighter, darker] = [luminance(hexA), luminance(hexB)].sort(
    (a, b) => b - a,
  );
  return (lighter + 0.05) / (darker + 0.05);
}

function parseRgba(value: string): {
  red: number;
  green: number;
  blue: number;
  alpha: number;
} {
  const match =
    /^rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*(0(?:\.\d+)?|1(?:\.0+)?)\s*\)$/.exec(
      value,
    );
  if (!match) {
    throw new Error(`Expected rgba() colour, got ${value}`);
  }
  return {
    red: Number.parseInt(match[1], 10),
    green: Number.parseInt(match[2], 10),
    blue: Number.parseInt(match[3], 10),
    alpha: Number.parseFloat(match[4]),
  };
}

/** The rgb base of the first rgba() in a (possibly compound) token value. */
function rgbaBase(value: string): [number, number, number] {
  const match = /rgba\(\s*(\d+),\s*(\d+),\s*(\d+)/.exec(value);
  if (!match) {
    throw new Error(`Expected an rgba() component in ${value}`);
  }
  return [
    Number.parseInt(match[1], 10),
    Number.parseInt(match[2], 10),
    Number.parseInt(match[3], 10),
  ];
}

describe("filled-button hover direction (elspeth-3b57d9025b)", () => {
  it("lightens every dark-theme filled hover, clearing both resting fills that share the primary token", () => {
    const primaryHover = luminance(rootToken("--color-btn-primary-bg-hover"));
    const dangerHover = luminance(rootToken("--color-btn-danger-bg-hover"));

    // House direction on dark theme is LIGHTEN (outlined .btn hovers with a
    // white wash). The primary hover token is shared by .btn-primary AND the
    // accent-filled guided/chat buttons, so it must sit above both bases or
    // one family keeps inverting.
    expect(primaryHover).toBeGreaterThan(
      luminance(rootToken("--color-btn-primary-bg")),
    );
    expect(primaryHover).toBeGreaterThan(luminance(rootToken("--color-accent")));
    expect(dangerHover).toBeGreaterThan(
      luminance(rootToken("--color-btn-danger-bg")),
    );
  });

  it("keeps the light theme darkening on hover — the direction colorContrast.test.ts pins for hover surfaces", () => {
    expect(luminance(lightToken("--color-btn-primary-bg-hover"))).toBeLessThan(
      luminance(lightToken("--color-btn-primary-bg")),
    );
    expect(luminance(lightToken("--color-btn-danger-bg-hover"))).toBeLessThan(
      luminance(lightToken("--color-btn-danger-bg")),
    );
  });

  it("keeps inverse button text at WCAG AA on the hover fills in both themes", () => {
    for (const [theme, token] of [
      ["dark", rootToken],
      ["light", lightToken],
    ] as const) {
      const inverse = token("--color-text-inverse");
      for (const hover of [
        "--color-btn-primary-bg-hover",
        "--color-btn-danger-bg-hover",
      ]) {
        expect(
          contrastRatio(inverse, token(hover)),
          `inverse text on ${hover} (${theme})`,
        ).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
});

describe("overlay scrim (elspeth-9ae8ee9ac2)", () => {
  it("is theme-paired, shares each theme's shadow ink, and dims within the shipped band", () => {
    const dark = parseRgba(rootToken("--color-scrim"));
    const light = parseRgba(lightToken("--color-scrim"));

    // One ink per theme: the veil uses the same base the elevation tiers
    // cast, so scrim and shadow read as one lighting model. This also pins
    // the light scrim as tinted (not plain black, which reads too heavy on
    // light surfaces) WITHOUT hardcoding the palette here.
    expect([dark.red, dark.green, dark.blue]).toEqual(
      rgbaBase(rootToken("--shadow-modal")),
    );
    expect([light.red, light.green, light.blue]).toEqual(
      rgbaBase(lightToken("--shadow-modal")),
    );

    // The dim band: strong enough to read as blocking on every overlay
    // (the shipped 0.3 catalog backdrop was the defect), light enough not
    // to black the page out. Light theme veils lighter than dark.
    expect(dark.alpha).toBeGreaterThanOrEqual(0.4);
    expect(dark.alpha).toBeLessThanOrEqual(0.5);
    expect(light.alpha).toBeGreaterThanOrEqual(0.3);
    expect(light.alpha).toBeLessThan(dark.alpha);
  });
});

describe("letter-spacing scale (elspeth-7ac472c22c)", () => {
  it("is em-based and strictly ascending, below the wordmark tracking", () => {
    const em = (name: string): number => {
      const value = rootToken(name);
      const match = /^(\d+(?:\.\d+)?)em$/.exec(value);
      expect(match, `${name} must be em-based (got ${value})`).not.toBeNull();
      return Number.parseFloat(match![1]);
    };

    const rungs = [
      em("--letter-spacing-tight"),
      em("--letter-spacing-normal"),
      em("--letter-spacing-wide"),
      em("--letter-spacing-caps"),
    ];
    for (let i = 1; i < rungs.length; i += 1) {
      expect(rungs[i]).toBeGreaterThan(rungs[i - 1]);
    }
    // The wordmark lockup tracks far wider than any label rung; a scale rung
    // reaching it would make body chrome shout like the brand mark.
    expect(rungs[rungs.length - 1]).toBeLessThan(em("--tracking-wordmark"));
  });
});

describe("icon sizes (elspeth-7f63055e06)", () => {
  it("are px-based (matching the px type scale) and strictly ascending", () => {
    const px = (name: string): number => {
      const value = rootToken(name);
      const match = /^(\d+(?:\.\d+)?)px$/.exec(value);
      expect(match, `${name} must be px-based (got ${value})`).not.toBeNull();
      return Number.parseFloat(match![1]);
    };

    const sm = px("--size-icon-sm");
    const md = px("--size-icon-md");
    expect(sm).toBeGreaterThan(0);
    expect(md).toBeGreaterThan(sm);
  });
});

describe("line-height ladder (elspeth-ec5ba7ba21)", () => {
  it("is unitless and strictly ascending across all six rungs", () => {
    const unitless = (name: string): number => {
      const value = rootToken(name);
      const match = /^\d+(?:\.\d+)?$/.exec(value);
      expect(match, `${name} must be unitless (got ${value})`).not.toBeNull();
      return Number.parseFloat(value);
    };

    const ladder = [
      unitless("--line-height-heading"),
      unitless("--line-height-tight"),
      unitless("--line-height-snug"),
      unitless("--line-height-prose"),
      unitless("--line-height-normal"),
      unitless("--line-height-relaxed"),
    ];
    for (let i = 1; i < ladder.length; i += 1) {
      expect(ladder[i]).toBeGreaterThan(ladder[i - 1]);
    }
  });
});
