import { usePreferencesStore } from "@/stores/preferencesStore";
import { useSessionStore } from "@/stores/sessionStore";
import { findGuidedRetry } from "@/stores/guidedOperationRetry";

function assertFreeformHandoff(sessionId: string): void {
  const current = useSessionStore.getState();
  if (current.activeSessionId !== sessionId) {
    throw new Error("The active session changed. Reopen the tutorial session before retrying.");
  }
  if (
    current.guidedSession?.terminal?.kind !== "exited_to_freeform" ||
    current.guidedChatPending || current.guidedResponsePending ||
    findGuidedRetry("guided_reenter", sessionId) !== null ||
    findGuidedRetry("guided_convert", sessionId) !== null ||
    findGuidedRetry("guided_start", sessionId) !== null
  ) {
    throw new Error("The tutorial session is changing mode. Finish the current operation and retry the exit.");
  }
}

/** Keep departure bound to the session the learner actually left. */
export async function departTutorialSession(
  sessionId: string,
  via: "complete" | "exit",
): Promise<void> {
  const current = useSessionStore.getState();
  if (current.activeSessionId !== sessionId) {
    throw new Error("The active session changed. Reopen the tutorial session before retrying.");
  }
  if (current.guidedSession === null) {
    throw new Error(current.error ?? "The tutorial session has not loaded. Reload it before retrying the exit.");
  }
  const terminal = current.guidedSession?.terminal?.kind;
  if (terminal == null || terminal === "completed") {
    const outcome = await current.exitToFreeform();
    if (outcome.status !== "applied") {
      throw new Error(outcome.message);
    }
  }
  assertFreeformHandoff(sessionId);
  const completedAt = await usePreferencesStore.getState().markTutorialGraduated({
    via,
    publishLocally: false,
  });
  assertFreeformHandoff(sessionId);
  usePreferencesStore.getState().publishTutorialGraduation(completedAt);
}
