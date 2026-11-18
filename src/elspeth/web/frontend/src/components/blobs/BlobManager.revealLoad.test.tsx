// The reveal must survive BlobManager's own mount-time refresh. Every test in
// BlobManager.test.tsx stubs loadBlobs (so isLoading never flips), which hides
// the race this file pins: the real loadBlobs sets isLoading synchronously in
// the same commit that the reveal effect runs.
import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as client from "@/api/client";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { BlobMetadata } from "@/types/api";
import { BlobManager } from "./BlobManager";

vi.mock("@/api/workflow", () => ({ fetchMyQuota: vi.fn().mockReturnValue(new Promise(() => {})) }));
vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  listBlobs: vi.fn(),
}));

const FILE_ID = "f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0";
function promptBlob(): BlobMetadata {
  return {
    id: FILE_ID, session_id: "session-1", filename: "prompt.md", mime_type: "text/plain", size_bytes: 12,
    content_hash: null, created_at: "2026-11-18T00:00:00Z", created_by: "assistant", source_description: null,
    status: "ready", creation_modality: "verbatim", created_from_message_id: null, creating_model_identifier: null,
    creating_model_version: null, creating_provider: null, creating_composer_skill_hash: null, creating_arguments_hash: null,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  useBlobStore.getState().reset();
  useSessionStore.setState({ activeSessionId: "session-1", selectedNodeId: null, compositionState: null });
});

it("focuses the revealed row after the mount-time list refresh, not before it", async () => {
  useBlobStore.setState({ activeSessionId: "session-1", blobs: [promptBlob()], isLoading: false });
  let resolveList!: (blobs: BlobMetadata[]) => void;
  vi.mocked(client.listBlobs).mockReturnValue(new Promise((resolve) => { resolveList = resolve; }));
  const trigger = document.createElement("button");
  document.body.appendChild(trigger);
  trigger.focus();
  useBlobStore.getState().revealBlob("session-1", FILE_ID, "prompt.md", trigger);
  const { container } = render(<BlobManager onUseAsInput={vi.fn()} />);
  // The refresh this mount started is in flight: the request must still be pending.
  expect(useBlobStore.getState().isLoading).toBe(true);
  expect(useBlobStore.getState().revealRequest).not.toBeNull();
  await act(async () => { resolveList([promptBlob()]); });
  await waitFor(() => expect(container.querySelector(`[data-blob-id="${FILE_ID}"]`)).toHaveFocus());
  expect(useBlobStore.getState().revealRequest).toBeNull();
  trigger.remove();
});
