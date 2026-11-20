import { useId, useMemo, type JSX } from "react";
import { AlertBanner, Button } from "@/components/ui";
import { useBlobFilename } from "@/components/blobs/BlobFileName";
import { promptFileBindings, promptFileSlots } from "@/lib/blobBindings";
import { useSessionStore } from "@/stores/sessionStore";
import { usePromptDraftStore, type PromptDraft } from "@/stores/promptDraftStore";
import type { CompositionState } from "@/types";
import { PromptCopyButton } from "./PromptCopyButton";
import { promptSlotLabel } from "./OptionRows";

/**
 * Unsaved prompt text whose slot is gone: the step was removed, or the prompt
 * stopped reading a file (it became inline text). No editor can open such a
 * draft, so without this list it would sit unseen in the store, still holding
 * the page's leave warning. Each one can be copied out or discarded; nothing
 * here saves or sends it anywhere.
 */
export function UnhomedPromptDrafts(): JSX.Element | null {
  const sessionId = useSessionStore((store) => store.activeSessionId);
  const state = useSessionStore((store) => store.compositionState);
  const loaded = useSessionStore((store) => store.compositionStateLoaded);
  const drafts = usePromptDraftStore((store) => store.drafts);
  const unhomed = useMemo(() => {
    // Judge only against this session's loaded graph: a graph still loading
    // (or another session's) would make every draft look homeless.
    if (sessionId === null || state === null || !loaded || state.session_id !== sessionId) return [];
    const homes = new Set(promptFileBindings(state).map((binding) => JSON.stringify([binding.nodeId, binding.fieldPath])));
    return Object.values(drafts).filter((draft) => draft.sessionId === sessionId
      && !homes.has(JSON.stringify([draft.nodeId, draft.fieldPath])));
  }, [sessionId, state, loaded, drafts]);
  const headingId = useId();
  if (state === null || unhomed.length === 0) return null;
  // The banner (a status, so a new arrival is announced) says what this is;
  // the list under it holds one row per draft.
  return (
    <section className="prompt-drafts-unhomed" aria-labelledby={headingId}>
      <AlertBanner tone="warning" role="status">
        <strong id={headingId} className="prompt-drafts-unhomed-title">Unsaved prompt text with no home ({unhomed.length})</strong>
        {" "}The prompt it was written for no longer reads a file. Copy it to keep it, or discard it.
      </AlertBanner>
      <ul className="prompt-drafts-unhomed-list">
        {unhomed.map((draft) => <UnhomedDraft key={JSON.stringify([draft.nodeId, draft.fieldPath])} draft={draft} state={state} />)}
      </ul>
    </section>
  );
}

function UnhomedDraft({ draft, state }: { draft: PromptDraft; state: CompositionState }): JSX.Element {
  const filename = useBlobFilename(draft.blobId);
  const node = state.nodes.find((candidate) => candidate.id === draft.nodeId);
  // The query's own name while its step still exists; the slot path otherwise.
  const queryName = node === undefined ? undefined
    : promptFileSlots(node.options).find((slot) => slot.fieldPath === draft.fieldPath)?.queryName;
  const slot = `${draft.nodeId}, ${promptSlotLabel(draft.fieldPath, queryName)}`;
  const reason = node === undefined ? "Step removed" : "Now inline text, not a file";
  return (
    <li className="prompt-drafts-unhomed-item">
      <span className="prompt-drafts-unhomed-name">
        <strong>{slot}</strong>
        <span className="prompt-drafts-unhomed-meta">{reason}{filename !== null && ` · written for ${filename}`}</span>
      </span>
      <span className="prompt-drafts-unhomed-actions">
        <PromptCopyButton value={draft.text} label={`unsaved text for ${slot}`} />
        <Button type="button" variant="ghost" compact aria-label={`Discard unsaved text for ${slot}`}
          onClick={() => usePromptDraftStore.getState().discard(draft)}>Discard</Button>
      </span>
    </li>
  );
}
