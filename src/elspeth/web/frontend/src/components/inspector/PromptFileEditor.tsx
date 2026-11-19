import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { fetchCompositionState, readPromptFileText, savePromptFile } from "@/api/client";
import { AlertBanner, Button } from "@/components/ui";
import { useBlobFilename } from "@/components/blobs/BlobFileName";
import { useBlobStore } from "@/stores/blobStore";
import { captureSessionPublicationGuard, useSessionStore } from "@/stores/sessionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { promptFileEditability, promptFileSlots } from "@/lib/blobBindings";
import { optionLabel } from "./OptionRows";

interface PromptFileEditorProps {
  nodeId: string;
  blobId: string;
  fieldPath: string;
  /** The query's reader-facing name for a `queries.<selector>.template` slot. */
  queryName?: string;
}

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
  // One editor per file-backed prompt. A step with several prompt files edits
  // and saves each on its own; promptFileEditability decides what is refused.
  return <>{bindings.map((binding) => <PromptFileEditor key={`${nodeId}:${binding.fieldPath}:${binding.blobId}`} nodeId={nodeId} {...binding} />)}</>;
}

export function PromptFileEditor({ nodeId, blobId, fieldPath, queryName }: PromptFileEditorProps) {
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
  // What the editor last has to say: a failure (announced as an alert) or a
  // confirmation or caution (announced politely). A save that worked is never
  // sent through the error channel.
  const [status, setStatus] = useState<{ tone: "error" | "success" | "warning"; text: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [expanded, setExpanded] = useState(false);
  // The seq of the last Edit request this editor took. It re-runs the focus
  // effect for a request that arrives while the editor is already open, where
  // neither `expanded` nor `text` changes and the click would do nothing.
  const [focusRequestSeq, setFocusRequestSeq] = useState(0);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const shouldFocusRef = useRef(false);
  const editability = compositionState?.nodes.some((node) => node.id === nodeId)
    ? promptFileEditability({ state: compositionState, blobs: blobSessionId === sessionId ? blobs : [],
      pendingEvents: Object.values(pending ?? {}), nodeId, fieldPath, blobId })
    : null;
  // Why the server would refuse a save right now. While set, the editor is
  // read-only: the text is shown, Save is disabled and this reason is given.
  const refusal = editability?.editable === false ? editability.reason : null;

  useLayoutEffect(() => {
    if (editRequest?.nodeId !== nodeId || editRequest.fieldPath !== fieldPath) return;
    shouldFocusRef.current = true;
    setExpanded(true);
    setFocusRequestSeq(editRequest.seq);
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
  }, [expanded, text, focusRequestSeq]);

  useEffect(() => {
    let live = true;
    setText(null);
    setSavedText(null);
    setExpectedSha256(null);
    setStatus(null);
    if (sessionId) {
      readPromptFileText(sessionId, blobId)
        .then(({ text: content, sha256 }) => {
          if (!live) return;
          setExpectedSha256(sha256);
          setSavedText(content);
          setText(content);
        })
        .catch((failure: unknown) => {
          if (live) setStatus({ tone: "error", text: failure instanceof Error ? failure.message : "Could not load this prompt file." });
        });
    }
    return () => { live = false; };
  }, [sessionId, nodeId, fieldPath, blobId]);

  const save = async () => {
    if (!sessionId || text === null || expectedSha256 === null || refusal !== null) return;
    setSaving(true);
    setStatus(null);
    // Bound to THIS activation, not only the session id: switching away and
    // back advances the publication generation, so a slow post-save refetch
    // cannot overwrite a newer graph published since.
    const isCurrent = captureSessionPublicationGuard(sessionId);
    try {
      const result = await savePromptFile(sessionId, blobId, text, expectedSha256, nodeId, fieldPath);
      if (!isCurrent()) return;
      setSavedText(text);
      setExpectedSha256(result.content_sha256);
      setStatus({ tone: "success", text: "Saved. This step now uses the new text." });
      // The save rewrote the blob, so the file manager's hash and size are stale.
      void useBlobStore.getState().loadBlobs(sessionId);
      try {
        const state = await fetchCompositionState(sessionId);
        if (isCurrent()) {
          useSessionStore.setState({ compositionState: state });
        }
      } catch {
        if (isCurrent()) setStatus({ tone: "warning", text: "Saved. Reload the session to see the updated prompt status." });
      }
    } catch (failure: unknown) {
      if (isCurrent()) {
        const typed = failure as { detail?: unknown; error_type?: unknown };
        setStatus({ tone: "error", text: typed.error_type === "prompt_file_review_pending"
          ? "Complete the existing node review before editing its files."
          : typed.error_type === "prompt_file_review_pending_inline"
            ? "Approve the review card first, then edit the file."
            : typed.error_type === "prompt_file_binding_changed"
              ? "This prompt file moved since you opened it. Reload the session."
          : typeof typed.detail === "string" ? typed.detail : "Could not save this prompt file. Reload and retry." });
      }
    } finally {
      setSaving(false);
    }
  };

  const fieldId = `prompt-file-${blobId}-${fieldPath}`;
  const refusalId = `${fieldId}-refusal`;
  const hintId = `${fieldId}-hint`;
  const shownName = filename ?? (filenameLoading ? "Loading file name…" : "File not in this session");
  const loading = text === null && status?.tone !== "error";
  const dirty = text !== null && text !== savedText;
  // Why Save is disabled when the server would not refuse: said in one line
  // under the buttons, so a greyed Save is never unexplained.
  const hint = refusal !== null || text === null
    ? (loading ? "Loading the file…" : null)
    : isComposing
      ? "Wait for the assistant to finish, then save."
      : !dirty
        ? (status?.tone === "success" ? null : "No changes to save.")
        : null;
  const close = () => {
    setExpanded(false);
    // The button that had focus is about to unmount; hand focus back to the
    // row's Edit button instead of letting it fall to the page.
    const slot = `${nodeId}:${fieldPath}`;
    const trigger = Array.from(document.querySelectorAll<HTMLElement>("[data-prompt-edit-slot]"))
      .find((element) => element.dataset.promptEditSlot === slot);
    trigger?.focus();
  };

  return (
    <section className="graph-config-section">
      {!expanded && <p className="prompt-editor-collapsed">Prompt file {shownName} ({slotLabel})</p>}
      {expanded && <>
      <div className="prompt-editor-heading">
        <label className="prompt-editor-label" htmlFor={fieldId}>Edit prompt file {shownName} ({slotLabel})</label>
        {refusal !== null && <span className="prompt-editor-tag">Read only</span>}
        {refusal === null && dirty && <span className="prompt-editor-tag">Unsaved changes</span>}
      </div>
      {refusal && <AlertBanner tone="warning" role="note" id={refusalId} className="prompt-editor-notice">{refusal}</AlertBanner>}
      <textarea
        ref={editorRef}
        id={fieldId}
        className="textarea input-mono prompt-editor-text"
        value={text ?? ""}
        onChange={(event) => { setText(event.target.value); if (status?.tone !== "error") setStatus(null); }}
        disabled={text === null}
        readOnly={refusal !== null}
        aria-busy={loading || undefined}
        placeholder={loading ? "Loading…" : undefined}
        aria-describedby={refusal ? refusalId : undefined}
        rows={12}
      />
      {status && <AlertBanner tone={status.tone} className="prompt-editor-notice">{status.text}</AlertBanner>}
      <div className="prompt-editor-actions">
        <Button
          type="button"
          variant="primary"
          compact
          onClick={save}
          disabled={refusal !== null || saving || isComposing || text === null || text === savedText || expectedSha256 === null}
          aria-describedby={refusal ? refusalId : hint ? hintId : undefined}
        >
          Save
        </Button>
        <Button type="button" variant="ghost" compact onClick={() => { setText(savedText); setStatus(null); }} disabled={saving || savedText === null}>
          Discard changes
        </Button>
        <Button type="button" variant="ghost" compact className="prompt-editor-close" onClick={close}>Close</Button>
      </div>
      {hint && <p id={hintId} className="prompt-editor-hint">{hint}</p>}
      </>}
    </section>
  );
}
