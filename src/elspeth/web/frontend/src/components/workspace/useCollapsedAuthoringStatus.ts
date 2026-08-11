import { useLayoutEffect, useRef } from "react";

import { usePendingAcknowledgements } from "@/components/chat/AcknowledgementStack";
import { useSessionStore } from "@/stores/sessionStore";

export interface CollapsedAuthoringStatus {
  text: string;
  tone: "neutral" | "busy" | "error";
}

interface UseCollapsedAuthoringStatusOptions {
  activeSessionId: string | null;
  authoringCollapsed: boolean;
}

interface UnreadBaseline {
  sessionId: string | null;
  messageSequence: number;
  acknowledgementCount: number;
}

function plural(count: number, singular: string, pluralForm: string): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

/**
 * Presentation-only projection for the collapsed authoring affordance.
 *
 * It observes existing Composer state and records only an in-memory UI
 * baseline. It does not own, advance, acknowledge, cancel, or retry any
 * authoring operation.
 */
export function useCollapsedAuthoringStatus({
  activeSessionId,
  authoringCollapsed,
}: UseCollapsedAuthoringStatusOptions): CollapsedAuthoringStatus {
  const messages = useSessionStore((state) => state.messages);
  const guidedTurnSequence = useSessionStore(
    (state) => state.guidedSession?.chat_turn_seq ?? 0,
  );
  const isComposing = useSessionStore((state) => state.isComposing);
  const guidedChatPending = useSessionStore(
    (state) => state.guidedChatPending,
  );
  const guidedResponsePending = useSessionStore(
    (state) => state.guidedResponsePending,
  );
  const error = useSessionStore((state) => state.error);
  const pendingAcknowledgements = usePendingAcknowledgements(
    activeSessionId ?? "",
  );
  const messageSequence = messages.length + guidedTurnSequence;
  const acknowledgementCount = pendingAcknowledgements.length;
  const baselineRef = useRef<UnreadBaseline>({
    sessionId: activeSessionId,
    messageSequence,
    acknowledgementCount,
  });

  useLayoutEffect(() => {
    if (
      !authoringCollapsed ||
      baselineRef.current.sessionId !== activeSessionId
    ) {
      baselineRef.current = {
        sessionId: activeSessionId,
        messageSequence,
        acknowledgementCount,
      };
    }
  }, [
    acknowledgementCount,
    activeSessionId,
    authoringCollapsed,
    messageSequence,
  ]);

  if (error !== null) {
    return { text: `Authoring error: ${error}`, tone: "error" };
  }
  if (isComposing || guidedChatPending || guidedResponsePending) {
    return { text: "Authoring in progress", tone: "busy" };
  }

  const baseline = baselineRef.current;
  if (authoringCollapsed && baseline.sessionId === activeSessionId) {
    const newMessages = Math.max(0, messageSequence - baseline.messageSequence);
    const newAcknowledgements = Math.max(
      0,
      acknowledgementCount - baseline.acknowledgementCount,
    );
    const unreadParts: string[] = [];
    if (newMessages > 0) {
      unreadParts.push(plural(newMessages, "new message", "new messages"));
    }
    if (newAcknowledgements > 0) {
      unreadParts.push(
        plural(
          newAcknowledgements,
          "decision to acknowledge",
          "decisions to acknowledge",
        ),
      );
    }
    if (unreadParts.length > 0) {
      return { text: unreadParts.join(", "), tone: "busy" };
    }
  }

  return { text: "Authoring pane collapsed", tone: "neutral" };
}
