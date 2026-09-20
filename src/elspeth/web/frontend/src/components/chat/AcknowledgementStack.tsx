// ============================================================================
// AcknowledgementStack.tsx — decision-panel rows for LLM-authored
// decisions awaiting acknowledgement.
//
// Unifies BOTH guided and freeform modes onto one surface (the surfaces can
// no longer drift).  Driven by the existing `pendingBySession[sessionId]`
// projection; renders nothing when empty so the conversation is unobstructed
// ("clear it to proceed").
//
//   * Header: "N decisions the LLM made — acknowledge each".
//   * Cards ordered by pipeline step then created_at (stable).
//   * ONE foot-of-stack session opt-out link (reuses the store's optOut
//     action + the shared error mapping + the verbatim ConfirmDialog copy).
//   * The parent decision panel owns the persistent arrival live region.
//
// Behaviour (resolve / amend / 8 KB cap / error mapping) is reused verbatim
// via `useInterpretationResolver` inside each AcknowledgementCard.
// ============================================================================

import { useEffect, useMemo, useRef, useState } from "react";
import type { CompositionState } from "@/types/index";
import type { InterpretationEvent } from "@/types/interpretation";
import { Button } from "@/components/ui";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { AcknowledgementCard } from "./AcknowledgementCard";
import { supportsAmendment } from "./acknowledgementLabels";
import {
  buildStepOrder,
  humaniseStepLabel,
  humaniseStepTitle,
} from "./interpretationStepLabel";
import {
  describeError,
  type DisplayedError,
} from "@/hooks/useInterpretationResolver";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useSessionStore } from "@/stores/sessionStore";

/**
 * The single predicate that defines a "pending acknowledgement": a user-approved
 * interpretation still awaiting the operator's decision.  Shared by the stack
 * (which cards to render), the announcer (the count it reads aloud), and the
 * guided advancement gate so the three can never drift — an announced N that
 * disagreed with the rendered card count would be a fresh defect.
 */
export function isPendingAcknowledgement(event: InterpretationEvent): boolean {
  return (
    event.choice === "pending" &&
    event.interpretation_source === "user_approved"
  );
}

export interface AcknowledgementStackProps {
  sessionId: string;
  /** Restore focus to a stable parent control after the final decision. */
  onFocusFallback?: () => void;
  /** Tutorial passive mode: hide the inline amend escape hatch + opt-out. */
  isTutorial?: boolean;
  /**
   * Fired after a successful per-card resolve (with the resolved event) or a
   * session opt-out (event = null).  The parent uses the event for its own
   * post-resolve UI (e.g. the freeform "Got it…" confirmation bubble).
   */
  onResolved?: (
    newState: CompositionState | null,
    event: InterpretationEvent | null,
  ) => void;
}

/**
 * The session's pending acknowledgements, sorted by pipeline step then
 * created_at (stable). Extracted from the stack's render path so other
 * surfaces (the wire-stage named-blocker panel in ChatPanel) list the SAME
 * cards in the SAME order the stack renders them — a blocker list that
 * disagreed with the stack would be a fresh defect.
 */
export function usePendingAcknowledgements(
  sessionId: string,
): InterpretationEvent[] {
  const pendingBySession = useInterpretationEventsStore(
    (s) => s.pendingBySession,
  );
  const compositionState = useSessionStore((s) => s.compositionState);

  return useMemo(() => {
    const events = Object.values(pendingBySession[sessionId] ?? {}).filter(
      isPendingAcknowledgement,
    );
    const stepOrder = buildStepOrder(compositionState);
    const stepIndexOf = (event: InterpretationEvent): number => {
      const id = event.affected_node_id;
      if (id === null) return Number.POSITIVE_INFINITY;
      const index = stepOrder.get(id);
      return index === undefined ? Number.POSITIVE_INFINITY : index;
    };
    return [...events].sort((a, b) => {
      // Compare step indices directly (subtraction would yield NaN when both
      // are POSITIVE_INFINITY — e.g. no composition loaded yet).
      const stepA = stepIndexOf(a);
      const stepB = stepIndexOf(b);
      if (stepA !== stepB) return stepA < stepB ? -1 : 1;
      const createdDelta = a.created_at.localeCompare(b.created_at);
      return createdDelta !== 0 ? createdDelta : a.id.localeCompare(b.id);
    });
  }, [pendingBySession, sessionId, compositionState]);
}

export function AcknowledgementStack({
  sessionId,
  isTutorial = false,
  onResolved,
  onFocusFallback,
}: AcknowledgementStackProps): JSX.Element | null {
  const compositionState = useSessionStore((s) => s.compositionState);
  const optOut = useInterpretationEventsStore((s) => s.optOut);

  const pending = usePendingAcknowledgements(sessionId);

  const [showOptOutConfirm, setShowOptOutConfirm] = useState(false);
  const [optOutInFlight, setOptOutInFlight] = useState(false);
  const [optOutError, setOptOutError] = useState<DisplayedError | null>(null);

  // ── Focus restoration on per-card resolve ─────────────────────────────────
  //
  // When a card resolves it is dropped from `pending` and unmounts; the
  // browser would otherwise send focus to document.body, stranding a keyboard
  // / SR user at the top of the page.  We capture each card's Acknowledge
  // button and labelled <section> by id, and after a resolve move focus to the
  // NEXT remaining card (its Acknowledge button when enabled, else its section
  // as a fallback for a still-gated prompt-template card).  Focus is moved
  // ONLY on resolve — never on mount/appearance (announce-don't-steal).
  const acceptRefs = useRef(new Map<string, HTMLButtonElement | null>());
  const sectionRefs = useRef(new Map<string, HTMLElement | null>());
  const [focusTargetId, setFocusTargetId] = useState<string | null>(null);

  useEffect(() => {
    if (focusTargetId === null) return;
    const button = acceptRefs.current.get(focusTargetId);
    const section = sectionRefs.current.get(focusTargetId);
    // "Enabled" means BOTH gates are open. The card's view-gate is expressed
    // as aria-disabled + a no-op click rather than native `disabled` (house
    // idiom for a gated primary carrying a reason — ExecuteButton.tsx), so
    // reading `.disabled` alone would land focus on an inert primary. The
    // section fallback is the better target anyway: it announces the card
    // title, and the first control after it is now the "View prompt"
    // prerequisite that opens the gate.
    const buttonInert =
      button == null ||
      button.disabled ||
      button.getAttribute("aria-disabled") === "true";
    if (!buttonInert) {
      button.focus();
    } else if (section != null) {
      section.focus();
    }
    // A single resolve restores focus exactly once.
    setFocusTargetId(null);
  }, [pending, focusTargetId]);

  async function handleConfirmOptOut(): Promise<void> {
    setShowOptOutConfirm(false);
    setOptOutInFlight(true);
    try {
      await optOut(sessionId);
      onResolved?.(null, null);
      onFocusFallback?.();
    } catch (err) {
      setOptOutError(describeError(err));
    } finally {
      setOptOutInFlight(false);
    }
  }

  if (pending.length === 0) return null;

  const count = pending.length;
  const headerText =
    count === 1
      ? "1 decision the LLM made — acknowledge it"
      : `${count} decisions the LLM made — acknowledge each`;

  return (
    <section
      className="ack-stack"
      aria-label="Interpretation approvals"
      data-testid="acknowledgement-stack"
    >
      {/* DecisionPanel owns announcements; arrival never moves focus. */}
      <p className="ack-stack-header">{headerText}</p>

      <ul className="ack-stack-rows">
        {pending.map((event, index) => (
          <li key={event.id}>
            <AcknowledgementCard
              event={event}
              sessionId={sessionId}
              stepLabel={humaniseStepLabel(compositionState, event.affected_node_id)}
              stepTitle={humaniseStepTitle(compositionState, event.affected_node_id)}
              // Live state for the card's resolved-prompt rendering
              // (elspeth-990f5ea562): refreshed on every sibling resolve, so an
              // open prompt card re-renders with fresh substitutions.
              compositionState={compositionState}
              showAmend={!isTutorial && supportsAmendment(event.kind)}
              acceptButtonRef={(el) => {
                if (el != null) acceptRefs.current.set(event.id, el);
                else acceptRefs.current.delete(event.id);
              }}
              sectionRef={(el) => {
                if (el != null) sectionRefs.current.set(event.id, el);
                else sectionRefs.current.delete(event.id);
              }}
              onResolved={(newState) => {
                // Wrap to an earlier unresolved row when resolving the last row.
                const next = pending[index + 1] ?? pending.find((item) => item.id !== event.id);
                setFocusTargetId(next?.id ?? null);
                onResolved?.(newState, event);
                if (next === undefined) onFocusFallback?.();
              }}
            />
          </li>
        ))}
      </ul>

      {!isTutorial && (
        <div className="ack-stack-opt-out">
          {optOutError !== null && (
            <div role="alert" className="ack-stack-error">
              <strong className="ack-stack-error-heading">
                {optOutError.heading}
              </strong>
              <span className="ack-stack-error-body">{optOutError.body}</span>
            </div>
          )}
          <Button
            variant="bare"
            className="ack-stack-opt-out-link"
            onClick={() => setShowOptOutConfirm(true)}
            disabled={optOutInFlight}
          >
            Stop reviewing interpretations this session
          </Button>
        </div>
      )}

      {showOptOutConfirm && (
        <ConfirmDialog
          title="Stop reviewing interpretations"
          message={
            "For the rest of this session, I'll bake interpretations in " +
            "automatically without asking you to review each one.  You can " +
            "audit what was baked from the session's audit-readiness panel."
          }
          confirmLabel="Stop reviewing for this session"
          cancelLabel="Keep reviewing"
          variant="default"
          onConfirm={() => void handleConfirmOptOut()}
          onCancel={() => setShowOptOutConfirm(false)}
        />
      )}
    </section>
  );
}

/**
 * True when the session has at least one pending user_approved interpretation
 * — the predicate the ChatPanel guided branch uses to block wizard
 * advancement while acknowledgements remain (D12).  Relocated here from the
 * retired GuidedInterpretationReviews module.
 */
export function useHasPendingGuidedInterpretations(sessionId: string): boolean {
  const pendingBySession = useInterpretationEventsStore(
    (s) => s.pendingBySession,
  );
  return useMemo(() => {
    const events = Object.values(pendingBySession[sessionId] ?? {});
    return events.some(isPendingAcknowledgement);
  }, [pendingBySession, sessionId]);
}
