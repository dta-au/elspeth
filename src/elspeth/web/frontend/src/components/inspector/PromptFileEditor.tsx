import { useEffect, useState } from "react";
import { fetchCompositionState, readPromptFileText, savePromptFile } from "@/api/client";
import { Button } from "@/components/ui";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";

interface PromptFileEditorProps {
  nodeId: string;
  blobId: string;
  fieldPath: string;
  /** Why the server will refuse every save for this binding; Save stays disabled while set. */
  saveRefusal?: string | null;
}

const MULTI_BINDING_REFUSAL = "This node uses more than one prompt file. Re-point it through the agent and approve the review card.";

function blobId(value: unknown): string | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const marker = value as Record<string, unknown>;
  return marker.mode === "inline_content" && typeof marker.blob_ref === "string" ? marker.blob_ref : null;
}

export function PromptFileEditors({ nodeId, options }: { nodeId: string; options: Record<string, unknown> }) {
  const bindings: Array<{ fieldPath: string; blobId: string }> = [];
  for (const fieldPath of ["prompt_template", "system_prompt"]) {
    const id = blobId(options[fieldPath]);
    if (id) bindings.push({ fieldPath, blobId: id });
  }
  const queries = options.queries;
  const entries = Array.isArray(queries)
    ? queries.map((query, index) => [String(index), query] as const)
    : typeof queries === "object" && queries !== null
      ? Object.entries(queries)
      : [];
  for (const [name, query] of entries) {
    if (typeof query !== "object" || query === null || Array.isArray(query)) continue;
    const id = blobId((query as Record<string, unknown>).template);
    if (id) bindings.push({ fieldPath: `queries.${name}.template`, blobId: id });
  }
  // Mirrors the server rule (prompt_file_binding_paths > 1 refuses every save
  // with prompt_file_node_multi_binding), so the user is told before saving.
  const saveRefusal = bindings.length > 1 ? MULTI_BINDING_REFUSAL : null;
  return <>{bindings.map((binding) => <PromptFileEditor key={`${nodeId}:${binding.fieldPath}:${binding.blobId}`} nodeId={nodeId} saveRefusal={saveRefusal} {...binding} />)}</>;
}

export function PromptFileEditor({ nodeId, blobId, fieldPath, saveRefusal = null }: PromptFileEditorProps) {
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const isComposing = useSessionStore((state) => state.isComposing);
  const [text, setText] = useState<string | null>(null);
  const [savedText, setSavedText] = useState<string | null>(null);
  const [expectedSha256, setExpectedSha256] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

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
    if (!sessionId || text === null || expectedSha256 === null) return;
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
      <label htmlFor={`prompt-file-${blobId}-${fieldPath}`}>Edit bound prompt file ({fieldPath})</label>
      <textarea
        id={`prompt-file-${blobId}-${fieldPath}`}
        value={text ?? ""}
        onChange={(event) => setText(event.target.value)}
        disabled={text === null}
        rows={12}
        style={{ width: "100%", whiteSpace: "pre-wrap" }}
      />
      {saveRefusal && <p id={`prompt-file-${blobId}-${fieldPath}-refusal`}>{saveRefusal}</p>}
      {error && <p role="alert">{error}</p>}
      <Button
        type="button"
        onClick={save}
        disabled={saveRefusal !== null || saving || isComposing || text === null || text === savedText || expectedSha256 === null}
        aria-describedby={saveRefusal ? `prompt-file-${blobId}-${fieldPath}-refusal` : undefined}
      >
        Save and approve
      </Button>
      <Button type="button" variant="bare" onClick={() => { setText(savedText); setError(null); }} disabled={saving || savedText === null}>
        Cancel
      </Button>
    </section>
  );
}
