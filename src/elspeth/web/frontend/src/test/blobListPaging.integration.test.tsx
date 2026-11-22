// A session with more files than one page of the list route. The real client
// and the real blob store run against a fetch double that pages like the
// route; only the network is replaced.
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BlobFileName } from "@/components/blobs/BlobFileName";
import { OptionRows } from "@/components/inspector/OptionRows";
import { useBlobStore } from "@/stores/blobStore";
import { usePluginCatalogStore } from "@/stores/pluginCatalogStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { CompositionState } from "@/types/api";
import { blobListRouteDouble, makeSessionBlobs, ROUTE_DEFAULT_LIMIT } from "@/test/blobListRouteDouble";
import { resetStore } from "@/test/store-helpers";

const SESSION = "session-1";
const ABSENT_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff";
const HASH = "a".repeat(64);

function renderBoundPrompt(blobId: string) {
  const options = { prompt_template: { blob_ref: blobId, mode: "inline_content", sha256: HASH } };
  useSessionStore.setState({
    compositionState: { sources: {}, outputs: [], nodes: [{ id: "llm", plugin: "llm", options }] } as unknown as CompositionState,
  });
  return render(<OptionRows options={options} ariaLabel="inspector options"
    plugin={{ kind: "transform", name: "llm" }} slotActions={{ nodeId: "llm" }} />);
}

let fetchSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  resetStore(usePreferencesStore);
  resetStore(usePluginCatalogStore);
  resetStore(useBlobStore);
  useBlobStore.setState({ fileManagerHosts: 1 });
  useSessionStore.setState({ activeSessionId: SESSION, compositionState: null });
  fetchSpy = vi.spyOn(globalThis, "fetch");
});

afterEach(() => {
  fetchSpy.mockRestore();
  useBlobStore.setState({ fileManagerHosts: 0 });
});

describe("a session with more files than one list page", () => {
  const blobs = makeSessionBlobs(SESSION, ROUTE_DEFAULT_LIMIT + 1);
  const oldest = blobs[0];

  it("loads every file, so the file manager list drops none", async () => {
    fetchSpy.mockImplementation(blobListRouteDouble(SESSION, blobs).fetch);

    await useBlobStore.getState().loadBlobs(SESSION);

    const state = useBlobStore.getState();
    expect(state.error).toBeNull();
    expect(state.blobs).toHaveLength(blobs.length);
    expect(state.blobs.map((blob) => blob.id)).toContain(oldest.id);
    // Newest first, as the route orders them.
    expect(state.blobs[0].id).toBe(blobs[blobs.length - 1].id);
  });

  it("lets the oldest file be edited and shown when a prompt is bound to it", async () => {
    fetchSpy.mockImplementation(blobListRouteDouble(SESSION, blobs).fetch);
    await useBlobStore.getState().loadBlobs(SESSION);

    renderBoundPrompt(oldest.id);

    const edit = screen.getByRole("button", { name: `Edit ${oldest.filename} (User prompt)` });
    expect(edit).toHaveAttribute("aria-disabled", "false");
    const show = screen.getByRole("button", { name: `Show in files: ${oldest.filename} (User prompt)` });
    expect(show).toHaveAttribute("aria-disabled", "false");
    expect(screen.queryByText("This file is not in this session.")).not.toBeInTheDocument();
    expect(screen.queryByText(/File not in this session/)).not.toBeInTheDocument();
  });

  it("names the oldest file wherever a file reference is rendered", async () => {
    fetchSpy.mockImplementation(blobListRouteDouble(SESSION, blobs).fetch);
    await useBlobStore.getState().loadBlobs(SESSION);

    render(<BlobFileName blobId={oldest.id} />);

    expect(screen.getByText(`File: ${oldest.filename}`)).toBeInTheDocument();
  });

  it("still reads a file that is not in the session as absent", async () => {
    fetchSpy.mockImplementation(blobListRouteDouble(SESSION, blobs).fetch);
    await useBlobStore.getState().loadBlobs(SESSION);
    // Control: the list really did load past the first page, so "absent"
    // below is a true negative and not the truncation this file guards.
    expect(useBlobStore.getState().blobs).toHaveLength(blobs.length);

    renderBoundPrompt(ABSENT_ID);
    render(<BlobFileName blobId={ABSENT_ID} />);

    const edit = screen.getByRole("button", { name: /^Edit .*\(User prompt\)$/ });
    expect(edit).toHaveAttribute("aria-disabled", "true");
    expect(edit).toHaveAccessibleDescription("This file is not in this session.");
    const show = screen.getByRole("button", { name: /^Show in files: .*\(User prompt\)$/ });
    expect(show).toHaveAttribute("aria-disabled", "true");
    expect(screen.getAllByText("File not in this session").length).toBeGreaterThan(0);
  });
});

describe("a session with more files than the client will list", () => {
  it("shows the newest files and says older ones are not listed", async () => {
    const ceiling = 10_000;
    const blobs = makeSessionBlobs(SESSION, ceiling + 1);
    fetchSpy.mockImplementation(blobListRouteDouble(SESSION, blobs).fetch);

    await useBlobStore.getState().loadBlobs(SESSION);

    const state = useBlobStore.getState();
    expect(state.isLoading).toBe(false);
    expect(state.blobs).toHaveLength(ceiling);
    expect(state.blobs[0].id).toBe(blobs[blobs.length - 1].id);
    expect(state.error).toMatch(/more than 10,000 files/);
    expect(state.error).toMatch(/older files are not shown/);
  });
});
