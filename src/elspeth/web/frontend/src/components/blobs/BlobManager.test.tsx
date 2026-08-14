import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { BlobManager } from "./BlobManager";
import type { BlobMetadata } from "@/types/api";

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
    isLoading: false,
    error: null,
    loadBlobs: vi.fn().mockResolvedValue(undefined),
  });
}

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
