import { act, cleanup, render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/client";
import { BlobListTruncatedError } from "@/api/blobListing";
import type { BlobMetadata, CompositionState } from "@/types";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { GraphFiles } from "./GraphFiles";

const marker = (id: string) => ({ blob_ref: id, mode: "inline_content", sha256: "a".repeat(64) });
const state: CompositionState = {
  id: "state-1", ...compositionStateAuthorityFields, version: 1, metadata: { name: "Mapping test", description: null }, edges: [],
  sources: {
    main: { plugin: "csv", options: { blob_ref: "input", mode: "bind_source" }, on_success: "rows" },
    support: { plugin: "blob_rows", options: { blobs: [{ blob_id: "shared" }] }, on_success: "rows" },
  },
  nodes: [
    { id: "score", node_type: "transform", plugin: "llm", input: "rows", on_success: "out", on_error: "discard", options: {
      system_prompt: marker("SHARED"), queries: [{ name: "CS1", template: marker("shared") }, { name: "CS2", template: marker("missing") }],
    } },
    { id: "rationale", node_type: "transform", plugin: "llm", input: "rows", on_success: "out", on_error: "discard", options: {
      queries: { comments: { template: marker("shared") } },
    } },
    { id: "inject", node_type: "transform", plugin: "file_inject", input: "rows", on_success: "out", on_error: "discard", options: {
      files: { criteria: { content: marker("criteria") } },
    } },
    { id: "solo", node_type: "transform", plugin: "llm", input: "rows", on_success: "out", on_error: "discard", options: { prompt_template: marker("solo") } },
  ],
  outputs: [{ name: "out", plugin: "csv", options: { path: "blob:shared" } }],
};

function blob(id: string, filename: string): BlobMetadata {
  return {
    id, filename, session_id: "session-1", mime_type: "text/plain", status: "ready", content_hash: "a".repeat(64),
    size_bytes: 100, created_at: "2026-01-01T00:00:00Z", created_by: "user", source_description: null,
    creation_modality: "verbatim", created_from_message_id: null, creating_model_identifier: null,
    creating_model_version: null, creating_provider: null, creating_composer_skill_hash: null, creating_arguments_hash: null,
  };
}

function filesSummary(count?: number) {
  return (_content: string, element: Element | null) => element?.tagName === "SUMMARY"
    && element.textContent?.trim() === (count === undefined ? "Files" : `Files (${count})`);
}

describe("GraphFiles", () => {
  let loadBlobs: ReturnType<typeof vi.fn<(sessionId: string) => Promise<void>>>;
  beforeEach(() => {
    useBlobStore.getState().reset();
    loadBlobs = vi.fn<(sessionId: string) => Promise<void>>().mockResolvedValue(undefined);
    useBlobStore.setState({ activeSessionId: "session-1", isLoading: false, error: null, loadBlobs,
      blobs: [blob("input", "input.csv"), blob("shared", "shared.txt"), blob("criteria", "criteria.txt"), blob("solo", "solo.txt")] });
    useSessionStore.setState({ activeSessionId: "session-1", compositionState: state, selectedNodeId: null, isComposing: false });
    useInterpretationEventsStore.setState({ pendingBySession: { "session-1": {} } });
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useBlobStore.getState().reset();
    useSessionStore.setState({ activeSessionId: null, compositionState: null, selectedNodeId: null });
  });

  it("only enumerates mappings when opened and recovers when the graph changes", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const options = new Proxy({}, { ownKeys() { throw new Error("Cannot enumerate this option value"); } });
    const broken = { ...state, sources: { main: { ...state.sources.main, options } } };
    const view = render(<GraphFiles state={broken} onShowNode={vi.fn()} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByText(filesSummary()));
    expect(screen.getByRole("alert")).toHaveTextContent("File mappings table encountered an error");
    view.rerender(<GraphFiles state={{ ...state, id: "repaired-state" }} onShowNode={vi.fn()} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("groups each file's fields by component, including sources, multi-prompts and sinks", async () => {
    const before = JSON.stringify(state);
    render(<GraphFiles state={state} onShowNode={vi.fn()} />);
    const summary = screen.getByText(filesSummary());
    expect(summary.closest("details")).not.toHaveAttribute("open");
    expect(loadBlobs).not.toHaveBeenCalled();
    await userEvent.setup().click(summary);
    await waitFor(() => expect(loadBlobs).toHaveBeenCalledWith("session-1"));
    expect(screen.getByText(filesSummary(5))).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(9);
    const score = within(table).getByRole("row", { name: /shared.txt Transform: score/ });
    expect(score).toHaveTextContent("System prompt");
    expect(score).toHaveTextContent("User prompt, query CS1");
    expect(within(table).getByRole("row", { name: /shared.txt Transform: rationale/ })).toHaveTextContent("User prompt, query comments");
    // Copy-register rule: the raw option path is only a title; the visible text is the option's label.
    const inject = within(table).getByRole("row", { name: /criteria.txt Transform: inject/ });
    expect(inject).not.toHaveTextContent("files.criteria.content");
    expect(within(inject).getByTitle("files.criteria.content")).toHaveTextContent(/^Files: criteria\.content$/);
    expect(within(table).getByRole("row", { name: /input.csv Source: main/ })).toBeInTheDocument();
    expect(within(table).getByRole("row", { name: /shared.txt Sink: out/ })).toBeInTheDocument();
    expect(screen.getByText("File not in this session")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "File mappings table" })).toHaveAttribute("tabindex", "0");
    expect(JSON.stringify(state)).toBe(before);
  });

  it("reveals the mapped component and opens the existing file editor for an editable prompt", async () => {
    const showNode = vi.fn((id: string) => useSessionStore.getState().selectNode(id));
    render(<GraphFiles state={state} onShowNode={showNode} />);
    const user = userEvent.setup();
    await user.click(screen.getByText(filesSummary()));
    await user.click(screen.getByRole("button", { name: "Show node main" }));
    expect(showNode).toHaveBeenLastCalledWith("source:main");
    await user.click(screen.getByRole("button", { name: "Edit file for solo" }));
    expect(showNode).toHaveBeenLastCalledWith("solo");
    expect(useSessionStore.getState().selectedNodeId).toBe("solo");
    expect(useBlobStore.getState().editRequest).toMatchObject({ nodeId: "solo", fieldPath: "prompt_template" });
    const dataEdit = screen.getByRole("button", { name: "Edit file for inject" });
    expect(dataEdit).toHaveAttribute("aria-disabled", "true");
    expect(dataEdit).toHaveAttribute("title", "Only prompt files can be edited here.");
    expect(dataEdit).toHaveAccessibleDescription("Only prompt files can be edited here.");
    useBlobStore.setState({ editRequest: null });
    await user.click(dataEdit);
    expect(useBlobStore.getState().editRequest).toBeNull();
  });

  it("follows the prompt editor's rules: a refused save opens read-only, a missing file stays shut", async () => {
    const showNode = vi.fn();
    render(<GraphFiles state={state} onShowNode={showNode} />);
    const user = userEvent.setup();
    await user.click(screen.getByText(filesSummary()));
    const table = screen.getByRole("table");
    // shared.txt is also a source and a sink file: the editor refuses the save but still opens with the reason.
    const shared = within(within(table).getByRole("row", { name: /shared.txt Transform: score/ })).getByRole("button", { name: "Edit file for score" });
    expect(shared).toHaveAttribute("aria-disabled", "false");
    expect(shared).toHaveAccessibleDescription("This file is also used outside a prompt. Ask Composer to give this prompt its own file.");
    await user.click(shared);
    expect(useBlobStore.getState().editRequest).toMatchObject({ nodeId: "score", fieldPath: "system_prompt" });
    // A file not in this session has no prompt text to show.
    useBlobStore.setState({ editRequest: null });
    const missing = within(within(table).getByRole("row", { name: /File not in this session Transform: score/ })).getByRole("button", { name: "Edit file for score" });
    expect(missing).toHaveAttribute("aria-disabled", "true");
    expect(missing).toHaveAccessibleDescription("This file is not in this session.");
    await user.click(missing);
    expect(useBlobStore.getState().editRequest).toBeNull();
  });

  it("keeps a component hidden by the graph view from being shown", async () => {
    const showNode = vi.fn();
    render(<GraphFiles state={state} onShowNode={showNode} visibleNodeIds={new Set(["score", "rationale", "inject", "solo", "source:main", "source:support"])} />);
    const user = userEvent.setup();
    await user.click(screen.getByText(filesSummary()));
    const sink = screen.getByRole("button", { name: "Show node out" });
    expect(sink).toHaveAttribute("aria-disabled", "true");
    expect(sink).toHaveAccessibleDescription("This component is hidden by the current graph view.");
    await user.click(sink);
    expect(showNode).not.toHaveBeenCalled();
  });

  it("blocks editing a graph that is no longer the current one, even with the file list ready", async () => {
    useSessionStore.setState({ compositionState: { ...state, id: "newer-state" } });
    render(<GraphFiles state={state} onShowNode={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByText(filesSummary()));
    const edit = screen.getByRole("button", { name: "Edit file for solo" });
    expect(edit).toHaveAttribute("aria-disabled", "true");
    expect(edit).toHaveAccessibleDescription("Open the current graph to edit its files.");
    useBlobStore.setState({ editRequest: null });
    await user.click(edit);
    expect(useBlobStore.getState().editRequest).toBeNull();
  });

  it("waits for the file list to load before offering Edit file", async () => {
    useBlobStore.setState({ isLoading: true });
    render(<GraphFiles state={state} onShowNode={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByText(filesSummary()));
    const edit = screen.getByRole("button", { name: "Edit file for solo" });
    expect(edit).toHaveAttribute("aria-disabled", "true");
    expect(edit).toHaveAccessibleDescription("Wait for the file list to load.");
    useBlobStore.setState({ editRequest: null });
    await user.click(edit);
    expect(useBlobStore.getState().editRequest).toBeNull();
  });

  it("names each entry of a multi-key option, keeping the raw path as the title", async () => {
    const twoEntries: CompositionState = { ...state, sources: {}, outputs: [], nodes: [
      { id: "inject", node_type: "transform", plugin: "file_inject", input: "rows", on_success: "out", on_error: "discard", options: {
        files: { alpha: { content: marker("criteria") }, beta: { content: marker("criteria") } },
      } },
    ] };
    render(<GraphFiles state={twoEntries} onShowNode={vi.fn()} />);
    await userEvent.setup().click(screen.getByText(filesSummary()));
    const row = within(screen.getByRole("table")).getByRole("row", { name: /criteria.txt Transform: inject/ });
    expect(within(row).getByTitle("files.alpha.content")).toHaveTextContent(/^Files: alpha\.content$/);
    expect(within(row).getByTitle("files.beta.content")).toHaveTextContent(/^Files: beta\.content$/);
  });

  it("leads with the actions, names files without a repeated prefix, and labels a file reference 'File'", async () => {
    render(<GraphFiles state={state} onShowNode={vi.fn()} />);
    await userEvent.setup().click(screen.getByText(filesSummary()));
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Actions", "File", "Used by", "Fields"]);
    expect(table).not.toHaveTextContent("File:");
    expect(table).not.toHaveTextContent("Blob Ref");
    const source = within(table).getByRole("row", { name: /input\.csv Source: main/ });
    expect(within(source).getByTitle("blob_ref")).toHaveTextContent(/^File$/);
  });

  it("offers Retry for a failed list read, re-issuing the same read", async () => {
    useBlobStore.setState({ blobs: [], error: "Failed to load files.", listError: "Failed to load files.", listLoadFailed: true });
    render(<GraphFiles state={state} onShowNode={vi.fn()} />);
    await userEvent.setup().click(screen.getByText(filesSummary()));
    loadBlobs.mockClear();
    await userEvent.setup().click(within(screen.getByRole("alert")).getByRole("button", { name: "Retry" }));
    expect(loadBlobs).toHaveBeenCalledWith(state.session_id);
  });

  const truncatedMessage = new BlobListTruncatedError([]).message;
  it.each([
    ["a failed list read (Retry offered)", "Failed to load files.", "Failed to load files.", true, "File names are unavailable. Use Retry above."],
    ["a truncated list (nothing to retry)", truncatedMessage, truncatedMessage, false, "File names are unavailable."],
    ["an action refusal", "Download failed.", null, false, null],
  ] as const)("gates Edit on the list state after %s", async (_case, error, listError, listLoadFailed, reason) => {
    useBlobStore.setState({ error, listError, listLoadFailed });
    render(<GraphFiles state={state} onShowNode={vi.fn()} />);
    await userEvent.setup().click(screen.getByText(filesSummary()));
    const edit = screen.getByRole("button", { name: "Edit file for solo" });
    if (reason === null) {
      expect(edit).not.toHaveAttribute("title");
      expect(edit).toHaveAttribute("aria-disabled", "false");
    } else {
      expect(edit).toHaveAttribute("title", reason);
      expect(edit).toHaveAttribute("aria-disabled", "true");
    }
    expect(document.body).not.toHaveTextContent(/Reload/i);
  });

  it.each([
    ["upload", "File exceeds the maximum upload size."],
    ["delete", "This file is still attached."],
  ] as const)("keeps file names and Edit available after a refused %s", async (action, message) => {
    vi.spyOn(api, "listBlobs").mockResolvedValueOnce(useBlobStore.getState().blobs);
    useBlobStore.setState({ loadBlobs: useBlobStore.getInitialState().loadBlobs });
    render(<GraphFiles state={state} onShowNode={vi.fn()} />);
    await userEvent.setup().click(screen.getByText(filesSummary()));
    await waitFor(() => expect(screen.getByRole("button", { name: "Edit file for solo" })).toHaveAttribute("aria-disabled", "false"));

    await act(async () => {
      if (action === "upload") {
        const refusal = { status: 413 };
        vi.spyOn(api, "uploadBlob").mockRejectedValueOnce(refusal);
        await expect(useBlobStore.getState().uploadBlob(state.session_id, new File(["prompt"], "new.txt"))).rejects.toBe(refusal);
      } else {
        vi.spyOn(api, "deleteBlob").mockRejectedValueOnce({ status: 409, detail: message });
        await useBlobStore.getState().deleteBlob(state.session_id, "solo");
      }
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(message);
    expect(within(alert).queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    const edit = screen.getByRole("button", { name: "Edit file for solo" });
    expect(edit).toHaveAttribute("aria-disabled", "false");
    expect(edit).not.toHaveAttribute("title");
    expect(screen.getByRole("rowheader", { name: "solo.txt" })).toBeInTheDocument();
    expect(screen.queryByText("File name unavailable")).not.toBeInTheDocument();
  });

  it("keeps Retry and the Edit list blocker after a successful upload following a failed list read", async () => {
    vi.spyOn(api, "listBlobs").mockRejectedValueOnce(new Error("Network error"));
    vi.spyOn(api, "uploadBlob").mockResolvedValueOnce(blob("uploaded", "uploaded.txt"));
    useBlobStore.setState({ blobs: [], loadBlobs: useBlobStore.getInitialState().loadBlobs });
    render(<GraphFiles state={state} onShowNode={vi.fn()} />);
    await userEvent.setup().click(screen.getByText(filesSummary()));
    await screen.findByRole("button", { name: "Retry" });

    await act(async () => {
      await useBlobStore.getState().uploadBlob(state.session_id, new File(["prompt"], "uploaded.txt"));
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Failed to load files.");
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit file for solo" })).toHaveAttribute("title", "File names are unavailable. Use Retry above.");
  });

  it("lets a long file or step name wrap at its separators, keeping the text whole", async () => {
    render(<GraphFiles state={state} onShowNode={vi.fn()} />);
    await userEvent.setup().click(screen.getByText(filesSummary()));
    const row = within(screen.getByRole("table")).getByRole("row", { name: /criteria\.txt Transform: inject/ });
    const name = within(row).getByRole("rowheader");
    expect(name).toHaveTextContent(/^criteria\.txt$/);
    expect(name.querySelectorAll("wbr")).toHaveLength(1);
    expect(row.querySelector(".graph-files-fields")).not.toBeNull();
  });

  it("sorts rows by file name, keeping graph order within a file", async () => {
    render(<GraphFiles state={state} onShowNode={vi.fn()} />);
    await userEvent.setup().click(screen.getByText(filesSummary()));
    const headers = within(screen.getByRole("table")).getAllByRole("rowheader").map((cell) => cell.textContent);
    expect(headers).toEqual(["criteria.txt", "input.csv", "File not in this session", "shared.txt", "shared.txt", "shared.txt", "shared.txt", "solo.txt"]);
    const sharedUsers = within(screen.getByRole("table")).getAllByRole("row").filter((row) => /shared\.txt/.test(row.textContent ?? ""))
      .map((row) => within(row).getByText(/^(Source|Transform|Sink):/).textContent);
    expect(sharedUsers).toEqual(["Source: support", "Transform: score", "Transform: rationale", "Sink: out"]);
  });

  it("filters rows by file name or step name and says how many match", async () => {
    render(<GraphFiles state={state} onShowNode={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByText(filesSummary()));
    const filter = screen.getByRole("searchbox", { name: "Filter by file or step" });
    expect(screen.getByRole("status")).toHaveTextContent("8 rows");
    await user.type(filter, "SHARED");
    await waitFor(() => expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(5));
    expect(screen.getByRole("status")).toHaveTextContent("4 of 8 rows");
    await user.clear(filter);
    await user.type(filter, "rational");
    await waitFor(() => expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(2));
    expect(screen.getByRole("button", { name: "Show node rationale" })).toBeInTheDocument();
    await user.clear(filter);
    await user.type(filter, "no such thing");
    await waitFor(() => expect(screen.getByText("No file or step matches this filter.")).toBeInTheDocument());
  });

  it("does not borrow another session's filenames or edit its files", async () => {
    useSessionStore.setState({ activeSessionId: "other-session" });
    useBlobStore.setState({ activeSessionId: "other-session", blobs: [blob("solo", "other-session-secret.txt")] });
    render(<GraphFiles state={state} onShowNode={vi.fn()} />);
    await userEvent.setup().click(screen.getByText(filesSummary()));
    expect(loadBlobs).not.toHaveBeenCalled();
    expect(screen.queryByText(/other-session-secret/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit file for solo" })).toHaveAttribute("aria-disabled", "true");
  });

  it("distinguishes a file-list failure from a missing file and blocks editing historical graphs", async () => {
    useBlobStore.setState({ blobs: [], error: truncatedMessage, listError: truncatedMessage });
    render(<GraphFiles state={state} onShowNode={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByText(filesSummary()));
    expect(screen.getByRole("alert")).toHaveTextContent(truncatedMessage);
    // The design system's error banner, not a bare paragraph at the pane edge.
    expect(screen.getByRole("alert")).toHaveClass("alert-banner", "graph-files-alert");
    // Retrying a truncated list would reach the same limit.
    expect(within(screen.getByRole("alert")).queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.queryByText("File not in this session")).not.toBeInTheDocument();
    expect(screen.getAllByText("File name unavailable")).toHaveLength(8);
    act(() => useSessionStore.setState({ compositionState: { ...state, id: "new-state" } }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Edit file for solo" })).toHaveAttribute("title", "Open the current graph to edit its files."));
  });
});
