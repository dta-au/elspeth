import { describe, expect, it, vi, beforeEach } from "vitest";
import { act } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { previewBlobContent, previewBlobContentSnippet } from "@/api/client";
import type { BlobMetadata } from "@/types/api";
import { BlobRow } from "./BlobRow";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { resetStore } from "@/test/store-helpers";
import { expectNoIdentifiersInDefaultDom } from "@/test/defaultDomPins";

vi.mock("@/api/client", () => ({
  previewBlobContent: vi.fn(),
  previewBlobContentSnippet: vi.fn(),
}));

beforeEach(() => resetStore(usePreferencesStore));

function makeBlob(overrides: Partial<BlobMetadata> = {}): BlobMetadata {
  return {
    id: "blob-1",
    session_id: "session-1",
    filename: "data.csv",
    mime_type: "text/csv",
    size_bytes: 6000,
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

describe("BlobRow preview", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("loads inline preview through the bounded preview helper", async () => {
    (previewBlobContentSnippet as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: "preview text",
      truncated: false,
      limit: 5000,
    });

    const user = userEvent.setup();
    render(
      <BlobRow
        blob={makeBlob()}
        sessionId="session-1"
        onDownload={vi.fn()}
        onDelete={vi.fn()}
        onUseAsInput={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /preview data\.csv/i }));

    await waitFor(() => {
      expect(previewBlobContentSnippet).toHaveBeenCalledWith(
        "session-1",
        "blob-1",
        5000,
      );
    });
    expect(previewBlobContent).not.toHaveBeenCalled();
    expect(screen.getByText("preview text")).toBeInTheDocument();
  });
});

describe("BlobRow structural self-disclosure (T-3)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows row count and column names for a well-formed CSV, as real accessible text", async () => {
    usePreferencesStore.setState({ showAdvanced: true });
    (previewBlobContentSnippet as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: "name,age\nAlice,30\nBob,40\n",
      truncated: false,
      limit: 5000,
    });

    const user = userEvent.setup();
    render(
      <BlobRow
        blob={makeBlob({ mime_type: "text/csv" })}
        sessionId="session-1"
        onDownload={vi.fn()}
        onDelete={vi.fn()}
        onUseAsInput={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /preview data\.csv/i }));

    const summary = await screen.findByTestId("blob-row-structure");
    expect(summary).toHaveTextContent("2 rows");
    expect(summary).toHaveTextContent("columns: name, age");
    // Real text in the accessibility tree, not a title-only affordance.
    expect(summary.getAttribute("title")).toBeNull();
  });

  it("shows row count and keys for a JSON array of records", async () => {
    usePreferencesStore.setState({ showAdvanced: true });
    (previewBlobContentSnippet as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: JSON.stringify([{ id: 1, status: "ok" }, { id: 2, status: "error" }]),
      truncated: false,
      limit: 5000,
    });

    const user = userEvent.setup();
    render(
      <BlobRow
        blob={makeBlob({ filename: "rows.json", mime_type: "application/json" })}
        sessionId="session-1"
        onDownload={vi.fn()}
        onDelete={vi.fn()}
        onUseAsInput={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /preview rows\.json/i }));

    const summary = await screen.findByTestId("blob-row-structure");
    expect(summary).toHaveTextContent("2 rows");
    expect(summary).toHaveTextContent("keys: id, status");
  });

  it("pretty-prints JSON previews and can switch to a table view", async () => {
    (previewBlobContentSnippet as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: JSON.stringify([{ id: 1, status: "ok" }, { id: 2, status: "error" }]),
      truncated: false,
      limit: 5000,
    });

    const user = userEvent.setup();
    const { container } = render(
      <BlobRow
        blob={makeBlob({ filename: "rows.json", mime_type: "application/json" })}
        sessionId="session-1"
        onDownload={vi.fn()}
        onDelete={vi.fn()}
        onUseAsInput={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /preview rows\.json/i }));

    await waitFor(() =>
      expect(
        container.querySelector('[data-codeblock-format="json"]')?.textContent,
      ).toContain('"id": 1,'),
    );

    await user.click(screen.getByRole("button", { name: "Table view" }));
    expect(screen.getByText("id")).toBeInTheDocument();
    expect(screen.getByText("status")).toBeInTheDocument();
    expect(screen.getByText("error")).toBeInTheDocument();
  });

  it("HONESTY: ragged CSV rows surface a plain failure, never a guessed count", async () => {
    usePreferencesStore.setState({ showAdvanced: true });
    (previewBlobContentSnippet as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: "name,age\nAlice,30\nBob\n",
      truncated: false,
      limit: 5000,
    });

    const user = userEvent.setup();
    render(
      <BlobRow
        blob={makeBlob({ mime_type: "text/csv" })}
        sessionId="session-1"
        onDownload={vi.fn()}
        onDelete={vi.fn()}
        onUseAsInput={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /preview data\.csv/i }));

    const summary = await screen.findByTestId("blob-row-structure");
    expect(summary).toHaveTextContent(/couldn't be read/i);
    expect(summary).not.toHaveTextContent(/0 rows/);
    expect(summary).not.toHaveTextContent(/unknown row count/i);
  });

  it("HONESTY: a truncated preview shows known columns but refuses a row count", async () => {
    usePreferencesStore.setState({ showAdvanced: true });
    (previewBlobContentSnippet as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: "name,age\nAlice,30\nBob,4",
      truncated: true,
      limit: 5000,
    });

    const user = userEvent.setup();
    render(
      <BlobRow
        blob={makeBlob({ mime_type: "text/csv" })}
        sessionId="session-1"
        onDownload={vi.fn()}
        onDelete={vi.fn()}
        onUseAsInput={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /preview data\.csv/i }));

    const summary = await screen.findByTestId("blob-row-structure");
    expect(summary).toHaveTextContent(/truncated/i);
    expect(summary).toHaveTextContent("columns: name, age");
    expect(summary).not.toHaveTextContent(/\d+ rows?\b/);
  });

  it("does not render a structure block for content types with no structural handling (e.g. text/plain)", async () => {
    usePreferencesStore.setState({ showAdvanced: true });
    (previewBlobContentSnippet as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: "just some free text",
      truncated: false,
      limit: 5000,
    });

    const user = userEvent.setup();
    render(
      <BlobRow
        blob={makeBlob({ filename: "notes.txt", mime_type: "text/plain" })}
        sessionId="session-1"
        onDownload={vi.fn()}
        onDelete={vi.fn()}
        onUseAsInput={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /preview notes\.txt/i }));

    await waitFor(() => {
      expect(screen.getByText("just some free text")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("blob-row-structure")).not.toBeInTheDocument();
  });

  it("keeps the row/column counts out of the default DOM but KEEPS the structure caveat (elspeth-f1394307e3)", async () => {
    // Two assertions, opposite directions. The counts are engineer-register
    // and gate; the caveat is a data-honesty disclosure and does not — a body
    // the engine could not parse must not read as a clean preview at the
    // default detail level (see the caveat ruling).
    //
    // TRUNCATED, and the fixture choice is forced by contentStructure.ts —
    // it is the ONLY shape that carries a caveat AND a gateable summary line:
    //   ragged (:193-200)      rowCount null, fields null  -> describeStructuralSummary
    //                          returns null at EVERY level, so a "counts are
    //                          gated" assertion CANNOT FAIL. Vacuous.
    //   truncated (:203-209)   rowCount null, fields header -> caveat + a real
    //                          "columns: …" line. Gating IS observable. USE THIS.
    //   well-formed (:212)     rowCount n, fields header, caveat NULL -> no
    //                          caveat to keep. Covered by the next test.
    // No fixture carries a caveat AND a row count, which is why the row-count
    // half of the gate needs its own test below.
    (previewBlobContentSnippet as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: "name,age\nAlice,30\nBob,40\n",
      truncated: true,
      limit: 5000,
    });
    const user = userEvent.setup();
    const { container } = render(<BlobRow blob={makeBlob({ mime_type: "text/csv" })} sessionId="session-1" onDownload={vi.fn()} onDelete={vi.fn()} onUseAsInput={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /preview data\.csv/i }));
    await screen.findByText(/name,age/);                      // the preview body rendered
    // SCOPED to the block, the idiom this file already uses at :208-209. This
    // is also what disambiguates /truncated/i: the <pre>'s own
    // "... (truncated)" span (BlobRow.tsx:286-290) is OUTSIDE this element, so
    // an unscoped screen.getByText would throw "Found multiple elements".
    const summary = await screen.findByTestId("blob-row-structure");
    expect(summary).toHaveTextContent(/truncated/i);            // caveat survives
    expect(summary).not.toHaveTextContent(/columns: name, age/); // summary line gated
    // Audit-required siblings, per the Global Constraints list. The
    // RecoveryPanel flag-off test asserts its siblings; this one must too —
    // the flag-off render is a NEW code path and the existing tests now run
    // with the flag ON, so nothing else covers it.
    expect(screen.getByRole("button", { name: /download/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /delete/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /use .* as input/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /hide data\.csv/i })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Ready" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Created by user" })).toBeInTheDocument();
    expectNoIdentifiersInDefaultDom(container);
  });

  it("renders no structure block at all for a clean CSV at the default level, and the counts with the flag on (elspeth-f1394307e3)", async () => {
    // The row-count half of the gate. A well-formed CSV is the only shape with
    // a rowCount (contentStructure.ts:212), and it has caveat: null — so at the
    // default level BOTH children are absent and the tightened render
    // condition must drop the wrapper rather than emit an empty <div>.
    (previewBlobContentSnippet as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: "name,age\nAlice,30\nBob,40\n",
      truncated: false,
      limit: 5000,
    });
    const user = userEvent.setup();
    render(<BlobRow blob={makeBlob({ mime_type: "text/csv" })} sessionId="session-1" onDownload={vi.fn()} onDelete={vi.fn()} onUseAsInput={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /preview data\.csv/i }));
    await screen.findByText(/name,age/);
    expect(screen.queryByTestId("blob-row-structure")).not.toBeInTheDocument();

    // Same fixture, flag on: the counts appear. This is the assertion the
    // ragged fixture could never make, and it is what makes the gate
    // observable on the rowCount axis rather than only on the fields axis.
    act(() => {
      usePreferencesStore.setState({ showAdvanced: true });
    });
    const summary = await screen.findByTestId("blob-row-structure");
    expect(summary).toHaveTextContent(/\d+ rows?\b/);
    expect(summary).toHaveTextContent("columns: name, age");
  });
});

describe("BlobRow status indicator (WCAG 1.4.1 non-colour cue)", () => {
  it("exposes the status as an accessible image with a visible icon, not colour alone", () => {
    render(
      <BlobRow
        blob={makeBlob({ status: "ready" })}
        sessionId="session-1"
        onDownload={vi.fn()}
        onDelete={vi.fn()}
        onUseAsInput={vi.fn()}
      />,
    );

    const dot = screen.getByRole("img", { name: "Ready" });
    // A shape icon carries the cue, so the status survives colour-vision
    // deficiency rather than relying on hue.
    expect(dot.querySelector("svg[data-icon='status-ready']")).not.toBeNull();
  });

  it("renders a distinct icon per status so ready/pending/error differ by shape", () => {
    const iconFor = (status: BlobMetadata["status"]): string => {
      const { unmount } = render(
        <BlobRow
          blob={makeBlob({ status })}
          sessionId="session-1"
          onDownload={vi.fn()}
          onDelete={vi.fn()}
          onUseAsInput={vi.fn()}
        />,
      );
      const label = status.charAt(0).toUpperCase() + status.slice(1);
      const icon =
        screen
          .getByRole("img", { name: label })
          .querySelector("svg[data-icon]")
          ?.getAttribute("data-icon") ?? "";
      unmount();
      return icon;
    };

    const icons = ["ready", "pending", "error"].map((s) =>
      iconFor(s as BlobMetadata["status"]),
    );
    // All three icons must be distinct shapes (a colour-blind user can tell
    // them apart without seeing the hue).
    expect(new Set(icons).size).toBe(3);
  });

  it("uses the shared icon component for file-manager row actions", () => {
    const { container } = render(
      <BlobRow
        blob={makeBlob({ status: "ready" })}
        sessionId="session-1"
        onDownload={vi.fn()}
        onDelete={vi.fn()}
        onUseAsInput={vi.fn()}
      />,
    );

    const actionIcons = Array.from(
      container.querySelectorAll(".blob-row-actions button svg[data-icon]"),
    ).map((icon) => icon.getAttribute("data-icon"));
    expect(actionIcons).toEqual(["eye", "play", "download", "trash"]);
  });
});

describe("BlobRow creator badge", () => {
  it.each([
    ["user", "Created by user", "user"],
    ["assistant", "Created by assistant", "assistant"],
    ["pipeline", "Created by pipeline", "pipeline"],
  ] as const)(
    "exposes the %s creator cue to assistive tech",
    (createdBy, label, iconName) => {
      render(
        <BlobRow
          blob={makeBlob({ created_by: createdBy })}
          sessionId="session-1"
          onDownload={vi.fn()}
          onDelete={vi.fn()}
          onUseAsInput={vi.fn()}
        />,
      );

      const creator = screen.getByRole("img", { name: label });
      expect(creator.querySelector(`svg[data-icon='${iconName}']`)).not.toBeNull();
    },
  );
});
