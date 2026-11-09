import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Class-name gate for the execution components (elspeth-61330c82fc,
// elspeth-1df26d22ae). The execution directory shipped BOTH P-level instances
// of the declared-but-undefined tenancy defect: header.css:6 claimed
// `.runs-history-*` and execution.css claimed `.run-outcome-notice*`, yet the
// drawer surface and the outcome strip had no rules at all — an aria-modal
// dialog rendering as raw in-flow prose with UA disc bullets. That failure is
// silent by construction (the element simply inherits its ancestors'
// treatment), so it needs a gate, not review vigilance.
//
// Same directory-scoped shape as catalogClassNames.test.ts (the whole-tree
// version belongs in styles/*.test.ts and needs its own allowlist pass).
const componentDir = "src/components/execution";
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
 * Class names an execution component applies. Handles the three authoring
 * forms in this directory: a plain string, a `{...}` expression holding
 * string literals, and a template literal with `${…}` interpolations.
 *
 * Interpolation is handled by POSITION, not by stripping: in
 * `inline-run-results${suffix}` the fragment is a PREFIX completed at
 * runtime, not a class name, so a token abutting an interpolation on either
 * side is dropped rather than reported as undefined. Only whole tokens
 * survive. (Same extractor as catalogClassNames.test.ts.)
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
  "discard-summary-reasons":
    "DiscardSummaryWarning's recorded-reasons span. Its parent " +
    ".discard-summary-warning is a display:grid stack whose gap supplies the " +
    "spacing, and the span inherits the warning tint and --font-size-xs from " +
    "that parent; a rule of its own could only restate them. The name exists " +
    "as a stable hook for tests and future differentiation.",
  "run-diagnostics":
    "The per-run diagnostics disclosure container in RunsHistoryDrawer. Its " +
    "collapsed state is carried entirely by the [hidden] attribute and its " +
    "visual treatment by the .run-diagnostics-panel child (header.css); the " +
    "container itself deliberately contributes no box of its own.",
};

const componentSources = readdirSync(componentDir)
  .filter((entry) => entry.endsWith(".tsx") && !entry.includes(".test."))
  .map((entry) => ({
    file: entry,
    source: readFileSync(join(componentDir, entry), "utf8"),
  }));

describe("execution class names are backed by a stylesheet (elspeth-61330c82fc)", () => {
  it("reads a non-trivial set of class names out of every execution component", () => {
    // Guards the gate itself: an extractor that silently matched nothing
    // would certify any tree.
    expect(componentSources.length).toBeGreaterThan(0);
    for (const { file, source } of componentSources) {
      expect(
        classNamesIn(source).size,
        `${file} yielded no class names`,
      ).toBeGreaterThan(0);
    }
    expect(definedClasses.has("runs-history-drawer")).toBe(true);
    expect(definedClasses.has("class-that-no-stylesheet-defines")).toBe(false);
  });

  it("defines every class the execution components apply", () => {
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
        "the name promises. Add the rule to execution.css, or add the name " +
        "to RULE_LESS_BY_DESIGN with the reason it needs none",
    ).toEqual([]);
  });

  it("keeps the rule-less allowlist honest", () => {
    for (const [name, reason] of Object.entries(RULE_LESS_BY_DESIGN)) {
      const applied = componentSources.some(({ source }) =>
        classNamesIn(source).has(name),
      );
      expect(applied, `${name} is allowlisted but no component applies it`).toBe(
        true,
      );
      expect(
        definedClasses.has(name),
        `${name} now HAS a rule — drop it from the allowlist`,
      ).toBe(false);
      expect(
        reason.length,
        `${name} needs a real reason, not a placeholder`,
      ).toBeGreaterThan(40);
    }
  });
});
