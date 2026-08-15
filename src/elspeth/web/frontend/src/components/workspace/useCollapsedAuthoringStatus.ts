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
  /** True only when this baseline was captured AFTER the session's message
   *  history hydrated (compositionStateLoaded). A pre-hydration baseline is
   *  measured against an empty store, so counting deltas from it would
   *  report the entire fetched transcript as "new messages" on a
   *  refresh-while-collapsed (2026-08-15). */
  historyLoaded: boolean;
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
  // selectSession sets this false when it clears the store for a new
  // selection and true in the SAME set() that lands the fetched history, so
  // it marks the hydration boundary for messages and guided state alike.
  const historyLoaded = useSessionStore(
    (state) => state.compositionStateLoaded,
  );
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
    historyLoaded,
  });

  useLayoutEffect(() => {
    // Re-baseline until a baseline is captured (a) while collapsed, (b) for
    // this session, AND (c) after history hydration — only such a baseline
    // measures activity the user has actually missed. The !historyLoaded arm
    // keeps the pre-hydration baseline tracking the (empty) store so the
    // hydration set() itself never reads as unread activity.
    if (
      !authoringCollapsed ||
      baselineRef.current.sessionId !== activeSessionId ||
      !baselineRef.current.historyLoaded
    ) {
      baselineRef.current = {
        sessionId: activeSessionId,
        messageSequence,
        acknowledgementCount,
        historyLoaded,
      };
    }
  }, [
    acknowledgementCount,
    activeSessionId,
    authoringCollapsed,
    historyLoaded,
    messageSequence,
  ]);

  if (error !== null) {
    return { text: `Authoring error: ${error}`, tone: "error" };
  }
  if (isComposing || guidedChatPending || guidedResponsePending) {
    return { text: "Authoring in progress", tone: "busy" };
  }

  const baseline = baselineRef.current;
  // baseline.historyLoaded (not just historyLoaded): the hydration render
  // itself still carries the PRE-hydration baseline — the layout effect
  // above re-baselines only after this render, and a ref update triggers no
  // re-render, so trusting the live flag here would flash the whole
  // transcript as unread for exactly one frame and then stick.
  if (
    authoringCollapsed &&
    baseline.sessionId === activeSessionId &&
    baseline.historyLoaded
  ) {
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
