import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { fetchCompositionState, readPromptFileText, savePromptFile } from "@/api/client";
import { Button } from "@/components/ui";
import { useBlobFilename } from "@/components/blobs/BlobFileName";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { promptFileEditability, promptFileSlots } from "@/lib/blobBindings";
import { optionLabel } from "./OptionRows";

interface PromptFileEditorProps {
  nodeId: string;
  blobId: string;
  fieldPath: string;
  /** Why the server will refuse every save for this binding; Save stays disabled while set. */
  saveRefusal?: string | null;
  /** The query's reader-facing name for a `queries.<selector>.template` slot. */
  queryName?: string;
}

const MULTI_BINDING_REFUSAL = "This node uses more than one prompt file. Re-point it through the agent and approve the review card.";

function blobId(value: unknown): string | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const marker = value as Record<string, unknown>;
  return marker.mode === "inline_content" && typeof marker.blob_ref === "string" ? marker.blob_ref : null;
}

export function PromptFileEditors({ nodeId, options }: { nodeId: string; options: Record<string, unknown> }) {
  const bindings = promptFileSlots(options).flatMap(({ fieldPath, value, queryName }) => {
    const id = blobId(value);
    return id ? [{ fieldPath, blobId: id, queryName }] : [];
  });
  // Mirrors the server rule (prompt_file_binding_paths > 1 refuses every save
  // with prompt_file_node_multi_binding), so the user is told before saving.
  const saveRefusal = bindings.length > 1 ? MULTI_BINDING_REFUSAL : null;
  return <>{bindings.map((binding) => <PromptFileEditor key={`${nodeId}:${binding.fieldPath}:${binding.blobId}`} nodeId={nodeId} saveRefusal={saveRefusal} {...binding} />)}</>;
}

export function PromptFileEditor({ nodeId, blobId, fieldPath, saveRefusal = null, queryName }: PromptFileEditorProps) {
  const filename = useBlobFilename(blobId);
  // The same query label the inspector row shows (promptQueryEntries `name`),
  // never the list index the slot path uses.
  const slotLabel = queryName === undefined ? optionLabel(fieldPath) : `User prompt, query ${queryName}`;
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const filenameLoading = useBlobStore((state) => state.activeSessionId !== sessionId || state.isLoading);
  const blobSessionId = useBlobStore((state) => state.activeSessionId);
  const blobs = useBlobStore((state) => state.blobs);
  const editRequest = useBlobStore((state) => state.editRequest);
  const compositionState = useSessionStore((state) => state.compositionState);
  const pending = useInterpretationEventsStore((state) => sessionId ? state.pendingBySession[sessionId] : undefined);
  const isComposing = useSessionStore((state) => state.isComposing);
  const [text, setText] = useState<string | null>(null);
  const [savedText, setSavedText] = useState<string | null>(null);
  const [expectedSha256, setExpectedSha256] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const shouldFocusRef = useRef(false);
  const editability = compositionState?.nodes.some((node) => node.id === nodeId)
    ? promptFileEditability({ state: compositionState, blobs: blobSessionId === sessionId ? blobs : [],
      pendingEvents: Object.values(pending ?? {}), nodeId, fieldPath, blobId })
    : null;
  const refusal = editability?.editable === false ? editability.reason : saveRefusal;

  useLayoutEffect(() => {
    if (editRequest?.nodeId !== nodeId || editRequest.fieldPath !== fieldPath) return;
    shouldFocusRef.current = true;
    setExpanded(true);
    useBlobStore.getState().consumePromptEdit(editRequest.seq);
  }, [editRequest, nodeId, fieldPath]);
  useLayoutEffect(() => {
    if (!expanded || text === null || !shouldFocusRef.current) return;
    const focusEditor = () => {
      shouldFocusRef.current = false;
      editorRef.current?.scrollIntoView?.({ block: "nearest" });
      editorRef.current?.focus({ preventScroll: true });
    };
    const hiddenPane = editorRef.current?.closest("[hidden], [inert]");
    if (hiddenPane) {
      const observer = new MutationObserver(() => {
        if (!hiddenPane.hasAttribute("hidden") && !hiddenPane.hasAttribute("inert")) {
          observer.disconnect();
          focusEditor();
        }
      });
      observer.observe(hiddenPane, { attributes: true, attributeFilter: ["hidden", "inert"] });
      return () => observer.disconnect();
    }
    focusEditor();
  }, [expanded, text]);

  useEffect(() => {
    let live = true;
    setText(null);
    setSavedText(null);
    setExpectedSha256(null);
    setError(null);
    if (sessionId) {
      readPromptFileText(sessionId, blobId)
        .then(({ text: content, sha256 }) => {
          if (!live) return;
          setExpectedSha256(sha256);
          setSavedText(content);
          setText(content);
        })
        .catch((failure: unknown) => {
          if (live) setError(failure instanceof Error ? failure.message : "Could not load this prompt file.");
        });
    }
    return () => { live = false; };
  }, [sessionId, nodeId, fieldPath, blobId]);

  const save = async () => {
    if (!sessionId || text === null || expectedSha256 === null || refusal !== null) return;
    setSaving(true);
    setError(null);
    try {
      const result = await savePromptFile(sessionId, blobId, text, expectedSha256, nodeId, fieldPath);
      if (useSessionStore.getState().activeSessionId !== sessionId) return;
      setSavedText(text);
      setExpectedSha256(result.content_sha256);
      // The save rewrote the blob, so the file manager's hash and size are stale.
      void useBlobStore.getState().loadBlobs(sessionId);
      try {
        const state = await fetchCompositionState(sessionId);
        if (useSessionStore.getState().activeSessionId === sessionId) {
          useSessionStore.setState({ compositionState: state });
        }
      } catch {
        setError("Saved and approved. Reload the session to see the updated prompt status.");
      }
    } catch (failure: unknown) {
      if (useSessionStore.getState().activeSessionId === sessionId) {
        const typed = failure as { detail?: unknown; error_type?: unknown };
        setError(typed.error_type === "prompt_file_node_multi_binding"
          ? MULTI_BINDING_REFUSAL
          : typed.error_type === "prompt_file_review_pending_inline"
            ? "Approve the review card first, then edit the file."
            : typed.error_type === "prompt_file_binding_changed"
              ? "This prompt file moved since you opened it. Reload the session."
          : typeof typed.detail === "string" ? typed.detail : "Could not save this prompt file. Reload and retry.");
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="graph-config-section">
      {!expanded && <p>Prompt file {filename ?? (filenameLoading ? "Loading file name…" : "File not in this session")} ({slotLabel})</p>}
      {expanded && <>
      <label htmlFor={`prompt-file-${blobId}-${fieldPath}`}>Edit prompt file {filename ?? (filenameLoading ? "Loading file name…" : "File not in this session")} ({slotLabel})</label>
      <textarea
        ref={editorRef}
        id={`prompt-file-${blobId}-${fieldPath}`}
        value={text ?? ""}
        onChange={(event) => setText(event.target.value)}
        disabled={text === null}
        rows={12}
        style={{ width: "100%", whiteSpace: "pre-wrap" }}
      />
      {refusal && <p id={`prompt-file-${blobId}-${fieldPath}-refusal`}>{refusal}</p>}
      {error && <p role="alert">{error}</p>}
      <Button
        type="button"
        onClick={save}
        disabled={refusal !== null || saving || isComposing || text === null || text === savedText || expectedSha256 === null}
        aria-describedby={refusal ? `prompt-file-${blobId}-${fieldPath}-refusal` : undefined}
      >
        Save and approve
      </Button>
      <Button type="button" variant="bare" onClick={() => { setText(savedText); setError(null); }} disabled={saving || savedText === null}>
        Cancel
      </Button>
      <Button type="button" variant="bare" onClick={() => setExpanded(false)}>Close editor</Button>
      </>}
    </section>
  );
}
