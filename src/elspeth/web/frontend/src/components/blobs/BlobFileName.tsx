import { useEffect } from "react";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { BreakableName } from "./BreakableName";

export function useBlobFilename(blobId: string): string | null {
  const sessionId = useSessionStore((state) => state.activeSessionId);
  return useBlobStore((state) =>
    state.activeSessionId === sessionId
      ? state.blobs.find((blob) => blob.id.toLowerCase() === blobId.toLowerCase())?.filename ?? null
      : null,
  );
}

/**
 * Read the active session's file list when a name is needed and a read has
 * yet to settle or start for this activation. A second mounted caller sees
 * the running read and skips it. Only a complete list establishes absence.
 */
export function useSessionFileList(needed: boolean): void {
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const listSessionId = useBlobStore((state) => state.activeSessionId);
  useEffect(() => {
    if (!needed || sessionId === null) return;
    // The store as it is now, not as this render saw it: another caller may
    // have started the read in the same commit.
    const state = useBlobStore.getState();
    if (state.activeSessionId === sessionId && (state.listCurrent || state.isLoading)) return;
    void state.loadBlobs(sessionId);
  }, [needed, sessionId, listSessionId]);
}

/** `bare` drops the "File:" prefix where a column header already says it, and lets a long name wrap at its separators. */
export function BlobFileName({ blobId, sha256, fallback, bare = false }: { blobId: string; sha256?: string; fallback?: string; bare?: boolean }) {
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const blob = useBlobStore((state) =>
    state.activeSessionId === sessionId
      ? state.blobs.find((item) => item.id.toLowerCase() === blobId.toLowerCase())
      : undefined,
  );
  const loading = useBlobStore((state) => sessionId === null || state.activeSessionId !== sessionId || !state.listCurrent || state.isLoading);
  const listIncomplete = useBlobStore((state) => sessionId !== null && state.activeSessionId === sessionId && state.listError !== null);
  if (blob === undefined) {
    const defaultName = loading ? "Loading file name…" : listIncomplete ? "File name unavailable" : "File not in this session";
    return <span className="graph-config-empty-value" title={blobId}>{fallback ?? defaultName}</span>;
  }
  const changed = sha256 !== undefined && blob.content_hash !== null && sha256.toLowerCase() !== blob.content_hash.toLowerCase();
  return (
    <span title={`File id ${blobId}${sha256 ? ` · SHA-256 ${sha256.slice(0, 12)}…` : ""}`} data-blob-id={blobId}>
      {bare ? null : "File: "}{bare ? <BreakableName name={blob.filename} /> : blob.filename}{changed && <span> (changed since attached)</span>}
    </span>
  );
}
