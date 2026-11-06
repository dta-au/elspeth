import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// ARGOnite: depth is carried by fills and shadows, which forced colours
// (Windows High Contrast) strips. Every boundary and state must survive as a
// system-colour border or outline, declared in themes.css.
const themes = readFileSync(join("src", "styles", "themes.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

/** Body of every `@media <query> { ... }` block, brace-balanced. */
function mediaBodies(query: string): string[] {
  const bodies: string[] = [];
  let from = 0;
  for (;;) {
    const at = themes.indexOf(`@media ${query}`, from);
    if (at === -1) return bodies;
    const open = themes.indexOf("{", at);
    let depth = 1;
    let i = open + 1;
    while (depth > 0 && i < themes.length) {
      if (themes[i] === "{") depth += 1;
      if (themes[i] === "}") depth -= 1;
      i += 1;
    }
    bodies.push(themes.slice(open + 1, i - 1));
    from = i;
  }
}

/** Declarations of the rule whose selector list contains `selector` exactly. */
function declarations(body: string, selector: string): string {
  const out: string[] = [];
  for (const m of body.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = m[1].split(",").map((s) => s.trim().replace(/\s+/g, " "));
    if (selectors.includes(selector)) out.push(m[2]);
  }
  return out.join("\n");
}

const forced = mediaBodies("(forced-colors: active)").join("\n");
const reduced = mediaBodies("(prefers-reduced-motion: reduce)").join("\n");

const BOUNDARY = /(?:border|outline)(?:-color)?\s*:[^;]*\b(?:CanvasText|ButtonText)\b/;

describe("forced-colors: active boundaries", () => {
  it("marks the selected tab with Highlight", () => {
    const rule = declarations(forced, '.artifact-tab[aria-selected="true"]');
    expect(rule).toMatch(/(?:border|outline)[^;]*\bHighlight\b/);
  });

  it.each([
    ".switch-track",
    ".switch-thumb",
    ".graph-view-canvas",
    ".chat-input-row",
    ".chat-input-icon-btn",
    ".chat-input-send-btn",
    ".graph-checksum-actions > .btn-compact",
    ".graph-checksum-value",
    ".react-flow__controls",
    ".graph-node-badge",
    ".artifact-tab-badge",
  ])("%s keeps a 1px system-colour boundary", (selector) => {
    const rule = declarations(forced, selector);
    expect(rule).toMatch(BOUNDARY);
    expect(rule).toMatch(/1px solid/);
  });

  it("keeps the checked switch distinguishable by Highlight", () => {
    const track = declarations(forced, ".switch-input:checked + .switch-track");
    expect(track).toMatch(/background-color:\s*Highlight/);
    const thumb = declarations(
      forced,
      ".switch-input:checked + .switch-track .switch-thumb",
    );
    expect(thumb).toMatch(/background-color:\s*HighlightText/);
  });

  it("replaces the dropped node ring with a Highlight outline", () => {
    const rule = declarations(forced, ".react-flow__node.graph-node--selected");
    expect(rule).toMatch(/outline:\s*2px solid Highlight/);
    expect(rule).toMatch(/outline-offset:\s*2px/);
  });

  it("uses no !important", () => {
    expect(forced).not.toContain("!important");
  });
});

describe("prefers-reduced-motion: reduce", () => {
  it.each([
    ".switch-track",
    ".switch-thumb",
    ".artifact-tab",
  ])("%s has no transition", (selector) => {
    expect(declarations(reduced, selector)).toMatch(/transition:\s*none/);
  });
});
