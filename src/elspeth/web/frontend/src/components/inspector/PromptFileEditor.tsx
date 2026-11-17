import { useEffect, useState } from "react";
import { fetchCompositionState, readPromptFileText, savePromptFile } from "@/api/client";
import { Button } from "@/components/ui";
import { useSessionStore } from "@/stores/sessionStore";

interface PromptFileEditorProps {
  blobId: string;
  fieldPath: string;
}

function blobId(value: unknown): string | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const marker = value as Record<string, unknown>;
  return marker.mode === "inline_content" && typeof marker.blob_ref === "string" ? marker.blob_ref : null;
}

export function PromptFileEditors({ options }: { options: Record<string, unknown> }) {
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
  return <>{bindings.map((binding) => <PromptFileEditor key={`${binding.fieldPath}:${binding.blobId}`} {...binding} />)}</>;
}

export function PromptFileEditor({ blobId, fieldPath }: PromptFileEditorProps) {
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
  }, [sessionId, blobId]);

  const save = async () => {
    if (!sessionId || text === null || expectedSha256 === null) return;
    setSaving(true);
    setError(null);
    try {
      const result = await savePromptFile(sessionId, blobId, text, expectedSha256);
      if (useSessionStore.getState().activeSessionId !== sessionId) return;
      setSavedText(text);
      setExpectedSha256(result.content_sha256);
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
        const typed = failure as { detail?: unknown };
        setError(typeof typed.detail === "string" ? typed.detail : "Could not save this prompt file. Reload and retry.");
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
      {error && <p role="alert">{error}</p>}
      <Button type="button" onClick={save} disabled={saving || isComposing || text === null || text === savedText || expectedSha256 === null}>
        Save and approve
      </Button>
      <Button type="button" variant="bare" onClick={() => { setText(savedText); setError(null); }} disabled={saving || savedText === null}>
        Cancel
      </Button>
    </section>
  );
}
