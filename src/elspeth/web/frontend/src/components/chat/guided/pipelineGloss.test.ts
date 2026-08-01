import { describe, expect, it } from "vitest";

import {
  buildPlainPhraseMap,
  GLOSS_FALLBACK,
  pipelineGloss,
  UNKNOWN_COMPONENT_PHRASE,
} from "./pipelineGloss";
import { makeComposition } from "@/test/composerFixtures";

// A source→llm→csv composition exercised by several cases: deterministic
// plugins so the phrase map is stable (text→"read your data", llm→"rate each
// row", csv→"write a CSV").
function sourceLlmCsv() {
  return makeComposition(1, {
    sources: { source: { plugin: "text", options: {} } },
    nodes: [
      {
        id: "rater",
        node_type: "transform",
        plugin: "llm",
        input: "source",
        on_success: null,
        on_error: null,
        options: {},
      },
    ],
    outputs: [{ name: "out", plugin: "csv", options: {} }],
  });
}

describe("pipelineGloss", () => {
  it("derives a one-sentence gloss from an ordered source→llm→csv pipeline", () => {
    expect(pipelineGloss(sourceLlmCsv())).toBe(
      "This pipeline will read your data, rate each row, and write a CSV.",
    );
  });

  it("derives a two-clause gloss with an Oxford 'and' for a source→sink pipeline", () => {
    const state = makeComposition(1, {
      sources: { source: { plugin: "text", options: {} } },
      nodes: [],
      outputs: [{ name: "out", plugin: "csv", options: {} }],
    });
    expect(pipelineGloss(state)).toBe(
      "This pipeline will read your data and write a CSV.",
    );
  });

  it("falls back to a safe phrase for an empty or null composition", () => {
    const empty = makeComposition(1, { sources: {}, nodes: [], outputs: [] });
    expect(pipelineGloss(empty)).toBe(GLOSS_FALLBACK);
    expect(pipelineGloss(null)).toBe(GLOSS_FALLBACK);
    expect(pipelineGloss(undefined)).toBe(GLOSS_FALLBACK);
  });

  it("handles a partial composition (source only) without crashing", () => {
    const partial = makeComposition(1, {
      sources: { source: { plugin: "text", options: {} } },
      nodes: [],
      outputs: [],
    });
    expect(pipelineGloss(partial)).toBe("This pipeline will read your data.");
  });
});

describe("buildPlainPhraseMap", () => {
  it("keys phrases by the GraphView component_id scheme (source / node.id / output.name)", () => {
    const map = buildPlainPhraseMap(sourceLlmCsv());
    // Default source name "source" → component_id "source".
    expect(map.get("source")).toBe("read your data");
    // Node keyed by node.id.
    expect(map.get("rater")).toBe("rate each row");
    // Output keyed by output.name.
    expect(map.get("out")).toBe("write a CSV");
  });

  it("keys a non-default source name via sourceComponentId (source:<name>)", () => {
    const state = makeComposition(1, {
      sources: { feed: { plugin: "api", options: {} } },
      nodes: [],
      outputs: [],
    });
    const map = buildPlainPhraseMap(state);
    expect(map.get("source:feed")).toBe("read from an API");
  });

  it("returns an empty map for a null/undefined composition", () => {
    expect(buildPlainPhraseMap(null).size).toBe(0);
    expect(buildPlainPhraseMap(undefined).size).toBe(0);
  });

  it("exports a non-empty generic fallback phrase for unmappable component ids", () => {
    expect(UNKNOWN_COMPONENT_PHRASE).toMatch(/\S/);
  });
});

// elspeth-bc8a35c1ab: two transforms that land on the same generic phrase used
// to stutter ("…process each row, process each row, and write a JSON file").
// The phrases are CATEGORY labels, not identities, so a repeat is expected —
// the sentence must count the run rather than print it twice. Dropping the
// repeat silently would under-report the pipeline, so the count is carried.
describe("pipelineGloss — repeated phrases", () => {
  function twoGenericTransforms() {
    return makeComposition(1, {
      sources: { source: { plugin: "csv", options: {} } },
      nodes: [
        {
          id: "first",
          node_type: "transform",
          plugin: "passthrough",
          input: "source",
          on_success: null,
          on_error: null,
          options: {},
        },
        {
          id: "second",
          node_type: "transform",
          plugin: "passthrough",
          input: "first",
          on_success: null,
          on_error: null,
          options: {},
        },
      ],
      outputs: [{ name: "out", plugin: "json", options: {} }],
    });
  }

  it("counts a repeated phrase instead of repeating it", () => {
    expect(pipelineGloss(twoGenericTransforms())).toBe(
      "This pipeline will read your CSV, process each row twice, and write a JSON file.",
    );
  });

  it("counts runs longer than two numerically", () => {
    const state = twoGenericTransforms();
    const third = { ...state.nodes[1], id: "third", input: "second" };
    expect(pipelineGloss({ ...state, nodes: [...state.nodes, third] })).toBe(
      "This pipeline will read your CSV, process each row 3 times, and write a JSON file.",
    );
  });

  // The phrase map is an identity lookup keyed by component_id, consumed by
  // PipelineValidationSummary to attribute findings. Two nodes sharing a
  // phrase is correct there — collapsing would break attribution.
  it("leaves the plain-phrase map un-collapsed", () => {
    const map = buildPlainPhraseMap(twoGenericTransforms());
    expect(map.get("first")).toBe("process each row");
    expect(map.get("second")).toBe("process each row");
  });
});

// A declared queue is uncorrelated structural fan-in: many producers publish
// one connection name and the queue interleaves those rows. The gloss must say
// so in plain language and must NOT reach for merge/join/union/buffer/priority
// wording (elspeth-a5b86149d4 / elspeth-6421ffa028).
describe("pipelineGloss — queue fan-in", () => {
  function queueComposition() {
    return makeComposition(1, {
      sources: {
        orders: { plugin: "csv", options: {} },
        refunds: { plugin: "csv", options: {} },
      },
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
        {
          id: "normalize",
          node_type: "transform",
          plugin: "passthrough",
          input: "inbound",
          on_success: null,
          on_error: null,
          options: {},
        },
      ],
      outputs: [{ name: "combined", plugin: "json", options: {} }],
    });
  }

  it("describes the queue as fan-in that interleaves the incoming rows", () => {
    const gloss = pipelineGloss(queueComposition());
    expect(gloss).toContain("interleave the incoming rows");
    expect(gloss).not.toMatch(/merge|join|union|buffer|priority/i);
  });

  it("keys the queue phrase by node id in the plain-phrase map", () => {
    const map = buildPlainPhraseMap(queueComposition());
    expect(map.get("inbound")).toBe("interleave the incoming rows");
  });
});

describe("pipelineGloss — row_union correlated N-to-N barrier", () => {
  function rowUnionComposition() {
    return makeComposition(1, {
      sources: {
        experiments: { plugin: "csv", options: {} },
      },
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
      outputs: [{ name: "results", plugin: "json", options: {} }],
    });
  }

  it("says row_union waits for every branch and preserves every row", () => {
    const gloss = pipelineGloss(rowUnionComposition());

    expect(gloss).toContain(
      "wait for every branch, then preserve every branch row",
    );
    expect(gloss).not.toContain("merge the branches");
    expect(gloss).not.toContain("interleave the incoming rows");
  });

  it("keys the distinct row_union phrase by node id", () => {
    const map = buildPlainPhraseMap(rowUnionComposition());

    expect(map.get("variant_union")).toBe(
      "wait for every branch, then preserve every branch row",
    );
  });
});
