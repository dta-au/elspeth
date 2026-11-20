import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { promptDraftUnloadGuard, usePromptDraftStore } from "@/stores/promptDraftStore";
import type { BlobMetadata, CompositionState } from "@/types/api";
import { UnhomedPromptDrafts } from "./UnhomedPromptDrafts";

const SHA = "a".repeat(64);
const marker = (blobId: string) => ({ mode: "inline_content", blob_ref: blobId, sha256: SHA });

function graph(nodes: Array<{ id: string; options: Record<string, unknown> }>): CompositionState {
  return {
    id: "state-1", session_id: "session-1", version: 1, sources: {}, edges: [], outputs: [],
    nodes: nodes.map((node) => ({ node_type: "transform", plugin: "llm", input: "rows", on_success: "out", on_error: null, ...node })),
    metadata: { name: "g", description: "" }, is_valid: true, validation_errors: null, validation_warnings: null,
    validation_suggestions: null, derived_from_state_id: null, created_at: "2026-01-01T00:00:00Z", composer_meta: null, plugin_policy_findings: [],
  } as unknown as CompositionState;
}

function draftOn(nodeId: string, fieldPath: string, text: string, sessionId = "session-1") {
  usePromptDraftStore.getState().edit({ sessionId, nodeId, fieldPath }, { blobId: "file-a", baseSha256: SHA, baseText: "base" }, text);
}

function unload(): boolean {
  const event = new Event("beforeunload", { cancelable: true }) as BeforeUnloadEvent;
  promptDraftUnloadGuard(event);
  return event.defaultPrevented;
}

beforeEach(() => {
  usePromptDraftStore.getState().reset();
  useBlobStore.setState({ activeSessionId: "session-1", isLoading: false, error: null,
    blobs: [{ id: "file-a", filename: "score.txt", content_hash: SHA } as BlobMetadata] });
  useSessionStore.setState({ activeSessionId: "session-1", compositionStateLoaded: true,
    compositionState: graph([{ id: "score", options: { prompt_template: marker("file-a"), queries: [{ name: "C1", template: marker("file-a") }] } }]) });
});

describe("unsaved prompt text whose slot is gone", () => {
  it("lists nothing while every draft still has its prompt file", () => {
    draftOn("score", "prompt_template", "mine");
    render(<UnhomedPromptDrafts />);
    expect(screen.queryByText(/no home/)).not.toBeInTheDocument();
  });

  it("lists a draft whose prompt became inline text, with Copy and Discard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    draftOn("score", "queries.0.template", "my query text");
    render(<UnhomedPromptDrafts />);
    act(() => useSessionStore.setState({
      compositionState: graph([{ id: "score", options: { prompt_template: marker("file-a"), queries: [{ name: "C1", template: "now inline" }] } }]),
    }));
    // A named region; its banner is a status, so the arrival is announced.
    const notice = screen.getByRole("region", { name: "Unsaved prompt text with no home (1)" });
    expect(notice.querySelector(".alert-banner")).toHaveAttribute("role", "status");
    expect(notice.querySelector(".alert-banner")).toHaveTextContent(/^Unsaved prompt text with no home \(1\)/);
    const item = within(notice).getByRole("listitem");
    expect(item).toHaveTextContent("score, User prompt, query C1");
    expect(item).toHaveTextContent("Now inline text, not a file · written for score.txt");
    fireEvent.click(within(item).getByRole("button", { name: "Copy unsaved text for score, User prompt, query C1" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("my query text"));
    // Copying keeps it; the leave warning still holds until it is discarded.
    expect(unload()).toBe(true);
    fireEvent.click(within(item).getByRole("button", { name: "Discard unsaved text for score, User prompt, query C1" }));
    expect(screen.queryByText(/no home/)).not.toBeInTheDocument();
    expect(usePromptDraftStore.getState().drafts).toEqual({});
    expect(unload()).toBe(false);
  });

  it("lists a draft whose step was removed, and counts every one", () => {
    draftOn("score", "prompt_template", "first");
    draftOn("gone", "system_prompt", "second");
    render(<UnhomedPromptDrafts />);
    act(() => useSessionStore.setState({ compositionState: graph([]) }));
    expect(screen.getByText("Unsaved prompt text with no home (2)")).toBeInTheDocument();
    expect(screen.getAllByText(/^Step removed/)).toHaveLength(2);
    expect(screen.getByText("gone, System prompt")).toBeInTheDocument();
  });

  it("judges only against this session's loaded graph", () => {
    draftOn("gone", "prompt_template", "other session", "session-2");
    draftOn("gone", "prompt_template", "this session");
    useSessionStore.setState({ compositionStateLoaded: false });
    const { rerender } = render(<UnhomedPromptDrafts />);
    expect(screen.queryByText(/no home/)).not.toBeInTheDocument();
    act(() => useSessionStore.setState({ compositionStateLoaded: true }));
    rerender(<UnhomedPromptDrafts />);
    expect(screen.getByText("Unsaved prompt text with no home (1)")).toBeInTheDocument();
  });
});
