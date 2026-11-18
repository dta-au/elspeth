// ============================================================================
// promptTemplateDisplay.ts — pure renderer for the prompt-template approval
// card's displayed prompt.
//
// Source of truth: the server's stored review snapshot for the event
// (`GET .../interpretations/{event_id}/prompt`). Every llm_prompt_template
// event carries one, inline or file-backed, and the resolve must name that
// snapshot's `anchor`. The card therefore renders ONLY from the snapshot's
// `authored_surface` (with each file binding's stored text in its slot), and
// never from the browser's compositionState: a second tab or a failed state
// read can leave that state behind the server, and the card would show one
// prompt while the approval recorded another.
//
// The snapshot is frozen when the review is surfaced. This review approves
// the prompt skeleton; each slot value is attested by its own vague_term
// review. So a structured prompt's slots (the snapshot's frozen
// `prompt_template_parts`) render as spans saying the term is reviewed in its
// own card, and the card adds each slot's live STATUS from the interpretation
// events. The slot's live TEXT is never shown here: accepting a term changes
// its status, never the prompt text this card displays. A snapshot without
// parts (legacy, or no slots) renders its stored text as-is.
//
// Tier discipline: `authored_surface` is untyped JSON from the backend, so
// parsing is lenient; any shape that does not describe a prompt yields
// `reviewAvailable: false` rather than a guessed render.
// ============================================================================

import type { InterpretationEvent, PromptReviewSnapshotResponse } from "@/types/interpretation";

/**
 * One piece of the displayed prompt. A "slot" is an interpretation slot from
 * the snapshot's frozen `prompt_template_parts`: the card never shows its
 * value here (that is attested by the slot's own review card); it shows that
 * the slot is reviewed in its own card, and that review's live STATUS, which
 * the card reads from the interpretation events by `eventId`.
 */
export type PromptSegment =
  | { kind: "text"; text: string }
  | { kind: "slot"; eventId: string | null; userTerm: string | null };

/** The visible wording of a slot span. */
export function promptSlotLabel(userTerm: string | null): string {
  return userTerm === null ? "reviewed in its own card" : `“${userTerm}”, reviewed in its own card`;
}

export interface PromptDisplayResult {
  /** False when the snapshot does not describe a complete, approvable prompt. */
  reviewAvailable: boolean;
  /** The user-prompt (single prompt) or the whole multi-query surface, as pieces. */
  segments: PromptSegment[];
  /** `segments` as plain text, each slot as its label in brackets. */
  text: string;
  /** Single-prompt nodes expose the separate system message; undefined for multi-query. */
  systemPrompt?: string | null;
  /** The text shows an interpretation slot that is reviewed in its own card. */
  hasPendingSlot?: boolean;
}

/**
 * What the reader sees at an interpretation slot that was unresolved when
 * the review was surfaced. It is the backend's own wording
 * (interpretation_state.py PENDING_INTERPRETATION_AUTHORING_TEXT): a
 * structured prompt is stored with it at every unresolved slot, and the
 * authoring view masks a legacy `{{interpretation: term}}` placeholder with
 * it. The card explains it beside the prompt.
 */
export const PENDING_INTERPRETATION_DISPLAY_TEXT = "pending interpretation";

/** A legacy inline placeholder (validation.py INTERPRETATION_PLACEHOLDER_RE). */
const INTERPRETATION_PLACEHOLDER = /\{\{\s*interpretation\s*:\s*([^{}]+?)\s*\}\}/g;

/**
 * Show a stored prompt string the way the authoring view does: a raw
 * `{{interpretation: term}}` placeholder is internal syntax, so it reads as
 * the pending-slot wording. Mirrors `_materialize_node_for_authoring`.
 */
function readableSlots(value: unknown): unknown {
  return typeof value === "string" ? value.replace(INTERPRETATION_PLACEHOLDER, PENDING_INTERPRETATION_DISPLAY_TEXT) : value;
}

/** The inline (not file-bound) prompt strings of a surface, made readable. */
function readableSurface(surface: Record<string, unknown>): Record<string, unknown> {
  const readable: Record<string, unknown> = { ...surface };
  for (const key of ["prompt_template", "system_prompt"]) {
    if (key in readable) readable[key] = readableSlots(readable[key]);
  }
  const queries = readable.queries;
  const readableEntry = (entry: unknown): unknown =>
    isJsonObject(entry) && "template" in entry ? { ...entry, template: readableSlots(entry.template) } : entry;
  if (Array.isArray(queries)) {
    readable.queries = queries.map(readableEntry);
  } else if (isJsonObject(queries)) {
    readable.queries = Object.fromEntries(Object.entries(queries).map(([name, entry]) => [name, readableEntry(entry)]));
  }
  return readable;
}

/**
 * The frozen skeleton of a structured prompt: `prompt_template_parts` (text
 * parts and `interpretation_ref` parts) plus the `interpretation_requirements`
 * skeleton linking each requirement id to its review event. Null when either
 * is absent or malformed (a legacy snapshot, or one with no slots), and the
 * caller shows the plain `prompt_template` text instead.
 */
function slotSegmentsFromSurface(surface: Record<string, unknown>): PromptSegment[] | null {
  const parts = surface.prompt_template_parts;
  const requirements = surface.interpretation_requirements;
  if (!Array.isArray(parts) || parts.length === 0 || !Array.isArray(requirements)) return null;
  const byId = new Map<string, { eventId: string | null; userTerm: string | null }>();
  for (const requirement of requirements) {
    if (!isJsonObject(requirement) || typeof requirement.id !== "string") return null;
    byId.set(requirement.id, {
      eventId: typeof requirement.event_id === "string" ? requirement.event_id : null,
      userTerm: typeof requirement.user_term === "string" && requirement.user_term !== "" ? requirement.user_term : null,
    });
  }
  const segments: PromptSegment[] = [];
  for (const part of parts) {
    if (!isJsonObject(part)) return null;
    if (part.kind === "text" && typeof part.text === "string") {
      segments.push({ kind: "text", text: part.text });
      continue;
    }
    if (part.kind === "interpretation_ref" && typeof part.requirement_id === "string") {
      const skeleton = byId.get(part.requirement_id);
      if (skeleton === undefined) return null;
      segments.push({ kind: "slot", ...skeleton });
      continue;
    }
    return null;
  }
  return segments;
}

function plainText(segments: readonly PromptSegment[]): string {
  return segments.map((segment) => segment.kind === "text" ? segment.text : `[${promptSlotLabel(segment.userTerm)}]`).join("");
}

/** A query whose `template` is present but is not text (backend InvalidQueryTemplate). */
const INVALID_QUERY_TEMPLATE = Symbol("invalid query template");

interface SurfaceQuery {
  name: string;
  /** Own template text; null = uses the node-level template; or not text. */
  override: string | null | typeof INVALID_QUERY_TEMPLATE;
}

interface MultiQuerySurface {
  /** The optional shared template; null when omitted or null in the options. */
  nodePromptTemplate: string | null;
  systemPrompt: string | null;
  queries: SurfaceQuery[];
}

/** A JSON object value (not an array, not null). */
function isJsonObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The authored surface with every file binding's stored text placed in its
 * slot. Both inputs come from the same snapshot, so nothing here reads
 * browser state. List-form queries are addressed by index, as the server
 * builds `queries.<index>.template`.
 */
function surfaceWithBindingText(snapshot: PromptReviewSnapshotResponse): Record<string, unknown> {
  // Inline strings first; a bound file's text is placed verbatim after.
  const surface = readableSurface(snapshot.authored_surface);
  for (const binding of snapshot.bindings) {
    if (binding.field_path === "prompt_template" || binding.field_path === "system_prompt") {
      surface[binding.field_path] = binding.text;
      continue;
    }
    const match = /^queries\.([^.]+)\.template$/.exec(binding.field_path);
    if (match === null) continue;
    const queries = surface.queries;
    if (Array.isArray(queries)) {
      const index = Number(match[1]);
      if (Number.isInteger(index) && index >= 0 && index < queries.length && isJsonObject(queries[index])) {
        const copy = [...queries];
        copy[index] = { ...queries[index] as Record<string, unknown>, template: binding.text };
        surface.queries = copy;
      }
    } else if (isJsonObject(queries) && isJsonObject(queries[match[1]])) {
      surface.queries = {
        ...queries,
        [match[1]]: { ...queries[match[1]] as Record<string, unknown>, template: binding.text },
      };
    }
  }
  return surface;
}

/**
 * The well-formed `(name, entry)` pairs of an untrusted `queries` option,
 * mirroring state.py `_well_formed_query_entries`: the mapping form keeps
 * every object-valued entry keyed by name; the list form keeps every object
 * item, named by its non-empty string `name` or else by its index in the
 * original list (`#<index>`); anything else yields no entries.
 */
function wellFormedQueryEntries(
  queries: unknown,
): [string, Record<string, unknown>][] {
  if (Array.isArray(queries)) {
    const entries: [string, Record<string, unknown>][] = [];
    queries.forEach((item, index) => {
      if (!isJsonObject(item)) return;
      const name = item.name;
      entries.push([
        typeof name === "string" && name !== "" ? name : `#${index}`,
        item,
      ]);
    });
    return entries;
  }
  if (isJsonObject(queries)) {
    const entries: [string, Record<string, unknown>][] = [];
    for (const [name, entry] of Object.entries(queries)) {
      if (isJsonObject(entry)) entries.push([name, entry]);
    }
    return entries;
  }
  return [];
}

/**
 * The multi-query prompt surface, mirroring interpretation_state.py
 * `multi_query_prompt_surface_from_options`: null unless a `queries` value
 * with at least one well-formed entry sits beside a string, omitted or null
 * `prompt_template` (every other shape is a single-prompt node there too).
 * The shared template is optional: every query may supply its own.
 */
function multiQuerySurfaceFromOptions(
  options: Record<string, unknown>,
): MultiQuerySurface | null {
  const rawQueries = options.queries;
  const promptTemplate = options.prompt_template;
  if (rawQueries === undefined || rawQueries === null) return null;
  if (
    promptTemplate !== undefined &&
    promptTemplate !== null &&
    typeof promptTemplate !== "string"
  ) {
    return null;
  }
  const entries = wellFormedQueryEntries(rawQueries);
  if (entries.length === 0) return null;
  const queries = entries.map(([name, entry]): SurfaceQuery => {
    const override = entry.template;
    if (override === undefined || override === null) {
      return { name, override: null };
    }
    return {
      name,
      override: typeof override === "string" ? override : INVALID_QUERY_TEMPLATE,
    };
  });
  const systemPrompt = options.system_prompt;
  return {
    nodePromptTemplate: typeof promptTemplate === "string" ? promptTemplate : null,
    systemPrompt: typeof systemPrompt === "string" ? systemPrompt : null,
    queries,
  };
}

/**
 * Render the COMPLETE multi-query surface.
 *
 * Covers exactly what the review anchor covers
 * (`MultiQueryPromptSurface.anchor_hash`): the system prompt, every query's
 * name and template override in order, and the node-level template. Labels
 * are the backend `render_for_review` wording, with two deliberate
 * differences: nothing is shortened or omitted (the draft is bounded to 8000
 * chars while the anchor covers every query), and the node-level template's
 * text is shown even when no query uses it (the anchor covers it either way).
 */
function renderMultiQuerySurface(
  surface: MultiQuerySurface,
  templateSegments: PromptSegment[] | null,
): RenderedSurface {
  const lines: string[] = [
    "Multi-query LLM node: for every row the model receives one call per query below.",
    "",
    "System prompt (sent with every query):",
    surface.systemPrompt !== null ? surface.systemPrompt : "(none)",
  ];
  for (const { name, override } of surface.queries) {
    lines.push("");
    if (override === null) {
      lines.push(`Query '${name}': uses the node-level prompt_template (below).`);
    } else if (override === INVALID_QUERY_TEMPLATE) {
      lines.push(
        `Query '${name}': (template value is not text; plugin validation rejects this node)`,
      );
    } else {
      lines.push(`Query '${name}':`, override);
    }
  }
  lines.push("");
  const users = surface.queries
    .filter((query) => query.override === null)
    .map((query) => query.name);
  if (users.length > 0) {
    lines.push(
      `Node-level prompt_template, used by queries without their own template: ${users.join(", ")}`,
    );
  } else if (
    surface.queries.some((query) => query.override === INVALID_QUERY_TEMPLATE)
  ) {
    lines.push("Node-level prompt_template: not used (no query falls back to it).");
  } else {
    lines.push(
      "Node-level prompt_template: not used (every query supplies its own template).",
    );
  }

  if (surface.nodePromptTemplate === null) {
    // No shared template: the per-query templates are the whole surface, as
    // in the backend draft. A query that falls back to the missing template
    // has no prompt to show, so the card must not offer approval.
    if (users.length > 0) {
      lines.push("(no node-level prompt_template is set)");
    }
    return { segments: [{ kind: "text", text: lines.join("\n") }], reviewAvailable: users.length === 0 };
  }
  return {
    segments: [
      { kind: "text", text: `${lines.join("\n")}\n` },
      ...(templateSegments ?? [{ kind: "text" as const, text: surface.nodePromptTemplate }]),
    ],
    reviewAvailable: true,
  };
}

/** A render before its plain text is derived. */
interface RenderedSurface {
  reviewAvailable: boolean;
  segments: PromptSegment[];
  systemPrompt?: string | null;
}

function unavailable(message: string): RenderedSurface {
  return { segments: [{ kind: "text", text: message }], reviewAvailable: false };
}

const UNAVAILABLE_MESSAGE = "This prompt review cannot be shown. Reload the session to see the current review.";

/**
 * Render a prompt-template review card from the event's stored snapshot.
 *
 * A snapshot for another event, or a file-backed snapshot whose bound files
 * no longer match the live node (`is_live_review: false`), is not
 * approvable. An inline snapshot is not gated on `is_live_review`: resolving
 * one of its interpretation slots rewrites the live `prompt_template` while
 * the reviewed skeleton, and so the anchor the resolve checks, is unchanged.
 */
export function promptReviewDisplay(
  eventId: string,
  snapshot: PromptReviewSnapshotResponse,
): PromptDisplayResult {
  const result = renderSnapshot(eventId, snapshot);
  const text = plainText(result.segments);
  if (!result.reviewAvailable) return { ...result, text };
  // The pending-slot wording can appear only in stored text, never in a
  // slot span, so look at the text pieces alone.
  const storedText = result.segments.flatMap((segment) => segment.kind === "text" ? [segment.text] : []);
  const shown = [result.systemPrompt ?? "", ...storedText].join("\n");
  return { ...result, text, hasPendingSlot: shown.includes(PENDING_INTERPRETATION_DISPLAY_TEXT) };
}

function renderSnapshot(
  eventId: string,
  snapshot: PromptReviewSnapshotResponse,
): RenderedSurface {
  if (snapshot.event_id !== eventId) return unavailable(UNAVAILABLE_MESSAGE);
  const fileBacked = snapshot.bindings.length > 0;
  if (fileBacked && !snapshot.is_live_review) {
    return unavailable("The prompt files behind this review have changed. Open the current review card.");
  }
  const surface = surfaceWithBindingText(snapshot);
  const fileLabels: PromptSegment[] = fileBacked
    ? [{ kind: "text", text: `${snapshot.bindings.map((binding) => `File: ${binding.filename} (${binding.field_path})`).join("\n")}\n\n` }]
    : [];
  // A file-bound node-level template has no inline skeleton to mark.
  const templateBound = snapshot.bindings.some((binding) => binding.field_path === "prompt_template");
  const templateSegments = templateBound ? null : slotSegmentsFromSurface(surface);
  const multiQuery = multiQuerySurfaceFromOptions(surface);
  if (multiQuery !== null) {
    const rendered = renderMultiQuerySurface(multiQuery, templateSegments);
    return { ...rendered, segments: [...fileLabels, ...rendered.segments] };
  }
  const template = surface.prompt_template;
  if (typeof template !== "string" && templateSegments === null) return unavailable(UNAVAILABLE_MESSAGE);
  const systemPrompt = surface.system_prompt;
  return {
    segments: [...fileLabels, ...(templateSegments ?? [{ kind: "text" as const, text: template as string }])],
    systemPrompt: typeof systemPrompt === "string" ? systemPrompt : null,
    reviewAvailable: true,
  };
}

/** The live state of a slot's own review, as the card shows it. */
export type PromptSlotStatus = "pending" | "accepted" | "unknown";

/**
 * A slot's status, read from the interpretation events (never from
 * compositionState, and never its text): pending while its review event is
 * outstanding, accepted once that event was accepted or amended.
 *
 * The slot's review is the requirement's frozen `event_id` when set. The
 * server writes it only once the vague term resolves, so at surfacing it is
 * usually null; the review is then the ONE vague_term event on the card's
 * node with the slot's user_term. No such event, more than one, or an event
 * that ended any other way (superseded, opted out, abandoned) is "unknown",
 * never accepted. This is display only: the anchor is the binding control.
 */
export function promptSlotStatus(
  slot: { eventId: string | null; userTerm: string | null },
  nodeId: string | null,
  pending: Readonly<Record<string, InterpretationEvent>> | undefined,
  resolved: readonly InterpretationEvent[] | undefined,
): PromptSlotStatus {
  const events = new Map<string, InterpretationEvent>();
  // Resolution is terminal, so a resolved row wins over a stale pending one.
  for (const event of Object.values(pending ?? {})) events.set(event.id, event);
  for (const event of resolved ?? []) events.set(event.id, event);
  let review: InterpretationEvent | undefined;
  if (slot.eventId !== null) {
    review = events.get(slot.eventId);
  } else {
    if (nodeId === null || slot.userTerm === null) return "unknown";
    const matches = [...events.values()].filter(
      (event) => event.kind === "vague_term" && event.affected_node_id === nodeId && event.user_term === slot.userTerm,
    );
    if (matches.length !== 1) return "unknown";
    review = matches[0];
  }
  if (review === undefined) return "unknown";
  if (review.choice === "pending") return "pending";
  if (review.choice === "accepted_as_drafted" || review.choice === "amended") return "accepted";
  return "unknown";
}
