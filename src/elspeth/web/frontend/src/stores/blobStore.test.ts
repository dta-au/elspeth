import { describe, it, expect, vi, beforeEach } from "vitest";
import { BlobListTruncatedError } from "@/api/blobListing";
import { blobBannerMessages, useBlobStore } from "./blobStore";
import type { BlobMetadata } from "@/types/api";

// Mock the API client
vi.mock("@/api/client", () => ({
  listBlobs: vi.fn(),
  uploadBlob: vi.fn(),
  deleteBlob: vi.fn(),
  downloadBlobContent: vi.fn(),
}));

function makeBlob(overrides: Partial<BlobMetadata> = {}): BlobMetadata {
  return {
    id: "blob-1",
    session_id: "session-1",
    filename: "data.csv",
    mime_type: "text/csv",
    size_bytes: 1024,
    content_hash: "abc123",
    created_at: "2026-04-01T00:00:00Z",
    created_by: "user",
    source_description: null,
    status: "ready",
    // Inline-blob provenance (Phase 5a Task 2.5): the fixture defaults
    // match the user-upload write path (modality=verbatim, all five
    // creating_* fields null).
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

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
} {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason?: unknown) => void = () => undefined;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("blobBannerMessages", () => {
  it("de-duplicates identical messages", () => {
    expect(blobBannerMessages("Failed to load files.", "Failed to load files.")).toEqual(["Failed to load files."]);
  });

  it("drops empty strings and null", () => {
    expect(blobBannerMessages("", "Failed to load files.")).toEqual(["Failed to load files."]);
    expect(blobBannerMessages("Download failed.", null)).toEqual(["Download failed."]);
  });

  it("puts the list message first", () => {
    expect(blobBannerMessages("Download failed.", "Failed to load files.")).toEqual([
      "Failed to load files.",
      "Download failed.",
    ]);
  });

  it("returns no messages when both fields are empty", () => {
    expect(blobBannerMessages("", null)).toEqual([]);
  });
});

describe("blobStore", () => {
  beforeEach(() => {
    useBlobStore.getState().reset();
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  it("opens a requested file and consumes only the matching reveal sequence", () => {
    useBlobStore.getState().activateSession("session-1");
    useBlobStore.getState().revealBlob("session-1", "file-1", "prompt.txt");
    const request = useBlobStore.getState().revealRequest;
    expect(request).toMatchObject({ sessionId: "session-1", blobId: "file-1", filename: "prompt.txt" });
    expect(useBlobStore.getState()).toMatchObject({ fileManagerOpen: true, focusedBlobId: "file-1" });
    useBlobStore.getState().consumeRevealRequest((request?.seq ?? 0) + 1);
    expect(useBlobStore.getState().revealRequest).toBe(request);
    useBlobStore.getState().consumeRevealRequest(request?.seq ?? 0);
    expect(useBlobStore.getState().revealRequest).toBeNull();
    expect(useBlobStore.getState().focusedBlobId).toBe("file-1");
  });

  it("clears open, focused, reveal and edit requests on a session switch", () => {
    useBlobStore.getState().activateSession("session-1");
    useBlobStore.getState().revealBlob("session-1", "file-1", "prompt.txt");
    useBlobStore.getState().requestPromptEdit("llm", "prompt_template");
    useBlobStore.getState().activateSession("session-2");
    expect(useBlobStore.getState()).toMatchObject({ fileManagerOpen: false, focusedBlobId: null, revealRequest: null, editRequest: null });
  });

  it("publishes a monotonic activation epoch for exact session fences", () => {
    const initialEpoch = useBlobStore.getState().activationEpoch;

    useBlobStore.getState().activateSession("session-a");
    expect(useBlobStore.getState().activeSessionId).toBe("session-a");
    expect(useBlobStore.getState().activationEpoch).toBe(initialEpoch + 1);

    useBlobStore.getState().activateSession("session-a");
    expect(useBlobStore.getState().activationEpoch).toBe(initialEpoch + 1);

    useBlobStore.getState().activateSession("session-b");
    expect(useBlobStore.getState().activeSessionId).toBe("session-b");
    expect(useBlobStore.getState().activationEpoch).toBe(initialEpoch + 2);
  });

  describe("invalidateBlobForEpoch", () => {
    it("removes only the exact UUID under matching session ownership", () => {
      const rejected = makeBlob({ id: "blob-rejected" });
      const retained = makeBlob({ id: "blob-retained" });
      useBlobStore.getState().activateSession("session-1");
      useBlobStore.setState({ blobs: [rejected, retained] });
      const epoch = useBlobStore.getState().activationEpoch;

      expect(
        useBlobStore
          .getState()
          .invalidateBlobForEpoch("session-1", epoch, rejected.id),
      ).toBe(true);
      expect(useBlobStore.getState().blobs).toEqual([retained]);
    });

    it("prevents an older overlapping list from restoring the rejected UUID", async () => {
      const olderList = deferred<BlobMetadata[]>();
      const rejected = makeBlob({ id: "blob-rejected" });
      const { listBlobs } = await import("@/api/client");
      (listBlobs as ReturnType<typeof vi.fn>).mockReturnValue(olderList.promise);
      useBlobStore.getState().activateSession("session-1");
      useBlobStore.setState({ blobs: [rejected] });
      const epoch = useBlobStore.getState().activationEpoch;
      const load = useBlobStore.getState().loadBlobs("session-1");

      expect(
        useBlobStore
          .getState()
          .invalidateBlobForEpoch("session-1", epoch, rejected.id),
      ).toBe(true);
      olderList.resolve([rejected]);
      await load;

      expect(useBlobStore.getState().blobs).toEqual([]);
      expect(useBlobStore.getState().isLoading).toBe(false);
    });

    it("does not remove a same-UUID row after an A-to-B-to-A activation cycle", () => {
      const fresh = makeBlob({
        id: "blob-same-uuid",
        session_id: "session-a",
      });
      useBlobStore.getState().activateSession("session-a");
      const oldEpoch = useBlobStore.getState().activationEpoch;
      useBlobStore.getState().activateSession("session-b");
      useBlobStore.getState().activateSession("session-a");
      useBlobStore.setState({ blobs: [fresh] });

      expect(
        useBlobStore
          .getState()
          .invalidateBlobForEpoch("session-a", oldEpoch, fresh.id),
      ).toBe(false);
      expect(useBlobStore.getState().blobs).toEqual([fresh]);
    });

    it("allows a current authoritative refresh to restore a ready UUID", async () => {
      const rejected = makeBlob({ id: "blob-rejected" });
      const { listBlobs } = await import("@/api/client");
      (listBlobs as ReturnType<typeof vi.fn>).mockResolvedValue([rejected]);
      useBlobStore.getState().activateSession("session-1");
      useBlobStore.setState({ blobs: [rejected] });
      const epoch = useBlobStore.getState().activationEpoch;

      expect(
        useBlobStore
          .getState()
          .invalidateBlobForEpoch("session-1", epoch, rejected.id),
      ).toBe(true);
      expect(useBlobStore.getState().blobs).toEqual([]);
      await useBlobStore.getState().loadBlobs("session-1");

      expect(useBlobStore.getState().blobs).toEqual([rejected]);
    });
  });

  describe("loadBlobs", () => {
    it("fetches blobs and sets them in state", async () => {
      const blobs = [makeBlob(), makeBlob({ id: "blob-2", filename: "other.json" })];
      const { listBlobs } = await import("@/api/client");
      (listBlobs as ReturnType<typeof vi.fn>).mockResolvedValue(blobs);

      const promise = useBlobStore.getState().loadBlobs("session-1");

      // isLoading should be true while in-flight
      expect(useBlobStore.getState().isLoading).toBe(true);

      await promise;

      const state = useBlobStore.getState();
      expect(state.blobs).toEqual(blobs);
      expect(state.isLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("records a failed list read and clears it when a fresh load starts", async () => {
      const { listBlobs } = await import("@/api/client");
      const freshList = deferred<BlobMetadata[]>();
      vi.mocked(listBlobs)
        .mockRejectedValueOnce(new Error("Network error"))
        .mockReturnValueOnce(freshList.promise);

      await useBlobStore.getState().loadBlobs("session-1");

      expect(useBlobStore.getState()).toMatchObject({
        blobs: [], error: "Failed to load files.", listError: "Failed to load files.",
        listLoadFailed: true, isLoading: false,
      });

      const retry = useBlobStore.getState().loadBlobs("session-1");
      expect(useBlobStore.getState()).toMatchObject({
        error: null, listError: null, listLoadFailed: false, isLoading: true,
      });
      const blobs = [makeBlob()];
      freshList.resolve(blobs);
      await retry;
      expect(useBlobStore.getState()).toMatchObject({
        blobs, error: null, listError: null, listLoadFailed: false, isLoading: false,
      });
    });

    it("keeps the newest files and records truncation without offering a retry", async () => {
      const { listBlobs } = await import("@/api/client");
      const blobs = [makeBlob()];
      const truncation = new BlobListTruncatedError(blobs);
      vi.mocked(listBlobs).mockRejectedValueOnce(truncation);

      await useBlobStore.getState().loadBlobs("session-1");

      expect(useBlobStore.getState()).toMatchObject({
        blobs, error: truncation.message, listError: truncation.message,
        listLoadFailed: false, isLoading: false,
      });
    });

    it("drops stale blob results when an older session load resolves last", async () => {
      const sessionA = deferred<BlobMetadata[]>();
      const sessionB = deferred<BlobMetadata[]>();
      const blobA = makeBlob({
        id: "blob-a",
        session_id: "session-a",
        filename: "a.csv",
      });
      const blobB = makeBlob({
        id: "blob-b",
        session_id: "session-b",
        filename: "b.csv",
      });
      const { listBlobs } = await import("@/api/client");
      (listBlobs as ReturnType<typeof vi.fn>).mockImplementation(
        (sessionId: string) => {
          if (sessionId === "session-a") return sessionA.promise;
          if (sessionId === "session-b") return sessionB.promise;
          throw new Error(`unexpected session ${sessionId}`);
        },
      );

      const loadA = useBlobStore.getState().loadBlobs("session-a");
      const loadB = useBlobStore.getState().loadBlobs("session-b");

      sessionB.resolve([blobB]);
      await loadB;
      expect(useBlobStore.getState().blobs).toEqual([blobB]);
      expect(useBlobStore.getState().isLoading).toBe(false);

      sessionA.resolve([blobA]);
      await loadA;
      expect(useBlobStore.getState().blobs).toEqual([blobB]);
      expect(useBlobStore.getState().error).toBeNull();
    });

    it("does not display the previous session while a new session load is pending", async () => {
      const sessionB = deferred<BlobMetadata[]>();
      const blobA = makeBlob({ id: "blob-a", session_id: "session-a" });
      const blobB = makeBlob({ id: "blob-b", session_id: "session-b" });
      const { listBlobs } = await import("@/api/client");
      (listBlobs as ReturnType<typeof vi.fn>).mockImplementation(
        (sessionId: string) =>
          sessionId === "session-a" ? Promise.resolve([blobA]) : sessionB.promise,
      );

      await useBlobStore.getState().loadBlobs("session-a");
      const loadB = useBlobStore.getState().loadBlobs("session-b");

      expect(useBlobStore.getState().blobs).toEqual([]);
      expect(useBlobStore.getState().isLoading).toBe(true);

      sessionB.resolve([blobB]);
      await loadB;
      expect(useBlobStore.getState().blobs).toEqual([blobB]);
    });
  });

  it.each([
    // This row guards listError invariance. GraphFiles and OptionRows refusal tests cover the visible regression.
    ["a good list", null, false],
    ["a failed list", "Failed to load files.", true],
  ] as const)("preserves %s state through upload, delete, and download refusals", async (_case, listError, listLoadFailed) => {
    const { listBlobs, uploadBlob, deleteBlob, downloadBlobContent } = await import("@/api/client");
    if (listLoadFailed) {
      vi.mocked(listBlobs).mockRejectedValueOnce(new Error("Network error"));
    } else {
      vi.mocked(listBlobs).mockResolvedValueOnce([makeBlob()]);
    }
    const store = useBlobStore.getState();
    const listState = { listError, listLoadFailed };
    await store.loadBlobs("session-1");
    expect(useBlobStore.getState()).toMatchObject(listState);

    const refusal = { status: 413 };
    vi.mocked(uploadBlob).mockRejectedValueOnce(refusal);
    const pendingUpload = store.uploadBlob("session-1", new File(["content"], "data.csv"));
    expect(useBlobStore.getState()).toMatchObject({ ...listState, error: null });
    await expect(pendingUpload).rejects.toBe(refusal);
    expect(useBlobStore.getState()).toMatchObject({
      ...listState, error: "File exceeds the maximum upload size.",
    });

    const detail = "This file is still attached.";
    vi.mocked(deleteBlob).mockRejectedValueOnce({ status: 409, detail });
    await store.deleteBlob("session-1", "blob-1");
    expect(useBlobStore.getState()).toMatchObject({ ...listState, error: detail });

    vi.mocked(downloadBlobContent).mockRejectedValueOnce(new Error("Download refused"));
    await store.downloadBlob("session-1", "blob-1");
    expect(useBlobStore.getState()).toMatchObject({ ...listState, error: "Download failed." });
  });

  describe("list refresh after mutations", () => {
    it.each(["upload", "delete"] as const)("replaces a pending Retry after a successful %s", async (action) => {
      const retryResult = deferred<BlobMetadata[]>();
      const refreshResult = deferred<BlobMetadata[]>();
      const target = makeBlob({ id: "target" });
      const existing = makeBlob({ id: "existing" });
      const { listBlobs, uploadBlob, deleteBlob } = await import("@/api/client");
      vi.mocked(listBlobs)
        .mockResolvedValueOnce([target])
        .mockRejectedValueOnce(new Error("Network error"))
        .mockReturnValueOnce(retryResult.promise)
        .mockReturnValueOnce(refreshResult.promise);
      const store = useBlobStore.getState();
      await store.loadBlobs("session-1");
      await store.loadBlobs("session-1");
      expect(useBlobStore.getState().listLoadFailed).toBe(true);

      const retry = store.loadBlobs("session-1");
      let refreshed: BlobMetadata[];
      if (action === "upload") {
        const uploaded = makeBlob({ id: "uploaded" });
        vi.mocked(uploadBlob).mockResolvedValueOnce(uploaded);
        await store.uploadBlob("session-1", new File(["content"], "new.csv"));
        expect(useBlobStore.getState().blobs).toEqual([uploaded, target]);
        refreshed = [uploaded, existing, target];
      } else {
        vi.mocked(deleteBlob).mockResolvedValueOnce(undefined);
        await store.deleteBlob("session-1", target.id);
        expect(useBlobStore.getState().blobs).toEqual([]);
        refreshed = [existing];
      }

      expect(useBlobStore.getState().isLoading).toBe(true);
      refreshResult.resolve(refreshed);
      await refreshResult.promise;
      expect(useBlobStore.getState()).toMatchObject({
        blobs: refreshed, isLoading: false, listError: null, listLoadFailed: false,
      });

      retryResult.resolve([target]);
      await retry;
      expect(useBlobStore.getState().blobs).toEqual(refreshed);
    });

    it("adds one list request for an upload only when a read is pending", async () => {
      const pendingList = deferred<BlobMetadata[]>();
      const refresh = deferred<BlobMetadata[]>();
      const first = makeBlob({ id: "first" });
      const second = makeBlob({ id: "second" });
      const { listBlobs, uploadBlob } = await import("@/api/client");
      vi.mocked(listBlobs).mockReturnValueOnce(pendingList.promise).mockReturnValueOnce(refresh.promise);
      vi.mocked(uploadBlob).mockResolvedValueOnce(first).mockResolvedValueOnce(second);
      const store = useBlobStore.getState();
      const file = new File(["content"], "new.csv");

      await store.uploadBlob("session-1", file);
      expect(listBlobs).not.toHaveBeenCalled();

      const read = store.loadBlobs("session-1");
      expect(listBlobs).toHaveBeenCalledTimes(1);
      await store.uploadBlob("session-1", file);
      expect(listBlobs).toHaveBeenCalledTimes(2);
      expect(listBlobs).toHaveBeenLastCalledWith("session-1");

      pendingList.resolve([first]);
      await read;
      refresh.resolve([second, first]);
      await refresh.promise;
      expect(listBlobs).toHaveBeenCalledTimes(2);
    });

    it("discards the original read while the replacement read is still pending", async () => {
      const original = deferred<BlobMetadata[]>();
      const replacement = deferred<BlobMetadata[]>();
      const uploaded = makeBlob({ id: "uploaded" });
      const { listBlobs, uploadBlob } = await import("@/api/client");
      vi.mocked(listBlobs).mockReturnValueOnce(original.promise).mockReturnValueOnce(replacement.promise);
      vi.mocked(uploadBlob).mockResolvedValueOnce(uploaded);
      const store = useBlobStore.getState();

      const read = store.loadBlobs("session-1");
      await store.uploadBlob("session-1", new File(["content"], "new.csv"));
      original.resolve([makeBlob({ id: "stale" })]);
      await read;

      expect(useBlobStore.getState()).toMatchObject({ blobs: [uploaded], isLoading: true });
      const refreshed = [uploaded, makeBlob({ id: "refreshed" })];
      replacement.resolve(refreshed);
      await replacement.promise;
      expect(useBlobStore.getState()).toMatchObject({ blobs: refreshed, isLoading: false });
    });

    it("leaves the new session untouched when the previous session's replacement read resolves", async () => {
      const original = deferred<BlobMetadata[]>();
      const replacement = deferred<BlobMetadata[]>();
      const uploaded = makeBlob({ id: "uploaded" });
      const { listBlobs, uploadBlob } = await import("@/api/client");
      vi.mocked(listBlobs).mockReturnValueOnce(original.promise).mockReturnValueOnce(replacement.promise);
      vi.mocked(uploadBlob).mockResolvedValueOnce(uploaded);
      const store = useBlobStore.getState();

      const read = store.loadBlobs("session-1");
      await store.uploadBlob("session-1", new File(["content"], "new.csv"));
      expect(listBlobs).toHaveBeenNthCalledWith(2, "session-1");
      expect(useBlobStore.getState().isLoading).toBe(true);
      store.activateSession("session-2");
      const newSessionState = useBlobStore.getState();
      expect(newSessionState).toMatchObject({ activeSessionId: "session-2", blobs: [], isLoading: false });

      replacement.resolve([uploaded]);
      await replacement.promise;
      expect(useBlobStore.getState()).toBe(newSessionState);

      original.resolve([]);
      await read;
      expect(useBlobStore.getState()).toBe(newSessionState);
    });

    it("keeps a delete refusal while an overlapping upload refreshes the list", async () => {
      const pendingList = deferred<BlobMetadata[]>();
      const refresh = deferred<BlobMetadata[]>();
      const pendingUpload = deferred<BlobMetadata>();
      const uploaded = makeBlob({ id: "uploaded" });
      const detail = "This file is still attached.";
      const { listBlobs, uploadBlob, deleteBlob } = await import("@/api/client");
      vi.mocked(listBlobs).mockReturnValueOnce(pendingList.promise).mockReturnValueOnce(refresh.promise);
      vi.mocked(uploadBlob).mockReturnValueOnce(pendingUpload.promise);
      vi.mocked(deleteBlob).mockRejectedValueOnce({ status: 409, detail });
      const store = useBlobStore.getState();

      const upload = store.uploadBlob("session-1", new File(["content"], "new.csv"));
      const read = store.loadBlobs("session-1");
      // Both starts clear error, so let the refusal land after both have started.
      await store.deleteBlob("session-1", "blob-1");
      expect(useBlobStore.getState().error).toBe(detail);

      pendingUpload.resolve(uploaded);
      await upload;
      expect(useBlobStore.getState()).toMatchObject({ isLoading: true, error: detail });
      pendingList.resolve([]);
      await read;
      const refreshed = [uploaded, makeBlob()];
      refresh.resolve(refreshed);
      await refresh.promise;
      expect(useBlobStore.getState()).toMatchObject({ blobs: refreshed, isLoading: false, error: detail });
    });

    it("replaces the list once per overlapping upload and stops after the final refresh", async () => {
      const pendingList = deferred<BlobMetadata[]>();
      const firstRefresh = deferred<BlobMetadata[]>();
      const finalRefresh = deferred<BlobMetadata[]>();
      const first = makeBlob({ id: "first" });
      const second = makeBlob({ id: "second" });
      const { listBlobs, uploadBlob } = await import("@/api/client");
      vi.mocked(listBlobs)
        .mockReturnValueOnce(pendingList.promise)
        .mockReturnValueOnce(firstRefresh.promise)
        .mockReturnValueOnce(finalRefresh.promise);
      vi.mocked(uploadBlob).mockResolvedValueOnce(first).mockResolvedValueOnce(second);
      const store = useBlobStore.getState();
      const file = new File(["content"], "new.csv");

      const read = store.loadBlobs("session-1");
      await store.uploadBlob("session-1", file);
      expect(useBlobStore.getState().isLoading).toBe(true);
      expect(listBlobs).toHaveBeenCalledTimes(2);
      await store.uploadBlob("session-1", file);
      expect(useBlobStore.getState().isLoading).toBe(true);
      expect(listBlobs).toHaveBeenCalledTimes(3);

      const refreshed = [second, first];
      finalRefresh.resolve(refreshed);
      await finalRefresh.promise;
      firstRefresh.resolve([first]);
      await firstRefresh.promise;
      pendingList.resolve([]);
      await read;

      expect(useBlobStore.getState()).toMatchObject({ blobs: refreshed, isLoading: false });
      expect(listBlobs).toHaveBeenCalledTimes(3);
    });
  });

  describe("uploadBlob", () => {
    it("uploads and prepends blob to existing list", async () => {
      const existing = makeBlob({ id: "blob-old" });
      useBlobStore.setState({ blobs: [existing] });

      const newBlob = makeBlob({ id: "blob-new", filename: "upload.csv" });
      const { uploadBlob } = await import("@/api/client");
      (uploadBlob as ReturnType<typeof vi.fn>).mockResolvedValue(newBlob);

      const file = new File(["content"], "upload.csv", { type: "text/csv" });
      const result = await useBlobStore.getState().uploadBlob("session-1", file);

      expect(result).toEqual(newBlob);
      const state = useBlobStore.getState();
      expect(state.blobs[0]).toEqual(newBlob);
      expect(state.blobs[1]).toEqual(existing);
      expect(state.error).toBeNull();
    });

    it("deduplicates an upload UUID already returned by an overlapping list", async () => {
      const listResult = deferred<BlobMetadata[]>();
      const uploadResult = deferred<BlobMetadata>();
      const uploadedBlob = makeBlob({
        id: "blob-shared",
        filename: "shared.csv",
      });
      const { listBlobs, uploadBlob } = await import("@/api/client");
      (listBlobs as ReturnType<typeof vi.fn>).mockReturnValue(listResult.promise);
      (uploadBlob as ReturnType<typeof vi.fn>).mockReturnValue(uploadResult.promise);

      const load = useBlobStore.getState().loadBlobs("session-1");
      const upload = useBlobStore
        .getState()
        .uploadBlob("session-1", new File(["content"], "shared.csv"));

      listResult.resolve([uploadedBlob]);
      await load;
      expect(useBlobStore.getState().blobs).toEqual([uploadedBlob]);

      uploadResult.resolve(uploadedBlob);
      await upload;

      expect(useBlobStore.getState().blobs).toEqual([uploadedBlob]);
    });

    it("lets a new session load win over an older session upload success", async () => {
      const sessionBList = deferred<BlobMetadata[]>();
      const uploadAResult = deferred<BlobMetadata>();
      const blobA = makeBlob({ id: "blob-a", session_id: "session-a" });
      const uploadedA = makeBlob({
        id: "blob-uploaded-a",
        session_id: "session-a",
      });
      const blobB = makeBlob({ id: "blob-b", session_id: "session-b" });
      const { listBlobs, uploadBlob } = await import("@/api/client");
      (listBlobs as ReturnType<typeof vi.fn>).mockImplementation(
        (sessionId: string) =>
          sessionId === "session-a" ? Promise.resolve([blobA]) : sessionBList.promise,
      );
      (uploadBlob as ReturnType<typeof vi.fn>).mockReturnValue(uploadAResult.promise);

      await useBlobStore.getState().loadBlobs("session-a");
      const uploadA = useBlobStore
        .getState()
        .uploadBlob("session-a", new File(["a"], "a.csv"));
      const loadB = useBlobStore.getState().loadBlobs("session-b");

      uploadAResult.resolve(uploadedA);
      await expect(uploadA).resolves.toEqual(uploadedA);
      sessionBList.resolve([blobB]);
      await loadB;

      const state = useBlobStore.getState();
      expect(state.blobs).toEqual([blobB]);
      expect(state.isLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("does not publish an older session upload failure into the loaded session", async () => {
      const sessionBList = deferred<BlobMetadata[]>();
      const uploadAResult = deferred<BlobMetadata>();
      const blobA = makeBlob({ id: "blob-a", session_id: "session-a" });
      const blobB = makeBlob({ id: "blob-b", session_id: "session-b" });
      const uploadError = { status: 500 };
      const { listBlobs, uploadBlob } = await import("@/api/client");
      (listBlobs as ReturnType<typeof vi.fn>).mockImplementation(
        (sessionId: string) =>
          sessionId === "session-a" ? Promise.resolve([blobA]) : sessionBList.promise,
      );
      (uploadBlob as ReturnType<typeof vi.fn>).mockReturnValue(uploadAResult.promise);

      await useBlobStore.getState().loadBlobs("session-a");
      const uploadA = useBlobStore
        .getState()
        .uploadBlob("session-a", new File(["a"], "a.csv"));
      const loadB = useBlobStore.getState().loadBlobs("session-b");
      sessionBList.resolve([blobB]);
      await loadB;

      uploadAResult.reject(uploadError);
      await expect(uploadA).rejects.toEqual(uploadError);

      const state = useBlobStore.getState();
      expect(state.blobs).toEqual([blobB]);
      expect(state.isLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets quota-exceeded message for 413 error", async () => {
      const { uploadBlob } = await import("@/api/client");
      (uploadBlob as ReturnType<typeof vi.fn>).mockRejectedValue({ status: 413 });

      const file = new File(["x"], "big.csv", { type: "text/csv" });
      await expect(
        useBlobStore.getState().uploadBlob("session-1", file),
      ).rejects.toEqual({ status: 413 });

      expect(useBlobStore.getState().error).toBe(
        "File exceeds the maximum upload size.",
      );
    });

    it("sets unsupported-type message for 415 error", async () => {
      const { uploadBlob } = await import("@/api/client");
      (uploadBlob as ReturnType<typeof vi.fn>).mockRejectedValue({ status: 415 });

      const file = new File(["x"], "bad.exe", { type: "application/octet-stream" });
      await expect(
        useBlobStore.getState().uploadBlob("session-1", file),
      ).rejects.toEqual({ status: 415 });

      expect(useBlobStore.getState().error).toBe(
        "Unsupported file type. Please use CSV, JSON, JSONL, or plain text.",
      );
    });

    it("does not publish an upload error rejected by the request owner fence", async () => {
      const uploadError = { status: 413 };
      const shouldPublishError = vi.fn().mockReturnValue(false);
      const { uploadBlob } = await import("@/api/client");
      (uploadBlob as ReturnType<typeof vi.fn>).mockRejectedValue(uploadError);

      const file = new File(["x"], "stale.csv", { type: "text/csv" });
      await expect(
        useBlobStore.getState().uploadBlob("session-1", file, {
          shouldPublishError,
        }),
      ).rejects.toEqual(uploadError);

      expect(shouldPublishError).toHaveBeenCalledWith(uploadError);
      expect(useBlobStore.getState().error).toBeNull();
    });

    it("publishes a mapped upload error accepted by the request owner fence", async () => {
      const uploadError = { status: 415 };
      const shouldPublishError = vi.fn().mockReturnValue(true);
      const { uploadBlob } = await import("@/api/client");
      (uploadBlob as ReturnType<typeof vi.fn>).mockRejectedValue(uploadError);

      const file = new File(["x"], "current.exe", {
        type: "application/octet-stream",
      });
      await expect(
        useBlobStore.getState().uploadBlob("session-1", file, {
          shouldPublishError,
        }),
      ).rejects.toEqual(uploadError);

      expect(shouldPublishError).toHaveBeenCalledWith(uploadError);
      expect(useBlobStore.getState().error).toBe(
        "Unsupported file type. Please use CSV, JSON, JSONL, or plain text.",
      );
    });
  });

  describe("deleteBlob", () => {
    it("removes blob from state on success", async () => {
      const blob1 = makeBlob({ id: "blob-1" });
      const blob2 = makeBlob({ id: "blob-2" });
      useBlobStore.setState({ blobs: [blob1, blob2] });

      const { deleteBlob } = await import("@/api/client");
      (deleteBlob as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);

      await useBlobStore.getState().deleteBlob("session-1", "blob-1");

      const state = useBlobStore.getState();
      expect(state.blobs).toHaveLength(1);
      expect(state.blobs[0].id).toBe("blob-2");
    });

    it("lets a new session load win over an older session deletion success", async () => {
      const sessionBList = deferred<BlobMetadata[]>();
      const deleteAResult = deferred<void>();
      const blobA = makeBlob({ id: "blob-a", session_id: "session-a" });
      const blobB = makeBlob({ id: "blob-b", session_id: "session-b" });
      const { listBlobs, deleteBlob } = await import("@/api/client");
      (listBlobs as ReturnType<typeof vi.fn>).mockImplementation(
        (sessionId: string) =>
          sessionId === "session-a" ? Promise.resolve([blobA]) : sessionBList.promise,
      );
      (deleteBlob as ReturnType<typeof vi.fn>).mockReturnValue(deleteAResult.promise);

      await useBlobStore.getState().loadBlobs("session-a");
      const deleteA = useBlobStore.getState().deleteBlob("session-a", blobA.id);
      const loadB = useBlobStore.getState().loadBlobs("session-b");

      deleteAResult.resolve(undefined);
      await deleteA;
      sessionBList.resolve([blobB]);
      await loadB;

      const state = useBlobStore.getState();
      expect(state.blobs).toEqual([blobB]);
      expect(state.isLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("does not publish an older session deletion failure into the loaded session", async () => {
      const sessionBList = deferred<BlobMetadata[]>();
      const deleteAResult = deferred<void>();
      const blobA = makeBlob({ id: "blob-a", session_id: "session-a" });
      const blobB = makeBlob({ id: "blob-b", session_id: "session-b" });
      const { listBlobs, deleteBlob } = await import("@/api/client");
      (listBlobs as ReturnType<typeof vi.fn>).mockImplementation(
        (sessionId: string) =>
          sessionId === "session-a" ? Promise.resolve([blobA]) : sessionBList.promise,
      );
      (deleteBlob as ReturnType<typeof vi.fn>).mockReturnValue(deleteAResult.promise);

      await useBlobStore.getState().loadBlobs("session-a");
      const deleteA = useBlobStore.getState().deleteBlob("session-a", blobA.id);
      const loadB = useBlobStore.getState().loadBlobs("session-b");
      sessionBList.resolve([blobB]);
      await loadB;

      deleteAResult.reject({ status: 409 });
      await deleteA;

      const state = useBlobStore.getState();
      expect(state.blobs).toEqual([blobB]);
      expect(state.isLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it.each([
      "Session operation is already active",
      "Blob is referenced by a pending proposal",
      "Blob is linked to an active run",
    ])("preserves the actual deletion blocker: %s", async (detail) => {
      const blob = makeBlob();
      useBlobStore.setState({ blobs: [blob] });
      const { deleteBlob } = await import("@/api/client");
      vi.mocked(deleteBlob).mockRejectedValue({ status: 409, detail });

      await useBlobStore.getState().deleteBlob("session-1", "blob-1");

      expect(useBlobStore.getState().error).toBe(detail);
      expect(useBlobStore.getState().blobs).toEqual([blob]);
    });

    it("does not invent a deletion blocker when the error has no detail", async () => {
      useBlobStore.setState({ blobs: [makeBlob()] });

      const { deleteBlob } = await import("@/api/client");
      (deleteBlob as ReturnType<typeof vi.fn>).mockRejectedValue({ status: 409 });

      await useBlobStore.getState().deleteBlob("session-1", "blob-1");

      expect(useBlobStore.getState().error).toBe(
        "Failed to delete file.",
      );
    });

    it("falls back to a generic delete failure when the refusal detail is empty", async () => {
      const { deleteBlob } = await import("@/api/client");
      vi.mocked(deleteBlob).mockRejectedValueOnce({ status: 409, detail: "" });
      await useBlobStore.getState().deleteBlob("session-1", "blob-1");
      expect(useBlobStore.getState().error).toBe("Failed to delete file.");
    });
  });

  describe("downloadBlob", () => {
    it.each(["session-b", "session-a"] as const)("drops a download failure after switching away from A and ending in %s", async (activeSessionId) => {
      const { downloadBlobContent } = await import("@/api/client");
      const result = deferred<Awaited<ReturnType<typeof downloadBlobContent>>>();
      vi.mocked(downloadBlobContent).mockReturnValueOnce(result.promise);
      const store = useBlobStore.getState();
      store.activateSession("session-a");
      const download = store.downloadBlob("session-a", "blob-1");
      store.activateSession("session-b");
      store.activateSession(activeSessionId);

      result.reject(new Error("Network error"));
      await download;

      expect(useBlobStore.getState()).toMatchObject({ activeSessionId, error: null });
    });

    it("triggers browser download via object URL", async () => {
      const fakeBlob = new Blob(["data"], { type: "text/csv" });
      const { downloadBlobContent } = await import("@/api/client");
      (downloadBlobContent as ReturnType<typeof vi.fn>).mockResolvedValue({
        data: fakeBlob,
        filename: "result.csv",
      });

      const fakeUrl = "blob:http://localhost/fake-url";
      const createObjectURL = vi.fn().mockReturnValue(fakeUrl);
      const revokeObjectURL = vi.fn();
      globalThis.URL.createObjectURL = createObjectURL;
      globalThis.URL.revokeObjectURL = revokeObjectURL;

      const clickSpy = vi.fn();
      const fakeAnchor = {
        href: "",
        download: "",
        click: clickSpy,
      } as unknown as HTMLAnchorElement;
      const createElementSpy = vi
        .spyOn(document, "createElement")
        .mockReturnValue(fakeAnchor as unknown as HTMLElement);
      const appendChildSpy = vi
        .spyOn(document.body, "appendChild")
        .mockImplementation((node) => node);
      const removeChildSpy = vi
        .spyOn(document.body, "removeChild")
        .mockImplementation((node) => node);

      await useBlobStore.getState().downloadBlob("session-1", "blob-1");

      expect(createObjectURL).toHaveBeenCalledWith(fakeBlob);
      expect(createElementSpy).toHaveBeenCalledWith("a");
      expect(fakeAnchor.href).toBe(fakeUrl);
      expect(fakeAnchor.download).toBe("result.csv");
      expect(appendChildSpy).toHaveBeenCalledWith(fakeAnchor);
      expect(clickSpy).toHaveBeenCalled();
      expect(removeChildSpy).toHaveBeenCalledWith(fakeAnchor);
      expect(revokeObjectURL).toHaveBeenCalledWith(fakeUrl);
      expect(useBlobStore.getState().error).toBeNull();
    });
  });

  describe("clearBlobs", () => {
    it("clears blobs and errors", () => {
      useBlobStore.setState({
        blobs: [makeBlob()],
        error: "Failed to load files.",
        listError: "Failed to load files.",
        listLoadFailed: true,
      });

      useBlobStore.getState().clearBlobs();

      const state = useBlobStore.getState();
      expect(state.blobs).toEqual([]);
      expect(state.error).toBeNull();
      expect(state.listError).toBeNull();
      expect(state.listLoadFailed).toBe(false);
    });
  });

  describe("reset", () => {
    it("restores full initial state", () => {
      useBlobStore.setState({
        blobs: [makeBlob()],
        isLoading: true,
        error: "Failed to load files.",
        listError: "Failed to load files.",
        listLoadFailed: true,
      });

      useBlobStore.getState().reset();

      const state = useBlobStore.getState();
      expect(state.blobs).toEqual([]);
      expect(state.isLoading).toBe(false);
      expect(state.error).toBeNull();
      expect(state.listError).toBeNull();
      expect(state.listLoadFailed).toBe(false);
    });
  });
});
