import type { InterpretationEvent } from "@/types/interpretation";
import type { CompositionState } from "@/types";
import { approvalRows, type ApprovalRow } from "./approvalRows";

/** The approvals table itself — Name, Approved value, Approved at. One
 *  component for both places it appears (the Workflow tab's disclosure and the
 *  Approvals tab), so the two cannot drift in columns or content. */
export function ApprovalsTable({ rows }: { rows: ReadonlyArray<ApprovalRow> }): JSX.Element {
  return (
    <table>
      <thead>
        <tr>
          <th scope="col">Name</th>
          <th scope="col">Approved value</th>
          <th scope="col" className="graph-approvals-date">Approved at</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.id}>
            <th scope="row">
              {row.name}
              {row.nodeName && <> for <code>{row.nodeName}</code></>}
              {row.plugin && <span className="graph-output-detail">{row.plugin}</span>}
              {/* The binding is the step's configuration NOW; the value
                  beside it was approved THEN. Said in words, not in a
                  hover title (hover-only disclosure ruled out 2026-09-13). */}
              {row.binding && (
                <span className="graph-output-detail">
                  {`Now: ${row.binding}`}
                </span>
              )}
            </th>
            <td className="graph-approvals-value">{row.value}</td>
            <td className="graph-approvals-date"><time dateTime={row.resolvedAt}>{row.dateLabel}</time></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function GraphApprovals({
  events,
  state,
}: {
  events: ReadonlyArray<InterpretationEvent>;
  state: CompositionState;
}): JSX.Element | null {
  if (events.length === 0) return null;
  const rows = approvalRows(events, state);

  return (
    <details className="graph-detail-table">
      <summary>Approvals ({rows.length})</summary>
      {/* Scrolls, and need not contain a focusable control: keyboard-focusable
          by the same WCAG 2.1.1 ruling as the chat dock (chat.css). */}
      <div className="graph-detail-table-scroll" tabIndex={0} role="group" aria-label="Approvals table">
        <ApprovalsTable rows={rows} />
      </div>
    </details>
  );
}
