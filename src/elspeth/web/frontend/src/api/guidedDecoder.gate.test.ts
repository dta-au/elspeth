// F11 drift guard: the gate-behavior contract is hand-mirrored in THREE
// places — the backend key set (web/composer/guided/protocol.py) and the two
// frontend exact-key lists in guidedDecoder.ts (validateProposalBehavior and
// decodeProposalBehavior). The fixture consumed here is REAL backend output:
// a gate proposal projection produced by build_guided_proposal_projection and
// blessed by verify_guided_proposal_projection (see the generator script noted
// inside the fixture's sibling comment below). If either side drifts, this
// suite goes red before a browser ever sees a rejected turn.
//
// Regenerate the fixture with the scratchpad script
// generate_gate_proposal_fixture.py (run from the worktree root with
// PYTHONPATH=src) whenever the gate projection contract changes.

import { describe, expect, it } from "vitest";

import gateProposalProjection from "./__fixtures__/gateProposalProjection.json";
import { decodeGetGuidedResponse } from "./guidedDecoder";

type Mutable = Record<string, unknown>;

function proposalResponse(payload: unknown): Record<string, unknown> {
  return {
    guided_session: {
      step: "step_3_transforms",
      history: [{
        step: "step_3_transforms",
        turn_type: "propose_pipeline",
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
      type: "propose_pipeline",
      step_index: 2,
      turn_token: "b".repeat(64),
      payload,
    },
    terminal: null,
    composition_state: null,
  };
}

function wireResponse(gateBehavior: unknown): Record<string, unknown> {
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
        nodes: [{
          stable_id: "00000000-0000-4000-8000-000000000021",
          label: "node-1",
          node_type: "gate",
          plugin: null,
          behavior: gateBehavior,
          node_options_summary: [],
          required_fields: [],
          guaranteed_fields: [],
          row_cardinality: { input: "one", output: "one", expected_output_count: null },
          structured_output_fields: [],
        }],
        outputs: [],
        connections: [],
        semantic_contracts: [],
        warnings: [],
        blockers: [],
        can_confirm: true,
      },
    },
    terminal: null,
    composition_state: null,
  };
}

function clonedProjection(): Mutable {
  return structuredClone(gateProposalProjection) as Mutable;
}

function projectionGateBehavior(projection: Mutable): Mutable {
  const nodes = projection.nodes as Array<Mutable>;
  const gate = nodes.find((node) => node.node_type === "gate");
  if (gate === undefined) throw new Error("fixture lost its gate node");
  return gate.behavior as Mutable;
}

describe("F11 gate behavior drift guard (real backend fixture)", () => {
  it("round-trips the backend-produced gate proposal through the propose_pipeline decode path", () => {
    const decoded = decodeGetGuidedResponse(proposalResponse(clonedProjection()));

    expect(decoded.next_turn?.type).toBe("propose_pipeline");
    if (decoded.next_turn?.type !== "propose_pipeline") return;
    const gate = decoded.next_turn.payload.nodes.find((node) => node.node_type === "gate");
    expect(gate).toBeDefined();
    expect(gate?.behavior).toEqual({
      kind: "gate",
      condition: "row['amount'] > 500",
      route_aliases: ["route-1", "route-2"],
      routes: [
        { alias: "route-1", key: "false" },
        { alias: "route-2", key: "true" },
      ],
      fork_branches: [],
    });
  });

  it("round-trips the same backend-produced gate behavior through the confirm_wiring decode path", () => {
    const behavior = projectionGateBehavior(clonedProjection());
    const decoded = decodeGetGuidedResponse(wireResponse(behavior));

    expect(decoded.next_turn?.type).toBe("confirm_wiring");
    if (decoded.next_turn?.type !== "confirm_wiring") return;
    expect(decoded.next_turn.payload.nodes[0].behavior).toEqual({
      kind: "gate",
      condition: "row['amount'] > 500",
      route_aliases: ["route-1", "route-2"],
      routes: [
        { alias: "route-1", key: "false" },
        { alias: "route-2", key: "true" },
      ],
      fork_branches: [],
    });
  });

  const mutations: Array<[string, (behavior: Mutable) => void, string]> = [
    ["missing condition", (behavior) => { delete behavior.condition; }, "condition"],
    ["empty condition", (behavior) => { behavior.condition = "   "; }, "condition"],
    ["missing routes", (behavior) => { delete behavior.routes; }, "routes"],
    [
      "reordered routes",
      (behavior) => { (behavior.routes as unknown[]).reverse(); },
      "one-to-one in the same order",
    ],
    [
      "route alias mismatch",
      (behavior) => { ((behavior.routes as Array<Mutable>)[0] as Mutable).alias = "route-2"; },
      "one-to-one in the same order",
    ],
    [
      "route binding shorter than aliases",
      (behavior) => { (behavior.routes as unknown[]).pop(); },
      "one-to-one in the same order",
    ],
    [
      "empty route key",
      (behavior) => { ((behavior.routes as Array<Mutable>)[0] as Mutable).key = ""; },
      "key",
    ],
  ];

  it.each(mutations)("rejects a gate proposal with %s", (_name, mutate, detail) => {
    const projection = clonedProjection();
    mutate(projectionGateBehavior(projection));

    expect(() => decodeGetGuidedResponse(proposalResponse(projection))).toThrow(detail);
  });

  it.each(mutations)("rejects a wire gate behavior with %s", (_name, mutate, detail) => {
    const behavior = projectionGateBehavior(clonedProjection());
    mutate(behavior);

    expect(() => decodeGetGuidedResponse(wireResponse(behavior))).toThrow(detail);
  });
});
