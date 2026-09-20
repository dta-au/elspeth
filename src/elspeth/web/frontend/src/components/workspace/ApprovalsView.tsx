import { useMemo } from "react";
import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { approvalRows } from "@/components/inspector/approvalRows";
import { selectApprovedInterpretations, useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { CompositionState } from "@/types";
import type { InterpretationEvent } from "@/types/interpretation";

/** The Approvals artifact tab: the same approvals the Workflow tab's table
 *  lists (one shared derivation, approvalRows), drawn in the Checks tab's
 *  audit-panel style so the two record surfaces read as one family. */
function ApprovalsList({
  events,
  state,
}: {
  events: ReadonlyArray<InterpretationEvent>;
  state: CompositionState;
}): JSX.Element {
  const rows = approvalRows(events, state);
  return (
    <ul className="audit-readiness-rows">
      {rows.map((row) => {
        const heading = row.nodeName ? `${row.name} for ${row.nodeName}` : row.name;
        return (
          <li key={row.id} className="audit-readiness-row audit-readiness-row--ok">
            <div className="audit-readiness-row-static" role="group" aria-label={heading}>
              <span className="audit-readiness-glyph" aria-hidden="true">✓</span>
              <span className="sr-only">Approved.</span>
              <span className="audit-readiness-row-label">
                {row.name}
                {row.nodeName && <> for <code>{row.nodeName}</code></>}
              </span>
              <span className="audit-readiness-row-summary">
                Approved <time dateTime={row.resolvedAt}>{row.dateLabel}</time>
                {row.plugin && <> · {row.plugin}</>}
                {row.binding && <> · {`Now: ${row.binding}`}</>}
              </span>
              <span className="audit-readiness-row-summary approvals-view-value">{row.value}</span>
            </div>
          </li>
        );
      })}
    </ul>
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
