// src/components/chat/guided/guidedGraphProjection.test.ts
//
// The projection is the single authority for what the right-hand Pipeline
// pane draws before Confirm wiring (elspeth-9f0873426a). These tests pin the
// per-step derivation: lone reviewed nodes with NO edges (the frontend never
// authors a route), the full proposal DAG with the virtual discard node and
// stable edge identities, the wire stage drawn from its connections, and the
// precedence rule (a pending decision beats the ledger).

import { describe, expect, it } from "vitest";

import { EMPTY_GUIDED_REVIEWED_COMPONENTS } from "@/stores/guidedReviewedComponents";
import type {
  ProposePipelinePayload,
  TurnPayload,
  WireStageData,
} from "@/types/guided";

import {
  GUIDED_GRAPH_DISCARD_NODE_ID,
  flowLabel,
  projectGuidedGraph,
  projectProposalGraph,
  projectReviewedComponentsGraph,
  projectWireStageGraph,
  routeKeysByAlias,
} from "./guidedGraphProjection";

const IDS = {
  proposal: "00000000-0000-4000-8000-000000000701",
  source: "00000000-0000-4000-8000-000000000702",
  gate: "00000000-0000-4000-8000-000000000703",
  union: "00000000-0000-4000-8000-000000000704",
  output: "00000000-0000-4000-8000-000000000705",
} as const;

function edgeId(index: number): string {
  return `00000000-0000-4000-8000-${String(800 + index).padStart(12, "0")}`;
}

function proposal(): ProposePipelinePayload {
  return {
    proposal_id: IDS.proposal,
    draft_hash: "d".repeat(64),
    supersedes_draft_hash: null,
    summary: "guided.proposal.summary.full_graph.v1",
    rationale: "guided.proposal.rationale.review_required.v1",
    component_counts: { sources: 1, nodes: 2, edges: 5, outputs: 1 },
    blockers: [],
    graph: {
      sources: [
        { stable_id: IDS.source, label: "source-1", plugin: { kind: "source", id: "csv" } },
      ],
      edges: [
        {
          stable_id: edgeId(1),
          from_endpoint: { kind: "source", stable_id: IDS.source },
          to_endpoint: { kind: "node", stable_id: IDS.gate },
          flow: { kind: "source_success", branch: null },
        },
        {
          stable_id: edgeId(2),
          from_endpoint: { kind: "source", stable_id: IDS.source },
          to_endpoint: { kind: "discard" },
          flow: { kind: "source_validation_failure" },
        },
        {
          stable_id: edgeId(3),
          from_endpoint: { kind: "node", stable_id: IDS.gate },
          to_endpoint: { kind: "node", stable_id: IDS.union },
          flow: { kind: "gate_fork", routes: ["route-1"], branch: "branch-1" },
        },
        {
          stable_id: edgeId(4),
          from_endpoint: { kind: "node", stable_id: IDS.gate },
          to_endpoint: { kind: "output", stable_id: IDS.output },
          flow: { kind: "gate_route", route: "route-2", branch: null },
        },
        {
          stable_id: edgeId(5),
          from_endpoint: { kind: "node", stable_id: IDS.union },
          to_endpoint: { kind: "output", stable_id: IDS.output },
          flow: { kind: "row_union_success", branch: null },
        },
      ],
    },
    nodes: [
      {
        stable_id: IDS.gate,
        label: "node-1",
        node_type: "gate",
        plugin: null,
        behavior: {
          kind: "gate",
          condition: "row['amount'] > 500",
          route_aliases: ["route-1", "route-2"],
          routes: [
            { alias: "route-1", key: "true" },
            { alias: "route-2", key: "false" },
          ],
          fork_branches: [{ routes: ["route-1"], branch: "branch-1" }],
        },
        node_options_summary: [],
      },
      {
        stable_id: IDS.union,
        label: "node-2",
        node_type: "row_union",
        plugin: null,
        behavior: {
          kind: "row_union",
          branch_aliases: ["branch-1"],
          policy: "require_all",
          timeout_seconds: null,
        },
        node_options_summary: [],
      },
    ],
    outputs: [
      { stable_id: IDS.output, label: "output-1", plugin: { kind: "sink", id: "json" } },
    ],
    edit_targets: [],
  };
}

function wireStage(): WireStageData {
  return {
    proposal_id: IDS.proposal,
    draft_hash: "d".repeat(64),
    sources: [{
      stable_id: IDS.source,
      label: "source-1",
      plugin: "csv",
      on_validation_failure: "discard",
      guaranteed_fields: ["body"],
      row_cardinality: { input: "none", output: "zero_or_many", expected_output_count: null },
    }],
    nodes: [{
      stable_id: IDS.gate,
      label: "node-1",
      node_type: "transform",
      plugin: "field_mapper",
      behavior: { kind: "transform" },
      required_fields: ["body"],
      guaranteed_fields: ["mapped"],
      row_cardinality: { input: "one", output: "one", expected_output_count: null },
      structured_output_fields: [],
      node_options_summary: [],
    }],
    outputs: [{
      stable_id: IDS.output,
      label: "output-1",
      plugin: "json",
      on_write_failure: "discard",
      required_fields: ["mapped"],
      business_schema: { mode: "observed", fields: [], guaranteed_fields: [], required_fields: ["mapped"] },
    }],
    connections: [
      {
        stable_id: edgeId(1),
        from_endpoint: { kind: "source", stable_id: IDS.source },
        to_endpoint: { kind: "node", stable_id: IDS.gate },
        flow: { kind: "source_success", branch: null },
        schema_contract: null,
      },
      {
        stable_id: edgeId(2),
        from_endpoint: { kind: "node", stable_id: IDS.gate },
        to_endpoint: { kind: "output", stable_id: IDS.output },
        flow: { kind: "node_success", branch: null },
        schema_contract: null,
      },
      {
        stable_id: edgeId(3),
        from_endpoint: { kind: "node", stable_id: IDS.gate },
        to_endpoint: { kind: "discard" },
        flow: { kind: "node_error" },
        schema_contract: null,
      },
    ],
    semantic_contracts: [],
    warnings: [],
    blockers: [],
    can_confirm: true,
  };
}

const REVIEWED_SOURCE = {
  stable_id: IDS.source,
  name: "source-1",
  plugin: "csv",
  status: "reviewed" as const,
};

const REVIEWED_OUTPUT = {
  stable_id: IDS.output,
  name: "output-1",
  plugin: "json",
  status: "reviewed" as const,
};

function turn<T extends TurnPayload>(value: T): T {
  return value;
}

describe("projectReviewedComponentsGraph", () => {
  it("draws every reviewed component as a lone node and never invents a route", () => {
    const graph = projectReviewedComponentsGraph({
      sources: [REVIEWED_SOURCE],
      outputs: [REVIEWED_OUTPUT],
    });
    expect(graph.nodes).toEqual([
      { id: IDS.source, label: "source-1", kind: "source", subtitle: "CSV" },
      { id: IDS.output, label: "output-1", kind: "output", subtitle: "JSON" },
    ]);
    expect(graph.edges).toEqual([]);
  });
});

describe("projectProposalGraph", () => {
  it("projects sources, nodes, outputs, the virtual discard node and stable edge ids", () => {
    const graph = projectProposalGraph(proposal());
    expect(graph.nodes.map((node) => [node.id, node.kind])).toEqual([
      [IDS.source, "source"],
      [IDS.gate, "gate"],
      [IDS.union, "row_union"],
      [IDS.output, "output"],
      [GUIDED_GRAPH_DISCARD_NODE_ID, "discard"],
    ]);
    expect(graph.edges.map((edge) => edge.id)).toEqual([1, 2, 3, 4, 5].map(edgeId));
    const discardEdge = graph.edges.find((edge) => edge.id === edgeId(2));
    expect(discardEdge).toEqual({
      id: edgeId(2),
      source: IDS.source,
      target: GUIDED_GRAPH_DISCARD_NODE_ID,
      label: "source-1 on validation failure → discard",
      isError: true,
    });
  });

  it("labels routes with the author-visible key and keeps the ordinal alias visible", () => {
    const graph = projectProposalGraph(proposal());
    expect(graph.edges.find((edge) => edge.id === edgeId(3))?.label).toBe(
      "node-1 when true (route-1) forks to branch-1 → node-2",
    );
    expect(graph.edges.find((edge) => edge.id === edgeId(4))?.label).toBe(
      "node-1 when false (route-2) → output-1",
    );
    expect(graph.edges.find((edge) => edge.id === edgeId(5))?.label).toBe(
      "node-2 after row union → output-1",
    );
  });

  it("omits the discard node when no edge discards", () => {
    const payload = proposal();
    payload.graph.edges = payload.graph.edges.filter((edge) => edge.to_endpoint.kind !== "discard");
    const graph = projectProposalGraph(payload);
    expect(graph.nodes.some((node) => node.kind === "discard")).toBe(false);
    expect(graph.edges.every((edge) => !edge.isError)).toBe(true);
  });
});

describe("projectWireStageGraph", () => {
  it("draws the wire stage from its candidate-derived connections", () => {
    const graph = projectWireStageGraph(wireStage());
    expect(graph.nodes.map((node) => [node.id, node.kind, node.subtitle])).toEqual([
      [IDS.source, "source", "CSV"],
      [IDS.gate, "transform", "Output"],
      [IDS.output, "output", "JSON"],
      [GUIDED_GRAPH_DISCARD_NODE_ID, "discard", null],
    ]);
    expect(graph.edges.map((edge) => [edge.target, edge.isError])).toEqual([
      [IDS.gate, false],
      [IDS.output, false],
      [GUIDED_GRAPH_DISCARD_NODE_ID, true],
    ]);
  });
});

describe("flowLabel / routeKeysByAlias", () => {
  it("resolves gate aliases through the behavior bindings and falls back to the bare alias", () => {
    const keys = routeKeysByAlias(proposal().nodes);
    expect(flowLabel({ kind: "gate_route", route: "route-1", branch: "b" }, keys)).toBe(
      "when true (route-1) in b",
    );
    expect(flowLabel({ kind: "gate_route", route: "route-9", branch: null }, keys)).toBe("route-9");
  });
});

describe("projectGuidedGraph", () => {
  it("returns null when nothing has been reviewed and no decision is pending", () => {
    expect(
      projectGuidedGraph({ nextTurn: null, reviewed: EMPTY_GUIDED_REVIEWED_COMPONENTS }),
    ).toBeNull();
    const sourceSelect = turn({
      type: "single_select",
      step_index: 0,
      turn_token: "a".repeat(64),
      payload: { question: "Choose a source", options: [], allow_custom: false },
    });
    expect(
      projectGuidedGraph({ nextTurn: sourceSelect, reviewed: EMPTY_GUIDED_REVIEWED_COMPONENTS }),
    ).toBeNull();
  });

  it("returns null once the guided session is terminal, whatever the ledger holds", () => {
    // Exit to freeform leaves the store's ledger in place (the fold is
    // identity on a null turn); a freeform session must not keep drawing the
    // guided "Reviewed so far" nodes (red-team F2, 2026-09-02). A completed
    // session draws its committed composition instead.
    const reviewed = {
      sources: [
        {
          stable_id: "src-1",
          name: "orders",
          plugin: "csv",
          summary: "orders.csv",
        } as never,
      ],
      outputs: [],
    };
    expect(
      projectGuidedGraph({
        nextTurn: null,
        reviewed,
        terminal: { kind: "exited_to_freeform", reason: null } as never,
      }),
    ).toBeNull();
    expect(
      projectGuidedGraph({
        nextTurn: null,
        reviewed,
        terminal: { kind: "completed", reason: null, pipeline_yaml: "" },
      }),
    ).toBeNull();
    // The same ledger with no terminal still draws.
    expect(projectGuidedGraph({ nextTurn: null, reviewed, terminal: null })?.stage).toBe("reviewed");
  });

  it("draws the reviewed source alone after step 1 and source plus output after step 2", () => {
    const sinkSelect = turn({
      type: "single_select",
      step_index: 1,
      turn_token: "b".repeat(64),
      payload: { question: "Choose a sink", options: [], allow_custom: false },
    });
    const afterSource = projectGuidedGraph({
      nextTurn: sinkSelect,
      reviewed: { sources: [REVIEWED_SOURCE], outputs: [] },
    });
    expect(afterSource?.stage).toBe("reviewed");
    expect(afterSource?.nodes.map((node) => node.id)).toEqual([IDS.source]);
    expect(afterSource?.edges).toEqual([]);
    expect(afterSource?.caption).toBe(
      "Reviewed so far: 1 source. Routes appear once a pipeline is proposed.",
    );
    expect(afterSource?.ariaLabel).toBe(
      "Reviewed components graph with 1 component and no routes yet",
    );

    const afterOutput = projectGuidedGraph({
      nextTurn: null,
      reviewed: { sources: [REVIEWED_SOURCE], outputs: [REVIEWED_OUTPUT] },
    });
    expect(afterOutput?.nodes.map((node) => node.id)).toEqual([IDS.source, IDS.output]);
    expect(afterOutput?.caption).toBe(
      "Reviewed so far: 1 source and 1 output. Routes appear once a pipeline is proposed.",
    );
  });

  it("draws the pending proposal over the ledger, with the proposal aria name", () => {
    const projection = projectGuidedGraph({
      nextTurn: turn({
        type: "propose_pipeline",
        step_index: 2,
        turn_token: "c".repeat(64),
        payload: proposal(),
      }),
      reviewed: { sources: [REVIEWED_SOURCE], outputs: [REVIEWED_OUTPUT] },
    });
    expect(projection?.stage).toBe("proposal");
    expect(projection?.nodes).toHaveLength(5);
    expect(projection?.edges).toHaveLength(5);
    expect(projection?.ariaLabel).toBe("Pipeline proposal graph with 5 components and 5 routes");
    expect(projection?.caption).toBe(
      "Proposed pipeline, not yet committed. Confirm the wiring to commit it.",
    );
  });

  it("draws the pending wire stage with the wiring aria name", () => {
    const projection = projectGuidedGraph({
      nextTurn: turn({
        type: "confirm_wiring",
        step_index: 3,
        turn_token: "e".repeat(64),
        payload: wireStage(),
      }),
      reviewed: EMPTY_GUIDED_REVIEWED_COMPONENTS,
    });
    expect(projection?.stage).toBe("wiring");
    expect(projection?.ariaLabel).toBe("Pipeline wiring graph with 4 components and 3 routes");
    expect(projection?.caption).toBe(
      "Proposed wiring under review. Confirm the wiring to commit it.",
    );
  });
});
