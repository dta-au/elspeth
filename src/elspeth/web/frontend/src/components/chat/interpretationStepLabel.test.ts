// ============================================================================
// interpretationStepLabel.test.ts — coverage for humaniseStepLabel's node-name
// preference (R2-F8b). Every node id is author/LLM-chosen — there is no
// Composer-side id generator — so the only id NOT title-cased as a name is
// one that is trivially just its own plugin's name (`llm`, `llm_2`): those
// still fall back to the per-plugin verb ("Summarise" for `llm`, …). Anything
// else, including a semantically-named id like `llm_rate_coolness`, is
// title-cased and shown as the author's own name for the step.
// ============================================================================

import { describe, it, expect } from "vitest";
import {
  buildStepOrder,
  humaniseStepLabel,
  resolveNodePlugin,
  stepLabelForNodeId,
  stepLabelForPlugin,
} from "./interpretationStepLabel";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import type { CompositionState, NodeSpec } from "@/types/index";

function makeNode(id: string, plugin: string): NodeSpec {
  return {
    id,
    node_type: "transform",
    plugin,
    input: "rows",
    on_success: null,
    on_error: null,
    options: {},
  };
}

function makeCompositionState(nodes: NodeSpec[]): CompositionState {
  return {
    id: "state-1",
    ...compositionStateAuthorityFields,
    version: 1,
    sources: {},
    nodes,
    edges: [],
    outputs: [],
    metadata: { name: null, description: null },
  };
}

describe("stepLabelForPlugin", () => {
  it("maps well-known plugins to their verb", () => {
    expect(stepLabelForPlugin("llm")).toBe("Summarise");
    expect(stepLabelForPlugin("web_scrape")).toBe("Fetch");
    expect(stepLabelForPlugin("field_mapper")).toBe("Output");
  });

  it("title-cases any other plugin name", () => {
    expect(stepLabelForPlugin("aws_textract_document_analysis")).toBe(
      "Aws Textract Document Analysis",
    );
  });
});

describe("humaniseStepLabel — node-name preference (R2-F8b)", () => {
  it("prefers a user-meaningful node id over the plugin verb (llm node named extract_invoice)", () => {
    const state = makeCompositionState([makeNode("extract_invoice", "llm")]);
    expect(humaniseStepLabel(state, "extract_invoice")).toBe("Extract Invoice");
  });

  it("falls back to the plugin verb when the node id is exactly the plugin name (llm)", () => {
    const state = makeCompositionState([makeNode("llm", "llm")]);
    expect(humaniseStepLabel(state, "llm")).toBe("Summarise");
  });

  it("falls back to the plugin verb when the node id is the plugin name plus a numeric suffix (llm_2)", () => {
    const state = makeCompositionState([makeNode("llm_2", "llm")]);
    expect(humaniseStepLabel(state, "llm_2")).toBe("Summarise");
  });

  it("title-cases a semantically-named id even though it starts with the plugin name (llm_rate_coolness) — accepted behaviour, not a false positive on the plugin-derived check", () => {
    const state = makeCompositionState([makeNode("llm_rate_coolness", "llm")]);
    expect(humaniseStepLabel(state, "llm_rate_coolness")).toBe("Llm Rate Coolness");
  });

  it("title-cases a user-meaningful node id for a non-llm plugin too", () => {
    const state = makeCompositionState([
      makeNode("scrape_landing_page", "web_scrape"),
    ]);
    expect(humaniseStepLabel(state, "scrape_landing_page")).toBe(
      "Scrape Landing Page",
    );
  });

  it("falls back to the raw id when the node is absent from the composition", () => {
    const state = makeCompositionState([]);
    expect(humaniseStepLabel(state, "ghost_node")).toBe("ghost_node");
  });

  it("falls back to a generic phrase when there is no id at all", () => {
    expect(humaniseStepLabel(null, null)).toBe("this step");
  });
});

describe("stepLabelForNodeId — the shared choke point (R2-F8b)", () => {
  it("returns the title-cased node name for a user-meaningful id", () => {
    const state = makeCompositionState([makeNode("extract_invoice", "llm")]);
    expect(stepLabelForNodeId(state, "extract_invoice")).toBe("Extract Invoice");
  });

  it("returns the plugin verb when the id is exactly the plugin name", () => {
    const state = makeCompositionState([makeNode("llm", "llm")]);
    expect(stepLabelForNodeId(state, "llm")).toBe("Summarise");
  });

  it("returns the plugin verb when the id is the plugin name plus a numeric suffix", () => {
    const state = makeCompositionState([makeNode("web_scrape_3", "web_scrape")]);
    expect(stepLabelForNodeId(state, "web_scrape_3")).toBe("Fetch");
  });

  it("does not treat a plugin-name PREFIX as plugin-derived (llm_rate_coolness stays a real name)", () => {
    const state = makeCompositionState([makeNode("llm_rate_coolness", "llm")]);
    expect(stepLabelForNodeId(state, "llm_rate_coolness")).toBe("Llm Rate Coolness");
  });

  it("returns null (not the raw id) when the node is absent — callers that must not leak an internal id (validationHumaniser's PipelineValidationSummary / ReadinessRowDetail consumers) depend on this", () => {
    const state = makeCompositionState([]);
    expect(stepLabelForNodeId(state, "ghost_node")).toBeNull();
  });

  it("returns null for a null id or a null composition", () => {
    expect(stepLabelForNodeId(null, "extract_invoice")).toBeNull();
    const state = makeCompositionState([makeNode("extract_invoice", "llm")]);
    expect(stepLabelForNodeId(state, null)).toBeNull();
  });

  // ── resolution fallthrough: sources and outputs (resolveNodePlugin also
  // searches state.sources and state.outputs, not just state.nodes) ────────
  it("resolves via state.sources and applies the SAME node-name preference (source id 'manifest', plugin 'csv' → title-cased, not the plugin verb)", () => {
    const state: CompositionState = {
      ...makeCompositionState([]),
      sources: { manifest: { plugin: "csv", options: {} } },
    };
    expect(stepLabelForNodeId(state, "manifest")).toBe("Manifest");
  });

  it("resolves via state.sources and falls back to the plugin verb when the source key IS the plugin name", () => {
    const state: CompositionState = {
      ...makeCompositionState([]),
      sources: { web_scrape: { plugin: "web_scrape", options: {} } },
    };
    expect(stepLabelForNodeId(state, "web_scrape")).toBe("Fetch");
  });

  it("resolves via state.outputs and applies the SAME node-name preference (output name 'results', plugin 'json' → title-cased)", () => {
    const state: CompositionState = {
      ...makeCompositionState([]),
      outputs: [{ name: "results", plugin: "json", options: {} }],
    };
    expect(stepLabelForNodeId(state, "results")).toBe("Results");
  });

  it("resolves via state.outputs and falls back to the well-known plugin verb when the output name IS the plugin name", () => {
    const state: CompositionState = {
      ...makeCompositionState([]),
      outputs: [{ name: "field_mapper", plugin: "field_mapper", options: {} }],
    };
    expect(stepLabelForNodeId(state, "field_mapper")).toBe("Output");
  });
});

describe("resolveNodePlugin", () => {
  it("resolves a node's plugin by id", () => {
    const state = makeCompositionState([makeNode("extract_invoice", "llm")]);
    expect(resolveNodePlugin(state, "extract_invoice")).toBe("llm");
  });

  it("returns null when the id is absent", () => {
    const state = makeCompositionState([]);
    expect(resolveNodePlugin(state, "missing")).toBeNull();
  });
});

describe("buildStepOrder", () => {
  it("orders sources before nodes before outputs", () => {
    const state: CompositionState = {
      ...makeCompositionState([makeNode("a", "llm"), makeNode("b", "web_scrape")]),
      sources: { in: { plugin: "csv", options: {} } },
      outputs: [{ name: "out", plugin: "json", options: {} }],
    };
    const order = buildStepOrder(state);
    expect(order.get("in")).toBe(0);
    expect(order.get("a")).toBe(1);
    expect(order.get("b")).toBe(2);
    expect(order.get("out")).toBe(3);
  });
});
