import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, within, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { BlobManager } from "./BlobManager";
import type { BlobMetadata, CompositionState } from "@/types/api";

vi.mock("@/api/workflow", () => ({ fetchMyQuota: vi.fn().mockReturnValue(new Promise(() => {})) }));

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
    // Inline-blob provenance defaults (Phase 5a Task 2.5).
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

/** Set up store state with a no-op loadBlobs so the useEffect doesn't clobber isLoading. */
function setBlobState(blobs: BlobMetadata[]) {
  useBlobStore.setState({
    blobs,
    activeSessionId: "session-1",
    isLoading: false,
    error: null,
    loadBlobs: vi.fn().mockResolvedValue(undefined),
  });
}

function selectedState(options: Record<string, unknown>): CompositionState {
  return {
    id: "state-1", session_id: "session-1", version: 1,
    sources: { main: { plugin: "csv", options: { blob_ref: "upload", mode: "bind_source" } } },
    nodes: [
      { id: "llm", node_type: "transform", plugin: "llm", options, input: "rows", on_success: "out", on_error: null },
      { id: "inject", node_type: "transform", plugin: "file_inject", options: { files: { notes: { content: { blob_ref: "other", mode: "inline_content", sha256: "hash" } } } }, input: "rows", on_success: "out", on_error: null },
    ],
    edges: [], outputs: [{ name: "out", plugin: "json", options: {} }],
    metadata: { name: "test", description: "" }, is_valid: true,
    validation_errors: null, validation_warnings: null, validation_suggestions: null,
    derived_from_state_id: null, created_at: "2026-01-01T00:00:00Z",
    composer_meta: null, plugin_policy_findings: [],
  };
}

describe("BlobManager selected-node highlighting", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useSessionStore.setState({ activeSessionId: "session-1", selectedNodeId: "llm", compositionState: selectedState({
      system_prompt: { blob_ref: "ONE", mode: "inline_content", sha256: "hash" },
      prompt_template: { blob_ref: "two", mode: "inline_content", sha256: "hash" },
    }) });
    setBlobState([
      makeBlob({ id: "one", filename: "system.txt" }),
      makeBlob({ id: "two", filename: "user.txt" }),
      makeBlob({ id: "other", filename: "notes.txt" }),
      makeBlob({ id: "upload", filename: "input.csv" }),
    ]);
  });

  afterEach(() => {
    useSessionStore.setState({ selectedNodeId: null, compositionState: null });
  });

  it("highlights only the selected node's files and moves with selection", () => {
    const { container } = render(<BlobManager onUseAsInput={vi.fn()} />);
    const attached = () => Array.from(container.querySelectorAll(".blob-row-item--attached"));
    expect(attached().map((row) => row.getAttribute("data-blob-id"))).toEqual(["one", "two"]);
    expect(screen.getByRole("status")).toHaveTextContent("2 files used by llm");
    expect(screen.getAllByText(/Used by llm/)).toHaveLength(2);
    act(() => useSessionStore.setState({ selectedNodeId: "inject" }));
    expect(attached().map((row) => row.getAttribute("data-blob-id"))).toEqual(["other"]);
    expect(screen.getByRole("status")).toHaveTextContent("1 file used by inject");
    act(() => useSessionStore.setState({ selectedNodeId: null }));
    expect(attached()).toHaveLength(0);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("highlights source bindings too and reports missing files only after load", () => {
    const { container } = render(<BlobManager onUseAsInput={vi.fn()} />);
    act(() => useSessionStore.setState({ selectedNodeId: "source:main" }));
    expect(container.querySelector(".blob-row-item--attached")?.getAttribute("data-blob-id")).toBe("upload");
    act(() => useSessionStore.setState({ selectedNodeId: "llm", compositionState: selectedState({ prompt_template: { blob_ref: "missing", mode: "inline_content", sha256: "hash" } }) }));
    expect(screen.getByRole("status")).toHaveTextContent("1 attached file is not in this session");
    act(() => useBlobStore.setState({ isLoading: true }));
    expect(screen.queryByText(/attached file is not in this session/)).not.toBeInTheDocument();
  });

  it("counts a file used by two slots once and lists both slots", () => {
    useSessionStore.setState({ compositionState: selectedState({
      system_prompt: { blob_ref: "one", mode: "inline_content", sha256: "hash" },
      prompt_template: { blob_ref: "one", mode: "inline_content", sha256: "hash" },
    }) });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("1 file used by llm");
    expect(screen.getByText(/Used by llm/)).toHaveTextContent("System prompt");
    expect(screen.getByText(/Used by llm/)).toHaveTextContent("User prompt");
  });

  it("does not associate a stale session's rows with the selected node", () => {
    useBlobStore.setState({ activeSessionId: "session-2" });
    const { container } = render(<BlobManager onUseAsInput={vi.fn()} />);
    expect(container.querySelector(".blob-row-item--attached")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("scrolls the first matching row only when selection changes, without moving focus", () => {
    const scroll = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scroll;
    try {
      render(<BlobManager onUseAsInput={vi.fn()} />);
      expect(scroll).toHaveBeenCalledWith({ block: "nearest", behavior: "auto" });
      const calls = scroll.mock.calls.length;
      act(() => useBlobStore.setState({ blobs: [...useBlobStore.getState().blobs] }));
      expect(scroll).toHaveBeenCalledTimes(calls);
      act(() => useSessionStore.setState({ selectedNodeId: "inject" }));
      expect(scroll).toHaveBeenCalledTimes(calls + 1);
      expect(document.activeElement).toBe(document.body);
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});

describe("BlobManager categorized folders", () => {
  beforeEach(() => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    vi.clearAllMocks();
  });

  it("groups blobs into Source, Output, and Other sections", () => {
    setBlobState([
      makeBlob({ id: "b1", filename: "input.csv", created_by: "user" }),
      makeBlob({ id: "b2", filename: "results.json", created_by: "pipeline" }),
      makeBlob({ id: "b3", filename: "prompt.txt", created_by: "assistant" }),
    ]);

    render(<BlobManager onUseAsInput={vi.fn()} />);

    expect(screen.getByText("Source files")).toBeInTheDocument();
    expect(screen.getByText("Output files")).toBeInTheDocument();
    expect(screen.getByText("Other files")).toBeInTheDocument();
  });

  it("puts user-uploaded files in Source section", () => {
    setBlobState([makeBlob({ id: "b1", filename: "data.csv", created_by: "user" })]);

    render(<BlobManager onUseAsInput={vi.fn()} />);

    expect(screen.getByText("Source files")).toBeInTheDocument();
    expect(screen.getByText("data.csv")).toBeInTheDocument();
  });

  it("puts pipeline-created files in Output section", () => {
    setBlobState([makeBlob({ id: "b2", filename: "results.json", created_by: "pipeline" })]);

    render(<BlobManager onUseAsInput={vi.fn()} />);

    expect(screen.getByText("Output files")).toBeInTheDocument();
    expect(screen.getByText("results.json")).toBeInTheDocument();
  });

  it("shows empty state for empty file list", () => {
    setBlobState([]);

    render(<BlobManager onUseAsInput={vi.fn()} />);

    expect(screen.getByText(/No files yet/)).toBeInTheDocument();
  });

  it("hides empty categories", () => {
    setBlobState([makeBlob({ id: "b1", filename: "data.csv", created_by: "user" })]);

    render(<BlobManager onUseAsInput={vi.fn()} />);

    expect(screen.getByText("Source files")).toBeInTheDocument();
    expect(screen.queryByText("Output files")).not.toBeInTheDocument();
    expect(screen.queryByText("Other files")).not.toBeInTheDocument();
  });
});

describe("BlobManager upload affordance (elspeth-29eef452a8)", () => {
  beforeEach(() => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    vi.clearAllMocks();
  });

  it("names the upload control by its own visible text", () => {
    setBlobState([]);
    render(<BlobManager onUseAsInput={vi.fn()} />);

    // getByRole computes the ACCESSIBLE name, so finding it by "Upload" and
    // then matching the rendered text proves the two agree (WCAG 2.5.3 Label
    // in Name — voice control targets a control by what the user can read).
    // Before the fix the visible label was "+ Upload" against an accessible
    // name of "Upload file", which shares no full label with it.
    const upload = screen.getByRole("button", { name: "Upload" });
    expect(upload.textContent?.trim()).toBe("Upload");
    expect(
      upload,
      "an aria-label here can only drift from the visible text",
    ).not.toHaveAttribute("aria-label");
  });

  it("draws no glyph in the UI text font", () => {
    // The label led with a typed "+": a glyph rendered at the button's 12px UI
    // size, matching neither the weight nor the optical size of the stroked
    // SVGs in the rows directly below it. A real icon may be added later — the
    // constraint is only that a punctuation character must not stand in for one.
    setBlobState([]);
    render(<BlobManager onUseAsInput={vi.fn()} />);

    const upload = screen.getByRole("button", { name: "Upload" });
    expect(upload.textContent).not.toMatch(/[+*×•▸◦→↑]/);
  });
});

describe("BlobManager compose-busy gating (elspeth-3f38ebb1b5)", () => {
  beforeEach(() => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    vi.clearAllMocks();
  });

  it("disables Use-as-input while a freeform compose is active", async () => {
    // Use-as-input starts a compose: while one is in flight it must not
    // offer a second entry point (the store admission gate refuses it; the
    // affordance must say so).
    const onUseAsInput = vi.fn();
    setBlobState([makeBlob({ id: "b1", filename: "data.csv", created_by: "user" })]);
    useSessionStore.setState({ isComposing: true });

    const user = userEvent.setup();
    render(<BlobManager onUseAsInput={onUseAsInput} />);

    const useButton = screen.getByRole("button", { name: "Use data.csv as input" });
    expect(useButton).toBeDisabled();
    await user.click(useButton).catch(() => undefined);
    expect(onUseAsInput).not.toHaveBeenCalled();
  });

  it("keeps Use-as-input enabled when idle (control)", async () => {
    const onUseAsInput = vi.fn();
    setBlobState([makeBlob({ id: "b1", filename: "data.csv", created_by: "user" })]);
    useSessionStore.setState({ isComposing: false });

    const user = userEvent.setup();
    render(<BlobManager onUseAsInput={onUseAsInput} />);

    await user.click(screen.getByRole("button", { name: "Use data.csv as input" }));
    expect(onUseAsInput).toHaveBeenCalledTimes(1);
  });
});

describe("BlobManager delete confirmation (WCAG 3.3.4)", () => {
  beforeEach(() => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    vi.clearAllMocks();
  });

  it("requires confirmation before deleting a file, then deletes on confirm", async () => {
    const deleteBlob = vi.fn().mockResolvedValue(undefined);
    setBlobState([makeBlob({ id: "b1", filename: "data.csv", created_by: "user" })]);
    useBlobStore.setState({ deleteBlob });

    const user = userEvent.setup();
    render(<BlobManager onUseAsInput={vi.fn()} />);

    // Requesting deletion opens a danger dialog naming the file — and does NOT
    // delete immediately (irreversible data-loss guard).
    await user.click(screen.getByRole("button", { name: "Delete data.csv" }));
    const dialog = screen.getByRole("alertdialog");
    expect(
      within(dialog).getByText(/Delete "data\.csv"\? This cannot be undone\./),
    ).toBeInTheDocument();
    expect(deleteBlob).not.toHaveBeenCalled();

    // Confirming fires the delete with the active session + blob id.
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(deleteBlob).toHaveBeenCalledWith("session-1", "b1");
  });

  it("deletes from the session that opened the dialog, even after the active session changes", async () => {
    // Regression: the confirmation captures the blob *and* the session it
    // belonged to. Switching sessions (e.g. via the global Ctrl/Cmd+N shortcut)
    // while the dialog is open must not retarget the delete at the new session,
    // which would leave the original file undeleted.
    const deleteBlob = vi.fn().mockResolvedValue(undefined);
    setBlobState([makeBlob({ id: "b1", filename: "data.csv", created_by: "user" })]);
    useBlobStore.setState({ deleteBlob });

    const user = userEvent.setup();
    render(<BlobManager onUseAsInput={vi.fn()} />);

    // Request deletion while session-1 is active.
    await user.click(screen.getByRole("button", { name: "Delete data.csv" }));

    // The active session changes underneath the open confirmation dialog.
    act(() => {
      useSessionStore.setState({ activeSessionId: "session-2" });
    });

    // Confirming must target the ORIGINAL session (session-1), not session-2.
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(deleteBlob).toHaveBeenCalledWith("session-1", "b1");
  });

  it("leaves the file intact when the dialog is cancelled", async () => {
    const deleteBlob = vi.fn().mockResolvedValue(undefined);
    setBlobState([makeBlob({ id: "b1", filename: "data.csv", created_by: "user" })]);
    useBlobStore.setState({ deleteBlob });

    const user = userEvent.setup();
    render(<BlobManager onUseAsInput={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Delete data.csv" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(deleteBlob).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});
