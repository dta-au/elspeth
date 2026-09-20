import type { InterpretationEvent } from "@/types/interpretation";

const APPROVAL_DATE_FORMATTER = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

function promptApprovalName(value: string, nodeId: string): string {
  if (value.startsWith("System prompt:\n") && value.includes("\n\nPrompt template:\n")) {
    return `System and user prompts for ${nodeId}`;
  }
  if (value.startsWith("Multi-query LLM node:")) {
    return value.includes("System prompt (sent with every query):\n(none)")
      ? `User query prompts for ${nodeId}`
      : `System and query prompts for ${nodeId}`;
  }
  return `User prompt for ${nodeId}`;
}

export function GraphApprovals({
  events,
}: {
  events: ReadonlyArray<InterpretationEvent>;
}): JSX.Element | null {
  if (events.length === 0) return null;

  return (
    <details className="graph-detail-table">
      <summary>Approvals ({events.length})</summary>
      <div className="graph-detail-table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Approved value</th>
              <th scope="col">Approved at</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => {
              if (event.user_term === null || event.accepted_value === null || event.resolved_at === null) {
                throw new Error("approved interpretation is missing its name, value, or approval time");
              }
              const approvedAt = new Date(event.resolved_at);
              const dateLabel = Number.isNaN(approvedAt.getTime())
                ? event.resolved_at
                : APPROVAL_DATE_FORMATTER.format(approvedAt);
              return (
                <tr key={event.id}>
                  <th scope="row">
                    {event.kind === "llm_prompt_template" && event.affected_node_id !== null
                      ? promptApprovalName(event.accepted_value, event.affected_node_id)
                      : event.user_term}
                  </th>
                  <td className="graph-approvals-value">{event.accepted_value}</td>
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
