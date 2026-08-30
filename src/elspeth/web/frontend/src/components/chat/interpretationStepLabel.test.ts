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
  humaniseStepTitle,
  isComponentPresent,
  resolveNodePlugin,
  stepLabelForNodeId,
  stepLabelForPlugin,
} from "./interpretationStepLabel";
import { pluginDisplayName } from "@/components/catalog/pluginDisplayName";
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

  // elspeth-d2de348437: a plugin outside the verb map must carry EXACTLY its
  // catalog card name — same curated overrides, same acronym set — so one
  // plugin never has two display names in one session. Derived from the
  // shared function (the constraint), with one literal anchor so the shared
  // function itself cannot silently regress.
  it("matches the catalog display name exactly for plugins outside the verb map", () => {
    expect(stepLabelForPlugin("json_explode")).toBe(
      pluginDisplayName("json_explode"),
    );
    expect(stepLabelForPlugin("json_explode")).toBe("JSON Explode");
    expect(stepLabelForPlugin("azure_blob")).toBe(pluginDisplayName("azure_blob"));
    expect(stepLabelForPlugin("csv")).toBe(pluginDisplayName("csv"));
  });

  it("is not confused by Object.prototype key names (verb map is a Map, not a bare object)", () => {
    expect(stepLabelForPlugin("constructor")).toBe("Constructor");
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
    // "LLM", not "Llm": author names share the catalog's acronym set
    // (elspeth-d2de348437).
    expect(humaniseStepLabel(state, "llm_rate_coolness")).toBe("LLM Rate Coolness");
  });

  it("title-cases a user-meaningful node id for a non-llm plugin too", () => {
    const state = makeCompositionState([
      makeNode("scrape_landing_page", "web_scrape"),
    ]);
    expect(humaniseStepLabel(state, "scrape_landing_page")).toBe(
      "Scrape Landing Page",
    );
  });

  it("names an absent node 'Removed' — never the raw id (elspeth-93f5621f18)", () => {
    const state = makeCompositionState([]);
    expect(humaniseStepLabel(state, "ghost_node")).toBe("Removed");
  });

  it("title-cases the id while the composition is still unloaded (unknown, not removed)", () => {
    expect(humaniseStepLabel(null, "extract_invoice")).toBe("Extract Invoice");
  });

  it("falls back to a generic phrase when there is no id at all", () => {
    expect(humaniseStepLabel(null, null)).toBe("this step");
  });

  it("keeps two removed steps distinguishable in the card title (ux M-2)", () => {
    // "Removed" alone is identical for every deleted node, so two
    // acknowledgement cards referencing two different removed steps carried
    // the same title with nothing visible or hoverable to tell them apart.
    // The ghost id is the author's own name for the step, so title-casing it
    // is not an identifier leak under this wave's own rule.
    const state = makeCompositionState([]);
    expect(humaniseStepTitle(state, "extract_invoice")).toBe(
      "Removed step (was Extract Invoice)",
    );
    expect(humaniseStepTitle(state, "rate_coolness")).toBe(
      "Removed step (was Rate Coolness)",
    );
    expect(humaniseStepTitle(state, "extract_invoice")).not.toBe(
      humaniseStepTitle(state, "rate_coolness"),
    );
  });

  it("names a present step, an unloaded composition and a missing id in the title register", () => {
    const state = makeCompositionState([makeNode("summarise_notes", "llm")]);
    // A present step is unchanged: the label plus the word the card appended
    // itself before this function existed.
    expect(humaniseStepTitle(state, "summarise_notes")).toBe("Summarise Notes step");
    // Unloaded is NOT removed — the same discrimination humaniseStepLabel makes.
    expect(humaniseStepTitle(null, "extract_invoice")).toBe("Extract Invoice step");
    // Not "this step step".
    expect(humaniseStepTitle(state, null)).toBe("this step");
  });

  it("does not mistake a step genuinely NAMED removed for a deleted one", () => {
    // The reason this resolves structurally rather than comparing against the
    // string "Removed": this node IS present, and its author-chosen name
    // title-cases to exactly the word a deleted step uses. A sentinel
    // comparison would have reported "Removed step (was Removed)" — a false
    // statement about the user's own pipeline.
    const state = makeCompositionState([makeNode("removed", "llm")]);
    expect(humaniseStepTitle(state, "removed")).toBe("Removed step");
    expect(humaniseStepTitle(state, "removed")).not.toContain("(was");
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
    expect(stepLabelForNodeId(state, "llm_rate_coolness")).toBe("LLM Rate Coolness");
  });

  it("never applies curated plugin display overrides to an author-chosen node name (id 'dataverse' on an llm node stays 'Dataverse', not 'Microsoft Dataverse')", () => {
    const state = makeCompositionState([makeNode("dataverse", "llm")]);
    expect(stepLabelForNodeId(state, "dataverse")).toBe("Dataverse");
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

// ── elspeth-9f21f3c57d: structural-node description fallback ────────────────
// The plugin chain cannot label a plugin-less structural node (gate/coalesce/
// queue/…); its authored description now fills that gap before the callers'
// raw-id / generic fallbacks. The plugin-resolvable primary path is untouched
// — a description never overrides a node's own (short) name on card surfaces.

describe("stepLabelForNodeId — structural-node description fallback (elspeth-9f21f3c57d)", () => {
  const gateNode = (description?: string): NodeSpec => ({
    id: "fan_out",
    node_type: "gate",
    plugin: null,
    input: "rows",
    on_success: null,
    on_error: null,
    options: {},
    condition: "row.kind == 'colour'",
    ...(description !== undefined ? { description } : {}),
  });

  it("labels a plugin-less structural node by its authored description (label register: trailing stop dropped)", () => {
    const state = makeCompositionState([gateNode("Send each colour down both branches.")]);
    expect(stepLabelForNodeId(state, "fan_out")).toBe(
      "Send each colour down both branches",
    );
    expect(humaniseStepLabel(state, "fan_out")).toBe(
      "Send each colour down both branches",
    );
  });

  it("title-cases a present plugin-less node with no description (present, so not 'Removed')", () => {
    const state = makeCompositionState([gateNode()]);
    expect(stepLabelForNodeId(state, "fan_out")).toBeNull();
    expect(humaniseStepLabel(state, "fan_out")).toBe("Fan Out");
  });

  it("does not let a description override a plugin-resolvable node's own name", () => {
    const node = { ...makeNode("rater", "llm"), description: "Rate each row for coolness." };
    const state = makeCompositionState([node]);
    expect(stepLabelForNodeId(state, "rater")).toBe("Rater");
  });

  it("returns null for an id absent from the composition entirely", () => {
    expect(stepLabelForNodeId(makeCompositionState([]), "ghost")).toBeNull();
  });
});

describe("isComponentPresent", () => {
  it("finds nodes, sources and outputs; false for an absent id or unloaded state", () => {
    const state: CompositionState = {
      ...makeCompositionState([makeNode("rater", "llm")]),
      sources: { input: { plugin: "csv", options: {} } },
      outputs: [{ name: "results", plugin: "csv", options: {} }],
    };
    expect(isComponentPresent(state, "rater")).toBe(true);
    expect(isComponentPresent(state, "input")).toBe(true);
    expect(isComponentPresent(state, "results")).toBe(true);
    expect(isComponentPresent(state, "ghost")).toBe(false);
    expect(isComponentPresent(null, "rater")).toBe(false);
    expect(isComponentPresent(state, null)).toBe(false);
  });
});
