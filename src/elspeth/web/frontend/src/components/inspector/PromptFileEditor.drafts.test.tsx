import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchCompositionState, readPromptFileText, savePromptFile } from "@/api/client";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { usePromptDraftStore } from "@/stores/promptDraftStore";
import type { BlobMetadata, CompositionState } from "@/types/api";
import { PromptFileEditor } from "./PromptFileEditor";
import { OptionRows } from "./OptionRows";

vi.mock("@/api/client", () => ({
  fetchCompositionState: vi.fn(),
  readPromptFileText: vi.fn(),
  savePromptFile: vi.fn(),
}));

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const marker = (blobId: string) => ({ mode: "inline_content", blob_ref: blobId, sha256: SHA_A });

function blob(id: string, filename: string): BlobMetadata {
  return {
    id, filename, session_id: "session-1", mime_type: "text/plain", status: "ready", content_hash: SHA_A, size_bytes: 10,
    created_at: "2026-01-01T00:00:00Z", created_by: "user", source_description: null, creation_modality: "verbatim",
    created_from_message_id: null, creating_model_identifier: null, creating_model_version: null, creating_provider: null,
    creating_composer_skill_hash: null, creating_arguments_hash: null,
  };
}

function graph(scoreFile = "file-a"): CompositionState {
  return {
    id: "state-1", session_id: "session-1", version: 1, sources: {}, edges: [], outputs: [],
    nodes: [
      { id: "score", node_type: "transform", plugin: "llm", input: "rows", on_success: "out", on_error: null, options: { prompt_template: marker(scoreFile) } },
      { id: "other", node_type: "transform", plugin: "llm", input: "rows", on_success: "out", on_error: null, options: { prompt_template: marker("file-c") } },
    ],
    metadata: { name: "drafts", description: "" }, is_valid: true, validation_errors: null, validation_warnings: null,
    validation_suggestions: null, derived_from_state_id: null, created_at: "2026-01-01T00:00:00Z", composer_meta: null, plugin_policy_findings: [],
  } as unknown as CompositionState;
}

/** The step panel as GraphView mounts it: one OptionRows per selected step, keyed by that step. */
function Inspector() {
  const selected = useSessionStore((state) => state.selectedNodeId);
  const state = useSessionStore((state) => state.compositionState);
  const node = state?.nodes.find((candidate) => candidate.id === selected);
  if (!node) return null;
  return <OptionRows key={node.id} options={node.options} ariaLabel={`${node.id} options`} plugin={{ kind: "transform", name: "llm" }}
    slotActions={{ nodeId: node.id, Editor: PromptFileEditor }} />;
}

const editor = () => screen.findByRole("textbox", { name: /Edit prompt file/ });

beforeEach(() => {
  vi.clearAllMocks();
  useBlobStore.getState().reset();
  useBlobStore.setState({ activeSessionId: "session-1", isLoading: false, error: null,
    blobs: [blob("file-a", "score.txt"), blob("file-b", "score-v2.txt"), blob("file-c", "other.txt")] });
  useSessionStore.setState({ activeSessionId: "session-1", isComposing: false, compositionState: graph(), selectedNodeId: "score" });
  useInterpretationEventsStore.setState({ pendingBySession: {} });
  vi.mocked(readPromptFileText).mockImplementation(async (_session, blobId) => ({ text: `text of ${blobId}`, sha256: SHA_A }));
  vi.mocked(savePromptFile).mockResolvedValue({ changed: true, content_sha256: SHA_B });
  vi.mocked(fetchCompositionState).mockResolvedValue(null as unknown as CompositionState);
});

async function typeDraft(value: string) {
  act(() => useBlobStore.getState().requestPromptEdit("score", "prompt_template"));
  const box = await editor();
  await waitFor(() => expect(box).toHaveValue("text of file-a"));
  fireEvent.change(box, { target: { value } });
  return box;
}

describe("unsaved prompt text survives the editor unmounting", () => {
  it.each([
    ["selecting another step and coming back", () => { useSessionStore.getState().selectNode("other"); }, () => { useSessionStore.getState().selectNode("score"); }],
    ["clicking the canvas (deselect) and reselecting", () => { useSessionStore.getState().selectNode(null); }, () => { useSessionStore.getState().selectNode("score"); }],
    ["Show node from the Files table", () => { useSessionStore.getState().selectNode("other"); }, () => { useSessionStore.getState().selectNode("score"); }],
    ["Edit file / Edit prompt from another surface", () => { useSessionStore.getState().selectNode(null); }, () => {
      useSessionStore.getState().selectNode("score");
      useBlobStore.getState().requestPromptEdit("score", "prompt_template");
    }],
  ])("keeps it across %s", async (_route, away, back) => {
    render(<Inspector />);
    await typeDraft("my unsaved text");
    act(away);
    expect(screen.queryByRole("textbox", { name: /Edit prompt file/ })).not.toBeInTheDocument();
    act(back);
    // The slot reopens by itself with the text and says it is unsaved.
    expect(await editor()).toHaveValue("my unsaved text");
    expect(screen.getAllByText("Unsaved changes").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("keeps the text when Close collapses the editor, and tags the prompt row", async () => {
    render(<Inspector />);
    await typeDraft("closed but kept");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("textbox", { name: /Edit prompt file/ })).not.toBeInTheDocument();
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    act(() => useBlobStore.getState().requestPromptEdit("score", "prompt_template"));
    expect(await editor()).toHaveValue("closed but kept");
  });

  it("keeps a draft when a Composer run re-points the slot, says so, and never saves it over the new file silently", async () => {
    render(<Inspector />);
    await typeDraft("written for file A");
    act(() => useSessionStore.setState({ compositionState: { ...graph("file-b"), id: "state-2" } }));
    const box = await editor();
    await waitFor(() => expect(screen.getByText(/This prompt now reads a different file\. Your unsaved text from score\.txt is kept/)).toBeInTheDocument());
    expect(box).toHaveValue("written for file A");
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(savePromptFile).not.toHaveBeenCalled();
    // Only the user's explicit choice puts it on the current file.
    fireEvent.click(screen.getByRole("button", { name: "Put my text on the current file" }));
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(savePromptFile).toHaveBeenCalledWith("session-1", "file-b", "written for file A", SHA_A, "score", "prompt_template"));
    await waitFor(() => expect(usePromptDraftStore.getState().drafts).toEqual({}));
  });

  it("re-pointed onto a file that cannot be edited here, offers Copy instead of a Save that never enables", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<Inspector />);
    await typeDraft("written for file A");
    // file-c also backs "other", so the server would refuse a save to it.
    act(() => useSessionStore.setState({ compositionState: { ...graph("file-c"), id: "state-2" } }));
    const box = await editor();
    const banner = await screen.findByText(/This prompt now reads a different file/);
    expect(banner).toHaveTextContent("This file cannot be edited here, so the text cannot be saved to it. Copy it to keep it, or discard it.");
    expect(banner).not.toHaveTextContent("Put it on the current file");
    expect(screen.queryByRole("button", { name: "Put my text on the current file" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Copy your unsaved text" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("written for file A"));
    expect(box).toHaveValue("written for file A");
    expect(savePromptFile).not.toHaveBeenCalled();
  });

  it("keeps a draft whose file changed underneath and sends its own base hash, never the new one", async () => {
    render(<Inspector />);
    await typeDraft("based on the old version");
    // The file changes on the server; the editor remounts and loads the new version.
    act(() => useSessionStore.getState().selectNode(null));
    vi.mocked(readPromptFileText).mockResolvedValue({ text: "someone else's text", sha256: SHA_B });
    act(() => useSessionStore.getState().selectNode("score"));
    const box = await editor();
    await waitFor(() => expect(screen.getByText(/This file changed since you started editing\. Your unsaved text is kept/)).toBeInTheDocument());
    expect(box).toHaveValue("based on the old version");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    // Discard is the other way out: it loads the current file.
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    await waitFor(() => expect(box).toHaveValue("someone else's text"));
    expect(usePromptDraftStore.getState().drafts).toEqual({});
  });

  it("clears the draft when Save succeeds", async () => {
    render(<Inspector />);
    await typeDraft("save me");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(savePromptFile).toHaveBeenCalledWith("session-1", "file-a", "save me", SHA_A, "score", "prompt_template"));
    await waitFor(() => expect(usePromptDraftStore.getState().drafts).toEqual({}));
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("keeps the draft when Save is refused", async () => {
    vi.mocked(savePromptFile).mockRejectedValue({ detail: "This file changed; reload it." });
    render(<Inspector />);
    await typeDraft("refused text");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This file changed; reload it.");
    expect(Object.values(usePromptDraftStore.getState().drafts)).toEqual([expect.objectContaining({ text: "refused text" })]);
  });

  it("does not show one session's draft in another session", async () => {
    render(<Inspector />);
    await typeDraft("session one only");
    act(() => {
      useSessionStore.setState({ activeSessionId: "session-2", selectedNodeId: null, compositionState: { ...graph(), session_id: "session-2" } as CompositionState });
      useBlobStore.setState({ activeSessionId: "session-2" });
    });
    act(() => useSessionStore.getState().selectNode("score"));
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
    act(() => useBlobStore.getState().requestPromptEdit("score", "prompt_template"));
    expect(await editor()).toHaveValue("text of file-a");
  });
});

describe("the file changes while its editor is open", () => {
  const STALE_SERVER_TEXT = "This file changed since you opened it. Reload it and retry.";
  const listHash = (sha256: string) => act(() => useBlobStore.setState({
    blobs: useBlobStore.getState().blobs.map((item) => item.id === "file-a" ? { ...item, content_hash: sha256 } : item),
  }));

  it("re-reads the file when the file list reports a new version, keeps the draft and announces the banner", async () => {
    render(<Inspector />);
    const box = await typeDraft("mine");
    vi.mocked(readPromptFileText).mockResolvedValue({ text: "Composer's newer text", sha256: SHA_B });
    listHash(SHA_B);
    await waitFor(() => expect(screen.getByText(/This file changed since you started editing\. Your unsaved text is kept/)).toBeInTheDocument());
    // Announced when it appears (a status), not a static note.
    expect(screen.getByText(/This file changed since you started editing/).closest("[role]")).toHaveAttribute("role", "status");
    expect(box).toHaveValue("mine");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(readPromptFileText).toHaveBeenCalledTimes(2);
  });

  it("shows the new text in a clean open editor", async () => {
    render(<Inspector />);
    act(() => useBlobStore.getState().requestPromptEdit("score", "prompt_template"));
    const box = await editor();
    await waitFor(() => expect(box).toHaveValue("text of file-a"));
    vi.mocked(readPromptFileText).mockResolvedValue({ text: "Composer's newer text", sha256: SHA_B });
    listHash(SHA_B);
    await waitFor(() => expect(box).toHaveValue("Composer's newer text"));
    expect(screen.queryByText(/This file changed/)).not.toBeInTheDocument();
  });

  it("does not re-read after its own save moves the listed hash", async () => {
    render(<Inspector />);
    await typeDraft("save me");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(usePromptDraftStore.getState().drafts).toEqual({}));
    listHash(SHA_B);
    await act(async () => { await Promise.resolve(); });
    expect(readPromptFileText).toHaveBeenCalledTimes(1);
  });

  it("turns a stale save refusal into the banner, with the server's 'reload' wording never shown", async () => {
    vi.mocked(savePromptFile).mockRejectedValue({ error_type: "prompt_file_stale", detail: STALE_SERVER_TEXT });
    render(<Inspector />);
    const box = await typeDraft("mine");
    vi.mocked(readPromptFileText).mockResolvedValue({ text: "Composer's newer text", sha256: SHA_B });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByText(/This file changed since you started editing/)).toBeInTheDocument());
    expect(screen.queryByText(/reload/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(box).toHaveValue("mine");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("keeps the server's wording when a stale refusal is about the step's review, not the file", async () => {
    const reviewText = "This step's prompt is waiting for review. Reload the page so the step's review card appears, approve it, then edit the file.";
    vi.mocked(savePromptFile).mockRejectedValue({ error_type: "prompt_file_stale", detail: reviewText });
    render(<Inspector />);
    await typeDraft("mine");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(reviewText);
    expect(screen.queryByText(/This file changed since you started editing/)).not.toBeInTheDocument();
  });

  it("shows what Save would replace, and moves focus to the text once the user keeps theirs", async () => {
    render(<Inspector />);
    const box = await typeDraft("mine");
    vi.mocked(readPromptFileText).mockResolvedValue({ text: "Composer's newer text", sha256: SHA_B });
    listHash(SHA_B);
    const summary = await screen.findByText("View current file");
    expect(summary.tagName).toBe("SUMMARY");
    expect(summary.closest("details")?.querySelector("pre")).toHaveTextContent("Composer's newer text");
    fireEvent.click(screen.getByRole("button", { name: "Put my text on the current file" }));
    expect(box).toHaveFocus();
    expect(box).toHaveValue("mine");
    expect(screen.queryByText("View current file")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Save" })).toBeEnabled());
  });
});
