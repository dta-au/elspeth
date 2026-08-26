import { describe, expect, it } from "vitest";

import {
  clientWireBlockerMessages,
  formatFindingBody,
  humaniseValidationMessage,
  makePhraseFor,
} from "./validationHumaniser";
import { UNKNOWN_COMPONENT_PHRASE } from "@/components/chat/guided/pipelineGloss";
import { makeComposition } from "@/test/composerFixtures";
import type { NodeSpec } from "@/types/index";

// ── humaniseValidationMessage ───────────────────────────────────────────────

describe("humaniseValidationMessage", () => {
  const identityPhraseFor = (id: string | null): string => id ?? "(null)";

  it("passes an unrecognised message through untouched", () => {
    const finding = humaniseValidationMessage("Some other error", identityPhraseFor);
    expect(finding).toEqual({ headline: "Some other error", raw: null, namedSteps: [] });
  });

  it("humanises a two-sided contract violation with both producer and consumer phrases", () => {
    const finding = humaniseValidationMessage(
      "Schema contract violation: 'rater' -> 'out'. Consumer requires: [score]",
      identityPhraseFor,
    );
    expect(finding.headline).toBe(
      "Two steps aren't connected correctly: the \"rater\" step's output doesn't match what \"out\" expects.",
    );
    expect(finding.raw).toContain("Schema contract violation");
  });

  it("humanises a one-sided contract violation (no consumer capture) without a second phrase", () => {
    const finding = humaniseValidationMessage(
      "Semantic contract violation: 'rater'. Declares output fields that don't match downstream.",
      identityPhraseFor,
    );
    expect(finding.headline).toBe(
      "A step isn't connected correctly: \"rater\" doesn't match what the next step expects.",
    );
  });

  it("humanises the backend transform contract shape without leaking its node id", () => {
    // Rule C's own headline since elspeth-920bd88299 — it no longer shares
    // "Transform contract violation" (nor an error_code) with the Rule D
    // collision check, so this fixture exercises the second pattern.
    const message =
      "Transform output guarantee violation: node 'select_output_fields' (field_mapper) declares output " +
      "fields [batch_size, customer_tier] (required) but with select_only: true the mapping can only " +
      "guarantee [customer_tier]. Declared required output fields not guaranteed by this transform: " +
      "[batch_size]. Those names are mapping TARGETS, and `schema` is this node's INPUT contract, so a " +
      "target is usually absent from `schema.fields` altogether.";

    const finding = humaniseValidationMessage(
      message,
      (id) => (id === "select_output_fields" ? "choose the output fields" : "unknown step"),
    );

    expect(finding.headline).toBe(
      "A step isn't connected correctly: \"choose the output fields\" doesn't match what the next step expects.",
    );
    expect(finding.headline).not.toContain("select_output_fields");
    expect(finding.raw).toBe(message);
  });

  it("humanises the transform output collision shape (Rule D keeps the original headline)", () => {
    const message =
      "Transform contract violation: node 'rewrite' (llm) declares output fields [headline] but " +
      "[headline] already arrive(s) on its input row. The engine rejects a transform that would " +
      "overwrite an existing input field, so this pipeline fails on the first row.";

    const finding = humaniseValidationMessage(
      message,
      (id) => (id === "rewrite" ? "rewrite the headline" : "unknown step"),
    );

    expect(finding.headline).toBe(
      "A step isn't connected correctly: \"rewrite the headline\" doesn't match what the next step expects.",
    );
    expect(finding.raw).toBe(message);
  });

  it("humanises the edge-contract preflight dump format", () => {
    const finding = humaniseValidationMessage(
      "Edge contract violation between producer node 'rater' (schema 'A') and consumer node 'out' (schema 'B'):\nMissing: score",
      identityPhraseFor,
    );
    expect(finding.headline).toContain("aren't connected correctly");
  });

  it("humanises an interpretation-review-pending dump via stepLabelFor", () => {
    const finding = humaniseValidationMessage(
      "pipeline_decision review pending for transform 'rater': drop_raw_html_fields",
      identityPhraseFor,
      () => "Summarise",
    );
    expect(finding.headline).toBe("The Summarise step is waiting for your review.");
    expect(finding.raw).toContain("pipeline_decision");
  });

  it("falls back to a generic review-pending headline when stepLabelFor cannot resolve the id", () => {
    const finding = humaniseValidationMessage(
      "pipeline_decision review pending for transform 'ghost': drop_raw_html_fields",
      identityPhraseFor,
      () => null,
    );
    expect(finding.headline).toBe("A step is waiting for your review.");
  });

  it("does not special-case review-pending dumps when no stepLabelFor is supplied", () => {
    // No stepLabelFor → falls through to the generic contract-violation /
    // passthrough path rather than crashing.
    const finding = humaniseValidationMessage(
      "pipeline_decision review pending for transform 'rater': drop_raw_html_fields",
      identityPhraseFor,
    );
    expect(finding.raw).toBeNull();
    expect(finding.headline).toContain("review pending");
  });
});

// ── makePhraseFor — direct / stripped / fuzzy / fallback / unknown ─────────

describe("makePhraseFor", () => {
  it("returns the neutral phrase for a null component id", () => {
    const phraseFor = makePhraseFor(null);
    expect(phraseFor(null)).toBe(UNKNOWN_COMPONENT_PHRASE);
  });

  it("resolves a direct component_id hit from the composition", () => {
    const state = makeComposition(1, {
      sources: { source: { plugin: "text", options: {} } },
      nodes: [],
      outputs: [{ name: "out", plugin: "csv", options: {} }],
    });
    const phraseFor = makePhraseFor(state);
    expect(phraseFor("out")).toBe("write a CSV");
  });

  it("resolves a role-prefixed id by stripping the node:/source:/output: prefix", () => {
    const state = makeComposition(1, {
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
      outputs: [],
    });
    const phraseFor = makePhraseFor(state);
    // "rater" is an author-meaningful id (not trivially "llm"/"llm_2"), so
    // the identity ladder title-cases it instead of the "rate each row"
    // plugin gloss (elspeth-9f21f3c57d) — the same name the acknowledgement
    // card shows.
    expect(phraseFor("node:rater")).toBe("Rater");
  });

  it("keeps the plugin gloss for an id that is trivially its own plugin's name", () => {
    const state = makeComposition(1, {
      sources: {},
      nodes: [
        {
          id: "llm_2",
          node_type: "transform",
          plugin: "llm",
          input: "source",
          on_success: null,
          on_error: null,
          options: {},
        },
      ],
      outputs: [],
    });
    const phraseFor = makePhraseFor(state);
    expect(phraseFor("llm_2")).toBe("rate each row");
  });

  it("prefers a node's authored description over both its title-cased id and the plugin gloss (elspeth-9f21f3c57d)", () => {
    const state = makeComposition(1, {
      sources: {},
      nodes: [
        {
          id: "recommend_pairing",
          node_type: "transform",
          plugin: "llm",
          input: "source",
          on_success: null,
          on_error: null,
          options: {},
          description: "Ask the LLM for a complementary colour pairing for this colour.",
        },
      ],
      outputs: [],
    });
    const phraseFor = makePhraseFor(state);
    // Label register: trailing full stop dropped.
    expect(phraseFor("recommend_pairing")).toBe(
      "Ask the LLM for a complementary colour pairing for this colour",
    );
  });

  it("falls back to the neutral phrase for an id with no direct, stripped, fuzzy, or generated match", () => {
    const state = makeComposition(1, { sources: {}, nodes: [], outputs: [] });
    const phraseFor = makePhraseFor(state);
    expect(phraseFor("xyz")).toBe(UNKNOWN_COMPONENT_PHRASE);
  });

  it("guesses a phrase for a role+format-bearing generated id absent from the composition", () => {
    const phraseFor = makePhraseFor(null);
    expect(phraseFor("sink_guided_output_csv_abcd1234")).toBe("write a CSV");
    expect(phraseFor("transform_guided_xform_0_abcd1234")).toBe("process each row");
  });

  // ── elspeth-66f50ba810: fuzzy known-component match must win over the ────
  // generic role guess when the two diverge (a specific user phrase exists
  // for a *different* format than the generic guess would produce).
  it("prefers a specific fuzzy-matched component phrase over the generic role fallback (elspeth-66f50ba810)", () => {
    const state = makeComposition(1, {
      sources: {},
      nodes: [],
      outputs: [{ name: "report", plugin: "json", options: {} }],
    });
    const phraseFor = makePhraseFor(state);
    // Generic role-only guessing (role="output", no format token in the id
    // itself) would produce the DEFAULT "write the results" — but "report"
    // is a known output whose real phrase is "write a JSON file". The fuzzy
    // match on the shared 'report' token must be tried before the generic
    // guess and win.
    expect(phraseFor("output_report_a1b2c3")).toBe("write a JSON file");
    expect(phraseFor("output_report_a1b2c3")).not.toBe("write the results");
  });

  // ── elspeth-8f89b0ba34: fuzzy match must prefer the candidate with the ───
  // most matched meaningful tokens, not the first entries()-order hit.
  it("prefers the more specific (more-tokens-matched) fuzzy candidate over an earlier, less-specific one (elspeth-8f89b0ba34)", () => {
    const state = makeComposition(1, {
      sources: {},
      // Array order controls Map insertion order here (unlike sources, which
      // sort alphabetically) — "refunds_raw" is inserted BEFORE
      // "refunds_clean" so a first-match-wins bug would surface here.
      nodes: [
        {
          id: "refunds_raw",
          node_type: "transform",
          plugin: "field_mapper",
          input: "source",
          on_success: null,
          on_error: null,
          options: {},
        },
        {
          id: "refunds_clean",
          node_type: "transform",
          plugin: "llm",
          input: "source",
          on_success: null,
          on_error: null,
          options: {},
        },
      ],
      outputs: [],
    });
    const phraseFor = makePhraseFor(state);
    // "refunds_clean_v2" shares 1 meaningful token with "refunds_raw"
    // ('raw' is <4 chars and filtered) but 2 meaningful tokens with
    // "refunds_clean" ('refunds' + 'clean') — the more specific candidate
    // must win regardless of map iteration order. (Both nodes now resolve
    // to their title-cased own names — elspeth-9f21f3c57d — which keeps the
    // two candidates distinct and this assertion discriminating.)
    expect(phraseFor("refunds_clean_v2")).toBe("Refunds Clean");
    expect(phraseFor("refunds_clean_v2")).not.toBe("Refunds Raw");
  });

  // ── elspeth-ede84df6b3: a role-less generated id must not default to a ───
  // write-direction phrase; a structured component_type hint should inform
  // (and can override) the role guess when the id itself carries no role
  // token.
  it("does not guess a write-direction phrase for a role-less CSV id with no component_type hint (elspeth-ede84df6b3)", () => {
    const phraseFor = makePhraseFor(null);
    const phrase = phraseFor("csv_refunds_a1b2");
    expect(phrase).not.toBe("write a CSV");
    expect(phrase).toBe(UNKNOWN_COMPONENT_PHRASE);
  });

  it("uses the component_type hint to resolve a role-less CSV id to the read-direction phrase (elspeth-ede84df6b3)", () => {
    const phraseFor = makePhraseFor(null);
    expect(phraseFor("csv_refunds_a1b2", "source")).toBe("read your CSV");
    expect(phraseFor("csv_refunds_a1b2", "source")).not.toBe("write a CSV");
  });

  it("uses the component_type hint for a role-less JSON output id", () => {
    const phraseFor = makePhraseFor(null);
    expect(phraseFor("json_export_a1b2", "sink")).toBe("write a JSON file");
  });

  it("uses the component_type hint for a role-less transform id with no format token", () => {
    const phraseFor = makePhraseFor(null);
    expect(phraseFor("select_cols", "transform")).toBe("process each row");
  });

  it("prefers the id's own role token over a conflicting component_type hint", () => {
    // The id itself says "output"; a (hypothetically wrong) "source" hint
    // must not override an explicit role token present in the id.
    const phraseFor = makePhraseFor(null);
    expect(phraseFor("output_csv_a1b2", "source")).toBe("write a CSV");
  });

  it("prioritises an authoritative row_union type over generated id-role heuristics", () => {
    const phraseFor = makePhraseFor(null);
    expect(phraseFor("output_guided_row_union_a1b2", "row_union")).toBe(
      "wait for every branch, then preserve every branch row",
    );
  });

  it("ignores an unrecognised component_type value and falls through to the id-substring guess", () => {
    const phraseFor = makePhraseFor(null);
    expect(phraseFor("transform_csv_normalize_a1b2c3", "graph")).toBe("process each row");
  });

  it("keeps structural barrier component types semantically distinct without a live composition", () => {
    const phraseFor = makePhraseFor(null);

    expect(phraseFor("barrier_a1b2", "row_union")).toBe(
      "wait for every branch, then preserve every branch row",
    );
    expect(phraseFor("barrier_a1b2", "coalesce")).toBe(
      "merge the branches",
    );
    expect(phraseFor("barrier_a1b2", "queue")).toBe(
      "interleave the incoming rows",
    );
  });

  it("does not let a bare 'source' or 'output' component match everything via fuzzy overreach", () => {
    const state = makeComposition(1, {
      sources: {
        source: { plugin: "text", options: {} },
        refunds: { plugin: "csv", options: {} },
      },
      nodes: [],
      outputs: [],
    });
    const phraseFor = makePhraseFor(state);
    expect(phraseFor("source_csv_refunds_a1b2c3")).toBe("read your CSV");
    expect(phraseFor("source_csv_refunds_a1b2c3")).not.toBe("read your data");
  });
});

// ── makePhraseFor — defensive compiled-DAG-id strip (elspeth-9f21f3c57d) ────
//
// Compiled DAG ids embed the composer node id between a node-kind prefix and
// a 12-hex hash suffix (`config_gate_fan_out_5176d9a61403` ↔ composer node
// `fan_out`). The strip is a fallback AFTER the exact/map paths, recovers the
// embedded composer id greedily (composer ids may contain underscores), and
// re-runs the map/fuzzy resolution on it.

describe("makePhraseFor — compiled-id strip", () => {
  function stateWithNodes(nodes: NodeSpec[]) {
    return makeComposition(1, { sources: {}, nodes, outputs: [] });
  }

  it("resolves a compiled id whose embedded composer id contains underscores (greedy capture)", () => {
    const state = stateWithNodes([
      {
        id: "step_0123456789ab",
        node_type: "transform",
        plugin: "llm",
        input: "source",
        on_success: null,
        on_error: null,
        options: {},
      },
    ]);
    const phraseFor = makePhraseFor(state);
    expect(phraseFor("transform_step_0123456789ab_ab12cd34ef56")).toBe(
      "Step 0123456789ab",
    );
  });

  it("resolves a config_gate compiled id to the gate node it embeds (prefix vocabulary is compiled kinds, not node_type)", () => {
    const state = stateWithNodes([
      {
        id: "fan_out",
        node_type: "gate",
        plugin: null,
        input: "source",
        on_success: null,
        on_error: null,
        options: {},
        condition: "row.kind == 'colour'",
      },
    ]);
    const phraseFor = makePhraseFor(state);
    expect(phraseFor("config_gate_fan_out_5176d9a61403")).toBe("Fan Out");
  });

  it("still degrades an unmappable compiled id to the generic phrase", () => {
    const phraseFor = makePhraseFor(makeComposition(1, { sources: {}, nodes: [], outputs: [] }));
    expect(phraseFor("config_gate_ghost_aaaabbbbcccc")).toBe(UNKNOWN_COMPONENT_PHRASE);
  });

  it("leaves short-hash generated ids to the existing fuzzy/role ladder (12-hex suffix required)", () => {
    const phraseFor = makePhraseFor(null);
    // 8-hex suffixes (the guided generated-id shape pinned above) must not
    // enter the strip; the role/format guess still answers.
    expect(phraseFor("transform_guided_xform_0_abcd1234")).toBe("process each row");
    expect(phraseFor("sink_guided_output_csv_abcd1234")).toBe("write a CSV");
  });
});

// ── the session-2e0c8ea3 banner shape (elspeth-9f21f3c57d) ──────────────────

describe("humaniseValidationMessage — compiled-id edge dump names the real steps", () => {
  it("renders both composer step names for the exact session-2e0c8ea3 banner shape", () => {
    const state = makeComposition(1, {
      sources: {},
      nodes: [
        {
          id: "fan_out",
          node_type: "gate",
          plugin: null,
          input: "source",
          on_success: null,
          on_error: null,
          options: {},
          condition: "row.kind == 'colour'",
        },
        {
          id: "recommend_pairing",
          node_type: "transform",
          plugin: "llm",
          input: "fan_out",
          on_success: null,
          on_error: null,
          options: {},
          description: "Ask the LLM for a complementary colour pairing for this colour.",
        },
      ],
      outputs: [],
    });
    const phraseFor = makePhraseFor(state);
    const finding = humaniseValidationMessage(
      "Schema contract violation: edge 'config_gate_fan_out_5176d9a61403' → 'transform_recommend_pairing_5176d9a61403'\n" +
        "  Consumer (llm) requires fields: ['colour']\n" +
        "  Producer (gate) guarantees: ['kind']\n" +
        "  Missing fields: ['colour']",
      phraseFor,
    );
    expect(finding.headline).toBe(
      'Two steps aren\'t connected correctly: the "Fan Out" step\'s output ' +
        'doesn\'t match what "Ask the LLM for a complementary colour pairing for this colour" expects.',
    );
    expect(finding.headline).not.toContain("this step");
    expect(finding.headline).not.toContain("rate each row");
    expect(finding.namedSteps).toEqual([
      "Fan Out",
      "Ask the LLM for a complementary colour pairing for this colour",
    ]);
  });

  it("still degrades to the generic phrases when neither compiled id is mappable", () => {
    const phraseFor = makePhraseFor(makeComposition(1, { sources: {}, nodes: [], outputs: [] }));
    const finding = humaniseValidationMessage(
      "Schema contract violation: edge 'config_gate_ghost_aaaabbbbcccc' → 'coalesce_phantom_aaaabbbbcccc'\n" +
        "  Missing fields: ['x']",
      phraseFor,
    );
    expect(finding.headline).toBe(
      'Two steps aren\'t connected correctly: the "this step" step\'s output ' +
        'doesn\'t match what "this step" expects.',
    );
    expect(finding.namedSteps).toEqual([]);
  });
});

// ── formatFindingBody ───────────────────────────────────────────────────────

describe("formatFindingBody", () => {
  it("prefixes the possessive step phrase when the finding is attributed and not raw-humanised", () => {
    const body = formatFindingBody(
      1,
      "problem to fix",
      { headline: "Prompt is empty", raw: null, namedSteps: [] },
      "rater",
      "transform",
      (id) => (id === "rater" ? "rate each row" : UNKNOWN_COMPONENT_PHRASE),
    );
    expect(body).toBe("1 problem to fix — 'rate each row': Prompt is empty");
  });

  it("omits the possessive prefix for a null component_id (settings-level finding)", () => {
    const body = formatFindingBody(
      1,
      "problem to fix",
      { headline: "Pipeline has no sink", raw: null, namedSteps: [] },
      null,
      null,
      () => UNKNOWN_COMPONENT_PHRASE,
    );
    expect(body).toBe("1 problem to fix — Pipeline has no sink");
  });

  it("omits the possessive prefix when the humanised headline already names this step", () => {
    const body = formatFindingBody(
      2,
      "problems to fix",
      {
        headline: "Two steps aren't connected correctly: …",
        raw: "Schema contract violation: …",
        namedSteps: ["rate each row", "write a CSV"],
      },
      "rater",
      "transform",
      () => "rate each row",
    );
    expect(body).toBe("2 problems to fix — Two steps aren't connected correctly: …");
  });

  it("prefixes the resolved step name on a humanised finding whose headline could not name it (elspeth-9f21f3c57d)", () => {
    // The message's own ids were unmappable (headline says "this step") but
    // the finding's component_id resolves — the one name we have must reach
    // the user rather than being suppressed with the raw dump.
    const body = formatFindingBody(
      1,
      "problem to fix",
      {
        headline: 'Two steps aren\'t connected correctly: the "this step" step\'s output doesn\'t match what "this step" expects.',
        raw: "Schema contract violation: …",
        namedSteps: [],
      },
      "recommend_pairing",
      "transform",
      () => "Recommend Pairing",
    );
    expect(body).toBe(
      "1 problem to fix — 'Recommend Pairing': Two steps aren't connected correctly: " +
        'the "this step" step\'s output doesn\'t match what "this step" expects.',
    );
  });

  it("never prefixes the generic phrase onto a humanised finding", () => {
    const body = formatFindingBody(
      1,
      "problem to fix",
      { headline: "A step isn't connected correctly: …", raw: "Schema contract violation: …", namedSteps: [] },
      "ghost",
      null,
      () => UNKNOWN_COMPONENT_PHRASE,
    );
    expect(body).toBe("1 problem to fix — A step isn't connected correctly: …");
  });

  it("never prefixes a review-pending ('self'-naming) finding", () => {
    const body = formatFindingBody(
      1,
      "problem to fix",
      { headline: "The Summarise step is waiting for your review.", raw: "pipeline_decision review pending …", namedSteps: "self" },
      "rater",
      "transform",
      () => "Rater",
    );
    expect(body).toBe("1 problem to fix — The Summarise step is waiting for your review.");
  });

  it("threads component_type into the phraseFor call so a role-less id resolves correctly", () => {
    const calls: Array<[string | null, string | null | undefined]> = [];
    const phraseFor = (id: string | null, componentType?: string | null): string => {
      calls.push([id, componentType]);
      return "read your CSV";
    };
    formatFindingBody(
      1,
      "problem to fix",
      { headline: "Missing field", raw: null, namedSteps: [] },
      "csv_refunds_a1b2",
      "source",
      phraseFor,
    );
    expect(calls).toEqual([["csv_refunds_a1b2", "source"]]);
  });
});

describe("clientWireBlockerMessages", () => {
  it("drops the guided deferred-commit placeholder status", () => {
    expect(clientWireBlockerMessages(["guided_composition_invalid"])).toEqual([]);
  });

  it("keeps real validation messages while dropping the placeholder", () => {
    expect(
      clientWireBlockerMessages(["guided_composition_invalid", "No source configured."]),
    ).toEqual(["No source configured."]);
  });

  it("passes ordinary error lists through untouched", () => {
    expect(clientWireBlockerMessages(["No sinks configured."])).toEqual(["No sinks configured."]);
  });
});
