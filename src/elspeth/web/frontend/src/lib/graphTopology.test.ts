import { describe, expect, it } from "vitest";

import {
  branchEntries,
  buildConnectionProducers,
  publishedSuccessConnection,
  COALESCE_MERGES,
  COALESCE_POLICIES,
  DISCARD_CONNECTION,
  FAN_IN_NODE_TYPES,
  FORK_CONNECTION,
  IMPLICIT_SELF_PUBLISHING_NODE_TYPES,
} from "./graphTopology";
import { buildProducerRegistry } from "@/components/inspector/GraphView";
import { makeComposition } from "@/test/composerFixtures";

describe("publishedSuccessConnection", () => {
  it("prefers an explicit on_success over the implicit self-published id", () => {
    expect(
      publishedSuccessConnection({ id: "merge", node_type: "coalesce", on_success: "tidy_output" }),
    ).toBe("tidy_output");
  });

  it("publishes under the node's own id for queue, coalesce and aggregation with no on_success", () => {
    // Mirrors _producer_resolver.published_success_connection. Re-deriving
    // this from on_success alone drew a working fork/coalesce pipeline as two
    // disconnected fragments (session 3f02c8fa).
    for (const node_type of ["queue", "coalesce", "aggregation"]) {
      expect(publishedSuccessConnection({ id: "n1", node_type, on_success: null })).toBe("n1");
    }
    expect(IMPLICIT_SELF_PUBLISHING_NODE_TYPES.has("row_union")).toBe(false);
    expect(IMPLICIT_SELF_PUBLISHING_NODE_TYPES.has("collector")).toBe(false);
  });

  it("publishes nothing for a kind that requires on_success and declares none", () => {
    expect(publishedSuccessConnection({ id: "u1", node_type: "row_union", on_success: null })).toBeNull();
    expect(publishedSuccessConnection({ id: "t1", node_type: "transform", on_success: null })).toBeNull();
  });
});

describe("branchEntries", () => {
  it("reads a map verbatim and expands a list to the identity mapping", () => {
    // A coalesce reaches the frontend still holding a LIST: the composer
    // normalises list -> identity only for row_union (_serialize_branches
    // preserves list-vs-mapping for a coalesce). Declining the list shape
    // reproduced elspeth-625e85c59b on a composition that validates green.
    expect(branchEntries({ branch_a: "pairing_done", branch_b: "hex_done" })).toEqual([
      ["branch_a", "pairing_done"],
      ["branch_b", "hex_done"],
    ]);
    expect(branchEntries(["a", "b"])).toEqual([
      ["a", "a"],
      ["b", "b"],
    ]);
    expect(branchEntries(null)).toEqual([]);
    expect(branchEntries(undefined)).toEqual([]);
  });

  it("covers both fan-in kinds", () => {
    expect([...FAN_IN_NODE_TYPES].sort()).toEqual(["coalesce", "row_union"]);
  });
});

describe("buildConnectionProducers", () => {
  it("registers a source's on_success, a node's published connection, on_error and routes", () => {
    const state = makeComposition(1, {
      sources: { intake: { plugin: "csv", options: {}, on_success: "raw_rows" } },
      nodes: [
        { id: "classify", node_type: "transform", plugin: "llm", options: {},
          input: "raw_rows", on_success: "scored", on_error: "review_queue" },
        { id: "route", node_type: "gate", plugin: null, options: {},
          input: "scored", on_success: null, on_error: null, routes: { pass: "kept", fail: "dropped" } },
      ],
      // `kept` is consumed by an output NAMED for it — OutputSpec has no
      // `input` field, and an output consumes the connection equal to its
      // own name.
      outputs: [{ name: "kept", plugin: "csv", options: {} }],
    });
    const producers = buildConnectionProducers(state);
    // A source publishes under its COMPONENT id, not its bare key — see the
    // id-vocabulary paragraph on buildConnectionProducers. `intake` is not the
    // default source, so its component id is `source:intake`.
    expect(producers.get("raw_rows")).toEqual(["source:intake"]);
    expect(producers.get("scored")).toEqual(["classify"]);
    expect(producers.get("review_queue")).toEqual(["classify"]);
    expect(producers.get("kept")).toEqual(["route"]);
    expect(producers.get("dropped")).toEqual(["route"]);
  });

  it("is a MULTIMAP — several producers on one connection all survive (ADR-028 fan-in)", () => {
    // GraphView.tsx: overwriting would silently drop every producer but the
    // last and misrender the intentional fan-in.
    const state = makeComposition(1, {
      sources: { a: { plugin: "csv", options: {}, on_success: "pooled" } },
      nodes: [
        { id: "b", node_type: "transform", plugin: "llm", options: {},
          input: "seed", on_success: "pooled", on_error: null },
        { id: "hold", node_type: "queue", plugin: null, options: {},
          input: "pooled", on_success: null, on_error: null },
      ],
      outputs: [],
    });
    expect(buildConnectionProducers(state).get("pooled")?.sort()).toEqual(["b", "source:a"]);
  });

  it("publishes a queue under its own id and does NOT register the 'fork' route sentinel", () => {
    const state = makeComposition(1, {
      sources: {},
      nodes: [
        { id: "hold", node_type: "queue", plugin: null, options: {},
          input: "pooled", on_success: null, on_error: null },
        { id: "split", node_type: "gate", plugin: null, options: {}, input: "hold",
          on_success: null, on_error: null, routes: { every: "fork" }, fork_to: ["arm_a", "arm_b"] },
      ],
      outputs: [],
    });
    const producers = buildConnectionProducers(state);
    expect(producers.get("hold")).toEqual(["hold"]);
    expect(producers.get("arm_a")).toEqual(["split"]);
    expect(producers.get("arm_b")).toEqual(["split"]);
    // "fork" is a sentinel, not a connection name. GraphView registers it and
    // never looks it up; this reduced view skips it so it cannot surface in
    // the Spec tab as a resolvable connection (see the producer-registry ruling).
    expect(producers.has("fork")).toBe(false);
  });

  it("never registers the discard sentinel as a connection", () => {
    // _producer_resolver.py:208 — discard is not a connection. A shared index
    // holding it as a key would let some component's `input: discard` resolve
    // to a producer.
    const state = makeComposition(1, {
      sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows", on_validation_failure: "discard" } },
      nodes: [{ id: "score", node_type: "transform", plugin: "llm", options: {},
                input: "raw_rows", on_success: "final_out", on_error: "discard" }],
      outputs: [{ name: "final_out", plugin: "csv", options: {} }],
    });
    expect(buildConnectionProducers(state).has(DISCARD_CONNECTION)).toBe(false);
  });
});

// The shared fixture for the cross-check below. Declared once and used by BOTH
// indexes — the point is a SHARED fixture, not two that happen to agree. It
// exercises all four registration rules and both sentinels at once: a source
// with on_success (under a NON-default name, so the component-id vocabulary is
// actually on test), a queue publishing implicitly under its own id, a node
// with on_error, a gate whose routes hold both a real target and "fork" with a
// fork_to array, and one on_error: "discard".
const SHARED_FANIN_FIXTURE = makeComposition(1, {
  sources: { intake: { plugin: "csv", options: {}, on_success: "raw_rows" } },
  nodes: [
    { id: "classify", node_type: "transform", plugin: "llm", options: {},
      input: "raw_rows", on_success: "scored", on_error: "review_queue" },
    { id: "split", node_type: "gate", plugin: null, options: {}, input: "scored",
      on_success: null, on_error: DISCARD_CONNECTION,
      routes: { keep: "kept", every: "fork" }, fork_to: ["arm_a", "arm_b"] },
    { id: "hold", node_type: "queue", plugin: null, options: {},
      input: "arm_a", on_success: null, on_error: null },
    { id: "merge", node_type: "coalesce", plugin: null, options: {},
      input: "hold", on_success: null, on_error: null,
      branches: { left: "hold", right: "arm_b" } },
  ],
  outputs: [{ name: "kept", plugin: "csv", options: {} }],
});

describe("buildConnectionProducers agrees with GraphView's producer registry", () => {
  it("registers the same producers for the same coalesce/fork composition", () => {
    // The two implementations that this task deliberately did NOT merge. They
    // diverge in the two sentinels, in ReactFlow decoration, and in dedup:
    // buildConnectionProducers dedupes producer ids per key (`push` above
    // checks `includes` before appending), buildProducerRegistry does not.
    // The assertions below Set-compare both the key set and each key's
    // producer-id set, which absorbs the dedup difference — a duplicate in
    // GraphView's array still compares equal to the deduped lifted array.
    // The KEY SET and the producer-id set per key must match exactly. Drift
    // on this axis is what misrendered a working fork/coalesce pipeline as
    // two disconnected fragments (session 3f02c8fa) and drew a coalesce with
    // a single arm (elspeth-625e85c59b) — the two incidents this module
    // exists to stop recurring.
    const state = SHARED_FANIN_FIXTURE;   // one fixture, used by both assertions
    const lifted = buildConnectionProducers(state);
    const graphView = buildProducerRegistry(state);   // ProducerInfo[] per key

    const graphViewKeys = new Set([...graphView.keys()].filter(
      (key) => key !== FORK_CONNECTION && key !== DISCARD_CONNECTION,
    ));
    expect(new Set(lifted.keys())).toEqual(graphViewKeys);
    for (const key of graphViewKeys) {
      expect(new Set(lifted.get(key))).toEqual(
        new Set((graphView.get(key) ?? []).map((producer) => producer.nodeId)),
      );
    }
    // Vacuity guard: the loop above passes trivially on an empty key set, and
    // both sentinels must actually be present in GraphView's index for the
    // filter to be doing anything.
    expect(graphViewKeys.size).toBeGreaterThan(4);
    expect(graphView.has(FORK_CONNECTION)).toBe(true);
    expect(graphView.has(DISCARD_CONNECTION)).toBe(true);
  });
});

describe("shared member sets and sentinels", () => {
  it("pins the frontend's single copy of the coalesce members", () => {
    // Backend authority: CoalesceSettings.policy / .merge in core/config.py.
    // These were declared privately a SECOND time in api/guidedDecoder.ts;
    // this module is now the one place the frontend states them.
    //
    // Be honest about what this assertion is: it compares a TypeScript literal
    // against another TypeScript literal, so it is DRIFT DETECTION within the
    // frontend — it can only fail when someone edits the tuple. It is NOT the
    // cross-language mirror. That is the parity assertion in
    // tests/unit/web/composer/test_graph_topology_parity.py, which
    // reads this file and compares against the Python Literals.
    expect([...COALESCE_POLICIES]).toEqual(["require_all", "quorum", "best_effort", "first"]);
    expect([...COALESCE_MERGES]).toEqual(["union", "nested", "select"]);
  });

  it("names the discard sentinel so PipelineSpecView and SchemaFormTurn stop spelling it", () => {
    // _producer_resolver.py:208 — discard is not a connection. The two
    // production sites this replaces are PipelineSpecView.tsx's routing
    // humaniser and SchemaFormTurn's blob-prefill default. This assertion
    // alone is a tautology; what makes
    // the constant load-bearing is that both callers import it, which the
    // Task 4 tests and the SchemaFormTurn suite exercise.
    expect(DISCARD_CONNECTION).toBe("discard");
  });
});
