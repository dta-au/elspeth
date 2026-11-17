import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { fetchCompositionState, readPromptFileText, savePromptFile } from "@/api/client";
import { useSessionStore } from "@/stores/sessionStore";
import { PromptFileEditors } from "./PromptFileEditor";

vi.mock("@/api/client", () => ({
  fetchCompositionState: vi.fn(),
  readPromptFileText: vi.fn(),
  savePromptFile: vi.fn(),
}));

beforeEach(() => {
  useSessionStore.setState({ activeSessionId: "session-1", isComposing: false });
  vi.mocked(readPromptFileText).mockResolvedValue({ text: "first line\n  second line\n", sha256: "a".repeat(64) });
  vi.mocked(savePromptFile).mockResolvedValue({ changed: true, content_sha256: "b".repeat(64) });
  vi.mocked(fetchCompositionState).mockResolvedValue(null);
});

it("renders full file text and saves with the observed hash", async () => {
  render(<PromptFileEditors options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit bound prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "new line\n  indented\n" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  await waitFor(() => expect(savePromptFile).toHaveBeenCalledWith("session-1", "blob-1", "new line\n  indented\n", "a".repeat(64)));
});

it("shows a typed refusal and retains the user's draft", async () => {
  vi.mocked(savePromptFile).mockRejectedValue({ error_type: "prompt_file_stale", detail: "This file changed; reload it." });
  render(<PromptFileEditors options={{ prompt_template: { mode: "inline_content", blob_ref: "blob-1" } }} />);
  const editor = await screen.findByRole("textbox", { name: /Edit bound prompt file/ });
  await waitFor(() => expect(editor).toHaveValue("first line\n  second line\n"));
  fireEvent.change(editor, { target: { value: "my draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("This file changed; reload it.");
  expect(editor).toHaveValue("my draft");
});
