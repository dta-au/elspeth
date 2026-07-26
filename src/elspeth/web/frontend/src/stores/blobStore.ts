// src/stores/blobStore.ts
import { create } from "zustand";
import type { BlobMetadata } from "@/types/api";
import * as api from "@/api/client";

interface BlobState {
  blobs: BlobMetadata[];
  isLoading: boolean;
  error: string | null;

  loadBlobs: (sessionId: string) => Promise<void>;
  uploadBlob: (sessionId: string, file: File) => Promise<BlobMetadata>;
  deleteBlob: (sessionId: string, blobId: string) => Promise<void>;
  downloadBlob: (sessionId: string, blobId: string) => Promise<void>;
  clearBlobs: () => void;
  reset: () => void;
}

const initialState = {
  blobs: [] as BlobMetadata[],
  isLoading: false,
  error: null as string | null,
};

let blobLoadRequestSeq = 0;
let blobOwnerSessionId: string | null = null;
let blobOwnerEpoch = 0;

interface BlobMutationOwnership {
  sessionId: string;
  ownerEpoch: number;
  ownsStore: boolean;
}

function beginBlobMutation(sessionId: string): BlobMutationOwnership {
  if (blobOwnerSessionId === null) {
    blobOwnerSessionId = sessionId;
    blobOwnerEpoch++;
  }
  return {
    sessionId,
    ownerEpoch: blobOwnerEpoch,
    ownsStore: blobOwnerSessionId === sessionId,
  };
}

function mutationStillOwnsStore(ownership: BlobMutationOwnership): boolean {
  return (
    ownership.ownsStore &&
    blobOwnerSessionId === ownership.sessionId &&
    blobOwnerEpoch === ownership.ownerEpoch
  );
}

export const useBlobStore = create<BlobState>((set) => ({
  ...initialState,

  async loadBlobs(sessionId: string) {
    const sessionChanged = blobOwnerSessionId !== sessionId;
    if (sessionChanged) {
      blobOwnerSessionId = sessionId;
      blobOwnerEpoch++;
    }
    const requestSeq = ++blobLoadRequestSeq;
    set({
      ...(sessionChanged ? { blobs: [] } : {}),
      isLoading: true,
      error: null,
    });
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

  async uploadBlob(sessionId: string, file: File) {
    const ownership = beginBlobMutation(sessionId);
    if (ownership.ownsStore) {
      set({ error: null });
    }
    try {
      const blob = await api.uploadBlob(sessionId, file);
      if (!mutationStillOwnsStore(ownership)) {
        return blob;
      }
      blobLoadRequestSeq++;
      set((state) => ({
        blobs: [blob, ...state.blobs],
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
      if (mutationStillOwnsStore(ownership)) {
        set({ error: detail });
      }
      throw err;
    }
  },

  async deleteBlob(sessionId: string, blobId: string) {
    const ownership = beginBlobMutation(sessionId);
    try {
      await api.deleteBlob(sessionId, blobId);
      if (!mutationStillOwnsStore(ownership)) {
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
      if (mutationStillOwnsStore(ownership)) {
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
    blobOwnerSessionId = null;
    blobOwnerEpoch++;
    blobLoadRequestSeq++;
    set({ blobs: [], isLoading: false, error: null });
  },

  reset() {
    blobOwnerSessionId = null;
    blobOwnerEpoch++;
    blobLoadRequestSeq++;
    set(initialState);
  },
}));
