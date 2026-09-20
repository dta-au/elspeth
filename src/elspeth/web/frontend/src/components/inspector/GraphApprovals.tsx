import type { InterpretationEvent } from "@/types/interpretation";
import type { CompositionState } from "@/types";
import { pluginBindingLabel } from "@/lib/pluginBindingLabel";
import { sourceComponentId } from "@/utils/compositionState";

const APPROVAL_DATE_FORMATTER = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

function promptApprovalName(value: string): string {
  if (value.startsWith("System prompt:\n") && value.includes("\n\nPrompt template:\n")) {
    return "System and user prompts";
  }
  if (value.startsWith("Multi-query LLM node:")) {
    return value.includes("System prompt (sent with every query):\n(none)")
      ? "User query prompts"
      : "System and query prompts";
  }
  return "User prompt";
}

const DECISION_NAMES: Readonly<Record<string, string>> = {
  prompt_injection_shield_recommendation: "Prompt injection protection",
  drop_raw_html_fields: "Remove raw HTML before saving",
  web_scrape_http_identity: "Website request identity",
  required_control_auto_wired: "Required protection added",
  gate_condition_authored: "Row routing condition",
};

function fallbackApprovalName(event: InterpretationEvent): string {
  if (event.kind === "llm_prompt_template") return promptApprovalName(event.accepted_value ?? "");
  if (event.kind === "llm_model_choice") return "LLM model selection";
  if (event.kind === "source_data_contract") return "Required source columns";
  if (event.kind === "invented_source") return "Generated source data";
  if (event.kind === "pipeline_decision") return DECISION_NAMES[event.user_term ?? ""] ?? "Pipeline decision";
  return event.user_term ?? "Interpretation";
}

function authoredApprovalName(event: InterpretationEvent, options: Record<string, unknown> | undefined): string | null {
  const requirements = options?.interpretation_requirements;
  if (!Array.isArray(requirements)) return null;
  for (const requirement of requirements) {
    if (typeof requirement !== "object" || requirement === null) continue;
    if (requirement.event_id === event.id && requirement.kind === event.kind && requirement.user_term === event.user_term
      && typeof requirement.display_title === "string" && requirement.display_title.trim()) {
      return requirement.display_title;
    }
  }
  return null;
}

export function GraphApprovals({
  events,
  state,
}: {
  events: ReadonlyArray<InterpretationEvent>;
  state: CompositionState;
}): JSX.Element | null {
  if (events.length === 0) return null;

  return (
    <details className="graph-detail-table">
      <summary>Approvals ({events.length})</summary>
      {/* Scrolls, and need not contain a focusable control: keyboard-focusable
          by the same WCAG 2.1.1 ruling as the chat dock (chat.css). */}
      <div className="graph-detail-table-scroll" tabIndex={0} role="group" aria-label="Approvals table">
        <table>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Approved value</th>
              <th scope="col" className="graph-approvals-date">Approved at</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => {
              if (event.user_term === null || event.accepted_value === null || event.resolved_at === null) {
                throw new Error("approved interpretation is missing its name, value, or approval time");
              }
              const approvedAt = new Date(event.resolved_at);
              const source = Object.entries(state.sources).find(([name]) => sourceComponentId(name) === event.affected_node_id);
              const component = source?.[1]
                ?? state.nodes.find((node) => node.id === event.affected_node_id)
                ?? state.outputs.find((output) => output.name === event.affected_node_id);
              const authoredName = authoredApprovalName(event, component?.options);
              const binding = component ? pluginBindingLabel(component.plugin, component.options) : null;
              const fallbackName = fallbackApprovalName(event);
              const name = authoredName === null
                ? fallbackName
                : event.kind === "llm_prompt_template"
                  ? `${authoredName} (${fallbackName.toLowerCase()})`
                  : authoredName;
              const nodeName = source?.[0] ?? event.affected_node_id;
              const dateLabel = Number.isNaN(approvedAt.getTime())
                ? event.resolved_at
                : APPROVAL_DATE_FORMATTER.format(approvedAt);
              return (
                <tr key={event.id}>
                  <th scope="row">
                    {name}
                    {nodeName && <> for <code>{nodeName}</code></>}
                    {component?.plugin && <span className="graph-output-detail">{component.plugin}</span>}
                    {/* The binding is the step's configuration NOW; the value
                        beside it was approved THEN. Said in words, not in a
                        hover title (hover-only disclosure ruled out 2026-09-13). */}
                    {binding && (
                      <span className="graph-output-detail">
                        {`Now: ${binding}`}
                      </span>
                    )}
                  </th>
                  <td className="graph-approvals-value">{event.accepted_value}</td>
                  <td className="graph-approvals-date"><time dateTime={event.resolved_at}>{dateLabel}</time></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </details>
  );
}
