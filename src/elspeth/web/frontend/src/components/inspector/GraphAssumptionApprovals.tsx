import type { InterpretationEvent } from "@/types/interpretation";

const APPROVAL_DATE_FORMATTER = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

export function GraphAssumptionApprovals({
  events,
}: {
  events: ReadonlyArray<InterpretationEvent>;
}): JSX.Element | null {
  if (events.length === 0) return null;

  return (
    <details className="graph-detail-table">
      <summary>Assumption approvals ({events.length})</summary>
      <div className="graph-detail-table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Assumption</th>
              <th scope="col">Approved at</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => {
              if (event.user_term === null || event.accepted_value === null || event.resolved_at === null) {
                throw new Error("approved interpretation is missing its name, assumption, or approval time");
              }
              const approvedAt = new Date(event.resolved_at);
              const dateLabel = Number.isNaN(approvedAt.getTime())
                ? event.resolved_at
                : APPROVAL_DATE_FORMATTER.format(approvedAt);
              return (
                <tr key={event.id}>
                  <th scope="row">
                    {event.kind === "llm_prompt_template" && event.affected_node_id !== null
                      ? `Prompt for ${event.affected_node_id}`
                      : event.user_term}
                  </th>
                  <td className="graph-assumption-approvals-value">{event.accepted_value}</td>
                  <td><time dateTime={event.resolved_at}>{dateLabel}</time></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </details>
  );
}
