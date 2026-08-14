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

// Keep Dagre's established slots stable so compact cards do not get scaled
// back up by a smaller SVG viewBox. The unused slot space is intentional: it
// is the label gutter in the fixed-width proposal panel.
const LAYOUT_NODE_WIDTH = 168;
const LAYOUT_NODE_HEIGHT = 62;
const NODE_WIDTH = 136;
const NODE_HEIGHT = 54;
const GRAPH_PADDING = 32;
const EDGE_LABEL_HORIZONTAL_PADDING = 12;
const EDGE_LABEL_VERTICAL_PADDING = 12;
const EDGE_LABEL_DY = -5;
const EDGE_LABEL_LINE_HEIGHT = 12;
const EDGE_LABEL_MAX_CHARACTERS = 14;
const EDGE_LABEL_APPROX_CHARACTER_WIDTH = 6;
const EDGE_LABEL_NODE_GAP = 8;

interface PositionedNode extends ReadOnlyPipelineGraphNode {
  x: number;
  y: number;
}

interface EdgeLabelPlacement {
  x: number;
  y: number;
  textAnchor: "middle" | "end";
}

function layoutGraph(
  nodes: ReadOnlyPipelineGraphNode[],
  edges: ReadOnlyPipelineGraphEdge[],
): { nodes: PositionedNode[]; width: number; height: number } {
  const graph = new dagre.graphlib.Graph({ multigraph: true });
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: "TB", nodesep: 34, ranksep: 72, marginx: GRAPH_PADDING, marginy: GRAPH_PADDING });
  for (const node of nodes) {
    graph.setNode(node.id, { width: LAYOUT_NODE_WIDTH, height: LAYOUT_NODE_HEIGHT });
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
    width: Math.max(graph.graph().width ?? 0, LAYOUT_NODE_WIDTH + GRAPH_PADDING * 2),
    height: Math.max(graph.graph().height ?? 0, LAYOUT_NODE_HEIGHT + GRAPH_PADDING * 2),
  };
}

function edgePath(
  source: PositionedNode,
  target: PositionedNode,
  laneOffset: number,
): string {
  const startY = source.y + NODE_HEIGHT / 2;
  const endY = target.y - NODE_HEIGHT / 2;
  const bend = Math.max(24, (endY - startY) / 2);
  return (
    `M ${source.x} ${startY} `
    + `C ${source.x + laneOffset} ${startY + bend}, `
    + `${target.x + laneOffset} ${endY - bend}, ${target.x} ${endY}`
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

function edgeMidpointY(
  source: PositionedNode,
  target: PositionedNode,
): number {
  return (source.y + target.y) / 2;
}

function edgeMidpointX(
  source: PositionedNode,
  target: PositionedNode,
  laneOffset: number,
): number {
  // At t=0.5, the two cubic control points contribute three quarters of
  // their shared horizontal lane offset.
  return (source.x + target.x) / 2 + laneOffset * 0.75;
}

function edgeLabelLines(label: string): string[] {
  const lines: string[] = [];
  let current = "";
  const words = label.trim().split(/\s+/);
  for (const word of words) {
    const chunks = word.match(new RegExp(`.{1,${EDGE_LABEL_MAX_CHARACTERS}}`, "g")) ?? [word];
    for (const chunk of chunks) {
      const candidate = current === "" ? chunk : `${current} ${chunk}`;
      if (candidate.length <= EDGE_LABEL_MAX_CHARACTERS) {
        current = candidate;
      } else {
        lines.push(current);
        current = chunk;
      }
    }
  }
  if (current !== "") lines.push(current);
  return lines.length > 0 ? lines : [""];
}

function edgeLabelPlacement(
  source: PositionedNode,
  target: PositionedNode,
  laneOffset: number,
  nodes: Iterable<PositionedNode>,
): EdgeLabelPlacement {
  const x = edgeMidpointX(source, target, laneOffset);
  const y = edgeMidpointY(source, target);
  for (const node of nodes) {
    if (node.id === source.id || node.id === target.id) continue;
    const overlapsNode =
      Math.abs(x - node.x) <= NODE_WIDTH / 2
      && Math.abs(y - node.y) <= NODE_HEIGHT / 2;
    if (overlapsNode) {
      return {
        x: node.x - NODE_WIDTH / 2 - EDGE_LABEL_NODE_GAP,
        y,
        textAnchor: "end",
      };
    }
  }
  return { x, y, textAnchor: "middle" };
}

function graphVerticalBounds(
  layoutHeight: number,
  edges: ReadOnlyPipelineGraphEdge[],
  nodesById: ReadonlyMap<string, PositionedNode>,
): { minY: number; height: number } {
  let minY = 0;
  let maxY = layoutHeight;
  for (const edge of edges) {
    const source = nodesById.get(edge.source);
    const target = nodesById.get(edge.target);
    if (source === undefined || target === undefined) continue;

    const labelY = edgeMidpointY(source, target) + EDGE_LABEL_DY;
    const labelLines = edgeLabelLines(edge.label);
    const labelHalfHeight =
      ((labelLines.length - 1) * EDGE_LABEL_LINE_HEIGHT) / 2
      + EDGE_LABEL_VERTICAL_PADDING;
    minY = Math.min(
      minY,
      labelY - labelHalfHeight,
    );
    maxY = Math.max(
      maxY,
      labelY + labelHalfHeight,
    );
  }
  return { minY, height: maxY - minY };
}

function graphHorizontalBounds(
  layoutWidth: number,
  edges: ReadOnlyPipelineGraphEdge[],
  nodesById: ReadonlyMap<string, PositionedNode>,
  laneOffsetByEdgeId: ReadonlyMap<string, number>,
): { minX: number; width: number } {
  let minX = 0;
  let maxX = layoutWidth;
  for (const edge of edges) {
    const source = nodesById.get(edge.source);
    const target = nodesById.get(edge.target);
    if (source === undefined || target === undefined) continue;

    const laneOffset = laneOffsetByEdgeId.get(edge.id) ?? 0;
    const label = edgeLabelPlacement(source, target, laneOffset, nodesById.values());
    const labelWidth =
      Math.max(...edgeLabelLines(edge.label).map((line) => line.length))
      * EDGE_LABEL_APPROX_CHARACTER_WIDTH;
    const labelMinX = label.textAnchor === "end"
      ? label.x - labelWidth
      : label.x - labelWidth / 2;
    const labelMaxX = label.textAnchor === "end"
      ? label.x
      : label.x + labelWidth / 2;
    minX = Math.min(
      minX,
      source.x + laneOffset,
      target.x + laneOffset,
      labelMinX - EDGE_LABEL_HORIZONTAL_PADDING,
    );
    maxX = Math.max(
      maxX,
      source.x + laneOffset,
      target.x + laneOffset,
      labelMaxX + EDGE_LABEL_HORIZONTAL_PADDING,
    );
  }
  return { minX, width: maxX - minX };
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
      ),
    [layout.height, edges, byId],
  );
  const horizontalBounds = useMemo(
    () =>
      graphHorizontalBounds(
        layout.width,
        edges,
        byId,
        laneOffsetByEdgeId,
      ),
    [layout.width, edges, byId, laneOffsetByEdgeId],
  );

  return (
    <div className="guided-readonly-graph">
      <svg
        className="guided-readonly-graph__canvas"
        viewBox={`${horizontalBounds.minX} ${verticalBounds.minY} ${horizontalBounds.width} ${verticalBounds.height}`}
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
            const label = edgeLabelPlacement(source, target, laneOffset, byId.values());
            const labelLines = edgeLabelLines(edge.label);
            const firstLineDy =
              EDGE_LABEL_DY
              - ((labelLines.length - 1) * EDGE_LABEL_LINE_HEIGHT) / 2;
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
                  x={label.x}
                  y={label.y}
                  textAnchor={label.textAnchor}
                >
                  {labelLines.map((line, index) => (
                    <tspan
                      key={`${edge.id}-line-${index + 1}`}
                      x={label.x}
                      dy={index === 0 ? firstLineDy : EDGE_LABEL_LINE_HEIGHT}
                    >
                      {line}
                    </tspan>
                  ))}
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
              {/* Corner radius: guided.css .guided-readonly-graph__node reads
                  rx from var(--radius-lg) — the same rank GraphView.tsx's
                  editable cards read (elspeth-37cc8b5310). The rx="8"
                  presentation attribute below is the value-identical fallback
                  (SVG2 CSS geometry wins over it wherever CSS applies); keep
                  the two in step with the token. */}
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
