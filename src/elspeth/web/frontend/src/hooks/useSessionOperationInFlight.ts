import { useExecutionStore } from "@/stores/executionStore";
import {
  interpretationMutationInFlight,
  useInterpretationEventsStore,
} from "@/stores/interpretationEventsStore";
import { useSessionStore } from "@/stores/sessionStore";

/**
 * Whether a request the client started for `sessionId` may still hold that
 * session's exclusive operation fence server-side. While one does, an
 * execute/run request is refused with 409 "Session operation is already
 * active" (`session_operation_handlers.py`), so every Run control gates on
 * this (#352). Each input is existing in-flight state, not a timer:
 *
 * - `isComposing`: a compose turn holds COMPOSE for its whole operation.
 * - `proposalActionPendingIds`: proposal accept/reject POSTs hold COMPOSE.
 * - interpretation resolve / opt-out POSTs hold COMPOSE until they return;
 *   a refreshAll serviced after their commit can drop the card from the
 *   pending map before that, so the pending count alone is not enough.
 * - `isValidating`: POST /validate takes a shareable BLOB_READ admission, but
 *   its surfacer repair pass escalates to its own COMPOSE lease when the
 *   state has an unsurfaced interpretation site
 *   (`interpretation_surfacing.py`). A resolve mints a new version and the
 *   auto-validate queue fires it synchronously, so this is the post-resolve
 *   request a Run click races.
 */
export function useSessionOperationInFlight(sessionId: string | null): boolean {
  const composing = useSessionStore(
    (s) => sessionId !== null && s.activeSessionId === sessionId && s.isComposing,
  );
  const proposalActionPending = useSessionStore(
    (s) => sessionId !== null && s.activeSessionId === sessionId && s.proposalActionPendingIds.length > 0,
  );
  const interpretationMutation = useInterpretationEventsStore(
    (s) => sessionId !== null && interpretationMutationInFlight(s, sessionId),
  );
  const validating = useExecutionStore((s) => s.isValidating);
  return composing || proposalActionPending || interpretationMutation || validating;
}
