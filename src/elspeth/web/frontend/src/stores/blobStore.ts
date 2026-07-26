// src/stores/blobStore.ts
import { create } from "zustand";
import type { BlobMetadata } from "@/types/api";
import * as api from "@/api/client";

interface BlobUploadOptions {
  /** Request-owner fence for publishing a mapped upload error. */
  shouldPublishError?: (error: unknown) => boolean;
}

interface BlobState {
  blobs: BlobMetadata[];
  isLoading: boolean;
  error: string | null;
  /** Session whose rows may be published into this store instance. */
  activeSessionId: string | null;
  /** Monotonic generation for session-bound async completion fences. */
  activationEpoch: number;

  activateSession: (sessionId: string | null) => void;
  invalidateBlobForEpoch: (
    sessionId: string,
    activationEpoch: number,
    blobId: string,
  ) => boolean;
  loadBlobs: (sessionId: string) => Promise<void>;
  uploadBlob: (
    sessionId: string,
    file: File,
    options?: BlobUploadOptions,
  ) => Promise<BlobMetadata>;
  deleteBlob: (sessionId: string, blobId: string) => Promise<void>;
  downloadBlob: (sessionId: string, blobId: string) => Promise<void>;
  clearBlobs: () => void;
  reset: () => void;
}

const initialState = {
  blobs: [] as BlobMetadata[],
  isLoading: false,
  error: null as string | null,
  activeSessionId: null as string | null,
  activationEpoch: 0,
};

let blobLoadRequestSeq = 0;

interface BlobMutationOwnership {
  sessionId: string;
  ownerEpoch: number;
  ownsStore: boolean;
}

export const useBlobStore = create<BlobState>((set, get) => ({
  ...initialState,

  activateSession(sessionId: string | null) {
    const state = get();
    if (state.activeSessionId === sessionId) return;
    blobLoadRequestSeq++;
    set({
      blobs: [],
      isLoading: false,
      error: null,
      activeSessionId: sessionId,
      activationEpoch: state.activationEpoch + 1,
    });
  },

  invalidateBlobForEpoch(
    sessionId: string,
    activationEpoch: number,
    blobId: string,
  ) {
    const state = get();
    if (
      state.activeSessionId !== sessionId ||
      state.activationEpoch !== activationEpoch
    ) {
      return false;
    }
    // Cancel any older list publication that could restore the invalidated
    // row. A subsequent authoritative load claims a newer request sequence.
    blobLoadRequestSeq++;
    set((current) => ({
      blobs: current.blobs.filter((blob) => blob.id !== blobId),
      isLoading: false,
    }));
    return true;
  },

  async loadBlobs(sessionId: string) {
    get().activateSession(sessionId);
    const requestSeq = ++blobLoadRequestSeq;
    set({ isLoading: true, error: null });
    try {
      const blobs = await api.listBlobs(sessionId);
      if (requestSeq !== blobLoadRequestSeq) {
        return;
      }
      set({ blobs, isLoading: false });
    } catch {
      if (requestSeq !== blobLoadRequestSeq) {
        return;
      }
      set({ error: "Failed to load files.", isLoading: false });
    }
  },

  async uploadBlob(
    sessionId: string,
    file: File,
    options?: BlobUploadOptions,
  ) {
    if (get().activeSessionId === null) {
      set((state) => ({
        activeSessionId: sessionId,
        activationEpoch: state.activationEpoch + 1,
      }));
    }
    const ownerState = get();
    const ownership: BlobMutationOwnership = {
      sessionId,
      ownerEpoch: ownerState.activationEpoch,
      ownsStore: ownerState.activeSessionId === sessionId,
    };
    if (ownership.ownsStore) {
      set({ error: null });
    }
    try {
      const blob = await api.uploadBlob(sessionId, file);
      const current = get();
      if (
        !ownership.ownsStore ||
        current.activeSessionId !== ownership.sessionId ||
        current.activationEpoch !== ownership.ownerEpoch
      ) {
        return blob;
      }
      blobLoadRequestSeq++;
      set((state) => ({
        blobs: [blob, ...state.blobs.filter((item) => item.id !== blob.id)],
        isLoading: false,
      }));
      return blob;
    } catch (err) {
      const detail =
        (err as { status?: number }).status === 413
          ? "File exceeds the maximum upload size."
          : (err as { status?: number }).status === 415
            ? "Unsupported file type. Please use CSV, JSON, JSONL, or plain text."
            : "Upload failed. Please try again.";
      const current = get();
      if (
        (options?.shouldPublishError?.(err) ?? true) &&
        ownership.ownsStore &&
        current.activeSessionId === ownership.sessionId &&
        current.activationEpoch === ownership.ownerEpoch
      ) {
        set({ error: detail });
      }
      throw err;
    }
  },

  async deleteBlob(sessionId: string, blobId: string) {
    if (get().activeSessionId === null) {
      set((state) => ({
        activeSessionId: sessionId,
        activationEpoch: state.activationEpoch + 1,
      }));
    }
    const ownerState = get();
    const ownership: BlobMutationOwnership = {
      sessionId,
      ownerEpoch: ownerState.activationEpoch,
      ownsStore: ownerState.activeSessionId === sessionId,
    };
    try {
      await api.deleteBlob(sessionId, blobId);
      const current = get();
      if (
        !ownership.ownsStore ||
        current.activeSessionId !== ownership.sessionId ||
        current.activationEpoch !== ownership.ownerEpoch
      ) {
        return;
      }
      blobLoadRequestSeq++;
      set((state) => ({
        blobs: state.blobs.filter((b) => b.id !== blobId),
        isLoading: false,
      }));
    } catch (err) {
      const detail =
        (err as { status?: number }).status === 409
          ? "Cannot delete — file is linked to an active run."
          : "Failed to delete file.";
      const current = get();
      if (
        ownership.ownsStore &&
        current.activeSessionId === ownership.sessionId &&
        current.activationEpoch === ownership.ownerEpoch
      ) {
        set({ error: detail });
      }
    }
  },

  async downloadBlob(sessionId: string, blobId: string) {
    try {
      const { data, filename } = await api.downloadBlobContent(
        sessionId,
        blobId,
      );
      // Trigger browser download via object URL
      const url = URL.createObjectURL(data);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch {
      set({ error: "Download failed." });
    }
  },

  clearBlobs() {
    blobLoadRequestSeq++;
    set((state) => ({
      blobs: [],
      isLoading: false,
      error: null,
      activeSessionId: null,
      activationEpoch: state.activationEpoch + 1,
    }));
  },

  reset() {
    blobLoadRequestSeq++;
    set((state) => ({
      ...initialState,
      activationEpoch: state.activationEpoch + 1,
    }));
  },
}));
