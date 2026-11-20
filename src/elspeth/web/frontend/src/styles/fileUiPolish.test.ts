import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Layout decisions from the release's "make the file UI look good" pass,
// pinned at the stylesheet: each one fixed a rendered defect (see the commit).
function rules(path: string): Array<{ selectors: string[]; declarations: string; media: string | null }> {
  const css = readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const out: Array<{ selectors: string[]; declarations: string; media: string | null }> = [];
  const media = /@media([^{]+)\{((?:[^{}]*\{[^{}]*\})*)\s*\}/g;
  for (const block of css.matchAll(media)) {
    for (const rule of block[2].matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      out.push({ selectors: rule[1].split(",").map((s) => s.trim()), declarations: rule[2], media: block[1].trim() });
    }
  }
  for (const rule of css.replace(media, "").matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    out.push({ selectors: rule[1].split(",").map((s) => s.trim()), declarations: rule[2], media: null });
  }
  return out;
}

function declared(path: string, selector: string, property: string, media: string | null = null): string | undefined {
  const values = rules(path)
    .filter((rule) => rule.media === media && rule.selectors.includes(selector))
    .map((rule) => new RegExp(`(?:^|;)\\s*${property}\\s*:([^;]+)`).exec(rule.declarations)?.[1].trim())
    .filter((value): value is string => value !== undefined);
  return values[values.length - 1];
}

const BLOBS = "src/components/blobs/blobs.css";
const INSPECTOR = "src/components/inspector/inspector.css";
const CHAT = "src/components/chat/chat.css";

describe("file manager rows in a narrow pane", () => {
  it("wraps the actions to a second line instead of squeezing the file name to nothing", () => {
    expect(declared(BLOBS, ".blob-row-container", "flex-wrap")).toBe("wrap");
    expect(declared(BLOBS, ".blob-row-filename", "min-width")).toBe("5rem");
    expect(declared(BLOBS, ".blob-row-actions", "margin-inline-start")).toBe("auto");
  });

  it("puts the title on the left and the header controls together on the right", () => {
    expect(declared(BLOBS, ".blob-manager-title", "margin-inline-end")).toBe("auto");
  });
});

describe("wiring pad", () => {
  it("wraps a long file name inside the dialog", () => {
    expect(declared(BLOBS, ".file-wiring-filename", "overflow-wrap")).toBe("anywhere");
  });

  it("stacks its buttons full width on a narrow screen", () => {
    expect(declared(BLOBS, ".file-wiring-actions .confirm-dialog-btn", "flex", "(max-width: 480px)")).toBe("1 1 100%");
  });
});

describe("Files table", () => {
  it("centres cells on the row, above the shared top alignment", () => {
    expect(declared(INSPECTOR, ".graph-files-scroll table td", "vertical-align")).toBe("middle");
  });

  it("keeps a readable width and scrolls sideways in a narrow pane", () => {
    expect(declared(INSPECTOR, ".graph-files-scroll table", "min-width")).toBe("42rem");
    expect(declared(INSPECTOR, ".graph-files-scroll", "overflow")).toBe("auto");
  });
});

describe("review card", () => {
  it("draws the View/Hide prompt toggle in the UI font, not the browser's", () => {
    expect(declared(CHAT, ".ack-card-view-toggle", "font-family")).toBe("var(--font-sans)");
  });

  it("lays the copy controls out as one wrapping row", () => {
    expect(declared(CHAT, ".ack-card-copy-actions", "display")).toBe("flex");
    expect(declared(CHAT, ".ack-card-copy-actions", "flex-wrap")).toBe("wrap");
  });
});
