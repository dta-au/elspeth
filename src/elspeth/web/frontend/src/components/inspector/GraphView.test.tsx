import { describe, it, expect, beforeEach, vi } from "vitest";
import { readFileSync } from "node:fs";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GraphView } from "./GraphView";
import { useSessionStore } from "@/stores/sessionStore";
import { useExecutionStore } from "@/stores/executionStore";
import type { CompositionProposal, CompositionState, NodeSpec, EdgeSpec } from "@/types/index";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import { projectValidationWorkspaceStatus } from "@/components/workspace/workspaceStatus";

// Mock @xyflow/react — jsdom cannot do DOM measurements required by React Flow.
// Render ordinary elements directly and invoke custom node/edge renderers with
// deterministic coordinates so geometry and handle placement stay testable.
vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  // Provider shares the flow store so Controls/MiniMap can render OUTSIDE
  // <ReactFlow> (they are siblings of the role="img" diagram scope —
  // elspeth-37f6f13132). The mock just passes children through.
  ReactFlowProvider: ({ children }: any) => (
    <div data-testid="react-flow-provider">{children}</div>
  ),
  ReactFlow: ({
    nodes,
    edges,
    edgeTypes,
    nodeTypes,
    children,
    colorMode,
    fitView,
    onInit,
    fitViewOptions,
    onNodeClick,
    nodesFocusable,
    edgesFocusable,
  }: any) => (
    <div
      data-testid="react-flow"
      data-color-mode={colorMode}
      // Structural assertions: bug elspeth-0e2d449d82 (GraphView fitView reset).
      // `fitView` boolean prop must NOT be passed — it re-fires on every
      // topology change and resets the operator's pan/zoom. `onInit` must be
      // passed so the imperative fitView() runs once on mount only.
      data-fit-view-prop={fitView === undefined ? "absent" : String(Boolean(fitView))}
      data-has-on-init={String(typeof onInit === "function")}
      data-fit-view-options={fitViewOptions ? JSON.stringify(fitViewOptions) : ""}
      // Focusability assertions (elspeth-437caadef3): both must be explicitly
      // false — React Flow defaults them to true, which puts invisible tab
      // stops inside the role="img" (children-presentational) diagram scope.
      data-nodes-focusable={nodesFocusable === undefined ? "absent" : String(Boolean(nodesFocusable))}
      data-edges-focusable={edgesFocusable === undefined ? "absent" : String(Boolean(edgesFocusable))}
    >
      {nodes?.map((n: any) => {
        const NodeRenderer = nodeTypes?.[n.type];
        return (
          <div
            key={n.id}
            data-testid={`node-${n.id}`}
            style={n.style}
            // jsdom cannot compute a var()-bearing shorthand, and React Flow's
            // real geometry never runs here, so the node's style OBJECT is the
            // testable truth about what GraphView hands the library
            // (elspeth-003794d55c).
            data-node-style={JSON.stringify(n.style ?? null)}
            onClick={(event) => onNodeClick?.(event, n)}
          >
            {NodeRenderer
              ? <NodeRenderer id={n.id} data={n.data} />
              : typeof n.data?.label === "string"
                ? n.data.label
                : n.data?.label}
          </div>
        );
      })}
      {edges?.map((e: any) => {
        const EdgeRenderer = edgeTypes?.[e.type];
        if (EdgeRenderer) {
          return (
            <svg
              key={e.id}
              data-testid={`edge-${e.id}`}
              data-edge-source={e.source}
              data-edge-target={e.target}
              data-source-handle={e.sourceHandle}
              data-target-handle={e.targetHandle}
              // The edge's OWN markerEnd, before the harness overrides it below
              // with a synthetic url(#...) so BaseEdge has something to render.
              // Direction must reach every edge, lane-assigned or not
              // (elspeth-ddae27dff1).
              data-marker-end={JSON.stringify(e.markerEnd ?? null)}
            >
              <EdgeRenderer
                {...e}
                sourceX={100 + (e.data?.laneOffset ?? 0)}
                sourceY={80}
                targetX={100 + (e.data?.laneOffset ?? 0)}
                targetY={260}
                sourcePosition="bottom"
                targetPosition="top"
                markerEnd={`url(#marker-${e.id})`}
              />
            </svg>
          );
        }
        return (
          <div
            key={e.id}
            data-testid={`edge-${e.id}`}
            data-edge-source={e.source}
            data-edge-target={e.target}
            data-marker-end={JSON.stringify(e.markerEnd ?? null)}
          >
            {e.label}
          </div>
        );
      })}
      {children}
    </div>
  ),
  BaseEdge: ({
    id,
    path,
    label,
    labelX,
    labelY,
    markerEnd,
  }: any) => (
    <g>
      <path
        data-edge-path-id={id}
        d={path}
        markerEnd={markerEnd}
      />
      <text x={labelX} y={labelY}>{label}</text>
    </g>
  ),
  Handle: ({ id, type, position, style }: any) => (
    <span
      data-handle-id={id}
      data-handle-type={type}
      data-handle-position={position}
      style={style}
    />
  ),
  Position: { Top: "top", Bottom: "bottom" },
  Background: ({ color, gap, size }: any) => (
    <div
      data-testid="react-flow-background"
      data-color={color}
      data-gap={gap}
      data-size={size}
    />
  ),
  Controls: ({ showInteractive, fitViewOptions }: any) => (
    <div
      data-testid="react-flow-controls"
      data-show-interactive={String(showInteractive)}
      // ControlsComponent calls fitView() with its OWN fitViewOptions prop, not
      // <ReactFlow>'s, so the two must be asserted separately
      // (elspeth-a8074a3a7b).
      data-fit-view-options={fitViewOptions ? JSON.stringify(fitViewOptions) : ""}
    />
  ),
  MiniMap: ({ nodeColor, nodeStrokeColor, bgColor, nodeStrokeWidth, style }: any) => (
    <div
      data-testid="minimap"
      data-bg-color={bgColor}
      data-node-stroke-width={nodeStrokeWidth}
      // Inline insets STACK on .react-flow__panel's own 15px margin rather than
      // replacing it, so their absence is the assertion (elspeth-a7ce6e6a4c).
      data-style={JSON.stringify(style ?? null)}
      data-source-color={nodeColor?.({ id: "source" })}
      data-gate-color={nodeColor?.({ id: "quality_gate" })}
      data-sink-color={nodeColor?.({ id: "results" })}
      // Queue-kind probe: a queue node with id "inbound" must colour via its
      // own --color-badge-queue token (Task 6 minimap recognition). Harmless
      // for compositions without such a node — it resolves to the fallback.
      data-queue-color={nodeColor?.({ id: "inbound" })}
      data-row-union-color={nodeColor?.({ id: "variant_union" })}
      data-unknown-color={nodeColor?.({ id: "unknown" })}
      data-stroke-color={nodeStrokeColor?.({ id: "source" })}
    />
  ),
}));

vi.mock("@/hooks/useTheme", () => ({
  useTheme: () => ({
    theme: "system",
    resolvedTheme: "light",
    setTheme: vi.fn(),
    toggleTheme: vi.fn(),
  }),
}));

// Mock @dagrejs/dagre — layout is not needed in tests.
vi.mock("@dagrejs/dagre", () => ({
  default: {
    graphlib: {
      Graph: class {
        setDefaultEdgeLabel() {}
        setGraph() {}
        setNode() {}
        setEdge() {}
        node(_id: string) { return { x: 0, y: 0 }; }
      },
    },
    layout() {},
  },
}));

// Mock React Flow CSS to avoid import errors in jsdom.
vi.mock("@xyflow/react/dist/style.css", () => ({}));

// ── Helpers ──────────────────────────────────────────────────────────────────

function makeNode(overrides: Partial<NodeSpec> = {}): NodeSpec {
  return {
    id: "n1",
    node_type: "transform",
    plugin: "llm_transform",
    input: "source_out",
    on_success: "main",
    on_error: null,
    options: {},
    ...overrides,
  };
}

function makeEdge(overrides: Partial<EdgeSpec> = {}): EdgeSpec {
  return {
    id: "e1",
    from_node: "n1",
    to_node: "n2",
    edge_type: "on_success",
    label: null,
    ...overrides,
  };
}

function makeState(overrides: Partial<CompositionState> = {}): CompositionState {
  return {
    id: "test-session",
    ...compositionStateAuthorityFields,
    version: 1,
    sources: {},
    nodes: [],
    edges: [],
    outputs: [],
    metadata: { name: "test", description: "" },
    ...overrides,
  };
}

function makeProposal(
  overrides: Partial<CompositionProposal> = {},
): CompositionProposal {
  return {
    id: "proposal-1",
    session_id: "session-1",
    tool_call_id: "call-1",
    tool_name: "set_pipeline",
    status: "pending",
    summary: "Replace the pipeline.",
    rationale: "Requested by the current composer turn.",
    affects: ["graph", "validation", "yaml"],
    arguments_redacted_json: {},
    base_state_id: null,
    committed_state_id: null,
    audit_event_id: "event-1",
    created_at: "2026-05-14T00:00:00Z",
    updated_at: "2026-05-14T00:00:00Z",
    ...overrides,
  };
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe("GraphView", () => {
  beforeEach(() => {
    useSessionStore.setState({ compositionState: null, compositionProposals: [] });
    // selectedNodeId is store state, not a per-render prop: a test that opens
    // the NodeConfigPanel leaks its selection into every later test in file
    // order unless it is reset here.
    useSessionStore.setState({ selectedNodeId: null } as never);
    useExecutionStore.setState({ validationResult: null } as never);
    document.documentElement.removeAttribute("style");
  });

  it("renders nodes with type badge and plugin name", () => {
    useSessionStore.setState({
      compositionState: makeState({
        nodes: [makeNode({ id: "classify", node_type: "transform", plugin: "llm_transform" })],
      }),
    });
    render(<GraphView />);
    // The badge renders node.node_type
    expect(screen.getByText("transform")).toBeInTheDocument();
    // The node ID as display name
    expect(screen.getByText("classify")).toBeInTheDocument();
    // The plugin name
    expect(screen.getByText("llm_transform")).toBeInTheDocument();
  });

  it("renders a pending proposal pill when proposal affects graph", () => {
    useSessionStore.setState({
      compositionState: makeState({
        nodes: [
          makeNode({
            id: "classify",
            node_type: "transform",
            plugin: "llm_transform",
          }),
        ],
      }),
      compositionProposals: [makeProposal()],
    });

    render(<GraphView />);

    expect(screen.getByText("pending #1")).toBeInTheDocument();
  });

  it("opens a structured plugin configuration panel when a graph node is clicked", async () => {
    const user = userEvent.setup();
    useSessionStore.setState({
      compositionState: makeState({
        nodes: [
          makeNode({
            id: "colour_lookup",
            node_type: "transform",
            plugin: "llm",
            options: {
              prompt: "Find colours",
              output_schema: {
                fields: ["url", "colours"],
              },
            },
          }),
        ],
      }),
    });

    render(<GraphView />);
    await user.click(screen.getByTestId("node-colour_lookup"));

    const panel = screen.getByRole("complementary", {
      name: /colour_lookup configuration/i,
    });
    expect(panel).toBeInTheDocument();
    expect(
      within(panel).getByRole("heading", { name: /colour_lookup config/i }),
    ).toBeInTheDocument();
    // elspeth-e1c5ad0b53: the panel's type chip is the ui/TypeBadge primitive
    // composing the shared .type-badge-* token classes.
    const typeChip = within(panel).getByText("transform");
    expect(typeChip).toHaveClass("type-badge", "type-badge-transform");
    expect(within(panel).getByText("llm")).toBeInTheDocument();
    expect(within(panel).getByText("prompt")).toBeInTheDocument();
    expect(within(panel).getByText("Find colours")).toBeInTheDocument();
    expect(within(panel).getByText("output_schema")).toBeInTheDocument();
    expect(within(panel).getByText("fields")).toBeInTheDocument();
    expect(within(panel).getByText("url")).toBeInTheDocument();
    expect(within(panel).queryByText(/^\{.*\}$/)).not.toBeInTheDocument();
  });

  it("renders edge labels for on_success", () => {
    useSessionStore.setState({
      compositionState: makeState({
        nodes: [
          makeNode({ id: "n1", node_type: "transform", plugin: "p" }),
          makeNode({ id: "n2", node_type: "transform", plugin: "q" }),
        ],
        edges: [makeEdge({ id: "e1", from_node: "n1", to_node: "n2", edge_type: "on_success" })],
      }),
    });
    render(<GraphView />);
    // EDGE_LABEL_MAP maps on_success -> "success"
    expect(screen.getByText("success")).toBeInTheDocument();
  });

  it("renders edge labels for on_error", () => {
    useSessionStore.setState({
      compositionState: makeState({
        nodes: [
          makeNode({ id: "n1", node_type: "transform", plugin: "p" }),
          makeNode({ id: "n2", node_type: "transform", plugin: "q" }),
        ],
        edges: [makeEdge({ id: "e1", from_node: "n1", to_node: "n2", edge_type: "on_error" })],
      }),
    });
    render(<GraphView />);
    // EDGE_LABEL_MAP maps on_error -> "error"
    expect(screen.getByText("error")).toBeInTheDocument();
  });

  it("shows minimap for >8 nodes", () => {
    const nodes = Array.from({ length: 9 }, (_, i) =>
      makeNode({ id: `n${i}`, node_type: "transform", plugin: "p" }),
    );
    useSessionStore.setState({
      compositionState: makeState({ nodes }),
    });
    render(<GraphView />);
    expect(screen.getByTestId("minimap")).toBeInTheDocument();
  });

  it("hides minimap for 6-node graphs that still fit the main viewport", () => {
    const nodes = Array.from({ length: 6 }, (_, i) =>
      makeNode({ id: `n${i}`, node_type: "transform", plugin: "p" }),
    );
    useSessionStore.setState({
      compositionState: makeState({ nodes }),
    });
    render(<GraphView />);
    expect(screen.queryByTestId("minimap")).not.toBeInTheDocument();
  });

  it("passes resolved theme and token-backed colours into React Flow controls", () => {
    document.documentElement.style.setProperty("--color-badge-source", "#4db89a");
    document.documentElement.style.setProperty("--color-badge-gate", "#c390f9");
    document.documentElement.style.setProperty("--color-badge-sink", "#e07040");
    document.documentElement.style.setProperty("--color-border-strong", "rgba(1, 2, 3, 0.4)");
    document.documentElement.style.setProperty("--color-text-muted", "#7a9a9a");

    const nodes = Array.from({ length: 6 }, (_, i) =>
      makeNode({ id: `n${i}`, node_type: "transform", plugin: "p" }),
    );
    useSessionStore.setState({
      compositionState: makeState({
        sources: {
          source: {
            plugin: "csv",
            options: {},
            on_success: "gate_in",
          },
        },
        nodes: [
          {
            id: "quality_gate",
            node_type: "gate" as const,
            plugin: null,
            input: "gate_in",
            on_success: null,
            on_error: null,
            options: {},
            condition: "row['score'] >= 0.8",
            routes: null,
          },
          ...nodes,
        ],
        outputs: [{ name: "results", plugin: "csv", options: {} }],
      }),
    });

    render(<GraphView />);

    expect(screen.getByTestId("react-flow")).toHaveAttribute("data-color-mode", "light");
    expect(screen.getByTestId("react-flow-background")).toHaveAttribute("data-color", "var(--color-canvas-grid)");
    expect(screen.getByTestId("react-flow-background")).toHaveAttribute("data-gap", "16");
    expect(screen.getByTestId("react-flow-background")).toHaveAttribute("data-size", "1");

    const minimap = screen.getByTestId("minimap");
    expect(minimap).toHaveAttribute("data-bg-color", "var(--color-surface)");
    expect(minimap).toHaveAttribute("data-source-color", "#4db89a");
    expect(minimap).toHaveAttribute("data-gate-color", "#c390f9");
    expect(minimap).toHaveAttribute("data-sink-color", "#e07040");
    expect(minimap).toHaveAttribute("data-unknown-color", "#7a9a9a");
    expect(minimap).toHaveAttribute("data-stroke-color", "rgba(1, 2, 3, 0.4)");
  });

  it("renders validation status markers with accessible names and non-colour glyphs", () => {
    useSessionStore.setState({
      compositionState: makeState({
        nodes: [
          makeNode({ id: "needs_fix", node_type: "transform", plugin: "p" }),
          makeNode({ id: "needs_review", node_type: "transform", plugin: "p" }),
        ],
      }),
    });
    useExecutionStore.setState({
      validationResult: {
        is_valid: false,
        checks: [],
        errors: [
          {
            component_id: "needs_fix",
            component_type: "transform",
            message: "Missing source plugin",
            suggestion: null,
          },
        ],
        warnings: [
          {
            component_id: "needs_review",
            component_type: "transform",
            message: "Review optional mapping",
            suggestion: null,
          },
        ],
      },
    } as never);

    render(<GraphView />);

    const errorMarker = screen.getByRole("img", {
      name: /validation: error/i,
    });
    const warningMarker = screen.getByRole("img", {
      name: /validation: warning/i,
    });

    // Non-colour-alone (WCAG 1.4.1): each dot carries a SHAPE, and the two
    // shapes differ. Stroked paths on a shared 12x12 viewBox replaced the old
    // letter `x` / `!` text glyphs, which mixed registers and left 1px of
    // clearance inside the 14px circle (elspeth-ac3fff7ef2).
    const errorMark = errorMarker.querySelector("svg");
    const warningMark = warningMarker.querySelector("svg");
    expect(errorMark).not.toBeNull();
    expect(warningMark).not.toBeNull();
    const errorPaths = [...errorMarker.querySelectorAll("path")].map((p) =>
      p.getAttribute("d"),
    );
    const warningPaths = [...warningMarker.querySelectorAll("path")].map((p) =>
      p.getAttribute("d"),
    );
    expect(errorPaths.length).toBeGreaterThan(0);
    expect(warningPaths.length).toBeGreaterThan(0);
    expect(errorPaths).not.toEqual(warningPaths);
  });

  // The mark is drawn, not typed, so the 12px type floor cannot pin it against
  // the 14px circle. Both marks must share one geometry: same box, same stroke
  // weight, same viewBox — an icon set, not two freelanced glyphs.
  it("draws every validation mark at one size, stroke weight and viewBox", () => {
    useSessionStore.setState({
      compositionState: makeState({
        nodes: [
          makeNode({ id: "needs_fix", node_type: "transform", plugin: "p" }),
          makeNode({ id: "needs_review", node_type: "transform", plugin: "p" }),
        ],
      }),
    });
    useExecutionStore.setState({
      validationResult: {
        is_valid: false,
        checks: [],
        errors: [
          {
            component_id: "needs_fix",
            component_type: "transform",
            message: "Missing source plugin",
            suggestion: null,
          },
        ],
        warnings: [
          {
            component_id: "needs_review",
            component_type: "transform",
            message: "Review optional mapping",
            suggestion: null,
          },
        ],
      },
    } as never);

    render(<GraphView />);

    const marks = [
      screen.getByRole("img", { name: /validation: error/i }),
      screen.getByRole("img", { name: /validation: warning/i }),
    ].map((marker) => marker.querySelector("svg")!);

    for (const mark of marks) {
      expect(mark).not.toBeNull();
      // 8px inside the 14px .graph-validation-dot circle = 3px of clearance on
      // every side, where a 12px text glyph left 1px.
      expect(mark.getAttribute("width")).toBe("8");
      expect(mark.getAttribute("height")).toBe("8");
      expect(mark.getAttribute("viewBox")).toBe("0 0 12 12");
      expect(mark.getAttribute("stroke-width")).toBe("2");
      expect(mark.getAttribute("stroke")).toBe("currentColor");
      // The dot's own aria-label is the accessible name; the drawing must not
      // add a second one.
      expect(mark.getAttribute("aria-hidden")).toBe("true");
    }
  });

  // The bridge from React Flow's --xy-* variables to the Elspeth tokens used
  // to be asserted here as DECLARATION TEXT: eleven toContain() calls over
  // inspector.css. Every one of them passed while the controls panel and the
  // minimap rendered completely unthemed for months, because the block was
  // scoped to `.react-flow` and both panels mount OUTSIDE it (elspeth-1adf90c933,
  // elspeth-525d335cbc). A stylesheet read as a string cannot see a scope bug:
  // the declarations were present and correct, and they reached nothing.
  // The replacement — reactFlowThemeScope.test.tsx, next to this file — runs
  // each declaring rule's selector list against the rendered ancestry with
  // Element.closest(), so it fails when the theme cannot arrive. What is kept
  // here is only the pairing that is genuinely a text fact: the two variables
  // whose VALUES are theme-specific literals rather than token references.
  it("bridges React Flow CSS variables to the Elspeth theme tokens", () => {
    const appCss = readFileSync("src/components/inspector/inspector.css", "utf8");

    // Which Elspeth token each React Flow variable maps to. These are value
    // facts, and they stay — it is the two SELECTOR assertions that used to
    // sit alongside them (`:root .react-flow.react-flow` and the light block's
    // selector) that were removed: they pinned a scope that reached nothing.
    expect(appCss).toContain("--xy-background-color-default: var(--color-bg);");
    expect(appCss).toContain("--xy-controls-button-background-color-default: var(--color-surface-elevated);");
    expect(appCss).toContain("--xy-controls-button-background-color-hover-default: var(--color-surface-raised);");
    expect(appCss).toContain("--xy-controls-button-color-default: var(--color-text);");
    expect(appCss).toContain("--xy-minimap-background-color-default: var(--color-surface);");
    expect(appCss).toContain("--xy-minimap-mask-stroke-color-default: var(--color-border-strong);");
    expect(appCss).toContain("--xy-edge-stroke-selected-default: var(--color-focus-ring);");
    expect(appCss).toMatch(
      /\[data-theme="light"\][\s\S]*--xy-minimap-mask-background-color-default:\s*rgba\(15, 45, 53, 0\.12\);/,
    );
    expect(appCss).toContain(".react-flow__controls-button:focus-visible");
    expect(appCss).toContain("outline: 2px solid var(--color-focus-ring);");
  });

  // Edge inference tests — verify connection point matching
  describe("edge inference via connection points", () => {
    it("infers source→transform edge when node.input matches source.on_success", () => {
      // This is the ELSPETH connection model: source.on_success is a connection point
      // name that must match node.input for data to flow.
      useSessionStore.setState({
        compositionState: makeState({
          sources: {
            source: {
              plugin: "text",
              options: {},
              on_success: "transform_in",  // Connection point name
            },
          },
          nodes: [
            makeNode({
              id: "my_transform",
              input: "transform_in",  // Matches source.on_success
              on_success: "results",
            }),
          ],
          outputs: [{ name: "results", plugin: "csv", options: {} }],
          edges: [],  // No explicit edges — should be inferred
        }),
      });
      render(<GraphView />);
      // Should infer edge from source to my_transform
      expect(screen.getByTestId("edge-inferred-conn-source-my_transform")).toBeInTheDocument();
    });

    it("infers transform→transform edge when inputs match on_success values", () => {
      useSessionStore.setState({
        compositionState: makeState({
          sources: {
            source: {
              plugin: "csv",
              options: {},
              on_success: "step1_in",
            },
          },
          nodes: [
            makeNode({
              id: "transform1",
              input: "step1_in",
              on_success: "step2_in",
            }),
            makeNode({
              id: "transform2",
              input: "step2_in",
              on_success: "results",
            }),
          ],
          outputs: [{ name: "results", plugin: "csv", options: {} }],
          edges: [],
        }),
      });
      render(<GraphView />);
      // Should infer: source → transform1 → transform2
      expect(screen.getByTestId("edge-inferred-conn-source-transform1")).toBeInTheDocument();
      expect(screen.getByTestId("edge-inferred-conn-transform1-transform2")).toBeInTheDocument();
    });

    it("infers error routing via connection points", () => {
      // Error handler receives rows via on_error connection point matching
      useSessionStore.setState({
        compositionState: makeState({
          sources: {
            source: {
              plugin: "csv",
              options: {},
              on_success: "process_in",
            },
          },
          nodes: [
            makeNode({
              id: "processor",
              input: "process_in",
              on_success: "results",
              on_error: "error_handler_in",  // Connection point for error routing
            }),
            makeNode({
              id: "error_handler",
              input: "error_handler_in",  // Receives errors from processor
              on_success: "errors",
            }),
          ],
          outputs: [
            { name: "results", plugin: "csv", options: {} },
            { name: "errors", plugin: "json", options: {} },
          ],
          edges: [],
        }),
      });
      render(<GraphView />);
      // Error edge should be inferred with error styling
      expect(screen.getByTestId("edge-inferred-conn-processor-error_handler")).toBeInTheDocument();
      // Label should be "error"
      expect(screen.getByText("error")).toBeInTheDocument();
    });

    it("infers gate routes via connection points", () => {
      // Gate routes to different nodes via connection point matching
      useSessionStore.setState({
        compositionState: makeState({
          sources: {
            source: {
              plugin: "csv",
              options: {},
              on_success: "gate_in",
            },
          },
          nodes: [
            {
              id: "quality_gate",
              node_type: "gate" as const,
              plugin: null,
              input: "gate_in",
              on_success: null,
              on_error: null,
              options: {},
              condition: "row['score'] >= 0.8",
              routes: { "true": "high_quality_in", "false": "low_quality_in" },
            },
            makeNode({
              id: "high_quality_handler",
              input: "high_quality_in",
              on_success: "good_output",
            }),
            makeNode({
              id: "low_quality_handler",
              input: "low_quality_in",
              on_success: "review_output",
            }),
          ],
          outputs: [
            { name: "good_output", plugin: "csv", options: {} },
            { name: "review_output", plugin: "csv", options: {} },
          ],
          edges: [],
        }),
      });
      render(<GraphView />);
      // Gate → handlers via route connection matching
      expect(screen.getByTestId("edge-inferred-conn-quality_gate-high_quality_handler")).toBeInTheDocument();
      expect(screen.getByTestId("edge-inferred-conn-quality_gate-low_quality_handler")).toBeInTheDocument();
      // Route labels should be present
      expect(screen.getByText("true")).toBeInTheDocument();
      expect(screen.getByText("false")).toBeInTheDocument();
    });

    it("merges inferred edges with partial explicit edges", () => {
      // When some edges are explicit and others need inference
      useSessionStore.setState({
        compositionState: makeState({
          sources: {
            source: {
              plugin: "csv",
              options: {},
              on_success: "step1_in",
            },
          },
          nodes: [
            makeNode({
              id: "transform1",
              input: "step1_in",
              on_success: "step2_in",
            }),
            makeNode({
              id: "transform2",
              input: "step2_in",
              on_success: "results",
            }),
          ],
          outputs: [{ name: "results", plugin: "csv", options: {} }],
          // Only one explicit edge — the other should be inferred
          edges: [makeEdge({ id: "e1", from_node: "source", to_node: "transform1" })],
        }),
      });
      render(<GraphView />);
      // Explicit edge exists
      expect(screen.getByTestId("edge-e-source-transform1-0")).toBeInTheDocument();
      // Second edge should be inferred (not blocked by explicit edge existing)
      expect(screen.getByTestId("edge-inferred-conn-transform1-transform2")).toBeInTheDocument();
    });

    it("infers transform→sink edges via direct sink references", () => {
      // When on_success points directly to a sink name (not a connection point)
      useSessionStore.setState({
        compositionState: makeState({
          sources: {
            source: {
              plugin: "csv",
              options: {},
              on_success: "process_in",
            },
          },
          nodes: [
            makeNode({
              id: "processor",
              input: "process_in",
              on_success: "results",  // Direct sink reference
              on_error: "errors",     // Direct sink reference
            }),
          ],
          outputs: [
            { name: "results", plugin: "csv", options: {} },
            { name: "errors", plugin: "json", options: {} },
          ],
          edges: [],
        }),
      });
      render(<GraphView />);
      // Sink edges should be inferred
      expect(screen.getByTestId("edge-inferred-sink-processor-results")).toBeInTheDocument();
      expect(screen.getByTestId("edge-inferred-sink-processor-errors-error")).toBeInTheDocument();
    });
  });

  // Honest structural queue fan-in (elspeth-a5b86149d4 / elspeth-6421ffa028).
  // Many producers publish one connection name; a declared queue node consumes
  // it and one ordinary node consumes the queue. The graph must draw every
  // producer -> queue edge and exactly one queue -> consumer edge — never a
  // dishonest producer -> consumer bypass, and never a queue self-loop.
  describe("queue fan-in", () => {
    function queueState(order: "orders-first" | "refunds-first" = "orders-first"): CompositionState {
      const orders: [string, unknown] = [
        "orders",
        { plugin: "csv", options: {}, on_success: "inbound" },
      ];
      const refunds: [string, unknown] = [
        "refunds",
        { plugin: "csv", options: {}, on_success: "inbound" },
      ];
      const entries = order === "orders-first" ? [orders, refunds] : [refunds, orders];
      return makeState({
        sources: Object.fromEntries(entries) as never,
        nodes: [
          {
            id: "inbound",
            node_type: "queue",
            plugin: null,
            input: "inbound",
            on_success: null,
            on_error: null,
            options: {},
          },
          makeNode({
            id: "normalize",
            node_type: "transform",
            plugin: "passthrough",
            input: "inbound",
            on_success: "combined",
          }),
        ],
        outputs: [{ name: "combined", plugin: "json", options: {} }],
      });
    }

    function renderedEdgeIds(): string[] {
      return Array.from(document.querySelectorAll('[data-testid^="edge-"]'))
        .map((el) => el.getAttribute("data-testid") ?? "")
        .sort();
    }

    it("draws every producer->queue edge and one queue->consumer edge, no bypass, no self-loop", () => {
      useSessionStore.setState({ compositionState: queueState() });
      render(<GraphView />);
      const ids = renderedEdgeIds();

      // Both sources draw distinct edges to the queue node.
      expect(ids).toContain("edge-inferred-queue-in-source:orders-inbound");
      expect(ids).toContain("edge-inferred-queue-in-source:refunds-inbound");
      // The queue draws exactly one edge to the downstream consumer of `inbound`.
      expect(ids).toContain("edge-inferred-queue-out-inbound-normalize");
      // NO dishonest producer -> consumer bypass edge (the current-code defect).
      expect(ids.some((id) => id.includes("source:orders-normalize"))).toBe(false);
      expect(ids.some((id) => id.includes("source:refunds-normalize"))).toBe(false);
      // The only edge terminating at the consumer comes from the queue.
      expect(ids.filter((id) => id.endsWith("-normalize"))).toEqual([
        "edge-inferred-queue-out-inbound-normalize",
      ]);
      // No queue self-loop (the queue's implicit output uses its own id).
      expect(ids.some((id) => id.includes("inbound-inbound"))).toBe(false);
    });

    it("produces the same edge set when source insertion order is reversed", () => {
      useSessionStore.setState({ compositionState: queueState("orders-first") });
      const { unmount } = render(<GraphView />);
      const forward = renderedEdgeIds();
      unmount();

      useSessionStore.setState({ compositionState: queueState("refunds-first") });
      render(<GraphView />);
      const reversed = renderedEdgeIds();

      expect(reversed).toEqual(forward);
    });

    it("exposes the queue node in the keyboard list and opens its config panel with the queue badge", async () => {
      useSessionStore.setState({
        selectedNodeId: null,
        selectNode: (nodeId: string | null) =>
          useSessionStore.setState({ selectedNodeId: nodeId } as never),
        compositionState: queueState(),
      } as never);
      render(<GraphView />);

      const list = screen.getByRole("list", { name: /pipeline components/i });
      await userEvent.click(
        within(list).getByRole("button", { name: /queue: inbound/i }),
      );
      const panel = screen.getByRole("complementary", {
        name: /inbound configuration/i,
      });
      expect(panel).toHaveFocus();
      expect(within(panel).getByText("queue")).toHaveClass(
        "type-badge",
        "type-badge-queue",
      );
    });

    it("colours the queue node in the minimap by its queue token", () => {
      document.documentElement.style.setProperty("--color-badge-queue", "#ff91c8");
      const padding = Array.from({ length: 8 }, (_, i) =>
        makeNode({ id: `pad${i}`, node_type: "transform", plugin: "p" }),
      );
      useSessionStore.setState({
        compositionState: makeState({
          sources: {
            orders: { plugin: "csv", options: {}, on_success: "inbound" },
          } as never,
          nodes: [
            {
              id: "inbound",
              node_type: "queue",
              plugin: null,
              input: "inbound",
              on_success: null,
              on_error: null,
              options: {},
            },
            ...padding,
          ],
          outputs: [{ name: "combined", plugin: "json", options: {} }],
        }),
      });
      render(<GraphView />);
      expect(screen.getByTestId("minimap")).toHaveAttribute(
        "data-queue-color",
        "#ff91c8",
      );
    });
  });

  describe("row_union correlated branch fan-in", () => {
    function rowUnionState(): CompositionState {
      return makeState({
        sources: {
          experiments: {
            plugin: "csv",
            options: {},
            on_success: "routed",
          },
        },
        nodes: [
          makeNode({
            id: "experiment_gate",
            node_type: "gate",
            plugin: null,
            input: "routed",
            on_success: null,
            routes: {
              split: "fork",
            },
            fork_to: ["control_raw", "treatment_raw"],
          }),
          makeNode({
            id: "control_score",
            input: "control_raw",
            on_success: "control_done",
          }),
          makeNode({
            id: "treatment_score",
            input: "treatment_raw",
            on_success: "treatment_done",
          }),
          {
            id: "variant_union",
            node_type: "row_union",
            plugin: null,
            // Backend compatibility placeholder only. It must not invent an
            // extra scalar input edge in the graph.
            input: "control_done",
            on_success: "experiment_rows",
            on_error: null,
            options: {},
            branches: {
              control: "control_done",
              treatment: "treatment_done",
            },
            timeout_seconds: 12.5,
          },
          makeNode({
            id: "compare",
            node_type: "aggregation",
            plugin: "batch_experiment_compare",
            input: "experiment_rows",
            on_success: "results",
          }),
        ],
        outputs: [{ name: "results", plugin: "json", options: {} }],
        edges: [],
      });
    }

    function renderedEdgeIds(): string[] {
      return Array.from(document.querySelectorAll('[data-testid^="edge-"]'))
        .map((el) => el.getAttribute("data-testid") ?? "")
        .sort();
    }

    it("draws every branch producer into row_union by alias and one success edge without placeholder bypasses", () => {
      useSessionStore.setState({ compositionState: rowUnionState() });
      render(<GraphView />);

      const ids = renderedEdgeIds();
      expect(ids).toContain(
        "edge-inferred-fan-in-control_score-variant_union-control",
      );
      expect(ids).toContain(
        "edge-inferred-fan-in-treatment_score-variant_union-treatment",
      );
      expect(
        screen.getByTestId(
          "edge-inferred-fan-in-control_score-variant_union-control",
        ),
      ).toHaveTextContent("control");
      expect(
        screen.getByTestId(
          "edge-inferred-fan-in-treatment_score-variant_union-treatment",
        ),
      ).toHaveTextContent("treatment");
      expect(
        screen.getByTestId(
          "edge-inferred-conn-experiment_gate-control_score",
        ),
      ).toHaveTextContent("control_raw");
      expect(
        screen.getByTestId(
          "edge-inferred-conn-experiment_gate-treatment_score",
        ),
      ).toHaveTextContent("treatment_raw");
      expect(ids).toContain(
        "edge-inferred-row-union-out-variant_union-compare",
      );

      // The scalar placeholder input is the first branch's connection for
      // backend compatibility. Rendering it through ordinary input inference
      // would create a duplicate, unlabelled edge.
      expect(ids).not.toContain(
        "edge-inferred-conn-control_score-variant_union",
      );
      // Every route into compare comes from the union itself: no branch
      // producer bypass and no union self-loop.
      expect(ids.filter((id) => id.endsWith("-compare"))).toEqual([
        "edge-inferred-row-union-out-variant_union-compare",
      ]);
      expect(
        ids.some((id) => id.includes("variant_union-variant_union")),
      ).toBe(false);
    });

    it("draws identity fork branches directly from their owning gate to row union", () => {
      const state = rowUnionState();
      state.nodes = state.nodes.filter(
        (node) => !["control_score", "treatment_score"].includes(node.id),
      );
      const union = state.nodes.find((node) => node.id === "variant_union");
      expect(union).toBeDefined();
      union!.input = "control_raw";
      union!.branches = {
        control: "control_raw",
        treatment: "treatment_raw",
      };
      useSessionStore.setState({ compositionState: state });

      render(<GraphView />);

      const identityEdges = Array.from(
        document.querySelectorAll(
          '[data-edge-source="experiment_gate"][data-edge-target="variant_union"]',
        ),
      );
      expect(identityEdges).toHaveLength(2);
      expect(identityEdges.map((edge) => edge.textContent).sort()).toEqual([
        "control",
        "treatment",
      ]);
    });

    it("routes parallel identity-fork aliases through distinct visible edge geometry", () => {
      const state = rowUnionState();
      state.nodes = state.nodes.filter(
        (node) => !["control_score", "treatment_score"].includes(node.id),
      );
      const union = state.nodes.find((node) => node.id === "variant_union");
      expect(union).toBeDefined();
      union!.input = "control_raw";
      union!.branches = {
        control: "control_raw",
        treatment: "treatment_raw",
      };
      useSessionStore.setState({ compositionState: state });

      const { container } = render(<GraphView />);

      const paths = Array.from(
        container.querySelectorAll(
          '[data-edge-source="experiment_gate"][data-edge-target="variant_union"] path[data-edge-path-id]',
        ),
      );
      expect(paths).toHaveLength(2);
      expect(paths[0]?.getAttribute("d")).not.toBe(paths[1]?.getAttribute("d"));

      const visualEdges = Array.from(
        container.querySelectorAll(
          '[data-edge-source="experiment_gate"][data-edge-target="variant_union"]',
        ),
      );
      const sourceHandleIds = visualEdges.map(
        (edge) => edge.getAttribute("data-source-handle"),
      );
      const targetHandleIds = visualEdges.map(
        (edge) => edge.getAttribute("data-target-handle"),
      );
      expect(new Set(sourceHandleIds).size).toBe(2);
      expect(new Set(targetHandleIds).size).toBe(2);
      const sourceHandleOffsets: string[] = [];
      for (const handleId of sourceHandleIds) {
        const handle = screen.getByTestId("node-experiment_gate").querySelector(
          `[data-handle-type="source"][data-handle-id="${handleId}"]`,
        );
        expect(handle).not.toBeNull();
        sourceHandleOffsets.push((handle as HTMLElement).style.left);
      }
      const targetHandleOffsets: string[] = [];
      for (const handleId of targetHandleIds) {
        const handle = screen.getByTestId("node-variant_union").querySelector(
          `[data-handle-type="target"][data-handle-id="${handleId}"]`,
        );
        expect(handle).not.toBeNull();
        targetHandleOffsets.push((handle as HTMLElement).style.left);
      }
      expect(new Set(sourceHandleOffsets).size).toBe(2);
      expect(new Set(targetHandleOffsets).size).toBe(2);
      for (const offset of [...sourceHandleOffsets, ...targetHandleOffsets]) {
        expect(Number.parseFloat(offset)).toBeGreaterThan(0);
        expect(Number.parseFloat(offset)).toBeLessThan(100);
      }

      const pathEndpoints = paths.map((path) => {
        const coordinates = path.getAttribute("d")?.match(
          /M ([\d.-]+) ([\d.-]+).* ([\d.-]+) ([\d.-]+)$/,
        );
        expect(coordinates).not.toBeNull();
        return coordinates?.slice(1);
      });
      expect(new Set(pathEndpoints.map((point) => point?.join(","))).size).toBe(2);
      expect(paths.every((path) => path.getAttribute("marker-end"))).toBe(true);

      const connectionList = screen.getByRole("list", {
        name: "Pipeline branch connections",
      });
      expect(within(connectionList).getByText(
        "experiment_gate to variant_union: control (success)",
      )).toBeInTheDocument();
      expect(within(connectionList).getByText(
        "experiment_gate to variant_union: treatment (success)",
      )).toBeInTheDocument();
    });

    it.each([
      ["a partial branch set", ["control"]],
      ["one null-labelled identity edge", [null]],
      ["two null-labelled identity edges", [null, null]],
      ["a labelled edge after a null-labelled edge", [null, "control"]],
    ])(
      "preserves every identity-fork alias with %s",
      (_caseName, explicitLabels) => {
        const state = rowUnionState();
        state.nodes = state.nodes.filter(
          (node) => !["control_score", "treatment_score"].includes(node.id),
        );
        const union = state.nodes.find((node) => node.id === "variant_union");
        expect(union).toBeDefined();
        union!.input = "control_raw";
        union!.branches = {
          control: "control_raw",
          treatment: "treatment_raw",
        };
        state.edges = explicitLabels.map((label, index) =>
          makeEdge({
            id: `partial-identity-fork-${index}`,
            from_node: "experiment_gate",
            to_node: "variant_union",
            edge_type: "fork",
            label,
          }),
        );
        useSessionStore.setState({ compositionState: state });

        render(<GraphView />);

        const identityEdges = Array.from(
          document.querySelectorAll(
            '[data-edge-source="experiment_gate"][data-edge-target="variant_union"]',
          ),
        );
        expect(identityEdges).toHaveLength(2);
        expect(identityEdges.map((edge) => edge.textContent).sort()).toEqual([
          "control",
          "treatment",
        ]);
      },
    );

    it("uses a queue as the authoritative row union producer without an upstream bypass", () => {
      const state = rowUnionState();
      state.nodes = [
        makeNode({
          id: "experiment_gate",
          node_type: "gate",
          plugin: null,
          input: "routed",
          on_success: null,
          routes: { split: "fork" },
          fork_to: ["control_queue", "treatment_raw"],
        }),
        makeNode({
          id: "control_queue",
          node_type: "queue",
          plugin: null,
          input: "control_queue",
          on_success: null,
        }),
        makeNode({
          id: "treatment_score",
          input: "treatment_raw",
          on_success: "treatment_done",
        }),
        {
          id: "variant_union",
          node_type: "row_union",
          plugin: null,
          input: "control_queue",
          on_success: "experiment_rows",
          on_error: null,
          options: {},
          branches: {
            control: "control_queue",
            treatment: "treatment_done",
          },
          timeout_seconds: null,
        },
        makeNode({
          id: "compare",
          node_type: "aggregation",
          plugin: "batch_experiment_compare",
          input: "experiment_rows",
          on_success: "results",
        }),
      ];
      useSessionStore.setState({ compositionState: state });

      render(<GraphView />);

      expect(
        document.querySelector(
          '[data-edge-source="experiment_gate"][data-edge-target="control_queue"]',
        ),
      ).not.toBeNull();
      expect(
        document.querySelector(
          '[data-edge-source="control_queue"][data-edge-target="variant_union"]',
        ),
      ).toHaveTextContent("control");
      expect(
        document.querySelector(
          '[data-edge-source="experiment_gate"][data-edge-target="variant_union"]',
        ),
      ).toBeNull();
    });

    it("gives adversarial hyphenated ids and aliases collision-proof inferred edge ids", () => {
      const state = makeState({
        nodes: [
          makeNode({
            id: "a-b",
            input: "unused-left",
            on_success: "left-ready",
          }),
          makeNode({
            id: "a",
            input: "unused-right",
            on_success: "right-ready",
          }),
          makeNode({
            id: "c",
            node_type: "row_union",
            plugin: null,
            input: "left-ready",
            on_success: null,
            branches: { "d-e": "left-ready" },
          }),
          makeNode({
            id: "b-c",
            node_type: "row_union",
            plugin: null,
            input: "right-ready",
            on_success: null,
            branches: { "d-e": "right-ready" },
          }),
        ],
      });
      useSessionStore.setState({ compositionState: state });

      render(<GraphView />);

      const left = document.querySelector(
        '[data-edge-source="a-b"][data-edge-target="c"]',
      );
      const right = document.querySelector(
        '[data-edge-source="a"][data-edge-target="b-c"]',
      );
      expect(left).not.toBeNull();
      expect(right).not.toBeNull();
      expect(left?.getAttribute("data-testid")).not.toBe(
        right?.getAttribute("data-testid"),
      );
    });

    it("exposes row union branch ownership in the accessible graph alternative and summary", () => {
      const state = rowUnionState();
      state.nodes = state.nodes.filter(
        (node) => !["control_score", "treatment_score"].includes(node.id),
      );
      const union = state.nodes.find((node) => node.id === "variant_union");
      expect(union).toBeDefined();
      union!.input = "control_raw";
      union!.branches = {
        control: "control_raw",
        treatment: "treatment_raw",
      };
      useSessionStore.setState({ compositionState: state });

      render(<GraphView />);

      expect(
        screen.getByRole("img", { name: /1 row union/i }),
      ).toBeInTheDocument();
      const branchConnections = screen.getByRole("list", {
        name: /pipeline branch connections/i,
      });
      expect(
        within(branchConnections).getByText(
          "experiment_gate to variant_union: control (success)",
        ),
      ).toBeInTheDocument();
      expect(
        within(branchConnections).getByText(
          "experiment_gate to variant_union: treatment (success)",
        ),
      ).toBeInTheDocument();
      expect(
        within(screen.getByTestId("node-variant_union")).getByText("row union"),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", {
          name: /row union: variant_union/i,
        }),
      ).toBeInTheDocument();
    });

    it("preserves canonical explicit row_union branch labels without inferred duplicates", () => {
      const state = rowUnionState();
      state.edges = [
        makeEdge({
          id: "control-to-union",
          from_node: "control_score",
          to_node: "variant_union",
          edge_type: "on_success",
          label: "control",
        }),
        makeEdge({
          id: "treatment-to-union",
          from_node: "treatment_score",
          to_node: "variant_union",
          edge_type: "on_success",
          label: "treatment",
        }),
      ];
      useSessionStore.setState({ compositionState: state });

      render(<GraphView />);

      expect(
        screen.getByTestId("edge-e-control_score-variant_union-0"),
      ).toHaveTextContent("control");
      expect(
        screen.getByTestId("edge-e-treatment_score-variant_union-1"),
      ).toHaveTextContent("treatment");

      const ids = renderedEdgeIds();
      expect(
        ids.filter((id) => id.includes("-control_score-variant_union")),
      ).toEqual(["edge-e-control_score-variant_union-0"]);
      expect(
        ids.filter((id) => id.includes("-treatment_score-variant_union")),
      ).toEqual(["edge-e-treatment_score-variant_union-1"]);
      expect(ids).not.toContain(
        "edge-inferred-conn-control_score-variant_union",
      );
      expect(ids.filter((id) => id.endsWith("-compare"))).toEqual([
        "edge-inferred-row-union-out-variant_union-compare",
      ]);
      expect(
        ids.some((id) => id.includes("variant_union-variant_union")),
      ).toBe(false);
    });

    it("keeps only the authoritative row union successor after its output is repointed", () => {
      // Reachable `with_node` state: the union now publishes a new connection,
      // the old consumer has been rewired, but the materialized explicit edge
      // still names the old union → consumer topology.
      const state = rowUnionState();
      const union = state.nodes.find((node) => node.id === "variant_union");
      const oldConsumer = state.nodes.find((node) => node.id === "compare");
      expect(union).toBeDefined();
      expect(oldConsumer).toBeDefined();
      union!.on_success = "repointed_rows";
      oldConsumer!.input = "control_done";
      state.nodes.push(
        makeNode({
          id: "new_compare",
          node_type: "aggregation",
          plugin: "batch_experiment_compare",
          input: "repointed_rows",
          on_success: "results",
        }),
      );
      state.edges = [
        makeEdge({
          id: "stale-union-successor",
          from_node: "variant_union",
          to_node: "compare",
          edge_type: "on_success",
          label: "success",
        }),
        makeEdge({
          id: "unrelated-explicit-edge",
          from_node: "control_score",
          to_node: "compare",
          edge_type: "on_success",
          label: "success",
        }),
      ];
      useSessionStore.setState({ compositionState: state });

      const { container } = render(<GraphView />);

      const unionOutbound = Array.from(
        container.querySelectorAll('[data-edge-source="variant_union"]'),
      );
      expect(unionOutbound).toHaveLength(1);
      expect(unionOutbound[0]).toHaveAttribute(
        "data-edge-target",
        "new_compare",
      );
      expect(unionOutbound[0]).toHaveTextContent("success");
      expect(unionOutbound[0]).toHaveAttribute(
        "data-testid",
        "edge-inferred-row-union-out-variant_union-new_compare",
      );
      expect(
        screen.getByTestId("edge-e-control_score-compare-1"),
      ).toHaveTextContent("success");

      const connections = screen.getByRole("list", {
        name: "Pipeline branch connections",
      });
      expect(
        within(connections).queryByText(
          "variant_union to compare: success (success)",
        ),
      ).not.toBeInTheDocument();
      expect(
        within(connections).getByText(
          "variant_union to new_compare: success (success)",
        ),
      ).toBeInTheDocument();
    });

    it("drops a stale error lane that shares the authoritative row union success endpoint", () => {
      const state = rowUnionState();
      state.edges = [
        makeEdge({
          id: "live-union-success",
          from_node: "variant_union",
          to_node: "compare",
          edge_type: "on_success",
          label: "success",
        }),
        makeEdge({
          id: "stale-union-error",
          from_node: "variant_union",
          to_node: "compare",
          edge_type: "on_error",
          label: "error",
        }),
      ];
      useSessionStore.setState({ compositionState: state });

      const { container } = render(<GraphView />);

      const unionOutbound = Array.from(
        container.querySelectorAll(
          '[data-edge-source="variant_union"][data-edge-target="compare"]',
        ),
      );
      expect(unionOutbound).toHaveLength(1);
      expect(unionOutbound[0]).toHaveTextContent("success");

      const connections = screen.getByRole("list", {
        name: "Pipeline branch connections",
      });
      expect(
        within(connections).getByText(
          "variant_union to compare: success (success)",
        ),
      ).toBeInTheDocument();
      expect(
        within(connections).queryByText(
          "variant_union to compare: error (error)",
        ),
      ).not.toBeInTheDocument();
    });

    it("replaces a stale row union outbound label with authoritative success semantics", () => {
      const state = rowUnionState();
      state.edges = [
        makeEdge({
          id: "stale-union-label",
          from_node: "variant_union",
          to_node: "compare",
          edge_type: "on_success",
          label: "legacy_success",
        }),
      ];
      useSessionStore.setState({ compositionState: state });

      render(<GraphView />);

      expect(
        screen.getByTestId("edge-e-variant_union-compare-0"),
      ).toHaveTextContent("success");
      const connections = screen.getByRole("list", {
        name: "Pipeline branch connections",
      });
      expect(
        within(connections).getByText(
          "variant_union to compare: success (success)",
        ),
      ).toBeInTheDocument();
      expect(
        within(connections).queryByText(/legacy_success/),
      ).not.toBeInTheDocument();
    });

    it.each([
      {
        caseName: "an explicit queue edge",
        target: "union_queue",
        configure: (state: CompositionState) => {
          const union = state.nodes.find(
            (node) => node.id === "variant_union",
          );
          expect(union).toBeDefined();
          union!.on_success = "union_queue";
          state.nodes.push(
            makeNode({
              id: "union_queue",
              node_type: "queue",
              plugin: null,
              input: "union_queue",
              on_success: null,
              on_error: null,
            }),
          );
          state.edges = [
            makeEdge({
              id: "union-to-queue",
              from_node: "variant_union",
              to_node: "union_queue",
              edge_type: "on_success",
              label: "success",
            }),
          ];
        },
        expectedConnections: [
          "variant_union to union_queue: success (success)",
        ],
      },
      {
        caseName: "an explicit direct-sink edge",
        target: "results",
        configure: (state: CompositionState) => {
          const union = state.nodes.find(
            (node) => node.id === "variant_union",
          );
          expect(union).toBeDefined();
          union!.on_success = "results";
          state.edges = [
            makeEdge({
              id: "union-to-sink",
              from_node: "variant_union",
              to_node: "results",
              edge_type: "on_success",
              label: "success",
            }),
          ];
        },
        expectedConnections: [
          "variant_union to results: success (success)",
        ],
      },
      {
        caseName: "parallel explicit success and error lanes",
        target: "compare",
        configure: (state: CompositionState) => {
          const union = state.nodes.find(
            (node) => node.id === "variant_union",
          );
          const consumer = state.nodes.find((node) => node.id === "compare");
          expect(union).toBeDefined();
          expect(consumer).toBeDefined();
          union!.on_success = "shared_rows";
          union!.on_error = "shared_rows";
          consumer!.input = "shared_rows";
          state.edges = [
            makeEdge({
              id: "union-success-lane",
              from_node: "variant_union",
              to_node: "compare",
              edge_type: "on_success",
              label: "success",
            }),
            makeEdge({
              id: "union-error-lane",
              from_node: "variant_union",
              to_node: "compare",
              edge_type: "on_error",
              label: "error",
            }),
          ];
        },
        expectedConnections: [
          "variant_union to compare: success (success)",
          "variant_union to compare: error (error)",
        ],
      },
    ])(
      "preserves live row union outbound topology for $caseName",
      ({ target, configure, expectedConnections }) => {
        const state = rowUnionState();
        configure(state);
        useSessionStore.setState({ compositionState: state });

        render(<GraphView />);

        const connections = screen.getByRole("list", {
          name: "Pipeline branch connections",
        });
        expect(
          within(connections).getAllByText(
            new RegExp(`^variant_union to ${target}:`),
          ),
        ).toHaveLength(expectedConnections.length);
        for (const expectedConnection of expectedConnections) {
          expect(
            within(connections).getByText(expectedConnection),
          ).toBeInTheDocument();
        }
      },
    );

    it.each([
      {
        authoritativeRoute: "on_success",
        explicitRoute: "on_error",
        expectedAccessibleText:
          "control_score to variant_union: control (success)",
      },
      {
        authoritativeRoute: "on_error",
        explicitRoute: "on_success",
        expectedAccessibleText:
          "control_score to variant_union: control (error)",
      },
    ] as const)(
      "announces a claimed row union alias as its authoritative $authoritativeRoute route",
      ({
        authoritativeRoute,
        explicitRoute,
        expectedAccessibleText,
      }) => {
        const state = rowUnionState();
        const producer = state.nodes.find(
          (node) => node.id === "control_score",
        );
        expect(producer).toBeDefined();
        producer!.on_success =
          authoritativeRoute === "on_success" ? "control_done" : "unused";
        producer!.on_error =
          authoritativeRoute === "on_error" ? "control_done" : null;
        state.edges = [
          makeEdge({
            id: "stale-route-type",
            from_node: "control_score",
            to_node: "variant_union",
            edge_type: explicitRoute,
            label: "control",
          }),
        ];
        useSessionStore.setState({ compositionState: state });

        const { container } = render(<GraphView />);

        const claimedEdges = container.querySelectorAll(
          '[data-edge-source="control_score"][data-edge-target="variant_union"]',
        );
        expect(claimedEdges).toHaveLength(1);
        expect(claimedEdges[0]).toHaveAttribute(
          "data-testid",
          "edge-e-control_score-variant_union-0",
        );
        expect(claimedEdges[0]).toHaveTextContent("control");

        const connections = screen.getByRole("list", {
          name: "Pipeline branch connections",
        });
        expect(
          within(connections).getByText(expectedAccessibleText),
        ).toBeInTheDocument();
      },
    );

    it("drops a stale-label explicit edge into an alias-mapped row union", () => {
      // Reachable state: `with_node` (upsert_node) replaces a row_union in
      // place and never reconciles `edges` (contrast `without_node`, which
      // prunes edges touching the removed node), and validation only checks
      // that edge endpoints resolve — never that a label names a live branch
      // alias. So renaming an alias leaves a validation-VALID composition
      // carrying an edge labelled with the OLD alias.
      const state = rowUnionState();
      state.edges = [
        makeEdge({
          id: "legacy-control-to-union",
          from_node: "control_score",
          to_node: "variant_union",
          edge_type: "on_success",
          label: "legacy_control",
        }),
      ];
      useSessionStore.setState({ compositionState: state });

      render(<GraphView />);

      const ids = renderedEdgeIds();
      expect(
        ids.filter((id) => id.includes("-control_score-variant_union")),
      ).toEqual([
        "edge-inferred-fan-in-control_score-variant_union-control",
      ]);
      expect(
        Array.from(document.querySelectorAll('[data-testid^="edge-"]')).some(
          (edge) => edge.textContent?.includes("legacy_control"),
        ),
      ).toBe(false);
    });

    it("drops a stale-source explicit edge into an alias-mapped row union", () => {
      // Same reachable path, other shape: the branch was repointed at a new
      // producer, so the surviving edge names a source that no longer feeds
      // any branch connection. Its label matches a live alias, so a
      // label-only check would wave it through.
      const state = rowUnionState();
      state.edges = [
        makeEdge({
          id: "stale-gate-to-union",
          from_node: "experiment_gate",
          to_node: "variant_union",
          edge_type: "on_success",
          label: "control",
        }),
      ];
      useSessionStore.setState({ compositionState: state });

      const { container } = render(<GraphView />);

      expect(
        container.querySelectorAll(
          '[data-edge-source="experiment_gate"][data-edge-target="variant_union"]',
        ),
      ).toHaveLength(0);
      expect(renderedEdgeIds()).toContain(
        "edge-inferred-fan-in-control_score-variant_union-control",
      );
    });

    it("drops an explicit edge whose alias names a connection no node publishes", () => {
      // Deliberate, not an oversight: the alias is live but its connection has
      // no producer, so the authoritative mapping cannot draw the lane and
      // nothing replaces the dropped edge. An edge asserting a route the
      // branches mapping cannot produce is the same phantom this guard exists
      // to remove — and widening the guard to "alias with a resolvable
      // producer" would resurrect stale edges every time a producer is
      // momentarily unwired mid-authoring.
      const state = rowUnionState();
      const union = state.nodes.find((node) => node.id === "variant_union");
      expect(union).toBeDefined();
      union!.branches = {
        control: "control_done",
        treatment: "unpublished_connection",
      };
      state.edges = [
        makeEdge({
          id: "treatment-to-union",
          from_node: "treatment_score",
          to_node: "variant_union",
          edge_type: "on_success",
          label: "treatment",
        }),
      ];
      useSessionStore.setState({ compositionState: state });

      render(<GraphView />);

      const ids = renderedEdgeIds();
      expect(
        ids.filter((id) => id.includes("-treatment_score-variant_union")),
      ).toEqual([]);
      expect(ids).toContain(
        "edge-inferred-fan-in-control_score-variant_union-control",
      );
    });

    it("keeps explicit edges into a row union with no alias mapping", () => {
      // Without a branches mapping there is no authoritative inbound wiring to
      // be the single source of truth, so the explicit edge is all the
      // operator has — dropping it would blank the union's inbound routes.
      const state = rowUnionState();
      const union = state.nodes.find((node) => node.id === "variant_union");
      expect(union).toBeDefined();
      union!.branches = null;
      state.edges = [
        makeEdge({
          id: "control-to-union",
          from_node: "control_score",
          to_node: "variant_union",
          edge_type: "on_success",
          label: "control",
        }),
      ];
      useSessionStore.setState({ compositionState: state });

      render(<GraphView />);

      expect(
        screen.getByTestId("edge-e-control_score-variant_union-0"),
      ).toHaveTextContent("control");
    });

    it("preserves every alias when two branches name the same producer connection", () => {
      const state = rowUnionState();
      const union = state.nodes.find((node) => node.id === "variant_union");
      expect(union).toBeDefined();
      union!.branches = {
        control: "control_done",
        treatment: "control_done",
      };
      useSessionStore.setState({ compositionState: state });

      render(<GraphView />);

      expect(
        screen.getByTestId(
          "edge-inferred-fan-in-control_score-variant_union-control",
        ),
      ).toHaveTextContent("control");
      expect(
        screen.getByTestId(
          "edge-inferred-fan-in-control_score-variant_union-treatment",
        ),
      ).toHaveTextContent("treatment");
    });

    it("exposes a distinct row union badge in the keyboard inspector", async () => {
      useSessionStore.setState({
        selectedNodeId: null,
        selectNode: (nodeId: string | null) =>
          useSessionStore.setState({ selectedNodeId: nodeId } as never),
        compositionState: rowUnionState(),
      } as never);
      render(<GraphView />);

      const list = screen.getByRole("list", {
        name: /pipeline components/i,
      });
      await userEvent.click(
        within(list).getByRole("button", {
          name: /row union: variant_union/i,
        }),
      );

      const panel = screen.getByRole("complementary", {
        name: /variant_union configuration/i,
      });
      expect(within(panel).getByText("row union")).toHaveClass(
        "type-badge",
        "type-badge-row_union",
      );
      expect(within(panel).getByText("12.5")).toBeInTheDocument();
    });

    it("colours row_union in the minimap using its dedicated kebab-case token", () => {
      document.documentElement.style.setProperty(
        "--color-badge-row-union",
        "#aeb8ff",
      );
      const state = rowUnionState();
      state.nodes.push(
        makeNode({ id: "padding-1", input: "unused-1" }),
        makeNode({ id: "padding-2", input: "unused-2" }),
      );
      useSessionStore.setState({ compositionState: state });

      render(<GraphView />);

      expect(screen.getByTestId("minimap")).toHaveAttribute(
        "data-row-union-color",
        "#aeb8ff",
      );
    });
  });

  // Regression: bug elspeth-625e85c59b.
  //
  // `coalesce` is the fan-in kind the composer actually authors — across 666
  // saved composition states the corpus holds 38 coalesces and zero row
  // unions. Both kinds declare their inbound topology the same way, in
  // `branches`, with `input` carrying only the backend-compatible first-branch
  // placeholder. Phase 1 originally enumerated `branches` for row_union alone,
  // so a coalesce fell through to ordinary `input` inference: exactly ONE
  // inbound edge appeared — from whichever producer happened to own the
  // placeholder connection — and every other branch producer was drawn nowhere
  // at all. The operator saw a merge node with a missing arm and no indication
  // that the arm existed.
  //
  // These fixtures are the two shapes the corpus actually contains, reproduced
  // verbatim.
  describe("coalesce correlated branch fan-in (elspeth-625e85c59b)", () => {
    function coalesceState(): CompositionState {
      return makeState({
        sources: {
          colours: {
            plugin: "csv",
            options: {},
            on_success: "colours_raw",
          },
        },
        nodes: [
          makeNode({
            id: "fan_out",
            node_type: "gate",
            plugin: null,
            input: "colours_raw",
            on_success: null,
            routes: { true: "fork", false: "fork" },
            fork_to: ["branch_a", "branch_b"],
          }),
          makeNode({
            id: "recommend_pairing",
            input: "branch_a",
            on_success: "pairing_done",
          }),
          makeNode({
            id: "get_hex",
            input: "branch_b",
            on_success: "hex_done",
          }),
          {
            id: "merge_branches",
            node_type: "coalesce",
            plugin: null,
            // Backend-compatible first-branch placeholder only. Rendering it
            // through ordinary input inference is what produced the single
            // mislabelled arm.
            input: "pairing_done",
            on_success: "final_out",
            on_error: null,
            options: {},
            branches: {
              branch_a: "pairing_done",
              branch_b: "hex_done",
            },
            policy: "require_all",
            merge: "union",
          },
        ],
        outputs: [{ name: "final_out", plugin: "csv", options: {} }],
        edges: [],
      });
    }

    function edgeIds(): string[] {
      return Array.from(document.querySelectorAll('[data-testid^="edge-"]'))
        .map((el) => el.getAttribute("data-testid") ?? "")
        .sort();
    }

    /** Rendered edge testids whose TARGET is `target`, sorted. */
    function inboundEdgeIds(target: string): string[] {
      return Array.from(
        document.querySelectorAll(`[data-edge-target="${target}"]`),
      )
        .map((el) => el.getAttribute("data-testid") ?? "")
        .sort();
    }

    it("draws every branch producer into a coalesce by alias, with no placeholder bypass", () => {
      useSessionStore.setState({ compositionState: coalesceState() });
      render(<GraphView />);

      const ids = edgeIds();
      expect(ids).toContain(
        "edge-inferred-fan-in-recommend_pairing-merge_branches-branch_a",
      );
      expect(ids).toContain(
        "edge-inferred-fan-in-get_hex-merge_branches-branch_b",
      );
      expect(
        screen.getByTestId(
          "edge-inferred-fan-in-recommend_pairing-merge_branches-branch_a",
        ),
      ).toHaveTextContent("branch_a");
      expect(
        screen.getByTestId(
          "edge-inferred-fan-in-get_hex-merge_branches-branch_b",
        ),
      ).toHaveTextContent("branch_b");

      // The placeholder must not also arrive as a generic connection edge:
      // that is the duplicate-arm regression, and it is the shape the bug
      // originally rendered ALONE, labelled "success" rather than "branch_a".
      expect(ids).not.toContain(
        "edge-inferred-conn-recommend_pairing-merge_branches",
      );
      expect(inboundEdgeIds("merge_branches")).toEqual([
        "edge-inferred-fan-in-get_hex-merge_branches-branch_b",
        "edge-inferred-fan-in-recommend_pairing-merge_branches-branch_a",
      ]);
    });

    it("claims and relabels unlabelled explicit branch edges rather than duplicating or pruning them", () => {
      // Every one of the 20 corpus states that materialises inbound coalesce
      // edges writes them with label: null. Phase 1 must claim each as its
      // alias hint; phase 1b must then find nothing unclaimed to prune.
      const state = coalesceState();
      state.edges = [
        makeEdge({
          id: "e_a_merge",
          from_node: "recommend_pairing",
          to_node: "merge_branches",
          label: null,
        }),
        makeEdge({
          id: "e_b_merge",
          from_node: "get_hex",
          to_node: "merge_branches",
          label: null,
        }),
      ];
      useSessionStore.setState({ compositionState: state });
      render(<GraphView />);

      const inbound = inboundEdgeIds("merge_branches");
      expect(inbound).toHaveLength(2);
      const labels = inbound
        .map((id) => screen.getByTestId(id).textContent ?? "")
        .sort();
      expect(labels[0]).toContain("branch_a");
      expect(labels[1]).toContain("branch_b");
      expect(
        inbound.some((id) => id.startsWith("edge-inferred-fan-in-")),
      ).toBe(false);
    });

    it("keeps a coalesce's explicit outbound edge when the node declares no outbound authority", () => {
      // The corpus holds `merge_branches -> tidy_columns` on a coalesce whose
      // on_success, on_error and routes are ALL null. Nothing can register an
      // authoritative outbound semantic for it, so the row_union outbound
      // rewrite — which drops every unclaimed hint whose source is a row union
      // — would erase a working connection. That rewrite is therefore
      // deliberately NOT extended to coalesce, and this test is the guard on
      // that exclusion.
      const state = coalesceState();
      const coalesce = state.nodes.find((node) => node.id === "merge_branches");
      expect(coalesce).toBeDefined();
      coalesce!.on_success = null;
      state.nodes.push(
        makeNode({ id: "tidy_columns", input: "nothing", on_success: null }),
      );
      state.edges = [
        makeEdge({
          id: "e_merge_tidy",
          from_node: "merge_branches",
          to_node: "tidy_columns",
          label: null,
        }),
      ];
      useSessionStore.setState({ compositionState: state });
      render(<GraphView />);

      expect(edgeIds()).toContain("edge-e-merge_branches-tidy_columns-0");
    });
  });

  // Regression: bug elspeth-0e2d449d82.
  // The `fitView` boolean prop on @xyflow/react v12 re-fires on every
  // `nodesInitialized` flip, which destroys the operator's pan/zoom whenever
  // the LLM mutates the DAG. The component must mount-fit imperatively via
  // `onInit` and never re-fit on topology change. This is a structural
  // contract test — jsdom cannot exercise viewport behaviour, so we pin the
  // prop shape that produces it.
  describe("viewport stability (regression elspeth-0e2d449d82)", () => {
    beforeEach(() => {
      useSessionStore.setState({
        compositionState: makeState({ nodes: [makeNode()], edges: [] }),
      });
    });

    it("does not pass `fitView` boolean prop to ReactFlow", () => {
      render(<GraphView />);
      const flow = screen.getByTestId("react-flow");
      expect(flow.dataset.fitViewProp).toBe("absent");
    });

    it("provides an onInit callback for one-shot mount fit", () => {
      render(<GraphView />);
      const flow = screen.getByTestId("react-flow");
      expect(flow.dataset.hasOnInit).toBe("true");
    });

    it("supplies fitViewOptions so the Controls fit-view button shares the same constraints", () => {
      render(<GraphView />);
      const flow = screen.getByTestId("react-flow");
      expect(flow.dataset.fitViewOptions).not.toBe("");
      const opts = JSON.parse(flow.dataset.fitViewOptions ?? "{}");
      expect(opts).toEqual({ padding: 0.15, maxZoom: 1.5, minZoom: 0.3 });
    });

    // The test above pins the <ReactFlow> prop, which the fit-view BUTTON never
    // reads: ControlsComponent calls fitView(its own fitViewOptions prop)
    // (@xyflow/react dist/esm/index.js:4558,4571), and fitView(undefined)
    // writes undefined into the store, so fitViewport falls through to the
    // library default maxZoom 2 / padding 0.1. That is what zoomed the canvas
    // to 2.0x on every click and then disabled the zoom-in button
    // (elspeth-a8074a3a7b). The Controls prop is a SEPARATE surface and needs
    // its own assertion.
    it("gives the Controls fit-view button its own copy of the options", () => {
      render(<GraphView />);
      const controls = screen.getByTestId("react-flow-controls");
      expect(controls.dataset.fitViewOptions).not.toBe("");
      expect(JSON.parse(controls.dataset.fitViewOptions ?? "{}")).toEqual({
        padding: 0.15,
        maxZoom: 1.5,
        minZoom: 0.3,
      });
    });

    it("hands both fit-view surfaces the identical options", () => {
      render(<GraphView />);
      expect(
        screen.getByTestId("react-flow-controls").dataset.fitViewOptions,
      ).toBe(screen.getByTestId("react-flow").dataset.fitViewOptions);
    });
  });

  // Node state must never be carried in border WIDTH: the card is a fixed
  // NODE_WIDTH x NODE_HEIGHT box, so 1px -> 2px reflows its contents 1px down
  // and 1px right and the canvas appears to shiver the instant validation
  // completes (elspeth-003794d55c).
  describe("node state rings cost no layout (elspeth-003794d55c)", () => {
    const styleOf = (nodeId: string) =>
      JSON.parse(
        screen.getByTestId(`node-${nodeId}`).getAttribute("data-node-style") ??
          "null",
      );

    const validatedState = () => {
      useSessionStore.setState({
        selectedNodeId: null,
        compositionState: makeState({
          nodes: [
            makeNode({ id: "plain", input: "", on_success: null }),
            makeNode({ id: "needs_fix", input: "", on_success: null }),
            makeNode({ id: "needs_review", input: "", on_success: null }),
          ],
        }),
      } as never);
      useExecutionStore.setState({
        validationResult: {
          is_valid: false,
          checks: [],
          errors: [
            {
              component_id: "needs_fix",
              component_type: "transform",
              message: "Missing source plugin",
              suggestion: null,
            },
          ],
          warnings: [
            {
              component_id: "needs_review",
              component_type: "transform",
              message: "Review optional mapping",
              suggestion: null,
            },
          ],
        },
      } as never);
    };

    it("keeps the border 1px in the unvalidated, error and warning states", () => {
      validatedState();
      render(<GraphView />);

      expect(styleOf("plain").border).toBe(
        "1px solid var(--color-border-strong)",
      );
      expect(styleOf("needs_fix").border).toBe("1px solid var(--color-error)");
      expect(styleOf("needs_review").border).toBe(
        "1px solid var(--color-warning)",
      );
    });

    it("carries the validation emphasis in a box-shadow ring instead", () => {
      validatedState();
      render(<GraphView />);

      expect(styleOf("plain").boxShadow).toBeUndefined();
      expect(styleOf("needs_fix").boxShadow).toBe(
        "0 0 0 1px var(--color-error)",
      );
      expect(styleOf("needs_review").boxShadow).toBe(
        "0 0 0 1px var(--color-warning)",
      );
    });

    it("keeps selection on the same 1px border as every other state", () => {
      useSessionStore.setState({
        selectedNodeId: "classify",
        compositionState: makeState({
          nodes: [
            makeNode({ id: "classify", input: "", on_success: null }),
            makeNode({ id: "other", input: "", on_success: null }),
          ],
        }),
      } as never);
      render(<GraphView />);

      expect(styleOf("classify").border).toBe(
        "1px solid var(--color-selected-ring)",
      );
      expect(styleOf("classify").boxShadow).toBe(
        "0 0 0 3px var(--color-selected-ring)",
      );
      // The whole point: selecting a node cannot move anything inside it, so
      // its border is byte-identical in width to its unselected neighbour's.
      expect(styleOf("classify").border.split(" ")[0]).toBe(
        styleOf("other").border.split(" ")[0],
      );
    });
  });

  // A lowercase Latin letter standing in for the close glyph reads as text
  // rather than as an affordance, and sits at a different optical weight and
  // cap height from the nine other closes in the product. GraphModal.test.tsx
  // already names the standard (elspeth-51cbcf1664).
  describe("node config close glyph (elspeth-51cbcf1664)", () => {
    it("uses the standard × close glyph (not a lowercase 'x')", () => {
      useSessionStore.setState({
        selectedNodeId: "classify",
        compositionState: makeState({
          nodes: [makeNode({ id: "classify", input: "", on_success: null })],
        }),
      } as never);
      render(<GraphView />);

      const closeBtn = screen.getByRole("button", {
        name: /close node configuration/i,
      });
      expect(closeBtn.textContent?.trim()).toBe("×");
      expect(closeBtn.textContent).not.toContain("x");
    });
  });

  // Direction is the single most important thing this diagram states, so it
  // belongs to EVERY connector. markerEnd used to be written only inside
  // assignParallelEdgeLanes, whose lane pass short-circuits for endpoint groups
  // of fewer than 2 edges — so one branching pipeline drew directed and
  // undirected connectors side by side (elspeth-ddae27dff1).
  describe("edge direction markers (elspeth-ddae27dff1)", () => {
    // a -> b twice (a parallel-lane group) alongside a lone b -> c edge: the
    // exact mix the lane pass used to split into directed and undirected.
    const mixedLaneState = () =>
      makeState({
        nodes: [
          makeNode({ id: "a", input: "", on_success: null }),
          makeNode({ id: "b", input: "", on_success: null }),
          makeNode({ id: "c", input: "", on_success: null }),
        ],
        edges: [
          makeEdge({
            id: "e1",
            from_node: "a",
            to_node: "b",
            edge_type: "on_success",
            label: "one",
          }),
          makeEdge({
            id: "e2",
            from_node: "a",
            to_node: "b",
            edge_type: "on_error",
            label: "two",
          }),
          makeEdge({
            id: "e3",
            from_node: "b",
            to_node: "c",
            edge_type: "on_success",
            label: "solo",
          }),
        ],
      });

    const markerOf = (edge: Element) =>
      JSON.parse(edge.getAttribute("data-marker-end") ?? "null");

    it("gives an arrowhead to the lone edge as well as the lane group", () => {
      useSessionStore.setState({ compositionState: mixedLaneState() });
      const { container } = render(<GraphView />);

      const laneEdges = container.querySelectorAll(
        '[data-edge-source="a"][data-edge-target="b"]',
      );
      const loneEdges = container.querySelectorAll(
        '[data-edge-source="b"][data-edge-target="c"]',
      );
      expect(laneEdges).toHaveLength(2);
      expect(loneEdges).toHaveLength(1);

      for (const edge of [...laneEdges, ...loneEdges]) {
        expect(markerOf(edge)).toMatchObject({ type: "arrowclosed" });
      }
    });

    // Every edge here is INFERRED, and each comes from a different construction
    // site: source -> transform (conn), transform -> queue (queue-in),
    // queue -> transform (queue-out), transform -> sink on success and on
    // error (sink). None of them forms a parallel lane group, so before the fix
    // not one of them carried an arrowhead.
    it("leaves no inferred edge undirected, whichever site built it", () => {
      useSessionStore.setState({
        compositionState: makeState({
          sources: {
            source: { plugin: "text", options: {}, on_success: "raw" },
          },
          nodes: [
            makeNode({ id: "clean", input: "raw", on_success: "queued" }),
            makeNode({
              id: "queued",
              node_type: "queue",
              input: "",
              on_success: null,
            }),
            makeNode({
              id: "score",
              input: "queued",
              on_success: "results",
              on_error: "rejects",
            }),
          ],
          outputs: [
            { name: "results", plugin: "csv", options: {} },
            { name: "rejects", plugin: "csv", options: {} },
          ],
        }),
      });
      const { container } = render(<GraphView />);

      const edges = [...container.querySelectorAll("[data-marker-end]")];
      // conn + queue-in + queue-out + sink(success) + sink(error).
      expect(edges).toHaveLength(5);
      for (const edge of edges) {
        expect(markerOf(edge)).toMatchObject({ type: "arrowclosed" });
      }
    });

    it("points the arrowhead in the colour of the line it terminates", () => {
      useSessionStore.setState({ compositionState: mixedLaneState() });
      const { container } = render(<GraphView />);

      // e1 (on_success) and e2 (on_error) share endpoints, so they are the same
      // lane group: the arrowhead colour must still follow each edge's own
      // flow type, not the group's.
      const laneMarkerColors = [
        ...container.querySelectorAll(
          '[data-edge-source="a"][data-edge-target="b"]',
        ),
      ]
        .map((edge) => markerOf(edge).color)
        .sort();
      expect(laneMarkerColors).toEqual(
        ["var(--color-error)", "var(--color-text-muted)"].sort(),
      );

      const lone = container.querySelector(
        '[data-edge-source="b"][data-edge-target="c"]',
      );
      expect(markerOf(lone!).color).toBe("var(--color-text-muted)");
    });
  });

  // Two floating panels on one canvas must rest on one baseline. An inline
  // bottom/right on the MiniMap does not replace .react-flow__panel's own 15px
  // margin, it STACKS on it — which put the MiniMap 23px off the canvas floor
  // against the Controls' 15px (elspeth-a7ce6e6a4c).
  describe("floating panel insets (elspeth-a7ce6e6a4c)", () => {
    beforeEach(() => {
      const nodes = Array.from({ length: 9 }, (_, i) =>
        makeNode({ id: `n${i}`, node_type: "transform", plugin: "p" }),
      );
      useSessionStore.setState({ compositionState: makeState({ nodes }) });
    });

    it("gives the MiniMap no inline inset of its own", () => {
      render(<GraphView />);
      const style = JSON.parse(
        screen.getByTestId("minimap").dataset.style ?? "null",
      );
      expect(style).not.toBeNull();
      expect(style).not.toHaveProperty("bottom");
      expect(style).not.toHaveProperty("right");
      expect(style).not.toHaveProperty("top");
      expect(style).not.toHaveProperty("left");
    });

    it("keeps the MiniMap's own dimensions, which MiniMapComponent scales by", () => {
      render(<GraphView />);
      const style = JSON.parse(
        screen.getByTestId("minimap").dataset.style ?? "null",
      );
      expect(style).toEqual({ width: 120, height: 80 });
    });
  });

  // Canvas nodes AND edges live inside the role="img" children-presentational
  // diagram wrapper: focusable, they would be invisible tab stops with no
  // usable announcement. The a11y node list and the "Pipeline branch
  // connections" list are the keyboard/AT equivalents (elspeth-437caadef3).
  describe("canvas focusability (elspeth-437caadef3)", () => {
    it("removes canvas nodes and edges from the tab order", () => {
      useSessionStore.setState({
        compositionState: makeState({ nodes: [makeNode({ id: "classify" })] }),
      });
      render(<GraphView />);
      const flow = screen.getByTestId("react-flow");
      expect(flow.dataset.nodesFocusable).toBe("false");
      expect(flow.dataset.edgesFocusable).toBe("false");
    });
  });

  describe("accessible node list (C2/C3, elspeth-ef897110dd / elspeth-d37b7217c9)", () => {
    it("exposes every component as a keyboard-operable item with type + validity", () => {
      useSessionStore.setState({
        compositionState: makeState({
          sources: { in: { plugin: "csv", on_success: "t1", options: {} } } as never,
          nodes: [
            makeNode({
              id: "classify",
              node_type: "transform",
              plugin: "llm_transform",
              input: "t1",
              on_success: "out",
            }),
          ],
          outputs: [{ name: "out", plugin: "jsonl", options: {} }] as never,
        }),
      });
      render(<GraphView />);
      const list = screen.getByRole("list", {
        name: /pipeline components in source-to-sink order/i,
      });
      const items = within(list).getAllByRole("button");
      // source + 1 transform + sink = 3 accessible entries.
      expect(items).toHaveLength(3);
      const text = items.map((b) => b.textContent ?? "");
      expect(text.some((t) => /source:.*csv/i.test(t))).toBe(true);
      expect(text.some((t) => /transform: classify \(llm_transform\)/i.test(t))).toBe(true);
      expect(text.some((t) => /sink:.*jsonl/i.test(t))).toBe(true);
      // Validity is announced, not colour-only.
      expect(text.every((t) => /valid|warning|error|not yet validated/i.test(t))).toBe(true);
    });

    it("selects a node via keyboard activation (drives the inspector path)", async () => {
      const selectNode = vi.fn();
      useSessionStore.setState({
        selectNode,
        compositionState: makeState({ nodes: [makeNode({ id: "classify" })] }),
      } as never);
      render(<GraphView />);
      const list = screen.getByRole("list", { name: /pipeline components/i });
      await userEvent.click(within(list).getByRole("button", { name: /classify/i }));
      expect(selectNode).toHaveBeenCalledWith("classify");
    });

    it("moves focus to the NodeConfigPanel when a node is selected from the keyboard list (elspeth-37f6f13132)", async () => {
      // Real selection semantics: selectNode drives selectedNodeId so the
      // panel actually opens (previous tests stub selectNode as a spy).
      useSessionStore.setState({
        selectedNodeId: null,
        selectNode: (nodeId: string | null) =>
          useSessionStore.setState({ selectedNodeId: nodeId } as never),
        compositionState: makeState({ nodes: [makeNode({ id: "classify" })] }),
      } as never);
      render(<GraphView />);
      const list = screen.getByRole("list", { name: /pipeline components/i });
      await userEvent.click(
        within(list).getByRole("button", { name: /classify/i }),
      );
      const panel = screen.getByRole("complementary", {
        name: /classify configuration/i,
      });
      expect(panel).toHaveFocus();
    });
  });

  // Truth tests (not declaration tests) for the per-node validity the a11y
  // list announces. A passing validation attributes no per-node findings, so
  // 'valid' is derived from is_valid === true — gated honestly: a failing
  // result with only unattributed (component_id: null) errors must never
  // claim per-node validity (elspeth-b5b7c5a6ad).
  describe("per-node validity announcement (elspeth-b5b7c5a6ad)", () => {
    const passingShapeState = () =>
      makeState({
        sources: { in: { plugin: "csv", on_success: "t1", options: {} } } as never,
        nodes: [
          makeNode({
            id: "classify",
            node_type: "transform",
            plugin: "llm_transform",
            input: "t1",
            on_success: "out",
          }),
        ],
        outputs: [{ name: "out", plugin: "jsonl", options: {} }] as never,
      });

    const a11yButtonText = () => {
      const list = screen.getByRole("list", {
        name: /pipeline components in source-to-sink order/i,
      });
      return within(list)
        .getAllByRole("button")
        .map((b) => b.textContent ?? "");
    };

    it("announces every component as valid on a passing result", () => {
      useSessionStore.setState({ compositionState: passingShapeState() });
      useExecutionStore.setState({
        validationResult: { is_valid: true, checks: [], errors: [], warnings: [] },
      } as never);
      render(<GraphView />);
      const text = a11yButtonText();
      expect(text).toHaveLength(3);
      expect(text.every((t) => /— passed validation\./.test(t))).toBe(true);
      expect(text.some((t) => t.includes("not yet validated"))).toBe(false);
    });

    // The graph's own summary counts components; the singular case had no
    // oracle, so "1 components" could ship unnoticed.
    it("names a one-component graph in the singular", () => {
      useSessionStore.setState({
        compositionState: makeState({
          sources: { in: { plugin: "csv", on_success: null, options: {} } } as never,
        }),
      });
      render(<GraphView />);
      expect(
        screen.getByRole("img", { name: /^Pipeline graph with 1 component \(/ }),
      ).toBeInTheDocument();
    });

    // Vocabulary parity, pinned as a relation rather than as two literals:
    // the node's passing phrase must be built from the same word the
    // validation status chip shows, so an operator cross-checking chip
    // against node never meets two names for one fact. Reworded on either
    // side without the other, this goes red.
    it("announces the passing state with the status chip's own word", () => {
      useSessionStore.setState({ compositionState: passingShapeState() });
      const passing = { is_valid: true, checks: [], errors: [], warnings: [] };
      useExecutionStore.setState({ validationResult: passing } as never);
      render(<GraphView />);
      const chip = projectValidationWorkspaceStatus(passing as never);
      expect(chip.text).toBe("Passed");
      const phrase = `— ${chip.text.toLowerCase()} validation.`;
      expect(a11yButtonText().every((t) => t.includes(phrase))).toBe(true);
    });

    // Verb-phrase parallelism across the four states: the passing arm must
    // not drift back to a bare adjective while its siblings stay verbs.
    it("keeps the passing phrase a verb phrase, not the bare adjective", () => {
      useSessionStore.setState({ compositionState: passingShapeState() });
      useExecutionStore.setState({
        validationResult: { is_valid: true, checks: [], errors: [], warnings: [] },
      } as never);
      render(<GraphView />);
      const text = a11yButtonText();
      expect(text).toHaveLength(3);
      expect(text.some((t) => /— valid\./.test(t))).toBe(false);
    });

    it("announces the warned node as warning and the rest as valid on a warnings-only pass", () => {
      useSessionStore.setState({ compositionState: passingShapeState() });
      useExecutionStore.setState({
        validationResult: {
          is_valid: true,
          checks: [],
          errors: [],
          warnings: [
            {
              component_id: "classify",
              component_type: "transform",
              message: "Review optional mapping",
              suggestion: null,
            },
          ],
        },
      } as never);
      render(<GraphView />);
      const text = a11yButtonText();
      const warned = text.filter((t) => t.includes("classify"));
      expect(warned).toHaveLength(1);
      expect(warned[0]).toMatch(/has warnings/);
      const others = text.filter((t) => !t.includes("classify"));
      expect(others).toHaveLength(2);
      expect(others.every((t) => /— passed validation\./.test(t))).toBe(true);
    });

    it("keeps every component 'not yet validated' when no validation has run", () => {
      useSessionStore.setState({ compositionState: passingShapeState() });
      // beforeEach leaves validationResult: null — the honest-unknown arm.
      render(<GraphView />);
      const text = a11yButtonText();
      expect(text).toHaveLength(3);
      expect(text.every((t) => t.includes("not yet validated"))).toBe(true);
      expect(text.some((t) => /— passed validation\./.test(t))).toBe(false);
    });

    it("never claims validity when a global (unattributed) error failed the pipeline", () => {
      useSessionStore.setState({ compositionState: passingShapeState() });
      useExecutionStore.setState({
        validationResult: {
          is_valid: false,
          checks: [],
          errors: [
            {
              component_id: null,
              component_type: null,
              message: "Pipeline is structurally incomplete",
              suggestion: null,
            },
          ],
          warnings: [],
        },
      } as never);
      render(<GraphView />);
      const text = a11yButtonText();
      expect(text).toHaveLength(3);
      expect(text.every((t) => t.includes("not yet validated"))).toBe(true);
      expect(text.some((t) => /— passed validation\./.test(t))).toBe(false);
    });
  });

  // role="img" is children-presentational: descendants are pruned from the
  // accessibility tree. It must therefore scope the diagram ONLY; the live
  // "pending #N" pill and the interactive zoom Controls (and MiniMap) are
  // siblings (WCAG 4.1.3 / 1.3.1, elspeth-37f6f13132).
  describe("role=img scoping (elspeth-37f6f13132)", () => {
    it("scopes role='img' to the diagram element with the pipeline label", () => {
      useSessionStore.setState({
        compositionState: makeState({ nodes: [makeNode({ id: "classify" })] }),
        compositionProposals: [makeProposal()],
      });
      render(<GraphView />);
      const img = screen.getByRole("img", { name: /pipeline graph with/i });
      expect(img).toHaveClass("graph-view-diagram");
      // The diagram itself lives inside the img scope.
      expect(within(img).getByTestId("react-flow")).toBeInTheDocument();
    });

    it("keeps the live pending pill and Controls OUTSIDE the img scope", () => {
      useSessionStore.setState({
        compositionState: makeState({ nodes: [makeNode({ id: "classify" })] }),
        compositionProposals: [makeProposal()],
      });
      render(<GraphView />);
      const img = screen.getByRole("img", { name: /pipeline graph with/i });
      const pill = screen.getByRole("status", { name: /pending graph proposal/i });
      const controls = screen.getByTestId("react-flow-controls");
      expect(img.contains(pill)).toBe(false);
      expect(img.contains(controls)).toBe(false);
    });

    it("keeps the MiniMap outside the img scope on large graphs", () => {
      const nodes = Array.from({ length: 9 }, (_, i) =>
        makeNode({ id: `n${i}`, node_type: "transform", plugin: "p" }),
      );
      useSessionStore.setState({ compositionState: makeState({ nodes }) });
      render(<GraphView />);
      const img = screen.getByRole("img", { name: /pipeline graph with/i });
      expect(img.contains(screen.getByTestId("minimap"))).toBe(false);
    });
  });
});
