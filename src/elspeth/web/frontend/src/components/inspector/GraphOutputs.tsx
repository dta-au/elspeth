import type { CompositionState, NodeSpec, NodeType } from "@/types";
import { FORK_CONNECTION, publishedSuccessConnection } from "@/lib/graphTopology";
import { llmBindingLabel } from "@/lib/llmBindingLabel";
import { sortedSourceEntries } from "@/utils/compositionState";

interface PolicyRow {
  component: string;
  model: string | null;
  success: string[];
  condition: string;
  action: string;
}

const NODE_LABELS: Record<NodeType, string> = {
  transform: "Transform",
  gate: "Gate",
  aggregation: "Aggregation",
  coalesce: "Coalesce",
  row_union: "Row union",
  queue: "Queue",
  collector: "Collector",
};

function successAction(route: string | null | undefined): string {
  if (route === "discard") return "Discard row (audit recorded)";
  if (route) return `Send to ${route}`;
  return "No success route set";
}

function nodeSuccessActions(node: NodeSpec): string[] {
  if (node.node_type === "gate" && node.routes && Object.keys(node.routes).length > 0) {
    return Object.entries(node.routes).map(([name, route]) => {
      const action = route === FORK_CONNECTION
        ? node.fork_to?.length
          ? `Fork to ${node.fork_to.join(", ")}`
          : "No fork destinations set"
        : successAction(route);
      return `${name}: ${action}`;
    });
  }
  return [successAction(publishedSuccessConnection(node))];
}

function failureAction(route: string | null | undefined): string {
  if (route === "discard") return "Discard row (audit recorded)";
  if (route) return `Send to ${route}`;
  return "No failure route set";
}

function policyRows(state: CompositionState): PolicyRow[] {
  const sources = sortedSourceEntries(state).map(([name, source]) => ({
    component: `Source: ${name}`,
    model: source.plugin === "llm" ? llmBindingLabel(source.options) : null,
    success: [successAction(source.on_success)],
    condition: "Row fails validation",
    action: failureAction(source.on_validation_failure),
  }));
  const nodes = state.nodes.map((node) => ({
    component: `${NODE_LABELS[node.node_type]}: ${node.id}`,
    model: node.plugin === "llm" ? llmBindingLabel(node.options) : null,
    success: nodeSuccessActions(node),
    condition: node.node_type === "coalesce" && node.policy === "require_all"
      ? "Required branch missing"
      : "Row processing fails",
    action: node.node_type === "coalesce" && node.policy === "require_all"
      ? "No combined row (failure recorded)"
      : failureAction(node.on_error),
  }));
  const outputs = state.outputs.map((output) => ({
    component: `Output: ${output.name} (${output.plugin})`,
    model: null,
    success: ["Row saved"],
    condition: "Row write fails",
    action: failureAction(output.on_write_failure),
  }));
  return [...sources, ...nodes, ...outputs];
}

export function GraphOutputs({ state }: { state: CompositionState }): JSX.Element {
  const rows = policyRows(state);
  return (
    <details className="graph-detail-table" open>
      <summary>Outputs ({rows.length})</summary>
      <div className="graph-detail-table-scroll">
        <table>
          <thead>
            <tr><th scope="col">Node</th><th scope="col">Success output</th><th scope="col">Failure output</th></tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.component}>
                <th scope="row">
                  {row.component}
                  {row.model && <span className="graph-output-detail">{row.model}</span>}
                </th>
                <td>{row.success.map((action) => <div key={action}>{action}</div>)}</td>
                <td>
                  <span className="graph-output-detail">{row.condition}</span>
                  {row.action}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
