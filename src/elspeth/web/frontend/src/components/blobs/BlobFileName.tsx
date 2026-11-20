import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";

export function useBlobFilename(blobId: string): string | null {
  const sessionId = useSessionStore((state) => state.activeSessionId);
  return useBlobStore((state) =>
    state.activeSessionId === sessionId
      ? state.blobs.find((blob) => blob.id.toLowerCase() === blobId.toLowerCase())?.filename ?? null
      : null,
  );
}

/** `bare` drops the "File:" prefix where a column header already says it. */
export function BlobFileName({ blobId, sha256, fallback, bare = false }: { blobId: string; sha256?: string; fallback?: string; bare?: boolean }) {
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const blob = useBlobStore((state) =>
    state.activeSessionId === sessionId
      ? state.blobs.find((item) => item.id.toLowerCase() === blobId.toLowerCase())
      : undefined,
  );
  const loading = useBlobStore((state) => sessionId === null || state.activeSessionId !== sessionId || state.isLoading);
  if (blob === undefined) {
    return <span className="graph-config-empty-value" title={blobId}>{fallback ?? (loading ? "Loading file name…" : "File not in this session")}</span>;
  }
  const changed = sha256 !== undefined && blob.content_hash !== null && sha256.toLowerCase() !== blob.content_hash.toLowerCase();
  return (
    <span title={`File id ${blobId}${sha256 ? ` · SHA-256 ${sha256.slice(0, 12)}…` : ""}`} data-blob-id={blobId}>
      {bare ? null : "File: "}{blob.filename}{changed && <span> (changed since attached)</span>}
    </span>
  );
}
