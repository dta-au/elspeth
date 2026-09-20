// Account-level preferences. Completion persistence and local publication are separate.

import { create } from "zustand";
import {
  fetchUserComposerPreferences,
  updateUserComposerPreferences,
} from "@/api/client";
import type {
  ApiError,
  ComposerMode,
  PersistedTutorialStage,
  UserComposerPreferencesPayload,
} from "@/types/api";

const FREEFORM_INTRO_DISMISSED_STORAGE_KEY =
  "elspeth_prefs_freeform_intro_dismissed_v1";

/**
 * In-progress tutorial resume state (elspeth-918f4434b3), server-persisted
 * on every stage transition so a reload resumes at the persisted stage with
 * the SAME session instead of restarting at Welcome (and silently
 * re-spending LLM budget). All four are null when no tutorial is in
 * progress; runId/sourceDataHash appear once the tutorial run completes.
 */
export interface TutorialProgress {
  stage: PersistedTutorialStage | null;
  sessionId: string | null;
  runId: string | null;
  sourceDataHash: string | null;
}

interface PreferencesState {
  defaultMode: ComposerMode | null;
  freeformIntroDismissedAt: string | null;
  tutorialCompletedAt: string | null;
  tutorialCompleted: boolean;
  tutorialStage: PersistedTutorialStage | null;
  tutorialSessionId: string | null;
  tutorialRunId: string | null;
  tutorialSourceDataHash: string | null;
  // Detail level (elspeth-9c11df65f8). false = standard view. Read it ONLY
  // through useShowAdvanced()/selectShowAdvanced so consumers cannot drift.
  showAdvanced: boolean;
  loaded: boolean;
  writing: boolean;
  // Most-recent error from a setDefaultMode or dismiss call. Components
  // render this as an accessible role="alert" region (Panel a11y F2).
  // Cleared on the next successful write or by explicit clearError().
  writeError: string | null;
  bootstrapError: string | null;
  bootstrap: () => Promise<void>;
  saveTutorialProgress: (progress: TutorialProgress) => Promise<void>;
  resolveDefaultMode: () => Promise<ComposerMode>;
  setDefaultMode: (mode: ComposerMode) => Promise<void>;
  setShowAdvanced: (value: boolean) => Promise<void>;
  markTutorialGraduated: (options: {
    publishLocally?: boolean;
    via: "complete" | "skip" | "exit";
  }) => Promise<string | null>;
  publishTutorialGraduation: (completedAt: string | null) => void;
  resetTutorial: () => Promise<void>;
  dismissFreeformIntro: () => Promise<void>;
  clearError: () => void;
  reset: () => void;
}

function tutorialCompletedFrom(value: string | null): boolean {
  return value !== null;
}

// Rate-limited ApiError guard (same per-module pattern as
// RunOutputsPanel.tsx / shareableReviewStore.ts): a thrown ApiError is a
// plain object, never `instanceof Error`, so discriminate on `status`.
function isRateLimitedApiError(
  err: unknown,
): err is { status: number; detail: string; retry_after?: number } {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { status?: unknown }).status === 429
  );
}

// Keep rate-limit sleeps shorter than the serialization wait. Longer server
// retry intervals surface immediately as actionable errors.
const MAX_RETRY_AFTER_WAIT_MS = 3_000;

const INITIAL_STATE = {
  defaultMode: null as ComposerMode | null,
  freeformIntroDismissedAt: null as string | null,
  tutorialCompletedAt: null as string | null,
  tutorialCompleted: false,
  tutorialStage: null as PersistedTutorialStage | null,
  tutorialSessionId: null as string | null,
  tutorialRunId: null as string | null,
  tutorialSourceDataHash: null as string | null,
  showAdvanced: false,
  loaded: false,
  writing: false,
  writeError: null as string | null,
  bootstrapError: null as string | null,
};

export const usePreferencesStore = create<PreferencesState>((set, get) => ({
  ...INITIAL_STATE,

  bootstrap: async () => {
    try {
      const payload = await fetchUserComposerPreferences();
      set({
        defaultMode: payload.default_mode,
        freeformIntroDismissedAt: payload.freeform_intro_dismissed_at,
        tutorialCompletedAt: payload.tutorial_completed_at,
        tutorialCompleted: tutorialCompletedFrom(payload.tutorial_completed_at),
        tutorialStage: payload.tutorial_stage,
        tutorialSessionId: payload.tutorial_session_id,
        tutorialRunId: payload.tutorial_run_id,
        tutorialSourceDataHash: payload.tutorial_source_data_hash,
        showAdvanced: payload.show_advanced,
        loaded: true,
        bootstrapError: null,
      });
    } catch (err) {
      // No-fabrication shape: an absent value stays null rather than
      // coerced to a default, because absence is evidence. Leave
      // defaultMode and tutorialCompletedAt at null — we genuinely
      // don't know what they were. Setting defaultMode="guided" here
      // would attribute a preference choice to the user that they
      // never made; the audit trail would later carry a confident
      // answer to a question the system never resolved.
      //
      // Set loaded:true so the UI unblocks (gating the whole UI on
      // loaded would leave a corrupt-row user unable to create a
      // session at all — strictly worse than presenting them with an
      // accurate "we couldn't load your preferences" banner).
      //
      // resolveDefaultMode() continues to throw when defaultMode is
      // null after a bootstrap pass; sessionStore.createSession()
      // catches that and presents the "couldn't apply your default
      // mode, you're in freeform" message — honest about not knowing.
      const apiError = err as Partial<ApiError>;
      const isCorrupt = apiError?.error_type === "corrupt_preferences";
      const message = isCorrupt
        ? "Your saved preferences are corrupted. Contact your administrator to restore them."
        : err instanceof Error
          ? `Couldn't load your preferences (${err.message}).`
          : "Couldn't load your preferences.";
      set({
        loaded: true,
        bootstrapError: message,
      });
    }
  },

  saveTutorialProgress: async (progress) => {
    // Completion clears these same fields, so progress participates in the
    // write lock. A queued transition becomes obsolete once completion lands,
    // even when its local publication is intentionally deferred.
    for (let waitedMs = 0; get().writing && waitedMs < 5000; waitedMs += 50) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    if (get().writing) {
      const error = new Error("Another preference save is still pending. Please try again.");
      set({ writeError: error.message });
      throw error;
    }
    if (get().tutorialCompletedAt !== null) return;
    set({ writing: true, writeError: null });
    try {
      const payload = await updateUserComposerPreferences({
        tutorial_stage: progress.stage,
        tutorial_session_id: progress.sessionId,
        tutorial_run_id: progress.runId,
        tutorial_source_data_hash: progress.sourceDataHash,
      });
      set({
        tutorialStage: payload.tutorial_stage,
        tutorialSessionId: payload.tutorial_session_id,
        tutorialRunId: payload.tutorial_run_id,
        tutorialSourceDataHash: payload.tutorial_source_data_hash,
        writing: false,
        writeError: null,
      });
    } catch (err) {
      set({
        writing: false,
        writeError: err instanceof Error
          ? `Couldn't save tutorial progress: ${err.message}`
          : "Couldn't save tutorial progress.",
      });
      throw err;
    }
  },

  resolveDefaultMode: async () => {
    const current = get();
    if (current.loaded) {
      // bootstrap has already run. If defaultMode is null at this point,
      // bootstrap failed (bootstrapError is set) and a second bootstrap pass
      // would just re-fail against the same broken backend. Throw
      // immediately so sessionStore.createSession surfaces the honest
      // secondary-failure attribution to the user without an extra
      // round-trip.
      if (current.defaultMode === null) {
        throw new Error(
          "preferencesStore: loaded=true but defaultMode is null — bootstrap failed or backend returned a null default_mode (contract violation)",
        );
      }
      return current.defaultMode;
    }
    await get().bootstrap();
    const after = get();
    if (after.defaultMode === null) {
      // bootstrap resolved without populating defaultMode — backend contract
      // violation (Phase 1A's GET always returns a row, defaulting to freeform).
      throw new Error(
        "preferencesStore: bootstrap completed but defaultMode is null",
      );
    }
    return after.defaultMode;
  },

  setDefaultMode: async (mode) => {
    if (get().writing) return;
    const previous = get().defaultMode;
    set({ defaultMode: mode, writing: true, writeError: null });
    try {
      const payload = await updateUserComposerPreferences({
        default_mode: mode,
      });
      set({
        defaultMode: payload.default_mode,
        writing: false,
      });
    } catch (err) {
      set({
        defaultMode: previous,
        writing: false,
        writeError:
          err instanceof Error
            ? `Couldn't save your preference: ${err.message}`
            : "Couldn't save your preference.",
      });
      throw err;
    }
  },

  setShowAdvanced: async (value) => {
    if (get().writing) return;
    const previous = get().showAdvanced;
    set({ showAdvanced: value, writing: true, writeError: null });
    try {
      const payload = await updateUserComposerPreferences({ show_advanced: value });
      set({ showAdvanced: payload.show_advanced, writing: false });
    } catch (err) {
      set({
        showAdvanced: previous,
        writing: false,
        writeError:
          err instanceof Error
            ? `Couldn't save your preference: ${err.message}`
            : "Couldn't save your preference.",
      });
      throw err;
    }
  },

  markTutorialGraduated: async (options) => {
    // Wait for the current writer, then re-read the durable timestamp. A
    // timeout must not overlap writes or report an unsaved completion.
    for (let waitedMs = 0; get().writing && waitedMs < 5000; waitedMs += 50) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    if (get().writing) {
      const error = new Error("Another preference save is still pending. Please try again.");
      set({ writeError: error.message });
      throw error;
    }
    const settled = get();
    const publishLocally = options.publishLocally ?? true;
    if (settled.tutorialCompletedAt !== null) {
      set({ writeError: null });
      if (publishLocally) get().publishTutorialGraduation(settled.tutorialCompletedAt);
      return settled.tutorialCompletedAt;
    }
    const stamp = new Date().toISOString();
    const previous = {
      tutorialCompletedAt: get().tutorialCompletedAt,
      tutorialCompleted: get().tutorialCompleted,
    };
    set({
      writing: true,
      writeError: null,
    });
    const patchBody = {
      default_mode: "freeform" as const,
      tutorial_completed_at: stamp,
      tutorial_completed_via: options.via,
    };
    try {
      let payload: UserComposerPreferencesPayload;
      try {
        payload = await updateUserComposerPreferences(patchBody);
      } catch (err) {
        // One delayed retry for a rate-limited save: the tutorial's own
        // stage-persist burst can transiently exhaust the write bucket,
        // and completion is the one write that must not be dropped (it
        // gates whether the tutorial re-shows on next load). `writing`
        // stays true across the wait so the graduation card's busy state
        // ("Saving tutorial completion") stays honest.
        if (!isRateLimitedApiError(err) || err.retry_after === undefined) {
          throw err;
        }
        const waitMs = err.retry_after * 1000;
        if (waitMs > MAX_RETRY_AFTER_WAIT_MS) {
          // Too long to hold `writing` — fail fast instead of sleeping.
          throw err;
        }
        await new Promise((resolve) => setTimeout(resolve, waitMs));
        payload = await updateUserComposerPreferences(patchBody);
      }
      set({
        defaultMode: payload.default_mode,
        tutorialCompletedAt: payload.tutorial_completed_at,
        tutorialCompleted: publishLocally && tutorialCompletedFrom(payload.tutorial_completed_at),
        // Completion-clears-progress (backend rule): the server just
        // terminated any in-progress resume state; mirror it. Safe to
        // publish even when publishLocally=false — the resume fields do
        // not drive the tutorial-vs-composer gate.
        tutorialStage: payload.tutorial_stage,
        tutorialSessionId: payload.tutorial_session_id,
        tutorialRunId: payload.tutorial_run_id,
        tutorialSourceDataHash: payload.tutorial_source_data_hash,
        writing: false,
        writeError: null,
      });
      return payload.tutorial_completed_at;
    } catch (err) {
      set({
        tutorialCompletedAt: previous.tutorialCompletedAt,
        tutorialCompleted: previous.tutorialCompleted,
        writing: false,
        // Surface the ApiError envelope detail too: a thrown ApiError is a
        // plain object, not `instanceof Error`, and the bare fallback hid
        // the actionable "try again in N seconds" message.
        writeError:
          err instanceof Error
            ? `Couldn't save tutorial completion: ${err.message}`
            : typeof err === "object" && err !== null && typeof (err as { detail?: unknown }).detail === "string"
              ? `Couldn't save tutorial completion: ${(err as { detail: string }).detail}`
              : "Couldn't save tutorial completion.",
      });
      throw err;
    }
  },

  publishTutorialGraduation: (completedAt) => {
    set({
      tutorialCompletedAt: completedAt,
      tutorialCompleted: tutorialCompletedFrom(completedAt),
    });
  },

  resetTutorial: async () => {
    if (get().writing) return;
    const previous = {
      tutorialCompletedAt: get().tutorialCompletedAt,
      tutorialCompleted: get().tutorialCompleted,
    };
    set({
      writing: true,
      writeError: null,
    });
    try {
      const payload = await updateUserComposerPreferences({
        tutorial_completed_at: null,
        // Explicitly clear the resume fields too. The completion-clears-
        // progress rule covers the graduated path, but Reset is now also
        // offered MID-tutorial (the wedged-resume escape hatch) — there the
        // stale stage/session must not survive the reset, or the next load
        // resumes straight back into the state being escaped.
        tutorial_stage: null,
        tutorial_session_id: null,
        tutorial_run_id: null,
        tutorial_source_data_hash: null,
      });
      set({
        defaultMode: payload.default_mode,
        tutorialCompletedAt: payload.tutorial_completed_at,
        tutorialCompleted: tutorialCompletedFrom(payload.tutorial_completed_at),
        // A retake restarts cleanly at Welcome: the reset PATCH also
        // cleared any lingering resume state server-side
        // (completion-clears-progress rule); mirror it locally.
        tutorialStage: payload.tutorial_stage,
        tutorialSessionId: payload.tutorial_session_id,
        tutorialRunId: payload.tutorial_run_id,
        tutorialSourceDataHash: payload.tutorial_source_data_hash,
        writing: false,
      });
    } catch (err) {
      set({
        tutorialCompletedAt: previous.tutorialCompletedAt,
        tutorialCompleted: previous.tutorialCompleted,
        writing: false,
        writeError:
          err instanceof Error
            ? `Couldn't reset the tutorial: ${err.message}`
            : "Couldn't reset the tutorial.",
      });
      throw err;
    }
  },

  dismissFreeformIntro: async () => {
    if (get().writing) {
      throw new Error(
        "preferencesStore: dismissFreeformIntro called while a write was in flight",
      );
    }
    const stamp = new Date().toISOString();
    set({ writing: true, writeError: null });
    try {
      const payload = await updateUserComposerPreferences({
        freeform_intro_dismissed_at: stamp,
      });
      const resolved = payload.freeform_intro_dismissed_at;
      set({
        freeformIntroDismissedAt: resolved,
        writing: false,
      });
      if (typeof window !== "undefined" && resolved !== null) {
        try {
          window.localStorage.setItem(
            FREEFORM_INTRO_DISMISSED_STORAGE_KEY,
            resolved,
          );
        } catch {
          // The account-level server value remains authoritative; peer tabs
          // catch up on their next preference bootstrap.
        }
      }
    } catch (err) {
      set({
        writing: false,
        writeError:
          err instanceof Error
            ? `Couldn't hide the freeform introduction: ${err.message}`
            : "Couldn't hide the freeform introduction.",
      });
      throw err;
    }
  },

  clearError: () => set({ writeError: null }),

  reset: () => set(INITIAL_STATE),
}));

// ── Cross-tab sync wiring ────────────────────────────────────────────────
// Idempotent at-most-once subscription, attached at module load. Mirrors
// the useTheme storage-event pattern for freeform-introduction dismissal.
// Guarded against re-execution (e.g. HMR) and SSR (no window).
let crossTabSyncInitialised = false;

export function initCrossTabSync(): void {
  if (crossTabSyncInitialised) return;
  if (typeof window === "undefined") return;
  crossTabSyncInitialised = true;

  window.addEventListener("storage", (event: StorageEvent) => {
    if (event.newValue === null) return;
    if (event.key === FREEFORM_INTRO_DISMISSED_STORAGE_KEY) {
      usePreferencesStore.setState({
        freeformIntroDismissedAt: event.newValue,
      });
      return;
    }
  });
}

// Auto-initialise on module load in browser environments. SSR/test
// environments without window are gracefully skipped.
initCrossTabSync();

export function selectTutorialCompleted(state: PreferencesState): boolean {
  return state.tutorialCompleted;
}

export function selectShowAdvanced(state: PreferencesState): boolean {
  return state.showAdvanced;
}

/** The single consumer entry point for the detail-level flag. */
export function useShowAdvanced(): boolean {
  return usePreferencesStore(selectShowAdvanced);
}
