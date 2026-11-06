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
    ":root .react-flow__controls",
    ".graph-node-badge",
    ".artifact-tab-badge",
    // ARGOnite: header controls are tint-only pills (header.css sets
    // border: 0, on hover too), and forced colours strips the tint.
    ".app-header .header-session-switcher-trigger",
    ".app-header .version-selector-trigger",
    ".app-header .user-menu-trigger",
    ".app-header .workflow-mailbox-badge",
    // The hover rules re-set border: 0 at (0,3,0), so each needs its own.
    ".app-header .header-session-switcher-trigger:hover",
    ".app-header .version-selector-trigger:hover",
    ".app-header .user-menu-trigger:hover",
    ".app-header .workflow-mailbox-badge:hover",
    ".app-header .user-menu-trigger:focus-visible",
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

// A forced-colours rule that is LESS specific than the normal-mode rule it
// must override is dead (themes.css wins only ties, by source order). This
// compares each border-setting forced rule against every barrel rule that
// sets border on the same final compound selector.
function specificity(selector: string): number {
  const s = selector.replace(/:where\([^)]*\)/g, "");
  const ids = (s.match(/#[\w-]+/g) ?? []).length;
  const classes = (
    s.match(/\.[\w-]+|\[[^\]]*\]|:(?!:)(?!root\b)[\w-]+(?:\([^)]*\))?/g) ?? []
  ).length;
  const root = (s.match(/:root\b/g) ?? []).length;
  const els = (
    s
      .replace(/\[[^\]]*\]|\([^)]*\)/g, "")
      .match(/(?:^|[\s>+~])[a-zA-Z][\w-]*/g) ?? []
  ).length;
  return ids * 10000 + (classes + root) * 100 + els;
}

function lastCompound(selector: string): string {
  return selector.split(/\s*[\s>+~]\s*/).filter(Boolean).pop() ?? selector;
}

const sheets = [
  "shared.css",
  "../components/workspace/workspace.css",
  "../components/inspector/inspector.css",
  "../components/chat/chat.css",
  "../components/header/header.css",
]
  .map((f) =>
    readFileSync(join("src", "styles", f), "utf8").replace(/\/\*[\s\S]*?\*\//g, ""),
  )
  .join("\n");

describe("forced-colours rules are not out-specified", () => {
  const borderTargets = [
    ".artifact-tab[aria-selected=\"true\"]",
    ".switch-track",
    ".switch-thumb",
    ".graph-view-canvas",
    ".chat-input-row",
    ".chat-input-icon-btn",
    ".chat-input-send-btn",
    ".graph-checksum-actions > .btn-compact",
    ".graph-checksum-value",
    ":root .react-flow__controls",
    ".graph-node-badge",
    ".artifact-tab-badge",
    ".app-header .header-session-switcher-trigger",
    ".app-header .version-selector-trigger",
    ".app-header .user-menu-trigger",
    ".app-header .workflow-mailbox-badge",
  ];

  it.each(borderTargets)("%s", (target) => {
    const forcedSpec = specificity(target);
    const key = lastCompound(target);
    for (const m of sheets.matchAll(/([^{}@]+)\{([^{}]*)\}/g)) {
      if (!/(?:^|;|\s)border(?:-color|-style)?\s*:/.test(m[2])) continue;
      // :where() holds zero specificity, so drop it before splitting the list.
      const list = m[1].replace(/:where\((?:[^()]|\([^)]*\))*\)/g, "");
      for (const sel of list.split(",").map((x) => x.trim())) {
        // State variants (:checked etc.) are restated by their own forced rule.
        if (/:(?:checked|hover|focus|focus-visible|disabled|active)/.test(sel)) continue;
        if (lastCompound(sel) !== key) continue;
        expect(
          forcedSpec,
          `forced "${target}" must be >= normal "${sel}"`,
        ).toBeGreaterThanOrEqual(specificity(sel));
      }
    }
  });
});
