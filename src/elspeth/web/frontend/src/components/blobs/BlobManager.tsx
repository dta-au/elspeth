// src/components/blobs/BlobManager.tsx
import { useEffect, useLayoutEffect, useRef, useCallback, useMemo, useState } from "react";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { Button, Input } from "@/components/ui";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { BlobRow } from "./BlobRow";
import { IdentityStorageTotal } from "./IdentityStorageTotal";
import type { BlobMetadata, BlobCategory } from "@/types/api";
import { componentOptionsById, optionBlobReferences, promptFileBindings, promptFileEditability } from "@/lib/blobBindings";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";

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
  const selectedNodeId = useSessionStore((s) => s.selectedNodeId);
  const compositionState = useSessionStore((s) => s.compositionState);
  // Use-as-input is a compose entry point: disable it while a freeform
  // compose is in flight (elspeth-3f38ebb1b5) so the manager cannot offer a
  // second compose the store admission gate would refuse.
  const isComposing = useSessionStore((s) => s.isComposing);
  const pending = useInterpretationEventsStore((s) => activeSessionId ? s.pendingBySession[activeSessionId] : undefined);
  const { blobs, isLoading, error, activeSessionId: blobSessionId, loadBlobs, uploadBlob, deleteBlob, downloadBlob } =
    useBlobStore();
  const revealRequest = useBlobStore((s) => s.revealRequest);
  const focusedBlobId = useBlobStore((s) => s.focusedBlobId);
  const [missingRevealName, setMissingRevealName] = useState<string | null>(null);
  const statusRef = useRef<HTMLParagraphElement>(null);
  const previousSelectedNode = useRef(selectedNodeId);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const lastScrolledSelection = useRef<string | null>(null);

  const attached = useMemo(() => {
    const fieldsByBlob = new Map<string, string[]>();
    const options = componentOptionsById(compositionState, selectedNodeId);
    if (options === null) return fieldsByBlob;
    for (const { blobId, fieldPath } of optionBlobReferences(options)) {
      const fields = fieldsByBlob.get(blobId) ?? [];
      if (!fields.includes(fieldPath)) fields.push(fieldPath);
      fieldsByBlob.set(blobId, fields);
    }
    return fieldsByBlob;
  }, [compositionState, selectedNodeId]);

  const listBelongsToSession = blobSessionId === activeSessionId;
  const matchedIds = useMemo(
    () => new Set(blobs.filter((blob) => attached.has(blob.id.toLowerCase())).map((blob) => blob.id.toLowerCase())),
    [attached, blobs],
  );
  const missingCount = !isLoading && listBelongsToSession ? attached.size - matchedIds.size : 0;
  const promptBindings = useMemo(() => promptFileBindings(compositionState), [compositionState]);

  useEffect(() => {
    if (previousSelectedNode.current !== selectedNodeId) {
      previousSelectedNode.current = selectedNodeId;
      useBlobStore.setState({ focusedBlobId: null });
      setMissingRevealName(null);
    }
  }, [selectedNodeId]);

  useLayoutEffect(() => {
    if (activeSessionId) void loadBlobs(activeSessionId);
  }, [activeSessionId, loadBlobs]);

  useLayoutEffect(() => {
    // Read loading from the store, not this render's closure: the mount-time
    // loadBlobs above runs first in this same commit and flips isLoading
    // synchronously. Acting on the stale `false` would focus a cached row the
    // "Loading..." render then unmounts, and consume the request with it.
    if (!revealRequest || revealRequest.sessionId !== activeSessionId || useBlobStore.getState().isLoading || !listBelongsToSession) return;
    const hiddenPane = listRef.current?.closest("[hidden], [inert]");
    if (hiddenPane) {
      const observer = new MutationObserver(() => {
        if (!hiddenPane.hasAttribute("hidden") && !hiddenPane.hasAttribute("inert")) {
          observer.disconnect();
          const current = useBlobStore.getState().revealRequest;
          if (current?.seq === revealRequest.seq) useBlobStore.setState({ revealRequest: { ...current } });
        }
      });
      observer.observe(hiddenPane, { attributes: true, attributeFilter: ["hidden", "inert"] });
      return () => observer.disconnect();
    }
    const row = Array.from(listRef.current?.querySelectorAll<HTMLElement>(".blob-row-item") ?? [])
      .find((item) => item.dataset.blobId?.toLowerCase() === revealRequest.blobId.toLowerCase());
    if (row) {
      setMissingRevealName(null);
      row.scrollIntoView?.({ block: "nearest" });
      row.focus({ preventScroll: true });
    } else {
      setMissingRevealName(revealRequest.filename);
      useBlobStore.setState({ focusedBlobId: null });
      statusRef.current?.focus({ preventScroll: true });
    }
    useBlobStore.getState().consumeRevealRequest(revealRequest.seq);
  }, [revealRequest, activeSessionId, isLoading, listBelongsToSession, blobs]);

  useEffect(() => {
    if (selectedNodeId === null) {
      lastScrolledSelection.current = null;
      return;
    }
    if (lastScrolledSelection.current === selectedNodeId || isLoading || !listBelongsToSession) return;
    const firstRow = Array.from(listRef.current?.querySelectorAll<HTMLElement>(".blob-row-item") ?? [])
      .find((row) => attached.has(row.dataset.blobId?.toLowerCase() ?? ""));
    if (firstRow) firstRow.scrollIntoView?.({ block: "nearest", behavior: "auto" });
    lastScrolledSelection.current = selectedNodeId;
  }, [selectedNodeId, attached, isLoading, listBelongsToSession, blobs]);
  // Blob queued for deletion, awaiting confirmation. Blobs are pipeline
  // inputs/outputs, so deletion is irreversible data loss (WCAG 3.3.4). We
  // capture the session the blob belongs to alongside the blob: if the active
  // session changes while the dialog is open (e.g. via the global Ctrl/Cmd+N
  // shortcut), the delete must still target the originating session, not
  // whichever session happens to be active at confirmation time.
  const [pendingDelete, setPendingDelete] = useState<{ blob: BlobMetadata; sessionId: string } | null>(null);

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

  const close = () => {
    useBlobStore.getState().setFileManagerOpen(false);
    useBlobStore.getState().returnToRevealTrigger();
  };

  if (!activeSessionId) return null;

  return (
    <div
      className="blob-manager blob-manager-container"
      onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); close(); } }}
    >
      {/* Header */}
      <div className="blob-manager-header">
        <span className="blob-manager-title">
          Files ({blobs.length})
        </span>
        <Button type="button" variant="bare" onClick={close}>Close file manager</Button>
        <IdentityStorageTotal refreshKey={`${activeSessionId}:${blobs.map((blob) => `${blob.id}:${blob.size_bytes}`).join(",")}`} />
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

      <p ref={statusRef} tabIndex={-1} className="blob-manager-attached-summary" role="status" aria-live="polite">
        {missingRevealName ? `${missingRevealName} is no longer in this session` : selectedNodeId && attached.size > 0 && listBelongsToSession ? <>
          {isLoading ? `Checking files used by ${selectedNodeId}` : `${matchedIds.size} ${matchedIds.size === 1 ? "file" : "files"} used by ${selectedNodeId}`}
          {missingCount > 0 && ` · ${missingCount} attached ${missingCount === 1 ? "file is" : "files are"} not in this session`}
          {focusedBlobId && blobs.find((blob) => blob.id.toLowerCase() === focusedBlobId.toLowerCase()) && ` · Showing ${blobs.find((blob) => blob.id.toLowerCase() === focusedBlobId.toLowerCase())?.filename}.`}
        </> : ""}
      </p>

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
      <div className="blob-manager-list" ref={listRef}>
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
                {categoryBlobs.map((blob) => {
                  // Every prompt-bound file gets an Edit prompt control, never a
                  // silently missing one. A file bound more than once opens the
                  // binding on the selected step when there is one, else the first.
                  const bindings = promptBindings.filter((binding) => binding.blobId === blob.id.toLowerCase());
                  const binding = bindings.find((candidate) => candidate.nodeId === selectedNodeId) ?? bindings[0];
                  const editability = binding ? promptFileEditability({ state: compositionState, blobs, pendingEvents: Object.values(pending ?? {}),
                    nodeId: binding.nodeId, fieldPath: binding.fieldPath, blobId: blob.id }) : null;
                  // A refused save still opens the editor read-only with its
                  // reason; only a file with no prompt text to show stays shut.
                  const opensEditor = editability !== null && (editability.editable || editability.opens);
                  return <BlobRow
                    key={blob.id}
                    blob={blob}
                    sessionId={activeSessionId}
                    onDownload={handleDownload}
                    onDelete={handleRequestDelete}
                    onUseAsInput={onUseAsInput}
                    useAsInputDisabled={isComposing}
                    attachedFields={listBelongsToSession ? attached.get(blob.id.toLowerCase()) : undefined}
                    selectedNodeLabel={selectedNodeId}
                    focused={focusedBlobId?.toLowerCase() === blob.id.toLowerCase()}
                    onSelect={() => useBlobStore.setState({ focusedBlobId: blob.id })}
                    editRefusal={editability?.editable === false ? editability.reason : undefined}
                    onEditPrompt={binding && opensEditor ? () => {
                      useSessionStore.getState().selectNode(binding.nodeId);
                      useBlobStore.getState().requestPromptEdit(binding.nodeId, binding.fieldPath);
                    } : undefined}
                  />
                })}
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
