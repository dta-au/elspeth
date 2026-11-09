import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { WORKSPACE_SCROLL_OWNERS } from "./scrollOwners";

// Integrity of the scroll-owner CONVENTION (elspeth-73849d9d16), not of any
// particular binding. Which box carries which name is the stylesheet's
// statement and is deliberately NOT pinned here — a duplicate of that binding
// in a test would be the same second registry this convention replaced. What
// is pinned is the machinery the runtime gate stands on:
//
//   1. the `@property` registration, whose `inherits: false` is load-bearing —
//      without it every descendant of a named scroller inherits the name and
//      the e2e gate would accept a nested unnamed scroller wearing it;
//   2. that a `--scroll-owner` is only ever declared in a rule that also
//      grants scrolling, so the name and the grant cannot drift apart;
//   3. that every declared name is drawn from the closed vocabulary the gate
//      caps over, and every vocabulary name is bound somewhere — an orphan on
//      either side is the old silent divergence coming back.
//
// cwd-relative per the tokenReferences.test.ts idiom; vitest runs from the
// frontend root.

function cssFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return cssFiles(path);
    return entry.isFile() && entry.name.endsWith(".css") ? [path] : [];
  });
}

interface Rule {
  file: string;
  selector: string;
  declarations: string;
}

/**
 * Flat rule list over every stylesheet in src. Rules nested in @media are
 * captured as their own entries — the at-rule prelude never matches, because
 * its body contains braces.
 */
const rules: Rule[] = cssFiles(join(process.cwd(), "src")).flatMap((file) => {
  const source = readFileSync(file, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  return [...source.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((match) => ({
    file: file.slice(process.cwd().length + 1),
    selector: match[1].replace(/\s+/g, " ").trim(),
    declarations: match[2],
  }));
});

const ownerDeclaration = /(?:^|;)\s*--scroll-owner:\s*([^;]+);/;
const scrollGrant = /(?:^|;)\s*overflow(?:-x|-y)?:\s*(?:auto|scroll)\s*;/;

const bindings = rules
  .filter((rule) => ownerDeclaration.test(rule.declarations))
  .map((rule) => ({
    ...rule,
    owner: rule.declarations.match(ownerDeclaration)![1].trim(),
  }));

describe("the scroll-owner convention (elspeth-73849d9d16)", () => {
  it("registers --scroll-owner non-inheriting, so a nested scroller cannot wear its ancestor's name", () => {
    const registration = rules.filter(
      (rule) => rule.selector === "@property --scroll-owner",
    );
    expect(registration).toHaveLength(1);
    expect(registration[0].file).toBe("src/components/workspace/workspace.css");
    expect(registration[0].declarations).toMatch(
      /(?:^|;)\s*inherits:\s*false\s*;/,
    );
    // Free-form on purpose: validity is the vocabulary's job (the e2e gate
    // rejects names outside it), not the CSS parser's. A typed syntax would
    // make a typo compute to the empty initial value and the gate's failure
    // would then say "no owner declared" instead of naming the bad name.
    expect(registration[0].declarations).toMatch(
      /(?:^|;)\s*syntax:\s*"\*"\s*;/,
    );
  });

  it("only ever names an owner in the rule that grants the scroll", () => {
    // The convention's whole point: the grant and the name are one act in one
    // block. A --scroll-owner on a non-scrolling rule is a name floating free
    // of any grant — the drift this replaced, with the polarity reversed.
    const floating = bindings.filter(
      (rule) => !scrollGrant.test(rule.declarations),
    );
    expect(
      floating.map((rule) => `${rule.file}: ${rule.selector}`),
    ).toEqual([]);
  });

  it("draws every declared name from the closed vocabulary", () => {
    const vocabulary: readonly string[] = WORKSPACE_SCROLL_OWNERS;
    const unknown = bindings.filter(
      (rule) => !vocabulary.includes(rule.owner),
    );
    expect(
      unknown.map((rule) => `${rule.file}: ${rule.selector} -> ${rule.owner}`),
    ).toEqual([]);
  });

  it("binds every vocabulary name to at least one granting rule", () => {
    const bound = new Set(bindings.map((rule) => rule.owner));
    const orphaned = WORKSPACE_SCROLL_OWNERS.filter(
      (owner) => !bound.has(owner),
    );
    expect(orphaned).toEqual([]);
  });
});
