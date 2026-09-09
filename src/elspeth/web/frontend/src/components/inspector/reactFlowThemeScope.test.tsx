// ============================================================================
// React Flow theme REACHABILITY (elspeth-1adf90c933, elspeth-525d335cbc).
//
// The defect this file exists for was invisible to every test the project had:
// inspector.css declared ~25 correct `--xy-*` custom properties on
// `:root .react-flow.react-flow`, while <Controls> and <MiniMap> mount as
// SIBLINGS of the role="img" diagram — outside `.react-flow` (WCAG 4.1.3,
// elspeth-37f6f13132). Custom properties fail SILENTLY across that boundary:
// every `var(--xy-…)` became invalid-at-computed-value-time, so the controls
// painted transparent and the minimap mask fell to the SVG initial (black).
// The declarations were all present and all correct; only their SCOPE was
// wrong, which is exactly what a test that greps a stylesheet for declaration
// text cannot see (GraphView.test.tsx used to assert precisely that).
//
// Why this is not a getComputedStyle test: vitest runs with `css: false`
// (vite.config.ts), so no stylesheet is ever attached to the jsdom document,
// and jsdom does not perform var() substitution or custom-property
// inheritance in any case. A getComputedStyle assertion here would return ""
// for every probe and pass whatever the CSS said. What CAN be measured
// honestly is REACHABILITY, which is the actual failed invariant:
//
//   (1) the DOM ancestry of the themed elements, from a real <GraphView />
//       render, and
//   (2) the selector list of the rule that declares the property, parsed from
//       inspector.css,
//
// combined through the DOM's own selector engine via Element.closest(). If the
// declaring rule matches no ancestor of the element it themes, the theme never
// arrives — regardless of how correct the declarations read.
// ============================================================================

import { readFileSync } from "node:fs";

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { GraphView } from "./GraphView";
import { useSessionStore } from "@/stores/sessionStore";
import { useExecutionStore } from "@/stores/executionStore";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import type { CompositionState, NodeSpec } from "@/types/index";

const INSPECTOR_CSS_PATH = "src/components/inspector/inspector.css";
const XYFLOW_CSS_PATH = "node_modules/@xyflow/react/dist/style.css";

// jsdom cannot do the DOM measurement React Flow needs, so the library is
// mocked — the same constraint GraphView.test.tsx works under. The mock
// reproduces the class names and nesting @xyflow actually renders, and the
// "vendor stylesheet" describe block below pins those names against the
// shipped stylesheet so a library rename cannot leave this file green.
vi.mock("@xyflow/react/dist/style.css", () => ({}));
vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  ReactFlowProvider: ({ children }: any) => <div className="rfp">{children}</div>,
  ReactFlow: ({ children }: any) => (
    // The real flow root carries `react-flow` (plus `react-flow__container`
    // descendants). Nodes/edges are irrelevant here: this file asks where
    // elements sit, not what they render.
    <div className="react-flow" data-testid="react-flow">
      <div className="react-flow__renderer">{children}</div>
    </div>
  ),
  Background: () => <div className="react-flow__background" />,
  Handle: () => <div className="react-flow__handle" />,
  BaseEdge: () => <g className="react-flow__edge" />,
  Controls: () => (
    <div className="react-flow__controls react-flow__panel bottom left">
      <button
        type="button"
        className="react-flow__controls-button react-flow__controls-zoomin"
        data-testid="controls-button"
      />
    </div>
  ),
  MiniMap: () => (
    <div className="react-flow__minimap react-flow__panel bottom right">
      <svg className="react-flow__minimap-svg">
        <path className="react-flow__minimap-mask" data-testid="minimap-mask" />
      </svg>
    </div>
  ),
}));

vi.mock("@dagrejs/dagre", () => ({
  default: {
    graphlib: {
      Graph: class {
        setDefaultEdgeLabel() {}
        setGraph() {}
        setNode() {}
        setEdge() {}
        node(_id: string) {
          return { x: 0, y: 0 };
        }
      },
    },
    layout() {},
  },
}));

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

interface CssRule {
  /** Selector list exactly as authored, comma-joined. */
  selectorList: string;
  declarations: string;
}

function parseRules(css: string): CssRule[] {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  return Array.from(withoutComments.matchAll(/([^{}]+)\{([^{}]*)\}/g)).map(
    (match) => ({
      selectorList: match[1]!.split(/\s*,\s*/).map((s) => s.trim()).join(", "),
      declarations: match[2]!,
    }),
  );
}

const inspectorCss = readFileSync(INSPECTOR_CSS_PATH, "utf8");
const inspectorRules = parseRules(inspectorCss);

/** Every rule in inspector.css that declares `property`. */
function rulesDeclaring(property: string): CssRule[] {
  return inspectorRules.filter((rule) =>
    new RegExp(`(^|[\\s;])${property}\\s*:`).test(rule.declarations),
  );
}

describe("React Flow theming reaches the elements it themes", () => {
  beforeEach(() => {
    // Nine nodes: the MiniMap mounts only above MINIMAP_NODE_COUNT_THRESHOLD.
    useSessionStore.setState({
      compositionState: makeState({
        nodes: Array.from({ length: 9 }, (_, index) =>
          makeNode({ id: `n${index}` }),
        ),
      }),
      compositionProposals: [],
    });
    useSessionStore.setState({ selectedNodeId: null } as never);
    useExecutionStore.setState({ validationResult: null } as never);
    document.documentElement.removeAttribute("data-theme");
  });

  // The two elements whose theming was dead, each paired with a property the
  // element's own vendor rule consumes.
  const PROBES: ReadonlyArray<{
    label: string;
    testId: string;
    property: string;
  }> = [
    {
      label: "the zoom controls' buttons",
      testId: "controls-button",
      property: "--xy-controls-button-background-color-default",
    },
    {
      label: "the minimap mask",
      testId: "minimap-mask",
      property: "--xy-minimap-mask-background-color-default",
    },
  ];

  for (const probe of PROBES) {
    it(`declares ${probe.property} on an ancestor of ${probe.label}`, () => {
      render(<GraphView />);
      const element = screen.getByTestId(probe.testId);

      const declaring = rulesDeclaring(probe.property);
      expect(declaring.length).toBeGreaterThan(0);

      for (const rule of declaring) {
        // Each rule is evaluated in the theme it was written for — the light
        // override block was entirely dead before the scope fix, which only a
        // per-theme evaluation can show.
        document.documentElement.setAttribute(
          "data-theme",
          /\[data-theme="light"\]/.test(rule.selectorList) ? "light" : "dark",
        );
        // closest() runs the DOM's own selector matching over the REAL
        // ancestry, so this answers "does the declaring rule reach this
        // element", not "does inspector.css contain this text".
        expect(
          element.closest(rule.selectorList),
          `no ancestor of ${probe.testId} matches "${rule.selectorList}" — `
            + `${probe.property} never reaches it`,
        ).not.toBeNull();
      }
    });
  }

  it("keeps the controls OUTSIDE the role=\"img\" diagram, which is why the scope must be wider", () => {
    render(<GraphView />);
    const button = screen.getByTestId("controls-button");

    // Pinned structure (elspeth-37f6f13132): role="img" is
    // children-presentational, so interactive controls must not live inside
    // it. This is the constraint that makes a `.react-flow`-only theme scope
    // insufficient — it must never be "fixed" by moving <Controls> back in.
    expect(button.closest(".react-flow")).toBeNull();
    expect(button.closest('[role="img"]')).toBeNull();
    expect(button.closest(".graph-view-canvas")).not.toBeNull();
  });

  it("scopes every --xy-* rule to the canvas wrapper as well as the flow element", () => {
    const themingRules = inspectorRules.filter((rule) =>
      /(^|[\s;])--xy-[a-z-]+\s*:/.test(rule.declarations),
    );
    expect(themingRules.length).toBeGreaterThan(0);

    for (const rule of themingRules) {
      // Both halves are load-bearing: `.graph-view-canvas` is the only common
      // ancestor of the diagram and the sibling-mounted panels, while the
      // `.react-flow` half must stay because @xyflow declares its own
      // --xy-*-default values ON that element, where they would beat a value
      // merely inherited from an ancestor.
      expect(rule.selectorList).toMatch(/\.graph-view-canvas\b/);
      expect(rule.selectorList).toMatch(/\.react-flow\.react-flow\b/);
    }
  });
});

describe("vendor stylesheet assumptions this file rests on", () => {
  const vendorCss = readFileSync(XYFLOW_CSS_PATH, "utf8");
  const vendorRules = parseRules(vendorCss);

  it("declares every --xy-*-default ONLY on .react-flow itself", () => {
    // This is the whole reason scope matters: outside `.react-flow` there is
    // no fallback value anywhere in the chain, so each var() collapses to
    // invalid-at-computed-value-time rather than to a sane vendor default.
    const declaringRules = vendorRules.filter((rule) =>
      /(^|[\s;])--xy-controls-button-background-color-default\s*:/.test(
        rule.declarations,
      ),
    );
    expect(declaringRules.length).toBeGreaterThan(0);
    for (const rule of declaringRules) {
      expect(rule.selectorList).toMatch(/\.react-flow\b/);
    }
  });

  it("still names the classes this file's mock renders", () => {
    for (const className of [
      ".react-flow__controls-button",
      ".react-flow__minimap-mask",
      ".react-flow__panel",
      ".react-flow__edge-textbg",
    ]) {
      expect(vendorCss).toContain(className);
    }
  });

  it("consumes the themed properties from the elements the probes use", () => {
    const consumers = new Map([
      [".react-flow__controls-button", "--xy-controls-button-background-color-default"],
      [".react-flow__minimap-mask", "--xy-minimap-mask-background-color-default"],
    ]);
    for (const [selector, property] of consumers) {
      const rule = vendorRules.find((candidate) =>
        candidate.selectorList.split(", ").includes(selector),
      );
      expect(rule, `@xyflow no longer styles ${selector}`).toBeDefined();
      expect(rule!.declarations).toContain(property);
    }
  });
});
