import { describe, expect, it } from "vitest";

import {
  clientWireBlockerMessages,
  formatFindingBody,
  humaniseValidationMessage,
  humaniseExecutionError,
  humaniseValidationWarning,
  humaniseValidationSuggestion,
  makePhraseFor,
} from "./validationHumaniser";
import {
  COLLECTOR_PHRASE,
  UNKNOWN_COMPONENT_PHRASE,
} from "@/components/chat/guided/pipelineGloss";
import { makeComposition } from "@/test/composerFixtures";
import type { NodeSpec } from "@/types/index";

// ── humaniseValidationMessage ───────────────────────────────────────────────

describe("humaniseValidationMessage", () => {
  const phraseFor = (id: string | null): string => id === "actual" ? "Actual step" : UNKNOWN_COMPONENT_PHRASE;

  it("uses code and the single component, retaining contradictory prose as detail", () => {
    const message = "Schema contract violation: edge 'invented' → 'other'";
    const finding = humaniseValidationMessage({ message, error_code: "schema_contract_violation", component: "actual" }, phraseFor);
    expect(finding.headline).toBe('A step has incompatible data: "Actual step".');
    expect(finding.namedSteps).toEqual(["Actual step"]);
    expect(finding.raw).toBe(message);
    expect(finding.headline).not.toContain("invented");
  });

  it.each([null, "", "unknown_code"])("does not classify prose with code %s", (error_code) => {
    const message = "Schema contract violation: edge 'invented' → 'other'";
    expect(humaniseValidationMessage({ message, error_code, component: "actual" }, phraseFor)).toEqual({ headline: message, raw: null, namedSteps: [] });
  });

  it("uses a generic contract headline without an identified component", () => {
    expect(humaniseValidationMessage({message: "arbitrary detail", error_code: "schema_contract_violation", component: null}, phraseFor).headline).toBe("The pipeline has incompatible data between steps.");
  });

  it("uses the structured review code even when prose has another identifier", () => {
    const finding = humaniseValidationMessage({message: "review pending for transform 'invented'", error_code: "interpretation_review_pending", component: "actual"}, phraseFor, (id) => id === "actual" ? "Summarise" : null);
    expect(finding.headline).toBe("The Summarise step is waiting for your review.");
  });

  it("renders a generic review headline without a resolvable component", () => {
    expect(humaniseValidationMessage({message: "detail", error_code: "interpretation_review_pending", component: null}, phraseFor).headline).toBe("A step is waiting for your review.");
  });
});

describe("validation adapters", () => {
  const message = "Schema contract violation: edge 'wrong' → 'also_wrong'";
  const phraseFor = (id: string | null): string => id === "owned" ? "Owned step" : UNKNOWN_COMPONENT_PHRASE;

  it("preserves warning prose without inventing an error code", () => {
    expect(humaniseValidationWarning({ message, component_id: "owned", component_type: "transform", suggestion: null })).toEqual({ headline: message, raw: null, namedSteps: [] });
  });

  it("adapts the actual execution error fields", () => {
    const finding = humaniseExecutionError({message, component_id: "owned", component_type: "transform", suggestion: null, error_code: "schema_contract_violation"}, phraseFor);
    expect(finding.namedSteps).toEqual(["Owned step"]);
    expect(finding.raw).toBe(message);
  });

  it("does not invent identity when an execution error has no code", () => {
    expect(humaniseExecutionError({message, component_id: "owned", component_type: "transform", suggestion: null}, phraseFor).headline).toBe(message);
  });

  it("adapts a suggestion without interpreting its severity or prose", () => {
    const finding = humaniseValidationSuggestion({message, component: "owned", severity: "info", error_code: "schema_contract_violation"}, phraseFor);
    expect(finding.namedSteps).toEqual(["Owned step"]);
    expect(finding.raw).toBe(message);
    expect(humaniseValidationSuggestion({message, component: "owned", severity: "info"}, phraseFor).headline).toBe(message);
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

  it("prioritises an authoritative collector type over generated id-role heuristics", () => {
    const phraseFor = makePhraseFor(null);

    expect(phraseFor("output_guided_collector_a1b2", "collector")).toBe(
      COLLECTOR_PHRASE,
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
  it("filters the structured guided status regardless of prose", () => {
    expect(clientWireBlockerMessages([{message: "placeholder details", error_code: "guided_composition_invalid", component: null}])).toEqual([]);
  });

  it("does not hide a message that only resembles the guided status", () => {
    const error = {message: "guided_composition_invalid", error_code: null, component: null};
    expect(clientWireBlockerMessages([error])).toEqual([error]);
  });

  it("preserves ordinary structured blockers", () => {
    const error = {message: "No source configured.", error_code: "source_missing", component: "source"};
    expect(clientWireBlockerMessages([{message: "placeholder", error_code: "guided_composition_invalid", component: null}, error])).toEqual([error]);
  });
});
