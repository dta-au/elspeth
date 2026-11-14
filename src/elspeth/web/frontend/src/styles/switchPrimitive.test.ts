import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

// ARGOnite: the .switch primitive (markup contract in the Task 4 brief). Colour
// helpers follow the pattern in colorContrast.test.ts, which does not export them.
const stylesheetBarrel = readFileSync("src/styles/index.css", "utf8");
const appCss = Array.from(stylesheetBarrel.matchAll(/@import\s+"(?<path>[^"]+)";/g))
  .map((match) => {
    const importPath = match.groups?.path;
    if (importPath === undefined) {
      throw new Error("styles/index.css import regex produced no path");
    }
    return readFileSync(fileURLToPath(new URL(importPath, import.meta.url)), "utf8");
  })
  .join("\n");
const css = appCss.replace(/\/\*[\s\S]*?\*\//g, "");

function ruleBody(selector: string): string {
  for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (match[1].split(",").some((candidate) => candidate.trim() === selector)) {
      return match[2];
    }
  }
  throw new Error(`No rule found for ${selector}`);
}

function tokenIn(block: string, name: string): string {
  const m = new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})\\s*;`).exec(block);
  if (!m) {
    throw new Error(`Could not find hex ${name}`);
  }
  return m[1];
}

const darkBlock = /^:root\s*\{([\s\S]*?)\n\}/m.exec(appCss)![1];
const lightBlock = /\[data-theme="light"\]\s*\{([\s\S]*?)\n\}/.exec(appCss)![1];

function linear(v: number): number {
  const n = v / 255;
  return n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4;
}
function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => linear(Number.parseInt(hex.slice(i, i + 2), 16)));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** Resolve `var(--token)` in a declaration of `selector` to a hex for the theme. */
function resolved(selector: string, property: string, block: string): string {
  const m = new RegExp(`${property}:\\s*var\\((--[\\w-]+)\\)`).exec(ruleBody(selector));
  if (!m) {
    throw new Error(`${selector} has no tokenised ${property}`);
  }
  return tokenIn(block, m[1]);
}

describe("switch primitive (ARGOnite)", () => {
  for (const [theme, block] of [
    ["dark", darkBlock],
    ["light", lightBlock],
  ] as const) {
    it(`keeps the off thumb at 3:1 on the off track in the ${theme} theme`, () => {
      expect(ruleBody(".switch-track")).toContain("background-color: var(--color-border-strong)");
      expect(
        contrast(
          resolved(".switch-thumb", "background-color", block),
          resolved(".switch-track", "background-color", block),
        ),
      ).toBeGreaterThanOrEqual(3);
    });

    it(`keeps the on thumb at 3:1 on the on track in the ${theme} theme`, () => {
      const onTrack = ".switch-input:checked + .switch-track";
      const onThumb = ".switch-input:checked + .switch-track .switch-thumb";
      expect(
        contrast(
          resolved(onThumb, "background-color", block),
          resolved(onTrack, "background-color", block),
        ),
      ).toBeGreaterThanOrEqual(3);
    });
  }

  it("shows a focus ring on the track when the hidden input is keyboard-focused", () => {
    expect(ruleBody(".switch-input:focus-visible + .switch-track")).toContain(
      "outline: 2px solid var(--color-focus-ring)",
    );
  });

  it("hides the input visually without removing it from the focus order", () => {
    const body = ruleBody(".switch-input");
    expect(body).toContain("position: absolute");
    expect(body).toContain("opacity: 0");
    expect(body).not.toContain("display: none");
    expect(body).not.toContain("visibility: hidden");
  });

  it("provides a hit target of at least --size-control-compact", () => {
    expect(ruleBody(".switch")).toContain("min-height: var(--size-control-compact)");
  });
});
