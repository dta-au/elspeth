import { describe, expect, it } from "vitest";

import { decodeGetGuidedResponse } from "./guidedDecoder";

function wireResponse(payloadOverrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    guided_session: {
      step: "step_4_wire",
      history: [{
        step: "step_4_wire",
        turn_type: "confirm_wiring",
        payload_hash: "a".repeat(64),
        response_hash: null,
        summary: null,
        emitter: "server",
      }],
      terminal: null,
      chat_history: [],
      chat_turn_seq: 0,
      profile: null,
    },
    next_turn: {
      type: "confirm_wiring",
      step_index: 3,
      turn_token: "b".repeat(64),
      payload: {
        proposal_id: "00000000-0000-4000-8000-000000000001",
        draft_hash: "d".repeat(64),
        sources: [],
        nodes: [],
        outputs: [],
        connections: [],
        semantic_contracts: [],
        warnings: [],
        blockers: [],
        can_confirm: true,
        ...payloadOverrides,
      },
    },
    terminal: null,
    composition_state: null,
  };
}

function sourceToOutputWireProjection({
  sourceId = "00000000-0000-4000-8000-000000000020",
  outputId = "00000000-0000-4000-8000-000000000021",
  targetId = outputId,
  connectionId = "00000000-0000-4000-8000-000000000022",
}: {
  sourceId?: string;
  outputId?: string;
  targetId?: string;
  connectionId?: string;
} = {}): Record<string, unknown> {
  return {
    sources: [{
      stable_id: sourceId,
      label: "source",
      plugin: "csv",
      on_validation_failure: "discard",
      guaranteed_fields: [],
      row_cardinality: {
        input: "none",
        output: "one",
        expected_output_count: null,
      },
    }],
    outputs: [{
      stable_id: outputId,
      label: "output",
      plugin: "csv",
      on_write_failure: "discard",
      required_fields: [],
      business_schema: {
        mode: "strict",
        fields: [],
        guaranteed_fields: [],
        required_fields: [],
      },
    }],
    connections: [{
      stable_id: connectionId,
      from_endpoint: { kind: "source", stable_id: sourceId },
      to_endpoint: { kind: "output", stable_id: targetId },
      flow: { kind: "source_success", branch: null },
      schema_contract: null,
    }],
  };
}

function singleSelectWireResponse(): Record<string, unknown> {
  return {
    guided_session: {
      step: "step_1_source",
      history: [{
        step: "step_1_source",
        turn_type: "single_select",
        payload_hash: "a".repeat(64),
        response_hash: null,
        summary: null,
        emitter: "server",
      }],
      terminal: null,
      chat_history: [],
      chat_turn_seq: 0,
      profile: null,
    },
    next_turn: {
      type: "single_select",
      step_index: 0,
      turn_token: "b".repeat(64),
      payload: {
        question: "Which data source would you like to use?",
        options: [
          { id: "csv", label: "CSV", hint: null },
          { id: "api", label: "API", hint: null },
        ],
        allow_custom: false,
        source_blob_compatible_option_ids: ["csv"],
      },
    },
    terminal: null,
    composition_state: null,
  };
}

function aggregationNode(
  behaviorOverrides: Record<string, unknown> = {},
  cardinalityOverrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    stable_id: "00000000-0000-4000-8000-000000000002",
    label: "batch",
    node_type: "aggregation",
    plugin: "batch_stats",
    behavior: {
      kind: "aggregation",
      trigger_kinds: ["count", "timeout"],
      count: "25",
      timeout_seconds: 12.5,
      output_mode: "transform",
      expected_output_count: "1",
      ...behaviorOverrides,
    },
    node_options_summary: [],
    required_fields: [],
    guaranteed_fields: [],
    row_cardinality: {
      input: "batch",
      output: "expected_count",
      expected_output_count: "1",
      ...cardinalityOverrides,
    },
    structured_output_fields: [],
  };
}

function nonRowUnionNodeWithRowUnionCardinality(
  nodeType: "transform" | "gate" | "aggregation" | "queue" | "coalesce",
): Record<string, unknown> {
  if (nodeType === "aggregation") {
    return aggregationNode({}, {
      output: "one_per_branch",
      expected_output_count: null,
    });
  }
  const behaviorByType = {
    transform: { kind: "transform" },
    gate: {
      kind: "gate",
      condition: "row['accepted']",
      route_aliases: ["route-1"],
      routes: [{ alias: "route-1", key: "true" }],
      fork_branches: [],
    },
    queue: { kind: "queue" },
    coalesce: {
      kind: "coalesce",
      branch_aliases: ["branch-1", "branch-2"],
      policy: "require_all",
      merge: "nested",
      timeout_seconds: null,
    },
  } as const;
  return {
    stable_id: "00000000-0000-4000-8000-000000000003",
    label: nodeType,
    node_type: nodeType,
    plugin: nodeType === "transform" ? "passthrough" : null,
    behavior: behaviorByType[nodeType],
    node_options_summary: [],
    required_fields: [],
    guaranteed_fields: [],
    row_cardinality: {
      input: nodeType === "coalesce" ? "branches" : "one",
      output: "one_per_branch",
      expected_output_count: null,
    },
    structured_output_fields: [],
  };
}

function rowUnionProposalWireResponse(): Record<string, unknown> {
  const sourceId = "00000000-0000-4000-8000-000000000401";
  const gateId = "00000000-0000-4000-8000-000000000402";
  const controlId = "00000000-0000-4000-8000-000000000403";
  const treatmentId = "00000000-0000-4000-8000-000000000404";
  const unionId = "00000000-0000-4000-8000-000000000405";
  const aggregateId = "00000000-0000-4000-8000-000000000406";
  const outputId = "00000000-0000-4000-8000-000000000407";
  const edgeIds = Array.from(
    { length: 12 },
    (_, index) =>
      `00000000-0000-4000-8000-${String(500 + index).padStart(12, "0")}`,
  );
  return {
    guided_session: {
      step: "step_3_transforms",
      history: [],
      terminal: null,
      chat_history: [],
      chat_turn_seq: 0,
      profile: null,
    },
    next_turn: {
      type: "propose_pipeline",
      step_index: 2,
      turn_token: "b".repeat(64),
      payload: {
        proposal_id: "00000000-0000-4000-8000-000000000400",
        draft_hash: "d".repeat(64),
        supersedes_draft_hash: null,
        summary: "guided.proposal.summary.full_graph.v1",
        rationale: "guided.proposal.rationale.review_required.v1",
        component_counts: {
          sources: 1,
          nodes: 5,
          edges: 12,
          outputs: 1,
        },
        blockers: [],
        graph: {
          sources: [
            {
              stable_id: sourceId,
              label: "source-1",
              plugin: { kind: "source", id: "csv" },
            },
          ],
          edges: [
            {
              stable_id: edgeIds[0],
              from_endpoint: { kind: "source", stable_id: sourceId },
              to_endpoint: { kind: "node", stable_id: gateId },
              flow: { kind: "source_success", branch: null },
            },
            {
              stable_id: edgeIds[1],
              from_endpoint: { kind: "source", stable_id: sourceId },
              to_endpoint: { kind: "discard" },
              flow: { kind: "source_validation_failure" },
            },
            {
              stable_id: edgeIds[2],
              from_endpoint: { kind: "node", stable_id: gateId },
              to_endpoint: { kind: "node", stable_id: controlId },
              flow: {
                kind: "gate_fork",
                routes: ["route-1"],
                branch: "branch-1",
              },
            },
            {
              stable_id: edgeIds[3],
              from_endpoint: { kind: "node", stable_id: gateId },
              to_endpoint: { kind: "node", stable_id: treatmentId },
              flow: {
                kind: "gate_fork",
                routes: ["route-1"],
                branch: "branch-2",
              },
            },
            {
              stable_id: edgeIds[4],
              from_endpoint: { kind: "node", stable_id: controlId },
              to_endpoint: { kind: "node", stable_id: unionId },
              flow: { kind: "node_success", branch: "branch-1" },
            },
            {
              stable_id: edgeIds[5],
              from_endpoint: { kind: "node", stable_id: treatmentId },
              to_endpoint: { kind: "node", stable_id: unionId },
              flow: { kind: "node_success", branch: "branch-2" },
            },
            {
              stable_id: edgeIds[6],
              from_endpoint: { kind: "node", stable_id: controlId },
              to_endpoint: { kind: "discard" },
              flow: { kind: "node_error" },
            },
            {
              stable_id: edgeIds[7],
              from_endpoint: { kind: "node", stable_id: treatmentId },
              to_endpoint: { kind: "discard" },
              flow: { kind: "node_error" },
            },
            {
              stable_id: edgeIds[8],
              from_endpoint: { kind: "node", stable_id: unionId },
              to_endpoint: { kind: "node", stable_id: aggregateId },
              flow: { kind: "row_union_success", branch: null },
            },
            {
              stable_id: edgeIds[9],
              from_endpoint: { kind: "node", stable_id: aggregateId },
              to_endpoint: { kind: "output", stable_id: outputId },
              flow: { kind: "node_success", branch: null },
            },
            {
              stable_id: edgeIds[10],
              from_endpoint: { kind: "node", stable_id: aggregateId },
              to_endpoint: { kind: "discard" },
              flow: { kind: "node_error" },
            },
            {
              stable_id: edgeIds[11],
              from_endpoint: { kind: "output", stable_id: outputId },
              to_endpoint: { kind: "discard" },
              flow: { kind: "output_write_failure" },
            },
          ],
        },
        nodes: [
          {
            stable_id: gateId,
            label: "node-1",
            node_type: "gate",
            plugin: null,
            behavior: {
              kind: "gate",
              condition: "row['group']",
              route_aliases: ["route-1"],
              routes: [{ alias: "route-1", key: "true" }],
              fork_branches: [
                { routes: ["route-1"], branch: "branch-1" },
                { routes: ["route-1"], branch: "branch-2" },
              ],
            },
            node_options_summary: [],
          },
          {
            stable_id: controlId,
            label: "node-2",
            node_type: "transform",
            plugin: { kind: "transform", id: "passthrough" },
            behavior: { kind: "transform" },
            node_options_summary: [],
          },
          {
            stable_id: treatmentId,
            label: "node-3",
            node_type: "transform",
            plugin: { kind: "transform", id: "passthrough" },
            behavior: { kind: "transform" },
            node_options_summary: [],
          },
          {
            stable_id: unionId,
            label: "node-4",
            node_type: "row_union",
            plugin: null,
            behavior: {
              kind: "row_union",
              branch_aliases: ["branch-1", "branch-2"],
              policy: "require_all",
              timeout_seconds: 12.5,
            },
            node_options_summary: [],
          },
          {
            stable_id: aggregateId,
            label: "node-5",
            node_type: "aggregation",
            plugin: {
              kind: "transform",
              id: "batch_experiment_compare",
            },
            behavior: {
              kind: "aggregation",
              trigger_kinds: [],
              count: null,
              timeout_seconds: null,
              output_mode: "transform",
              expected_output_count: null,
            },
            node_options_summary: [],
          },
        ],
        outputs: [
          {
            stable_id: outputId,
            label: "output-1",
            plugin: { kind: "sink", id: "json" },
          },
        ],
        edit_targets: [],
      },
    },
    terminal: null,
    composition_state: null,
  };
}

function routeRowUnionThroughQueue(
  response: Record<string, unknown>,
): void {
  const payload = (response.next_turn as {
    payload: Record<string, unknown>;
  }).payload;
  const nodes = payload.nodes as Array<Record<string, unknown>>;
  const graph = payload.graph as {
    edges: Array<Record<string, unknown>>;
  };
  const counts = payload.component_counts as Record<string, number>;
  const aggregate = nodes[4];
  const queueId = "00000000-0000-4000-8000-000000000408";
  aggregate.label = "node-6";
  nodes.splice(4, 0, {
    stable_id: queueId,
    label: "node-5",
    node_type: "queue",
    plugin: null,
    behavior: { kind: "queue" },
    node_options_summary: [],
  });
  graph.edges[8].to_endpoint = {
    kind: "node",
    stable_id: queueId,
  };
  graph.edges.splice(9, 0, {
    stable_id: "00000000-0000-4000-8000-000000000512",
    from_endpoint: { kind: "node", stable_id: queueId },
    to_endpoint: { kind: "node", stable_id: aggregate.stable_id },
    flow: { kind: "queue_continue", branch: null },
  });
  counts.nodes += 1;
  counts.edges += 1;
}

function contaminateRowUnionBranchQueue(
  response: Record<string, unknown>,
): void {
  const payload = (response.next_turn as {
    payload: Record<string, unknown>;
  }).payload;
  const nodes = payload.nodes as Array<Record<string, unknown>>;
  const graph = payload.graph as {
    sources: Array<Record<string, unknown>>;
    edges: Array<Record<string, unknown>>;
  };
  const counts = payload.component_counts as Record<string, number>;
  const unionId = nodes[3].stable_id;
  const contaminantSourceId = "00000000-0000-4000-8000-000000000408";
  const queueId = "00000000-0000-4000-8000-000000000409";

  graph.sources.push({
    stable_id: contaminantSourceId,
    label: "source-2",
    plugin: { kind: "source", id: "csv" },
  });
  nodes.splice(3, 0, {
    stable_id: queueId,
    label: "node-4",
    node_type: "queue",
    plugin: null,
    behavior: { kind: "queue" },
    node_options_summary: [],
  });
  nodes.forEach((node, index) => {
    node.label = `node-${index + 1}`;
  });

  graph.edges[4].to_endpoint = {
    kind: "node",
    stable_id: queueId,
  };
  graph.edges[4].flow = {
    kind: "node_success",
    branch: null,
  };
  graph.edges.splice(5, 0, {
    stable_id: "00000000-0000-4000-8000-000000000512",
    from_endpoint: { kind: "node", stable_id: queueId },
    to_endpoint: { kind: "node", stable_id: unionId },
    flow: { kind: "queue_continue", branch: "branch-1" },
  });
  graph.edges.push(
    {
      stable_id: "00000000-0000-4000-8000-000000000513",
      from_endpoint: { kind: "source", stable_id: contaminantSourceId },
      to_endpoint: { kind: "node", stable_id: queueId },
      flow: { kind: "source_success", branch: null },
    },
    {
      stable_id: "00000000-0000-4000-8000-000000000514",
      from_endpoint: { kind: "source", stable_id: contaminantSourceId },
      to_endpoint: { kind: "discard" },
      flow: { kind: "source_validation_failure" },
    },
  );
  counts.sources += 1;
  counts.nodes += 1;
  counts.edges += 3;
}

function coalesceProposalWireResponse(
  policy: "require_all" | "quorum" | "best_effort",
  timeoutSeconds: number | null,
): Record<string, unknown> {
  const response = rowUnionProposalWireResponse();
  const payload = (response.next_turn as {
    payload: Record<string, unknown>;
  }).payload;
  const nodes = payload.nodes as Array<Record<string, unknown>>;
  const edges = (payload.graph as {
    edges: Array<Record<string, unknown>>;
  }).edges;
  nodes[3].node_type = "coalesce";
  nodes[3].behavior = {
    kind: "coalesce",
    branch_aliases: ["branch-1", "branch-2"],
    policy,
    merge: "nested",
    timeout_seconds: timeoutSeconds,
  };
  edges[8].flow = { kind: "coalesce_success", branch: null };
  return response;
}

function nestedForkOuterRowUnionProposalWireResponse(): Record<string, unknown> {
  const response = rowUnionProposalWireResponse();
  const payload = (response.next_turn as {
    payload: Record<string, unknown>;
  }).payload;
  const nodes = payload.nodes as Array<Record<string, unknown>>;
  const graph = payload.graph as {
    edges: Array<Record<string, unknown>>;
  };
  const counts = payload.component_counts as Record<string, number>;
  const outerAProducerId = nodes[1].stable_id;
  const nestedGateId = "00000000-0000-4000-8000-000000000420";
  const innerAId = "00000000-0000-4000-8000-000000000421";
  const innerBId = "00000000-0000-4000-8000-000000000422";
  const innerCoalesceId = "00000000-0000-4000-8000-000000000423";

  // The already branch-specific outer origin is the nested gate, not its
  // descendant child branches. The existing outer branch-1 producer remains
  // after the inner coalesce so its final edge into the outer row_union keeps
  // the outer alias.
  graph.edges[2].to_endpoint = {
    kind: "node",
    stable_id: nestedGateId,
  };
  nodes.push(
    {
      stable_id: nestedGateId,
      label: "node-6",
      node_type: "gate",
      plugin: null,
      behavior: {
        kind: "gate",
        condition: "row['nested']",
        route_aliases: ["route-2"],
        routes: [{ alias: "route-2", key: "true" }],
        fork_branches: [
          { routes: ["route-2"], branch: "branch-3" },
          { routes: ["route-2"], branch: "branch-4" },
        ],
      },
      node_options_summary: [],
    },
    {
      stable_id: innerAId,
      label: "node-7",
      node_type: "transform",
      plugin: { kind: "transform", id: "passthrough" },
      behavior: { kind: "transform" },
      node_options_summary: [],
    },
    {
      stable_id: innerBId,
      label: "node-8",
      node_type: "transform",
      plugin: { kind: "transform", id: "passthrough" },
      behavior: { kind: "transform" },
      node_options_summary: [],
    },
    {
      stable_id: innerCoalesceId,
      label: "node-9",
      node_type: "coalesce",
      plugin: null,
      behavior: {
        kind: "coalesce",
        branch_aliases: ["branch-3", "branch-4"],
        policy: "require_all",
        merge: "union",
        timeout_seconds: null,
      },
      node_options_summary: [],
    },
  );
  const edgeIds = Array.from(
    { length: 7 },
    (_, index) =>
      `00000000-0000-4000-8000-${String(520 + index).padStart(12, "0")}`,
  );
  graph.edges.push(
    {
      stable_id: edgeIds[0],
      from_endpoint: { kind: "node", stable_id: nestedGateId },
      to_endpoint: { kind: "node", stable_id: innerAId },
      flow: {
        kind: "gate_fork",
        routes: ["route-2"],
        branch: "branch-3",
      },
    },
    {
      stable_id: edgeIds[1],
      from_endpoint: { kind: "node", stable_id: nestedGateId },
      to_endpoint: { kind: "node", stable_id: innerBId },
      flow: {
        kind: "gate_fork",
        routes: ["route-2"],
        branch: "branch-4",
      },
    },
    {
      stable_id: edgeIds[2],
      from_endpoint: { kind: "node", stable_id: innerAId },
      to_endpoint: { kind: "node", stable_id: innerCoalesceId },
      flow: { kind: "node_success", branch: "branch-3" },
    },
    {
      stable_id: edgeIds[3],
      from_endpoint: { kind: "node", stable_id: innerBId },
      to_endpoint: { kind: "node", stable_id: innerCoalesceId },
      flow: { kind: "node_success", branch: "branch-4" },
    },
    {
      stable_id: edgeIds[4],
      from_endpoint: { kind: "node", stable_id: innerAId },
      to_endpoint: { kind: "discard" },
      flow: { kind: "node_error" },
    },
    {
      stable_id: edgeIds[5],
      from_endpoint: { kind: "node", stable_id: innerBId },
      to_endpoint: { kind: "discard" },
      flow: { kind: "node_error" },
    },
    {
      stable_id: edgeIds[6],
      from_endpoint: { kind: "node", stable_id: innerCoalesceId },
      to_endpoint: { kind: "node", stable_id: outerAProducerId },
      flow: { kind: "coalesce_success", branch: null },
    },
  );
  counts.nodes += 4;
  counts.edges += 7;
  return response;
}

describe("guided schema-10 wire decoder", () => {
  it("decodes the server-owned source-blob-compatible option set", () => {
    const decoded = decodeGetGuidedResponse(singleSelectWireResponse());

    expect(decoded.next_turn?.type).toBe("single_select");
    if (decoded.next_turn?.type === "single_select") {
      expect(
        decoded.next_turn.payload.source_blob_compatible_option_ids,
      ).toEqual(["csv"]);
    }
  });

  it("decodes a wire turn bound to its pending proposal", () => {
    const decoded = decodeGetGuidedResponse(wireResponse());

    expect(decoded.next_turn?.type).toBe("confirm_wiring");
    if (decoded.next_turn?.type === "confirm_wiring") {
      expect(decoded.next_turn.payload.proposal_id).toBe("00000000-0000-4000-8000-000000000001");
      expect(decoded.next_turn.payload.draft_hash).toBe("d".repeat(64));
    }
  });

  it("decodes the projected key transform options on a wire node", () => {
    // R2-F3 decode seam: node_options_summary is a closed {key, value} pair
    // list, so the decoder must admit it positively — not merely tolerate it.
    const decoded = decodeGetGuidedResponse(
      wireResponse({
        nodes: [
          {
            stable_id: "00000000-0000-4000-8000-000000000009",
            label: "rename",
            node_type: "transform",
            plugin: "field_mapper",
            behavior: { kind: "transform" },
            required_fields: [],
            guaranteed_fields: [],
            row_cardinality: { input: "one", output: "one", expected_output_count: null },
            structured_output_fields: [],
            node_options_summary: [
              { key: "mapping", value: "given_name → first_name" },
              { key: "select_only", value: "only the mapped fields are kept" },
            ],
          },
        ],
      }),
    );

    expect(decoded.next_turn?.type).toBe("confirm_wiring");
    if (decoded.next_turn?.type === "confirm_wiring") {
      expect(decoded.next_turn.payload.nodes[0].node_options_summary).toEqual([
        { key: "mapping", value: "given_name → first_name" },
        { key: "select_only", value: "only the mapped fields are kept" },
      ]);
    }
  });

  it.each([
    ["missing pair value", [{ key: "mapping" }]],
    ["non-string pair value", [{ key: "mapping", value: 7 }]],
    ["extra pair key", [{ key: "mapping", value: "a → b", raw: { a: "b" } }]],
  ])("rejects a wire node options summary with a %s", (_label, summary) => {
    expect(() => decodeGetGuidedResponse(
      wireResponse({
        nodes: [
          {
            stable_id: "00000000-0000-4000-8000-000000000009",
            label: "rename",
            node_type: "transform",
            plugin: "field_mapper",
            behavior: { kind: "transform" },
            required_fields: [],
            guaranteed_fields: [],
            row_cardinality: { input: "one", output: "one", expected_output_count: null },
            structured_output_fields: [],
            node_options_summary: summary,
          },
        ],
      }),
    )).toThrow(/node_options_summary/);
  });

  it.each(["proposal_id", "draft_hash"])("rejects a wire turn missing %s", (missing) => {
    const response = wireResponse();
    const nextTurn = response.next_turn as { payload: Record<string, unknown> };
    delete nextTurn.payload[missing];

    expect(() => decodeGetGuidedResponse(response)).toThrow(missing);
  });

  it.each(["advisor_findings", "signoff_outcome", "passes_remaining"])(
    "rejects removed wire sign-off field %s",
    (field) => {
      expect(() => decodeGetGuidedResponse(wireResponse({ [field]: field === "passes_remaining" ? 1 : "legacy" })))
        .toThrow(`unexpected ${field}`);
    },
  );

  it.each([
    [["count", "unknown"], "unknown trigger"],
    [["count", "count"], "duplicate trigger"],
  ])("rejects %s aggregation trigger kinds", (triggerKinds) => {
    expect(() => decodeGetGuidedResponse(wireResponse({
      nodes: [aggregationNode({ trigger_kinds: triggerKinds })],
    }))).toThrow("trigger_kinds");
  });

  it.each([
    ["count trigger without count", { trigger_kinds: ["count", "timeout"], count: null }, "count"],
    ["count without count trigger", { trigger_kinds: ["timeout"], count: "25" }, "count"],
    ["timeout trigger without timeout", { trigger_kinds: ["count", "timeout"], timeout_seconds: null }, "timeout_seconds"],
    ["timeout without timeout trigger", { trigger_kinds: ["count"], timeout_seconds: 12.5 }, "timeout_seconds"],
  ])("rejects aggregation %s", (_case, behaviorOverrides, field) => {
    expect(() => decodeGetGuidedResponse(wireResponse({
      nodes: [aggregationNode(behaviorOverrides)],
    }))).toThrow(field);
  });

  it.each(["01", "+1", "1.0"])("rejects noncanonical cardinality count %s", (count) => {
    expect(() => decodeGetGuidedResponse(wireResponse({
      nodes: [aggregationNode({}, { expected_output_count: count })],
    }))).toThrow("expected_output_count");
  });

  it("rejects cardinality output/count coupling violations", () => {
    expect(() => decodeGetGuidedResponse(wireResponse({
      nodes: [aggregationNode({}, { output: "expected_count", expected_output_count: null })],
    }))).toThrow("expected_output_count");
    expect(() => decodeGetGuidedResponse(wireResponse({
      nodes: [aggregationNode({}, { output: "one", expected_output_count: "1" })],
    }))).toThrow("expected_output_count");
  });

  it("rejects one_per_branch cardinality on sources", () => {
    expect(() => decodeGetGuidedResponse(wireResponse({
      sources: [{
        stable_id: "00000000-0000-4000-8000-000000000004",
        label: "source",
        plugin: "csv",
        on_validation_failure: "discard",
        guaranteed_fields: [],
        row_cardinality: {
          input: "none",
          output: "one_per_branch",
          expected_output_count: null,
        },
      }],
    }))).toThrow(/one_per_branch|row_union|cardinality/i);
  });

  it.each([
    "transform",
    "gate",
    "aggregation",
    "queue",
    "coalesce",
  ] as const)("rejects one_per_branch cardinality on %s nodes", (nodeType) => {
    expect(() => decodeGetGuidedResponse(wireResponse({
      nodes: [nonRowUnionNodeWithRowUnionCardinality(nodeType)],
    }))).toThrow(/one_per_branch|row_union|cardinality/i);
  });

  it("decodes the exact row_union behavior and distinct row_union_success flow", () => {
    const decoded = decodeGetGuidedResponse(rowUnionProposalWireResponse());

    expect(decoded.next_turn?.type).toBe("propose_pipeline");
    if (decoded.next_turn?.type !== "propose_pipeline") {
      throw new Error("row_union fixture did not decode as a proposal");
    }
    expect(decoded.next_turn.payload.nodes[3]).toMatchObject({
      node_type: "row_union",
      behavior: {
        kind: "row_union",
        branch_aliases: ["branch-1", "branch-2"],
        policy: "require_all",
        timeout_seconds: 12.5,
      },
      node_options_summary: [],
    });
    expect(decoded.next_turn.payload.graph.edges[8].flow).toEqual({
      kind: "row_union_success",
      branch: null,
    });
  });

  it.each([
    ["best_effort", 12.5],
    ["quorum", 30],
    ["require_all", null],
  ] as const)(
    "decodes %s coalesce with its material timeout",
    (policy, timeoutSeconds) => {
      const decoded = decodeGetGuidedResponse(
        coalesceProposalWireResponse(policy, timeoutSeconds),
      );

      expect(decoded.next_turn?.type).toBe("propose_pipeline");
      if (decoded.next_turn?.type !== "propose_pipeline") {
        throw new Error("coalesce fixture did not decode as a proposal");
      }
      expect(decoded.next_turn.payload.nodes[3]).toMatchObject({
        node_type: "coalesce",
        behavior: {
          kind: "coalesce",
          branch_aliases: ["branch-1", "branch-2"],
          policy,
          merge: "nested",
          timeout_seconds: timeoutSeconds,
        },
        node_options_summary: [],
      });
    },
  );

  it("rejects a lossy coalesce behavior without the required timeout key", () => {
    const response = coalesceProposalWireResponse("require_all", null);
    const payload = (response.next_turn as {
      payload: Record<string, unknown>;
    }).payload;
    const nodes = payload.nodes as Array<Record<string, unknown>>;
    delete (nodes[3].behavior as Record<string, unknown>).timeout_seconds;

    expect(() => decodeGetGuidedResponse(response)).toThrow("timeout_seconds");
  });

  it.each([true, 0, Number.POSITIVE_INFINITY])(
    "rejects invalid coalesce timeout %s",
    (timeoutSeconds) => {
      expect(() => decodeGetGuidedResponse(
        coalesceProposalWireResponse("quorum", timeoutSeconds as number),
      )).toThrow("timeout_seconds");
    },
  );

  it("accepts a nested descendant fork inside one outer row_union arm", () => {
    const decoded = decodeGetGuidedResponse(
      nestedForkOuterRowUnionProposalWireResponse(),
    );

    expect(decoded.next_turn?.type).toBe("propose_pipeline");
    if (decoded.next_turn?.type !== "propose_pipeline") {
      throw new Error("nested fork fixture did not decode as a proposal");
    }
    expect(
      decoded.next_turn.payload.nodes.filter(
        (node) => node.node_type === "gate",
      ),
    ).toHaveLength(2);
    expect(
      decoded.next_turn.payload.nodes.some(
        (node) => node.node_type === "coalesce",
      ),
    ).toBe(true);
  });

  it("rejects outer row_union branch aliases sourced from a sibling arm", () => {
    const response = nestedForkOuterRowUnionProposalWireResponse();
    const payload = (response.next_turn as {
      payload: Record<string, unknown>;
    }).payload;
    const nodes = payload.nodes as Array<Record<string, unknown>>;
    const outerUnionId = nodes[3].stable_id;
    const edges = (payload.graph as {
      edges: Array<Record<string, unknown>>;
    }).edges;
    const incoming = edges.filter((edge) =>
      (edge.to_endpoint as Record<string, unknown>).stable_id === outerUnionId
    );
    [incoming[0].from_endpoint, incoming[1].from_endpoint] = [
      incoming[1].from_endpoint,
      incoming[0].from_endpoint,
    ];

    expect(() => decodeGetGuidedResponse(response)).toThrow(
      /downstream|not connected/i,
    );
  });

  it("decodes row_union_success targeting a queue before an ordinary processing node", () => {
    const response = rowUnionProposalWireResponse();
    routeRowUnionThroughQueue(response);

    const decoded = decodeGetGuidedResponse(response);

    expect(decoded.next_turn?.type).toBe("propose_pipeline");
    if (decoded.next_turn?.type !== "propose_pipeline") {
      throw new Error("row_union queue fixture did not decode as a proposal");
    }
    expect(decoded.next_turn.payload.nodes[4]).toMatchObject({
      node_type: "queue",
      behavior: { kind: "queue" },
      node_options_summary: [],
    });
    expect(decoded.next_turn.payload.graph.edges.slice(8, 10).map(
      (edge) => edge.flow.kind,
    )).toEqual(["row_union_success", "queue_continue"]);
  });

  it("rejects a row_union queue branch with an unrelated source predecessor", () => {
    const response = rowUnionProposalWireResponse();
    contaminateRowUnionBranchQueue(response);

    expect(() => decodeGetGuidedResponse(response)).toThrow(/downstream/i);
  });

  it.each([
    ["one branch", { branch_aliases: ["branch-1"] }],
    ["duplicate branches", { branch_aliases: ["branch-1", "branch-1"] }],
    ["non-require-all policy", { policy: "quorum" }],
    ["boolean timeout", { timeout_seconds: true }],
    ["zero timeout", { timeout_seconds: 0 }],
    ["infinite timeout", { timeout_seconds: Number.POSITIVE_INFINITY }],
    ["coalesce field", { merge: "union" }],
  ])("rejects row_union behavior with %s", (_label, override) => {
    const response = rowUnionProposalWireResponse();
    const payload = (response.next_turn as { payload: Record<string, unknown> })
      .payload;
    const nodes = payload.nodes as Array<Record<string, unknown>>;
    nodes[3].behavior = {
      ...(nodes[3].behavior as Record<string, unknown>),
      ...override,
    };

    expect(() => decodeGetGuidedResponse(response)).toThrow(/behavior/);
  });

  it("rejects row_union_success targeting an output instead of one processing or queue node", () => {
    const response = rowUnionProposalWireResponse();
    const payload = (response.next_turn as { payload: Record<string, unknown> })
      .payload;
    const edges = (payload.graph as { edges: Array<Record<string, unknown>> })
      .edges;
    const outputs = payload.outputs as Array<Record<string, unknown>>;
    edges[8].to_endpoint = {
      kind: "output",
      stable_id: outputs[0].stable_id,
    };

    expect(() => decodeGetGuidedResponse(response)).toThrow(
      /row_union_success|target/i,
    );
  });

  it.each(["coalesce", "row_union"] as const)(
    "rejects row_union_success targeting a downstream %s barrier",
    (barrierType) => {
      const response = rowUnionProposalWireResponse();
      const payload = (response.next_turn as {
        payload: Record<string, unknown>;
      }).payload;
      const nodes = payload.nodes as Array<Record<string, unknown>>;
      const edges = (payload.graph as {
        edges: Array<Record<string, unknown>>;
      }).edges;
      const counts = payload.component_counts as Record<string, number>;
      edges[8].flow = {
        kind: "row_union_success",
        branch: "branch-1",
      };
      nodes[4].node_type = barrierType;
      nodes[4].plugin = null;
      nodes[4].behavior = barrierType === "coalesce"
        ? {
            kind: "coalesce",
            branch_aliases: ["branch-1", "branch-2"],
            policy: "require_all",
            merge: "nested",
            timeout_seconds: null,
          }
        : {
            kind: "row_union",
            branch_aliases: ["branch-1", "branch-2"],
            policy: "require_all",
            timeout_seconds: null,
          };
      edges[9].to_endpoint = {
        kind: "node",
        stable_id: nodes[1].stable_id,
      };
      edges[9].flow = {
        kind: `${barrierType}_success`,
        branch: null,
      };
      edges.splice(10, 1);
      counts.edges -= 1;

      expect(() => decodeGetGuidedResponse(response)).toThrow(
        /row_union success must target one ordinary processing or queue node/i,
      );
    },
  );

  it("rejects row_union branches that originate under different gate forks", () => {
    const response = rowUnionProposalWireResponse();
    const payload = (response.next_turn as { payload: Record<string, unknown> })
      .payload;
    const nodes = payload.nodes as Array<Record<string, unknown>>;
    const graph = payload.graph as { edges: Array<Record<string, unknown>> };
    const unionId = nodes[3].stable_id;
    const treatmentId = nodes[2].stable_id;
    const secondGateId = "00000000-0000-4000-8000-000000000408";
    nodes.push({
      stable_id: secondGateId,
      label: "node-6",
      node_type: "gate",
      plugin: null,
      behavior: {
        kind: "gate",
        condition: "row['variant']",
        route_aliases: ["route-2"],
        routes: [{ alias: "route-2", key: "treatment" }],
        fork_branches: [
          { routes: ["route-2"], branch: "branch-2" },
        ],
      },
      node_options_summary: [],
    });
    // The treatment remains downstream of the first gate's branch-2 fork,
    // then a second gate re-forks the same alias before row_union.
    graph.edges[5].to_endpoint = {
      kind: "node",
      stable_id: secondGateId,
    };
    graph.edges.push({
      stable_id: "00000000-0000-4000-8000-000000000599",
      from_endpoint: { kind: "node", stable_id: secondGateId },
      to_endpoint: { kind: "node", stable_id: unionId },
      flow: {
        kind: "gate_fork",
        routes: ["route-2"],
        branch: "branch-2",
      },
    });
    // Pin that the rewritten edge is still the treatment's one success edge.
    expect(
      (graph.edges[5].from_endpoint as Record<string, unknown>).stable_id,
    ).toBe(treatmentId);
    const counts = payload.component_counts as Record<string, number>;
    counts.nodes += 1;
    counts.edges += 1;

    expect(() => decodeGetGuidedResponse(response)).toThrow(/one gate_fork/i);
  });

  it("rejects a row_union branch producer outside its authoritative branch", () => {
    const response = rowUnionProposalWireResponse();
    const payload = (response.next_turn as { payload: Record<string, unknown> })
      .payload;
    const edges = (payload.graph as { edges: Array<Record<string, unknown>> })
      .edges;
    const firstProducer = edges[4].from_endpoint;
    edges[4].from_endpoint = edges[5].from_endpoint;
    edges[5].from_endpoint = firstProducer;

    expect(() => decodeGetGuidedResponse(response)).toThrow(
      /downstream|fork origin/i,
    );
  });

  it("decodes a direct gate fork whose row_union releases against fork order", () => {
    const response = rowUnionProposalWireResponse();
    const payload = (response.next_turn as { payload: Record<string, unknown> })
      .payload;
    const nodes = payload.nodes as Array<Record<string, unknown>>;
    const counts = payload.component_counts as Record<string, number>;
    const unionId = (nodes[3] as { stable_id: string }).stable_id;
    const producerIds = new Set([
      (nodes[1] as { stable_id: string }).stable_id,
      (nodes[2] as { stable_id: string }).stable_id,
    ]);
    nodes.splice(1, 2);
    nodes.forEach((node, index) => {
      node.label = `node-${index + 1}`;
    });
    const graph = payload.graph as { edges: Array<Record<string, unknown>> };
    const kept = graph.edges.filter(
      (edge) => !producerIds.has(
        (edge.from_endpoint as { stable_id?: string }).stable_id ?? "",
      ),
    );
    for (const edge of kept) {
      if ((edge.flow as { kind: string }).kind === "gate_fork") {
        edge.to_endpoint = { kind: "node", stable_id: unionId };
      }
    }
    const forkPositions = kept.flatMap(
      (edge, index) => (edge.flow as { kind: string }).kind === "gate_fork" ? [index] : [],
    );
    const [first, second] = forkPositions;
    [kept[first], kept[second]] = [kept[second], kept[first]];
    graph.edges = kept;
    // The gate keeps its authored fork order; the row_union releases in reverse.
    (nodes[1].behavior as { branch_aliases: string[] }).branch_aliases = forkPositions.map(
      (index) => (kept[index].flow as { branch: string }).branch,
    );
    counts.nodes = nodes.length;
    counts.edges = kept.length;

    const decoded = decodeGetGuidedResponse(response);

    expect(decoded.next_turn?.type).toBe("propose_pipeline");
    if (decoded.next_turn?.type !== "propose_pipeline") {
      throw new Error("direct fork fixture did not decode as a proposal");
    }
    expect(decoded.next_turn.payload.nodes[1].behavior).toMatchObject({
      branch_aliases: ["branch-2", "branch-1"],
    });
    expect(decoded.next_turn.payload.nodes[0].behavior).toMatchObject({
      fork_branches: [
        { routes: ["route-1"], branch: "branch-1" },
        { routes: ["route-1"], branch: "branch-2" },
      ],
    });
  });

  it("decodes multi-stage branch arms whose interior hop carries no branch alias", () => {
    const response = rowUnionProposalWireResponse();
    const payload = (response.next_turn as { payload: Record<string, unknown> })
      .payload;
    const nodes = payload.nodes as Array<Record<string, unknown>>;
    const edges = (payload.graph as { edges: Array<Record<string, unknown>> })
      .edges;
    const counts = payload.component_counts as Record<string, number>;
    const unionId = (nodes[3] as { stable_id: string }).stable_id;
    [4, 5].forEach((edgeIndex, offset) => {
      const stageId = `00000000-0000-4000-8000-${String(409 + offset).padStart(12, "0")}`;
      const branch = (edges[edgeIndex].flow as { branch: string }).branch;
      nodes.push({
        stable_id: stageId,
        label: `node-${nodes.length + 1}`,
        node_type: "transform",
        plugin: { kind: "transform", id: "passthrough" },
        behavior: { kind: "transform" },
        node_options_summary: [],
      });
      edges[edgeIndex].to_endpoint = { kind: "node", stable_id: stageId };
      (edges[edgeIndex].flow as { branch: string | null }).branch = null;
      edges.push({
        stable_id: `00000000-0000-4000-8000-${String(520 + offset * 2).padStart(12, "0")}`,
        from_endpoint: { kind: "node", stable_id: stageId },
        to_endpoint: { kind: "node", stable_id: unionId },
        flow: { kind: "node_success", branch },
      });
      edges.push({
        stable_id: `00000000-0000-4000-8000-${String(521 + offset * 2).padStart(12, "0")}`,
        from_endpoint: { kind: "node", stable_id: stageId },
        to_endpoint: { kind: "discard" },
        flow: { kind: "node_error" },
      });
    });
    counts.nodes = nodes.length;
    counts.edges = edges.length;

    const decoded = decodeGetGuidedResponse(response);

    expect(decoded.next_turn?.type).toBe("propose_pipeline");
  });

  it("decodes row_union N-to-N wire cardinality", () => {
    const decoded = decodeGetGuidedResponse(
      wireResponse({
        nodes: [
          {
            stable_id: "00000000-0000-4000-8000-000000000009",
            label: "row union",
            node_type: "row_union",
            plugin: null,
            behavior: {
              kind: "row_union",
              branch_aliases: ["branch-1", "branch-2"],
              policy: "require_all",
              timeout_seconds: null,
            },
            node_options_summary: [],
            required_fields: [],
            guaranteed_fields: [],
            row_cardinality: {
              input: "branches",
              output: "one_per_branch",
              expected_output_count: null,
            },
            structured_output_fields: [],
          },
        ],
      }),
    );

    expect(decoded.next_turn?.type).toBe("confirm_wiring");
    if (decoded.next_turn?.type === "confirm_wiring") {
      expect(decoded.next_turn.payload.nodes[0].row_cardinality).toEqual({
        input: "branches",
        output: "one_per_branch",
        expected_output_count: null,
      });
    }
  });

  it("rejects row_union_success wiring that targets an output", () => {
    expect(() => decodeGetGuidedResponse(
      wireResponse({
        nodes: [
          {
            stable_id: "00000000-0000-4000-8000-000000000009",
            label: "row union",
            node_type: "row_union",
            plugin: null,
            behavior: {
              kind: "row_union",
              branch_aliases: ["branch-1", "branch-2"],
              policy: "require_all",
              timeout_seconds: null,
            },
            node_options_summary: [],
            required_fields: [],
            guaranteed_fields: [],
            row_cardinality: {
              input: "branches",
              output: "one_per_branch",
              expected_output_count: null,
            },
            structured_output_fields: [],
          },
        ],
        outputs: [
          {
            stable_id: "00000000-0000-4000-8000-000000000010",
            label: "output",
            plugin: "csv",
            on_write_failure: "discard",
            required_fields: [],
            business_schema: {
              mode: "strict",
              fields: [],
              guaranteed_fields: [],
              required_fields: [],
            },
          },
        ],
        connections: [
          {
            stable_id: "00000000-0000-4000-8000-000000000011",
            from_endpoint: {
              kind: "node",
              stable_id: "00000000-0000-4000-8000-000000000009",
            },
            to_endpoint: {
              kind: "output",
              stable_id: "00000000-0000-4000-8000-000000000010",
            },
            flow: { kind: "row_union_success", branch: null },
            schema_contract: null,
          },
        ],
      }),
    )).toThrow(/row_union_success|target endpoint|ordinary processing/i);
  });

  it("rejects wire connections whose endpoint stable ID does not resolve", () => {
    expect(() => decodeGetGuidedResponse(wireResponse(
      sourceToOutputWireProjection({
        targetId: "00000000-0000-4000-8000-000000000099",
      }),
    ))).toThrow(/to_endpoint|resolve/i);
  });

  it("rejects wire connection stable IDs that collide with components", () => {
    const sourceId = "00000000-0000-4000-8000-000000000020";
    expect(() => decodeGetGuidedResponse(wireResponse(
      sourceToOutputWireProjection({
        sourceId,
        connectionId: sourceId,
      }),
    ))).toThrow(/stable IDs|unique/i);
  });

  it.each([
    { input: "one", output: "one_per_branch", expected_output_count: null },
    { input: "branches", output: "one_per_branch_set", expected_output_count: null },
    { input: "branches", output: "one_per_branch", expected_output_count: "2" },
  ])("rejects dishonest row_union wire cardinality %j", (rowCardinality) => {
    expect(() => decodeGetGuidedResponse(
      wireResponse({
        nodes: [
          {
            stable_id: "00000000-0000-4000-8000-000000000009",
            label: "row union",
            node_type: "row_union",
            plugin: null,
            behavior: {
              kind: "row_union",
              branch_aliases: ["branch-1", "branch-2"],
              policy: "require_all",
              timeout_seconds: null,
            },
            node_options_summary: [],
            required_fields: [],
            guaranteed_fields: [],
            row_cardinality: rowCardinality,
            structured_output_fields: [],
          },
        ],
      }),
    )).toThrow(/cardinality|row_union/i);
  });

  it("decodes row_union timeout_seconds on a current composition state", () => {
    const response = singleSelectWireResponse();
    response.composition_state = {
      id: "state-1",
      session_id: "session-1",
      version: 1,
      sources: {},
      nodes: [
        {
          id: "variant_union",
          node_type: "row_union",
          plugin: null,
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
      ],
      edges: [],
      outputs: [],
      metadata: { name: null, description: null },
      is_valid: true,
      validation_errors: null,
      validation_warnings: null,
      validation_suggestions: null,
      derived_from_state_id: null,
      created_at: "2026-07-31T00:00:00Z",
      composer_meta: null,
      plugin_policy_findings: [],
    };

    const decoded = decodeGetGuidedResponse(response);

    expect(decoded.composition_state?.nodes[0]).toMatchObject({
      node_type: "row_union",
      timeout_seconds: 12.5,
    });
  });
});

describe("guided collector proposals (WS6 lift, ruling 7878)", () => {
  function collectorProposalWireResponse(
    mutate: (payload: Record<string, unknown>) => void = () => {},
  ): Record<string, unknown> {
    const sourceId = "00000000-0000-4000-8000-000000000601";
    const explodeId = "00000000-0000-4000-8000-000000000602";
    const collectorId = "00000000-0000-4000-8000-000000000603";
    const outputId = "00000000-0000-4000-8000-000000000604";
    const edgeIds = Array.from(
      { length: 6 },
      (_, index) => `00000000-0000-4000-8000-${String(610 + index).padStart(12, "0")}`,
    );
    const payload: Record<string, unknown> = {
      proposal_id: "00000000-0000-4000-8000-000000000600",
      draft_hash: "d".repeat(64),
      supersedes_draft_hash: null,
      summary: "guided.proposal.summary.full_graph.v1",
      rationale: "guided.proposal.rationale.review_required.v1",
      component_counts: { sources: 1, nodes: 2, edges: 6, outputs: 1 },
      blockers: [],
      graph: {
        sources: [
          { stable_id: sourceId, label: "source-1", plugin: { kind: "source", id: "csv" } },
        ],
        edges: [
          {
            stable_id: edgeIds[0],
            from_endpoint: { kind: "source", stable_id: sourceId },
            to_endpoint: { kind: "node", stable_id: explodeId },
            flow: { kind: "source_success", branch: null },
          },
          {
            stable_id: edgeIds[1],
            from_endpoint: { kind: "source", stable_id: sourceId },
            to_endpoint: { kind: "discard" },
            flow: { kind: "source_validation_failure" },
          },
          {
            stable_id: edgeIds[2],
            from_endpoint: { kind: "node", stable_id: explodeId },
            to_endpoint: { kind: "node", stable_id: collectorId },
            flow: { kind: "node_success", branch: null },
          },
          {
            stable_id: edgeIds[3],
            from_endpoint: { kind: "node", stable_id: explodeId },
            to_endpoint: { kind: "discard" },
            flow: { kind: "node_error" },
          },
          {
            stable_id: edgeIds[4],
            from_endpoint: { kind: "node", stable_id: collectorId },
            to_endpoint: { kind: "output", stable_id: outputId },
            flow: { kind: "node_success", branch: null },
          },
          {
            stable_id: edgeIds[5],
            from_endpoint: { kind: "output", stable_id: outputId },
            to_endpoint: { kind: "discard" },
            flow: { kind: "output_write_failure" },
          },
        ],
      },
      nodes: [
        {
          stable_id: explodeId,
          label: "node-1",
          node_type: "transform",
          plugin: { kind: "transform", id: "json_explode" },
          behavior: { kind: "transform" },
          node_options_summary: [],
        },
        {
          stable_id: collectorId,
          label: "node-2",
          node_type: "collector",
          plugin: { kind: "transform", id: "batch_stats" },
          behavior: {
            kind: "collector",
            opener_stable_id: explodeId,
            policy: "require_all",
          },
          node_options_summary: [],
        },
      ],
      outputs: [
        { stable_id: outputId, label: "output-1", plugin: { kind: "sink", id: "json" } },
      ],
      edit_targets: [
        { kind: "node", stable_id: explodeId },
        { kind: "node", stable_id: collectorId },
      ],
    };
    mutate(payload);
    return {
      guided_session: {
        step: "step_3_transforms",
        history: [],
        terminal: null,
        chat_history: [],
        chat_turn_seq: 0,
        profile: null,
      },
      next_turn: {
        type: "propose_pipeline",
        step_index: 2,
        turn_token: "b".repeat(64),
        payload,
      },
      terminal: null,
      composition_state: null,
    };
  }

  it("decodes the collector behavior with its opener identity and closed policy", () => {
    // No collector node_error edge in the fixture: on_error is OPTIONAL on a
    // collector (group failure is structural), so this decode succeeding is
    // itself the optional-error pin.
    const decoded = decodeGetGuidedResponse(collectorProposalWireResponse());

    expect(decoded.next_turn?.type).toBe("propose_pipeline");
    if (decoded.next_turn?.type !== "propose_pipeline") {
      throw new Error("collector fixture did not decode as a proposal");
    }
    expect(decoded.next_turn.payload.nodes[1]).toMatchObject({
      node_type: "collector",
      plugin: { kind: "transform", id: "batch_stats" },
      behavior: {
        kind: "collector",
        opener_stable_id: "00000000-0000-4000-8000-000000000602",
        policy: "require_all",
      },
    });
  });

  it("rejects a collector whose opener does not name a payload node", () => {
    expect(() => decodeGetGuidedResponse(collectorProposalWireResponse((payload) => {
      const nodes = payload.nodes as Array<Record<string, unknown>>;
      (nodes[1].behavior as Record<string, unknown>).opener_stable_id = "00000000-0000-4000-8000-0000000006ff";
    }))).toThrow(/opener/i);
  });

  it("rejects a collector policy outside the closed vocabulary", () => {
    expect(() => decodeGetGuidedResponse(collectorProposalWireResponse((payload) => {
      const nodes = payload.nodes as Array<Record<string, unknown>>;
      (nodes[1].behavior as Record<string, unknown>).policy = "quorum";
    }))).toThrow(/policy/i);
  });

  it("rejects a collector without a transform plugin ref", () => {
    expect(() => decodeGetGuidedResponse(collectorProposalWireResponse((payload) => {
      const nodes = payload.nodes as Array<Record<string, unknown>>;
      nodes[1].plugin = null;
    }))).toThrow(/plugin/i);
  });

  it("rejects a second collector success flow", () => {
    expect(() => decodeGetGuidedResponse(collectorProposalWireResponse((payload) => {
      const graph = payload.graph as { edges: Array<Record<string, unknown>> };
      graph.edges.push({
        stable_id: "00000000-0000-4000-8000-000000000620",
        from_endpoint: { kind: "node", stable_id: "00000000-0000-4000-8000-000000000603" },
        to_endpoint: { kind: "output", stable_id: "00000000-0000-4000-8000-000000000604" },
        flow: { kind: "node_success", branch: null },
      });
      (payload.component_counts as Record<string, number>).edges = 7;
    }))).toThrow(/success flow/i);
  });
});
