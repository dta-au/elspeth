/**
 * Tests for src/stores/preferencesStore.ts (Phase 1B Task 2).
 *
 * Mocking convention: vi.mock("@/api/client", () => ({...})) at module load,
 * vi.mocked(fn).mockResolvedValueOnce(...) per test. Mirrors
 * sessionStore.test.ts.
 *
 * Store-isolation convention: resetStore(usePreferencesStore) in beforeEach,
 * because Zustand stores are module-level singletons and any earlier
 * test in the same vitest worker leaks state otherwise (the Phase 1A
 * finding-7 pattern).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { selectShowAdvanced, selectTutorialCompleted, usePreferencesStore } from "./preferencesStore";
import { resetStore } from "@/test/store-helpers";
import {
  fetchUserComposerPreferences,
  updateUserComposerPreferences,
} from "@/api/client";

vi.mock("@/api/client", () => ({
  fetchUserComposerPreferences: vi.fn(),
  updateUserComposerPreferences: vi.fn(),
  // Phase 1B Task 4.5 — the integration test below drives the real
  // sessionStore.createSession, which calls api.createSession; mock here so
  // the module-load wiring for that test resolves cleanly.
  createSession: vi.fn(),
}));

const mockFetch = vi.mocked(fetchUserComposerPreferences);
const mockUpdate = vi.mocked(updateUserComposerPreferences);

describe("preferencesStore", () => {
  beforeEach(() => {
    resetStore(usePreferencesStore);
    vi.clearAllMocks();
  });

  it("loads preferences from API on bootstrap", async () => {
    mockFetch.mockResolvedValueOnce({
      default_mode: "freeform",
      freeform_intro_dismissed_at: "2026-07-12T05:00:00Z",
      tutorial_completed_at: null,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-05-15T00:00:00Z",
    });

    await usePreferencesStore.getState().bootstrap();

    const state = usePreferencesStore.getState();
    expect(state.defaultMode).toBe("freeform");
    expect(state.freeformIntroDismissedAt).toBe("2026-07-12T05:00:00Z");
    expect(state.tutorialCompletedAt).toBeNull();
    expect(selectTutorialCompleted(state)).toBe(false);
    expect(state.loaded).toBe(true);
  });

  it("dismisses the freeform introduction only after the server confirms", async () => {
    usePreferencesStore.setState({
      loaded: true,
      freeformIntroDismissedAt: null,
    });
    mockUpdate.mockResolvedValueOnce({
      default_mode: "freeform",
      freeform_intro_dismissed_at: "2026-07-12T05:00:00Z",
      tutorial_completed_at: null,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-07-12T05:00:00Z",
    });

    const pending = usePreferencesStore.getState().dismissFreeformIntro();
    expect(usePreferencesStore.getState().writing).toBe(true);
    expect(usePreferencesStore.getState().freeformIntroDismissedAt).toBeNull();
    await pending;

    expect(mockUpdate).toHaveBeenCalledWith({
      freeform_intro_dismissed_at: expect.any(String),
    });
    expect(usePreferencesStore.getState().freeformIntroDismissedAt).toBe(
      "2026-07-12T05:00:00Z",
    );
  });

  it("selectTutorialCompleted derives true when tutorialCompletedAt is set", () => {
    usePreferencesStore.setState({
      tutorialCompletedAt: "2026-05-19T12:00:00Z",
      tutorialCompleted: true,
    });

    expect(selectTutorialCompleted(usePreferencesStore.getState())).toBe(true);
  });

  it("setDefaultMode updates state optimistically and persists", async () => {
    usePreferencesStore.setState({ loaded: true, defaultMode: "guided" });
    mockUpdate.mockResolvedValueOnce({
      default_mode: "freeform",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: null,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-05-15T00:00:00Z",
    });

    await usePreferencesStore.getState().setDefaultMode("freeform");

    expect(usePreferencesStore.getState().defaultMode).toBe("freeform");
    expect(mockUpdate).toHaveBeenCalledWith({ default_mode: "freeform" });
  });

  it("setDefaultMode reverts on error", async () => {
    usePreferencesStore.setState({ loaded: true, defaultMode: "guided" });
    mockUpdate.mockRejectedValueOnce(new Error("network failure"));

    await expect(
      usePreferencesStore.getState().setDefaultMode("freeform"),
    ).rejects.toThrow("network failure");

    expect(usePreferencesStore.getState().defaultMode).toBe("guided");
    expect(usePreferencesStore.getState().writing).toBe(false);
  });

  it("setDefaultMode ignores concurrent calls while writing", async () => {
    usePreferencesStore.setState({
      loaded: true,
      defaultMode: "guided",
      writing: true,
    });

    await usePreferencesStore.getState().setDefaultMode("freeform");

    expect(mockUpdate).not.toHaveBeenCalled();
    expect(usePreferencesStore.getState().defaultMode).toBe("guided");
  });

  it("markTutorialGraduated atomically PATCHes mode, completion, and provenance", async () => {
    usePreferencesStore.setState({
      loaded: true,
      defaultMode: "freeform",
      tutorialCompletedAt: null,
      tutorialCompleted: false,
    });
    mockUpdate.mockResolvedValueOnce({
      default_mode: "freeform",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: "2026-05-19T12:30:00Z",
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-05-19T12:30:00Z",
    });

    await usePreferencesStore.getState().markTutorialGraduated({ via: "complete" });

    expect(mockUpdate).toHaveBeenCalledTimes(1);
    expect(mockUpdate.mock.calls[0][0]).toEqual({
      default_mode: "freeform",
      tutorial_completed_at: expect.any(String),
      tutorial_completed_via: "complete",
    });
    expect(usePreferencesStore.getState().defaultMode).toBe("freeform");
    expect(usePreferencesStore.getState().tutorialCompletedAt).toBe(
      "2026-05-19T12:30:00Z",
    );
    expect(selectTutorialCompleted(usePreferencesStore.getState())).toBe(true);
  });

  it("markTutorialGraduated can defer the local completion flip until the caller publishes it", async () => {
    usePreferencesStore.setState({
      loaded: true,
      defaultMode: "freeform",
      tutorialCompletedAt: null,
      tutorialCompleted: false,
    });
    mockUpdate.mockResolvedValueOnce({
      default_mode: "freeform",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: "2026-05-19T12:30:00Z",
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-05-19T12:30:00Z",
    });

    const completedAt = await usePreferencesStore
      .getState()
      .markTutorialGraduated({ via: "skip", publishLocally: false });

    expect(completedAt).toBe("2026-05-19T12:30:00Z");
    expect(mockUpdate.mock.calls[0][0]).toEqual({
      default_mode: "freeform",
      tutorial_completed_at: expect.any(String),
      tutorial_completed_via: "skip",
    });
    expect(usePreferencesStore.getState().tutorialCompletedAt).toBe(completedAt);
    expect(selectTutorialCompleted(usePreferencesStore.getState())).toBe(false);

    usePreferencesStore.getState().publishTutorialGraduation(completedAt);

    expect(usePreferencesStore.getState().tutorialCompletedAt).toBe(
      "2026-05-19T12:30:00Z",
    );
    expect(selectTutorialCompleted(usePreferencesStore.getState())).toBe(true);
  });

  it("markTutorialGraduated waits out an in-flight write instead of dropping the opt-out", async () => {
    // The old `if (writing) return` no-op silently dropped the graduation
    // PATCH — an exit/skip click landing while another preferences write was
    // in flight simply never persisted (elspeth-61591e64bb). The store now
    // waits for the in-flight write to settle, then sends the PATCH.
    usePreferencesStore.setState({
      loaded: true,
      defaultMode: "guided",
      writing: true,
    });
    mockUpdate.mockResolvedValueOnce({
      default_mode: "freeform",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: "2026-07-09T00:00:00Z",
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-07-09T00:00:00Z",
    });
    // Simulate the in-flight write settling shortly after the click.
    setTimeout(() => {
      usePreferencesStore.setState({ writing: false });
    }, 120);

    await usePreferencesStore.getState().markTutorialGraduated({ via: "complete" });

    expect(mockUpdate).toHaveBeenCalledWith(
      expect.objectContaining({ tutorial_completed_at: expect.any(String) }),
    );
    expect(selectTutorialCompleted(usePreferencesStore.getState())).toBe(true);
  });

  it("markTutorialGraduated does not re-PATCH a completion that landed while it waited", async () => {
    // Double-clicked Exit (or the chrome exit racing the wizard's onExited
    // hand-off): the write this call waits out IS the same graduation. The
    // backend counts every via=exit PATCH (no prior-state check), so an
    // unconditional second PATCH double-counts completion_path telemetry.
    usePreferencesStore.setState({
      loaded: true,
      defaultMode: "guided",
      writing: true,
    });
    // Simulate the first exit click's PATCH landing while this one waits.
    setTimeout(() => {
      usePreferencesStore.setState({
        writing: false,
        tutorialCompletedAt: "2026-07-09T00:00:00Z",
        tutorialCompleted: true,
      });
    }, 120);

    const completedAt = await usePreferencesStore
      .getState()
      .markTutorialGraduated({ via: "exit" });

    expect(completedAt).toBe("2026-07-09T00:00:00Z");
    expect(mockUpdate).not.toHaveBeenCalled();
  });

  it("two rapid exit clicks send exactly one completion PATCH", async () => {
    usePreferencesStore.setState({ loaded: true, defaultMode: "guided" });
    let resolveFirst!: (payload: {
      default_mode: "freeform";
      freeform_intro_dismissed_at: null;
      tutorial_completed_at: string;
      tutorial_stage: null;
      tutorial_session_id: null;
      tutorial_run_id: null;
      tutorial_source_data_hash: null;
      show_advanced: false;
      updated_at: string;
    }) => void;
    mockUpdate.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
    );

    const first = usePreferencesStore
      .getState()
      .markTutorialGraduated({ via: "exit" });
    const second = usePreferencesStore
      .getState()
      .markTutorialGraduated({ via: "exit" });
    resolveFirst({
      default_mode: "freeform",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: "2026-07-09T00:00:00Z",
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-07-09T00:00:00Z",
    });

    const [a, b] = await Promise.all([first, second]);

    expect(mockUpdate).toHaveBeenCalledTimes(1);
    expect(a).toBe("2026-07-09T00:00:00Z");
    expect(b).toBe("2026-07-09T00:00:00Z");
  });

  it("markTutorialGraduated sends the exit discriminator when asked", async () => {
    // The backend infers first_time/skip from payload shape; an explicit
    // exit must carry tutorial_completed_via so it is not bucketed as
    // "skip" (elspeth-61591e64bb telemetry correction).
    usePreferencesStore.setState({ loaded: true, defaultMode: "guided" });
    mockUpdate.mockResolvedValueOnce({
      default_mode: "freeform",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: "2026-07-09T00:00:00Z",
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-07-09T00:00:00Z",
    });

    await usePreferencesStore.getState().markTutorialGraduated({ via: "exit" });

    expect(mockUpdate).toHaveBeenCalledWith({
      default_mode: "freeform",
      tutorial_completed_at: expect.any(String),
      tutorial_completed_via: "exit",
    });
    expect(selectTutorialCompleted(usePreferencesStore.getState())).toBe(true);
  });

  it("bootstrap is re-entrant safe (always fetches; two calls = two API calls)", async () => {
    mockFetch.mockResolvedValue({
      default_mode: "guided",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: null,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-05-15T00:00:00Z",
    });

    await usePreferencesStore.getState().bootstrap();
    await usePreferencesStore.getState().bootstrap();

    expect(usePreferencesStore.getState().loaded).toBe(true);
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it("resolveDefaultMode returns cached value if already loaded", async () => {
    usePreferencesStore.setState({ loaded: true, defaultMode: "guided" });

    const mode = await usePreferencesStore.getState().resolveDefaultMode();

    expect(mode).toBe("guided");
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("resolveDefaultMode awaits bootstrap when not yet loaded", async () => {
    mockFetch.mockResolvedValueOnce({
      default_mode: "freeform",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: null,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-05-15T00:00:00Z",
    });

    const mode = await usePreferencesStore.getState().resolveDefaultMode();

    expect(mode).toBe("freeform");
    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(usePreferencesStore.getState().loaded).toBe(true);
  });

  // I5 — silent-failure-hunter remediation. Before this fix, bootstrap()
  // would reject and the App.tsx caller's `.catch(console.error)` would
  // swallow the failure. `loaded` stayed false, gating the UI on a
  // condition that would never become true — a CorruptPreferencesError
  // (named for incident response) was completely invisible to the user.
  //
  // The new contract: bootstrap() NEVER rejects. On failure it sets
  // loaded:true + bootstrapError but LEAVES defaultMode null — an absent
  // value stays null rather than coerced to a default, because absence is
  // evidence. Setting defaultMode="guided"
  // on failure would attribute a preference choice to the user that
  // they never made. resolveDefaultMode() continues to throw on the
  // null branch; sessionStore.createSession catches that and tells the
  // user "you're in freeform; we couldn't apply your default mode" —
  // an honest secondary-failure attribution.

  it("bootstrap sets loaded+bootstrapError on generic API failure without fabricating a defaultMode", async () => {
    mockFetch.mockRejectedValueOnce(new Error("network failure"));

    await expect(
      usePreferencesStore.getState().bootstrap(),
    ).resolves.toBeUndefined();

    const state = usePreferencesStore.getState();
    expect(state.loaded).toBe(true);
    // Honest: we don't know what mode the user had set.
    expect(state.defaultMode).toBeNull();
    expect(state.bootstrapError).not.toBeNull();
    expect(state.bootstrapError).toMatch(/network failure/);
  });

  it("bootstrap surfaces a corrupt-preferences message when the backend signals error_type=corrupt_preferences", async () => {
    // ApiError shape produced by parseResponse() in @/api/client when the
    // backend returns a structured 5xx with error_type. The store branches
    // on error_type to distinguish a corrupt-row failure (needs operator
    // action) from a transient unavailability.
    const apiError = {
      status: 500,
      detail: "Saved preferences are corrupt; the composer is using defaults.",
      error_type: "corrupt_preferences",
    };
    mockFetch.mockRejectedValueOnce(apiError);

    await expect(
      usePreferencesStore.getState().bootstrap(),
    ).resolves.toBeUndefined();

    const state = usePreferencesStore.getState();
    expect(state.loaded).toBe(true);
    expect(state.defaultMode).toBeNull();
    expect(state.bootstrapError).not.toBeNull();
    expect(state.bootstrapError).toMatch(/corrupt/i);
    expect(state.bootstrapError).toMatch(/administrator|operator|contact/i);
  });

  it("leaves showAdvanced false and surfaces the error when the payload is rejected (elspeth-7d07df6438)", async () => {
    // The decoder lives in api/preferencesDecoder.ts and is exercised in
    // isolation there; this pins the CONSEQUENCE of a rejection through the
    // store's existing fail-closed catch (bootstrap never fabricates a
    // preference the user never set — see the block comment above).
    mockFetch.mockRejectedValueOnce(
      new Error("Invalid composer preferences at composer-preferences: missing show_advanced"),
    );

    await usePreferencesStore.getState().bootstrap();

    const state = usePreferencesStore.getState();
    expect(state.showAdvanced).toBe(false);
    expect(state.bootstrapError).not.toBeNull();
  });

  it("resolveDefaultMode throws immediately without re-bootstrapping when loaded=true and defaultMode=null", async () => {
    // P0.9: the prior implementation guarded as
    // `if (current.loaded && current.defaultMode !== null) return …`,
    // then fell through to a second `bootstrap()` call when that guard
    // failed because of `defaultMode === null`. A bootstrap that already
    // produced `loaded:true, defaultMode:null` is in a known-broken
    // state (bootstrapError is set); re-running it just re-fails. Throw
    // immediately so sessionStore.createSession surfaces the honest
    // secondary-failure message to the user without an extra round-trip.
    usePreferencesStore.setState({
      loaded: true,
      defaultMode: null,
      bootstrapError: "Saved preferences are corrupt; using defaults.",
    });

    await expect(
      usePreferencesStore.getState().resolveDefaultMode(),
    ).rejects.toThrow(/loaded=true but defaultMode is null/);

    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("resolveDefaultMode still throws after a failed bootstrap (preserves sessionStore secondary-failure attribution)", async () => {
    // mockRejected is consumed twice because resolveDefaultMode awaits
    // bootstrap() once and that bootstrap consumes one mockRejectedValueOnce.
    // Use mockRejectedValue (not Once) so the implementation can retry
    // without exhausting the queue.
    mockFetch.mockRejectedValue(new Error("server unreachable"));

    await expect(
      usePreferencesStore.getState().resolveDefaultMode(),
    ).rejects.toThrow(/bootstrap completed but defaultMode is null/);

    // The bootstrapError is set on the failure path even though the throw
    // bubbles out of resolveDefaultMode — that's the channel
    // sessionStore.createSession surfaces to the user.
    expect(usePreferencesStore.getState().bootstrapError).not.toBeNull();
  });
});

// ── Phase 1B Task 4.5: preferences → session integration ───────────────────
// Both real stores. Only the API layer is mocked (and enterGuided is stubbed
// to avoid pulling in the GET /guided machinery). Proves the inter-store
// contract — sessionStore.createSession actually consults the live
// preferencesStore via resolveDefaultMode() rather than reading a stale
// closure or its own state.
describe("preferences → session integration (real stores, API mocked)", () => {
  beforeEach(async () => {
    resetStore(usePreferencesStore);
    const { useSessionStore } = await import("@/stores/sessionStore");
    resetStore(useSessionStore);
    vi.clearAllMocks();
  });

  it("createSession enters guided when the live preference is guided (smoke)", async () => {
    const { useSessionStore } = await import("@/stores/sessionStore");
    const api = await import("@/api/client");
    usePreferencesStore.setState({
      loaded: true,
      defaultMode: "guided",
      writing: false,
    });
    (api.createSession as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      id: "sess-int",
      title: "untitled",
      created_at: "2026-05-15T00:00:00Z",
      updated_at: "2026-05-15T00:00:00Z",
    });
    const enterGuided = vi
      .spyOn(useSessionStore.getState(), "enterGuided")
      .mockResolvedValue();

    await useSessionStore.getState().createSession();

    expect(enterGuided).toHaveBeenCalledTimes(1);
  });
});

describe("preferencesStore — tutorial resume state (elspeth-918f4434b3)", () => {
  beforeEach(() => {
    resetStore(usePreferencesStore);
    vi.clearAllMocks();
  });

  it("bootstrap loads the persisted tutorial progress fields", async () => {
    mockFetch.mockResolvedValueOnce({
      default_mode: "guided",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: null,
      tutorial_stage: "run",
      tutorial_session_id: "sess-1",
      tutorial_run_id: "run-1",
      tutorial_source_data_hash: "hash-1",
      show_advanced: false,
      updated_at: "2026-07-02T00:00:00Z",
    });

    await usePreferencesStore.getState().bootstrap();

    const state = usePreferencesStore.getState();
    expect(state.tutorialStage).toBe("run");
    expect(state.tutorialSessionId).toBe("sess-1");
    expect(state.tutorialRunId).toBe("run-1");
    expect(state.tutorialSourceDataHash).toBe("hash-1");
  });

  it("saveTutorialProgress PATCHes the four resume fields and mirrors the response", async () => {
    usePreferencesStore.setState({ loaded: true, defaultMode: "guided" });
    mockUpdate.mockResolvedValueOnce({
      default_mode: "guided",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: null,
      tutorial_stage: "guided",
      tutorial_session_id: "sess-2",
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-07-02T00:00:00Z",
    });

    await usePreferencesStore.getState().saveTutorialProgress({
      stage: "guided",
      sessionId: "sess-2",
      runId: null,
      sourceDataHash: null,
    });

    expect(mockUpdate).toHaveBeenCalledWith({
      tutorial_stage: "guided",
      tutorial_session_id: "sess-2",
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
    });
    const state = usePreferencesStore.getState();
    expect(state.tutorialStage).toBe("guided");
    expect(state.tutorialSessionId).toBe("sess-2");
  });

  it("saveTutorialProgress waits for another preferences writer", async () => {
    vi.useFakeTimers();
    try {
      let resolveMode!: (payload: Awaited<ReturnType<typeof fetchUserComposerPreferences>>) => void;
      const payload = {
        default_mode: "freeform" as const,
        freeform_intro_dismissed_at: null,
        tutorial_completed_at: null,
        tutorial_stage: null,
        tutorial_session_id: null,
        tutorial_run_id: null,
        tutorial_source_data_hash: null,
        show_advanced: false,
        updated_at: null,
      };
      mockUpdate.mockImplementationOnce(() => new Promise((resolve) => { resolveMode = resolve; }));
      mockUpdate.mockResolvedValueOnce({ ...payload, tutorial_stage: "run", tutorial_session_id: "sess-3" });
      const modeSave = usePreferencesStore.getState().setDefaultMode("freeform");
      const progressSave = usePreferencesStore.getState().saveTutorialProgress({
        stage: "run", sessionId: "sess-3", runId: null, sourceDataHash: null,
      });
      expect(mockUpdate).toHaveBeenCalledTimes(1);
      resolveMode(payload);
      await modeSave;
      await vi.advanceTimersByTimeAsync(50);
      await progressSave;
      expect(mockUpdate).toHaveBeenCalledTimes(2);
      expect(usePreferencesStore.getState().tutorialStage).toBe("run");
      expect(usePreferencesStore.getState().writing).toBe(false);
    } finally { vi.useRealTimers(); }
  });

  it("markTutorialGraduated mirrors the server clearing the resume fields", async () => {
    usePreferencesStore.setState({
      loaded: true,
      tutorialStage: "graduation",
      tutorialSessionId: "sess-4",
      tutorialRunId: "run-4",
      tutorialSourceDataHash: "hash-4",
    });
    mockUpdate.mockResolvedValueOnce({
      default_mode: "freeform",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: "2026-07-02T00:00:00Z",
      // Completion-clears-progress: the backend terminated the resume state.
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-07-02T00:00:00Z",
    });

    await usePreferencesStore.getState().markTutorialGraduated({ via: "complete" });

    const state = usePreferencesStore.getState();
    expect(state.tutorialCompleted).toBe(true);
    expect(state.tutorialStage).toBeNull();
    expect(state.tutorialSessionId).toBeNull();
    expect(state.tutorialRunId).toBeNull();
    expect(state.tutorialSourceDataHash).toBeNull();
  });

  it("loads showAdvanced from the payload and defaults to false before bootstrap", async () => {
    expect(usePreferencesStore.getState().showAdvanced).toBe(false);
    mockFetch.mockResolvedValueOnce({
      default_mode: "guided",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: null,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: true,
      updated_at: "2026-05-15T00:00:00Z",
    });
    await usePreferencesStore.getState().bootstrap();
    expect(selectShowAdvanced(usePreferencesStore.getState())).toBe(true);
  });

  it("setShowAdvanced writes optimistically and reverts on failure", async () => {
    mockUpdate.mockRejectedValueOnce(new Error("offline"));
    await expect(usePreferencesStore.getState().setShowAdvanced(true)).rejects.toThrow("offline");
    const state = usePreferencesStore.getState();
    expect(state.showAdvanced).toBe(false);
    expect(state.writeError).toMatch(/Couldn't save your preference/);
    expect(mockUpdate).toHaveBeenCalledWith({ show_advanced: true });
  });

  it("resetTutorial clears tutorial_completed_at through the PATCH contract", async () => {
    usePreferencesStore.setState({
      loaded: true,
      defaultMode: "guided",
      tutorialCompletedAt: "2026-05-19T12:00:00Z",
      tutorialCompleted: true,
    });
    mockUpdate.mockResolvedValueOnce({
      default_mode: "guided",
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: null,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: false,
      updated_at: "2026-05-19T12:30:00Z",
    });

    await usePreferencesStore.getState().resetTutorial();

    // Completion AND the resume fields clear in one PATCH: Reset is also
    // offered mid-tutorial (the wedged-resume escape hatch), where a stale
    // stage/session surviving the reset would resume straight back into the
    // state being escaped.
    expect(mockUpdate).toHaveBeenCalledWith({
      tutorial_completed_at: null,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
    });
    expect(usePreferencesStore.getState().tutorialCompletedAt).toBeNull();
    expect(selectTutorialCompleted(usePreferencesStore.getState())).toBe(false);
  });
});

// ── Rate-limit retry (2026-08-16 bucket-split plan, Task 6) ────────────────
// The tutorial's own stage-persist burst can transiently exhaust the write
// bucket; the completion save is the one write whose loss has a durable
// consequence (the tutorial re-shows on next load), so it retries exactly
// once after a rate-limited 429, waiting out the envelope's retry_after.
describe("preferencesStore — markTutorialGraduated 429 retry", () => {
  const completedPayload = {
    default_mode: "freeform" as const,
    freeform_intro_dismissed_at: null,
    tutorial_completed_at: "2026-08-16T00:00:00Z",
    tutorial_stage: null,
    tutorial_session_id: null,
    tutorial_run_id: null,
    tutorial_source_data_hash: null,
    show_advanced: false,
    updated_at: "2026-08-16T00:00:00Z",
  };

  beforeEach(() => {
    resetStore(usePreferencesStore);
    vi.clearAllMocks();
  });

  it("retries the graduation PATCH once after a rate-limited failure", async () => {
    vi.useFakeTimers();
    mockUpdate
      .mockRejectedValueOnce({ status: 429, error_type: "rate_limited", detail: "…", retry_after: 2 })
      .mockResolvedValueOnce(completedPayload); // reuse the file's payload fixture
    const promise = usePreferencesStore.getState().markTutorialGraduated({ via: "complete" });
    await vi.advanceTimersByTimeAsync(2_000);
    await expect(promise).resolves.toBe(completedPayload.tutorial_completed_at);
    expect(mockUpdate).toHaveBeenCalledTimes(2);
    expect(usePreferencesStore.getState().writeError).toBeNull();
    vi.useRealTimers();
  });

  it("gives up after the second rate-limited failure and surfaces the detail", async () => {
    vi.useFakeTimers();
    mockUpdate.mockRejectedValue({
      status: 429, error_type: "rate_limited",
      detail: "Rate limit exceeded. Try again in 2 seconds.", retry_after: 2,
    });
    const promise = usePreferencesStore.getState().markTutorialGraduated({ via: "complete" });
    promise.catch(() => undefined); // assertion happens via store state below
    await vi.advanceTimersByTimeAsync(2_000);
    await expect(promise).rejects.toMatchObject({ status: 429 });
    expect(mockUpdate).toHaveBeenCalledTimes(2); // exactly one retry, no loop
    expect(usePreferencesStore.getState().writeError).toContain("Rate limit exceeded");
    expect(usePreferencesStore.getState().writing).toBe(false);
    vi.useRealTimers();
  });

  // Large retry intervals must surface immediately so reset and other
  // preference actions are not held behind a sleeping completion write.
  it("does not retry — or hold the write flag — when retry_after exceeds the cap", async () => {
    mockUpdate.mockRejectedValue({
      status: 429,
      error_type: "rate_limited",
      detail: "Rate limit exceeded. Try again in 26 seconds.",
      retry_after: 26,
    });
    const promise = usePreferencesStore.getState().markTutorialGraduated({ via: "complete" });
    await expect(promise).rejects.toMatchObject({ status: 429 });
    // Exactly one attempt: no sleep, no second PATCH.
    expect(mockUpdate).toHaveBeenCalledTimes(1);
    // Flag released immediately, so resetTutorial et al. stay reachable.
    expect(usePreferencesStore.getState().writing).toBe(false);
    expect(usePreferencesStore.getState().writeError).toContain(
      "Try again in 26 seconds",
    );
  });
});

describe("atomic tutorial completion", () => {
  beforeEach(() => { resetStore(usePreferencesStore); vi.resetAllMocks(); });
  it("persists freeform and provenance once while deferring publication", async () => {
    mockUpdate.mockResolvedValue({ default_mode: "freeform", tutorial_completed_at: "2026-09-20T00:00:00Z", tutorial_stage: null, tutorial_session_id: null, tutorial_run_id: null, tutorial_source_data_hash: null, freeform_intro_dismissed_at: null, show_advanced: false, updated_at: null });
    const stamp = await usePreferencesStore.getState().markTutorialGraduated({ via: "skip", publishLocally: false });
    expect(mockUpdate).toHaveBeenCalledWith({ default_mode: "freeform", tutorial_completed_at: expect.any(String), tutorial_completed_via: "skip" });
    expect(usePreferencesStore.getState().tutorialCompletedAt).toBe(stamp);
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
    await usePreferencesStore.getState().markTutorialGraduated({ via: "complete" });
    expect(mockUpdate).toHaveBeenCalledTimes(1);
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(true);
  });
  it("rejects a timed-out writer without overlapping or dropping completion", async () => {
    vi.useFakeTimers();
    try {
      usePreferencesStore.setState({ writing: true });
      const pending = usePreferencesStore.getState().markTutorialGraduated({ via: "exit" });
      const rejected = expect(pending).rejects.toThrow(/try again/i);
      await vi.advanceTimersByTimeAsync(5000);
      await rejected;
      expect(mockUpdate).not.toHaveBeenCalled();
      expect(usePreferencesStore.getState().writing).toBe(true);
    } finally { vi.useRealTimers(); }
  });
  it("clears a failed bootstrap independently from write failures", async () => {
    mockFetch.mockRejectedValueOnce(new Error("offline"));
    await usePreferencesStore.getState().bootstrap();
    expect(usePreferencesStore.getState().bootstrapError).toContain("offline");
    expect(usePreferencesStore.getState().writeError).toBeNull();
    mockFetch.mockResolvedValueOnce({ default_mode: "freeform", tutorial_completed_at: null, tutorial_stage: null, tutorial_session_id: null, tutorial_run_id: null, tutorial_source_data_hash: null, freeform_intro_dismissed_at: null, show_advanced: false, updated_at: null });
    await usePreferencesStore.getState().bootstrap();
    expect(usePreferencesStore.getState().bootstrapError).toBeNull();
  });
});

describe("preferences publication boundaries", () => {
  const stamp = "2026-09-20T00:00:00Z";
  const completed = {
    default_mode: "freeform" as const,
    tutorial_completed_at: stamp,
    tutorial_stage: null,
    tutorial_session_id: null,
    tutorial_run_id: null,
    tutorial_source_data_hash: null,
    freeform_intro_dismissed_at: null,
    show_advanced: false,
    updated_at: stamp,
  };
  beforeEach(() => { resetStore(usePreferencesStore); vi.resetAllMocks(); });

  it.each(["complete", "skip", "exit"] as const)("persists %s with freeform in the same PATCH", async (via) => {
    mockUpdate.mockResolvedValueOnce(completed);
    await usePreferencesStore.getState().markTutorialGraduated({ via });
    expect(mockUpdate).toHaveBeenCalledWith({
      default_mode: "freeform",
      tutorial_completed_at: expect.any(String),
      tutorial_completed_via: via,
    });
    expect(usePreferencesStore.getState().defaultMode).toBe("freeform");
  });

  it("does not publish a deferred graduation through a settings write", async () => {
    usePreferencesStore.setState({ tutorialCompletedAt: stamp, tutorialCompleted: false });
    mockUpdate.mockResolvedValueOnce({ ...completed, default_mode: "guided" });
    await usePreferencesStore.getState().setDefaultMode("guided");
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
    expect(usePreferencesStore.getState().tutorialCompletedAt).toBe(stamp);
  });

  it("keeps completion absent after a failure so the next attempt really saves", async () => {
    mockUpdate.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(completed);
    await expect(usePreferencesStore.getState().markTutorialGraduated({ via: "complete" })).rejects.toThrow("offline");
    expect(usePreferencesStore.getState().tutorialCompletedAt).toBeNull();
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
    expect(usePreferencesStore.getState().writing).toBe(false);
    await usePreferencesStore.getState().markTutorialGraduated({ via: "complete" });
    expect(mockUpdate).toHaveBeenCalledTimes(2);
    expect(usePreferencesStore.getState().writeError).toBeNull();
  });

  it("keeps freeform-introduction cross-tab dismissal without a second PATCH", () => {
    window.dispatchEvent(new StorageEvent("storage", {
      key: "elspeth_prefs_freeform_intro_dismissed_v1", newValue: stamp,
    }));
    expect(usePreferencesStore.getState().freeformIntroDismissedAt).toBe(stamp);
    expect(mockUpdate).not.toHaveBeenCalled();
  });

  it("does not erase a bootstrap failure when a preference write succeeds", async () => {
    usePreferencesStore.setState({ bootstrapError: "Could not load", writeError: "Old write failure" });
    mockUpdate.mockResolvedValueOnce(completed);
    await usePreferencesStore.getState().setDefaultMode("freeform");
    expect(usePreferencesStore.getState().bootstrapError).toBe("Could not load");
    expect(usePreferencesStore.getState().writeError).toBeNull();
  });
});

describe("tutorial progress and completion ordering", () => {
  const progress = { stage: "run" as const, sessionId: "tutorial-session", runId: null, sourceDataHash: null };
  const completed = {
    default_mode: "freeform" as const,
    tutorial_completed_at: "2026-09-20T00:00:00Z",
    tutorial_stage: null,
    tutorial_session_id: null,
    tutorial_run_id: null,
    tutorial_source_data_hash: null,
    freeform_intro_dismissed_at: null,
    show_advanced: false,
    updated_at: null,
  };
  beforeEach(() => { resetStore(usePreferencesStore); vi.resetAllMocks(); });

  it("releases the progress write lock on failure so completion can retry", async () => {
    mockUpdate.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(completed);
    await expect(usePreferencesStore.getState().saveTutorialProgress(progress)).rejects.toThrow("offline");
    expect(usePreferencesStore.getState().writing).toBe(false);
    expect(usePreferencesStore.getState().writeError).toContain("offline");
    await usePreferencesStore.getState().markTutorialGraduated({ via: "exit" });
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(true);
    expect(usePreferencesStore.getState().writeError).toBeNull();
  });

  it("rejects timed-out progress without releasing the current writer", async () => {
    vi.useFakeTimers();
    try {
      usePreferencesStore.setState({ writing: true });
      const pending = usePreferencesStore.getState().saveTutorialProgress(progress);
      const rejected = expect(pending).rejects.toThrow(/try again/i);
      await vi.advanceTimersByTimeAsync(5000);
      await rejected;
      expect(mockUpdate).not.toHaveBeenCalled();
      expect(usePreferencesStore.getState().writing).toBe(true);
    } finally { vi.useRealTimers(); }
  });

  it("waits for the pending progress response before sending completion", async () => {
    vi.useFakeTimers();
    try {
      let resolveProgress!: (payload: Awaited<ReturnType<typeof fetchUserComposerPreferences>>) => void;
      mockUpdate.mockImplementationOnce(() => new Promise((resolve) => { resolveProgress = resolve; }));
      mockUpdate.mockResolvedValueOnce(completed);
      const savingProgress = usePreferencesStore.getState().saveTutorialProgress(progress);
      const graduating = usePreferencesStore.getState().markTutorialGraduated({ via: "complete" });
      expect(mockUpdate).toHaveBeenCalledTimes(1);
      expect(usePreferencesStore.getState().writing).toBe(true);
      resolveProgress({ ...completed, tutorial_completed_at: null, tutorial_stage: "run", tutorial_session_id: "tutorial-session" });
      await savingProgress;
      await vi.advanceTimersByTimeAsync(50);
      await graduating;
      expect(mockUpdate).toHaveBeenCalledTimes(2);
      expect(mockUpdate.mock.calls[1][0]).toMatchObject({ tutorial_completed_via: "complete" });
      expect(usePreferencesStore.getState().tutorialStage).toBeNull();
      expect(usePreferencesStore.getState().tutorialCompleted).toBe(true);
    } finally { vi.useRealTimers(); }
  });

  it("discards queued progress after deferred-publication completion lands", async () => {
    vi.useFakeTimers();
    try {
      let resolveCompletion!: (payload: Awaited<ReturnType<typeof fetchUserComposerPreferences>>) => void;
      mockUpdate.mockImplementationOnce(() => new Promise((resolve) => { resolveCompletion = resolve; }));
      mockUpdate.mockResolvedValueOnce({ ...completed, tutorial_stage: "run", tutorial_session_id: "tutorial-session" });
      const graduating = usePreferencesStore.getState().markTutorialGraduated({ via: "skip", publishLocally: false });
      const savingProgress = usePreferencesStore.getState().saveTutorialProgress(progress);
      expect(mockUpdate).toHaveBeenCalledTimes(1);
      resolveCompletion(completed);
      await graduating;
      await vi.advanceTimersByTimeAsync(50);
      await savingProgress;
      expect(mockUpdate).toHaveBeenCalledTimes(1);
      expect(usePreferencesStore.getState().tutorialStage).toBeNull();
      expect(usePreferencesStore.getState().tutorialSessionId).toBeNull();
      expect(usePreferencesStore.getState().tutorialCompletedAt).toBe(completed.tutorial_completed_at);
      expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
    } finally { vi.useRealTimers(); }
  });
});
