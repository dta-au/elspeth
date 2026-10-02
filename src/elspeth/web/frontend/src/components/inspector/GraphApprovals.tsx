import type { JSX } from "react";
import type { ApprovalRow } from "./approvalRows";

/** The dedicated Approvals tab's table: Name, Approved value, Approved at. */
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
