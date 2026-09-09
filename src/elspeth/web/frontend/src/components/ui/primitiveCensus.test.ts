import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

// Primitive-adoption census gate (D3 full-migration wave, operator decision
// 2026-08-14 — see the policy header in ./index.ts). The adopt-as-touched era
// left 217 raw <button> against 10 <Button>; the wave migrated every raw
// <button>/<input> outside components/ui/ onto the ui primitives. Both halves
// are enforced live at ZERO: the last six raw text inputs (bespoke-recipe
// sites that the forced .input base class would have restyled) migrated onto
// <Input bare> once the chrome-suppression escape landed.
//
// cwd-relative path per the established test idiom (tokenReferences.test.ts,
// colorContrast.test.ts read stylesheets the same way; vitest runs from the
// frontend root).
const srcRoot = "src";
const uiDir = join("components", "ui");

function walkProductTsxFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      // components/ui/ is the primitives' own home — Button.tsx renders the
      // one sanctioned raw <button>, Input.tsx the one raw <input>.
      if (relative(srcRoot, path) === uiDir) continue;
      found.push(...walkProductTsxFiles(path));
    } else if (entry.endsWith(".tsx") && !entry.endsWith(".test.tsx")) {
      found.push(path);
    }
  }
  return found;
}

/**
 * Blank out comments while preserving every newline, so match indices still
 * map to true line numbers. Policy prose legitimately NAMES the raw elements
 * ("<button type=\"button\">, never <div onClick>"), and a census that
 * flagged prose would teach people to stop writing it.
 *
 * Known limits (fail-visible, not fail-silent): a line comment containing an
 * unterminated block-open, or a string literal containing one, could make
 * the block stripper overrun — the tree currently has neither (verified at
 * gate introduction), and an overrun UNDER-strips nothing silently dangerous:
 * a resurfaced comment mention fails the census with a file:line pointer a
 * human immediately recognises as prose.
 */
function blankComments(source: string): string {
  const blank = (text: string) => text.replace(/[^\n]/g, " ");
  return source
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/(^|\s)\/\/[^\n]*/gm, (match, lead: string) => lead + blank(match.slice(lead.length)));
}

interface CensusHit {
  file: string;
  line: number;
}

function censusOf(pattern: RegExp): CensusHit[] {
  const hits: CensusHit[] = [];
  for (const path of walkProductTsxFiles(srcRoot)) {
    const code = blankComments(readFileSync(path, "utf8"));
    for (const match of code.matchAll(pattern)) {
      hits.push({
        file: relative(srcRoot, path),
        line: code.slice(0, match.index).split("\n").length,
      });
    }
  }
  return hits;
}

function formatHits(hits: CensusHit[]): string {
  return hits.map((hit) => `  ${hit.file}:${hit.line}`).join("\n");
}

// The trailing [\s>/] keeps <Button>/<ButtonProps mentions and longer tag
// names out; \s matches the newline of a multi-line JSX opening tag, which is
// how every real site in the tree is formatted.
const RAW_BUTTON = /<button[\s>/]/g;
const RAW_INPUT = /<input[\s>/]/g;

describe("primitive census (full-migration end state, ./index.ts policy)", () => {
  it("has zero raw <button> outside components/ui/", () => {
    const hits = censusOf(RAW_BUTTON);
    expect(
      hits,
      `Raw <button> outside components/ui/ — use the ui <Button> primitive ` +
        `(variant="bare" carries a bespoke className verbatim; note Button ` +
        `defaults type="button", so a button inside a <form> needs an ` +
        `explicit type decision):\n${formatHits(hits)}`,
    ).toEqual([]);
  });

  it("has zero raw <input> outside components/ui/", () => {
    const hits = censusOf(RAW_INPUT);
    expect(
      hits,
      `Raw <input> outside components/ui/ — use the ui <Input> primitive ` +
        `(non-text types omit the .input base class automatically; a ` +
        `text-like field with a complete bespoke recipe takes the \`bare\` ` +
        `prop, which emits the className verbatim with no .input chrome):\n` +
        formatHits(hits),
    ).toEqual([]);
  });
});
