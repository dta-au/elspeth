import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from "vitest";
import { render, screen, within, act, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as api from "@/api/client";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { BlobManager } from "./BlobManager";
import type { BlobMetadata, CompositionState } from "@/types/api";
import { COMPOSE_LOADING_SESSION_MESSAGE } from "@/config/composer";

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
    listError: null,
    listLoadFailed: false,
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
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
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
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("keeps the same live status element mounted while selection changes", () => {
    act(() => useSessionStore.setState({ selectedNodeId: null }));
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const status = screen.getByRole("status");
    expect(status).toBeEmptyDOMElement();
    act(() => useSessionStore.setState({ selectedNodeId: "llm" }));
    expect(screen.getByRole("status")).toBe(status);
    expect(status).toHaveTextContent("2 files used by llm");
    act(() => useSessionStore.setState({ selectedNodeId: null }));
    expect(screen.getByRole("status")).toBe(status);
    expect(status).toBeEmptyDOMElement();
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

describe("BlobManager reveal request", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useBlobStore.getState().reset();
    useSessionStore.setState({ activeSessionId: "session-1", selectedNodeId: "llm",
      compositionState: selectedState({ prompt_template: { blob_ref: "two", mode: "inline_content", sha256: "hash" },
        system_prompt: { blob_ref: "one", mode: "inline_content", sha256: "hash" } }) });
    setBlobState([makeBlob({ id: "one", filename: "system.txt", mime_type: "text/plain" }),
      makeBlob({ id: "two", filename: "user.txt", mime_type: "text/plain" }), makeBlob({ id: "three" })]);
  });

  it("focuses and scrolls the requested row while sibling files remain highlighted", () => {
    const scroll = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scroll;
    try {
      useBlobStore.getState().revealBlob("session-1", "two", "user.txt");
      const { container } = render(<BlobManager onUseAsInput={vi.fn()} />);
      const focused = container.querySelector(".blob-row-item--focused");
      expect(focused).toHaveAttribute("data-blob-id", "two");
      expect(document.activeElement).toBe(focused);
      expect(scroll).toHaveBeenCalledWith({ block: "nearest" });
      expect(container.querySelector('[data-blob-id="one"]')).toHaveClass("blob-row-item--attached");
      expect(container.querySelector('[data-blob-id="one"]')).not.toHaveClass("blob-row-item--focused");
      expect(useBlobStore.getState().revealRequest).toBeNull();
      expect(screen.getByRole("status")).toHaveTextContent("Showing user.txt.");
    } finally { Element.prototype.scrollIntoView = original; }
  });

  it("waits for loading and focuses the status when the requested file has disappeared", () => {
    useBlobStore.setState({ isLoading: true });
    useBlobStore.getState().revealBlob("session-1", "missing", "missing.txt");
    const { container } = render(<BlobManager onUseAsInput={vi.fn()} />);
    expect(useBlobStore.getState().revealRequest).not.toBeNull();
    act(() => useBlobStore.setState({ isLoading: false }));
    expect(container.querySelector(".blob-row-item--focused")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("missing.txt is no longer in this session");
    expect(screen.getByRole("status")).toHaveFocus();
  });

  it("waits for a pending list before focusing an arriving file", () => {
    useBlobStore.setState({ blobs: [], isLoading: true });
    useBlobStore.getState().revealBlob("session-1", "two", "user.txt");
    const { container } = render(<BlobManager onUseAsInput={vi.fn()} />);
    expect(useBlobStore.getState().revealRequest).not.toBeNull();
    act(() => useBlobStore.setState({ blobs: [makeBlob({ id: "two", filename: "user.txt" })], isLoading: false }));
    expect(container.querySelector('[data-blob-id="two"]')).toHaveFocus();
    expect(useBlobStore.getState().revealRequest).toBeNull();
  });

  it("waits for a hidden authoring pane to reveal before focusing its row", async () => {
    useBlobStore.getState().revealBlob("session-1", "two", "user.txt");
    const contents = <BlobManager onUseAsInput={vi.fn()} />;
    const { container, rerender } = render(<div hidden>{contents}</div>);
    expect(container.querySelector('[data-blob-id="two"]')).not.toHaveFocus();
    expect(useBlobStore.getState().revealRequest).not.toBeNull();
    rerender(<div>{contents}</div>);
    await waitFor(() => expect(container.querySelector('[data-blob-id="two"]')).toHaveFocus());
    expect(useBlobStore.getState().revealRequest).toBeNull();
  });

  it("returns focus to the Show in files trigger on Escape", () => {
    const trigger = document.createElement("button");
    document.body.appendChild(trigger);
    useBlobStore.getState().revealBlob("session-1", "two", "user.txt", trigger);
    const { container } = render(<BlobManager onUseAsInput={vi.fn()} />);
    expect(container.querySelector('[data-blob-id="two"]')).toHaveFocus();
    fireEvent.keyDown(container.querySelector(".blob-manager") as HTMLElement, { key: "Escape" });
    expect(useBlobStore.getState().fileManagerOpen).toBe(false);
    expect(trigger).toHaveFocus();
    trigger.remove();
  });

  it("offers Edit prompt for every prompt-bound file, including one that backs two prompts", () => {
    useSessionStore.setState({ compositionState: selectedState({ prompt_template: { blob_ref: "two", mode: "inline_content", sha256: "hash" } }) });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const edit = screen.getByRole("button", { name: "Edit prompt user.txt" });
    expect(edit).toHaveAttribute("aria-disabled", "false");
    // The design system's ghost compact button, not a raw browser button.
    expect(edit).toHaveClass("btn-compact", "btn-ghost");
    act(() => edit.click());
    expect(useSessionStore.getState().selectedNodeId).toBe("llm");
    expect(useBlobStore.getState().editRequest).toMatchObject({ nodeId: "llm", fieldPath: "prompt_template" });
    // The same file on a second prompt slot: the control stays, and still opens the editor.
    act(() => useBlobStore.setState({ editRequest: null }));
    act(() => useSessionStore.setState({ compositionState: selectedState({ prompt_template: { blob_ref: "two", mode: "inline_content", sha256: "hash" },
      system_prompt: { blob_ref: "two", mode: "inline_content", sha256: "hash" } }) }));
    const shared = screen.getByRole("button", { name: "Edit prompt user.txt" });
    expect(shared).toHaveAttribute("aria-disabled", "false");
    act(() => shared.click());
    expect(useBlobStore.getState().editRequest).toMatchObject({ nodeId: "llm" });
  });

  it("opens a twice-bound file on the selected step's binding, else on the first", () => {
    const marker = { blob_ref: "two", mode: "inline_content", sha256: "hash" };
    const base = selectedState({ prompt_template: marker });
    const state: CompositionState = { ...base, nodes: [...base.nodes,
      { id: "llm-2", node_type: "transform", plugin: "llm", options: { system_prompt: marker }, input: "rows", on_success: "out", on_error: null }] };
    useSessionStore.setState({ compositionState: state, selectedNodeId: "llm-2" });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    act(() => screen.getByRole("button", { name: "Edit prompt user.txt" }).click());
    expect(useBlobStore.getState().editRequest).toMatchObject({ nodeId: "llm-2", fieldPath: "system_prompt" });
    expect(useSessionStore.getState().selectedNodeId).toBe("llm-2");
    // A step that does not use the file is selected: the first binding is opened.
    act(() => useSessionStore.setState({ selectedNodeId: "inject" }));
    act(() => screen.getByRole("button", { name: "Edit prompt user.txt" }).click());
    expect(useBlobStore.getState().editRequest).toMatchObject({ nodeId: "llm", fieldPath: "prompt_template" });
  });

  it("keeps Edit prompt visible but disabled, with the reason, for a bound file that cannot be opened", () => {
    useSessionStore.setState({ compositionState: selectedState({ prompt_template: { blob_ref: "two", mode: "inline_content", sha256: "hash" } }) });
    useBlobStore.setState({ blobs: useBlobStore.getState().blobs.map((blob) => blob.id === "two" ? { ...blob, status: "pending" as const } : blob) });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const edit = screen.getByRole("button", { name: "Edit prompt user.txt" });
    expect(edit).toHaveAttribute("aria-disabled", "true");
    expect(edit).toHaveAccessibleDescription(/This file is still being processed\./);
    act(() => edit.click());
    expect(useBlobStore.getState().editRequest).toBeNull();
  });
});

describe("BlobManager file filter", () => {
  beforeEach(() => {
    useSessionStore.setState({ activeSessionId: "session-1", selectedNodeId: null, compositionState: null });
    setBlobState([makeBlob({ id: "one", filename: "system.txt" }), makeBlob({ id: "two", filename: "User-Prompt.txt" }),
      makeBlob({ id: "three", filename: "data.csv" })]);
  });

  it("narrows the list by file name and says when nothing matches", async () => {
    const { container } = render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    const filter = screen.getByRole("searchbox", { name: "Filter files" });
    await user.type(filter, "prompt");
    expect(Array.from(container.querySelectorAll(".blob-row-item")).map((row) => row.getAttribute("data-blob-id"))).toEqual(["two"]);
    expect(screen.getByText("1 of 3")).toBeInTheDocument();
    await user.clear(filter);
    await user.type(filter, "zzz");
    expect(container.querySelector(".blob-row-item")).toBeNull();
    expect(screen.getByText("No file name matches this filter.")).toBeInTheDocument();
    await user.clear(filter);
    expect(container.querySelectorAll(".blob-row-item")).toHaveLength(3);
  });

  it("clears the filter to reveal a file it hides, instead of calling it missing", async () => {
    const { container } = render(<BlobManager onUseAsInput={vi.fn()} />);
    await userEvent.setup().type(screen.getByRole("searchbox", { name: "Filter files" }), "csv");
    act(() => useBlobStore.getState().revealBlob("session-1", "two", "User-Prompt.txt"));
    expect(screen.getByRole("searchbox", { name: "Filter files" })).toHaveValue("");
    expect(document.activeElement).toBe(container.querySelector('[data-blob-id="two"]'));
    expect(screen.getByRole("status")).not.toHaveTextContent("no longer in this session");
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

  it("shows no file count until the list has been read, and says it is loading", () => {
    setBlobState([]);
    useBlobStore.setState({ isLoading: true });
    const { rerender } = render(<BlobManager onUseAsInput={vi.fn()} />);
    expect(screen.getByText("Files", { selector: ".blob-manager-title" })).toBeInTheDocument();
    expect(screen.queryByText(/Files \(0\)/)).not.toBeInTheDocument();
    expect(screen.getByText("Loading files…")).toHaveAttribute("role", "status");
    act(() => useBlobStore.setState({
      isLoading: false, error: "Failed to load files.", listError: "Failed to load files.", listLoadFailed: true,
    }));
    rerender(<BlobManager onUseAsInput={vi.fn()} />);
    expect(screen.getByText("Files", { selector: ".blob-manager-title" })).toBeInTheDocument();
    act(() => useBlobStore.setState({ error: null, listError: null, listLoadFailed: false }));
    rerender(<BlobManager onUseAsInput={vi.fn()} />);
    expect(screen.getByText("Files (0)", { selector: ".blob-manager-title" })).toBeInTheDocument();
  });

  it("offers Retry for a failed list read, re-issuing the same read", async () => {
    setBlobState([]);
    useBlobStore.setState({ error: "Failed to load files.", listError: "Failed to load files.", listLoadFailed: true });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const loadBlobs = useBlobStore.getState().loadBlobs;
    vi.mocked(loadBlobs).mockClear();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Failed to load files.");
    await userEvent.setup().click(within(alert).getByRole("button", { name: "Retry" }));
    expect(loadBlobs).toHaveBeenCalledWith("session-1");
  });

  it("offers no Retry for a failed upload or download", () => {
    setBlobState([]);
    useBlobStore.setState({ error: "Download failed.", listLoadFailed: false });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Download failed.");
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it.each([
    ["refused", "File exceeds the maximum upload size."],
    ["successful", "Failed to load files."],
  ] as const)("keeps Retry after a %s upload following a failed list read", async (outcome, message) => {
    setBlobState([]);
    const list = vi.spyOn(api, "listBlobs").mockRejectedValueOnce(new Error("Network error"));
    const upload = vi.spyOn(api, "uploadBlob");
    onTestFinished(() => {
      list.mockRestore();
      upload.mockRestore();
      useBlobStore.getState().reset();
    });
    useBlobStore.setState({ loadBlobs: useBlobStore.getInitialState().loadBlobs });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    await screen.findByRole("button", { name: "Retry" });

    const file = new File(["content"], "new.csv");
    await act(async () => {
      if (outcome === "refused") {
        const refusal = { status: 413 };
        upload.mockRejectedValueOnce(refusal);
        await expect(useBlobStore.getState().uploadBlob("session-1", file)).rejects.toBe(refusal);
      } else {
        upload.mockResolvedValueOnce(makeBlob({ filename: file.name }));
        await useBlobStore.getState().uploadBlob("session-1", file);
      }
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(message);
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByText("Files", { selector: ".blob-manager-title" })).toBeInTheDocument();
  });

  it("closes from a design-system button at the end of the header", async () => {
    setBlobState([]);
    useBlobStore.setState({ fileManagerOpen: true });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const close = screen.getByRole("button", { name: "Close file manager" });
    expect(close).toHaveClass("btn-compact", "btn-ghost");
    // Visible "Close" leads the accessible name (WCAG 2.5.3).
    expect(close.textContent?.trim()).toBe("Close");
    const header = close.parentElement as HTMLElement;
    const buttons = within(header).getAllByRole("button");
    expect(buttons[buttons.length - 1]).toBe(close);
    await userEvent.setup().click(close);
    expect(useBlobStore.getState().fileManagerOpen).toBe(false);
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

  it("shows an upload in progress on the Upload control until it settles", async () => {
    setBlobState([]);
    const realUpload = useBlobStore.getState().uploadBlob;
    onTestFinished(() => useBlobStore.setState({ uploadBlob: realUpload }));
    let finishUpload: (blob: BlobMetadata) => void = () => undefined;
    useBlobStore.setState({
      uploadBlob: vi.fn().mockReturnValue(new Promise<BlobMetadata>((resolve) => { finishUpload = resolve; })),
    });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const fileInput = document.querySelector<HTMLInputElement>('.blob-manager-header input[type="file"]');
    expect(fileInput).not.toBeNull();
    await userEvent.setup().upload(fileInput!, new File(["id\n1\n"], "rows.csv", { type: "text/csv" }));

    expect(screen.getByRole("button", { name: "Uploading…" })).toBeDisabled();
    expect(screen.getByText("Uploading rows.csv…")).toHaveAttribute("aria-live", "polite");
    await act(async () => { finishUpload(makeBlob({ id: "rows", filename: "rows.csv" })); });
    expect(screen.getByRole("button", { name: "Upload" })).toBeEnabled();
    expect(screen.queryByText("Uploading rows.csv…")).not.toBeInTheDocument();
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
    useSessionStore.setState({ activeSessionId: "session-1", compositionStateLoaded: true });
    vi.clearAllMocks();
  });
  afterEach(() => {
    useSessionStore.setState({ compositionStateLoaded: false });
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

  it("disables Use-as-input while the session has not finished loading", async () => {
    // The store admission gate also refuses a compose before the session's
    // state is loaded (compositionStateLoaded), the same flag the chat box waits on.
    const onUseAsInput = vi.fn();
    setBlobState([makeBlob({ id: "b1", filename: "data.csv", created_by: "user" })]);
    useSessionStore.setState({ isComposing: false, compositionStateLoaded: false });
    const user = userEvent.setup();
    render(<BlobManager onUseAsInput={onUseAsInput} />);
    const useButton = screen.getByRole("button", { name: "Use data.csv as input" });
    expect(useButton).toBeDisabled();
    await user.click(useButton).catch(() => undefined);
    expect(onUseAsInput).not.toHaveBeenCalled();
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

describe("BlobManager file wiring", () => {
  const originalComposeRequest = useSessionStore.getState().composeRequest;
  const originalSendMessage = useSessionStore.getState().sendMessage;
  beforeEach(() => {
    useSessionStore.setState({ activeSessionId: "session-1", isComposing: false, compositionStateLoaded: true, composeRequests: new Map(),
      compositionState: selectedState({ prompt_template: "Original prompt." }) });
    setBlobState([makeBlob({ id: "replacement", filename: "prompt.txt", mime_type: "text/plain", content_hash: "hash" })]);
  });
  afterEach(() => {
    useSessionStore.setState({ composeRequest: originalComposeRequest, sendMessage: originalSendMessage, compositionState: null,
      isComposing: false, compositionStateLoaded: false, composeRequests: new Map() });
    useBlobStore.setState({ fileManagerOpen: false });
  });

  it("opens the wiring pad beside the file actions and sends through the real compose lifecycle", async () => {
    // Real composeRequest (its admission gate included); only the network-facing store send is stubbed.
    const storeSend = vi.fn<(content: string, signal?: AbortSignal) => Promise<void>>().mockResolvedValue(undefined);
    useSessionStore.setState({ sendMessage: storeSend });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    expect(storeSend).not.toHaveBeenCalled();
    await user.click(screen.getByRole("checkbox", { name: /User prompt/ }));
    await user.click(screen.getByRole("button", { name: "Connect selected prompts" }));
    expect(storeSend).toHaveBeenCalledOnce();
    expect(storeSend.mock.calls[0][0]).toContain("node:llm.options.prompt_template");
    expect(screen.queryByRole("dialog", { name: "File wiring pad" })).not.toBeInTheDocument();
  });

  it("refuses to connect before the session has loaded, with the reason, and keeps the typed request", async () => {
    // Real composeRequest: its gate would drop this request silently, so the pad must not offer it.
    const storeSend = vi.fn<(content: string, signal?: AbortSignal) => Promise<void>>().mockResolvedValue(undefined);
    useSessionStore.setState({ sendMessage: storeSend, compositionStateLoaded: false });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    await user.click(screen.getByRole("radio", { name: "Ask Composer" }));
    const box = screen.getByRole("textbox", { name: "What should Composer connect?" });
    await user.type(box, "Use this file for the scoring prompt.");
    const connect = screen.getByRole("button", { name: "Ask Composer to connect" });
    expect(connect).toBeDisabled();
    expect(screen.getByText(COMPOSE_LOADING_SESSION_MESSAGE)).toBeInTheDocument();
    await user.click(connect);
    expect(storeSend).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "File wiring pad" })).toBeInTheDocument();
    expect(box).toHaveValue("Use this file for the scoring prompt.");
    // Once the session loads the same pad sends the kept text.
    act(() => useSessionStore.setState({ compositionStateLoaded: true }));
    await user.click(screen.getByRole("button", { name: "Ask Composer to connect" }));
    expect(storeSend).toHaveBeenCalledWith(expect.stringContaining("Use this file for the scoring prompt."), expect.anything());
    expect(screen.queryByRole("dialog", { name: "File wiring pad" })).not.toBeInTheDocument();
  });

  it("stays open with the user's input when Composer does not take the request", async () => {
    // A composeRequest that returns without registering a request is what a refused admission looks like.
    const composeRequest = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ composeRequest });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    const slot = screen.getByRole("checkbox", { name: /User prompt/ });
    await user.click(slot);
    await user.click(screen.getByRole("button", { name: "Connect selected prompts" }));
    expect(composeRequest).toHaveBeenCalledOnce();
    expect(screen.getByRole("dialog", { name: "File wiring pad" })).toBeInTheDocument();
    const status = within(screen.getByRole("dialog", { name: "File wiring pad" })).getByRole("status");
    expect(status).toHaveTextContent("Composer did not take this request.");
    // Beside the button it explains, which names it as its description.
    expect(status.closest("footer")).toBe(screen.getByRole("button", { name: "Connect selected prompts" }).closest("footer"));
    expect(screen.getByRole("button", { name: "Connect selected prompts" })).toHaveAccessibleDescription(/^Composer did not take this request\./);
    expect(slot).toBeChecked();
  });

  it("refuses while another request for the session is in flight", async () => {
    const composeRequest = vi.fn();
    useSessionStore.setState({ composeRequest });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    await user.click(screen.getByRole("checkbox", { name: /User prompt/ }));
    act(() => useSessionStore.setState({ composeRequests: new Map([["session-1", { current: null }]]) }));
    expect(screen.getByRole("button", { name: "Connect selected prompts" })).toBeDisabled();
    expect(within(screen.getByRole("dialog", { name: "File wiring pad" })).getByRole("status")).toHaveTextContent("Wait for Composer to finish before connecting this file.");
    expect(composeRequest).not.toHaveBeenCalled();
  });

  it("stays open when a request registered after the last render makes the real gate refuse", async () => {
    // Another surface registers a request between the pad's render and the
    // click: the click still runs (old render), the real composeRequest
    // refuses, and the earlier owner must not be read as acceptance.
    const storeSend = vi.fn<(content: string, signal?: AbortSignal) => Promise<void>>().mockResolvedValue(undefined);
    useSessionStore.setState({ sendMessage: storeSend });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    await user.click(screen.getByRole("checkbox", { name: /User prompt/ }));
    const connect = screen.getByRole("button", { name: "Connect selected prompts" });
    act(() => {
      useSessionStore.setState({ composeRequests: new Map([["session-1", { current: null }]]) });
      connect.click();
    });
    expect(storeSend).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "File wiring pad" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /User prompt/ })).toBeChecked();
  });

  it("re-announces a repeated refusal and clears it when the pad is reopened", async () => {
    const composeRequest = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ composeRequest });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    await user.click(screen.getByRole("checkbox", { name: /User prompt/ }));
    await user.click(screen.getByRole("button", { name: "Connect selected prompts" }));
    const padStatus = () => within(screen.getByRole("dialog", { name: "File wiring pad" })).getByRole("status");
    const first = padStatus().firstElementChild;
    await user.click(screen.getByRole("button", { name: "Connect selected prompts" }));
    const second = padStatus().firstElementChild;
    expect(second).toHaveTextContent("Composer did not take this request.");
    expect(second).not.toBe(first);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    expect(screen.getByRole("dialog", { name: "File wiring pad" })).toBeInTheDocument();
    expect(padStatus()).toBeEmptyDOMElement();
  });

  it("keeps Shift+Tab inside the pad after a click on its text", async () => {
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    await user.click(screen.getByText(/Choose the prompts yourself/));
    const dialog = screen.getByRole("dialog", { name: "File wiring pad" });
    expect(document.activeElement).toBe(dialog);
    await user.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).not.toBe(dialog);
  });

  it("refuses a connection if the file changes while the pad is open", async () => {
    const composeRequest = vi.fn();
    useSessionStore.setState({ composeRequest });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    await user.click(screen.getByRole("checkbox", { name: /User prompt/ }));
    act(() => useBlobStore.setState({ blobs: [makeBlob({ id: "replacement", filename: "prompt.txt", mime_type: "text/plain", content_hash: "new-hash" })] }));
    expect(screen.getByRole("button", { name: "Connect selected prompts" })).toBeDisabled();
    expect(screen.getByText("This file changed or is unavailable. Close the wiring pad and reopen it.")).toBeInTheDocument();
    expect(composeRequest).not.toHaveBeenCalled();
  });

  it.each([
    ["the file is still processing", "This file is not ready yet. Wait for it to finish processing.",
      () => setBlobState([makeBlob({ id: "replacement", filename: "prompt.txt", mime_type: "text/plain", status: "pending" })])],
    ["the file list belongs to another session", "Wait for the file list to load.", () => useBlobStore.setState({ activeSessionId: "session-2" })],
  ])("refuses the wiring control while %s, saying why, and stays focusable", async (_case, reason, arrange) => {
    arrange();
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const wiring = screen.getByRole("button", { name: "Wiring prompt.txt" });
    expect(wiring).toHaveAttribute("aria-disabled", "true");
    expect(wiring).toBeEnabled();
    expect(wiring).toHaveAccessibleDescription(reason);
    expect(wiring).toHaveAttribute("title", reason);
    await userEvent.setup().click(wiring);
    expect(screen.queryByRole("dialog", { name: "File wiring pad" })).not.toBeInTheDocument();
  });

  it("offers no wiring control for a file that cannot hold prompt text", () => {
    setBlobState([makeBlob({ id: "image", filename: "chart.png", mime_type: "image/png", content_hash: "hash" })]);
    render(<BlobManager onUseAsInput={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Wiring chart.png" })).not.toBeInTheDocument();
  });

  it("withholds the old dialog after a session switch, including on return", async () => {
    const composeRequest = vi.fn();
    useSessionStore.setState({ composeRequest });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    act(() => useSessionStore.setState({ activeSessionId: "session-2" }));
    expect(screen.queryByRole("dialog", { name: "File wiring pad" })).not.toBeInTheDocument();
    act(() => useSessionStore.setState({ activeSessionId: "session-1" }));
    expect(screen.queryByRole("dialog", { name: "File wiring pad" })).not.toBeInTheDocument();
    expect(composeRequest).not.toHaveBeenCalled();
  });

  it("refuses a connection if the graph changes while the pad is open", async () => {
    const composeRequest = vi.fn();
    useSessionStore.setState({ composeRequest });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    await user.click(screen.getByRole("checkbox", { name: /User prompt/ }));
    act(() => useSessionStore.setState({ compositionState: { ...selectedState({ prompt_template: "New prompt." }), id: "state-2" } }));
    expect(screen.getByRole("button", { name: "Connect selected prompts" })).toBeDisabled();
    expect(screen.getByText("The graph changed. Close the wiring pad and reopen it.")).toBeInTheDocument();
    expect(composeRequest).not.toHaveBeenCalled();
  });

  it("refuses a connection while Composer is busy", async () => {
    const composeRequest = vi.fn();
    useSessionStore.setState({ composeRequest });
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    await user.click(screen.getByRole("checkbox", { name: /User prompt/ }));
    act(() => useSessionStore.setState({ isComposing: true }));
    expect(screen.getByRole("button", { name: "Connect selected prompts" })).toBeDisabled();
    expect(within(screen.getByRole("dialog", { name: "File wiring pad" })).getByRole("status")).toHaveTextContent("Wait for Composer to finish before connecting this file.");
    expect(composeRequest).not.toHaveBeenCalled();
  });

  it("closes on Escape after a click on the pad's text, not only from a focused control", async () => {
    useBlobStore.getState().setFileManagerOpen(true);
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    await user.click(screen.getByText(/Choose the prompts yourself/));
    expect(document.activeElement).toBe(screen.getByRole("dialog", { name: "File wiring pad" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "File wiring pad" })).not.toBeInTheDocument();
    expect(useBlobStore.getState().fileManagerOpen).toBe(true);
  });

  it("keeps the file manager open when Escape closes the pad", async () => {
    useBlobStore.getState().setFileManagerOpen(true);
    render(<BlobManager onUseAsInput={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Wiring prompt.txt" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "File wiring pad" })).not.toBeInTheDocument();
    expect(useBlobStore.getState().fileManagerOpen).toBe(true);
  });
});
