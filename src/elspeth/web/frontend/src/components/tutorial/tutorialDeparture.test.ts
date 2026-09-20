import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionStore } from "@/stores/sessionStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { resetStore } from "@/test/store-helpers";
import { acquireGuidedRetry, clearAllGuidedRetries } from "@/stores/guidedOperationRetry";
import * as api from "@/api/client";
import type { GuidedSession } from "@/types/guided";
import { departTutorialSession } from "./tutorialDeparture";

vi.mock("@/api/client", () => ({ updateUserComposerPreferences: vi.fn() }));

function completedSession(): GuidedSession {
  return {
    step: "step_4_wire",
    history: [],
    terminal: { kind: "completed", reason: null, pipeline_yaml: "sources: []" },
    chat_history: [],
    chat_turn_seq: 0,
    reviewed_components: { sources: [], outputs: [] },
    profile: null,
  };
}

describe("tutorial departure custody", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(usePreferencesStore);
    vi.clearAllMocks();
    clearAllGuidedRetries();
    useSessionStore.setState({ activeSessionId: "tutorial", guidedSession: completedSession() });
    usePreferencesStore.setState({ loaded: true, defaultMode: "guided" });
    vi.mocked(api.updateUserComposerPreferences).mockImplementation(async (body) => ({
      default_mode: body.default_mode ?? "guided",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: body.tutorial_completed_at ?? null,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-09-20T00:00:00Z",
    }));
  });

  it("waits for the authoritative exit before persisting or publishing completion", async () => {
    let settle!: () => void;
    const exit = vi.fn(async () => {
      await new Promise<void>((resolve) => { settle = resolve; });
      useSessionStore.setState({ guidedSession: {
        ...completedSession(), terminal: { kind: "exited_to_freeform", reason: "user_pressed_exit", pipeline_yaml: null },
      } });
      return { status: "applied" as const };
    });
    useSessionStore.setState({ exitToFreeform: exit });
    const departure = departTutorialSession("tutorial", "complete");
    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
    settle();
    await departure;
    expect(useSessionStore.getState().activeSessionId).toBe("tutorial");
    expect(useSessionStore.getState().guidedSession?.terminal?.kind).toBe("exited_to_freeform");
    expect(usePreferencesStore.getState().defaultMode).toBe("freeform");
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(true);
  });

  it("does not graduate when exit is pending and accepts an explicit retry", async () => {
    const exit = vi.fn<ReturnType<typeof useSessionStore.getState>["exitToFreeform"]>();
    exit.mockResolvedValueOnce({ status: "not_applied", reason: "pending", message: "Wait for the current operation." });
    exit.mockImplementationOnce(async () => {
      useSessionStore.setState({ guidedSession: {
        ...completedSession(), terminal: { kind: "exited_to_freeform", reason: "user_pressed_exit", pipeline_yaml: null },
      } });
      return { status: "applied" };
    });
    useSessionStore.setState({ exitToFreeform: exit });
    await expect(departTutorialSession("tutorial", "exit")).rejects.toThrow("Wait for the current operation.");
    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
    await departTutorialSession("tutorial", "exit");
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(true);
  });

  it("does not save completion for a different active session after awaiting exit", async () => {
    useSessionStore.setState({ exitToFreeform: vi.fn(async () => {
      useSessionStore.setState({ activeSessionId: "another-session" });
      return { status: "applied" as const };
    }) });
    await expect(departTutorialSession("tutorial", "exit")).rejects.toThrow("active session changed");
    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
  });

  it("does not mistake a selected ID with failed hydration for a Freeform session", async () => {
    useSessionStore.setState({ guidedSession: null, compositionStateLoaded: true, error: "Session load failed" });
    await expect(departTutorialSession("tutorial", "complete")).rejects.toThrow("Session load failed");
    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
  });

  it("does not publish if the same session re-enters Guided during the preference save", async () => {
    useSessionStore.setState({ guidedSession: {
      ...completedSession(), terminal: { kind: "exited_to_freeform", reason: "user_pressed_exit", pipeline_yaml: null },
    } });
    const response = vi.mocked(api.updateUserComposerPreferences).getMockImplementation();
    if (response === undefined) throw new Error("Missing preferences responder");
    vi.mocked(api.updateUserComposerPreferences).mockImplementationOnce(async (body) => {
      useSessionStore.setState({ guidedSession: { ...completedSession(), terminal: null } });
      return response(body);
    });
    await expect(departTutorialSession("tutorial", "exit")).rejects.toThrow("changing mode");
    expect(usePreferencesStore.getState().tutorialCompletedAt).not.toBeNull();
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
  });

  it("does not publish while Guided re-entry owns an unsettled request", async () => {
    const sessionId = "12345678-1234-4234-8234-123456789abc";
    useSessionStore.setState({ activeSessionId: sessionId, guidedSession: {
      ...completedSession(), terminal: { kind: "exited_to_freeform", reason: "user_pressed_exit", pipeline_yaml: null },
    } });
    const response = vi.mocked(api.updateUserComposerPreferences).getMockImplementation();
    if (response === undefined) throw new Error("Missing preferences responder");
    vi.mocked(api.updateUserComposerPreferences).mockImplementationOnce(async (body) => {
      expect(acquireGuidedRetry("guided_reenter", sessionId, []).status).toBe("acquired");
      return response(body);
    });
    await expect(departTutorialSession(sessionId, "exit")).rejects.toThrow("changing mode");
    expect(usePreferencesStore.getState().tutorialCompletedAt).not.toBeNull();
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
  });

  it("retries a failed preference write without re-exiting the settled Guided session", async () => {
    const exit = vi.fn(async () => {
      useSessionStore.setState({ guidedSession: {
        ...completedSession(), terminal: { kind: "exited_to_freeform", reason: "user_pressed_exit", pipeline_yaml: null },
      } });
      return { status: "applied" as const };
    });
    useSessionStore.setState({ exitToFreeform: exit });
    vi.mocked(api.updateUserComposerPreferences).mockRejectedValueOnce(new Error("Save unavailable"));
    await expect(departTutorialSession("tutorial", "exit")).rejects.toThrow("Save unavailable");
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
    await departTutorialSession("tutorial", "exit");
    expect(exit).toHaveBeenCalledTimes(1);
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(true);
  });
});
