import { useMemo } from "react";
import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { approvalRows } from "@/components/inspector/approvalRows";
import { ApprovalsTable } from "@/components/inspector/GraphApprovals";
import { selectApprovedInterpretations, useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { CompositionState } from "@/types";
import type { InterpretationEvent } from "@/types/interpretation";

/** Approval history uses the whole panel inside the shared audit-panel card. */
function ApprovalsList({
  events,
  state,
}: {
  events: ReadonlyArray<InterpretationEvent>;
  state: CompositionState;
}): JSX.Element {
  return (
    <div className="graph-detail-table approvals-view-table">
      <ApprovalsTable rows={approvalRows(events, state)} />
    </div>
  );
}

export function ApprovalsView(): JSX.Element {
  const compositionState = useSessionStore((s) => s.compositionState);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const resolvedBySession = useInterpretationEventsStore((s) => s.resolvedBySession);
  const events = useMemo(
    () => selectApprovedInterpretations(activeSessionId === null ? [] : resolvedBySession[activeSessionId] ?? []),
    [activeSessionId, resolvedBySession],
  );

  return (
    <div className="checks-view">
      <section aria-label="Approvals" className="audit-readiness">
        <header className="audit-readiness-header">
          <div>
            <h2 className="audit-readiness-title">Approvals</h2>
            <p className="audit-readiness-freshness">
              {events.length === 0
                ? "Nothing has been approved for this pipeline yet. Choices the composer makes for you appear here once you approve them."
                : `${events.length} approved ${events.length === 1 ? "choice" : "choices"} recorded for this pipeline. Each value is what you approved; "Now" is the step's configuration today.`}
            </p>
          </div>
        </header>
        {compositionState && events.length > 0 && (
          <ErrorBoundary label="Approvals">
            <ApprovalsList events={events} state={compositionState} />
          </ErrorBoundary>
        )}
      </section>
    </div>
  );
}
