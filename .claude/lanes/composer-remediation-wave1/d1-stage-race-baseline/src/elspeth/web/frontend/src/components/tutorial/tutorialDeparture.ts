import { capturePreferenceOwner, requirePreferenceOwner, usePreferencesStore } from "@/stores/preferencesStore";
import { useSessionStore } from "@/stores/sessionStore";

function assertLoadedFreeformSession(sessionId: string): void {
  const current = useSessionStore.getState();
  if (current.activeSessionId !== sessionId) {
    throw new Error("The active session changed. Reopen the tutorial session before retrying.");
  }
  if (!current.compositionStateLoaded || current.error !== null) {
    throw new Error(current.error ?? "The tutorial session has not loaded. Reload it before retrying.");
  }
  if (current.isComposing || current.proposalActionPendingIds.length > 0) {
    throw new Error("Wait for the current Composer operation before leaving the tutorial.");
  }
}

/** Persist completion only while the same loaded freeform session is in view. */
export async function departTutorialSession(
  sessionId: string,
  via: "complete" | "exit",
  ownsLifecycle: () => boolean,
): Promise<void> {
  const ownsPreferences = capturePreferenceOwner();
  const requireCurrent = () => {
    requirePreferenceOwner(ownsPreferences);
    requirePreferenceOwner(ownsLifecycle);
  };
  requireCurrent();
  assertLoadedFreeformSession(sessionId);
  const completion = await usePreferencesStore.getState().markTutorialGraduated({
    via,
    publishLocally: false,
  });
  requireCurrent();
  assertLoadedFreeformSession(sessionId);
  requireCurrent();
  completion.publish();
}
