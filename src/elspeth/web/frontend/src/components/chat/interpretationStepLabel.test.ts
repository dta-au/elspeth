// ============================================================================
// interpretationStepLabel.test.ts — coverage for humaniseStepLabel's node-name
// preference (R2-F8b): a user-meaningful node id (e.g. `extract_invoice`) is
// title-cased and shown as-is; a Composer-generated id (`guided_xform_1`)
// still falls back to the per-plugin verb ("Summarise" for `llm`, …).
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

  it("falls back to the plugin verb for a Composer-generated node id", () => {
    const state = makeCompositionState([makeNode("guided_xform_1", "llm")]);
    expect(humaniseStepLabel(state, "guided_xform_1")).toBe("Summarise");
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

  it("returns the plugin verb for a Composer-generated id", () => {
    const state = makeCompositionState([makeNode("guided_xform_1", "llm")]);
    expect(stepLabelForNodeId(state, "guided_xform_1")).toBe("Summarise");
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
