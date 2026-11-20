import { afterEach, describe, expect, it } from "vitest";
import { hasPromptDraft, promptDraftKey, promptDraftUnloadGuard, usePromptDraftStore } from "./promptDraftStore";

const slot = { sessionId: "session-1", nodeId: "score", fieldPath: "prompt_template" };
const base = { blobId: "file-a", baseSha256: "a".repeat(64), baseText: "original" };

afterEach(() => usePromptDraftStore.getState().reset());

function unloadEvent(): BeforeUnloadEvent {
  return new Event("beforeunload", { cancelable: true }) as BeforeUnloadEvent;
}

describe("promptDraftStore", () => {
  it("keeps an edit with the base it started from, and keeps that base on later edits", () => {
    usePromptDraftStore.getState().edit(slot, base, "first change");
    usePromptDraftStore.getState().edit(slot, { blobId: "file-b", baseSha256: "b".repeat(64), baseText: "other" }, "second change");
    expect(usePromptDraftStore.getState().drafts[promptDraftKey(slot)]).toEqual({ ...slot, ...base, text: "second change" });
  });

  it("drops a draft typed back to its starting text", () => {
    usePromptDraftStore.getState().edit(slot, base, "changed");
    usePromptDraftStore.getState().edit(slot, base, "original");
    expect(usePromptDraftStore.getState().drafts).toEqual({});
  });

  it("clears only on a save of exactly the draft's text, or on Discard", () => {
    const store = usePromptDraftStore.getState();
    store.edit(slot, base, "typed");
    store.settleSaved(slot, "typed earlier", "c".repeat(64));
    // Typed on during the save: the newer text stays, on the saved base.
    expect(usePromptDraftStore.getState().drafts[promptDraftKey(slot)]).toMatchObject({ text: "typed", baseText: "typed earlier", baseSha256: "c".repeat(64) });
    store.settleSaved(slot, "typed", "d".repeat(64));
    expect(usePromptDraftStore.getState().drafts).toEqual({});
    store.edit(slot, base, "again");
    store.discard(slot);
    expect(usePromptDraftStore.getState().drafts).toEqual({});
  });

  it("re-bases a draft only when the user asks", () => {
    const store = usePromptDraftStore.getState();
    store.edit(slot, base, "mine");
    const current = { blobId: "file-b", baseSha256: "b".repeat(64), baseText: "theirs" };
    store.rebase(slot, current);
    expect(usePromptDraftStore.getState().drafts[promptDraftKey(slot)]).toEqual({ ...slot, ...current, text: "mine" });
  });

  it("keeps sessions apart", () => {
    usePromptDraftStore.getState().edit(slot, base, "session one text");
    const { drafts } = usePromptDraftStore.getState();
    expect(hasPromptDraft(drafts, "session-1", "score")).toBe(true);
    expect(hasPromptDraft(drafts, "session-2", "score")).toBe(false);
    expect(drafts[promptDraftKey({ ...slot, sessionId: "session-2" })]).toBeUndefined();
  });

  it("asks before the page unloads only while a draft is unsaved", () => {
    const clean = unloadEvent();
    promptDraftUnloadGuard(clean);
    expect(clean.defaultPrevented).toBe(false);
    usePromptDraftStore.getState().edit(slot, base, "unsaved");
    const dirty = unloadEvent();
    window.dispatchEvent(dirty);
    expect(dirty.defaultPrevented).toBe(true);
  });

  it("never writes prompt text to browser storage", () => {
    localStorage.clear();
    sessionStorage.clear();
    usePromptDraftStore.getState().edit(slot, base, "secret prompt text");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
});
