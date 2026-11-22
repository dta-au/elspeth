// src/stores/promptDraftStore.ts
import { create } from "zustand";

/**
 * Unsaved prompt-file text, held outside the editor component so that
 * unmounting the editor (selecting another step, clicking the canvas, Show
 * node, Edit file, a Composer run that re-points the slot) never drops it.
 *
 * In memory only: prompt text is never written to browser storage. A draft is
 * keyed by session + step + prompt slot, and records the file it was written
 * against (`blobId` + `baseSha256`), so an editor that remounts on a different
 * file, or on a newer version of the same file, can say so instead of either
 * dropping the text or saving it over content the user never saw.
 *
 * A draft leaves the store only through `settleSaved` (the save the user made
 * succeeded with exactly this text), `discard` (the user chose Discard), or
 * `reset` (sign-out, which clears every per-principal cache).
 */
export interface PromptDraft {
  sessionId: string;
  nodeId: string;
  fieldPath: string;
  /** The file the user was editing when the draft began (or was last re-based). */
  blobId: string;
  /** The file content hash that text was based on; Save sends it as the expected hash. */
  baseSha256: string;
  /** The file text the draft started from; a draft equal to it is not kept. */
  baseText: string;
  text: string;
}

export interface PromptDraftSlot {
  sessionId: string;
  nodeId: string;
  fieldPath: string;
}

interface PromptDraftState {
  drafts: Readonly<Record<string, PromptDraft>>;
  /** Slots whose editor is open right now, so a row does not repeat the editor's own tag. */
  openEditors: Readonly<Record<string, true>>;
  /**
   * Slots with a save in flight. While one is pending, a draft typed back to
   * its starting text is kept: the save is about to replace that text, so
   * the reversion is itself an unsaved change.
   */
  savesInFlight: Readonly<Record<string, true>>;
  setEditorOpen: (slot: PromptDraftSlot, open: boolean) => void;
  beginSave: (slot: PromptDraftSlot) => void;
  /** A save failed: the file still holds the starting text, so a draft equal to it holds nothing. */
  saveFailed: (slot: PromptDraftSlot) => void;
  /** Record an edit. A new draft takes `base`; an existing one keeps its own base. */
  edit: (slot: PromptDraftSlot, base: { blobId: string; baseSha256: string; baseText: string }, text: string) => void;
  /** Point a draft at the current file version: the user's explicit choice to save it there. */
  rebase: (slot: PromptDraftSlot, base: { blobId: string; baseSha256: string; baseText: string }) => void;
  discard: (slot: PromptDraftSlot) => void;
  /** A save of `savedText` succeeded: drop the draft if that is all it held, else move its base forward. */
  settleSaved: (slot: PromptDraftSlot, savedText: string, sha256: string) => void;
  reset: () => void;
}

export function promptDraftKey({ sessionId, nodeId, fieldPath }: PromptDraftSlot): string {
  return JSON.stringify([sessionId, nodeId, fieldPath]);
}

function without(drafts: Readonly<Record<string, PromptDraft>>, key: string): Record<string, PromptDraft> {
  const next = { ...drafts };
  delete next[key];
  return next;
}

export const usePromptDraftStore = create<PromptDraftState>((set) => ({
  drafts: {},
  openEditors: {},
  savesInFlight: {},
  beginSave(slot) {
    set((state) => ({ savesInFlight: { ...state.savesInFlight, [promptDraftKey(slot)]: true } }));
  },
  saveFailed(slot) {
    set((state) => {
      const key = promptDraftKey(slot);
      const savesInFlight: Record<string, true> = { ...state.savesInFlight };
      delete savesInFlight[key];
      const current = state.drafts[key];
      if (current && current.text === current.baseText) return { savesInFlight, drafts: without(state.drafts, key) };
      return { savesInFlight };
    });
  },
  setEditorOpen(slot, open) {
    set((state) => {
      const key = promptDraftKey(slot);
      if (open === (key in state.openEditors)) return state;
      const next: Record<string, true> = { ...state.openEditors };
      if (open) next[key] = true;
      else delete next[key];
      return { openEditors: next };
    });
  },
  edit(slot, base, text) {
    set((state) => {
      const key = promptDraftKey(slot);
      const current = state.drafts[key];
      const draft: PromptDraft = current ? { ...current, text } : { ...slot, ...base, text };
      // Typing back to the exact text the draft started from leaves nothing
      // unsaved, unless a save in flight is about to replace that text.
      if (draft.text === draft.baseText && !(key in state.savesInFlight)) {
        return current ? { drafts: without(state.drafts, key) } : state;
      }
      return { drafts: { ...state.drafts, [key]: draft } };
    });
  },
  rebase(slot, base) {
    set((state) => {
      const key = promptDraftKey(slot);
      const current = state.drafts[key];
      if (!current) return state;
      return { drafts: { ...state.drafts, [key]: { ...current, ...base } } };
    });
  },
  discard(slot) {
    set((state) => ({ drafts: without(state.drafts, promptDraftKey(slot)) }));
  },
  settleSaved(slot, savedText, sha256) {
    set((state) => {
      const key = promptDraftKey(slot);
      const savesInFlight: Record<string, true> = { ...state.savesInFlight };
      delete savesInFlight[key];
      const current = state.drafts[key];
      if (!current) return { savesInFlight };
      if (current.text === savedText) return { savesInFlight, drafts: without(state.drafts, key) };
      // Typed on (or back to the old text) while the save was in flight: keep
      // the newer text on the saved base.
      return { savesInFlight, drafts: { ...state.drafts, [key]: { ...current, baseText: savedText, baseSha256: sha256 } } };
    });
  },
  reset() {
    set({ drafts: {}, openEditors: {}, savesInFlight: {} });
  },
}));

/** The draft for one slot of the active session, if any. */
export function usePromptDraft(slot: PromptDraftSlot | null): PromptDraft | undefined {
  return usePromptDraftStore((state) => slot === null ? undefined : state.drafts[promptDraftKey(slot)]);
}

/** Whether this slot holds unsaved text while its editor is closed (the open editor shows its own tag). */
export function usePromptDraftHiddenUnsaved(slot: PromptDraftSlot): boolean {
  return usePromptDraftStore((state) => {
    const key = promptDraftKey(slot);
    return key in state.drafts && !(key in state.openEditors);
  });
}

/** Whether a step (optionally one slot of it) in this session holds unsaved prompt text. */
export function hasPromptDraft(drafts: Readonly<Record<string, PromptDraft>>, sessionId: string | null, nodeId: string, fieldPath?: string): boolean {
  if (sessionId === null) return false;
  return Object.values(drafts).some((draft) => draft.sessionId === sessionId && draft.nodeId === nodeId
    && (fieldPath === undefined || draft.fieldPath === fieldPath));
}

/** Warn before the page unloads while any draft is unsaved. */
export function promptDraftUnloadGuard(event: BeforeUnloadEvent): void {
  if (Object.keys(usePromptDraftStore.getState().drafts).length === 0) return;
  event.preventDefault();
  // Older browsers show the prompt only when returnValue is set.
  event.returnValue = "";
}

if (typeof window !== "undefined") {
  window.addEventListener("beforeunload", promptDraftUnloadGuard);
}
