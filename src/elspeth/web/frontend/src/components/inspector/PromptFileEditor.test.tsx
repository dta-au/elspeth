import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { fetchCompositionState, readPromptFileText, savePromptFile } from "@/api/client";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { BlobMetadata } from "@/types/api";
import type { CompositionState } from "@/types/api";
import type { InterpretationEvent } from "@/types/interpretation";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { ConfigValue } from "./ConfigRows";
import { PromptFileEditors } from "./PromptFileEditor";
import { OptionRows } from "./OptionRows";

vi.mock("@/api/client", () => ({
  fetchCompositionState: vi.fn(),
  readPromptFileText: vi.fn(),
  savePromptFile: vi.fn(),
}));

beforeEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
  useSessionStore.setState({ activeSessionId: "session-1", isComposing: false, compositionState: null });
  useBlobStore.getState().reset();
  useInterpretationEventsStore.setState({ pendingBySession: {} });
  useBlobStore.getState().requestPromptEdit("first", "prompt_template");
  vi.mocked(readPromptFileText).mockResolvedValue({ text: "first line\n  second line\n", sha256: "a".repeat(64) });
  vi.mocked(savePromptFile).mockResolvedValue({ changed: true, content_sha256: "b".repeat(64) });
  vi.mocked(fetchCompositionState).mockResolvedValue(null);
});

it("starts collapsed and opens the requested slot with keyboard focus", async () => {
  useBlobStore.setState({ editRequest: null });
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  expect(screen.queryByRole("textbox", { name: /Edit prompt file/ })).not.toBeInTheDocument();
  expect(screen.getByText(/Prompt file .*User prompt/)).toBeInTheDocument();
  act(() => useBlobStore.getState().requestPromptEdit("first", "prompt_template"));
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  expect(editor).toHaveFocus();
  expect(useBlobStore.getState().editRequest).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Close editor" }));
  expect(screen.queryByRole("textbox", { name: /Edit prompt file/ })).not.toBeInTheDocument();
});

it("waits for a hidden graph pane before focusing an Edit prompt request", async () => {
  useBlobStore.setState({ editRequest: null });
  const contents = <PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />;
  const { container, rerender } = render(<div hidden>{contents}</div>);
  act(() => useBlobStore.getState().requestPromptEdit("first", "prompt_template"));
  await waitFor(() => expect(container.querySelector("textarea")).toBeInTheDocument());
  expect(container.querySelector("textarea")).not.toHaveFocus();
  rerender(<div>{contents}</div>);
  await waitFor(() => expect(container.querySelector("textarea")).toHaveFocus());
});

it("blocks Save before a request when a pending review has an inline sibling", async () => {
  const marker = { mode: "inline_content", blob_ref: "blob-1", sha256: "a".repeat(64) };
  const options = { prompt_template: marker, system_prompt: "You are helpful." };
  useSessionStore.setState({ compositionState: { nodes: [{ id: "first", plugin: "llm", options }], sources: {}, outputs: [] } as unknown as CompositionState });
  useBlobStore.setState({ activeSessionId: "session-1", blobs: [{ id: "blob-1", filename: "prompt.md", status: "ready", mime_type: "text/plain" } as BlobMetadata] });
  useInterpretationEventsStore.setState({ pendingBySession: { "session-1": { card: { choice: "pending", kind: "llm_prompt_template", affected_node_id: "first" } as InterpretationEvent } } });
  render(<PromptFileEditors nodeId="first" options={options} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  fireEvent.change(editor, { target: { value: "a different prompt" } });
  const save = screen.getByRole("button", { name: "Save and approve" });
  expect(save).toBeDisabled();
  expect(save).toHaveAccessibleDescription("Approve the review card first, then edit the file.");
  fireEvent.click(save);
  expect(savePromptFile).not.toHaveBeenCalled();
});

it("names the file and human prompt slot in editor labels", async () => {
  useBlobStore.setState({ activeSessionId: "session-1", blobs: [{ id: "blob-1", filename: "prompt.md" } as BlobMetadata] });
  render(<PromptFileEditors nodeId="first" options={{
    system_prompt: { mode: "inline_content", blob_ref: "blob-1" },
    queries: [{ template: { mode: "inline_content", blob_ref: "blob-1" } }],
  }} />);
  act(() => useBlobStore.getState().requestPromptEdit("first", "system_prompt"));
  act(() => useBlobStore.getState().requestPromptEdit("first", "queries.0.template"));
  expect(await screen.findByRole("textbox", { name: "Edit prompt file prompt.md (System prompt)" })).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Edit prompt file prompt.md (User prompt, query #0)" })).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: /prompt_template|queries\.0\.template/ })).not.toBeInTheDocument();
});

it("renders full file text and saves with the observed hash", async () => {
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "new line\n  indented\n" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  await waitFor(() => expect(savePromptFile).toHaveBeenCalledWith("session-1", "blob-1", "new line\n  indented\n", "a".repeat(64), "first", "prompt_template"));
});

it("shows a typed refusal and retains the user's draft", async () => {
  vi.mocked(savePromptFile).mockRejectedValue({ error_type: "prompt_file_stale", detail: "This file changed; reload it." });
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "my draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("This file changed; reload it.");
  expect(editor).toHaveValue("my draft");
});

it("explains why a node with two prompt files needs agent review", async () => {
  vi.mocked(savePromptFile).mockRejectedValue({ error_type: "prompt_file_node_multi_binding" });
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "my draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Re-point it through the agent and approve the review card.");
  expect(editor).toHaveValue("my draft");
});

it.each([
  ["prompt_file_review_pending_inline", "Approve the review card first, then edit the file."],
  ["prompt_file_binding_changed", "This prompt file moved since you opened it. Reload the session."],
])("shows %s guidance", async (errorType, message) => {
  vi.mocked(savePromptFile).mockRejectedValue({ error_type: errorType });
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "my draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(message);
  expect(editor).toHaveValue("my draft");
});

it("disables Save before any request on a node with two prompt files, and says why", async () => {
  render(
    <PromptFileEditors
      nodeId="first"
      options={{
        prompt_template: { mode: "inline_content", blob_ref: "blob-1" },
        system_prompt: { mode: "inline_content", blob_ref: "blob-2" },
      }}
    />,
  );
  act(() => useBlobStore.getState().requestPromptEdit("first", "system_prompt"));
  const editors = await screen.findAllByRole("textbox", { name: /Edit prompt file/ });
  expect(editors).toHaveLength(2);
  await waitFor(() => expect(editors[0]).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editors[0], { target: { value: "my draft" } });
  const saves = screen.getAllByRole("button", { name: "Save and approve" });
  for (const save of saves) {
    expect(save).toBeDisabled();
    expect(save).toHaveAccessibleDescription("This node uses more than one prompt file. Re-point it through the agent and approve the review card.");
  }
  fireEvent.click(saves[0]);
  expect(savePromptFile).not.toHaveBeenCalled();
  expect(editors[0]).toHaveValue("my draft");
});

it("keeps Save enabled for a single file-backed slot beside an inline prompt", async () => {
  render(
    <PromptFileEditors
      nodeId="first"
      options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" }, system_prompt: "You are terse." }}
    />,
  );
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "my draft" } });
  expect(screen.getByRole("button", { name: "Save and approve" })).toBeEnabled();
});

it("reloads the session file list after a successful save so its hash is current", async () => {
  const loadBlobs = vi.spyOn(useBlobStore.getState(), "loadBlobs").mockResolvedValue();
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "new text" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  await waitFor(() => expect(loadBlobs).toHaveBeenCalledWith("session-1"));
});

it("does not show a hash mismatch after save repins the marker and reloads metadata", async () => {
  const oldMarker = { mode: "inline_content", blob_ref: "blob-1", sha256: "a".repeat(64) };
  const newMarker = { ...oldMarker, sha256: "b".repeat(64) };
  const oldState = { nodes: [{ options: { prompt_template: oldMarker } }] } as unknown as CompositionState;
  const newState = { nodes: [{ options: { prompt_template: newMarker } }] } as unknown as CompositionState;
  useSessionStore.setState({ compositionState: oldState });
  useBlobStore.setState({ activeSessionId: "session-1", blobs: [{ id: "blob-1", filename: "prompt.md", content_hash: oldMarker.sha256 } as BlobMetadata] });
  vi.mocked(fetchCompositionState).mockResolvedValue(newState);
  const loadBlobs = vi.spyOn(useBlobStore.getState(), "loadBlobs").mockImplementation(async () => {
    useBlobStore.setState({ blobs: [{ id: "blob-1", filename: "prompt.md", content_hash: newMarker.sha256 } as BlobMetadata] });
  });
  function BoundPrompt() {
    const marker = useSessionStore((state) => state.compositionState?.nodes[0]?.options.prompt_template);
    return <ConfigValue value={marker} />;
  }
  render(<><PromptFileEditors nodeId="first" options={{ prompt_template: oldMarker }} /><BoundPrompt /></>);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "new text" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  await waitFor(() => expect(loadBlobs).toHaveBeenCalledWith("session-1"));
  await waitFor(() => expect(useSessionStore.getState().compositionState).toBe(newState));
  expect(screen.queryByText(/changed since attached/)).not.toBeInTheDocument();
});

it("does not reload the file list when the save is refused", async () => {
  const loadBlobs = vi.spyOn(useBlobStore.getState(), "loadBlobs").mockResolvedValue();
  vi.mocked(savePromptFile).mockRejectedValue({ error_type: "prompt_file_stale", detail: "This file changed; reload it." });
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "new text" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  await screen.findByRole("alert");
  expect(loadBlobs).not.toHaveBeenCalled();
});

it("opens a list-form query's editor from the inspector Edit control, labelled by the query name", async () => {
  const marker = { mode: "inline_content", blob_ref: "blob-1", sha256: "a".repeat(64) };
  const options = { system_prompt: "You are a classifier.", queries: [{ name: "classify", template: marker }] };
  useBlobStore.setState({ editRequest: null, activeSessionId: "session-1", isLoading: false,
    blobs: [{ id: "blob-1", filename: "prompt.md", status: "ready", mime_type: "text/plain", content_hash: null } as BlobMetadata] });
  useSessionStore.setState({ compositionState: { nodes: [{ id: "first", plugin: "llm", options }], sources: {}, outputs: [] } as unknown as CompositionState });
  render(<>
    <OptionRows options={options} ariaLabel="inspector options" plugin={{ kind: "transform", name: "llm" }} slotActions={{ nodeId: "first" }} />
    <PromptFileEditors nodeId="first" options={options} />
  </>);
  const edit = screen.getByRole("button", { name: "Edit prompt.md (User prompt, query classify)" });
  expect(edit).toHaveAttribute("aria-disabled", "false");
  fireEvent.click(edit);
  expect(await screen.findByRole("textbox", { name: "Edit prompt file prompt.md (User prompt, query classify)" })).toHaveFocus();
});

it("does not let a post-save refetch from an earlier activation replace a newer graph", async () => {
  const marker = { mode: "inline_content", blob_ref: "blob-1", sha256: "a".repeat(64) };
  const options = { prompt_template: marker };
  const stateAt = (version: number) => ({ version, nodes: [{ id: "first", plugin: "llm", options }], sources: {}, outputs: [] }) as unknown as CompositionState;
  useSessionStore.setState({ compositionState: stateAt(1) });
  useBlobStore.setState({ activeSessionId: "session-1", isLoading: false,
    blobs: [{ id: "blob-1", filename: "prompt.md", status: "ready", mime_type: "text/plain", content_hash: null } as BlobMetadata] });
  let resolveFetch!: (state: CompositionState) => void;
  vi.mocked(fetchCompositionState).mockReturnValue(new Promise((resolve) => { resolveFetch = resolve; }));
  vi.spyOn(useBlobStore.getState(), "loadBlobs").mockResolvedValue();
  render(<PromptFileEditors nodeId="first" options={options} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "new text" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  await waitFor(() => expect(fetchCompositionState).toHaveBeenCalled());
  // Away and back: each activation advances the session publication generation.
  act(() => useSessionStore.getState().resetForTutorialSession("session-2"));
  act(() => useSessionStore.getState().resetForTutorialSession("session-1"));
  act(() => useSessionStore.setState({ compositionState: stateAt(7) }));
  await act(async () => { resolveFetch(stateAt(2)); });
  expect(useSessionStore.getState().compositionState?.version).toBe(7);
});

it.each([
  ["ordered list form", { query_input_mode: "ordered", queries: [{ name: "classify", template: { mode: "inline_content", blob_ref: "blob-1", sha256: "a".repeat(64) } }] }, "queries.0.template"],
  ["mapping form (control)", { queries: { classify: { template: { mode: "inline_content", blob_ref: "blob-1", sha256: "a".repeat(64) } } } }, "queries.classify.template"],
])("saves a %s query file from the inspector Edit with the server's slot path", async (_form, queryOptions, expectedPath) => {
  const options = { system_prompt: "You are a classifier.", ...queryOptions };
  useBlobStore.setState({ editRequest: null, activeSessionId: "session-1", isLoading: false,
    blobs: [{ id: "blob-1", filename: "prompt.md", status: "ready", mime_type: "text/plain", content_hash: null } as BlobMetadata] });
  useSessionStore.setState({ compositionState: { nodes: [{ id: "first", plugin: "llm", options }], sources: {}, outputs: [] } as unknown as CompositionState });
  vi.spyOn(useBlobStore.getState(), "loadBlobs").mockResolvedValue();
  render(<>
    <OptionRows options={options} ariaLabel="inspector options" plugin={{ kind: "transform", name: "llm" }} slotActions={{ nodeId: "first" }} />
    <PromptFileEditors nodeId="first" options={options} />
  </>);
  fireEvent.click(screen.getByRole("button", { name: "Edit prompt.md (User prompt, query classify)" }));
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file prompt\.md/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "edited" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  await waitFor(() => expect(savePromptFile).toHaveBeenCalledWith("session-1", "blob-1", "edited", "a".repeat(64), "first", expectedPath));
});
