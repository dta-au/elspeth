// ============================================================================
// promptTemplateDisplay — pure-helper coverage for the resolved-prompt
// rendering chain (elspeth-990f5ea562): structured parts → node
// prompt_template → event llm_draft.
// ============================================================================

import { describe, it, expect } from "vitest";
import {
  PENDING_INTERPRETATION_DISPLAY_TEXT,
  resolvePromptDisplaySegments,
} from "./promptTemplateDisplay";
import type { CompositionState, NodeSpec } from "@/types/index";
import type { InterpretationEvent } from "@/types/interpretation";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";

function makeEvent(
  overrides: Partial<InterpretationEvent> = {},
): InterpretationEvent {
  return {
    id: "evt-1",
    session_id: "sess-1",
    composition_state_id: "state-1",
    affected_node_id: "node-1",
    tool_call_id: "tool-1",
    user_term: null,
    kind: "llm_prompt_template",
    llm_draft: "Summarise pending interpretation for an auditor.",
    accepted_value: null,
    choice: "pending",
    created_at: "2026-05-18T00:00:00Z",
    resolved_at: null,
    actor: "user:owner:u-1",
    interpretation_source: "user_approved",
    model_identifier: "anthropic/claude-opus-4-7",
    model_version: "20260518",
    provider: "anthropic",
    composer_skill_hash: "deadbeef",
    arguments_hash: null,
    hash_domain_version: null,
    runtime_model_identifier_at_resolve: null,
    runtime_model_version_at_resolve: null,
    approved_prompt_artifact_hash: null,
    ...overrides,
  };
}

function makeNode(options: Record<string, unknown>): NodeSpec {
  return {
    id: "node-1",
    node_type: "transform",
    plugin: "llm",
    input: "rows",
    on_success: null,
    on_error: null,
    options,
  };
}

function makeState(nodes: NodeSpec[]): CompositionState {
  return {
    id: "state-1",
    ...compositionStateAuthorityFields,
    version: 2,
    sources: {},
    nodes,
    edges: [],
    outputs: [],
    metadata: { name: null, description: null },
  };
}

const STRUCTURED_PARTS = [
  { kind: "text", text: "Summarise " },
  { kind: "interpretation_ref", requirement_id: "req-1" },
  { kind: "text", text: " for an auditor." },
];

function makeRequirement(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: "req-1",
    kind: "vague_term",
    user_term: "punchy",
    status: "pending",
    draft: "short and direct",
    event_id: "evt-vague-1",
    accepted_value: null,
    accepted_artifact_hash: null,
    resolved_prompt_template_hash: null,
    ...overrides,
  };
}

describe("resolvePromptDisplaySegments — structured parts", () => {
  it("substitutes a resolved requirement's accepted_value as a 'resolved' segment", () => {
    const state = makeState([
      makeNode({
        prompt_template_parts: STRUCTURED_PARTS,
        interpretation_requirements: [
          makeRequirement({ status: "resolved", accepted_value: "concise and neutral" }),
        ],
      }),
    ]);
    const result = resolvePromptDisplaySegments(state, makeEvent());
    expect(result.usedFallback).toBe(false);
    expect(result.segments).toEqual([
      { kind: "text", text: "Summarise " },
      { kind: "resolved", text: "concise and neutral" },
      { kind: "text", text: " for an auditor." },
    ]);
  });

  it("renders a pending requirement's draft as a 'pending' segment", () => {
    const state = makeState([
      makeNode({
        prompt_template_parts: STRUCTURED_PARTS,
        interpretation_requirements: [makeRequirement()],
      }),
    ]);
    const result = resolvePromptDisplaySegments(state, makeEvent());
    expect(result.usedFallback).toBe(false);
    expect(result.segments[1]).toEqual({
      kind: "pending",
      text: "short and direct",
    });
  });

  it("falls back to the pending-interpretation literal when the draft is missing", () => {
    const state = makeState([
      makeNode({
        prompt_template_parts: STRUCTURED_PARTS,
        interpretation_requirements: [makeRequirement({ draft: null })],
      }),
    ]);
    const result = resolvePromptDisplaySegments(state, makeEvent());
    expect(result.segments[1]).toEqual({
      kind: "pending",
      text: PENDING_INTERPRETATION_DISPLAY_TEXT,
    });
  });
});

describe("resolvePromptDisplaySegments — fallback chain", () => {
  it("falls back to the node's prompt_template on malformed parts", () => {
    const state = makeState([
      makeNode({
        prompt_template: "Summarise short and direct for an auditor.",
        prompt_template_parts: [{ kind: "mystery" }],
        interpretation_requirements: [makeRequirement()],
      }),
    ]);
    const result = resolvePromptDisplaySegments(state, makeEvent());
    expect(result.usedFallback).toBe(true);
    expect(result.segments).toEqual([
      { kind: "text", text: "Summarise short and direct for an auditor." },
    ]);
  });

  it("refuses to render a 'resolved' requirement that carries no accepted value", () => {
    // The safety arm of the chain (TQ-7): a payload claiming status
    // "resolved" while accepted_value is null is malformed, and it is
    // precisely the shape that must NOT reach an approval surface as an
    // authoritative substituted value. Without this the resolver would push
    // a "resolved" segment with empty text, showing the approver a prompt
    // with a SILENTLY BLANKED slot — worse than the honest fallback, because
    // the card's entire purpose is showing what actually runs.
    const state = makeState([
      makeNode({
        prompt_template: "Summarise short and direct for an auditor.",
        prompt_template_parts: STRUCTURED_PARTS,
        interpretation_requirements: [
          makeRequirement({ status: "resolved", accepted_value: null }),
        ],
      }),
    ]);
    const result = resolvePromptDisplaySegments(state, makeEvent());
    expect(result.usedFallback).toBe(true);
    expect(result.segments).toEqual([
      { kind: "text", text: "Summarise short and direct for an auditor." },
    ]);
    // No slot segment of any kind survived into the render.
    expect(result.segments.every((s) => s.kind === "text")).toBe(true);
  });

  it("falls back to prompt_template on malformed requirements (non-list)", () => {
    const state = makeState([
      makeNode({
        prompt_template: "Current rendered template.",
        prompt_template_parts: STRUCTURED_PARTS,
        interpretation_requirements: { "req-1": makeRequirement() },
      }),
    ]);
    const result = resolvePromptDisplaySegments(state, makeEvent());
    expect(result.usedFallback).toBe(true);
    expect(result.segments).toEqual([
      { kind: "text", text: "Current rendered template." },
    ]);
  });

  it("falls back when a part references an unknown requirement", () => {
    const state = makeState([
      makeNode({
        prompt_template: "Current rendered template.",
        prompt_template_parts: [
          { kind: "interpretation_ref", requirement_id: "req-unknown" },
        ],
        interpretation_requirements: [makeRequirement()],
      }),
    ]);
    const result = resolvePromptDisplaySegments(state, makeEvent());
    expect(result.usedFallback).toBe(true);
    expect(result.segments[0]?.text).toBe("Current rendered template.");
  });

  it("legacy no-parts node: falls back to its prompt_template string", () => {
    const state = makeState([
      makeNode({ prompt_template: "Legacy rendered template." }),
    ]);
    const result = resolvePromptDisplaySegments(state, makeEvent());
    expect(result.usedFallback).toBe(true);
    expect(result.segments).toEqual([
      { kind: "text", text: "Legacy rendered template." },
    ]);
  });

  it("node absent from the state: falls back to event.llm_draft", () => {
    const state = makeState([]);
    const event = makeEvent();
    const result = resolvePromptDisplaySegments(state, event);
    expect(result.usedFallback).toBe(true);
    expect(result.segments).toEqual([
      { kind: "text", text: event.llm_draft },
    ]);
  });

  it("null state: falls back to event.llm_draft ('' when null)", () => {
    const withDraft = resolvePromptDisplaySegments(null, makeEvent());
    expect(withDraft.usedFallback).toBe(true);
    expect(withDraft.segments[0]?.text).toBe(
      "Summarise pending interpretation for an auditor.",
    );

    const noDraft = resolvePromptDisplaySegments(
      null,
      makeEvent({ llm_draft: null }),
    );
    expect(noDraft.segments).toEqual([{ kind: "text", text: "" }]);
  });

  it("null affected_node_id never matches a node", () => {
    const state = makeState([
      makeNode({ prompt_template: "Should not be used." }),
    ]);
    const result = resolvePromptDisplaySegments(
      state,
      makeEvent({ affected_node_id: null }),
    );
    expect(result.segments[0]?.text).toBe(
      "Summarise pending interpretation for an auditor.",
    );
  });
});

// ── Multi-query prompt surface ──────────────────────────────────────────────
//
// A multi-query node's review anchor (interpretation_state.py
// MultiQueryPromptSurface.anchor_hash) covers the system prompt, every
// well-formed query's name + template override, and the node-level template
// skeleton. The event's llm_draft is bounded to 8000 chars and frozen at
// staging, so the card renders the COMPLETE surface from live node options.

const SURFACE_HEAD = [
  "Multi-query LLM node: for every row the model receives one call per query below.",
  "",
  "System prompt (sent with every query):",
];

/** Frozen staging-time draft: different from every live render below. */
const FROZEN_SURFACE_DRAFT = [
  ...SURFACE_HEAD,
  "(none)",
  "",
  "Query 'frozen':",
  "Frozen staging-time text.",
  "",
  "Node-level prompt_template, used by queries without their own template: frozen",
  "Summarise pending interpretation.",
].join("\n");

const SLOT_PARTS = [
  { kind: "text", text: "Summarise " },
  { kind: "interpretation_ref", requirement_id: "req-1" },
  { kind: "text", text: "." },
];

function displayText(result: { segments: { text: string }[] }): string {
  return result.segments.map((segment) => segment.text).join("");
}

function surfaceEvent(): InterpretationEvent {
  return makeEvent({ llm_draft: FROZEN_SURFACE_DRAFT });
}

describe("resolvePromptDisplaySegments — multi-query prompt surface", () => {
  it("renders byte-identical to the backend review draft when nothing is bounded or resolved", () => {
    // Expected text produced by the backend on the same options:
    // multi_query_prompt_surface_from_options(opts).render_for_review()
    // (list-form queries; the third entry is an object without a name, so it
    // is labelled by its list index '#2'). The pending slot has no draft, so
    // it shows the same "pending interpretation" literal the backend masks.
    const backendDraft =
      "Multi-query LLM node: for every row the model receives one call per query below.\n\nSystem prompt (sent with every query):\nReply briefly.\n\nQuery 'a':\nTell me {{ row.x }}.\n\nQuery 'b': uses the node-level prompt_template (below).\n\nQuery '#2': (template value is not text; plugin validation rejects this node)\n\nNode-level prompt_template, used by queries without their own template: b\nSummarise pending interpretation.";
    const state = makeState([
      makeNode({
        prompt_template: "Summarise pending interpretation.",
        prompt_template_parts: SLOT_PARTS,
        interpretation_requirements: [makeRequirement({ draft: null })],
        system_prompt: "Reply briefly.",
        queries: [
          { name: "a", template: "Tell me {{ row.x }}." },
          { name: "b" },
          { bad: 1, template: 5 },
        ],
      }),
    ]);
    const result = resolvePromptDisplaySegments(state, surfaceEvent());
    expect(result.usedFallback).toBe(false);
    expect(displayText(result)).toBe(backendDraft);
  });

  it("shows a resolved slot's accepted value inside the node-level template (not the frozen draft)", () => {
    const state = makeState([
      makeNode({
        prompt_template: "Summarise concise and neutral.",
        prompt_template_parts: SLOT_PARTS,
        interpretation_requirements: [
          makeRequirement({ status: "resolved", accepted_value: "concise and neutral" }),
        ],
        queries: { summary: {} },
      }),
    ]);
    const result = resolvePromptDisplaySegments(state, surfaceEvent());
    expect(result.usedFallback).toBe(false);
    const text = displayText(result);
    expect(text).toContain("Summarise concise and neutral.");
    expect(text).not.toContain("pending interpretation");
    expect(result.segments).toContainEqual({
      kind: "resolved",
      text: "concise and neutral",
    });
  });

  it("keeps a pending slot marked inside the node-level template", () => {
    const state = makeState([
      makeNode({
        prompt_template: "Summarise pending interpretation.",
        prompt_template_parts: SLOT_PARTS,
        interpretation_requirements: [makeRequirement()],
        queries: { summary: {} },
      }),
    ]);
    const result = resolvePromptDisplaySegments(state, surfaceEvent());
    expect(result.segments).toContainEqual({
      kind: "pending",
      text: "short and direct",
    });
  });

  it("shows all 40 queries, in order, each with its complete template", () => {
    const queries: Record<string, unknown> = {};
    const templates: string[] = [];
    for (let index = 0; index < 40; index += 1) {
      const template = `Query number ${index} asks about {{ row.field_${index} }}: ${"detail ".repeat(40)}end of query ${index}.`;
      templates.push(template);
      queries[`q_${index}`] = { template };
    }
    const state = makeState([
      makeNode({
        prompt_template: "Unused node-level text.",
        prompt_template_parts: [{ kind: "text", text: "Unused node-level text." }],
        interpretation_requirements: [],
        queries,
      }),
    ]);
    const result = resolvePromptDisplaySegments(state, surfaceEvent());
    const text = displayText(result);
    expect(templates[0].length).toBeGreaterThan(300);
    let previous = -1;
    templates.forEach((template, index) => {
      const at = text.indexOf(`Query 'q_${index}':\n${template}\n`);
      expect(at).toBeGreaterThan(previous);
      previous = at;
    });
    expect(text).not.toContain("not shown");
    expect(text).not.toContain("not listed");
  });

  it("shows an unused node-level template's text, and the text follows an edit", () => {
    const stateWith = (nodeText: string) =>
      makeState([
        makeNode({
          prompt_template: nodeText,
          prompt_template_parts: [{ kind: "text", text: nodeText }],
          interpretation_requirements: [],
          queries: { a: { template: "Query A text." } },
        }),
      ]);
    const before = displayText(
      resolvePromptDisplaySegments(stateWith("Original dead text."), surfaceEvent()),
    );
    const after = displayText(
      resolvePromptDisplaySegments(stateWith("Edited dead text."), surfaceEvent()),
    );
    expect(before).toContain(
      "Node-level prompt_template: not used (every query supplies its own template).\nOriginal dead text.",
    );
    expect(after).toContain("\nEdited dead text.");
    expect(after).not.toContain("Original dead text.");
  });

  it("labels an unused node-level template beside a non-text query template", () => {
    const state = makeState([
      makeNode({
        prompt_template: "Dead text.",
        prompt_template_parts: [{ kind: "text", text: "Dead text." }],
        interpretation_requirements: [],
        queries: { a: { template: "A." }, b: { template: 5 } },
      }),
    ]);
    const text = displayText(resolvePromptDisplaySegments(state, surfaceEvent()));
    expect(text).toContain(
      "Query 'b': (template value is not text; plugin validation rejects this node)",
    );
    expect(text).toContain(
      "Node-level prompt_template: not used (no query falls back to it).\nDead text.",
    );
  });

  it("shows the system prompt, and '(none)' when it is absent or not text", () => {
    const stateWith = (systemPrompt: unknown) =>
      makeState([
        makeNode({
          prompt_template: "Node text.",
          prompt_template_parts: [{ kind: "text", text: "Node text." }],
          interpretation_requirements: [],
          system_prompt: systemPrompt,
          queries: { a: { template: "A." } },
        }),
      ]);
    expect(
      displayText(resolvePromptDisplaySegments(stateWith("Answer in French."), surfaceEvent())),
    ).toContain("System prompt (sent with every query):\nAnswer in French.\n");
    expect(
      displayText(resolvePromptDisplaySegments(stateWith(42), surfaceEvent())),
    ).toContain("System prompt (sent with every query):\n(none)\n");
  });

  it("labels a query that uses the node-level template and names it in the node-level label", () => {
    const state = makeState([
      makeNode({
        prompt_template: "Shared text.",
        prompt_template_parts: [{ kind: "text", text: "Shared text." }],
        interpretation_requirements: [],
        queries: {
          own: { template: "Own text." },
          shared_one: { template: null },
          shared_two: {},
        },
      }),
    ]);
    const text = displayText(resolvePromptDisplaySegments(state, surfaceEvent()));
    expect(text).toContain("Query 'own':\nOwn text.\n");
    expect(text).toContain(
      "Query 'shared_one': uses the node-level prompt_template (below).",
    );
    expect(text).toContain(
      "Query 'shared_two': uses the node-level prompt_template (below).",
    );
    expect(text).toContain(
      "Node-level prompt_template, used by queries without their own template: shared_one, shared_two\nShared text.",
    );
  });

  it("counts only object query entries and labels unnamed list entries by their list index", () => {
    const listState = makeState([
      makeNode({
        prompt_template: "Node text.",
        prompt_template_parts: [{ kind: "text", text: "Node text." }],
        interpretation_requirements: [],
        queries: ["junk", { name: "", template: "T1." }, ["array"], { name: "q3", template: "T3." }],
      }),
    ]);
    const listText = displayText(resolvePromptDisplaySegments(listState, surfaceEvent()));
    expect(listText).toContain("Query '#1':\nT1.\n\nQuery 'q3':\nT3.\n");
    expect(listText).not.toContain("junk");
    expect(listText).not.toContain("array");

    const mappingState = makeState([
      makeNode({
        prompt_template: "Node text.",
        prompt_template_parts: [{ kind: "text", text: "Node text." }],
        interpretation_requirements: [],
        queries: { skipped_array: ["x"], skipped_null: null, kept: { template: "K." } },
      }),
    ]);
    const mappingText = displayText(resolvePromptDisplaySegments(mappingState, surfaceEvent()));
    expect(mappingText).toContain("Query 'kept':\nK.\n");
    expect(mappingText).not.toContain("skipped_");
  });

  it("renders the node-level prompt_template text, flagged as a fallback, when its parts cannot be broken out", () => {
    const state = makeState([
      makeNode({
        prompt_template: "Stored node text.",
        prompt_template_parts: [{ kind: "mystery" }],
        interpretation_requirements: [],
        queries: { a: { template: "A." } },
      }),
    ]);
    const result = resolvePromptDisplaySegments(state, surfaceEvent());
    expect(result.usedFallback).toBe(true);
    const text = displayText(result);
    expect(text).toContain("Query 'a':\nA.\n");
    expect(text).toContain("\nStored node text.");
    expect(text).not.toContain("Frozen staging-time text.");
  });

  it("falls back to the event's llm_draft when the multi-query options are malformed", () => {
    // Well-formed queries but no string prompt_template and no parts: the
    // backend reads no surface, and nothing live can be rendered.
    const state = makeState([
      makeNode({ prompt_template: 7, queries: { a: { template: "A." } } }),
    ]);
    const result = resolvePromptDisplaySegments(state, surfaceEvent());
    expect(result.usedFallback).toBe(true);
    expect(result.segments).toEqual([
      { kind: "text", text: FROZEN_SURFACE_DRAFT },
    ]);
  });

  it("a queries value with no well-formed entry is a single-prompt node", () => {
    const state = makeState([
      makeNode({
        prompt_template: "Summarise concise and neutral.",
        prompt_template_parts: SLOT_PARTS,
        interpretation_requirements: [
          makeRequirement({ status: "resolved", accepted_value: "concise and neutral" }),
        ],
        queries: {},
      }),
    ]);
    const result = resolvePromptDisplaySegments(state, surfaceEvent());
    expect(result.usedFallback).toBe(false);
    expect(displayText(result)).toBe("Summarise concise and neutral.");
  });
});
