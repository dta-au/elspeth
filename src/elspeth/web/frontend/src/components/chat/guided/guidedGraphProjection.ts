// src/components/chat/guided/guidedGraphProjection.ts
//
// Single authority for "the graph the right-hand Pipeline pane draws during a
// guided build" (elspeth-9f0873426a, design review IA-1 / V-1).
//
// In guided mode the committed composition is empty until Confirm wiring, so
// GraphView's committed-state pipeline has nothing to draw for four steps.
// Everything needed to draw sooner is already in the guided turn payloads the
// store holds: the reviewed source/output ledger (guidedReviewedComponents),
// the ProposePipelinePayload of a pending propose_pipeline turn, and the
// WireStageData of a pending confirm_wiring turn. This module projects those
// into ReadOnlyPipelineGraph's display shape — the same projection the
// proposal card used to make privately for its in-card mini graph, now
// exported so the pane and the card cannot drift (the repo's leaf-module
// remedy for frontend rule duplication).
//
// Guards this module keeps:
//   - D1 (epic elspeth-e7757e5c58): no source rows are read. The
//     inspect_and_confirm payload's `observed.samples` is never touched.
//   - Composer invariant 1: nothing here authors structure. Reviewed
//     components are drawn as lone nodes; every edge drawn comes verbatim from
//     a planner proposal or the wire stage derived from one.
//   - No tutorial-special path: the tutorial shell mounts the same
//     ArtifactWorkspace and inherits this projection unchanged.

import { stepLabelForPlugin } from "@/components/chat/interpretationStepLabel";
import type { GuidedReviewedComponents } from "@/stores/guidedReviewedComponents";
import type {
  ProposalFlow,
  ProposalNodeBehavior,
  ProposalNodeType,
  ProposalTargetEndpoint,
  ProposePipelinePayload,
  TurnPayload,
  WireStageData,
} from "@/types/guided";
import { plural } from "@/utils/plural";

import type {
  ReadOnlyPipelineGraphEdge,
  ReadOnlyPipelineGraphNode,
} from "./ReadOnlyPipelineGraph";

/** Virtual node every `{ kind: "discard" }` endpoint resolves to. */
export const GUIDED_GRAPH_DISCARD_NODE_ID = "guided-proposal-discard";

export type GuidedGraphStage = "reviewed" | "proposal" | "wiring";

export interface GuidedGraphProjection {
  stage: GuidedGraphStage;
  nodes: ReadOnlyPipelineGraphNode[];
  edges: ReadOnlyPipelineGraphEdge[];
  /** Accessible name for the role="img" SVG. */
  ariaLabel: string;
  /** One visible sentence: what the drawing is and what it is not yet. */
  caption: string;
}

/** Human name for one gate route: "when true (route-1)". The ordinal alias
 *  stays visible — it is the revise-target / integrity token — while the
 *  author-visible key says which branch this actually is (F11). */
function routeName(alias: string, routeKeys: ReadonlyMap<string, string>): string {
  const key = routeKeys.get(alias);
  return key === undefined ? alias : `when ${key} (${alias})`;
}

/** Alias → author-visible route key, from each gate's behavior bindings
 *  (bijective with route_aliases; aliases are globally unique ordinals, so
 *  one flat map covers every gate on the surface). */
export function routeKeysByAlias(
  nodes: ReadonlyArray<{ behavior: ProposalNodeBehavior }>,
): Map<string, string> {
  const keys = new Map<string, string>();
  for (const node of nodes) {
    if (node.behavior.kind !== "gate") continue;
    for (const { alias, key } of node.behavior.routes) keys.set(alias, key);
  }
  return keys;
}

export function flowLabel(flow: ProposalFlow, routeKeys: ReadonlyMap<string, string>): string {
  switch (flow.kind) {
    case "source_success":
      return flow.branch === null ? "on source success" : `on source success in ${flow.branch}`;
    case "source_validation_failure":
      return "on validation failure";
    case "node_success":
      return flow.branch === null ? "on success" : `on success in ${flow.branch}`;
    case "node_error":
      return "on error";
    case "gate_route": {
      const label = routeName(flow.route, routeKeys);
      return flow.branch === null ? label : `${label} in ${flow.branch}`;
    }
    case "gate_fork":
      return `${flow.routes.map((route) => routeName(route, routeKeys)).join(" + ")} forks to ${flow.branch}`;
    case "queue_continue":
      return flow.branch === null ? "queue continues" : `queue continues in ${flow.branch}`;
    case "coalesce_success":
      return flow.branch === null ? "after join" : `after join in ${flow.branch}`;
    case "row_union_success":
      return flow.branch === null
        ? "after row union"
        : `after row union in ${flow.branch}`;
    case "output_write_failure":
      return "on write failure";
  }
}

const ERROR_FLOW_KINDS: ReadonlySet<ProposalFlow["kind"]> = new Set([
  "source_validation_failure",
  "node_error",
  "output_write_failure",
]);

interface ProjectableEdge {
  stable_id: string;
  from_endpoint: { stable_id: string };
  to_endpoint: ProposalTargetEndpoint;
  flow: ProposalFlow;
}

function projectEdges(
  edges: readonly ProjectableEdge[],
  labelById: ReadonlyMap<string, string>,
  routeKeys: ReadonlyMap<string, string>,
): ReadOnlyPipelineGraphEdge[] {
  return edges.map((edge) => {
    const from = labelById.get(edge.from_endpoint.stable_id) ?? edge.from_endpoint.stable_id;
    const targetId = edge.to_endpoint.kind === "discard"
      ? GUIDED_GRAPH_DISCARD_NODE_ID
      : edge.to_endpoint.stable_id;
    const to = edge.to_endpoint.kind === "discard"
      ? "discard"
      : (labelById.get(edge.to_endpoint.stable_id) ?? edge.to_endpoint.stable_id);
    return {
      id: edge.stable_id,
      source: edge.from_endpoint.stable_id,
      target: targetId,
      label: `${from} ${flowLabel(edge.flow, routeKeys)} → ${to}`,
      isError: ERROR_FLOW_KINDS.has(edge.flow.kind),
    };
  });
}

function discardNode(edges: readonly ProjectableEdge[]): ReadOnlyPipelineGraphNode[] {
  return edges.some((edge) => edge.to_endpoint.kind === "discard")
    ? [{ id: GUIDED_GRAPH_DISCARD_NODE_ID, label: "discard", kind: "discard", subtitle: null }]
    : [];
}

interface ProjectableComponents {
  sources: ReadonlyArray<{ stable_id: string; label: string; pluginId: string }>;
  nodes: ReadonlyArray<{
    stable_id: string;
    label: string;
    node_type: ProposalNodeType;
    pluginId: string | null;
  }>;
  outputs: ReadonlyArray<{ stable_id: string; label: string; pluginId: string }>;
}

function projectComponentNodes(components: ProjectableComponents): ReadOnlyPipelineGraphNode[] {
  return [
    ...components.sources.map((source) => ({
      id: source.stable_id,
      label: source.label,
      kind: "source" as const,
      subtitle: stepLabelForPlugin(source.pluginId),
    })),
    ...components.nodes.map((node) => ({
      id: node.stable_id,
      label: node.label,
      kind: node.node_type,
      subtitle: node.pluginId === null ? null : stepLabelForPlugin(node.pluginId),
    })),
    ...components.outputs.map((output) => ({
      id: output.stable_id,
      label: output.label,
      kind: "output" as const,
      subtitle: stepLabelForPlugin(output.pluginId),
    })),
  ];
}

function labelsById(nodes: readonly ReadOnlyPipelineGraphNode[]): Map<string, string> {
  return new Map(nodes.map((node) => [node.id, node.label]));
}

/** Full-DAG projection of a pending pipeline proposal (step 3). */
export function projectProposalGraph(
  payload: ProposePipelinePayload,
): Pick<GuidedGraphProjection, "nodes" | "edges"> {
  const componentNodes = projectComponentNodes({
    sources: payload.graph.sources.map((source) => ({
      stable_id: source.stable_id,
      label: source.label,
      pluginId: source.plugin.id,
    })),
    nodes: payload.nodes.map((node) => ({
      stable_id: node.stable_id,
      label: node.label,
      node_type: node.node_type,
      pluginId: node.plugin === null ? null : node.plugin.id,
    })),
    outputs: payload.outputs.map((output) => ({
      stable_id: output.stable_id,
      label: output.label,
      pluginId: output.plugin.id,
    })),
  });
  return {
    nodes: [...componentNodes, ...discardNode(payload.graph.edges)],
    edges: projectEdges(
      payload.graph.edges,
      labelsById(componentNodes),
      routeKeysByAlias(payload.nodes),
    ),
  };
}

/** Full-DAG projection of the pending wire stage (step 4): the same
 *  proposal, now carried as candidate-derived connections. */
export function projectWireStageGraph(
  data: WireStageData,
): Pick<GuidedGraphProjection, "nodes" | "edges"> {
  const componentNodes = projectComponentNodes({
    sources: data.sources.map((source) => ({
      stable_id: source.stable_id,
      label: source.label,
      pluginId: source.plugin,
    })),
    nodes: data.nodes.map((node) => ({
      stable_id: node.stable_id,
      label: node.label,
      node_type: node.node_type,
      pluginId: node.plugin,
    })),
    outputs: data.outputs.map((output) => ({
      stable_id: output.stable_id,
      label: output.label,
      pluginId: output.plugin,
    })),
  });
  return {
    nodes: [...componentNodes, ...discardNode(data.connections)],
    edges: projectEdges(
      data.connections,
      labelsById(componentNodes),
      routeKeysByAlias(data.nodes),
    ),
  };
}

/** Lone-node projection of the components reviewed so far (steps 1–2).
 *  Deliberately edgeless: the planner has not proposed any routes yet, and
 *  drawing one would be the frontend authoring structure. */
export function projectReviewedComponentsGraph(
  reviewed: GuidedReviewedComponents,
): Pick<GuidedGraphProjection, "nodes" | "edges"> {
  return {
    nodes: projectComponentNodes({
      sources: reviewed.sources.map((item) => ({
        stable_id: item.stable_id,
        label: item.name,
        pluginId: item.plugin,
      })),
      nodes: [],
      outputs: reviewed.outputs.map((item) => ({
        stable_id: item.stable_id,
        label: item.name,
        pluginId: item.plugin,
      })),
    }),
    edges: [],
  };
}

function reviewedCaption(reviewed: GuidedReviewedComponents): string {
  const parts = [
    ...(reviewed.sources.length > 0 ? [plural(reviewed.sources.length, "source")] : []),
    ...(reviewed.outputs.length > 0 ? [plural(reviewed.outputs.length, "output")] : []),
  ];
  return `Reviewed so far: ${parts.join(" and ")}. Routes appear once a pipeline is proposed.`;
}

/**
 * The one graph the pane draws for the current guided state, or null when
 * nothing has been reviewed yet (the pane keeps its empty state). A pending
 * proposal or wire stage always wins over the ledger: it is what the learner
 * is being asked to decide on.
 */
export function projectGuidedGraph(input: {
  nextTurn: TurnPayload | null;
  reviewed: GuidedReviewedComponents;
}): GuidedGraphProjection | null {
  const { nextTurn, reviewed } = input;
  if (nextTurn !== null && nextTurn.type === "propose_pipeline") {
    const graph = projectProposalGraph(nextTurn.payload);
    return {
      stage: "proposal",
      ...graph,
      ariaLabel: `Pipeline proposal graph with ${graph.nodes.length} components and ${graph.edges.length} routes`,
      caption: "Proposed pipeline, not yet committed. Confirm the wiring to commit it.",
    };
  }
  if (nextTurn !== null && nextTurn.type === "confirm_wiring") {
    const graph = projectWireStageGraph(nextTurn.payload);
    return {
      stage: "wiring",
      ...graph,
      ariaLabel: `Pipeline wiring graph with ${graph.nodes.length} components and ${graph.edges.length} routes`,
      caption: "Proposed wiring under review. Confirm the wiring to commit it.",
    };
  }
  if (reviewed.sources.length === 0 && reviewed.outputs.length === 0) return null;
  const graph = projectReviewedComponentsGraph(reviewed);
  return {
    stage: "reviewed",
    ...graph,
    ariaLabel: `Reviewed components graph with ${plural(graph.nodes.length, "component")} and no routes yet`,
    caption: reviewedCaption(reviewed),
  };
}
