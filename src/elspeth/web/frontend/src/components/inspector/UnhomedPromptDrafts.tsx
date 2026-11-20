import { useId, useMemo, useRef, type JSX } from "react";
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
  const sectionRef = useRef<HTMLElement | null>(null);
  // Discarding removes the row whose button had focus. Focus moves to the
  // next row's Discard (else the previous one); after the last row the whole
  // notice goes, so focus moves to the next control after it (the Files
  // table's toggle), never to the page body.
  const discard = (draft: PromptDraft, button: HTMLElement) => {
    const section = sectionRef.current;
    const buttons = section === null ? [] : Array.from(section.querySelectorAll<HTMLElement>("[data-unhomed-discard]"));
    const index = buttons.indexOf(button);
    const target = buttons[index + 1] ?? buttons[index - 1] ?? nextFocusableAfter(section);
    usePromptDraftStore.getState().discard(draft);
    target?.focus();
  };
  if (state === null || unhomed.length === 0) return null;
  // The banner (a status, so a new arrival is announced) says what this is;
  // the list under it holds one row per draft.
  return (
    <section ref={sectionRef} className="prompt-drafts-unhomed" aria-labelledby={headingId}>
      <AlertBanner tone="warning" role="status">
        <strong id={headingId} className="prompt-drafts-unhomed-title">Unsaved prompt text with no home ({unhomed.length})</strong>
        {" "}The prompt it was written for no longer reads a file. Copy it to keep it, or discard it.
      </AlertBanner>
      <ul className="prompt-drafts-unhomed-list">
        {unhomed.map((draft) => <UnhomedDraft key={JSON.stringify([draft.nodeId, draft.fieldPath])} draft={draft} state={state} onDiscard={discard} />)}
      </ul>
    </section>
  );
}

const FOCUSABLE = "summary, button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex=\"0\"]";

/** The first focusable control after `element` in document order, outside it. */
function nextFocusableAfter(element: Element | null): HTMLElement | null {
  for (let from = element; from !== null && from !== document.body; from = from.parentElement) {
    for (let sibling = from.nextElementSibling; sibling !== null; sibling = sibling.nextElementSibling) {
      if (sibling instanceof HTMLElement && sibling.matches(FOCUSABLE)) return sibling;
      const inside = sibling.querySelector<HTMLElement>(FOCUSABLE);
      if (inside !== null) return inside;
    }
  }
  return null;
}

function UnhomedDraft({ draft, state, onDiscard }: {
  draft: PromptDraft;
  state: CompositionState;
  onDiscard: (draft: PromptDraft, button: HTMLElement) => void;
}): JSX.Element {
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
      {/* Read it before deciding, like "View current file" in the editor. */}
      <details className="prompt-drafts-unhomed-view">
        <summary aria-label={`View unsaved text for ${slot}`}>View text</summary>
        <pre className="prompt-drafts-unhomed-text">{draft.text}</pre>
      </details>
      <span className="prompt-drafts-unhomed-actions">
        <PromptCopyButton value={draft.text} label={`unsaved text for ${slot}`} />
        <Button type="button" variant="ghost" compact aria-label={`Discard unsaved text for ${slot}`}
          data-unhomed-discard="" onClick={(event) => onDiscard(draft, event.currentTarget)}>Discard</Button>
      </span>
    </li>
  );
}
