import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Layout decisions from the release's "make the file UI look good" pass,
// pinned at the stylesheet: each one fixed a rendered defect (see the commit).
function rules(path: string): Array<{ selectors: string[]; declarations: string; media: string | null }> {
  const css = readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const out: Array<{ selectors: string[]; declarations: string; media: string | null }> = [];
  // @media and @container blocks; `media` holds the condition either way.
  const media = /@(?:media|container)([^{]+)\{((?:[^{}]*\{[^{}]*\})*)\s*\}/g;
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

describe("file manager rows", () => {
  it("put the actions on their own line at every width, so the name is never cut to look like its neighbours", () => {
    expect(declared(BLOBS, ".blob-row-container", "flex-wrap")).toBe("wrap");
    expect(declared(BLOBS, ".blob-row-actions", "flex")).toBe("1 0 100%");
    expect(declared(BLOBS, ".blob-row-filename", "overflow-wrap")).toBe("anywhere");
    expect(declared(BLOBS, ".blob-row-filename", "text-overflow")).toBeUndefined();
    expect(declared(BLOBS, ".blob-row-filename", "white-space")).toBeUndefined();
  });

  it("keep the icons at the end of the line and the text action at the start", () => {
    expect(declared(BLOBS, ".blob-row-actions", "justify-content")).toBe("flex-end");
    expect(declared(BLOBS, ".blob-row-edit-prompt", "margin-inline-end")).toBe("auto");
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

  it("stacks each row in a narrow pane instead of cutting columns off", () => {
    expect(declared(INSPECTOR, ".graph-files-body", "container")).toBe("graph-files / inline-size");
    expect(declared(INSPECTOR, ".graph-files-scroll table", "min-width")).toBeUndefined();
    expect(declared(INSPECTOR, ".graph-files-scroll tr", "flex-direction", "graph-files (width < 40rem)")).toBe("column");
    expect(declared(INSPECTOR, ".graph-files-scroll table td", "display", "graph-files (width < 40rem)")).toBe("block");
  });
});

describe("review card", () => {
  it("draws the View/Hide prompt toggle in the UI font, not the browser's", () => {
    expect(declared(CHAT, ".ack-card-view-toggle", "font-family")).toBe("var(--font-sans)");
  });

  it("keeps the prompt box inside the card in a narrow column", () => {
    expect(declared(CHAT, ".ack-card-value", "min-width")).toBe("0");
    expect(declared(CHAT, ".ack-card-copy-actions .btn-compact", "white-space")).toBe("normal");
  });

  it("lays the copy controls out as one wrapping row", () => {
    expect(declared(CHAT, ".ack-card-copy-actions", "display")).toBe("flex");
    expect(declared(CHAT, ".ack-card-copy-actions", "flex-wrap")).toBe("wrap");
  });
});
