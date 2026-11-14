import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const headerCss = readFileSync("src/components/header/header.css", "utf8");
const pillSelectors = [
  ".app-header .header-session-switcher-trigger",
  ".app-header .version-selector-trigger",
  ".app-header .chat-model-chip",
  ".app-header .user-menu-trigger",
];

function rulesFor(css: string, selector: string): string[] {
  return Array.from(
    css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}]+)\{([^{}]*)\}/g),
  )
    .filter((match) => match[1].split(",").some((part) => part.trim() === selector))
    .map((match) => match[2]);
}

function pillSizes(css: string, selector: string, property: string): string[] {
  return rulesFor(css, selector).flatMap((body) =>
    Array.from(body.matchAll(new RegExp(`(?:^|;)\\s*${property}:\\s*([^;]+);`, "g")))
      .map((match) => match[1].trim()),
  );
}

describe("header pill geometry", () => {
  it("reads actual declarations and detects a mismatched chip height", () => {
    const selector = ".app-header .chat-model-chip";
    expect(pillSizes(`${selector} { height: 22px; }`, selector, "height"))
      .toEqual(["22px"]);
    expect(pillSizes(".other { height: 36px; }", selector, "height"))
      .toEqual([]);
    const drifted = headerCss.replace(
      "height: var(--size-header-pill-height);",
      "height: 22px;",
    );
    expect(pillSizes(drifted, selector, "height"))
      .not.toEqual(["var(--size-header-pill-height)"]);
  });

  it("sizes all five pills from one register, without separate compact overrides", () => {
    // Composer and Advisor both render .chat-model-chip; the same rule must
    // cover them and all three triggers, including compact desktop mode.
    for (const selector of pillSelectors) {
      for (const property of ["height", "min-height"]) {
        expect(pillSizes(headerCss, selector, property), `${selector} ${property}`)
          .toEqual(["var(--size-header-pill-height)"]);
      }
    }
  });

  it("keeps the mono model text centred and truncated inside the shared height", () => {
    expect(rulesFor(headerCss, ".app-header .chat-model-chip").join("\n"))
      .toContain("line-height: calc(var(--size-header-pill-height) - 2 * var(--space-2xs))");
    const chip = rulesFor(headerCss, ".chat-model-chip").join("\n");
    for (const declaration of [
      "font-family: var(--font-mono)",
      "min-width: 0",
      "max-width: 48ch",
      "overflow: hidden",
      "text-overflow: ellipsis",
      "white-space: nowrap",
    ]) {
      expect(chip).toContain(declaration);
    }
  });

  it("adds bottom breathing room in regular, compact and wrapped headers", () => {
    const bands = rulesFor(headerCss, ".app-header");
    expect(bands).toHaveLength(3);
    expect(bands[0]).toContain("padding-bottom: var(--space-xs)");
    expect(bands[1]).toContain("padding: var(--space-xs) var(--space-md) var(--space-sm)");
    expect(bands[2]).toContain(
      "height: calc(var(--size-header-pill-height) + var(--space-xs) + 1px)",
    );
  });
});
