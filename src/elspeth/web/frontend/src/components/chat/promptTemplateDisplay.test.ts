// ============================================================================
// promptTemplateDisplay — pure-helper coverage for rendering a prompt review
// card from the server's stored review snapshot (never browser state).
// ============================================================================

import { describe, it, expect } from "vitest";
import { promptReviewDisplay, promptSlotStatus } from "./promptTemplateDisplay";
import type { InterpretationEvent } from "@/types/interpretation";
import type { PromptReviewSnapshotResponse } from "@/types/interpretation";

type Binding = PromptReviewSnapshotResponse["bindings"][number];

function snapshot(
  authoredSurface: Record<string, unknown>,
  overrides: Partial<PromptReviewSnapshotResponse> = {},
): PromptReviewSnapshotResponse {
  return {
    version: 1,
    session_id: "sess-1",
    node_id: "node-1",
    event_id: "evt-1",
    authored_surface: authoredSurface,
    bindings: [],
    anchor: "b".repeat(64),
    effective_artifact_hash: null,
    is_live_review: true,
    ...overrides,
  };
}

function binding(fieldPath: string, text: string, filename = "review.txt"): Binding {
  return {
    field_path: fieldPath,
    blob_id: "blob-1",
    content_sha256: "a".repeat(64),
    encoding: "utf-8",
    byte_length: text.length,
    filename,
    creation_modality: "LLM_GENERATED",
    created_by: "assistant",
    provenance: {},
    text,
  };
}

const MARKER = { blob_ref: "blob-1", mode: "inline_content", sha256: "a".repeat(64) };

function display(authoredSurface: Record<string, unknown>) {
  return promptReviewDisplay("evt-1", snapshot(authoredSurface));
}

describe("promptReviewDisplay — single prompt", () => {
  it("shows the snapshot's template and system prompt", () => {
    const result = display({ system_prompt: "You are a careful reviewer.", prompt_template: "Summarise {{ row.text }}." });
    expect(result).toEqual({
      reviewAvailable: true,
      segments: [{ kind: "text", text: "Summarise {{ row.text }}." }],
      text: "Summarise {{ row.text }}.",
      systemPrompt: "You are a careful reviewer.",
      hasPendingSlot: false,
    });
  });

  it("shows a structured prompt's unresolved slot in the backend's own wording, flagged", () => {
    // A structured prompt is stored with PENDING_INTERPRETATION_AUTHORING_TEXT
    // at each unresolved slot (interpretation_state.py _render_prompt_parts).
    const result = display({ prompt_template: "Summarise pending interpretation for an auditor." });
    expect(result.text).toBe("Summarise pending interpretation for an auditor.");
    expect(result.hasPendingSlot).toBe(true);
  });

  it("never shows a raw {{interpretation: term}} placeholder: it reads as the pending-slot wording", () => {
    const result = display({
      prompt_template: "Rate how {{interpretation:cool}} this is, then {{ interpretation : punchy }} it.",
    });
    expect(result.text).toBe("Rate how pending interpretation this is, then pending interpretation it.");
    expect(result.text).not.toContain("{{");
    expect(result.hasPendingSlot).toBe(true);
    const multi = display({ prompt_template: "Shared.", queries: [{ name: "q", template: "Use {{interpretation:tone}}." }] });
    expect(multi.text).toContain("Query 'q':\nUse pending interpretation.\n");
  });

  it("leaves a bound file's text verbatim, placeholder-like text included", () => {
    const result = promptReviewDisplay("evt-1", snapshot(
      { prompt_template: MARKER },
      { bindings: [binding("prompt_template", "Literal {{interpretation:x}} in a file.")] },
    ));
    expect(result.text).toContain("Literal {{interpretation:x}} in a file.");
  });

  it("reports no system prompt as null", () => {
    expect(display({ prompt_template: "T." }).systemPrompt).toBeNull();
    expect(display({ prompt_template: "T.", system_prompt: 42 }).systemPrompt).toBeNull();
  });

  it("is not approvable without a text template", () => {
    for (const surface of [{}, { prompt_template: 7 }, { prompt_template: null }]) {
      expect(display(surface).reviewAvailable).toBe(false);
    }
  });

  it("is not approvable for a snapshot of another event", () => {
    const result = promptReviewDisplay("evt-other", snapshot({ prompt_template: "T." }));
    expect(result.reviewAvailable).toBe(false);
    expect(result.text).not.toContain("T.");
  });

  it("does not gate an inline snapshot on is_live_review (a slot resolve rewrites the live text)", () => {
    const result = promptReviewDisplay("evt-1", snapshot({ prompt_template: "T." }, { is_live_review: false }));
    expect(result.reviewAvailable).toBe(true);
  });
});

describe("promptReviewDisplay — file-backed", () => {
  it("shows complete escaped file text and its label in the bound slot", () => {
    const text = "  <unsafe> & exact\n\nline  ";
    const result = promptReviewDisplay("evt-1", snapshot(
      { prompt_template: MARKER, system_prompt: "Inline system." },
      { bindings: [binding("prompt_template", text)] },
    ));
    expect(result.reviewAvailable).toBe(true);
    expect(result.text).toBe(`File: review.txt (prompt_template)\n\n${text}`);
    expect(result.systemPrompt).toBe("Inline system.");
  });

  it("places a bound system prompt's text", () => {
    const result = promptReviewDisplay("evt-1", snapshot(
      { prompt_template: "User.", system_prompt: MARKER },
      { bindings: [binding("system_prompt", "Bound system.", "system.md")] },
    ));
    expect(result.systemPrompt).toBe("Bound system.");
    expect(result.text).toBe("File: system.md (system_prompt)\n\nUser.");
  });

  it.each([
    ["list", [{ name: "classify", template: MARKER }], "queries.0.template"],
    ["mapping", { classify: { template: MARKER } }, "queries.classify.template"],
  ])("places a %s-form query's bound text by the server's slot path", (_form, queries, fieldPath) => {
    const result = promptReviewDisplay("evt-1", snapshot(
      { system_prompt: "S.", queries },
      { bindings: [binding(fieldPath, "Bound query text.")] },
    ));
    expect(result.reviewAvailable).toBe(true);
    expect(result.text).toContain(`File: review.txt (${fieldPath})\n\n`);
    expect(result.text).toContain("Query 'classify':\nBound query text.\n");
  });

  it("is not approvable once the bound files no longer match the live node", () => {
    const result = promptReviewDisplay("evt-1", snapshot(
      { prompt_template: MARKER },
      { bindings: [binding("prompt_template", "Old file text.")], is_live_review: false },
    ));
    expect(result.reviewAvailable).toBe(false);
    expect(result.text).not.toContain("Old file text.");
  });
});

// ── Multi-query prompt surface ──────────────────────────────────────────────
//
// A multi-query node's review anchor (interpretation_state.py
// MultiQueryPromptSurface.anchor_hash) covers the system prompt, every
// well-formed query's name + template override, and the node-level template.
// The event's llm_draft is bounded to 8000 chars, so the card renders the
// COMPLETE surface from the snapshot.

describe("promptReviewDisplay — multi-query prompt surface", () => {
  it.each(["mapping", "list"])(
    "renders every complete query without a shared template (%s form)",
    (form) => {
      const entries = Array.from({ length: 6 }, (_, index) => ({
        name: `query_${index}`,
        template: `Complete instruction ${index}: ${"detail ".repeat(250)}end ${index}.`,
      }));
      const queries = form === "list"
        ? entries
        : Object.fromEntries(entries.map((entry) => [entry.name, { template: entry.template }]));
      const result = display({ system_prompt: "Shared system instruction.", queries });
      expect(result.text.length).toBeGreaterThan(8000);
      expect(result.text).toContain("System prompt (sent with every query):\nShared system instruction.");
      for (const entry of entries) {
        expect(result.text).toContain(`Query '${entry.name}':\n${entry.template}\n`);
      }
      expect(result.reviewAvailable).toBe(true);
      expect(result.systemPrompt).toBeUndefined();
    },
  );

  it("renders byte-identical to the backend review draft when nothing is bounded", () => {
    // Expected text produced by the backend on the same options:
    // multi_query_prompt_surface_from_options(opts).render_for_review()
    // (list-form queries; the third entry is an object without a name, so it
    // is labelled by its list index '#2').
    const backendDraft =
      "Multi-query LLM node: for every row the model receives one call per query below.\n\nSystem prompt (sent with every query):\nReply briefly.\n\nQuery 'a':\nTell me {{ row.x }}.\n\nQuery 'b': uses the node-level prompt_template (below).\n\nQuery '#2': (template value is not text; plugin validation rejects this node)\n\nNode-level prompt_template, used by queries without their own template: b\nSummarise pending interpretation.";
    const result = display({
      prompt_template: "Summarise pending interpretation.",
      system_prompt: "Reply briefly.",
      queries: [
        { name: "a", template: "Tell me {{ row.x }}." },
        { name: "b" },
        { bad: 1, template: 5 },
      ],
    });
    expect(result.text).toBe(backendDraft);
    expect(result.reviewAvailable).toBe(true);
  });

  it("shows all 40 queries, in order, each with its complete template", () => {
    const queries: Record<string, unknown> = {};
    const templates: string[] = [];
    for (let index = 0; index < 40; index += 1) {
      const template = `Query number ${index} asks about {{ row.field_${index} }}: ${"detail ".repeat(40)}end of query ${index}.`;
      templates.push(template);
      queries[`q_${index}`] = { template };
    }
    const { text } = display({ prompt_template: "Unused node-level text.", queries });
    let previous = -1;
    templates.forEach((template, index) => {
      const at = text.indexOf(`Query 'q_${index}':\n${template}\n`);
      expect(at).toBeGreaterThan(previous);
      previous = at;
    });
    expect(text).not.toContain("not listed");
  });

  it("shows an unused node-level template's text", () => {
    const { text } = display({ prompt_template: "Dead text.", queries: { a: { template: "Query A text." } } });
    expect(text).toContain(
      "Node-level prompt_template: not used (every query supplies its own template).\nDead text.",
    );
  });

  it("labels an unused node-level template beside a non-text query template", () => {
    const { text } = display({ prompt_template: "Dead text.", queries: { a: { template: "A." }, b: { template: 5 } } });
    expect(text).toContain("Query 'b': (template value is not text; plugin validation rejects this node)");
    expect(text).toContain("Node-level prompt_template: not used (no query falls back to it).\nDead text.");
  });

  it("shows the system prompt, and '(none)' when it is absent or not text", () => {
    const surface = (systemPrompt: unknown) => ({ prompt_template: "Node text.", system_prompt: systemPrompt, queries: { a: { template: "A." } } });
    expect(display(surface("Answer in French.")).text).toContain("System prompt (sent with every query):\nAnswer in French.\n");
    expect(display(surface(42)).text).toContain("System prompt (sent with every query):\n(none)\n");
  });

  it("labels a query that uses the node-level template and names it in the node-level label", () => {
    const { text } = display({
      prompt_template: "Shared text.",
      queries: { own: { template: "Own text." }, shared_one: { template: null }, shared_two: {} },
    });
    expect(text).toContain("Query 'own':\nOwn text.\n");
    expect(text).toContain("Query 'shared_one': uses the node-level prompt_template (below).");
    expect(text).toContain("Query 'shared_two': uses the node-level prompt_template (below).");
    expect(text).toContain(
      "Node-level prompt_template, used by queries without their own template: shared_one, shared_two\nShared text.",
    );
  });

  it("counts only object query entries and labels unnamed list entries by their list index", () => {
    const listText = display({
      prompt_template: "Node text.",
      queries: ["junk", { name: "", template: "T1." }, ["array"], { name: "q3", template: "T3." }],
    }).text;
    expect(listText).toContain("Query '#1':\nT1.\n\nQuery 'q3':\nT3.\n");
    expect(listText).not.toContain("junk");
    expect(listText).not.toContain("array");
    const mappingText = display({
      prompt_template: "Node text.",
      queries: { skipped_array: ["x"], skipped_null: null, kept: { template: "K." } },
    }).text;
    expect(mappingText).toContain("Query 'kept':\nK.\n");
    expect(mappingText).not.toContain("skipped_");
  });

  it("is not approvable when the multi-query surface has a non-text shared template", () => {
    expect(display({ prompt_template: 7, queries: { a: { template: "A." } } }).reviewAvailable).toBe(false);
  });

  it("a queries value with no well-formed entry is a single-prompt node", () => {
    const result = display({ prompt_template: "Summarise concise and neutral.", queries: {} });
    expect(result.text).toBe("Summarise concise and neutral.");
    expect(result.reviewAvailable).toBe(true);
  });
});

// ── Multi-query surface without a shared prompt_template ────────────────────
//
// Staging report 2026-11-16: the backend's multi_query_prompt_surface_from_options
// accepts an absent or null node-level prompt_template.

/** Backend render_for_review() of MULTI_QUERY_OWN_TEMPLATES (with or without prompt_template: null). */
const OWN_TEMPLATES_BACKEND_DRAFT =
  "Multi-query LLM node: for every row the model receives one call per query below.\n\nSystem prompt (sent with every query):\nReply briefly.\n\nQuery 'first':\nSummarise {{ row.text }}.\n\nQuery 'second':\nList themes in {{ row.text }}.\n\nNode-level prompt_template: not used (every query supplies its own template).";

const MULTI_QUERY_OWN_TEMPLATES = {
  system_prompt: "Reply briefly.",
  queries: [
    { name: "first", template: "Summarise {{ row.text }}." },
    { name: "second", template: "List themes in {{ row.text }}." },
  ],
};

describe("promptReviewDisplay — multi-query surface without a shared template", () => {
  it.each([
    ["omitted", MULTI_QUERY_OWN_TEMPLATES],
    ["null", { ...MULTI_QUERY_OWN_TEMPLATES, prompt_template: null }],
  ])("renders the complete surface, reviewable, when prompt_template is %s", (_case, surface) => {
    const result = display(surface);
    expect(result.reviewAvailable).toBe(true);
    expect(result.text).toBe(OWN_TEMPLATES_BACKEND_DRAFT);
  });

  it("keeps review unavailable when a query needs the missing shared template", () => {
    const result = display({ system_prompt: "Reply briefly.", queries: [{ name: "first", template: "A." }, { name: "second" }] });
    expect(result.reviewAvailable).toBe(false);
    expect(result.text).toContain("Query 'second': uses the node-level prompt_template (below).");
    expect(result.text).toContain("Node-level prompt_template, used by queries without their own template: second");
    expect(result.text).toContain("no node-level prompt_template is set");
  });
});

// Option (d): a structured prompt's slots come from the snapshot's frozen
// `prompt_template_parts` skeleton. The card marks each one as reviewed in its
// own card; the slot's value is never part of this render.
const PARTS_SURFACE = {
  prompt_template: "Summarise pending interpretation for an auditor.",
  prompt_template_parts: [
    { kind: "text", text: "Summarise " },
    { kind: "interpretation_ref", requirement_id: "req-1" },
    { kind: "text", text: " for an auditor." },
  ],
  interpretation_requirements: [{ id: "req-1", event_id: "evt-vague", user_term: "punchy" }],
};

describe("promptReviewDisplay — frozen slot skeleton", () => {
  it("breaks a structured prompt into text and slot pieces from the snapshot parts", () => {
    const result = display(PARTS_SURFACE);
    expect(result.reviewAvailable).toBe(true);
    expect(result.segments).toEqual([
      { kind: "text", text: "Summarise " },
      { kind: "slot", eventId: "evt-vague", userTerm: "punchy" },
      { kind: "text", text: " for an auditor." },
    ]);
    expect(result.text).toBe("Summarise [“punchy”, reviewed in its own card] for an auditor.");
    expect(result.hasPendingSlot).toBe(false);
  });

  it("renders a legacy snapshot without parts as its plain stored text", () => {
    const { prompt_template_parts: _parts, interpretation_requirements: _reqs, ...legacy } = PARTS_SURFACE;
    const result = display(legacy);
    expect(result.reviewAvailable).toBe(true);
    expect(result.segments).toEqual([{ kind: "text", text: "Summarise pending interpretation for an auditor." }]);
    expect(result.hasPendingSlot).toBe(true);
  });

  it.each([
    ["a part of unknown kind", { prompt_template_parts: [{ kind: "mystery" }] }],
    ["a ref with no matching requirement", { interpretation_requirements: [] }],
    ["parts that are not a list", { prompt_template_parts: "Summarise" }],
    ["requirements that are not a list", { interpretation_requirements: null }],
  ])("falls back to the plain stored text for %s", (_label, override) => {
    const result = display({ ...PARTS_SURFACE, ...override });
    expect(result.reviewAvailable).toBe(true);
    expect(result.segments).toEqual([{ kind: "text", text: "Summarise pending interpretation for an auditor." }]);
  });

  it("does not break out slots of a file-bound prompt_template", () => {
    const result = promptReviewDisplay(
      "evt-1",
      snapshot({ ...PARTS_SURFACE, prompt_template: MARKER }, { bindings: [binding("prompt_template", "From the file.")] }),
    );
    expect(result.segments.some((segment) => segment.kind === "slot")).toBe(false);
    expect(result.text).toContain("From the file.");
  });
});

describe("promptSlotStatus", () => {
  const event = (id: string, choice: InterpretationEvent["choice"], overrides: Partial<InterpretationEvent> = {}) =>
    ({ id, choice, kind: "vague_term", affected_node_id: "node-1", user_term: "punchy", ...overrides }) as InterpretationEvent;
  const linked = { eventId: "e1", userTerm: "punchy" };
  const unlinked = { eventId: null, userTerm: "punchy" };

  it("is pending while the slot's own review is outstanding", () => {
    expect(promptSlotStatus(linked, "node-1", { e1: event("e1", "pending") }, [])).toBe("pending");
  });

  it.each(["accepted_as_drafted", "amended"] as const)("is accepted once its review was %s", (choice) => {
    expect(promptSlotStatus(linked, "node-1", {}, [event("e1", choice)])).toBe("accepted");
  });

  it.each(["superseded", "opted_out", "abandoned"] as const)("is unknown when its review ended %s", (choice) => {
    expect(promptSlotStatus(linked, "node-1", {}, [event("e1", choice)])).toBe("unknown");
  });

  it("is unknown when the linked review is not loaded", () => {
    expect(promptSlotStatus(linked, "node-1", undefined, undefined)).toBe("unknown");
    expect(promptSlotStatus(linked, "node-1", { e2: event("e2", "pending") }, [])).toBe("unknown");
  });

  it("prefers a resolved row over a stale pending copy of the same event", () => {
    expect(promptSlotStatus(linked, "node-1", { e1: event("e1", "pending") }, [event("e1", "accepted_as_drafted")])).toBe("accepted");
  });

  it("finds an unlinked slot's review by the card's node and the slot's term", () => {
    expect(promptSlotStatus(unlinked, "node-1", { e1: event("e1", "pending") }, [])).toBe("pending");
    expect(promptSlotStatus(unlinked, "node-1", {}, [event("e1", "amended")])).toBe("accepted");
  });

  it.each([
    ["two reviews share the term on the node", [event("e1", "accepted_as_drafted"), event("e2", "accepted_as_drafted")]],
    ["the only review is on another node", [event("e1", "accepted_as_drafted", { affected_node_id: "node-2" })]],
    ["the only review is for another term", [event("e1", "accepted_as_drafted", { user_term: "bold" })]],
    ["the only review is not a vague term", [event("e1", "accepted_as_drafted", { kind: "llm_model_choice" })]],
    ["the only review was superseded", [event("e1", "superseded")]],
  ])("never shows an unlinked slot as accepted when %s", (_label, resolved) => {
    expect(promptSlotStatus(unlinked, "node-1", {}, resolved)).toBe("unknown");
  });

  it("is unknown for an unlinked slot with no term or no card node", () => {
    expect(promptSlotStatus({ eventId: null, userTerm: null }, "node-1", {}, [event("e1", "accepted_as_drafted")])).toBe("unknown");
    expect(promptSlotStatus(unlinked, null, {}, [event("e1", "accepted_as_drafted")])).toBe("unknown");
  });
});
