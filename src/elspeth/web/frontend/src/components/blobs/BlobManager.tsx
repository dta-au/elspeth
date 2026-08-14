// src/components/blobs/BlobManager.tsx
import { useEffect, useRef, useCallback, useMemo, useState } from "react";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { Button, Input } from "@/components/ui";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { BlobRow } from "./BlobRow";
import type { BlobMetadata, BlobCategory } from "@/types/api";

interface BlobManagerProps {
  onUseAsInput: (blob: BlobMetadata) => void;
}

/**
 * Categorize a blob into source/sink/other based on who created it.
 * - User uploads → source files (pipeline inputs)
 * - Pipeline outputs → sink files (results)
 * - Assistant-created → other (prompts, templates, config)
 */
function categorizeBlob(blob: BlobMetadata): BlobCategory {
  if (blob.created_by === "user") return "source";
  if (blob.created_by === "pipeline") return "sink";
  return "other";
}

const CATEGORY_LABELS: Record<BlobCategory, string> = {
  source: "Source files",
  sink: "Output files",
  other: "Other files",
};

const CATEGORY_ORDER: BlobCategory[] = ["source", "sink", "other"];

/**
 * Collapsible blob manager panel with categorized folders.
 * Shows session-scoped files grouped by source/output/other
 * with upload, download, delete, and "use as input" actions.
 */
export function BlobManager({ onUseAsInput }: BlobManagerProps) {
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  // Use-as-input is a compose entry point: disable it while a freeform
  // compose is in flight (elspeth-3f38ebb1b5) so the manager cannot offer a
  // second compose the store admission gate would refuse.
  const isComposing = useSessionStore((s) => s.isComposing);
  const { blobs, isLoading, error, loadBlobs, uploadBlob, deleteBlob, downloadBlob } =
    useBlobStore();
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Blob queued for deletion, awaiting confirmation. Blobs are pipeline
  // inputs/outputs, so deletion is irreversible data loss (WCAG 3.3.4). We
  // capture the session the blob belongs to alongside the blob: if the active
  // session changes while the dialog is open (e.g. via the global Ctrl/Cmd+N
  // shortcut), the delete must still target the originating session, not
  // whichever session happens to be active at confirmation time.
  const [pendingDelete, setPendingDelete] = useState<{ blob: BlobMetadata; sessionId: string } | null>(null);

  useEffect(() => {
    if (activeSessionId) {
      loadBlobs(activeSessionId);
    }
  }, [activeSessionId, loadBlobs]);

  const grouped = useMemo(() => {
    const groups: Record<BlobCategory, BlobMetadata[]> = {
      source: [],
      sink: [],
      other: [],
    };
    for (const blob of blobs) {
      groups[categorizeBlob(blob)].push(blob);
    }
    return groups;
  }, [blobs]);

  const handleUpload = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file || !activeSessionId) return;
      try {
        await uploadBlob(activeSessionId, file);
      } catch {
        // Error is already in the store
      } finally {
        if (fileInputRef.current) {
          fileInputRef.current.value = "";
        }
      }
    },
    [activeSessionId, uploadBlob],
  );

  // Row delete buttons only *request* deletion; the actual delete fires from
  // confirmDelete once the user confirms in the dialog.
  const handleRequestDelete = useCallback(
    (blobId: string) => {
      if (!activeSessionId) return;
      const blob = blobs.find((b) => b.id === blobId);
      if (!blob) return;
      setPendingDelete({ blob, sessionId: activeSessionId });
    },
    [blobs, activeSessionId],
  );

  const confirmDelete = useCallback(() => {
    if (!pendingDelete) return;
    // Use the session captured when deletion was requested, not the current
    // activeSessionId — they can differ if the session changed mid-dialog.
    deleteBlob(pendingDelete.sessionId, pendingDelete.blob.id);
    setPendingDelete(null);
  }, [pendingDelete, deleteBlob]);

  const handleDownload = useCallback(
    (blobId: string) => {
      if (!activeSessionId) return;
      downloadBlob(activeSessionId, blobId);
    },
    [activeSessionId, downloadBlob],
  );

  if (!activeSessionId) return null;

  return (
    <div
      className="blob-manager blob-manager-container"
    >
      {/* Header */}
      <div className="blob-manager-header">
        <span className="blob-manager-title">
          Files ({blobs.length})
        </span>
        {/* Label-only, and its accessible name IS its visible text
            (elspeth-29eef452a8). The label used to read "+ Upload" against an
            aria-label of "Upload file": a typed plus sign is a glyph drawn in
            the 12px UI font, so it matched neither the weight nor the optical
            size of the stroked SVGs in the rows directly below it, and the
            accessible name did not contain the visible label, which breaks
            voice-control targeting by visible text (WCAG 2.5.3). Dropping the
            redundant aria-label is what keeps the two in step — do not
            reintroduce one that says something else. */}
        <Button
          onClick={() => fileInputRef.current?.click()}
          className="blob-manager-upload-btn"
        >
          Upload
        </Button>
        <Input
          ref={fileInputRef}
          type="file"
          onChange={handleUpload}
          style={{ display: "none" }}
          aria-hidden="true"
          tabIndex={-1}
        />
      </div>

      {/* Error */}
      {error && (
        <div
          role="alert"
          className="blob-manager-error"
        >
          {error}
        </div>
      )}

      {/* Categorized file list */}
      <div className="blob-manager-list">
        {isLoading ? (
          <div className="blob-manager-loading">
            Loading...
          </div>
        ) : blobs.length === 0 ? (
          <div className="blob-manager-empty">
            No files yet. Upload a file to get started.
          </div>
        ) : (
          CATEGORY_ORDER.map((category) => {
            const categoryBlobs = grouped[category];
            if (categoryBlobs.length === 0) return null;
            return (
              <div key={category}>
                <div className="blob-manager-category-header">
                  {CATEGORY_LABELS[category]}
                </div>
                {categoryBlobs.map((blob) => (
                  <BlobRow
                    key={blob.id}
                    blob={blob}
                    sessionId={activeSessionId}
                    onDownload={handleDownload}
                    onDelete={handleRequestDelete}
                    onUseAsInput={onUseAsInput}
                    useAsInputDisabled={isComposing}
                  />
                ))}
              </div>
            );
          })
        )}
      </div>

      {pendingDelete && (
        <ConfirmDialog
          title="Delete file"
          message={`Delete "${pendingDelete.blob.filename}"? This cannot be undone.`}
          confirmLabel="Delete"
          variant="danger"
          onConfirm={confirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  );
}
