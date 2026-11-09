// Account-level preferences. Completion persistence and local publication are separate.

import { create } from "zustand";
import { currentAuthGeneration, isCurrentAuthGeneration } from "@/api/authSession";
import {
  fetchUserComposerPreferences,
  updateUserComposerPreferences,
} from "@/api/client";
import type {
  ApiError,
  PersistedTutorialStage,
  UserComposerPreferencesPayload,
} from "@/types/api";

const FREEFORM_INTRO_DISMISSED_STORAGE_KEY =
  "elspeth_prefs_freeform_intro_dismissed_v2";

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

/** The caller may publish this completion only while its saved account view
 * still owns the result. A deferred tutorial departure carries this receipt
 * across subsequent session-loading awaits. */
export interface PreferenceWriteReceipt {
  assertCurrent: () => void;
}

export interface TutorialGraduationReceipt extends PreferenceWriteReceipt {
  completedAt: string | null;
  publish: () => void;
}

export class PreferencesRequestSuperseded extends Error {
  constructor() {
    super("The account changed while saving preferences. Reload before retrying.");
  }
}

let preferenceRevision = 0;
let publicationRevision = 0;
let bootstrapIntent = 0;

// Principal keys namespace cross-tab hints; the auth generation and revision
// own async work. Backend live authorization remains the authority.
let preferencePrincipal: string | null = null;

export function capturePreferenceOwner(): () => boolean {
  const revision = preferenceRevision;
  const authGeneration = currentAuthGeneration();
  return () => revision === preferenceRevision && isCurrentAuthGeneration(authGeneration);
}

export function requirePreferenceOwner(owns: () => boolean): void {
  if (!owns()) throw new PreferencesRequestSuperseded();
}

function captureRequestOwner(): () => boolean {
  const ownsScope = capturePreferenceOwner();
  const request = ++publicationRevision;
  return () => ownsScope() && request === publicationRevision;
}

function isUnavailableForRole(err: unknown): boolean {
  if (typeof err !== "object" || err === null) return false;
  const apiError = err as Partial<ApiError>;
  return apiError.status === 403 && apiError.error_type === "user_role_required";
}

interface PreferencesState {
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
  // A 403 user_role_required is an expected composer boundary for an
  // authenticated administrator. It is not a loaded preference or a failed
  // persistence attempt.
  unavailableForRole: boolean;
  writing: boolean;
  // Most-recent error from a preference write. Components
  // render this as an accessible role="alert" region (Panel a11y F2).
  // Cleared on the next successful write or by explicit clearError().
  writeError: string | null;
  bootstrapError: string | null;
  bootstrap: (options?: { publishTutorialCompletion?: boolean }) => Promise<void>;
  saveTutorialProgress: (progress: TutorialProgress) => Promise<void>;
  setShowAdvanced: (value: boolean) => Promise<void>;
  markTutorialGraduated: (options: {
    publishLocally?: boolean;
    via: "complete" | "skip" | "exit";
  }) => Promise<TutorialGraduationReceipt>;
  resetTutorial: () => Promise<PreferenceWriteReceipt>;
  dismissFreeformIntro: () => Promise<void>;
  clearError: () => void;
  bindPrincipal: (principalId: string, authProvider: string) => void;
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
  freeformIntroDismissedAt: null as string | null,
  tutorialCompletedAt: null as string | null,
  tutorialCompleted: false,
  tutorialStage: null as PersistedTutorialStage | null,
  tutorialSessionId: null as string | null,
  tutorialRunId: null as string | null,
  tutorialSourceDataHash: null as string | null,
  showAdvanced: false,
  loaded: false,
  unavailableForRole: false,
  writing: false,
  writeError: null as string | null,
  bootstrapError: null as string | null,
};

export const usePreferencesStore = create<PreferencesState>((set, get) => ({
  ...INITIAL_STATE,

  bootstrap: async (options) => {
    const ownsScope = capturePreferenceOwner();
    const intent = ++bootstrapIntent;
    // Read after the current writer settles, so a refresh cannot discard a
    // legitimate save or replace its state with a pre-write GET snapshot.
    for (let waitedMs = 0; get().writing && waitedMs < 5000; waitedMs += 50) {
      await new Promise((resolve) => setTimeout(resolve, 50));
      if (!ownsScope() || intent !== bootstrapIntent) return;
    }
    if (!ownsScope() || intent !== bootstrapIntent) return;
    if (get().writing) {
      set({ bootstrapError: "A preference save is still pending. Reload preferences after it finishes." });
      return;
    }
    const ownsRequest = captureRequestOwner();
    const owns = () => ownsRequest() && intent === bootstrapIntent;
    try {
      const payload = await fetchUserComposerPreferences();
      if (!owns()) return;
      set({
        freeformIntroDismissedAt: payload.freeform_intro_dismissed_at,
        tutorialCompletedAt: payload.tutorial_completed_at,
        tutorialCompleted: (options?.publishTutorialCompletion ?? true)
          ? tutorialCompletedFrom(payload.tutorial_completed_at)
          : get().tutorialCompleted && tutorialCompletedFrom(payload.tutorial_completed_at),
        tutorialStage: payload.tutorial_stage,
        tutorialSessionId: payload.tutorial_session_id,
        tutorialRunId: payload.tutorial_run_id,
        tutorialSourceDataHash: payload.tutorial_source_data_hash,
        showAdvanced: payload.show_advanced,
        loaded: true,
        unavailableForRole: false,
        bootstrapError: null,
      });
    } catch (err) {
      if (!owns()) return;
      const apiError = err as Partial<ApiError>;
      if (isUnavailableForRole(err)) {
        // R8 forbids an admin from also holding the pipeline-user role. Keep
        // the backend refusal and clear any previous principal's local view.
        preferenceRevision += 1;
        set({ ...INITIAL_STATE, unavailableForRole: true });
        return;
      }
      // No-fabrication shape: leave tutorialCompletedAt null because
      // absence is evidence, not a completion verdict.
      //
      // Set loaded:true so the UI unblocks (gating the whole UI on
      // loaded would leave a corrupt-row user unable to create a
      // session at all — strictly worse than presenting them with an
      // accurate "we couldn't load your preferences" banner).
      //
      const isCorrupt = apiError?.error_type === "corrupt_preferences";
      const message = isCorrupt
        ? "Your saved preferences are corrupted. Contact your administrator to restore them."
        : err instanceof Error
          ? `Couldn't load your preferences (${err.message}).`
          : "Couldn't load your preferences.";
      set({
        loaded: true,
        unavailableForRole: false,
        bootstrapError: message,
      });
    }
  },

  saveTutorialProgress: async (progress) => {
    let owns = capturePreferenceOwner();
    if (get().unavailableForRole) throw new PreferencesRequestSuperseded();
    // Completion clears these same fields, so progress participates in the
    // write lock. A queued transition becomes obsolete once completion lands,
    // even when its local publication is intentionally deferred.
    for (let waitedMs = 0; get().writing && waitedMs < 5000; waitedMs += 50) {
      await new Promise((resolve) => setTimeout(resolve, 50));
      requirePreferenceOwner(owns);
    }
    requirePreferenceOwner(owns);
    if (get().writing) {
      const error = new Error("Another preference save is still pending. Please try again.");
      set({ writeError: error.message });
      throw error;
    }
    if (get().tutorialCompletedAt !== null) return;
    owns = captureRequestOwner();
    set({ writing: true, writeError: null });
    try {
      const payload = await updateUserComposerPreferences({
        tutorial_stage: progress.stage,
        tutorial_session_id: progress.sessionId,
        tutorial_run_id: progress.runId,
        tutorial_source_data_hash: progress.sourceDataHash,
      });
      requirePreferenceOwner(owns);
      set({
        tutorialStage: payload.tutorial_stage,
        tutorialSessionId: payload.tutorial_session_id,
        tutorialRunId: payload.tutorial_run_id,
        tutorialSourceDataHash: payload.tutorial_source_data_hash,
        writing: false,
        writeError: null,
      });
    } catch (err) {
      requirePreferenceOwner(owns);
      if (isUnavailableForRole(err)) {
        preferenceRevision += 1;
        set({ ...INITIAL_STATE, unavailableForRole: true });
        throw new PreferencesRequestSuperseded();
      }
      set({
        writing: false,
        writeError: err instanceof Error
          ? `Couldn't save tutorial progress: ${err.message}`
          : "Couldn't save tutorial progress.",
      });
      throw err;
    }
  },

  setShowAdvanced: async (value) => {
    let owns = capturePreferenceOwner();
    if (get().unavailableForRole) throw new PreferencesRequestSuperseded();
    if (get().writing) throw new Error("Another preference save is still pending. Please try again.");
    const previous = get().showAdvanced;
    owns = captureRequestOwner();
    set({ showAdvanced: value, writing: true, writeError: null });
    try {
      const payload = await updateUserComposerPreferences({ show_advanced: value });
      requirePreferenceOwner(owns);
      set({ showAdvanced: payload.show_advanced, writing: false });
    } catch (err) {
      requirePreferenceOwner(owns);
      if (isUnavailableForRole(err)) {
        preferenceRevision += 1;
        set({ ...INITIAL_STATE, unavailableForRole: true });
        throw new PreferencesRequestSuperseded();
      }
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
    let owns = capturePreferenceOwner();
    if (get().unavailableForRole) throw new PreferencesRequestSuperseded();
    const receipt = (completedAt: string | null): TutorialGraduationReceipt => ({
      completedAt,
      assertCurrent: () => requirePreferenceOwner(owns),
      publish: () => {
        requirePreferenceOwner(owns);
        if (get().tutorialCompletedAt !== completedAt) throw new PreferencesRequestSuperseded();
        set({ tutorialCompletedAt: completedAt, tutorialCompleted: tutorialCompletedFrom(completedAt) });
      },
    });
    // Wait for the current writer, then re-read the durable timestamp. A
    // timeout must not overlap writes or report an unsaved completion.
    for (let waitedMs = 0; get().writing && waitedMs < 5000; waitedMs += 50) {
      await new Promise((resolve) => setTimeout(resolve, 50));
      requirePreferenceOwner(owns);
    }
    requirePreferenceOwner(owns);
    if (get().writing) {
      const error = new Error("Another preference save is still pending. Please try again.");
      set({ writeError: error.message });
      throw error;
    }
    const settled = get();
    const publishLocally = options.publishLocally ?? true;
    if (settled.tutorialCompletedAt !== null) {
      owns = captureRequestOwner();
      set({ writeError: null });
      const currentReceipt = receipt(settled.tutorialCompletedAt);
      if (publishLocally) currentReceipt.publish();
      return currentReceipt;
    }
    const stamp = new Date().toISOString();
    const previous = {
      tutorialCompletedAt: get().tutorialCompletedAt,
      tutorialCompleted: get().tutorialCompleted,
    };
    owns = captureRequestOwner();
    set({
      writing: true,
      writeError: null,
    });
    const patchBody = {
      tutorial_completed_at: stamp,
      tutorial_completed_via: options.via,
    };
    try {
      let payload: UserComposerPreferencesPayload;
      try {
        payload = await updateUserComposerPreferences(patchBody);
      } catch (err) {
        requirePreferenceOwner(owns);
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
        requirePreferenceOwner(owns);
        payload = await updateUserComposerPreferences(patchBody);
      }
      requirePreferenceOwner(owns);
      set({
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
      return receipt(payload.tutorial_completed_at);
    } catch (err) {
      requirePreferenceOwner(owns);
      if (isUnavailableForRole(err)) {
        preferenceRevision += 1;
        set({ ...INITIAL_STATE, unavailableForRole: true });
        throw new PreferencesRequestSuperseded();
      }
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

  resetTutorial: async () => {
    let owns = capturePreferenceOwner();
    if (get().unavailableForRole) throw new PreferencesRequestSuperseded();
    if (get().writing) throw new Error("Another preference save is still pending. Please try again.");
    const previous = {
      tutorialCompletedAt: get().tutorialCompletedAt,
      tutorialCompleted: get().tutorialCompleted,
    };
    owns = captureRequestOwner();
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
      requirePreferenceOwner(owns);
      set({
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
      return { assertCurrent: () => requirePreferenceOwner(owns) };
    } catch (err) {
      requirePreferenceOwner(owns);
      if (isUnavailableForRole(err)) {
        preferenceRevision += 1;
        set({ ...INITIAL_STATE, unavailableForRole: true });
        throw new PreferencesRequestSuperseded();
      }
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
    let owns = capturePreferenceOwner();
    if (get().unavailableForRole) throw new PreferencesRequestSuperseded();
    if (get().writing) {
      throw new Error(
        "preferencesStore: dismissFreeformIntro called while a write was in flight",
      );
    }
    const stamp = new Date().toISOString();
    owns = captureRequestOwner();
    set({ writing: true, writeError: null });
    try {
      const payload = await updateUserComposerPreferences({
        freeform_intro_dismissed_at: stamp,
      });
      requirePreferenceOwner(owns);
      const resolved = payload.freeform_intro_dismissed_at;
      set({
        freeformIntroDismissedAt: resolved,
        writing: false,
      });
      if (typeof window !== "undefined" && resolved !== null && preferencePrincipal !== null) {
        try {
          window.localStorage.setItem(
            `${FREEFORM_INTRO_DISMISSED_STORAGE_KEY}:${preferencePrincipal}`,
            resolved,
          );
        } catch {
          // The account-level server value remains authoritative; peer tabs
          // catch up on their next preference bootstrap.
        }
      }
    } catch (err) {
      requirePreferenceOwner(owns);
      if (isUnavailableForRole(err)) {
        preferenceRevision += 1;
        set({ ...INITIAL_STATE, unavailableForRole: true });
        throw new PreferencesRequestSuperseded();
      }
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

  bindPrincipal: (principalId, authProvider) => {
    preferenceRevision += 1;
    preferencePrincipal = JSON.stringify([authProvider, principalId]);
    set(INITIAL_STATE);
  },

  reset: () => {
    preferenceRevision += 1;
    preferencePrincipal = null;
    set(INITIAL_STATE);
  },
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
    if (preferencePrincipal === null) return;
    if (event.key !== `${FREEFORM_INTRO_DISMISSED_STORAGE_KEY}:${preferencePrincipal}`) return;
    const state = usePreferencesStore.getState();
    if (!state.loaded || state.unavailableForRole) return;
    // Storage is a refresh hint, never an account preference authority. The
    // owned GET discards a late event/read after reset or account replacement.
    void state.bootstrap({ publishTutorialCompletion: false });
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
