import { DISCARD_CONNECTION } from "./graphTopology";

/** Filter presentation only, after layout; retain the authored graph and geometry. */
export function visibleGraphRoutes<
  N extends { id: string },
  E extends { source: string; target: string; data: { flowType: "success" | "error" } },
>(nodes: N[], edges: E[], sinkIds: ReadonlySet<string>, showFailureRoutes: boolean): { nodes: N[]; edges: E[] } {
  if (showFailureRoutes) return { nodes, edges };
  const visible = edges.filter((edge) => edge.data.flowType !== "error" && edge.target !== DISCARD_CONNECTION);
  const visibleEndpoints = new Set(visible.flatMap((edge) => [edge.source, edge.target]));
  const hiddenTargets = new Set(edges.filter((edge) => edge.data.flowType === "error" || edge.target === DISCARD_CONNECTION).map((edge) => edge.target));
  return {
    nodes: nodes.filter((node) => !(sinkIds.has(node.id) || node.id === DISCARD_CONNECTION) || !hiddenTargets.has(node.id) || visibleEndpoints.has(node.id)),
    edges: visible,
  };
}
