import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionStore } from "@/stores/sessionStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { resetStore } from "@/test/store-helpers";
import * as api from "@/api/client";
import { departTutorialSession } from "./tutorialDeparture";

vi.mock("@/api/client", () => ({ updateUserComposerPreferences: vi.fn() }));

describe("freeform tutorial departure custody", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(usePreferencesStore);
    vi.clearAllMocks();
    useSessionStore.setState({
      activeSessionId: "tutorial",
      compositionStateLoaded: true,
      isComposing: false,
      proposalActionPendingIds: [],
      error: null,
    });
    usePreferencesStore.setState({ loaded: true });
    vi.mocked(api.updateUserComposerPreferences).mockImplementation(async (body) => ({
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: body.tutorial_completed_at ?? null,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-09-28T00:00:00Z",
    }));
  });

  it("graduates the loaded freeform session without a mode preference write", async () => {
    await departTutorialSession("tutorial", "complete", () => true);

    expect(api.updateUserComposerPreferences).toHaveBeenCalledWith(
      expect.not.objectContaining({ default_mode: expect.anything() }),
    );
    expect(useSessionStore.getState().activeSessionId).toBe("tutorial");
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(true);
  });

  it("refuses to publish while a freeform compose is in flight", async () => {
    useSessionStore.setState({ isComposing: true });

    await expect(departTutorialSession("tutorial", "exit", () => true)).rejects.toThrow("current Composer operation");

    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
  });

  it("refuses to publish while a proposal action is in flight", async () => {
    useSessionStore.setState({ proposalActionPendingIds: ["proposal-1"] });

    await expect(departTutorialSession("tutorial", "exit", () => true)).rejects.toThrow("current Composer operation");

    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
  });

  it("does not publish if selection changes during the preference save", async () => {
    const response = vi.mocked(api.updateUserComposerPreferences).getMockImplementation();
    if (response === undefined) throw new Error("Missing preference responder");
    vi.mocked(api.updateUserComposerPreferences).mockImplementationOnce(async (body) => {
      useSessionStore.setState({ activeSessionId: "another-session" });
      return response(body);
    });

    await expect(departTutorialSession("tutorial", "exit", () => true)).rejects.toThrow("active session changed");

    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
  });

  it("does not mistake a selected ID with failed hydration for a loaded session", async () => {
    useSessionStore.setState({ error: "Session load failed" });

    await expect(departTutorialSession("tutorial", "complete", () => true)).rejects.toThrow("Session load failed");

    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
  });

  it("retries a failed preference write without changing the session", async () => {
    vi.mocked(api.updateUserComposerPreferences).mockRejectedValueOnce(new Error("Save unavailable"));

    await expect(departTutorialSession("tutorial", "exit", () => true)).rejects.toThrow("Save unavailable");
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
    await departTutorialSession("tutorial", "exit", () => true);
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(true);
    expect(useSessionStore.getState().activeSessionId).toBe("tutorial");
  });
});


it("does not publish a departure after preference account replacement", async () => {
  usePreferencesStore.getState().reset();
  useSessionStore.setState({ activeSessionId: "tutorial", compositionStateLoaded: true, isComposing: false, proposalActionPendingIds: [], error: null });
  let resolve!: (payload: Awaited<ReturnType<typeof api.updateUserComposerPreferences>>) => void;
  vi.mocked(api.updateUserComposerPreferences).mockReturnValueOnce(new Promise((yes) => { resolve = yes; }));
  const pending = departTutorialSession("tutorial", "complete", () => true);
  const failed = expect(pending).rejects.toThrow(/account changed/i);
  usePreferencesStore.getState().bindPrincipal("new-user", "local");
  resolve({ freeform_intro_dismissed_at: null, tutorial_completed_at: "old completion", tutorial_stage: null, tutorial_session_id: null, tutorial_run_id: null, tutorial_source_data_hash: null, show_advanced: false, updated_at: null });
  await failed;
  expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
});


it("keeps completion unpublished when the caller unmounts during the departure save", async () => {
  usePreferencesStore.getState().reset();
  useSessionStore.setState({ activeSessionId: "tutorial", compositionStateLoaded: true, isComposing: false, proposalActionPendingIds: [], error: null });
  let active = true;
  let resolve!: (payload: Awaited<ReturnType<typeof api.updateUserComposerPreferences>>) => void;
  vi.mocked(api.updateUserComposerPreferences).mockReturnValueOnce(new Promise((yes) => { resolve = yes; }));
  const pending = departTutorialSession("tutorial", "complete", () => active);
  const failed = expect(pending).rejects.toThrow(/account changed/i);
  active = false;
  resolve({ freeform_intro_dismissed_at: null, tutorial_completed_at: "persisted completion", tutorial_stage: null, tutorial_session_id: null, tutorial_run_id: null, tutorial_source_data_hash: null, show_advanced: false, updated_at: null });
  await failed;
  expect(usePreferencesStore.getState().tutorialCompletedAt).toBe("persisted completion");
  expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
});
