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
    resolved_prompt_template_hash: null,
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
