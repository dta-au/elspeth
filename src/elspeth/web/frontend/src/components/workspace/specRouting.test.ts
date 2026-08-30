import { describe, expect, it } from "vitest";

import { makeComposition } from "@/test/composerFixtures";
import {
  buildConnectionIndex,
  componentPhrase,
  routingPhrase,
} from "./specRouting";

const state = makeComposition(9, {
  sources: {
    source: { plugin: "csv", options: {}, on_success: "raw_rows", on_validation_failure: "discard" },
  },
  nodes: [
    {
      id: "extract_invoice",
      node_type: "transform",
      plugin: "llm",
      input: "raw_rows",
      on_success: "invest_cs1_done",
      on_error: null,
      options: {},
    },
    {
      id: "merge_invest",
      node_type: "coalesce",
      plugin: null,
      input: "invest_cs1_done",
      on_success: "tidy_output",
      on_error: null,
      branches: { branch_invest_cs1: "invest_cs1_done", branch_invest_cs2: "invest_cs2_done" },
      policy: "require_all",
      merge: "union",
      options: {},
    },
  ],
  outputs: [{ name: "tidy_output", plugin: "csv", on_write_failure: "discard", options: {} }],
});
const index = buildConnectionIndex(state);

describe("buildConnectionIndex", () => {
  it("maps a connection to the components on each end", () => {
    expect(index.consumers.get("raw_rows")).toEqual(["extract_invoice"]);
    expect(index.producers.get("raw_rows")).toEqual(["source"]);
    expect(index.consumers.get("tidy_output")).toEqual(["tidy_output"]);
    expect(index.producers.get("invest_cs1_done")).toEqual(["extract_invoice"]);
  });

  it("registers a fan-in node against its BRANCH connections, never its placeholder input", () => {
    // core/config.py: branches is a "Branch identity -> input connection
    // mapping"; connection_consumers.py skips node.input for
    // coalesce/row_union because that scalar is only the backend-compatible
    // first-branch placeholder. Both branches are consumed; neither is
    // produced by the coalesce (its on_success is tidy_output).
    expect(index.consumers.get("invest_cs1_done")).toEqual(["merge_invest"]);
    expect(index.consumers.get("invest_cs2_done")).toEqual(["merge_invest"]);
    expect(index.producers.get("invest_cs2_done")).toBeUndefined();
    expect(index.producers.get("tidy_output")).toEqual(["merge_invest"]);
  });

  it("credits a queue/coalesce/aggregation with its implicit self-published connection", () => {
    // publishedSuccessConnection, not node.on_success: a fan-in node with no
    // on_success publishes under its own id (session 3f02c8fa).
    const implicit = makeComposition(12, {
      sources: { source: { plugin: "csv", options: {}, on_success: "rows" } },
      nodes: [
        { id: "hold", node_type: "queue", plugin: null, input: "rows", on_success: null, on_error: null, options: {} },
      ],
      outputs: [{ name: "hold", plugin: "csv", on_write_failure: "discard", options: {} }],
    });
    expect(buildConnectionIndex(implicit).producers.get("hold")).toEqual(["hold"]);
  });
});

describe("componentPhrase", () => {
  it("uses the shared step label, title-casing an unlabelable id", () => {
    expect(componentPhrase(state, "extract_invoice")).toBe("Extract Invoice");
    expect(componentPhrase(state, "merge_invest")).toBe("Merge Invest");
    expect(componentPhrase(state, "tidy_output")).toBe("Tidy Output");
  });

  it("resolves a non-default source through its COMPONENT id and phrases it by description", () => {
    // The index speaks component ids (buildConnectionProducers registers
    // sourceComponentId(name), not the bare key), so componentPhrase must
    // resolve `source:<name>` — `state.sources[id]` misses it entirely and
    // would title-case the prefixed id into "Source:intake". The description
    // rung then applies to the source NAME, matching makePhraseFor.
    const described = makeComposition(16, {
      sources: {
        intake: {
          plugin: "csv",
          options: {},
          on_success: "raw_rows",
          on_validation_failure: "rejects",
          description: "Quarterly invoices from finance",
        },
      },
      nodes: [
        { id: "extract", node_type: "transform", plugin: "llm", input: "raw_rows", on_success: "final_out", on_error: null, options: {} },
      ],
      outputs: [{ name: "final_out", plugin: "csv", options: {} }],
    });
    const { producers } = buildConnectionIndex(described);
    // Both source registrations — buildConnectionProducers' on_success and
    // this module's added on_validation_failure — must agree on the id.
    expect(producers.get("raw_rows")).toEqual(["source:intake"]);
    expect(producers.get("rejects")).toEqual(["source:intake"]);
    expect(componentPhrase(described, "source:intake")).toBe(
      "Quarterly invoices from finance",
    );
  });
});

describe("routingPhrase", () => {
  it("names the consumer for a downstream connection and keeps the raw name", () => {
    expect(routingPhrase(state, index, "on_success", "raw_rows")).toEqual({
      text: "Extract Invoice",
      raw: "raw_rows",
    });
  });

  it("names the producer for an input connection", () => {
    expect(routingPhrase(state, index, "input", "invest_cs1_done")).toEqual({
      text: "Extract Invoice",
      raw: "invest_cs1_done",
    });
  });

  it("marks a dangling connection so it cannot pass for a wired one", () => {
    // Title case alone made "Then: Nowhere Yet" read as a step actually named
    // "Nowhere Yet" — same register and shape as a resolved connection, on the
    // surface this wave asks non-engineers to review instead of the YAML.
    // `raw` is unchanged: the marker is reader-register prose, not part of the
    // value the `title` attribute carries.
    expect(routingPhrase(state, index, "on_success", "nowhere_yet")).toEqual({
      text: "Nowhere Yet (not connected)",
      raw: "nowhere_yet",
    });
  });

  it("does NOT mark a resolved connection", () => {
    // The other half of the pin: if the marker leaked onto the resolved arm it
    // would be noise on every healthy card, and the distinction it exists to
    // draw would be gone in the other direction.
    // Asserted as an EQUALITY, not a `?.text` not-to-contain: optional
    // chaining would make this pass vacuously the day the resolved arm starts
    // returning null, which is the direction a regression here would take.
    expect(routingPhrase(state, index, "on_success", "raw_rows")).toEqual({
      text: "Extract Invoice",
      raw: "raw_rows",
    });
  });

  it("never marks the fork/discard sentinels, which are not connections at all", () => {
    // Nothing registers a sentinel as a producer or consumer
    // (_producer_resolver.py), so both reach the unresolved arm permanently. A
    // deliberately-forked branch is not dangling, and saying so would be false.
    // `discard` reaches this path only through a map entry — the scalar arm
    // returns null before `connectionPhrase` is ever called.
    expect(routingPhrase(state, index, "routes", { every: "fork" })?.text).not.toContain(
      "not connected",
    );
    expect(
      routingPhrase(state, index, "routes", { dropped: "discard" })?.text,
    ).not.toContain("not connected");
  });

  it("resolves a branch map UPSTREAM — the producer feeding the branch, never the fan-in node itself", () => {
    // The regression this test exists for: resolving branches downstream
    // yields "Branch Invest Cs1 -> Merge Invest", a node pointing at itself,
    // because a fan-in node's own `input` is one of its branch connections.
    expect(
      routingPhrase(state, index, "branches", {
        branch_invest_cs1: "invest_cs1_done",
        branch_invest_cs2: "invest_cs2_done",
      }),
    ).toEqual({
      // The marker earns its keep inside one sentence here: `invest_cs1_done`
      // resolves to a real producer, `invest_cs2_done` is unwired in this
      // fixture. Before the marker both arms read as ordinary Title Case step
      // names and the reader had no way to tell the wired branch from the
      // dangling one — the coalesce-branch half of the same defect.
      text: "Branch Invest Cs1 → Extract Invoice; Branch Invest Cs2 → Invest Cs2 Done (not connected)",
      raw: "branch_invest_cs1 → invest_cs1_done; branch_invest_cs2 → invest_cs2_done",
    });
  });

  it("expands a list-form branches to the identity mapping (a coalesce arrives holding a list)", () => {
    expect(routingPhrase(state, index, "branches", ["invest_cs1_done"])).toEqual({
      text: "Invest Cs1 Done → Extract Invoice",
      raw: "invest_cs1_done → invest_cs1_done",
    });
  });

  it("keeps the all-fork routes sentence and phrases other route targets", () => {
    expect(routingPhrase(state, index, "routes", { a: "fork", b: "fork" })?.text).toBe(
      "every row continues to all branches",
    );
    expect(routingPhrase(state, index, "routes", { rejected: "tidy_output" })).toEqual({
      text: "Rejected → Tidy Output",
      raw: "rejected → tidy_output",
    });
  });

  it("phrases the closed policy enums and falls back to title case for an unknown value", () => {
    expect(routingPhrase(state, index, "policy", "require_all")).toEqual({
      text: "wait for every branch",
      raw: "require_all",
    });
    expect(routingPhrase(state, index, "merge", "union")).toEqual({
      text: "combine every branch's fields",
      raw: "union",
    });
    expect(routingPhrase(state, index, "scope_policy", "require_all")).toEqual({
      text: "wait for every row in the group",
      raw: "require_all",
    });
    expect(routingPhrase(state, index, "output_mode", "passthrough")).toEqual({
      text: "pass rows through unchanged",
      raw: "passthrough",
    });
    expect(routingPhrase(state, index, "output_mode", "default")).toEqual({
      text: "use the plugin's own behaviour",
      raw: "default",
    });
    // `policy` closes against a compile-time union, so this arm is reachable
    // only because types/index.ts still types the wire field as `string`. It
    // keeps title case; the OPEN map below is the one that changed.
    expect(routingPhrase(state, index, "policy", "someday_maybe")).toEqual({
      text: "Someday Maybe",
      raw: "someday_maybe",
    });
  });

  it("renders an unknown scope_policy as an identifier, never as fake prose", () => {
    // scope_policy is the ONE open map here — no backend Literal, no frontend
    // member set — so its unknown arm is genuinely reachable, and title-casing
    // it produced text that READS like a phrase the product chose
    // ("Someday Maybe") for a value nobody has phrased at all. Aligned on the
    // diagnosticPhrases register: an unphrased identifier renders as code.
    expect(routingPhrase(state, index, "scope_policy", "someday_maybe")).toEqual({
      text: "someday_maybe",
      raw: "someday_maybe",
      register: "identifier",
    });
    // A phrased member is unaffected and carries no register marker.
    expect(routingPhrase(state, index, "scope_policy", "best_effort")).toEqual({
      text: "close the group with whichever rows arrive",
      raw: "best_effort",
    });
  });

  it("names the opener node for scope_opener, says Removed for a deleted one, and passes discard / numbers through as null", () => {
    expect(routingPhrase(state, index, "scope_opener", "extract_invoice")).toEqual({
      text: "Extract Invoice",
      raw: "extract_invoice",
    });
    // scope_opener names a COMPONENT, not a connection, so absence is a
    // deletion and reads as one (isComponentPresent).
    expect(routingPhrase(state, index, "scope_opener", "deleted_opener")).toEqual({
      text: "Removed",
      raw: "deleted_opener",
    });
    expect(routingPhrase(state, index, "on_write_failure", "discard")).toBeNull();
    expect(routingPhrase(state, index, "timeout_seconds", 300)).toBeNull();
  });
});

describe("buildConnectionIndex — the branches the ruling states and nothing tested", () => {
  // Every case below is a rule this module DOCUMENTS. A documented rule with
  // no test is the shape this whole wave exists to remove.

  it("keeps ordinary `input` inference for a fan-in node with NO branches", () => {
    // The `entries.length > 0` guard in nodeInputs, matching GraphView's
    // aliasMappedFanInIds guard. Silent failure mode: a mid-edit coalesce
    // drops out of the consumer index entirely, so every routing field
    // pointing at it title-cases instead of naming it — indistinguishable
    // from a dangling connection. All three empty shapes reach the same arm
    // and all three are representable on the wire.
    const emptyBranchShapes: Array<string[] | Record<string, string> | null> = [null, [], {}];
    for (const branches of emptyBranchShapes) {
      const composition = makeComposition(1, {
        sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
        nodes: [{ id: "merge", node_type: "coalesce", plugin: null, input: "raw_rows",
                  on_success: "final_out", on_error: null, branches, options: {} }],
        outputs: [{ name: "final_out", plugin: "csv", options: {} }],
      });
      expect(buildConnectionIndex(composition).consumers.get("raw_rows")).toContain("merge");
    }
  });

  it("registers BOTH consumers of one connection", () => {
    // connectionPhrase joins with ", "; a single-consumer-only index would
    // read the same in every existing fixture.
    const composition = makeComposition(1, {
      sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
      nodes: [
        { id: "score", node_type: "transform", plugin: "llm", input: "raw_rows", on_success: "a", on_error: null, options: {} },
        { id: "audit", node_type: "transform", plugin: "llm", input: "raw_rows", on_success: "b", on_error: null, options: {} },
      ],
      outputs: [],
    });
    expect(buildConnectionIndex(composition).consumers.get("raw_rows")?.sort()).toEqual([
      "audit",
      "score",
    ]);
  });

  it("registers a source's on_validation_failure and an output's on_write_failure as producers, but never `discard`", () => {
    // The two additions this task layers on top of buildConnectionProducers.
    // The discard guard keeps a sentinel out of the shared index.
    const composition = makeComposition(1, {
      sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows", on_validation_failure: "rejects" } },
      nodes: [{ id: "triage", node_type: "transform", plugin: "llm", input: "rejects", on_success: "final_out", on_error: null, options: {} }],
      outputs: [
        { name: "final_out", plugin: "csv", options: {}, on_write_failure: "write_errors" },
        { name: "write_errors", plugin: "csv", options: {}, on_write_failure: "discard" },
      ],
    });
    const { producers } = buildConnectionIndex(composition);
    expect(producers.get("rejects")).toEqual(["source"]);
    expect(producers.get("write_errors")).toEqual(["final_out"]);
    expect(producers.has("discard")).toBe(false);
  });
});

describe("routingPhrase — the partly-fork routes map", () => {
  it("renders a mixed routes map alias-by-alias rather than as 'every row continues to all branches'", () => {
    // routingValue's every()-guard is false here, so it falls through to the
    // alias-by-alias arm. The "fork" arm renders as the sentinel word and NOT
    // as a resolved component — pinned so the output is a decision rather
    // than an accident.
    const composition = makeComposition(1, {
      sources: { source: { plugin: "csv", options: {}, on_success: "raw_rows" } },
      nodes: [
        { id: "split", node_type: "gate", plugin: null, input: "raw_rows", on_success: null, on_error: null,
          routes: { kept: "tidy_output", every: "fork" }, fork_to: ["arm_a"], options: {} },
        { id: "tidy", node_type: "transform", plugin: "llm", input: "tidy_output", on_success: "final_out", on_error: null, options: {} },
      ],
      outputs: [{ name: "final_out", plugin: "csv", options: {} }],
    });
    const mixedIndex = buildConnectionIndex(composition);
    const phrase = routingPhrase(composition, mixedIndex, "routes", {
      kept: "tidy_output",
      every: "fork",
    });
    expect(phrase?.text).toBe("Kept → Tidy; Every → Fork");
    expect(phrase?.raw).toBe("kept → tidy_output; every → fork");
  });
});
