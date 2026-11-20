import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { fetchCompositionState, readPromptFileText, savePromptFile } from "@/api/client";
import { AlertBanner, Button } from "@/components/ui";
import { useBlobFilename } from "@/components/blobs/BlobFileName";
import { useBlobStore } from "@/stores/blobStore";
import { captureSessionPublicationGuard, useSessionStore } from "@/stores/sessionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { promptFileEditability } from "@/lib/blobBindings";
import { usePromptDraft, usePromptDraftStore } from "@/stores/promptDraftStore";
import { optionLabel } from "./OptionRows";
import { PromptCopyButton } from "./PromptCopyButton";

/** Under any refused save whose own wording does not already say so: the
 *  draft lives in memory only, so a reload the refusal may ask for loses it. */
export const REFUSED_SAVE_KEPT_LINE = "Your text is still here and not saved. Copy it before you reload the page.";

interface EditorStatus {
  tone: "error" | "success" | "warning";
  text: string;
  /** The save landed but the graph refetch after it failed; offer it again. */
  retryRefresh?: true;
  /** The save was refused; the unsaved text stays in the editor. */
  refused?: true;
  /** Add REFUSED_SAVE_KEPT_LINE (the text does not already say it is kept). */
  keptLine?: true;
  /** Offer "Refresh pipeline": the graph read, never a save or page reload. */
  refreshPipeline?: true;
}

interface PromptFileEditorProps {
  nodeId: string;
  blobId: string;
  fieldPath: string;
  /** The query's reader-facing name for a `queries.<selector>.template` slot. */
  queryName?: string;
}

/**
 * The editor for ONE file-backed prompt slot. OptionRows mounts one per slot,
 * inside that prompt's own row (`slotActions.Editor`), so a step with several
 * prompt files edits and saves each on its own; promptFileEditability decides
 * what is refused. Until Edit is pressed it renders nothing, unless the slot
 * holds unsaved text, which reopens it.
 *
 * Typed text lives in the prompt draft store, not in this component, so it
 * survives the editor unmounting. A draft written against another file or
 * an older version of this one is shown as such and is never saved silently.
 */
export function PromptFileEditor({ nodeId, blobId, fieldPath, queryName }: PromptFileEditorProps) {
  const filename = useBlobFilename(blobId);
  // The same query label the inspector row shows (promptQueryEntries `name`),
  // never the list index the slot path uses.
  const slotLabel = queryName === undefined ? optionLabel(fieldPath) : `User prompt, query ${queryName}`;
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const filenameLoading = useBlobStore((state) => state.activeSessionId !== sessionId || state.isLoading);
  const blobSessionId = useBlobStore((state) => state.activeSessionId);
  const blobs = useBlobStore((state) => state.blobs);
  // The file's hash as the file list last reported it. A change that is not
  // this editor's own save means the file was rewritten elsewhere (Composer,
  // another tab), so the editor re-reads it instead of saving against an old base.
  const listedHash = useBlobStore((state) => state.activeSessionId !== sessionId ? null
    : state.blobs.find((candidate) => candidate.id.toLowerCase() === blobId.toLowerCase())?.content_hash ?? null);
  const editRequest = useBlobStore((state) => state.editRequest);
  const compositionState = useSessionStore((state) => state.compositionState);
  const pending = useInterpretationEventsStore((state) => sessionId ? state.pendingBySession[sessionId] : undefined);
  const isComposing = useSessionStore((state) => state.isComposing);
  const slot = sessionId === null ? null : { sessionId, nodeId, fieldPath };
  const draft = usePromptDraft(slot);
  const draftFilename = useBlobFilename(draft?.blobId ?? blobId);
  const [savedText, setSavedText] = useState<string | null>(null);
  const [expectedSha256, setExpectedSha256] = useState<string | null>(null);
  // What the editor last has to say: a failure (announced as an alert) or a
  // confirmation or caution (announced politely). A save that worked is never
  // sent through the error channel.
  const [status, setStatus] = useState<EditorStatus | null>(null);
  const [saving, setSaving] = useState(false);
  // A slot with unsaved text opens with it showing, wherever the editor remounts.
  const [expanded, setExpanded] = useState(() => draft !== undefined);
  // The seq of the last Edit request this editor took. It re-runs the focus
  // effect for a request that arrives while the editor is already open, where
  // neither `expanded` nor `text` changes and the click would do nothing.
  const [focusRequestSeq, setFocusRequestSeq] = useState(0);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  // Which file the current read belongs to; a read that resolves after the
  // slot moved to another file (or the editor unmounted) is dropped.
  const readKeyRef = useRef<string | null>(null);
  const seenListedHashRef = useRef(listedHash);
  // The Edit button that opened the editor, so Close can hand focus back to
  // the row the user came from (several query rows can share one slot).
  const triggerRef = useRef<HTMLElement | null>(null);
  const shouldFocusRef = useRef(false);
  const editability = compositionState?.nodes.some((node) => node.id === nodeId)
    ? promptFileEditability({ state: compositionState, blobs: blobSessionId === sessionId ? blobs : [],
      pendingEvents: Object.values(pending ?? {}), nodeId, fieldPath, blobId })
    : null;
  // Why the server would refuse a save right now. While set, the editor is
  // read-only: the text is shown, Save is disabled and this reason is given.
  const refusal = editability?.editable === false ? editability.reason : null;

  // The draft no longer matches the file this slot reads: re-pointed to another
  // file, or the file changed underneath. Its text is kept and shown, and Save
  // waits until the user chooses to put it on the current file (or discards it).
  const stale = draft !== undefined && (draft.blobId.toLowerCase() !== blobId.toLowerCase()
    || (expectedSha256 !== null && draft.baseSha256 !== expectedSha256));
  const text = draft?.text ?? savedText;

  useLayoutEffect(() => {
    if (editRequest?.nodeId !== nodeId || editRequest.fieldPath !== fieldPath) return;
    shouldFocusRef.current = true;
    const active = document.activeElement;
    triggerRef.current = active instanceof HTMLElement && active.dataset.promptEditSlot === `${nodeId}:${fieldPath}` ? active : null;
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

  // Tell the prompt row this slot's editor is showing, so it does not repeat the tag.
  useEffect(() => {
    if (!sessionId || !expanded) return;
    const open = { sessionId, nodeId, fieldPath };
    usePromptDraftStore.getState().setEditorOpen(open, true);
    return () => usePromptDraftStore.getState().setEditorOpen(open, false);
  }, [sessionId, nodeId, fieldPath, expanded]);

  useEffect(() => {
    const key = JSON.stringify([sessionId, nodeId, fieldPath, blobId]);
    readKeyRef.current = key;
    setSavedText(null);
    setExpectedSha256(null);
    setStatus(null);
    if (sessionId) {
      readPromptFileText(sessionId, blobId)
        .then(({ text: content, sha256 }) => {
          if (readKeyRef.current !== key) return;
          setExpectedSha256(sha256);
          setSavedText(content);
        })
        .catch((failure: unknown) => {
          if (readKeyRef.current === key) setStatus({ tone: "error", text: failure instanceof Error ? failure.message : "Could not load this prompt file." });
        });
    }
    return () => { readKeyRef.current = null; };
  }, [sessionId, nodeId, fieldPath, blobId]);

  // Read the current file again, keeping what is on screen until it arrives.
  // Unsaved text stays in the draft store, so a newer file turns it stale (the
  // banner below says so) and a clean editor simply shows the new text.
  // Resolves to the file's hash, or null when the editor moved on meanwhile.
  const reread = async (): Promise<string | null> => {
    const key = readKeyRef.current;
    if (!sessionId || key === null) return null;
    const { text: content, sha256 } = await readPromptFileText(sessionId, blobId);
    if (readKeyRef.current !== key) return null;
    setExpectedSha256(sha256);
    setSavedText(content);
    return sha256;
  };
  const rereadRef = useRef(reread);
  rereadRef.current = reread;

  // The file list reported a new hash that is not the version on screen.
  useEffect(() => {
    const previous = seenListedHashRef.current;
    seenListedHashRef.current = listedHash;
    if (listedHash === null || previous === null || listedHash === previous) return;
    if (expectedSha256 === null || listedHash === expectedSha256) return;
    void rereadRef.current().catch(() => undefined);
  }, [listedHash, expectedSha256]);

  // Re-read the graph after a save so the step's prompt status follows. A
  // failure here never asks for a reload: other prompt drafts in this session
  // may be unsaved, and a reload would lose them.
  const refreshGraphAfterSave = async (
    activeSessionId: string,
    isCurrent: () => boolean,
  ): Promise<void> => {
    try {
      const state = await fetchCompositionState(activeSessionId);
      if (!isCurrent()) return;
      useSessionStore.setState({ compositionState: state });
      setStatus({ tone: "success", text: "Saved. This step now uses the new text." });
    } catch {
      if (isCurrent()) {
        setStatus({
          tone: "warning",
          text: "Saved. The prompt status will update when the pipeline next refreshes.",
          retryRefresh: true,
        });
      }
    }
  };

  const retryRefresh = () => {
    if (!sessionId) return;
    void refreshGraphAfterSave(sessionId, captureSessionPublicationGuard(sessionId));
  };

  // After "the prompt no longer reads this file the way it did": read the
  // graph again (drafts are in memory, so they survive), then let the stale
  // banner say where the text stands. Never a save, never a page reload.
  const refreshPipeline = async () => {
    if (!sessionId) return;
    const isCurrent = captureSessionPublicationGuard(sessionId);
    try {
      const state = await fetchCompositionState(sessionId);
      if (!isCurrent()) return;
      useSessionStore.setState({ compositionState: state });
      setStatus(null);
    } catch {
      if (isCurrent()) {
        setStatus({ tone: "error", refused: true, refreshPipeline: true,
          text: "Could not refresh the pipeline. Your text is kept. Try Refresh pipeline again." });
      }
    }
  };

  const save = async () => {
    if (!slot || !sessionId || !draft || stale || refusal !== null) return;
    const sent = draft.text;
    setSaving(true);
    setStatus(null);
    // Bound to THIS activation, not only the session id: switching away and
    // back advances the publication generation, so a slow post-save refetch
    // cannot overwrite a newer graph published since.
    const isCurrent = captureSessionPublicationGuard(sessionId);
    try {
      // The draft's own base hash, never the latest one: the server refuses
      // the save if the file changed since the text was written.
      const result = await savePromptFile(sessionId, blobId, sent, draft.baseSha256, nodeId, fieldPath);
      usePromptDraftStore.getState().settleSaved(slot, sent, result.content_sha256);
      if (!isCurrent()) return;
      setSavedText(sent);
      setExpectedSha256(result.content_sha256);
      setStatus({ tone: "success", text: "Saved. This step now uses the new text." });
      // The save rewrote the blob, so the file manager's hash and size are stale.
      void useBlobStore.getState().loadBlobs(sessionId);
      await refreshGraphAfterSave(sessionId, isCurrent);
    } catch (failure: unknown) {
      if (isCurrent()) {
        const typed = failure as { detail?: unknown; error_type?: unknown };
        if (typed.error_type === "prompt_file_stale") {
          // The server refuses a save whose base is not the file's current
          // version. Read the current version: if it is newer, the draft turns
          // stale and the banner offers the way out, so no second message.
          // If the file is unchanged, the refusal was about the step's review,
          // and the server's own wording says what to do.
          let current: string | null = null;
          try {
            current = await reread();
          } catch {
            setStatus({ tone: "error", refused: true, keptLine: true,
              text: "Could not read the current version of this file. Try saving again." });
            return;
          }
          if (current === null || current !== draft.baseSha256) return;
        }
        if (typed.error_type === "prompt_file_binding_changed") {
          setStatus({ tone: "error", refused: true, refreshPipeline: true,
            text: "The pipeline changed and this prompt no longer reads this file the way it did when you opened it. "
              + "Your text is kept. Refresh the pipeline to see where the prompt points now." });
        } else if (typed.error_type === "prompt_file_review_pending" || typed.error_type === "prompt_file_review_pending_inline"
          || typeof typed.detail === "string") {
          // Server wording is shown as sent; some of it asks for a page
          // reload, so the kept-text line follows it.
          setStatus({ tone: "error", refused: true, keptLine: true, text: typed.error_type === "prompt_file_review_pending"
            ? "Complete the existing node review before editing its files."
            : typed.error_type === "prompt_file_review_pending_inline"
              ? "Approve the review card first, then edit the file."
              : typed.detail as string });
        } else {
          setStatus({ tone: "error", refused: true,
            text: "Could not save this prompt file. Your text is still here. Try Save again; "
              + "if it keeps failing, copy your text before you leave this page." });
        }
      }
    } finally {
      setSaving(false);
    }
  };

  const fieldId = `prompt-file-${blobId}-${fieldPath}`;
  // Re-pointed (or changed) onto a file this editor cannot save: "Put my
  // text on the current file" would lead to a Save that never enables.
  const staleReadOnly = stale && refusal !== null;
  // Copy sits in the button row whenever unsaved text cannot be saved now.
  const offerCopy = draft !== undefined && (status?.refused === true || staleReadOnly);
  const refusalId = `${fieldId}-refusal`;
  const hintId = `${fieldId}-hint`;
  const staleId = `${fieldId}-stale`;
  const shownName = filename ?? (filenameLoading ? "Loading file name…" : "File not in this session");
  const loading = text === null && status?.tone !== "error";
  const dirty = draft !== undefined;
  // Why Save is disabled when the server would not refuse: said in one line
  // under the buttons, so a greyed Save is never unexplained.
  const hint = refusal !== null || text === null
    ? (loading ? "Loading the file…" : null)
    : stale
      ? "Choose what to do with your unsaved text above, then save."
      : isComposing
        ? "Wait for Composer to finish, then save."
        : !dirty
          ? (status?.tone === "success" ? null : "No changes to save.")
          : null;
  const currentBase = savedText !== null && expectedSha256 !== null ? { blobId, baseSha256: expectedSha256, baseText: savedText } : null;
  const onEdit = (value: string) => {
    if (!slot) return;
    if (draft) {
      usePromptDraftStore.getState().edit(slot, { blobId: draft.blobId, baseSha256: draft.baseSha256, baseText: draft.baseText }, value);
    } else if (currentBase) {
      usePromptDraftStore.getState().edit(slot, currentBase, value);
    }
    if (status?.tone !== "error") setStatus(null);
  };
  const discard = () => {
    if (slot) usePromptDraftStore.getState().discard(slot);
    setStatus(null);
  };
  const close = () => {
    setExpanded(false);
    // The button that had focus is about to unmount; hand focus back to the
    // Edit button that opened the editor (or, when it was opened from the file
    // list, the slot's first Edit button) instead of letting it fall to the page.
    const slot = `${nodeId}:${fieldPath}`;
    const trigger = triggerRef.current?.isConnected
      ? triggerRef.current
      : Array.from(document.querySelectorAll<HTMLElement>("[data-prompt-edit-slot]"))
        .find((element) => element.dataset.promptEditSlot === slot);
    trigger?.focus();
  };

  if (!expanded) return null;
  return (
    <section className="graph-config-section prompt-editor">
      <div className="prompt-editor-heading">
        <label className="prompt-editor-label" htmlFor={fieldId}>Edit prompt file {shownName} ({slotLabel})</label>
        {refusal !== null && <span className="prompt-editor-tag">Read only</span>}
        {dirty && <span className="prompt-editor-tag">Unsaved changes</span>}
      </div>
      {refusal && <AlertBanner tone="warning" role="note" id={refusalId} className="prompt-editor-notice">{refusal}</AlertBanner>}
      {stale && draft && (
        // A status, so the banner is announced when the file changes under an
        // open editor, not only found later.
        <AlertBanner tone="warning" role="status" id={staleId} className="prompt-editor-notice">
          {draft.blobId.toLowerCase() !== blobId.toLowerCase()
            ? `This prompt now reads a different file. Your unsaved text from ${draftFilename ?? "the earlier file"} is kept below.`
            : "This file changed since you started editing. Your unsaved text is kept below."}
          {staleReadOnly
            ? " This file cannot be edited here, so the text cannot be saved to it. Copy it to keep it, or discard it."
            : " Put it on the current file to save it there, or discard it to load the current file."}
          {!staleReadOnly && (
            <span className="prompt-editor-stale-actions">
              <Button type="button" variant="ghost" compact disabled={currentBase === null}
                onClick={() => {
                  if (!slot || !currentBase) return;
                  usePromptDraftStore.getState().rebase(slot, currentBase);
                  // The banner and this button go away; the text the user kept is next.
                  editorRef.current?.focus();
                }}>
                Put my text on the current file
              </Button>
            </span>
          )}
          {savedText !== null && (
            // What Save would replace, before the user chooses to replace it.
            <details className="prompt-editor-current">
              <summary>View current file</summary>
              <pre className="prompt-editor-current-text">{savedText}</pre>
            </details>
          )}
        </AlertBanner>
      )}
      <textarea
        ref={editorRef}
        id={fieldId}
        className="textarea input-mono prompt-editor-text"
        value={text ?? ""}
        onChange={(event) => onEdit(event.target.value)}
        disabled={text === null}
        readOnly={refusal !== null}
        aria-busy={loading || undefined}
        placeholder={loading ? "Loading…" : undefined}
        aria-describedby={[refusal ? refusalId : null, stale ? staleId : null].filter((id) => id !== null).join(" ") || undefined}
        rows={12}
      />
      {status && (
        <AlertBanner
          tone={status.tone}
          className="prompt-editor-notice"
          action={status.retryRefresh ? (
            // The same graph read the save made, issued again; nothing is resaved.
            <Button type="button" variant="ghost" compact onClick={retryRefresh}>Retry</Button>
          ) : status.refreshPipeline ? (
            <Button type="button" variant="ghost" compact onClick={() => void refreshPipeline()}>Refresh pipeline</Button>
          ) : null}
        >
          {status.text}
          {status.keptLine && <span className="prompt-editor-kept">{REFUSED_SAVE_KEPT_LINE}</span>}
        </AlertBanner>
      )}
      <div className="prompt-editor-actions">
        <Button
          type="button"
          variant="primary"
          compact
          onClick={save}
          disabled={refusal !== null || saving || isComposing || !dirty || stale || expectedSha256 === null}
          aria-describedby={refusal ? refusalId : hint ? hintId : undefined}
        >
          Save
        </Button>
        {offerCopy && draft && <PromptCopyButton value={draft.text} label="your unsaved text" showLabel />}
        <Button type="button" variant="ghost" compact onClick={discard} disabled={saving || (savedText === null && !dirty)}>
          Discard changes
        </Button>
        <Button type="button" variant="ghost" compact className="prompt-editor-close" onClick={close}>Close</Button>
      </div>
      {hint && <p id={hintId} className="prompt-editor-hint">{hint}</p>}
    </section>
  );
}
