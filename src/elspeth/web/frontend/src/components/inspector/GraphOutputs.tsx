import { Fragment, type ReactNode } from "react";
import type { CompositionState, NodeSpec, NodeType } from "@/types";
import { FORK_CONNECTION, publishedSuccessConnection } from "@/lib/graphTopology";
import { pluginBindingLabel } from "@/lib/pluginBindingLabel";
import { sortedSourceEntries } from "@/utils/compositionState";
import { buildConnectionIndex } from "@/components/workspace/specRouting";

interface PolicyRow {
  kind: string;
  name: string;
  plugin: string | null;
  binding: string | null;
  success: ReactNode[];
  condition: string;
  action: ReactNode;
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

type DestinationName = (route: string, fromNode?: string) => ReactNode;

function successAction(route: string | null | undefined, destinationName: DestinationName, fromNode?: string): ReactNode {
  if (route === "discard") return "Discard row (audit recorded)";
  if (route) return <>Send to {destinationName(route, fromNode)}</>;
  return "No success route set";
}

function nodeSuccessActions(node: NodeSpec, destinationName: DestinationName): ReactNode[] {
  if (node.node_type === "gate" && node.routes && Object.keys(node.routes).length > 0) {
    return Object.entries(node.routes).map(([name, route]) => {
      const action = route === FORK_CONNECTION
        ? node.fork_to?.length
          ? <>Fork to {node.fork_to.map((branch, index) => (
            <Fragment key={branch}>{index > 0 && ", "}{destinationName(branch, node.id)}</Fragment>
          ))}</>
          : "No fork destinations set"
        : successAction(route, destinationName, node.id);
      return <Fragment key={name}>{name}: {action}</Fragment>;
    });
  }
  return [successAction(publishedSuccessConnection(node), destinationName, node.id)];
}

function failureAction(route: string | null | undefined, destinationName: DestinationName, fromNode?: string): ReactNode {
  if (route === "discard") return "Discard row (audit recorded)";
  if (route) return <>Send to {destinationName(route, fromNode)}</>;
  return "No failure route set";
}

function policyRows(state: CompositionState): PolicyRow[] {
  const { consumers } = buildConnectionIndex(state);
  const queueIds = new Set(state.nodes.filter((node) => node.node_type === "queue").map((node) => node.id));
  const destinationName: DestinationName = (route, fromNode) => {
    // Queue input and output share a connection name: upstream rows enter the
    // queue first; the queue itself forwards to the other consumers.
    if (queueIds.has(route) && route !== fromNode) return <code>{route}</code>;
    const destinations = consumers.get(route)?.filter((id) => id !== fromNode);
    return destinations?.length
      ? destinations.map((name, index) => (
        <Fragment key={name}>{index > 0 && ", "}<code>{name}</code></Fragment>
      ))
      : <><code>{route}</code> (not connected)</>;
  };
  const sources = sortedSourceEntries(state).map(([name, source]) => ({
    kind: "Source",
    name,
    plugin: source.plugin,
    binding: pluginBindingLabel(source.plugin, source.options),
    success: [successAction(source.on_success, destinationName)],
    condition: "Row fails validation",
    action: failureAction(source.on_validation_failure, destinationName),
  }));
  const nodes = state.nodes.map((node) => ({
    kind: NODE_LABELS[node.node_type],
    name: node.id,
    plugin: node.plugin,
    binding: pluginBindingLabel(node.plugin, node.options),
    success: nodeSuccessActions(node, destinationName),
    condition: node.node_type === "coalesce" && node.policy === "require_all"
      ? "Required branch missing"
      : "Row processing fails",
    action: node.node_type === "coalesce" && node.policy === "require_all"
      ? "No combined row (failure recorded)"
      : failureAction(node.on_error, destinationName, node.id),
  }));
  const outputs = state.outputs.map((output) => ({
    kind: "Sink",
    name: output.name,
    plugin: output.plugin,
    binding: pluginBindingLabel(output.plugin, output.options),
    success: ["Row written"],
    condition: "Row write fails",
    action: failureAction(output.on_write_failure, destinationName, output.name),
  }));
  return [...sources, ...nodes, ...outputs];
}

export function GraphOutputs({ state }: { state: CompositionState }): JSX.Element {
  const rows = policyRows(state);
  return (
    <details className="graph-detail-table">
      {/* Row kinds are the canonical node names (Source, Transform, Gate, ...,
          Sink); the section and its columns use routing vocabulary. */}
      <summary>Wiring ({rows.length})</summary>
      <div className="graph-detail-table-scroll" tabIndex={0} role="group" aria-label="Wiring table">
        <table>
          <thead>
            <tr><th scope="col">Component</th><th scope="col">On success</th><th scope="col">On failure</th></tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.kind}:${row.name}`}>
                <th scope="row">
                  {row.kind}: <code>{row.name}</code>
                  {row.plugin && <span className="graph-output-detail">{row.plugin}</span>}
                  {row.binding && <span className="graph-output-detail">{row.binding}</span>}
                </th>
                <td>{row.success.map((action, index) => <div key={index}>{action}</div>)}</td>
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
