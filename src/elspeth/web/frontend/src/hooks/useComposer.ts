// src/hooks/useComposer.ts
import { useCallback } from "react";
import { useSessionStore } from "@/stores/sessionStore";

/**
 * All authoring surfaces share the session store's request lifecycle and
 * cancellation owner. Unmounting the originating surface does not remove
 * Stop's authority over the active session's request.
 */
export function useComposer() {
  const composeRequest = useSessionStore((s) => s.composeRequest);
  const cancelComposition = useSessionStore((s) => s.cancelComposition);
  const isComposing = useSessionStore((s) => s.isComposing);
  const compositionState = useSessionStore((s) => s.compositionState);
  const error = useSessionStore((s) => s.error);
  const errorDetails = useSessionStore((s) => s.errorDetails);
  const sendMessage = useCallback(
    (content: string) => composeRequest("send", content),
    [composeRequest],
  );

  const retryMessage = useCallback(
    (messageId: string) => composeRequest("retry", messageId),
    [composeRequest],
  );

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
