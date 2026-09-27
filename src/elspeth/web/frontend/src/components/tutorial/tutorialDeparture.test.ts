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
      guidedSession: null,
      isComposing: false,
      proposalActionPendingIds: [],
      error: null,
    });
    usePreferencesStore.setState({ loaded: true, defaultMode: "freeform" });
    vi.mocked(api.updateUserComposerPreferences).mockImplementation(async (body) => ({
      default_mode: body.default_mode ?? "freeform",
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

  it("graduates the loaded freeform session without switching modes", async () => {
    const exitToFreeform = vi.fn();
    useSessionStore.setState({ exitToFreeform });

    await departTutorialSession("tutorial", "complete");

    expect(exitToFreeform).not.toHaveBeenCalled();
    expect(useSessionStore.getState().activeSessionId).toBe("tutorial");
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(true);
  });

  it("refuses to publish while a freeform compose is in flight", async () => {
    useSessionStore.setState({ isComposing: true });

    await expect(departTutorialSession("tutorial", "exit")).rejects.toThrow("current Composer operation");

    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
  });

  it("refuses to publish while a proposal action is in flight", async () => {
    useSessionStore.setState({ proposalActionPendingIds: ["proposal-1"] });

    await expect(departTutorialSession("tutorial", "exit")).rejects.toThrow("current Composer operation");

    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
  });

  it("does not publish if selection changes during the preference save", async () => {
    const response = vi.mocked(api.updateUserComposerPreferences).getMockImplementation();
    if (response === undefined) throw new Error("Missing preference responder");
    vi.mocked(api.updateUserComposerPreferences).mockImplementationOnce(async (body) => {
      useSessionStore.setState({ activeSessionId: "another-session" });
      return response(body);
    });

    await expect(departTutorialSession("tutorial", "exit")).rejects.toThrow("active session changed");

    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
  });

  it("does not mistake a selected ID with failed hydration for a loaded session", async () => {
    useSessionStore.setState({ error: "Session load failed" });

    await expect(departTutorialSession("tutorial", "complete")).rejects.toThrow("Session load failed");

    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
  });

  it("retries a failed preference write without changing the session", async () => {
    vi.mocked(api.updateUserComposerPreferences).mockRejectedValueOnce(new Error("Save unavailable"));

    await expect(departTutorialSession("tutorial", "exit")).rejects.toThrow("Save unavailable");
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
    await departTutorialSession("tutorial", "exit");
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(true);
    expect(useSessionStore.getState().activeSessionId).toBe("tutorial");
  });
});
