import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Class-name gate for the catalog drawer (elspeth-56e9c8e3b2).
//
// tokenReferences.test.ts gates every var(--x) against the stylesheets, so a
// CUSTOM PROPERTY that no stylesheet defines cannot ship. Nothing gated the
// same mistake one level up: a className the components use that no stylesheet
// defines. That failure is silent by construction — the element simply
// inherits whatever its ancestors set — which is exactly how
// .plugin-card-variants / .plugin-card-variant / .plugin-card-variant-label
// shipped, rendering discriminated-schema variant labels at the 16px body
// register inside a 12px reference card with no gap between the blocks.
//
// This is the catalog drawer's half of that gate. It is deliberately scoped to
// one directory rather than the whole tree: a whole-tree version belongs in
// styles/*.test.ts alongside tokenReferences.test.ts, and would need its own
// pass over every component's allowlist.
//
// Adding a class name here is cheap and adding a rule for it is cheap; the
// allowlist below is for the genuinely rule-less cases, and every entry states
// WHY the element needs no rule of its own. "It looks fine" is not a reason —
// if the element needs a treatment, give it one in catalog.css.
const componentDir = "src/components/catalog";
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
  .join("\n")
  .replace(/\/\*[\s\S]*?\*\//g, "");

/** Every class name any barrel stylesheet writes a selector for. */
const definedClasses = new Set(
  Array.from(appCss.matchAll(/\.([A-Za-z_][\w-]*)/g)).map((match) => match[1]),
);

/**
 * Class names a catalog component applies. Handles the three forms the
 * directory uses: a plain string, a `{...}` expression holding string
 * literals, and a template literal with `${…}` interpolations.
 *
 * Interpolation is handled by POSITION, not by stripping: in
 * `audit-icon-${meta.tone}` the fragment "audit-icon-" is a PREFIX completed
 * at runtime, not a class name, so a token abutting an interpolation on either
 * side is dropped rather than reported as undefined. Only whole tokens survive.
 */
function classNamesIn(source: string): Set<string> {
  const found = new Set<string>();

  function addLiteral(raw: string, isTemplate: boolean): void {
    const segments = isTemplate ? raw.split(/\$\{[^}]*\}/g) : [raw];
    segments.forEach((segment, index) => {
      const tokens = segment.split(/\s+/).filter((token) => token.length > 0);
      const abutsBefore = index > 0 && !/^\s/.test(segment);
      const abutsAfter = index < segments.length - 1 && !/\s$/.test(segment);
      const start = abutsBefore ? 1 : 0;
      const end = abutsAfter ? tokens.length - 1 : tokens.length;
      for (const token of tokens.slice(start, end)) {
        if (/^[A-Za-z_][\w-]*$/.test(token)) found.add(token);
      }
    });
  }

  for (const match of source.matchAll(/className\s*=\s*/g)) {
    let i = (match.index ?? 0) + match[0].length;
    // A `{…}` expression: take the balanced span and read every literal in it.
    let span = source.slice(i);
    if (source[i] === "{") {
      let depth = 0;
      let end = i;
      for (; end < source.length; end += 1) {
        if (source[end] === "{") depth += 1;
        else if (source[end] === "}") {
          depth -= 1;
          if (depth === 0) break;
        }
      }
      span = source.slice(i + 1, end);
      i += 1;
    } else {
      // A plain literal: stop at its closing quote.
      const quote = source[i];
      const end = source.indexOf(quote, i + 1);
      span = source.slice(i, end + 1);
    }
    for (const literal of span.matchAll(/"([^"]*)"|'([^']*)'|`([^`]*)`/g)) {
      if (literal[3] !== undefined) addLiteral(literal[3], true);
      else addLiteral(literal[1] ?? literal[2] ?? "", false);
    }
  }
  return found;
}

/**
 * Class names that are applied but carry NO rule, each with the reason it
 * needs none. Keyed by class name so a stale entry is visible.
 */
const RULE_LESS_BY_DESIGN: Record<string, string> = {
  "audit-icon-label":
    "AuditCharacteristicIcon's inner text span. It fills the whole chip and " +
    "takes every visual property — size, weight, colour, case — from " +
    ".audit-icon on its parent; a rule of its own could only restate them.",
  "audit-icon-unknown":
    "Forward-compatibility modifier for a backend audit flag that predates " +
    "its frontend metadata. It deliberately renders in .audit-icon's neutral " +
    "base treatment: an unrecognised flag must not be coloured as though its " +
    "tone were known.",
};

const componentSources = readdirSync(componentDir)
  .filter((entry) => entry.endsWith(".tsx") && !entry.includes(".test."))
  .map((entry) => ({
    file: entry,
    source: readFileSync(join(componentDir, entry), "utf8"),
  }));

describe("catalog class names are backed by a stylesheet (elspeth-56e9c8e3b2)", () => {
  it("reads a non-trivial set of class names out of every catalog component", () => {
    // Guards the gate itself: an extractor that silently matched nothing would
    // certify any tree, which is the shape this whole sweep keeps finding.
    expect(componentSources.length).toBeGreaterThan(0);
    for (const { file, source } of componentSources) {
      expect(classNamesIn(source).size, `${file} yielded no class names`).toBeGreaterThan(0);
    }
    expect(definedClasses.has("plugin-card")).toBe(true);
    expect(definedClasses.has("class-that-no-stylesheet-defines")).toBe(false);
  });

  it("defines every class the catalog components apply", () => {
    const undefinedUses: string[] = [];
    for (const { file, source } of componentSources) {
      for (const name of classNamesIn(source)) {
        if (definedClasses.has(name)) continue;
        if (name in RULE_LESS_BY_DESIGN) continue;
        undefinedUses.push(`${name} (applied by ${file})`);
      }
    }
    expect(
      undefinedUses.sort(),
      "class names no barrel stylesheet defines — the element silently " +
        "inherits its ancestors' type and spacing instead of the treatment " +
        "the name promises. Add the rule to catalog.css, or add the name to " +
        "RULE_LESS_BY_DESIGN with the reason it needs none",
    ).toEqual([]);
  });

  it("keeps the rule-less allowlist honest", () => {
    for (const [name, reason] of Object.entries(RULE_LESS_BY_DESIGN)) {
      const applied = componentSources.some(({ source }) => classNamesIn(source).has(name));
      expect(applied, `${name} is allowlisted but no component applies it`).toBe(true);
      expect(
        definedClasses.has(name),
        `${name} now HAS a rule — drop it from the allowlist`,
      ).toBe(false);
      expect(reason.length, `${name} needs a real reason, not a placeholder`).toBeGreaterThan(40);
    }
  });
});
