// src/stores/blobStore.ts
import { create } from "zustand";
import type { BlobMetadata } from "@/types/api";
import * as api from "@/api/client";
import { BlobListTruncatedError } from "@/api/blobListing";
import type { ApiError } from "@/types/index";
import { storageQuotaMessage } from "@/utils/bytes";

interface BlobUploadOptions {
  /** Request-owner fence for publishing a mapped upload error. */
  shouldPublishError?: (error: unknown) => boolean;
}

interface BlobState {
  blobs: BlobMetadata[];
  isLoading: boolean;
  error: string | null;
  listError: string | null;
  /** The file-list read failed and can be retried. */
  listLoadFailed: boolean;
  /** Session whose rows may be published into this store instance. */
  activeSessionId: string | null;
  /** Monotonic generation for session-bound async completion fences. */
  activationEpoch: number;
  fileManagerOpen: boolean;
  revealRequest: { sessionId: string; blobId: string; seq: number; filename: string } | null;
  focusedBlobId: string | null;
  editRequest: { nodeId: string; fieldPath: string; seq: number } | null;
  /** Mounted components that can render the file manager. Show in files is offered only while one exists. */
  fileManagerHosts: number;
  /**
   * Closing the file manager could not focus the Show in files trigger
   * because it sits in a hidden or inert pane (the narrow workspace swapped
   * it out). The workspace consumes this by showing the pane that holds
   * `trigger`; focus follows once the pane is no longer hidden.
   */
  returnRequest: { trigger: HTMLElement; seq: number } | null;
  registerFileManagerHost: () => () => void;
  consumeReturnRequest: (seq: number) => void;
  revealBlob: (sessionId: string, blobId: string, filename: string, trigger?: HTMLElement) => void;
  consumeRevealRequest: (seq: number) => void;
  setFileManagerOpen: (open: boolean) => void;
  requestPromptEdit: (nodeId: string, fieldPath: string) => void;
  consumePromptEdit: (seq: number) => void;
  returnToRevealTrigger: () => void;

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

export function blobBannerMessages(error: string | null, listError: string | null): string[] {
  const messages = [listError, error].filter(
    (message): message is string => message !== null && message !== "",
  );
  return [...new Set(messages)];
}

const initialState = {
  blobs: [] as BlobMetadata[],
  isLoading: false,
  error: null as string | null,
  listError: null as string | null,
  listLoadFailed: false,
  activeSessionId: null as string | null,
  activationEpoch: 0,
  fileManagerOpen: false,
  revealRequest: null as BlobState["revealRequest"],
  focusedBlobId: null as string | null,
  editRequest: null as BlobState["editRequest"],
  returnRequest: null as BlobState["returnRequest"],
};

let blobLoadRequestSeq = 0;
let revealSeq = 0;
let editSeq = 0;
let revealTrigger: HTMLElement | null = null;
let returnSeq = 0;
let returnObserver: MutationObserver | null = null;

function stopReturnObserver(): void {
  returnObserver?.disconnect();
  returnObserver = null;
}

const HIDDEN_ANCESTOR = "[hidden], [inert]";

/** Focus `trigger` once no ancestor hides it or makes it inert. */
function focusWhenShown(trigger: HTMLElement): void {
  stopReturnObserver();
  const hidden = trigger.closest(HIDDEN_ANCESTOR);
  if (hidden === null) {
    trigger.focus({ preventScroll: true });
    return;
  }
  const observer = new MutationObserver(() => {
    if (!trigger.isConnected) {
      stopReturnObserver();
      return;
    }
    // A nested chain of hidden containers clears one attribute at a time:
    // re-arm on whichever ancestor is still hiding the trigger.
    if (hidden.hasAttribute("hidden") || hidden.hasAttribute("inert")) return;
    focusWhenShown(trigger);
  });
  observer.observe(hidden, { attributes: true, attributeFilter: ["hidden", "inert"] });
  returnObserver = observer;
}

/** User-facing reason for a failed upload: storage quota, size, file type, or a generic retry. */
export function uploadRefusalMessage(err: unknown): string {
  const refusal = (typeof err === "object" && err !== null ? err : {}) as Partial<ApiError>;
  if (refusal.status === 413 && refusal.storage_quota !== undefined) return storageQuotaMessage(refusal.storage_quota);
  if (refusal.status === 413 && refusal.error_type === "storage_quota_exceeded") {
    return "Storage quota reached. Free storage or ask an administrator to review the applicable limit, then try again.";
  }
  if (refusal.status === 413) return "File exceeds the maximum upload size.";
  if (refusal.status === 415) return "Unsupported file type. Please use CSV, JSON, JSONL, or plain text.";
  return "Upload failed. Please try again.";
}

interface BlobMutationOwnership {
  sessionId: string;
  ownerEpoch: number;
  ownsStore: boolean;
}

export const useBlobStore = create<BlobState>((set, get) => ({
  ...initialState,
  // Not part of initialState: hosts are mounted components, so reset() on
  // logout must not forget one that is still on screen.
  fileManagerHosts: 0,

  registerFileManagerHost() {
    set((state) => ({ fileManagerHosts: state.fileManagerHosts + 1 }));
    let registered = true;
    return () => {
      if (!registered) return;
      registered = false;
      set((state) => ({ fileManagerHosts: Math.max(0, state.fileManagerHosts - 1) }));
    };
  },
  consumeReturnRequest(seq) {
    if (get().returnRequest?.seq === seq) set({ returnRequest: null });
  },

  revealBlob(sessionId, blobId, filename, trigger) {
    if (get().activeSessionId !== sessionId) return;
    revealTrigger = trigger ?? null;
    stopReturnObserver();
    set({ returnRequest: null, fileManagerOpen: true, focusedBlobId: blobId, revealRequest: { sessionId, blobId, filename, seq: ++revealSeq } });
  },
  consumeRevealRequest(seq) {
    if (get().revealRequest?.seq === seq) set({ revealRequest: null });
  },
  setFileManagerOpen(open) {
    if (!open) set({ fileManagerOpen: false, focusedBlobId: null, revealRequest: null });
    else set({ fileManagerOpen: true });
  },
  requestPromptEdit(nodeId, fieldPath) {
    set({ editRequest: { nodeId, fieldPath, seq: ++editSeq } });
  },
  consumePromptEdit(seq) {
    if (get().editRequest?.seq === seq) set({ editRequest: null });
  },
  returnToRevealTrigger() {
    const trigger = revealTrigger;
    revealTrigger = null;
    if (!trigger?.isConnected) return;
    // A hidden or inert trigger is not focusable (HTML focusing steps), so
    // focus() would silently drop to <body>. Ask the workspace to show the
    // view that holds it, and focus it when it is shown.
    if (trigger.closest(HIDDEN_ANCESTOR) !== null) set({ returnRequest: { trigger, seq: ++returnSeq } });
    focusWhenShown(trigger);
  },

  activateSession(sessionId: string | null) {
    const state = get();
    if (state.activeSessionId === sessionId) return;
    blobLoadRequestSeq++;
    set({
      blobs: [],
      isLoading: false,
      error: null,
      listError: null,
      listLoadFailed: false,
      activeSessionId: sessionId,
      activationEpoch: state.activationEpoch + 1,
      fileManagerOpen: false,
      revealRequest: null,
      focusedBlobId: null,
      editRequest: null,
      returnRequest: null,
    });
    revealTrigger = null;
    stopReturnObserver();
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
    set({ isLoading: true, error: null, listError: null, listLoadFailed: false });
    try {
      const blobs = await api.listBlobs(sessionId);
      if (requestSeq !== blobLoadRequestSeq) {
        return;
      }
      set({ blobs, listError: null, isLoading: false });
    } catch (err) {
      if (requestSeq !== blobLoadRequestSeq) {
        return;
      }
      if (err instanceof BlobListTruncatedError) {
        // Past the client's ceiling: show the newest files and say that
        // older ones are not listed, instead of dropping them silently.
        set({
          blobs: err.blobs,
          error: err.message,
          listError: err.message,
          listLoadFailed: false,
          isLoading: false,
        });
        return;
      }
      const message = "Failed to load files.";
      set({ error: message, listError: message, listLoadFailed: true, isLoading: false });
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
      const detail = uploadRefusalMessage(err);
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
      const detail = (err as ApiError).detail ?? "Failed to delete file.";
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
      listError: null,
      listLoadFailed: false,
      activeSessionId: null,
      activationEpoch: state.activationEpoch + 1,
      fileManagerOpen: false,
      revealRequest: null,
      focusedBlobId: null,
      editRequest: null,
      returnRequest: null,
    }));
    revealTrigger = null;
    stopReturnObserver();
  },

  reset() {
    blobLoadRequestSeq++;
    set((state) => ({
      ...initialState,
      activationEpoch: state.activationEpoch + 1,
    }));
    revealTrigger = null;
    stopReturnObserver();
  },
}));
