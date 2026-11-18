import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { fetchCompositionState, readPromptFileText, savePromptFile } from "@/api/client";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { PromptFileEditors } from "./PromptFileEditor";

vi.mock("@/api/client", () => ({
  fetchCompositionState: vi.fn(),
  readPromptFileText: vi.fn(),
  savePromptFile: vi.fn(),
}));

beforeEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
  useSessionStore.setState({ activeSessionId: "session-1", isComposing: false });
  vi.mocked(readPromptFileText).mockResolvedValue({ text: "first line\n  second line\n", sha256: "a".repeat(64) });
  vi.mocked(savePromptFile).mockResolvedValue({ changed: true, content_sha256: "b".repeat(64) });
  vi.mocked(fetchCompositionState).mockResolvedValue(null);
});

it("renders full file text and saves with the observed hash", async () => {
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit bound prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "new line\n  indented\n" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  await waitFor(() => expect(savePromptFile).toHaveBeenCalledWith("session-1", "blob-1", "new line\n  indented\n", "a".repeat(64), "first", "prompt_template"));
});

it("shows a typed refusal and retains the user's draft", async () => {
  vi.mocked(savePromptFile).mockRejectedValue({ error_type: "prompt_file_stale", detail: "This file changed; reload it." });
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit bound prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "my draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("This file changed; reload it.");
  expect(editor).toHaveValue("my draft");
});

it("explains why a node with two prompt files needs agent review", async () => {
  vi.mocked(savePromptFile).mockRejectedValue({ error_type: "prompt_file_node_multi_binding" });
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit bound prompt file/ });
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
  const editor = await screen.findByRole("textbox", { name: /Edit bound prompt file/ });
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
  const editors = await screen.findAllByRole("textbox", { name: /Edit bound prompt file/ });
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
  const editor = await screen.findByRole("textbox", { name: /Edit bound prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "my draft" } });
  expect(screen.getByRole("button", { name: "Save and approve" })).toBeEnabled();
});

it("reloads the session file list after a successful save so its hash is current", async () => {
  const loadBlobs = vi.spyOn(useBlobStore.getState(), "loadBlobs").mockResolvedValue();
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit bound prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "new text" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  await waitFor(() => expect(loadBlobs).toHaveBeenCalledWith("session-1"));
});

it("does not reload the file list when the save is refused", async () => {
  const loadBlobs = vi.spyOn(useBlobStore.getState(), "loadBlobs").mockResolvedValue();
  vi.mocked(savePromptFile).mockRejectedValue({ error_type: "prompt_file_stale", detail: "This file changed; reload it." });
  render(<PromptFileEditors nodeId="first" options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit bound prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "new text" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  await screen.findByRole("alert");
  expect(loadBlobs).not.toHaveBeenCalled();
});
