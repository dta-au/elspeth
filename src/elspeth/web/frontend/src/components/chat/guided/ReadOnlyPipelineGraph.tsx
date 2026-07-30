import { useId, useMemo } from "react";
import dagre from "@dagrejs/dagre";

export interface ReadOnlyPipelineGraphNode {
  id: string;
  label: string;
  kind:
    | "source"
    | "transform"
    | "gate"
    | "aggregation"
    | "queue"
    | "coalesce"
    | "row_union"
    | "output"
    | "discard";
  subtitle: string | null;
}

export interface ReadOnlyPipelineGraphEdge {
  /** Durable server-generated edge identity. Never derive this from array order. */
  id: string;
  source: string;
  target: string;
  label: string;
  isError: boolean;
}

interface ReadOnlyPipelineGraphProps {
  nodes: ReadOnlyPipelineGraphNode[];
  edges: ReadOnlyPipelineGraphEdge[];
  ariaLabel: string;
}

const NODE_WIDTH = 168;
const NODE_HEIGHT = 62;
const GRAPH_PADDING = 32;
const EDGE_LABEL_VERTICAL_PADDING = 12;

interface PositionedNode extends ReadOnlyPipelineGraphNode {
  x: number;
  y: number;
}

function layoutGraph(
  nodes: ReadOnlyPipelineGraphNode[],
  edges: ReadOnlyPipelineGraphEdge[],
): { nodes: PositionedNode[]; width: number; height: number } {
  const graph = new dagre.graphlib.Graph({ multigraph: true });
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: "LR", nodesep: 34, ranksep: 72, marginx: GRAPH_PADDING, marginy: GRAPH_PADDING });
  for (const node of nodes) {
    graph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const edge of edges) {
    graph.setEdge(edge.source, edge.target, {}, edge.id);
  }
  dagre.layout(graph);
  return {
    nodes: nodes.map((node) => {
      const position = graph.node(node.id);
      return { ...node, x: position.x, y: position.y };
    }),
    width: Math.max(graph.graph().width ?? 0, NODE_WIDTH + GRAPH_PADDING * 2),
    height: Math.max(graph.graph().height ?? 0, NODE_HEIGHT + GRAPH_PADDING * 2),
  };
}

function edgePath(
  source: PositionedNode,
  target: PositionedNode,
  laneOffset: number,
): string {
  const startX = source.x + NODE_WIDTH / 2;
  const endX = target.x - NODE_WIDTH / 2;
  const bend = Math.max(24, (endX - startX) / 2);
  return (
    `M ${startX} ${source.y} `
    + `C ${startX + bend} ${source.y + laneOffset}, `
    + `${endX - bend} ${target.y + laneOffset}, ${endX} ${target.y}`
  );
}

function edgeLaneOffsets(
  edges: ReadOnlyPipelineGraphEdge[],
): Map<string, number> {
  const edgeIdsByEndpoints = new Map<string, string[]>();
  for (const edge of edges) {
    const key = `${edge.source.length}:${edge.source}|${edge.target.length}:${edge.target}`;
    const ids = edgeIdsByEndpoints.get(key) ?? [];
    ids.push(edge.id);
    edgeIdsByEndpoints.set(key, ids);
  }
  const offsets = new Map<string, number>();
  for (const ids of edgeIdsByEndpoints.values()) {
    ids.forEach((id, index) => {
      offsets.set(id, (index - (ids.length - 1) / 2) * 22);
    });
  }
  return offsets;
}

function graphVerticalBounds(
  layoutHeight: number,
  edges: ReadOnlyPipelineGraphEdge[],
  nodesById: ReadonlyMap<string, PositionedNode>,
  laneOffsetByEdgeId: ReadonlyMap<string, number>,
): { minY: number; height: number } {
  let minY = 0;
  let maxY = layoutHeight;
  for (const edge of edges) {
    const source = nodesById.get(edge.source);
    const target = nodesById.get(edge.target);
    if (source === undefined || target === undefined) continue;

    const laneOffset = laneOffsetByEdgeId.get(edge.id) ?? 0;
    const labelY = (source.y + target.y) / 2 + laneOffset - 5;
    minY = Math.min(
      minY,
      source.y + laneOffset,
      target.y + laneOffset,
      labelY - EDGE_LABEL_VERTICAL_PADDING,
    );
    maxY = Math.max(
      maxY,
      source.y + laneOffset,
      target.y + laneOffset,
      labelY + EDGE_LABEL_VERTICAL_PADDING,
    );
  }
  return { minY, height: maxY - minY };
}

/**
 * Presentation-only full-DAG renderer. Its input is already-decoded display
 * data: it does not inspect CompositionState and does not infer topology.
 */
export function ReadOnlyPipelineGraph({
  nodes,
  edges,
  ariaLabel,
}: ReadOnlyPipelineGraphProps): JSX.Element {
  const titleId = useId();
  const layout = useMemo(() => layoutGraph(nodes, edges), [nodes, edges]);
  const byId = useMemo(
    () => new Map(layout.nodes.map((node) => [node.id, node])),
    [layout.nodes],
  );
  const laneOffsetByEdgeId = useMemo(
    () => edgeLaneOffsets(edges),
    [edges],
  );
  const verticalBounds = useMemo(
    () =>
      graphVerticalBounds(
        layout.height,
        edges,
        byId,
        laneOffsetByEdgeId,
      ),
    [layout.height, edges, byId, laneOffsetByEdgeId],
  );

  return (
    <div className="guided-readonly-graph">
      <svg
        className="guided-readonly-graph__canvas"
        viewBox={`0 ${verticalBounds.minY} ${layout.width} ${verticalBounds.height}`}
        role="img"
        aria-labelledby={titleId}
      >
        <title id={titleId}>{ariaLabel}</title>
        <g className="guided-readonly-graph__edges">
          {edges.map((edge) => {
            const source = byId.get(edge.source);
            const target = byId.get(edge.target);
            if (source === undefined || target === undefined) {
              throw new Error(`ReadOnlyPipelineGraph edge ${edge.id} has an unresolved endpoint`);
            }
            const laneOffset = laneOffsetByEdgeId.get(edge.id) ?? 0;
            const labelX = (source.x + target.x) / 2;
            const labelY = (source.y + target.y) / 2 + laneOffset - 5;
            return (
              <g key={edge.id}>
                <path
                  data-edge-id={edge.id}
                  className={
                    edge.isError
                      ? "guided-readonly-graph__edge guided-readonly-graph__edge--error"
                      : "guided-readonly-graph__edge"
                  }
                  d={edgePath(source, target, laneOffset)}
                />
                <text
                  data-edge-id={edge.id}
                  className="guided-readonly-graph__edge-label"
                  x={labelX}
                  y={labelY}
                  textAnchor="middle"
                >
                  {edge.label}
                </text>
              </g>
            );
          })}
        </g>
        <g className="guided-readonly-graph__nodes">
          {layout.nodes.map((node) => (
            <g
              key={node.id}
              data-node-id={node.id}
              data-node-kind={node.kind}
              transform={`translate(${node.x - NODE_WIDTH / 2} ${node.y - NODE_HEIGHT / 2})`}
            >
              <rect
                className={`guided-readonly-graph__node guided-readonly-graph__node--${node.kind}`}
                width={NODE_WIDTH}
                height={NODE_HEIGHT}
                rx="8"
              />
              <text className="guided-readonly-graph__node-label" x="12" y="25">
                {node.label}
              </text>
              {node.subtitle !== null ? (
                <text className="guided-readonly-graph__node-subtitle" x="12" y="45">
                  {node.subtitle}
                </text>
              ) : null}
            </g>
          ))}
        </g>
      </svg>
      <ul className="visually-hidden">
        {edges.map((edge) => (
          <li key={edge.id}>{edge.label}</li>
        ))}
      </ul>
    </div>
  );
}
