// ============================================================================
// GraphView
//
// React Flow (@xyflow/react) DAG visualisation of the current CompositionState.
// Converts nodes and edges to React Flow format with colour-coded node types.
// Read-only (no drag-to-connect, no node deletion). Pan and zoom enabled.
// Auto-layout using dagre (@dagrejs/dagre) for hierarchical top-to-bottom.
//
// ARIA: container has aria-label describing the pipeline structure.
// "Pipeline graph with N components."
//
// Empty state when no nodes.
// ============================================================================

import { useMemo, useCallback, useEffect, useRef, type ReactNode } from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  BaseEdge,
  Handle,
  MarkerType,
  Position,
  type Node,
  type Edge,
  type EdgeProps,
  type EdgeTypes,
  type NodeProps,
  type NodeTypes,
  type NodeMouseHandler,
  type OnInit,
  Background,
  Controls,
  MiniMap,
} from "@xyflow/react";
import dagre from "@dagrejs/dagre";
import "@xyflow/react/dist/style.css";
import { useSessionStore } from "@/stores/sessionStore";
import { useExecutionStore } from "@/stores/executionStore";
import { useTheme } from "@/hooks/useTheme";
import {
  hasCompositionContent,
  sortedSourceEntries,
  sourceComponentId,
} from "@/utils/compositionState";
import { BADGE_COLORS, BADGE_BACKGROUNDS, EDGE_COLORS, EDGE_LABEL_COLOR, VALIDATION_COLORS } from "@/styles/tokens";
import { TypeBadge } from "@/components/ui";
import type { CompositionState } from "@/types/index";

const NODE_WIDTH = 260;
const NODE_HEIGHT = 80;
const FALLBACK_MINIMAP_NODE_COLOR_VAR = "--color-text-muted";
const MINIMAP_NODE_STROKE_COLOR_VAR = "--color-border-strong";
// Interim UX threshold: avoid MiniMap noise on small graphs until we can
// promote this to a viewport-overflow heuristic or an explicit user toggle.
const MINIMAP_NODE_COUNT_THRESHOLD = 8;
const PIPELINE_NODE_TYPE = "pipeline-component";
const PARALLEL_EDGE_TYPE = "parallel-lane";
const PARALLEL_EDGE_LANE_GAP = 22;
const PARALLEL_HANDLE_INSET = 16;

const EDGE_LABEL_MAP: Record<string, string> = {
  on_success: "success",
  on_error: "error",
  route_true: "true",
  route_false: "false",
  fork: "fork",
};

function humanNodeType(typeLabel: string): string {
  return typeLabel === "row_union" ? "row union" : typeLabel;
}

/**
 * Keep simple inferred ids readable while length-prefixing any hyphenated
 * component. Plain delimiter joining is ambiguous for valid ids such as
 * ("a-b", "c") and ("a", "b-c").
 */
function inferredEdgeId(kind: string, ...parts: string[]): string {
  const payload = parts.some((part) => part.includes("-"))
    ? parts.map((part) => `${part.length}:${part}`).join("|")
    : parts.join("-");
  return `inferred-${kind}-${payload}`;
}

type EdgeFlowType = "success" | "error";

interface PipelineEdgeData extends Record<string, unknown> {
  flowType: EdgeFlowType;
}

interface ParallelLaneEdgeData extends PipelineEdgeData {
  laneOffset: number;
}

interface ParallelHandle {
  id: string;
  offsetPercent: number;
}

interface PipelineGraphNodeData extends Record<string, unknown> {
  label: ReactNode;
  parallelSourceHandles?: ParallelHandle[];
  parallelTargetHandles?: ParallelHandle[];
}

type PipelineGraphNodeModel = Node<
  PipelineGraphNodeData,
  typeof PIPELINE_NODE_TYPE
>;

type PipelineGraphEdgeModel = Edge<PipelineEdgeData> & {
  data: PipelineEdgeData;
};

type ParallelLaneEdgeModel = Edge<
  ParallelLaneEdgeData,
  typeof PARALLEL_EDGE_TYPE
> & {
  data: ParallelLaneEdgeData;
};

function parallelLanePath(
  sourceX: number,
  sourceY: number,
  targetX: number,
  targetY: number,
  laneOffset: number,
): { path: string; labelX: number; labelY: number } {
  const horizontal = Math.abs(targetX - sourceX) > Math.abs(targetY - sourceY);
  if (horizontal) {
    const bend = Math.max(40, Math.abs(targetX - sourceX) * 0.4);
    const direction = targetX >= sourceX ? 1 : -1;
    return {
      path:
        `M ${sourceX} ${sourceY} `
        + `C ${sourceX + direction * bend} ${sourceY + laneOffset}, `
        + `${targetX - direction * bend} ${targetY + laneOffset}, `
        + `${targetX} ${targetY}`,
      labelX: (sourceX + targetX) / 2,
      labelY: (sourceY + targetY) / 2 + laneOffset,
    };
  }

  const bend = Math.max(40, Math.abs(targetY - sourceY) * 0.4);
  const direction = targetY >= sourceY ? 1 : -1;
  return {
    path:
      `M ${sourceX} ${sourceY} `
      + `C ${sourceX + laneOffset} ${sourceY + direction * bend}, `
      + `${targetX + laneOffset} ${targetY - direction * bend}, `
      + `${targetX} ${targetY}`,
    labelX: (sourceX + targetX) / 2 + laneOffset,
    labelY: (sourceY + targetY) / 2,
  };
}

function PipelineGraphNode({
  data,
}: NodeProps<PipelineGraphNodeModel>): JSX.Element {
  return (
    <>
      <Handle
        type="target"
        position={Position.Top}
        isConnectable={false}
        style={{ opacity: 0, pointerEvents: "none" }}
      />
      {data.parallelTargetHandles?.map((handle) => (
        <Handle
          key={handle.id}
          id={handle.id}
          type="target"
          position={Position.Top}
          isConnectable={false}
          style={{
            left: `${handle.offsetPercent}%`,
            opacity: 0,
            pointerEvents: "none",
          }}
        />
      ))}
      {data.label}
      <Handle
        type="source"
        position={Position.Bottom}
        isConnectable={false}
        style={{ opacity: 0, pointerEvents: "none" }}
      />
      {data.parallelSourceHandles?.map((handle) => (
        <Handle
          key={handle.id}
          id={handle.id}
          type="source"
          position={Position.Bottom}
          isConnectable={false}
          style={{
            left: `${handle.offsetPercent}%`,
            opacity: 0,
            pointerEvents: "none",
          }}
        />
      ))}
    </>
  );
}

function ParallelLaneEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  data,
  label,
  labelStyle,
  markerEnd,
  style,
}: EdgeProps<ParallelLaneEdgeModel>): JSX.Element {
  const { path, labelX, labelY } = parallelLanePath(
    sourceX,
    sourceY,
    targetX,
    targetY,
    data?.laneOffset ?? 0,
  );
  return (
    <BaseEdge
      id={id}
      path={path}
      label={label}
      labelX={labelX}
      labelY={labelY}
      labelStyle={labelStyle}
      markerEnd={markerEnd}
      style={style}
    />
  );
}

const EDGE_TYPES: EdgeTypes = {
  [PARALLEL_EDGE_TYPE]: ParallelLaneEdge,
};

const NODE_TYPES: NodeTypes = {
  [PIPELINE_NODE_TYPE]: PipelineGraphNode,
};

function assignParallelEdgeLanes(
  nodes: PipelineGraphNodeModel[],
  edges: PipelineGraphEdgeModel[],
): { nodes: PipelineGraphNodeModel[]; edges: PipelineGraphEdgeModel[] } {
  const indexesByEndpoints = new Map<string, number[]>();
  for (const [index, edge] of edges.entries()) {
    const key =
      `${edge.source.length}:${edge.source}|${edge.target.length}:${edge.target}`;
    const indexes = indexesByEndpoints.get(key) ?? [];
    indexes.push(index);
    indexesByEndpoints.set(key, indexes);
  }

  const laneOffsetByIndex = new Map<number, number>();
  const sourceHandlesByNode = new Map<string, ParallelHandle[]>();
  const targetHandlesByNode = new Map<string, ParallelHandle[]>();
  for (const indexes of indexesByEndpoints.values()) {
    if (indexes.length < 2) continue;
    const sortedIndexes = [...indexes].sort((left, right) =>
      edges[left]!.id.localeCompare(edges[right]!.id),
    );
    const laneGap = Math.min(
      PARALLEL_EDGE_LANE_GAP,
      (NODE_WIDTH - PARALLEL_HANDLE_INSET * 2) / (sortedIndexes.length - 1),
    );
    sortedIndexes.forEach((edgeIndex, laneIndex) => {
      const edge = edges[edgeIndex]!;
      const laneOffset =
        (laneIndex - (sortedIndexes.length - 1) / 2) * laneGap;
      laneOffsetByIndex.set(edgeIndex, laneOffset);
      const offsetPercent = 50 + (laneOffset / NODE_WIDTH) * 100;
      const sourceHandle = {
        id: `parallel-source-${edge.id}`,
        offsetPercent,
      };
      const targetHandle = {
        id: `parallel-target-${edge.id}`,
        offsetPercent,
      };
      sourceHandlesByNode.set(edge.source, [
        ...(sourceHandlesByNode.get(edge.source) ?? []),
        sourceHandle,
      ]);
      targetHandlesByNode.set(edge.target, [
        ...(targetHandlesByNode.get(edge.target) ?? []),
        targetHandle,
      ]);
    });
  }

  const parallelEdges = edges.map((edge, index) => {
    const laneOffset = laneOffsetByIndex.get(index);
    if (laneOffset === undefined) return edge;
    return {
      ...edge,
      type: PARALLEL_EDGE_TYPE,
      sourceHandle: `parallel-source-${edge.id}`,
      targetHandle: `parallel-target-${edge.id}`,
      data: {
        ...edge.data,
        laneOffset,
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color:
          typeof edge.style?.stroke === "string"
            ? edge.style.stroke
            : undefined,
        width: 12,
        height: 12,
      },
    };
  });
  const nodesWithHandles = nodes.map((node) => ({
    ...node,
    data: {
      ...node.data,
      parallelSourceHandles: sourceHandlesByNode.get(node.id),
      parallelTargetHandles: targetHandlesByNode.get(node.id),
    },
  }));
  return { nodes: nodesWithHandles, edges: parallelEdges };
}

type MiniMapNodeKind = keyof typeof BADGE_COLORS;
type ValidationStatus = "valid" | "warning" | "error";

const VALIDATION_STATUS_MARKERS: Record<
  ValidationStatus,
  { ariaLabel: string; glyph: string; fallbackTitle: string }
> = {
  error: {
    ariaLabel: "Validation: error",
    glyph: "x",
    fallbackTitle: "Has validation errors",
  },
  warning: {
    ariaLabel: "Validation: warning",
    glyph: "!",
    fallbackTitle: "Has warnings",
  },
  valid: {
    ariaLabel: "Validation: passing",
    glyph: "✓",
    fallbackTitle: "Valid",
  },
};

interface SelectedComponentConfig {
  id: string;
  typeLabel: MiniMapNodeKind;
  plugin: string | null;
  connections: Record<string, unknown>;
  options: Record<string, unknown>;
}

function readThemeColor(cssVariableName: string, fallbackVariableName: string): string {
  if (typeof document === "undefined" || typeof getComputedStyle !== "function") {
    return `var(${fallbackVariableName})`;
  }

  const rootStyles = getComputedStyle(document.documentElement);
  return (
    rootStyles.getPropertyValue(cssVariableName).trim() ||
    rootStyles.getPropertyValue(fallbackVariableName).trim() ||
    `var(${fallbackVariableName})`
  );
}

function buildMiniMapNodeKindById(
  compositionState: CompositionState | null,
): Map<string, MiniMapNodeKind> {
  const kindById = new Map<string, MiniMapNodeKind>();
  if (!compositionState) {
    return kindById;
  }

  for (const [sourceName] of sortedSourceEntries(compositionState)) {
    kindById.set(sourceComponentId(sourceName), "source");
  }
  for (const node of compositionState.nodes) {
    kindById.set(node.id, node.node_type);
  }
  for (const output of compositionState.outputs) {
    kindById.set(output.name, "sink");
  }

  return kindById;
}

function withoutNullishFields(
  fields: Record<string, unknown>,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(fields).filter(([, value]) => value !== null && value !== undefined),
  );
}

function selectedComponentConfig(
  compositionState: CompositionState | null,
  selectedNodeId: string | null,
): SelectedComponentConfig | null {
  if (!compositionState || !selectedNodeId) {
    return null;
  }

  const selectedSource = sortedSourceEntries(compositionState).find(
    ([sourceName]) => sourceComponentId(sourceName) === selectedNodeId,
  );
  if (selectedSource) {
    const [sourceName, source] = selectedSource;
    const componentId = sourceComponentId(sourceName);
    return {
      id: componentId,
      typeLabel: "source",
      plugin: source.plugin,
      connections: withoutNullishFields({
        on_success: source.on_success,
        on_validation_failure: source.on_validation_failure,
      }),
      options: source.options,
    };
  }

  const node = compositionState.nodes.find((candidate) => candidate.id === selectedNodeId);
  if (node) {
    return {
      id: node.id,
      typeLabel: node.node_type,
      plugin: node.plugin,
      connections: withoutNullishFields({
        input: node.input,
        on_success: node.on_success,
        on_error: node.on_error,
        condition: node.condition,
        routes: node.routes,
        fork_to: node.fork_to,
        branches: node.branches,
        policy: node.policy,
        merge: node.merge,
        timeout_seconds: node.timeout_seconds,
      }),
      options: node.options,
    };
  }

  const output = compositionState.outputs.find(
    (candidate) => candidate.name === selectedNodeId,
  );
  if (output) {
    return {
      id: output.name,
      typeLabel: "sink",
      plugin: output.plugin,
      connections: {},
      options: output.options,
    };
  }

  return null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function ConfigValue({ value }: { value: unknown }): JSX.Element {
  if (value === null) {
    return <span className="graph-config-empty-value">not set</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <span className="graph-config-empty-value">empty list</span>;
    }
    return (
      <ul className="graph-config-list">
        {value.map((item, index) => (
          <li key={index}>
            <ConfigValue value={item} />
          </li>
        ))}
      </ul>
    );
  }
  if (isRecord(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) {
      return <span className="graph-config-empty-value">empty object</span>;
    }
    return (
      <dl className="graph-config-nested">
        {entries.map(([key, nestedValue]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>
              <ConfigValue value={nestedValue} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  if (typeof value === "boolean") {
    return <span>{value ? "true" : "false"}</span>;
  }
  return <span>{String(value)}</span>;
}

function ConfigRows({
  values,
  emptyText,
}: {
  values: Record<string, unknown>;
  emptyText: string;
}): JSX.Element {
  const entries = Object.entries(values);
  if (entries.length === 0) {
    return <p className="graph-config-empty-value">{emptyText}</p>;
  }
  return (
    <dl className="graph-config-rows">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>
            <ConfigValue value={value} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

function NodeConfigPanel({
  config,
  onClose,
  panelRef,
}: {
  config: SelectedComponentConfig;
  onClose: () => void;
  panelRef?: React.Ref<HTMLElement>;
}): JSX.Element {
  return (
    <aside
      ref={panelRef}
      // Programmatic focus target: keyboard selection via the a11y list
      // moves focus here so the opened panel is announced and reachable
      // without tabbing back through the whole canvas (elspeth-37f6f13132).
      tabIndex={-1}
      className="graph-config-panel"
      role="complementary"
      aria-label={`${config.id} configuration`}
    >
      <header className="graph-config-panel-header">
        <div>
          {/* ui/TypeBadge composes the same --color-badge-* tokens the inline
              style used; the primitive replaces the hand-rolled span
              (elspeth-e1c5ad0b53). Canvas-drawn badges keep the raw token
              values because React Flow / MiniMap need resolved colours. */}
          <TypeBadge type={config.typeLabel} />
          <h3>{config.id} config</h3>
          {config.plugin && (
            <p className="graph-config-plugin">{config.plugin}</p>
          )}
        </div>
        <button
          type="button"
          className="graph-config-close"
          onClick={onClose}
          aria-label="Close node configuration"
        >
          x
        </button>
      </header>

      <section className="graph-config-section">
        <h4>Connections</h4>
        <ConfigRows
          values={config.connections}
          emptyText="No explicit connections configured."
        />
      </section>

      <section className="graph-config-section">
        <h4>Plugin options</h4>
        <ConfigRows
          values={config.options}
          emptyText="No plugin options configured."
        />
      </section>
    </aside>
  );
}

// ── Dagre layout ─────────────────────────────────────────────────────────────

/**
 * Apply dagre layout to nodes and edges, returning positioned React Flow
 * nodes. Layout is top-to-bottom (TB) with reasonable spacing.
 */
function layoutGraph(
  rfNodes: Node[],
  rfEdges: PipelineGraphEdgeModel[],
): { nodes: Node[]; edges: PipelineGraphEdgeModel[] } {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "TB", nodesep: 60, ranksep: 100 });

  for (const node of rfNodes) {
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const edge of rfEdges) {
    g.setEdge(edge.source, edge.target);
  }

  dagre.layout(g);

  const positionedNodes = rfNodes.map((node) => {
    const pos = g.node(node.id);
    return {
      ...node,
      position: {
        x: pos.x - NODE_WIDTH / 2,
        y: pos.y - NODE_HEIGHT / 2,
      },
    };
  });

  return { nodes: positionedNodes, edges: rfEdges };
}

// ── GraphView component ──────────────────────────────────────────────────────

export function GraphView() {
  const compositionState = useSessionStore((s) => s.compositionState);
  const pendingProposalCount = useSessionStore(
    (s) =>
      s.compositionProposals.filter(
        (proposal) =>
          proposal.status === "pending" && proposal.affects.includes("graph"),
      ).length,
  );
  const selectedNodeId = useSessionStore((s) => s.selectedNodeId);
  const selectNode = useSessionStore((s) => s.selectNode);
  const { resolvedTheme } = useTheme();

  const validationResult = useExecutionStore((s) => s.validationResult);
  const selectedConfig = useMemo(
    () => selectedComponentConfig(compositionState, selectedNodeId),
    [compositionState, selectedNodeId],
  );

  // Keyboard-selection focus hand-off (elspeth-37f6f13132): activating an
  // a11y-list item flips aria-pressed, but the resulting NodeConfigPanel
  // opened silently elsewhere in the DOM. When the selection came from the
  // keyboard list, move focus to the panel once it renders. Mouse clicks on
  // canvas nodes do not set the flag, so pointer users keep their focus.
  const configPanelRef = useRef<HTMLElement | null>(null);
  const focusPanelOnOpenRef = useRef(false);
  useEffect(() => {
    if (selectedConfig && focusPanelOnOpenRef.current) {
      focusPanelOnOpenRef.current = false;
      configPanelRef.current?.focus();
    }
    if (!selectedConfig) {
      focusPanelOnOpenRef.current = false;
    }
  }, [selectedConfig]);

  const miniMapNodeKindById = useMemo(
    () => buildMiniMapNodeKindById(compositionState),
    [compositionState],
  );

  const getMiniMapNodeColor = useCallback(
    (node: Node) => {
      const nodeKind = miniMapNodeKindById.get(node.id);
      return readThemeColor(
        nodeKind
          ? `--color-badge-${nodeKind.replace("_", "-")}`
          : FALLBACK_MINIMAP_NODE_COLOR_VAR,
        FALLBACK_MINIMAP_NODE_COLOR_VAR,
      );
    },
    [miniMapNodeKindById],
  );

  const getMiniMapNodeStrokeColor = useCallback(
    () => readThemeColor(MINIMAP_NODE_STROKE_COLOR_VAR, FALLBACK_MINIMAP_NODE_COLOR_VAR),
    [],
  );

  // Node click handler — toggle selection
  const onNodeClick: NodeMouseHandler = useCallback(
    (_event, node) => {
      selectNode(selectedNodeId === node.id ? null : node.id);
    },
    [selectedNodeId, selectNode],
  );

  // Pane click handler — deselect when clicking background
  const onPaneClick = useCallback(() => {
    selectNode(null);
  }, [selectNode]);

  // Fit-to-view ONCE on first render. Using `fitView` as a static prop
  // re-triggers viewport reset whenever `nodesInitialized` flips (i.e. every
  // chat-driven topology change), which destroys the operator's pan/zoom.
  // The Controls fit-view button continues to honour `fitViewOptions` below.
  const handleInit: OnInit = useCallback((instance) => {
    instance.fitView();
  }, []);

  // Build a map of component_id → validation severity for border coloring
  const nodeValidationMap = useMemo(() => {
    const map: Record<string, ValidationStatus> = {};
    if (!validationResult) return map;

    // All nodes with errors
    for (const err of validationResult.errors) {
      if (err.component_id) {
        map[err.component_id] = "error";
      }
    }

    // All nodes with warnings (only if not already error)
    if (validationResult.warnings) {
      for (const warn of validationResult.warnings) {
        if (warn.component_id && !map[warn.component_id]) {
          map[warn.component_id] = "warning";
        }
      }
    }

    return map;
  }, [validationResult]);

  // Build a map of component_id → tooltip string with error/warning messages
  const nodeMessageMap = useMemo(() => {
    const map: Record<string, string> = {};
    if (!validationResult) return map;

    // Collect messages per component_id
    const errors: Record<string, string[]> = {};
    const warnings: Record<string, string[]> = {};

    for (const err of validationResult.errors) {
      if (err.component_id) {
        (errors[err.component_id] ??= []).push(err.message);
      }
    }

    if (validationResult.warnings) {
      for (const warn of validationResult.warnings) {
        if (warn.component_id) {
          (warnings[warn.component_id] ??= []).push(warn.message);
        }
      }
    }

    // Merge into tooltip strings
    const allIds = new Set([...Object.keys(errors), ...Object.keys(warnings)]);
    for (const id of allIds) {
      const parts: string[] = [];
      if (errors[id]) {
        parts.push(`Errors:\n${errors[id].map((m) => `- ${m}`).join("\n")}`);
      }
      if (warnings[id]) {
        parts.push(`Warnings:\n${warnings[id].map((m) => `- ${m}`).join("\n")}`);
      }
      map[id] = parts.join("\n\n");
    }

    return map;
  }, [validationResult]);

  const { nodes, edges } = useMemo(() => {
    if (!hasCompositionContent(compositionState)) {
      return {
        nodes: [] as Node[],
        edges: [] as PipelineGraphEdgeModel[],
      };
    }

    function makeRfNode(
      id: string,
      typeLabel: string,
      subtitle: string | null,
      badgeBg: string,
      badgeColor: string,
      validationStatus?: ValidationStatus,
      validationTooltip?: string,
      isSelected?: boolean,
    ): PipelineGraphNodeModel {
      const validationMarker = validationStatus
        ? VALIDATION_STATUS_MARKERS[validationStatus]
        : null;
      // Selection ring takes priority over validation border
      const borderStyle = isSelected
        ? "2px solid var(--color-selected-ring)"
        : validationStatus === "error"
          ? `2px solid ${VALIDATION_COLORS.invalid}`
          : validationStatus === "warning"
            ? `2px solid ${VALIDATION_COLORS.warning}`
            : validationStatus === "valid"
              ? `2px solid ${VALIDATION_COLORS.valid}`
              : "1px solid var(--color-border-strong)";

      return {
        id,
        type: PIPELINE_NODE_TYPE,
        data: {
          label: (
            <div
              className="graph-node-content"
              title={validationTooltip ?? (validationStatus === "valid" ? "Valid" : undefined)}
            >
              <div className="graph-node-header">
                <span
                  className="graph-node-badge"
                  style={{ backgroundColor: badgeBg, color: badgeColor }}
                >
                  {humanNodeType(typeLabel)}
                </span>
                <span className="graph-node-label">
                  {id}
                </span>
                {validationStatus && (
                  <span
                    className="graph-validation-dot"
                    role="img"
                    aria-label={validationMarker?.ariaLabel}
                    style={{
                      backgroundColor:
                        validationStatus === "error"
                          ? VALIDATION_COLORS.invalid
                          : validationStatus === "warning"
                            ? VALIDATION_COLORS.warning
                            : VALIDATION_COLORS.valid,
                    }}
                    title={
                      validationTooltip
                        ? validationTooltip
                        : validationMarker?.fallbackTitle
                    }
                  >
                    {validationMarker?.glyph}
                  </span>
                )}
              </div>
              {subtitle && (
                <div className="graph-node-subtitle">
                  {subtitle}
                </div>
              )}
            </div>
          ),
        },
        position: { x: 0, y: 0 },
        style: {
          backgroundColor: "var(--color-surface-elevated)",
          border: borderStyle,
          borderRadius: 8,
          width: NODE_WIDTH,
          height: NODE_HEIGHT,
          padding: 0,
          // Selection box-shadow for extra emphasis
          boxShadow: isSelected ? "0 0 0 3px var(--color-selected-ring)" : undefined,
          cursor: "pointer",
        },
      };
    }

    const rfNodes: PipelineGraphNodeModel[] = [];

    // Source nodes (synthetic — source names are producer roots in edges)
    for (const [sourceName, source] of sortedSourceEntries(compositionState)) {
      const componentId = sourceComponentId(sourceName);
      rfNodes.push(
        makeRfNode(
          componentId,
          "source",
          source.plugin,
          BADGE_BACKGROUNDS.source,
          BADGE_COLORS.source,
          nodeValidationMap[componentId],
          nodeMessageMap[componentId],
          selectedNodeId === componentId,
        ),
      );
    }

    // Pipeline nodes (transforms, gates, aggregations, coalesces)
    for (const node of compositionState.nodes) {
      rfNodes.push(
        makeRfNode(
          node.id,
          node.node_type,
          node.plugin,
          BADGE_BACKGROUNDS[node.node_type],
          BADGE_COLORS[node.node_type],
          nodeValidationMap[node.id],
          nodeMessageMap[node.id],
          selectedNodeId === node.id,
        ),
      );
    }

    // Output/sink nodes (synthetic — output names are used as to_node in edges)
    for (const output of compositionState.outputs) {
      rfNodes.push(
        makeRfNode(
          output.name,
          "sink",
          output.plugin,
          BADGE_BACKGROUNDS.sink,
          BADGE_COLORS.sink,
          nodeValidationMap[output.name],
          nodeMessageMap[output.name],
          selectedNodeId === output.name,
        ),
      );
    }

    // Build edges: start with explicit edges from compositionState
    const sourceIds = new Set(Object.keys(compositionState.sources));
    const toGraphNodeId = (id: string): string =>
      sourceIds.has(id) ? sourceComponentId(id) : id;

    const explicitEdges = compositionState.edges;
    const rfEdges: PipelineGraphEdgeModel[] = explicitEdges.map((edge, i) => ({
      id: `e-${edge.from_node}-${edge.to_node}-${i}`,
      source: toGraphNodeId(edge.from_node),
      target: toGraphNodeId(edge.to_node),
      label: edge.label ?? EDGE_LABEL_MAP[edge.edge_type] ?? edge.edge_type,
      data: {
        flowType: edge.edge_type === "on_error" ? "error" : "success",
      },
      animated: edge.edge_type === "on_error",
      style: {
        stroke: edge.edge_type === "on_error" ? EDGE_COLORS.error : EDGE_COLORS.normal,
        strokeWidth: 1.5,
      },
      labelStyle: { fontSize: 10, fill: EDGE_LABEL_COLOR },
    }));
    const explicitRfEdgeIds = new Set(rfEdges.map((edge) => edge.id));

    // Build a set of existing edge connections to avoid duplicates
    const existingConnections = new Set(
      rfEdges.map(e => `${e.source}->${e.target}`)
    );
    const explicitEdgeIndexesByConnection = new Map<string, number[]>();
    for (const [index, edge] of rfEdges.entries()) {
      const connectionKey = `${edge.source}->${edge.target}`;
      const indexes = explicitEdgeIndexesByConnection.get(connectionKey) ?? [];
      indexes.push(index);
      explicitEdgeIndexesByConnection.set(connectionKey, indexes);
    }
    const claimedExplicitRowUnionEdges = new Set<number>();
    function claimExplicitRowUnionAlias(
      connectionKey: string,
      alias: string,
      edgeType: EdgeFlowType,
    ): boolean {
      const candidateIndexes =
        explicitEdgeIndexesByConnection.get(connectionKey) ?? [];
      const matchingIndex = candidateIndexes.find(
        (index) =>
          !claimedExplicitRowUnionEdges.has(index)
          && explicitEdges[index]?.label === alias,
      );
      const unlabelledIndex = candidateIndexes.find(
        (index) =>
          !claimedExplicitRowUnionEdges.has(index)
          && explicitEdges[index]?.label === null,
      );
      const claimedIndex = matchingIndex ?? unlabelledIndex;
      if (claimedIndex === undefined) return false;

      claimedExplicitRowUnionEdges.add(claimedIndex);
      const isError = edgeType === "error";
      rfEdges[claimedIndex] = {
        ...rfEdges[claimedIndex],
        label: alias,
        data: {
          ...rfEdges[claimedIndex]!.data,
          flowType: edgeType,
        },
        animated: isError,
        style: {
          ...rfEdges[claimedIndex]!.style,
          stroke: isError ? EDGE_COLORS.error : EDGE_COLORS.normal,
        },
      };
      return true;
    }
    const nodeIds = new Set(rfNodes.map(n => n.id));

    // Always infer missing edges from connection properties.
    // ELSPETH uses a NAMED CONNECTION POINT model:
    // - source.on_success is a connection point name (e.g., "transform_in")
    // - node.input is the connection point name it reads from
    // - node.on_success/on_error/routes are connection points or sink names
    //
    // Edge inference strategy:
    // 1. Build a producer registry: connection_point → { nodeId, edgeType, label }
    // 2. For each node, look up node.input in the registry to find upstream producer
    // 3. For direct sink references (on_success/on_error/routes pointing to sink names),
    //    create edges directly since sinks are in nodeIds

    // Producer registry: connection_point_name → producers.
    // ELSPETH allows MANY producers to publish one connection name ONLY when a
    // declared queue node consumes it (structural fan-in, ADR-028). So this is a
    // MULTIMAP, not one-producer-per-connection: overwriting would silently drop
    // every producer but the last and misrender the intentional fan-in.
    type ProducerInfo = {
      nodeId: string;
      edgeType: EdgeFlowType;
      label: string;
    };
    const connectionProducers = new Map<string, ProducerInfo[]>();
    function registerProducer(connection: string, producer: ProducerInfo): void {
      const producers = connectionProducers.get(connection) ?? [];
      producers.push(producer);
      connectionProducers.set(connection, producers);
    }

    // Declared queue ids: a queue is the SOLE canonical producer of its own
    // connection for ordinary downstream lookup, and every producer publishing
    // that name draws an edge to the queue node.
    const queueIds = new Set(
      compositionState.nodes
        .filter((node) => node.node_type === "queue")
        .map((node) => node.id),
    );
    const rowUnionIds = new Set(
      compositionState.nodes
        .filter((node) => node.node_type === "row_union")
        .map((node) => node.id),
    );
    const authoritativeRowUnionOutboundSemantics = new Map<
      string,
      ProducerInfo[]
    >();
    function registerAuthoritativeRowUnionOutbound(
      connectionKey: string,
      producer: ProducerInfo,
    ): void {
      const semantics =
        authoritativeRowUnionOutboundSemantics.get(connectionKey) ?? [];
      if (
        semantics.some(
          (semantic) =>
            semantic.edgeType === producer.edgeType
            && semantic.label === producer.label,
        )
      ) {
        return;
      }
      semantics.push(producer);
      authoritativeRowUnionOutboundSemantics.set(connectionKey, semantics);
    }

    // Each source produces on its on_success connection
    for (const [sourceName, source] of sortedSourceEntries(compositionState)) {
      if (source.on_success) {
        registerProducer(source.on_success, {
          nodeId: sourceComponentId(sourceName),
          edgeType: "success",
          label: "success",
        });
      }
    }

    // Each node can produce on on_success, on_error, or routes. Queue nodes have
    // none of these (their output is implicit under their own id), so they
    // register nothing here.
    for (const node of compositionState.nodes) {
      if (node.on_success) {
        registerProducer(node.on_success, {
          nodeId: node.id,
          edgeType: "success",
          label: "success",
        });
      }
      if (node.on_error) {
        registerProducer(node.on_error, {
          nodeId: node.id,
          edgeType: "error",
          label: "error",
        });
      }
      if (node.routes) {
        for (const [routeLabel, targetConn] of Object.entries(node.routes)) {
          registerProducer(targetConn, {
            nodeId: node.id,
            edgeType: "success",
            label: routeLabel,
          });
        }
      }
      if (
        node.node_type === "gate"
        && node.routes
        && Object.values(node.routes).includes("fork")
        && node.fork_to
      ) {
        for (const branchConnection of node.fork_to) {
          registerProducer(branchConnection, {
            nodeId: node.id,
            edgeType: "success",
            label: branchConnection,
          });
        }
      }
    }

    // Phase 1: draw every producer → row_union edge from the authoritative
    // alias→connection mapping. NodeSpec.input is only the backend-compatible
    // first-branch placeholder for row_union; it is deliberately ignored so
    // the graph cannot invent a duplicate unlabelled input edge.
    const inferredRowUnionAliases = new Set<string>();
    // Row unions whose aliases phase 1 actually enumerated. Recorded here, at
    // the one place the predicate is evaluated, so the phase-1b guard can
    // never drift from "phase 1 spoke for this node's inbound wiring".
    const aliasMappedRowUnionIds = new Set<string>();
    for (const rowUnion of compositionState.nodes.filter(
      (node) => node.node_type === "row_union",
    )) {
      const branches =
        rowUnion.branches !== null
        && rowUnion.branches !== undefined
        && !Array.isArray(rowUnion.branches)
          ? Object.entries(rowUnion.branches)
          : [];
      if (branches.length === 0) continue;
      aliasMappedRowUnionIds.add(rowUnion.id);
      for (const [alias, connection] of branches) {
        const producers: ProducerInfo[] = queueIds.has(connection)
          ? [{
              nodeId: connection,
              edgeType: "success",
              label: "success",
            }]
          : [...(connectionProducers.get(connection) ?? [])]
              .sort((a, b) => a.nodeId.localeCompare(b.nodeId));
        for (const producer of producers) {
          if (producer.nodeId === rowUnion.id) continue;
          const connectionKey = `${producer.nodeId}->${rowUnion.id}`;
          const aliasKey = `${connectionKey}:${alias}`;
          if (inferredRowUnionAliases.has(aliasKey)) continue;
          if (
            claimExplicitRowUnionAlias(
              connectionKey,
              alias,
              producer.edgeType,
            )
          ) {
            inferredRowUnionAliases.add(aliasKey);
            continue;
          }
          const isError = producer.edgeType === "error";
          rfEdges.push({
            id: inferredEdgeId(
              "row-union-in",
              producer.nodeId,
              rowUnion.id,
              alias,
            ),
            source: producer.nodeId,
            target: rowUnion.id,
            label: alias,
            data: { flowType: producer.edgeType },
            animated: isError,
            style: {
              stroke: isError ? EDGE_COLORS.error : EDGE_COLORS.normal,
              strokeWidth: 1.5,
            },
            labelStyle: { fontSize: 10, fill: EDGE_LABEL_COLOR },
          });
          inferredRowUnionAliases.add(aliasKey);
          existingConnections.add(connectionKey);
        }
      }
    }

    // Phase 1b: once phase 1 has spoken, `branches` is the ONLY route into an
    // alias-mapped row union — every inbound lane, of every edge_type, comes
    // from that mapping (phase 1 carries error styling via producer.edgeType).
    // An explicit edge into such a union that no alias claimed is stale
    // wiring: `with_node` replaces a node in place and never reconciles
    // `edges` (unlike `without_node`, which prunes edges touching the removed
    // node), and validation only checks that edge endpoints resolve — never
    // that a label names a live alias. So a renamed alias or a repointed
    // branch leaves a validation-VALID composition whose edge list still
    // describes the old wiring. Rendering it would show operators — in the
    // diagram AND in its accessible text alternative — a route into the union
    // that the authoritative state does not have.
    //
    // Removal is by descending index so the remaining explicit indexes stay
    // aligned; `existingConnections` is deliberately left intact, since it
    // only ever suppresses duplicates and phase 1 does not consult it.
    const staleRowUnionEdgeIndexes: number[] = [];
    for (let index = 0; index < explicitEdges.length; index += 1) {
      if (claimedExplicitRowUnionEdges.has(index)) continue;
      const target = rfEdges[index]?.target;
      if (target === undefined || !aliasMappedRowUnionIds.has(target)) continue;
      staleRowUnionEdgeIndexes.push(index);
    }
    for (const index of staleRowUnionEdgeIndexes.reverse()) {
      rfEdges.splice(index, 1);
    }

    // Phase 2: draw every producer → queue edge. Deterministic (sorted by
    // producer id) so source insertion order cannot change the output; a
    // self-producer is skipped. This runs BEFORE the direct-edge blocks below so
    // these ids own the producer→queue pairs and the dedup set suppresses the
    // direct-block duplicates (a queue IS a node, so nodeIds.has(queueId) is true).
    for (const queueId of queueIds) {
      const producers = [...(connectionProducers.get(queueId) ?? [])].sort((a, b) =>
        a.nodeId.localeCompare(b.nodeId),
      );
      for (const producer of producers) {
        if (producer.nodeId === queueId) continue; // no queue self-loop
        const connectionKey = `${producer.nodeId}->${queueId}`;
        if (rowUnionIds.has(producer.nodeId)) {
          registerAuthoritativeRowUnionOutbound(connectionKey, producer);
        }
        if (existingConnections.has(connectionKey)) continue;
        const isError = producer.edgeType === "error";
        rfEdges.push({
          id: inferredEdgeId("queue-in", producer.nodeId, queueId),
          source: producer.nodeId,
          target: queueId,
          label: producer.label,
          data: { flowType: producer.edgeType },
          animated: isError,
          style: {
            stroke: isError ? EDGE_COLORS.error : EDGE_COLORS.normal,
            strokeWidth: 1.5,
          },
          labelStyle: { fontSize: 10, fill: EDGE_LABEL_COLOR },
        });
        existingConnections.add(connectionKey);
      }
    }

    // Phase 3: infer edges by matching node.input to its upstream producer.
    for (const node of compositionState.nodes) {
      if (!node.input) continue;
      if (rowUnionIds.has(node.id)) continue;

      // A queue is the SOLE canonical producer of its own connection: synthesise
      // the queue node as the producer (queue → consumer), never the upstream
      // sources (which would be a dishonest producer → consumer bypass).
      if (queueIds.has(node.input)) {
        if (node.input === node.id) continue; // the queue's own implicit output
        if (!existingConnections.has(`${node.input}->${node.id}`)) {
          rfEdges.push({
            id: inferredEdgeId("queue-out", node.input, node.id),
            source: node.input,
            target: node.id,
            label: "success",
            data: { flowType: "success" },
            style: { stroke: EDGE_COLORS.normal, strokeWidth: 1.5 },
            labelStyle: { fontSize: 10, fill: EDGE_LABEL_COLOR },
          });
          existingConnections.add(`${node.input}->${node.id}`);
        }
        continue;
      }

      const producers = [...(connectionProducers.get(node.input) ?? [])].sort((a, b) =>
        a.nodeId.localeCompare(b.nodeId),
      );
      for (const producer of producers) {
        if (producer.nodeId === node.id) continue;
        const connectionKey = `${producer.nodeId}->${node.id}`;
        if (rowUnionIds.has(producer.nodeId)) {
          registerAuthoritativeRowUnionOutbound(connectionKey, producer);
        }
        if (existingConnections.has(connectionKey)) continue;
        const isError = producer.edgeType === "error";
        rfEdges.push({
          id: rowUnionIds.has(producer.nodeId)
            ? inferredEdgeId("row-union-out", producer.nodeId, node.id)
            : inferredEdgeId("conn", producer.nodeId, node.id),
          source: producer.nodeId,
          target: node.id,
          label: producer.label,
          data: { flowType: producer.edgeType },
          animated: isError,
          style: {
            stroke: isError ? EDGE_COLORS.error : EDGE_COLORS.normal,
            strokeWidth: 1.5,
          },
          labelStyle: { fontSize: 10, fill: EDGE_LABEL_COLOR },
        });
        existingConnections.add(connectionKey);
      }
    }

    // Handle source → sink direct edges (no transforms in between)
    for (const [sourceName, source] of sortedSourceEntries(compositionState)) {
      const sourceId = sourceComponentId(sourceName);
      if (
        source.on_success &&
        nodeIds.has(source.on_success) &&
        !existingConnections.has(`${sourceId}->${source.on_success}`)
      ) {
        rfEdges.push({
          id: inferredEdgeId("sink", sourceId, source.on_success),
          source: sourceId,
          target: source.on_success,
          label: "success",
          data: { flowType: "success" },
          style: { stroke: EDGE_COLORS.normal, strokeWidth: 1.5 },
          labelStyle: { fontSize: 10, fill: EDGE_LABEL_COLOR },
        });
        existingConnections.add(`${sourceId}->${source.on_success}`);
      }
    }

    // Handle direct sink references (on_success/on_error/routes pointing to sink names)
    // Sinks are in nodeIds, so we can create edges directly to them
    for (const node of compositionState.nodes) {
      // on_success → sink (only if target is a sink, not a connection point)
      const successConnectionKey = `${node.id}->${node.on_success}`;
      if (
        rowUnionIds.has(node.id)
        && node.on_success
        && nodeIds.has(node.on_success)
      ) {
        registerAuthoritativeRowUnionOutbound(successConnectionKey, {
          nodeId: node.id,
          edgeType: "success",
          label: "success",
        });
      }
      if (
        node.on_success &&
        nodeIds.has(node.on_success) &&
        !existingConnections.has(successConnectionKey)
      ) {
        rfEdges.push({
          id: inferredEdgeId("sink", node.id, node.on_success),
          source: node.id,
          target: node.on_success,
          label: "success",
          data: { flowType: "success" },
          style: { stroke: EDGE_COLORS.normal, strokeWidth: 1.5 },
          labelStyle: { fontSize: 10, fill: EDGE_LABEL_COLOR },
        });
        existingConnections.add(successConnectionKey);
      }

      // on_error → sink
      const errorConnectionKey = `${node.id}->${node.on_error}`;
      if (
        rowUnionIds.has(node.id)
        && node.on_error
        && nodeIds.has(node.on_error)
      ) {
        registerAuthoritativeRowUnionOutbound(errorConnectionKey, {
          nodeId: node.id,
          edgeType: "error",
          label: "error",
        });
      }
      if (
        node.on_error &&
        nodeIds.has(node.on_error) &&
        !existingConnections.has(errorConnectionKey)
      ) {
        rfEdges.push({
          id: inferredEdgeId("sink", node.id, node.on_error, "error"),
          source: node.id,
          target: node.on_error,
          label: "error",
          data: { flowType: "error" },
          animated: true,
          style: { stroke: EDGE_COLORS.error, strokeWidth: 1.5 },
          labelStyle: { fontSize: 10, fill: EDGE_LABEL_COLOR },
        });
        existingConnections.add(errorConnectionKey);
      }

      // Gate routes → sink
      if (node.routes) {
        for (const [routeLabel, targetId] of Object.entries(node.routes)) {
          const routeConnectionKey = `${node.id}->${targetId}`;
          if (rowUnionIds.has(node.id) && nodeIds.has(targetId)) {
            registerAuthoritativeRowUnionOutbound(routeConnectionKey, {
              nodeId: node.id,
              edgeType: "success",
              label: routeLabel,
            });
          }
          if (nodeIds.has(targetId) && !existingConnections.has(routeConnectionKey)) {
            rfEdges.push({
              id: inferredEdgeId("sink", node.id, targetId, routeLabel),
              source: node.id,
              target: targetId,
              label: routeLabel,
              data: { flowType: "success" },
              style: { stroke: EDGE_COLORS.normal, strokeWidth: 1.5 },
              labelStyle: { fontSize: 10, fill: EDGE_LABEL_COLOR },
            });
            existingConnections.add(routeConnectionKey);
          }
        }
      }
    }

    // A row union's connection properties are authoritative for its outbound
    // semantics just as `branches` is authoritative for its inbound topology.
    // Explicit edges are materialized render hints and can retain a stale
    // label/route even when their endpoints still match. Claim at most one hint
    // per live semantic, rewrite it from authority, and drop every unclaimed
    // row_union hint. When no explicit hint exists, the inference phase above
    // has already emitted the authoritative edge.
    const claimedExplicitRowUnionOutboundEdgeIds = new Set<string>();
    for (
      const [connectionKey, semantics]
      of authoritativeRowUnionOutboundSemantics
    ) {
      const candidates = rfEdges.filter(
        (edge) =>
          explicitRfEdgeIds.has(edge.id)
          && rowUnionIds.has(edge.source)
          && `${edge.source}->${edge.target}` === connectionKey,
      );
      for (const semantic of semantics) {
        const unclaimed = candidates.filter(
          (edge) => !claimedExplicitRowUnionOutboundEdgeIds.has(edge.id),
        );
        const claimed =
          unclaimed.find(
            (edge) =>
              edge.data.flowType === semantic.edgeType
              && edge.label === semantic.label,
          )
          ?? unclaimed.find(
            (edge) => edge.data.flowType === semantic.edgeType,
          )
          ?? unclaimed[0];
        if (!claimed) continue;

        claimedExplicitRowUnionOutboundEdgeIds.add(claimed.id);
        const claimedIndex = rfEdges.findIndex(
          (edge) => edge.id === claimed.id,
        );
        const isError = semantic.edgeType === "error";
        rfEdges[claimedIndex] = {
          ...claimed,
          label: semantic.label,
          data: {
            ...claimed.data,
            flowType: semantic.edgeType,
          },
          animated: isError,
          style: {
            ...claimed.style,
            stroke: isError ? EDGE_COLORS.error : EDGE_COLORS.normal,
          },
        };
      }
    }
    for (let index = rfEdges.length - 1; index >= 0; index -= 1) {
      const edge = rfEdges[index]!;
      if (!explicitRfEdgeIds.has(edge.id) || !rowUnionIds.has(edge.source)) {
        continue;
      }
      if (claimedExplicitRowUnionOutboundEdgeIds.has(edge.id)) {
        continue;
      }
      rfEdges.splice(index, 1);
    }

    const parallelGeometry = assignParallelEdgeLanes(rfNodes, rfEdges);
    return layoutGraph(parallelGeometry.nodes, parallelGeometry.edges);
  }, [compositionState, nodeValidationMap, nodeMessageMap, selectedNodeId]);

  // Accessible, keyboard-operable equivalent of the visual DAG. The React Flow
  // diagram is a visual image (role="img", scoped to the diagram element
  // only); this list is its text alternative
  // (WCAG 1.1.1 — elspeth-ef897110dd) AND the keyboard path to node inspection,
  // since onNodeClick is mouse-only (WCAG 2.1.1 — elspeth-d37b7217c9). Activating
  // an item drives the same selectedNodeId store that opens NodeConfigPanel.
  const accessibleNodes = useMemo(() => {
    if (!compositionState) {
      return [] as Array<{ id: string; status?: ValidationStatus; label: string }>;
    }
    const describe = (
      id: string,
      typeLabel: string,
      plugin: string | null,
    ): { id: string; status?: ValidationStatus; label: string } => {
      const status = nodeValidationMap[id];
      const validity =
        status === "error"
          ? "has validation errors"
          : status === "warning"
            ? "has warnings"
            : status === "valid"
              ? "valid"
              : "not yet validated";
      const message = nodeMessageMap[id];
      return {
        id,
        status,
        label: `${humanNodeType(typeLabel)}: ${id}${plugin ? ` (${plugin})` : ""} — ${validity}${message ? `. ${message}` : ""}`,
      };
    };
    const out: Array<{ id: string; status?: ValidationStatus; label: string }> = [];
    for (const [sourceName, source] of sortedSourceEntries(compositionState)) {
      out.push(describe(sourceComponentId(sourceName), "source", source.plugin));
    }
    for (const node of compositionState.nodes) {
      out.push(describe(node.id, node.node_type, node.plugin));
    }
    for (const output of compositionState.outputs) {
      out.push(describe(output.name, "sink", output.plugin));
    }
    return out;
  }, [compositionState, nodeValidationMap, nodeMessageMap]);

  const accessibleEdges = useMemo(
    () =>
      edges.map((edge) => ({
        id: edge.id,
        label:
          `${edge.source} to ${edge.target}: ${
            typeof edge.label === "string" ? edge.label : "connection"
          } (${edge.data.flowType})`,
      })),
    [edges],
  );

  // Empty state — must match the hasContent check above so that a
  // source-to-sink pipeline (zero transform nodes) still renders.
  if (nodes.length === 0) {
    return (
      <div
        className="empty-state"
      >
        No pipeline to visualise. Start a conversation to build one.
      </div>
    );
  }

  const nodeCount = nodes.length;
  const rowUnionCount =
    compositionState?.nodes.filter((node) => node.node_type === "row_union")
      .length ?? 0;
  const rowUnionSummary =
    rowUnionCount > 0
      ? `, including ${rowUnionCount} row union${rowUnionCount === 1 ? "" : "s"}`
      : "";
  const ariaLabel =
    `Pipeline graph with ${nodeCount} component${nodeCount !== 1 ? "s" : ""}`
    + `${rowUnionSummary} (sources, processing steps, sinks).`;

  return (
    // ReactFlowProvider lets Controls/MiniMap render OUTSIDE the <ReactFlow>
    // element while sharing its store — required so they can live outside the
    // role="img" diagram scope below (elspeth-37f6f13132).
    <ReactFlowProvider>
      <div
        className="graph-view-shell"
      >
        {/* Accessible + keyboard-operable equivalent of the visual DAG.
            The diagram below stays role="img" (a visual diagram); THIS list is
            its text alternative (WCAG 1.1.1) and the keyboard path to node
            inspection (WCAG 2.1.1), since onNodeClick is mouse-only. Visually
            hidden until a child is focused (.graph-a11y-list, inspector.css) —
            a real keyboard target with a visible focus state, not an invisible
            tab trap. */}
        <ol
          className="graph-a11y-list"
          aria-label={`Pipeline components in source-to-sink order (${accessibleNodes.length})`}
        >
          {accessibleNodes.map((node) => (
            <li key={node.id}>
              <button
                type="button"
                aria-pressed={selectedNodeId === node.id}
                onClick={() => {
                  const nextNodeId =
                    selectedNodeId === node.id ? null : node.id;
                  // Keyboard path: hand focus to the config panel once it
                  // opens (see the focusPanelOnOpen effect above).
                  focusPanelOnOpenRef.current = nextNodeId !== null;
                  selectNode(nextNodeId);
                }}
              >
                {node.label}. Activate to inspect.
              </button>
            </li>
          ))}
        </ol>
        <ul
          className="visually-hidden"
          aria-label="Pipeline branch connections"
        >
          {accessibleEdges.map((edge) => (
            <li key={edge.id}>{edge.label}</li>
          ))}
        </ul>
        {/* role="img" is children-presentational: everything inside it is
            pruned from the accessibility tree. It therefore scopes the
            DIAGRAM ONLY — the live "pending #N" pill and the interactive
            zoom/fit Controls are siblings so AT can still reach them
            (WCAG 4.1.3 / 1.3.1, elspeth-37f6f13132). */}
        <div className="graph-view-canvas">
          {pendingProposalCount > 0 && (
            <div
              role="status"
              className="pending-overlay-pill"
              aria-label={`${pendingProposalCount} pending graph proposal${pendingProposalCount === 1 ? "" : "s"}`}
            >
              pending #{pendingProposalCount}
            </div>
          )}
          <div
            className="graph-view-diagram"
            aria-label={ariaLabel}
            aria-roledescription="Pipeline DAG diagram"
            role="img"
          >
            <ReactFlow
              nodes={nodes}
              edges={edges}
              edgeTypes={EDGE_TYPES}
              nodeTypes={NODE_TYPES}
              nodesDraggable={false}
              nodesConnectable={false}
              // Visual nodes are display-only; keyboard node selection is
              // provided by the .graph-a11y-list above, so the canvas nodes
              // are not extra (invisible, role="img"-hidden) tab stops.
              nodesFocusable={false}
              elementsSelectable={true}
              onNodeClick={onNodeClick}
              onPaneClick={onPaneClick}
              colorMode={resolvedTheme}
              onInit={handleInit}
              fitViewOptions={{ padding: 0.15, maxZoom: 1.5, minZoom: 0.3 }}
              proOptions={{ hideAttribution: true }}
            >
              <Background gap={16} size={1} color="var(--color-canvas-grid)" />
            </ReactFlow>
          </div>
          <Controls showInteractive={false} />
          {nodes.length > MINIMAP_NODE_COUNT_THRESHOLD && (
            <MiniMap
              bgColor="var(--color-surface)"
              nodeColor={getMiniMapNodeColor}
              nodeStrokeColor={getMiniMapNodeStrokeColor}
              nodeStrokeWidth={3}
              zoomable
              pannable
              style={{ bottom: 8, right: 8, width: 120, height: 80 }}
            />
          )}
        </div>
        {selectedConfig && (
          <NodeConfigPanel
            config={selectedConfig}
            onClose={() => selectNode(null)}
            panelRef={configPanelRef}
          />
        )}
      </div>
    </ReactFlowProvider>
  );
}
