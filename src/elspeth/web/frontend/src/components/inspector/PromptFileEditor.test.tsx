import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { fetchCompositionState, readPromptFileText, savePromptFile } from "@/api/client";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { BlobMetadata } from "@/types/api";
import type { CompositionState } from "@/types/api";
import type { InterpretationEvent } from "@/types/interpretation";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { PromptFileEditor } from "./PromptFileEditor";
import { OptionRows } from "./OptionRows";

// The inspector's wiring (GraphView): the step's option rows, which mount one
// editor per file-backed prompt inside that prompt's own row.
function PromptFileEditors({ nodeId, options }: { nodeId: string; options: Record<string, unknown> }) {
  return <OptionRows options={options} ariaLabel="inspector options" plugin={{ kind: "transform", name: "llm" }} slotActions={{ nodeId, Editor: PromptFileEditor }} />;
}

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
  // Collapsed, the editor renders nothing at all: no placeholder line, no empty section.
  expect(screen.queryByText(/Prompt file /)).not.toBeInTheDocument();
  expect(document.querySelector(".prompt-editor")).toBeNull();
  act(() => useBlobStore.getState().requestPromptEdit("first", "prompt_template"));
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  expect(editor).toHaveFocus();
  expect(useBlobStore.getState().editRequest).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
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
  useBlobStore.setState({ activeSessionId: "session-1", blobs: [{ id: "blob-1", filename: "prompt.md", status: "ready", mime_type: "text/plain", content_hash: null } as BlobMetadata] });
  useInterpretationEventsStore.setState({ pendingBySession: { "session-1": { card: { choice: "pending", kind: "llm_prompt_template", affected_node_id: "first" } as InterpretationEvent } } });
  render(<PromptFileEditors nodeId="first" options={options} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  fireEvent.change(editor, { target: { value: "a different prompt" } });
  const save = screen.getByRole("button", { name: "Save" });
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
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(savePromptFile).toHaveBeenCalledWith("session-1", "blob-1", "new line\n  indented\n", "a".repeat(64), "first", "prompt_template"));
});

it("shows a typed refusal and retains the user's draft", async () => {
  vi.mocked(savePromptFile).mockRejectedValue({ error_type: "prompt_file_stale", detail: "This file changed; reload it." });
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "my draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("This file changed; reload it.");
  expect(editor).toHaveValue("my draft");
});

it.each([
  ["prompt_file_review_pending_inline", "Approve the review card first, then edit the file."],
  ["prompt_file_review_pending", "Complete the existing node review before editing its files."],
  ["prompt_file_binding_changed", "This prompt file moved since you opened it. Reload the session."],
])("shows %s guidance", async (errorType, message) => {
  vi.mocked(savePromptFile).mockRejectedValue({ error_type: errorType });
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "my draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(message);
  expect(editor).toHaveValue("my draft");
});

// --- A step that uses more than one prompt file ---

const FILE_A = { mode: "inline_content", blob_ref: "blob-a", sha256: "a".repeat(64) };
const FILE_B = { mode: "inline_content", blob_ref: "blob-b", sha256: "b".repeat(64) };
const TWO_FILE_OPTIONS = { prompt_template: FILE_A, system_prompt: FILE_B };

function seedTwoFileStep(options: Record<string, unknown> = TWO_FILE_OPTIONS) {
  useBlobStore.setState({ editRequest: null, activeSessionId: "session-1", isLoading: false, blobs: [
    { id: "blob-a", filename: "user.md", status: "ready", mime_type: "text/plain", content_hash: null } as BlobMetadata,
    { id: "blob-b", filename: "system.md", status: "ready", mime_type: "text/plain", content_hash: null } as BlobMetadata,
  ] });
  useSessionStore.setState({ compositionState: { nodes: [{ id: "first", plugin: "llm", options }], sources: {}, outputs: [] } as unknown as CompositionState });
  vi.mocked(readPromptFileText).mockImplementation(async (_sessionId: string, blobId: string) => blobId === "blob-a"
    ? { text: "text of file A", sha256: "a".repeat(64) }
    : { text: "text of file B", sha256: "b".repeat(64) });
  vi.spyOn(useBlobStore.getState(), "loadBlobs").mockResolvedValue();
  render(<PromptFileEditors nodeId="first" options={options} />);
}

it.each([
  ["Edit user.md (User prompt)", "Edit prompt file user.md (User prompt)", "text of file A", "blob-a", "a".repeat(64), "prompt_template"],
  ["Edit system.md (System prompt)", "Edit prompt file system.md (System prompt)", "text of file B", "blob-b", "b".repeat(64), "system_prompt"],
])("on a step with two prompt files, %s opens that file and saves it", async (editName, editorName, fileText, blobId, sha256, fieldPath) => {
  seedTwoFileStep();
  const edit = screen.getByRole("button", { name: editName });
  expect(edit).toHaveAttribute("aria-disabled", "false");
  fireEvent.click(edit);
  const editor = await screen.findByRole("textbox", { name: editorName });
  await waitFor(() => expect(editor).toHaveValue(fileText));
  expect(editor).toHaveFocus();
  expect(editor).not.toHaveAttribute("readonly");
  // Only the requested file's editor opened.
  expect(screen.getAllByRole("textbox", { name: /Edit prompt file/ })).toHaveLength(1);
  fireEvent.change(editor, { target: { value: "edited" } });
  const save = screen.getByRole("button", { name: "Save" });
  expect(save).toBeEnabled();
  fireEvent.click(save);
  await waitFor(() => expect(savePromptFile).toHaveBeenCalledWith("session-1", blobId, "edited", sha256, "first", fieldPath));
});

it.each([
  ["two query files (list form)", { system_prompt: "You classify.", queries: [{ name: "one", template: FILE_A }, { name: "two", template: FILE_B }] },
    "Edit system.md (User prompt, query two)", "blob-b", "b".repeat(64), "queries.1.template"],
  ["two query files (mapping form)", { system_prompt: "You classify.", queries: { one: { template: FILE_A }, two: { template: FILE_B } } },
    "Edit system.md (User prompt, query two)", "blob-b", "b".repeat(64), "queries.two.template"],
])("on a step with %s, Edit opens the query's own file and saves it", async (_form, options, editName, blobId, sha256, fieldPath) => {
  seedTwoFileStep(options);
  fireEvent.click(screen.getByRole("button", { name: editName }));
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file system\.md/ });
  await waitFor(() => expect(editor).toHaveValue("text of file B"));
  fireEvent.change(editor, { target: { value: "edited" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(savePromptFile).toHaveBeenCalledWith("session-1", blobId, "edited", sha256, "first", fieldPath));
});

it("scrolls to and focuses an editor that is already open when Edit is requested again", async () => {
  const scroll = vi.fn();
  const original = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = scroll;
  try {
    seedTwoFileStep();
    const edit = screen.getByRole("button", { name: "Edit user.md (User prompt)" });
    fireEvent.click(edit);
    const editor = await screen.findByRole("textbox", { name: "Edit prompt file user.md (User prompt)" });
    await waitFor(() => expect(editor).toHaveFocus());
    const scrollsAfterOpen = scroll.mock.calls.length;
    expect(scrollsAfterOpen).toBeGreaterThan(0);
    // Focus leaves the open editor; the inspector row's Edit is clicked again.
    act(() => edit.focus());
    expect(editor).not.toHaveFocus();
    fireEvent.click(edit);
    await waitFor(() => expect(editor).toHaveFocus());
    expect(scroll.mock.calls.length).toBe(scrollsAfterOpen + 1);
    // The file manager's "Edit prompt" sends the same request (BlobManager.test.tsx pins that).
    act(() => edit.focus());
    act(() => useBlobStore.getState().requestPromptEdit("first", "prompt_template"));
    await waitFor(() => expect(editor).toHaveFocus());
    expect(scroll.mock.calls.length).toBe(scrollsAfterOpen + 2);
    expect(useBlobStore.getState().editRequest).toBeNull();
  } finally {
    Element.prototype.scrollIntoView = original;
  }
});

it("does not pull focus back into the editor after a repeated Edit request has been served", async () => {
  seedTwoFileStep();
  const edit = screen.getByRole("button", { name: "Edit user.md (User prompt)" });
  fireEvent.click(edit);
  const editor = await screen.findByRole("textbox", { name: "Edit prompt file user.md (User prompt)" });
  await waitFor(() => expect(editor).toHaveFocus());
  act(() => edit.focus());
  fireEvent.click(edit);
  await waitFor(() => expect(editor).toHaveFocus());
  // The user moves to Discard changes; a later text change must not steal focus back.
  const cancel = screen.getByRole("button", { name: "Discard changes" });
  act(() => cancel.focus());
  fireEvent.change(editor, { target: { value: "a draft" } });
  expect(cancel).toHaveFocus();
  fireEvent.click(cancel);
  expect(editor).toHaveValue("text of file A");
  expect(cancel).toHaveFocus();
});

it("saves the same file a second time with the hash the first save returned", async () => {
  vi.mocked(savePromptFile).mockResolvedValueOnce({ changed: true, content_sha256: "c".repeat(64) });
  const repinned = { nodes: [{ id: "first", plugin: "llm", options: { ...TWO_FILE_OPTIONS, prompt_template: { ...FILE_A, sha256: "c".repeat(64) } } }], sources: {}, outputs: [] } as unknown as CompositionState;
  vi.mocked(fetchCompositionState).mockResolvedValue(repinned);
  seedTwoFileStep();
  fireEvent.click(screen.getByRole("button", { name: "Edit user.md (User prompt)" }));
  const editor = await screen.findByRole("textbox", { name: "Edit prompt file user.md (User prompt)" });
  await waitFor(() => expect(editor).toHaveValue("text of file A"));
  fireEvent.change(editor, { target: { value: "first edit" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(useSessionStore.getState().compositionState).toBe(repinned));
  expect(savePromptFile).toHaveBeenLastCalledWith("session-1", "blob-a", "first edit", "a".repeat(64), "first", "prompt_template");
  // The refreshed graph is in the store; the editor is still open and still saveable.
  fireEvent.change(editor, { target: { value: "second edit" } });
  const save = screen.getByRole("button", { name: "Save" });
  await waitFor(() => expect(save).toBeEnabled());
  fireEvent.click(save);
  await waitFor(() => expect(savePromptFile).toHaveBeenLastCalledWith("session-1", "blob-a", "second edit", "c".repeat(64), "first", "prompt_template"));
});

it("opens read-only with Save disabled and the reason while the step's card covers its other prompt file", async () => {
  useInterpretationEventsStore.setState({ pendingBySession: { "session-1": { card: { choice: "pending", kind: "llm_prompt_template", affected_node_id: "first" } as InterpretationEvent } } });
  seedTwoFileStep();
  const reason = "Complete the existing node review before editing its files.";
  const edit = screen.getByRole("button", { name: "Edit user.md (User prompt)" });
  expect(edit).toHaveAttribute("aria-disabled", "false");
  expect(edit).toHaveAccessibleDescription(reason);
  fireEvent.click(edit);
  const editor = await screen.findByRole("textbox", { name: "Edit prompt file user.md (User prompt)" });
  await waitFor(() => expect(editor).toHaveValue("text of file A"));
  expect(editor).toHaveAttribute("readonly");
  expect(editor).toHaveAccessibleDescription(reason);
  const save = screen.getByRole("button", { name: "Save" });
  expect(save).toBeDisabled();
  expect(save).toHaveAccessibleDescription(reason);
  fireEvent.click(save);
  expect(savePromptFile).not.toHaveBeenCalled();
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
  expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
});

it("reloads the session file list after a successful save so its hash is current", async () => {
  const loadBlobs = vi.spyOn(useBlobStore.getState(), "loadBlobs").mockResolvedValue();
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "new text" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
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
  // As the inspector does: the row and its editor read the step's options
  // from the session's graph, so the row shows the marker the save repinned.
  function Inspector() {
    const options = useSessionStore((state) => state.compositionState?.nodes[0]?.options);
    return options ? <PromptFileEditors nodeId="first" options={options} /> : null;
  }
  render(<Inspector />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "new text" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
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
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByRole("alert");
  expect(loadBlobs).not.toHaveBeenCalled();
});

it("opens a list-form query's editor from the inspector Edit control, labelled by the query name", async () => {
  const marker = { mode: "inline_content", blob_ref: "blob-1", sha256: "a".repeat(64) };
  const options = { system_prompt: "You are a classifier.", queries: [{ name: "classify", template: marker }] };
  useBlobStore.setState({ editRequest: null, activeSessionId: "session-1", isLoading: false,
    blobs: [{ id: "blob-1", filename: "prompt.md", status: "ready", mime_type: "text/plain", content_hash: null } as BlobMetadata] });
  useSessionStore.setState({ compositionState: { nodes: [{ id: "first", plugin: "llm", options }], sources: {}, outputs: [] } as unknown as CompositionState });
  render(<PromptFileEditors nodeId="first" options={options} />);
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
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
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
  render(<PromptFileEditors nodeId="first" options={options} />);
  fireEvent.click(screen.getByRole("button", { name: "Edit prompt.md (User prompt, query classify)" }));
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file prompt\.md/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "edited" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(savePromptFile).toHaveBeenCalledWith("session-1", "blob-1", "edited", "a".repeat(64), "first", expectedPath));
});

// --- What the editor says and how its controls are presented ---

const ONE_FILE = { prompt_template: { mode: "inline_content", blob_ref: "blob-1" } };

async function openOneFileEditor(): Promise<HTMLElement> {
  render(<PromptFileEditors nodeId="first" options={ONE_FILE} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  return editor;
}

it("confirms a save politely, not through the error channel, and clears it on the next keystroke", async () => {
  const editor = await openOneFileEditor();
  fireEvent.change(editor, { target: { value: "new text" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  const saved = await screen.findByRole("status");
  expect(saved).toHaveTextContent("Saved. This step now uses the new text.");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  fireEvent.change(editor, { target: { value: "newer text" } });
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

it("says saved-but-not-refreshed without asking for a reload, and Retry refetches the graph only", async () => {
  // A reload would discard other unsaved prompt drafts in the session, so the
  // fallback never asks for one; Retry repeats the graph read, not the save.
  vi.mocked(fetchCompositionState).mockRejectedValueOnce(new Error("offline"));
  const editor = await openOneFileEditor();
  fireEvent.change(editor, { target: { value: "new text" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(
    "Saved. The prompt status will update when the pipeline next refreshes.",
  ));
  expect(screen.getByRole("status")).not.toHaveTextContent(/reload/i);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(savePromptFile).toHaveBeenCalledTimes(1);
  const fetchesBefore = vi.mocked(fetchCompositionState).mock.calls.length;

  fireEvent.click(screen.getByRole("button", { name: "Retry" }));

  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(
    "Saved. This step now uses the new text.",
  ));
  expect(vi.mocked(fetchCompositionState).mock.calls.length).toBe(fetchesBefore + 1);
  expect(savePromptFile).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
});

it("keeps a refused save as an alert and keeps it while the user goes on typing", async () => {
  vi.mocked(savePromptFile).mockRejectedValue({ error_type: "prompt_file_stale", detail: "This file changed; reload it." });
  const editor = await openOneFileEditor();
  fireEvent.change(editor, { target: { value: "my draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("This file changed; reload it.");
  fireEvent.change(editor, { target: { value: "my draft, continued" } });
  expect(screen.getByRole("alert")).toHaveTextContent("This file changed; reload it.");
  expect(editor).toHaveValue("my draft, continued");
});

it("reports a file that cannot be loaded as an alert", async () => {
  vi.mocked(readPromptFileText).mockRejectedValue(new Error("Could not read the file."));
  render(<PromptFileEditors nodeId="first" options={ONE_FILE} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not read the file.");
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
});

it("uses the design system's input and button classes, with the three actions in one row", async () => {
  const editor = await openOneFileEditor();
  expect(editor).toHaveClass("textarea", "input-mono", "prompt-editor-text");
  expect(editor).not.toHaveAttribute("style");
  const actions = editor.closest("section")?.querySelector(".prompt-editor-actions");
  expect(actions).not.toBeNull();
  expect(Array.from(actions?.querySelectorAll("button") ?? []).map((button) => button.textContent)).toEqual(["Save", "Discard changes", "Close"]);
  expect(screen.getByRole("button", { name: "Save" })).toHaveClass("btn-compact", "btn-primary");
  expect(screen.getByRole("button", { name: "Discard changes" })).toHaveClass("btn-compact", "btn-ghost");
  expect(screen.getByRole("button", { name: "Close" })).toHaveClass("btn-compact", "btn-ghost");
});

it("says why Save is disabled when the server would not refuse", async () => {
  let deliver: (value: { text: string; sha256: string }) => void = () => undefined;
  vi.mocked(readPromptFileText).mockReturnValue(new Promise((resolve) => { deliver = resolve; }));
  render(<PromptFileEditors nodeId="first" options={ONE_FILE} />);
  const editor = await screen.findByRole("textbox", { name: /Edit prompt file/ });
  const save = screen.getByRole("button", { name: "Save" });
  // Loading: an empty disabled box is not an empty prompt file.
  expect(editor).toHaveAttribute("aria-busy", "true");
  expect(editor).toHaveAttribute("placeholder", "Loading…");
  expect(save).toBeDisabled();
  expect(save).toHaveAccessibleDescription("Loading the file…");
  await act(async () => { deliver({ text: "loaded text", sha256: "a".repeat(64) }); await Promise.resolve(); });
  expect(editor).not.toHaveAttribute("aria-busy");
  // Nothing changed yet.
  expect(save).toBeDisabled();
  expect(save).toHaveAccessibleDescription("No changes to save.");
  expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  // Changed, but the assistant is replying.
  fireEvent.change(editor, { target: { value: "edited" } });
  expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
  act(() => useSessionStore.setState({ isComposing: true }));
  expect(save).toBeDisabled();
  expect(save).toHaveAccessibleDescription("Wait for Composer to finish, then save.");
  // Changed and free to save: no hint, nothing describing the button.
  act(() => useSessionStore.setState({ isComposing: false }));
  expect(save).toBeEnabled();
  expect(save).not.toHaveAccessibleDescription();
});

it("shows a read-only editor as read-only, with the reason above the text and Save still present and disabled", async () => {
  useInterpretationEventsStore.setState({ pendingBySession: { "session-1": { card: { choice: "pending", kind: "llm_prompt_template", affected_node_id: "first" } as InterpretationEvent } } });
  seedTwoFileStep();
  fireEvent.click(screen.getByRole("button", { name: "Edit user.md (User prompt)" }));
  const editor = await screen.findByRole("textbox", { name: "Edit prompt file user.md (User prompt)" });
  await waitFor(() => expect(editor).toHaveValue("text of file A"));
  const reason = screen.getByRole("note");
  expect(reason).toHaveTextContent("Complete the existing node review before editing its files.");
  // The reason comes before the text in reading order, and is not an alert: it is there on open.
  expect(reason.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByText("Read only")).toBeInTheDocument();
  expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  expect(editor).toHaveAttribute("readonly");
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Close" })).toBeEnabled();
});

it("returns focus to the row's Edit button when the editor is closed", async () => {
  seedTwoFileStep();
  const edit = screen.getByRole("button", { name: "Edit user.md (User prompt)" });
  fireEvent.click(edit);
  const editor = await screen.findByRole("textbox", { name: "Edit prompt file user.md (User prompt)" });
  await waitFor(() => expect(editor).toHaveFocus());
  const close = screen.getByRole("button", { name: "Close" });
  act(() => close.focus());
  fireEvent.click(close);
  expect(screen.queryByRole("textbox", { name: /Edit prompt file/ })).not.toBeInTheDocument();
  expect(edit).toHaveFocus();
});

// --- Where the editor sits: inside the row of the prompt it edits ---

it("opens each editor inside the row of its own prompt", async () => {
  seedTwoFileStep();
  const editUser = screen.getByRole("button", { name: "Edit user.md (User prompt)" });
  const editSystem = screen.getByRole("button", { name: "Edit system.md (System prompt)" });
  fireEvent.click(editUser);
  const userEditor = await screen.findByRole("textbox", { name: "Edit prompt file user.md (User prompt)" });
  expect(userEditor.closest("dd")).toBe(editUser.closest("dd"));
  expect(editSystem.closest("dd")?.querySelector("textarea")).toBeNull();
  fireEvent.click(editSystem);
  const systemEditor = await screen.findByRole("textbox", { name: "Edit prompt file system.md (System prompt)" });
  expect(systemEditor.closest("dd")).toBe(editSystem.closest("dd"));
});

const STEP_FILE = { mode: "inline_content", blob_ref: "blob-a", sha256: "a".repeat(64) };
const OWN_FILE = { mode: "inline_content", blob_ref: "blob-b", sha256: "b".repeat(64) };
const SHARED_NOTE = /This query uses the step's shared prompt file\. Its editor opens under query alpha\./;

it("puts the one editor for a shared step-level file under the first query that uses it, and the other rows say so", async () => {
  seedTwoFileStep({ prompt_template: STEP_FILE, queries: [{ name: "alpha" }, { name: "beta" }, { name: "gamma", template: OWN_FILE }] });
  const editAlpha = screen.getByRole("button", { name: "Edit user.md (User prompt, query alpha)" });
  const editBeta = screen.getByRole("button", { name: "Edit user.md (User prompt, query beta)" });
  const editGamma = screen.getByRole("button", { name: "Edit system.md (User prompt, query gamma)" });
  // Only the row that does not hold the editor carries the note.
  const note = screen.getByText(SHARED_NOTE);
  expect(note.closest("dd")).toBe(editBeta.closest("dd"));
  expect(screen.getAllByText(SHARED_NOTE)).toHaveLength(1);
  // Edit pressed in beta's row opens the shared editor under alpha's row.
  act(() => editBeta.focus());
  fireEvent.click(editBeta);
  const shared = await screen.findByRole("textbox", { name: "Edit prompt file user.md (User prompt)" });
  expect(shared.closest("dd")).toBe(editAlpha.closest("dd"));
  expect(screen.getAllByRole("textbox")).toHaveLength(1);
  // Closing hands focus back to the row the user came from.
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(editBeta).toHaveFocus();
  // A query with its own file keeps its own editor in its own row.
  fireEvent.click(editGamma);
  const own = await screen.findByRole("textbox", { name: "Edit prompt file system.md (User prompt, query gamma)" });
  expect(own.closest("dd")).toBe(editGamma.closest("dd"));
});

it("does not add the shared-file note when the shared step-level prompt is typed text", () => {
  seedTwoFileStep({ prompt_template: "Classify {{ row.text }}", queries: [{ name: "alpha" }, { name: "beta" }] });
  expect(screen.queryByText(/shared prompt file/)).not.toBeInTheDocument();
});

it("still opens a file-backed slot that no row shows, when it is opened from the file list", async () => {
  // Every query overrides the step-level prompt, so no row shows it; the file
  // list can still ask for its editor.
  seedTwoFileStep({ prompt_template: STEP_FILE, queries: [{ name: "alpha", template: OWN_FILE }] });
  expect(screen.queryByRole("button", { name: /Edit user\.md/ })).not.toBeInTheDocument();
  act(() => useBlobStore.getState().requestPromptEdit("first", "prompt_template"));
  expect(await screen.findByRole("textbox", { name: "Edit prompt file user.md (User prompt)" })).toBeInTheDocument();
});

it("mounts no editor and no shared-file note where the host passes no editor", () => {
  const options = { prompt_template: STEP_FILE, queries: [{ name: "alpha" }, { name: "beta" }] };
  useBlobStore.setState({ editRequest: null, activeSessionId: "session-1", isLoading: false,
    blobs: [{ id: "blob-a", filename: "user.md", status: "ready", mime_type: "text/plain", content_hash: null } as BlobMetadata] });
  render(<OptionRows options={options} ariaLabel="inspector options" plugin={{ kind: "transform", name: "llm" }} slotActions={{ nodeId: "first" }} />);
  act(() => useBlobStore.getState().requestPromptEdit("first", "prompt_template"));
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(screen.queryByText(/shared prompt file/)).not.toBeInTheDocument();
});
