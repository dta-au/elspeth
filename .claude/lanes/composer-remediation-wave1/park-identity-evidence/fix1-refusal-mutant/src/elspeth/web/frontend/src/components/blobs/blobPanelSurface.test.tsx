import { readFileSync } from "node:fs";

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { previewBlobContentSnippet } from "@/api/client";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { BlobMetadata } from "@/types/api";
import { BlobManager } from "./BlobManager";

// Only the preview fetch is stubbed; the rest of the client module stays real
// so the blob store keeps working.
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  previewBlobContentSnippet: vi.fn(),
}));

// ---------------------------------------------------------------------------
// The divider rules, read out of the shipped stylesheet
// ---------------------------------------------------------------------------
//
// The row divider used to be an inline `style` on .blob-row-container
// (elspeth-a1a1b62aa9). Inline styles cannot express "except the last one", so
// the final row's border stacked on .chat-input's border-top as a doubled
// seam. Moving it to CSS is only half the fix: the obvious scoping —
// `.blob-row-item:last-child` — means "last row of its CATEGORY", and
// .blob-manager-category-header carries border-bottom with NO border-top, so
// suppressing it would merge the last Source row into the OUTPUT FILES band.
// That trades one doubled seam for two MISSING dividers, in the same theme.
//
// So these tests do not check that a declaration exists. They read the
// selectors the stylesheet actually ships, run them against a real rendered
// BlobManager, and assert WHICH elements lose their divider. A bare
// `:last-child` matches two rows here and fails.
const blobsCss = readFileSync("src/components/blobs/blobs.css", "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

interface Rule {
  selectors: string[];
  declarations: string;
}

const rules: Rule[] = Array.from(blobsCss.matchAll(/([^{}]+)\{([^{}]*)\}/g)).map(
  (match) => ({
    selectors: match[1].split(",").map((selector) => selector.trim()),
    declarations: match[2],
  }),
);

/** The last value blobs.css declares for `property` on a rule naming `selector`. */
function declaredValue(selector: string, property: string): string {
  const found = rules
    .filter((rule) => rule.selectors.includes(selector))
    .map((rule) =>
      new RegExp(`(?:^|;)\\s*${property}\\s*:([^;]+)`).exec(rule.declarations),
    )
    .filter((match): match is RegExpExecArray => match !== null)
    .map((match) => match[1].trim());
  if (found.length === 0) {
    throw new Error(`No ${property} declared for ${selector} in blobs.css`);
  }
  return found[found.length - 1];
}

/** Every selector in blobs.css whose rule takes a bottom border away. */
const suppressionSelectors = rules
  .filter((rule) => /(?:^|;)\s*border-bottom\s*:\s*none\s*(?:;|$)/.test(rule.declarations))
  .flatMap((rule) => rule.selectors);

/** The elements the shipped stylesheet strips a bottom border from. */
function undividedIn(root: ParentNode): Set<Element> {
  const stripped = new Set<Element>();
  for (const selector of suppressionSelectors) {
    for (const element of root.querySelectorAll(selector)) stripped.add(element);
  }
  return stripped;
}

function makeBlob(overrides: Partial<BlobMetadata> = {}): BlobMetadata {
  return {
    id: "blob-1",
    session_id: "session-1",
    filename: "data.csv",
    mime_type: "text/csv",
    size_bytes: 1024,
    content_hash: null,
    created_at: new Date().toISOString(),
    created_by: "user",
    source_description: null,
    status: "ready",
    creation_modality: "verbatim",
    created_from_message_id: null,
    creating_model_identifier: null,
    creating_model_version: null,
    creating_provider: null,
    creating_composer_skill_hash: null,
    creating_arguments_hash: null,
    ...overrides,
  };
}

/** Two categories, two rows each: Source (user) then Output (pipeline). */
function renderTwoCategories() {
  useBlobStore.setState({
    blobs: [
      makeBlob({ id: "s1", filename: "first-source.csv", created_by: "user" }),
      makeBlob({ id: "s2", filename: "last-source.csv", created_by: "user" }),
      makeBlob({ id: "o1", filename: "first-output.csv", created_by: "pipeline" }),
      makeBlob({ id: "o2", filename: "last-output.csv", created_by: "pipeline" }),
    ],
    isLoading: false,
    error: null,
    loadBlobs: vi.fn().mockResolvedValue(undefined),
  });
  return render(<BlobManager onUseAsInput={vi.fn()} />);
}

/** The row wrapper carrying `filename`, i.e. the element `:last-child` sees. */
function rowItemFor(root: ParentNode, filename: string): Element {
  const wrapper = Array.from(root.querySelectorAll(".blob-row-item")).find(
    (item) => item.querySelector(".blob-row-filename")?.textContent === filename,
  );
  if (!wrapper) throw new Error(`No blob row rendered for ${filename}`);
  return wrapper;
}

function containerOf(rowItem: Element): Element {
  const container = rowItem.querySelector(".blob-row-container");
  if (!container) throw new Error("row wrapper has no .blob-row-container");
  return container;
}

describe("the blob panel keeps one content edge (elspeth-350d5a8b0e)", () => {
  /** The inline-axis inset from a two-value padding shorthand. */
  function contentEdge(selector: string): string {
    const shorthand = declaredValue(selector, "padding").split(/\s+/);
    expect(
      shorthand,
      `${selector}'s padding shorthand must stay two-valued`,
    ).toHaveLength(2);
    return shorthand[1];
  }

  it("insets rows, header, category bands and previews to the same rung", () => {
    // Four stacked elements on three different left edges left the panel with
    // no single content margin, and an expanded preview indented AWAY from the
    // row that opened it — the detail read as less nested than its parent.
    // Asserted as agreement rather than as a literal so a future retune of the
    // panel's edge moves all four together or fails here.
    const rowEdge = contentEdge(".blob-row-container");
    for (const selector of [
      ".blob-manager-header",
      ".blob-manager-category-header",
      ".blob-row-preview",
    ]) {
      expect(
        contentEdge(selector),
        `${selector} must sit on the row's content edge`,
      ).toBe(rowEdge);
    }
    expect(
      rowEdge,
      "the content edge must come from a spacing token, not a raw literal",
    ).toMatch(/^var\(--space-[\w-]+\)$/);
  });

  it("leaves the row's BLOCK inset free to differ from its inline inset", () => {
    // Row height is a separate decision from the content edge: it is what the
    // 280px max-height on .blob-manager-container is sized against, so the
    // shorthand must stay two-valued rather than collapsing to one.
    const shorthand = declaredValue(".blob-row-container", "padding").split(/\s+/);
    expect(shorthand[0]).toMatch(/^var\(--space-[\w-]+\)$/);
  });
});

describe("blob row dividers live in CSS, not on the element (elspeth-a1a1b62aa9)", () => {
  beforeEach(() => {
    useSessionStore.setState({ activeSessionId: "session-1", isComposing: false });
    vi.clearAllMocks();
  });

  it("gives every row its divider from the stylesheet, with no inline style", () => {
    const { container } = renderTwoCategories();

    // The stylesheet is where the divider comes from now...
    expect(declaredValue(".blob-row-container", "border-bottom")).not.toBe("none");

    // ...and no row carries a border decision of its own. This is the whole
    // point: a row that styles itself cannot know whether it is the last one.
    const rows = Array.from(container.querySelectorAll(".blob-row-container"));
    expect(rows).toHaveLength(4);
    for (const row of rows) {
      expect(
        row.getAttribute("style"),
        "a row must not carry an inline border — it cannot know if it is last",
      ).toBeNull();
    }
  });

  it("strips the divider from the last row of the LAST category only", () => {
    const { container } = renderTwoCategories();
    const stripped = undividedIn(container);

    const lastRowOverall = containerOf(rowItemFor(container, "last-output.csv"));
    expect(
      stripped.has(lastRowOverall),
      "the final row's divider would otherwise double up with .chat-input's border-top",
    ).toBe(true);

    // Exactly one row loses its divider — and it is that one.
    const strippedRows = Array.from(stripped).filter((element) =>
      element.classList.contains("blob-row-container"),
    );
    expect(strippedRows).toEqual([lastRowOverall]);
  });

  it("keeps the divider under the last row of a NON-final category", () => {
    // The regression a bare `.blob-row-item:last-child` would introduce:
    // .blob-manager-category-header has border-bottom and no border-top, so
    // without this row's divider the last Source row merges into the OUTPUT
    // FILES band below it.
    const { container } = renderTwoCategories();
    const stripped = undividedIn(container);

    const lastSourceRow = containerOf(rowItemFor(container, "last-source.csv"));
    expect(
      stripped.has(lastSourceRow),
      "this row is followed by a category band with no top border; it needs its divider",
    ).toBe(false);

    // Sanity: there really is another category group below this row, so the
    // divider it keeps is the only rule separating the two bands.
    // (.blob-manager-category-header ships border-bottom and no border-top; if
    // a later pass gives it one, this row's divider becomes redundant rather
    // than wrong — deliberately not asserted here, since that is the band's
    // decision to make, not this rule's.)
    expect(lastSourceRow.closest(".blob-row-item")!.parentElement!.nextElementSibling)
      .not.toBeNull();
  });
});

describe("an open preview takes over its row's divider (elspeth-a1a1b62aa9)", () => {
  beforeEach(() => {
    useSessionStore.setState({ activeSessionId: "session-1", isComposing: false });
    vi.clearAllMocks();
    (previewBlobContentSnippet as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: "a,b\n1,2\n",
      truncated: false,
      limit: 5000,
    });
  });

  it("hands the divider to the preview mid-list, and drops both at the very bottom", async () => {
    const user = userEvent.setup();
    const { container } = renderTwoCategories();

    // The preview panel is the element that carries the divider while open.
    expect(declaredValue(".blob-row-preview", "border-bottom")).not.toBe("none");

    // Mid-list: the row hands its border down to the preview, which keeps it
    // so the expanded pair still closes against the next row.
    await user.click(screen.getByRole("button", { name: /Preview last-source\.csv/ }));
    const midItem = rowItemFor(container, "last-source.csv");
    await waitFor(() => expect(midItem.querySelector(".blob-row-preview")).not.toBeNull());
    expect(midItem.classList.contains("blob-row-item--preview-open")).toBe(true);

    let stripped = undividedIn(container);
    expect(stripped.has(containerOf(midItem))).toBe(true);
    expect(
      stripped.has(midItem.querySelector(".blob-row-preview")!),
      "a mid-list preview still closes against the row below it",
    ).toBe(false);

    // At the very bottom BOTH go: the preview's border is the one that would
    // land on .chat-input's border-top once the row's is already suppressed.
    await user.click(screen.getByRole("button", { name: /Preview last-output\.csv/ }));
    const lastItem = rowItemFor(container, "last-output.csv");
    await waitFor(() => expect(lastItem.querySelector(".blob-row-preview")).not.toBeNull());

    stripped = undividedIn(container);
    expect(stripped.has(containerOf(lastItem))).toBe(true);
    expect(
      stripped.has(lastItem.querySelector(".blob-row-preview")!),
      "an open preview on the final row is what abuts the chat input",
    ).toBe(true);
  });
});
