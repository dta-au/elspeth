import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Style invariants for the recovery panel (elspeth-76a6d842f1,
// elspeth-f59db0669e).
//
// Two failure modes are gated here, both of which shipped:
//
//   1. A class emitted by a component that NO stylesheet defines. recovery.css
//      styled roughly half the vocabulary its own components emit, so added,
//      removed and changed diff rows rendered as three identical grey boxes
//      and "Missing tool response" read as ordinary prose. The vocabulary is
//      scraped out of the components rather than hand-listed, so a new
//      className has to be styled (or explicitly exempted below) to land.
//
//   2. A grid child left to auto-placement. `.recovery-panel-body` is a
//      two-track grid with three children; with only one of them placed, the
//      third row-bumped and stranded a full-height empty cell beside it. The
//      assertion is arithmetic, not textual: the item holding the left column
//      must span exactly as many rows as the right column stacks.
//
// cwd-relative reads per the tokenReferences.test.ts / chatBubbleGutter.test.ts
// idiom; vitest runs from the frontend root.
const recoveryDir = "src/components/recovery";
const recoveryCss = readFileSync(join(recoveryDir, "recovery.css"), "utf8");
const stylesheetBarrel = readFileSync("src/styles/index.css", "utf8");

const componentSources = [
  "RecoveryPanel.tsx",
  "RecoveryDiff.tsx",
  "RecoveryTranscript.tsx",
].map((name) => readFileSync(join(recoveryDir, name), "utf8"));

/** The DiffKind union (RecoveryDiff.tsx), substituted for the interpolation in
 *  `recovery-diff-row--${entry.kind}` and `recovery-diff-group--${group.kind}`. */
const DIFF_KINDS = ["added", "removed", "changed"];

/** Classes carried for BEHAVIOUR, not appearance: useFocusTrap targets these
 *  two by selector (RecoveryPanel.tsx:46). They are deliberately unstyled. */
const SELECTOR_ONLY_CLASSES = new Set([
  "recovery-panel-apply",
  "recovery-panel-discard",
]);

function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

/** Every `recovery-*` class name the components put on an element. */
function emittedRecoveryClasses(): Set<string> {
  const emitted = new Set<string>();
  for (const source of componentSources) {
    for (const match of source.matchAll(
      /className=(?:"([^"]*)"|\{`([^`]*)`\})/g,
    )) {
      const raw = match[1] ?? match[2] ?? "";
      for (const token of raw.split(/\s+/).filter(Boolean)) {
        if (!token.startsWith("recovery-")) continue;
        if (token.includes("${")) {
          for (const kind of DIFF_KINDS) {
            emitted.add(token.replace(/\$\{[^}]*\}/, kind));
          }
          continue;
        }
        emitted.add(token);
      }
    }
  }
  return emitted;
}

interface Rule {
  selectors: string[];
  declarations: string;
}

function parseRules(css: string): Rule[] {
  return Array.from(stripComments(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)).map(
    (match) => ({
      selectors: match[1].split(",").map((selector) => selector.trim()),
      declarations: match[2],
    }),
  );
}

/** recovery.css split at its single responsive branch, so a declaration and
 *  its narrow-viewport release can be told apart. */
function splitAtNarrowBranch(css: string): { wide: Rule[]; narrow: Rule[] } {
  const marker = "@media (max-width: 900px)";
  const start = css.indexOf(marker);
  if (start === -1) {
    throw new Error("recovery.css no longer declares its 900px branch");
  }
  return {
    wide: parseRules(css.slice(0, start)),
    narrow: parseRules(css.slice(start)),
  };
}

/** The LAST value declared for `property` by a rule listing `selector`
 *  verbatim — the one that reaches the element among equal-specificity ties. */
function declaredValue(
  rules: Rule[],
  selector: string,
  property: string,
): string | undefined {
  let found: string | undefined;
  for (const rule of rules) {
    if (!rule.selectors.includes(selector)) continue;
    const match = new RegExp(`(?:^|;)\\s*${property}\\s*:([^;]+)`).exec(
      rule.declarations,
    );
    if (match) found = match[1].trim();
  }
  return found;
}

const { wide, narrow } = splitAtNarrowBranch(recoveryCss);

describe("recovery class vocabulary (elspeth-f59db0669e)", () => {
  it("scrapes the vocabulary the components actually emit", () => {
    const emitted = emittedRecoveryClasses();
    // Guard against a silently mis-parsing scrape reporting a clean sheet:
    // the three components carry well over twenty recovery-* classes, and the
    // interpolated kinds have to survive expansion.
    expect(emitted.size).toBeGreaterThan(20);
    expect(emitted).toContain("recovery-diff-row--removed");
    expect(emitted).toContain("recovery-diff-group--changed");
    expect(emitted).toContain("recovery-transcript-missing");
  });

  it("defines every emitted class in a stylesheet the barrel loads", () => {
    expect(stylesheetBarrel).toContain(
      '@import "../components/recovery/recovery.css";',
    );

    const styled = stripComments(recoveryCss);
    const undefinedClasses = [...emittedRecoveryClasses()]
      .filter((name) => !SELECTOR_ONLY_CLASSES.has(name))
      .filter((name) => !new RegExp(`\\.${name}(?![\\w-])`).test(styled))
      .sort();

    expect(
      undefinedClasses,
      "classes emitted by the recovery components that no rule defines — a " +
        "diff whose --added/--removed/--changed modifiers resolve to nothing " +
        "renders as identical grey boxes",
    ).toEqual([]);
  });

  it("keeps the three diff kinds on three different colour families", () => {
    const tints = DIFF_KINDS.map((kind) =>
      declaredValue(wide, `.recovery-diff-row--${kind}`, "background-color"),
    );
    expect(tints.every((tint) => tint !== undefined)).toBe(true);
    expect(new Set(tints).size).toBe(DIFF_KINDS.length);
    // The warning family is this panel's degraded-evidence tone
    // (.recovery-panel-reason, .recovery-transcript-missing); spending it on an
    // ordinary changed row would repeat elspeth-7161879770's palette misuse.
    expect(tints).not.toContain("var(--color-warning-bg)");
    expect(
      declaredValue(wide, ".recovery-transcript-missing", "background-color"),
    ).toBe("var(--color-warning-bg)");
  });
});

describe("recovery panel grid placement (elspeth-76a6d842f1)", () => {
  const template = declaredValue(
    wide,
    ".recovery-panel-body",
    "grid-template-columns",
  );

  it("still declares the two-track grid these placements assume", () => {
    expect(declaredValue(wide, ".recovery-panel-body", "display")).toBe("grid");
    expect(template).toBeDefined();
    expect(template?.match(/minmax\(/g)).toHaveLength(2);
  });

  it("gives the transcript's column the larger flex share", () => {
    // The panel's longest content — prose plus tool payloads — is the thing
    // the reader scrolls, and it lives in column 2. Arithmetic rather than a
    // literal: whatever the two shares become, the transcript's track must be
    // the bigger of them.
    const shares = [...(template ?? "").matchAll(/([\d.]+)fr/g)].map((match) =>
      Number.parseFloat(match[1]),
    );
    expect(shares).toHaveLength(2);
    expect(shares[1]).toBeGreaterThan(shares[0]);
  });

  it("spans the left column across exactly as many rows as the right stacks", () => {
    const children = [
      ".recovery-diff",
      ".recovery-panel-transcript-controls",
      ".recovery-transcript",
    ];
    const rightColumn = children.filter(
      (child) => declaredValue(wide, child, "grid-column") === "2",
    );
    const span = declaredValue(wide, ".recovery-diff", "grid-row");

    // Every child is placed: an auto-placed third item is what row-bumped the
    // transcript under the diff and stranded the cell beside it.
    for (const child of children) {
      const placed =
        declaredValue(wide, child, "grid-column") !== undefined ||
        declaredValue(wide, child, "grid-row") !== undefined;
      expect(placed, `${child} is left to auto-placement`).toBe(true);
    }

    // `1 / -1` cannot be used here: with no grid-template-rows the explicit
    // grid has no row lines, so -1 resolves back to line 1 and the span
    // silently collapses to one row.
    expect(span).toMatch(/^span \d+$/);
    expect(Number.parseInt(span!.replace("span ", ""), 10)).toBe(
      rightColumn.length,
    );
  });

  it("releases every placement in the single-column branch", () => {
    expect(declaredValue(narrow, ".recovery-panel-body", "grid-template-columns"))
      .toBe("1fr");
    expect(declaredValue(narrow, ".recovery-diff", "grid-row")).toBe("auto");
    for (const child of [
      ".recovery-panel-transcript-controls",
      ".recovery-transcript",
    ]) {
      expect(
        declaredValue(narrow, child, "grid-column"),
        `${child} keeps an explicit column in the one-column branch, which ` +
          "conjures an implicit second track",
      ).toBe("auto");
    }
  });
});
