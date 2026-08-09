// src/hooks/useComposer.ts
import { useCallback, useRef } from "react";
import { useSessionStore } from "@/stores/sessionStore";
import {
  COMPOSE_USER_CANCEL_ABORT_REASON,
  runComposeWithTimeout,
} from "@/config/composer";

/**
 * Hook for composing messages. Wraps sessionStore.sendMessage()
 * with an AbortController timeout. Dispatches error messages
 * based on HTTP status and error_type field.
 *
 * The AbortController is wired to abort the underlying fetch when the
 * timeout fires. Because abort() is given a bare-string reason, the
 * in-flight fetch rejects with that raw string (not a DOMException);
 * sessionStore classifies it and maps it to the user-facing copy.
 */
export function useComposer() {
  const storeSendMessage = useSessionStore((s) => s.sendMessage);
  const storeRetryMessage = useSessionStore((s) => s.retryMessage);
  const isComposing = useSessionStore((s) => s.isComposing);
  const compositionState = useSessionStore((s) => s.compositionState);
  const error = useSessionStore((s) => s.error);
  const errorDetails = useSessionStore((s) => s.errorDetails);
  // Single source of truth for the bootstrap-race gate; passed to the shared
  // primitive so freeform and guided (ChatPanel.sendGuidedChat) read the same
  // readiness signal.
  const composeTimeoutReady = useSessionStore((s) => s.composeTimeoutReady);
  const activeControllerRef = useRef<AbortController | null>(null);

  // Delegates to the shared compose-timeout primitive so freeform and guided
  // (ChatPanel.sendGuidedChat) sends share ONE timer + readiness guard and
  // cannot drift apart. The guard means a send started before the backend
  // wall clock has landed (bootstrap window) does not run at all — the Send
  // affordance is disabled until readiness, so this only backstops
  // programmatic callers (SideRailValidationBanner).
  const runWithTimeout = useCallback(
    (runner: (signal: AbortSignal) => Promise<void>) =>
      runComposeWithTimeout(activeControllerRef, composeTimeoutReady, runner),
    [composeTimeoutReady],
  );

  // Admission pre-check (elspeth-3f38ebb1b5): the store's synchronous gate
  // refuses a second freeform compose, but the refusal must also never touch
  // activeControllerRef — runComposeWithTimeout installs (then clears) a
  // fresh controller before the store can refuse, which would leave Stop
  // owning no controller while the first compose still runs.
  const sendMessage = useCallback(
    async (content: string) => {
      if (useSessionStore.getState().isComposing) return;
      await runWithTimeout((signal) => storeSendMessage(content, signal));
    },
    [runWithTimeout, storeSendMessage],
  );

  const retryMessage = useCallback(
    async (messageId: string) => {
      if (useSessionStore.getState().isComposing) return;
      await runWithTimeout((signal) => storeRetryMessage(messageId, signal));
    },
    [runWithTimeout, storeRetryMessage],
  );

  const cancelComposition = useCallback(() => {
    activeControllerRef.current?.abort(COMPOSE_USER_CANCEL_ABORT_REASON);
  }, []);

  return {
    sendMessage,
    retryMessage,
    cancelComposition,
    isComposing,
    compositionState,
    error,
    errorDetails,
  };
}
