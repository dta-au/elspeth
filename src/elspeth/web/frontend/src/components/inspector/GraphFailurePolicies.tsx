import type { CompositionState } from "@/types";
import { llmBindingLabel } from "@/lib/llmBindingLabel";
import { sortedSourceEntries } from "@/utils/compositionState";

interface PolicyRow {
  component: string;
  model: string | null;
  condition: string;
  action: string;
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
    condition: "Row fails validation",
    action: failureAction(source.on_validation_failure),
  }));
  const nodes = state.nodes.map((node) => ({
    component: `Node: ${node.id}`,
    model: node.plugin === "llm" ? llmBindingLabel(node.options) : null,
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
    condition: "Row write fails",
    action: failureAction(output.on_write_failure),
  }));
  return [...sources, ...nodes, ...outputs];
}

export function GraphFailurePolicies({ state }: { state: CompositionState }): JSX.Element {
  const rows = policyRows(state);
  return (
    <details className="graph-detail-table" open>
      <summary>Failure handling</summary>
      <div className="graph-detail-table-scroll">
        <table>
          <thead>
            <tr><th scope="col">Component</th><th scope="col">When</th><th scope="col">Action</th></tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.component}>
                <th scope="row">
                  {row.component}
                  {row.model && <span className="graph-failure-policies-model">{row.model}</span>}
                </th>
                <td>{row.condition}</td>
                <td>{row.action}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
