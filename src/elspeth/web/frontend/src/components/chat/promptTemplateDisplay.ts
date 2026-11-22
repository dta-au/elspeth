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
// review. A structured prompt's slots (the snapshot's frozen
// `prompt_template_parts`) are shown by `promptSlotViews`:
//   - a slot whose frozen `event_id` is set names a term accepted before this
//     card was made (carried across a wording edit, or resolved before the
//     card was issued). It shows THAT event's `accepted_value` in place,
//     whole, marked as approved earlier: this card is the only step before a
//     run uses the definition at its new place. The values must reproduce
//     the snapshot's rendered `prompt_template` (the text the anchor covers);
//     an event that is not loaded or does not match the slot, or values that
//     do not reproduce it, leave the card with no Approve and a stated reason.
//   - a slot with a null `event_id` was not accepted when the card was made.
//     It says the term is reviewed in its own card, with that review's live
//     STATUS from the interpretation events, and never a definition: a
//     frozen card does not show live values.
// A snapshot without parts (legacy, or no slots) renders its stored text as-is.
//
// Tier discipline: `authored_surface` is untyped JSON from the backend, so
// parsing is lenient; any shape that does not describe a prompt yields
// `reviewAvailable: false` rather than a guessed render.
// ============================================================================

import { promptFileSlots } from "@/lib/blobBindings";
import type { InterpretationEvent, PromptReviewSnapshotResponse } from "@/types/interpretation";

/**
 * One piece of the displayed prompt. A "slot" is an interpretation slot from
 * the snapshot's frozen `prompt_template_parts`, with the requirement's frozen
 * `eventId` and `userTerm`. What the card shows there comes from
 * `promptSlotViews`, never from this pure render.
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
  /**
   * Single-prompt nodes: the file each prompt is read from, shown under that
   * prompt's own heading. A multi-query surface names its files in its text,
   * under each section's line.
   */
  systemPromptFile?: string;
  promptTemplateFile?: string;
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
  /** The server's slot selector: the list index, or the mapping key. */
  selector: string;
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
): [string, Record<string, unknown>, string][] {
  if (Array.isArray(queries)) {
    const entries: [string, Record<string, unknown>, string][] = [];
    queries.forEach((item, index) => {
      if (!isJsonObject(item)) return;
      const name = item.name;
      entries.push([
        typeof name === "string" && name !== "" ? name : `#${index}`,
        item,
        String(index),
      ]);
    });
    return entries;
  }
  if (isJsonObject(queries)) {
    const entries: [string, Record<string, unknown>, string][] = [];
    for (const [name, entry] of Object.entries(queries)) {
      if (isJsonObject(entry)) entries.push([name, entry, name]);
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
  const queries = entries.map(([name, entry, selector]): SurfaceQuery => {
    const override = entry.template;
    if (override === undefined || override === null) {
      return { name, selector, override: null };
    }
    return {
      name,
      selector,
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
  files: FileNames,
): RenderedSurface {
  // A bound file's name sits on the line under its own section's heading.
  const fileLine = (fieldPath: string): string[] => {
    const filename = files.take(fieldPath);
    return filename === undefined ? [] : [`File: ${filename}`];
  };
  const lines: string[] = [
    "Multi-query LLM node: for every row the model receives one call per query below.",
    "",
    "System prompt (sent with every query):",
    ...fileLine("system_prompt"),
    surface.systemPrompt !== null ? surface.systemPrompt : "(none)",
  ];
  for (const { name, selector, override } of surface.queries) {
    lines.push("");
    if (override === null) {
      lines.push(`Query '${name}': uses the node-level prompt_template (below).`);
    } else if (override === INVALID_QUERY_TEMPLATE) {
      lines.push(
        `Query '${name}': (template value is not text; plugin validation rejects this node)`,
      );
    } else {
      lines.push(`Query '${name}':`, ...fileLine(`queries.${selector}.template`), override);
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
  lines.push(...fileLine("prompt_template"));

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
  systemPromptFile?: string;
  promptTemplateFile?: string;
}

/**
 * The snapshot's bound file names by slot path. A render `take`s each name
 * where it shows it; any it did not place are listed above the prompt, so no
 * file the review covers goes unnamed.
 */
interface FileNames {
  take: (fieldPath: string) => string | undefined;
  rest: () => PromptSegment[];
}

function fileNames(snapshot: PromptReviewSnapshotResponse): FileNames {
  const remaining = new Map(snapshot.bindings.map((binding) => [binding.field_path, binding.filename]));
  return {
    take(fieldPath) {
      const filename = remaining.get(fieldPath);
      remaining.delete(fieldPath);
      return filename;
    },
    rest() {
      if (remaining.size === 0) return [];
      return [{ kind: "text", text: `${[...remaining].map(([fieldPath, filename]) => `File: ${filename} (${fieldPath})`).join("\n")}\n\n` }];
    },
  };
}

function unavailable(message: string): RenderedSurface {
  return { segments: [{ kind: "text", text: message }], reviewAvailable: false };
}

const UNAVAILABLE_MESSAGE = "This prompt review cannot be shown. Reload the session to see the current review.";

/**
 * Whether the reviewed prompt is made only of files a user supplied.
 *
 * Decides card wording and nothing else. True only when every prompt slot of
 * the reviewed surface is one of the snapshot's file bindings and every
 * binding is user-supplied. "User-supplied" is the PAIR the server uses
 * (`is_user_uploaded_blob`): the file's author is the user AND its content
 * was not authored by the LLM. A file the assistant created is not
 * user-supplied even when it holds the user's words verbatim, so the
 * modality alone never decides. "verbatim" is the one modality that is not
 * LLM-authored; a missing or unrecognised author or modality counts as not
 * user-supplied.
 */
export function promptReviewIsUserSuppliedFiles(snapshot: PromptReviewSnapshotResponse): boolean {
  if (!snapshot.is_live_review || snapshot.bindings.length === 0) return false;
  const filled = promptFileSlots(snapshot.authored_surface).filter((slot) => slot.value !== null && slot.value !== undefined);
  const bound = new Set(snapshot.bindings.map((binding) => binding.field_path));
  if (bound.size !== snapshot.bindings.length || filled.length !== bound.size) return false;
  if (!filled.every((slot) => typeof slot.value === "object" && bound.has(slot.fieldPath))) return false;
  return snapshot.bindings.every((binding) => binding.created_by === "user" && binding.creation_modality === "verbatim");
}

/**
 * Render a prompt-template review card from the event's stored snapshot.
 *
 * A snapshot for another event, or a file-backed snapshot whose bound files
 * no longer match the live node (`is_live_review: false`), is not
 * approvable. An inline snapshot is not gated on `is_live_review`: the
 * stored snapshot and its anchor are what the resolve checks. Resolving one
 * of its interpretation slots while this card is pending re-renders the live
 * `prompt_template`, so the server supersedes this card and issues a new one
 * with its own snapshot and anchor (coordination/repository.py, "Flag pending
 * prompt cards whose stored snapshot no longer matches the live surface").
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
  // Display only: which file each prompt is read from. The approval names the
  // snapshot's anchor, which the server computes; none of this text is sent.
  const files = fileNames(snapshot);
  // A file-bound node-level template has no inline skeleton to mark.
  const templateBound = snapshot.bindings.some((binding) => binding.field_path === "prompt_template");
  const templateSegments = templateBound ? null : slotSegmentsFromSurface(surface);
  const multiQuery = multiQuerySurfaceFromOptions(surface);
  if (multiQuery !== null) {
    const rendered = renderMultiQuerySurface(multiQuery, templateSegments, files);
    return { ...rendered, segments: [...files.rest(), ...rendered.segments] };
  }
  const template = surface.prompt_template;
  if (typeof template !== "string" && templateSegments === null) return unavailable(UNAVAILABLE_MESSAGE);
  const systemPrompt = surface.system_prompt;
  const systemPromptFile = typeof systemPrompt === "string" ? files.take("system_prompt") : undefined;
  const promptTemplateFile = files.take("prompt_template");
  return {
    segments: [...files.rest(), ...(templateSegments ?? [{ kind: "text" as const, text: template as string }])],
    systemPrompt: typeof systemPrompt === "string" ? systemPrompt : null,
    ...(systemPromptFile !== undefined ? { systemPromptFile } : {}),
    ...(promptTemplateFile !== undefined ? { promptTemplateFile } : {}),
    reviewAvailable: true,
  };
}

/** The live state of a slot's own review, as the card shows it. */
export type PromptSlotStatus = "pending" | "accepted" | "unknown";

/**
 * A slot's status, read from the interpretation events (never from
 * compositionState): pending while its review event is outstanding, accepted
 * once that event was accepted or amended. The card shows it for a slot with
 * a null `event_id`; a slot naming an event shows that event's definition
 * instead (`promptSlotViews`).
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
  const events = eventsById(pending, resolved);
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

/** What the card shows at one slot of the displayed prompt. */
export type PromptSlotView =
  /** A term accepted before this card was made: its approved definition, in place. */
  | { kind: "definition"; userTerm: string | null; value: string; choice: "accepted_as_drafted" | "amended" }
  /** A term not accepted when this card was made: reviewed in its own card, no definition. */
  | { kind: "own_card"; userTerm: string | null; status: PromptSlotStatus }
  /** A slot naming an event the card cannot show; the card is not approvable. */
  | { kind: "not_shown"; userTerm: string | null };

export interface PromptSlotViews {
  /** One view per slot segment of the display, in display order. */
  slots: PromptSlotView[];
  /** Why the card cannot be approved, or null when every slot can be shown. */
  failure: string | null;
}

function termName(userTerm: string | null): string {
  return userTerm === null ? "this term" : `“${userTerm}”`;
}

/** The visible wording of a slot that shows its approved definition. */
export function promptSlotDefinitionLabel(
  userTerm: string | null,
  choice: "accepted_as_drafted" | "amended",
  value: string,
): string {
  const how = choice === "amended" ? "amended by you" : "accepted as drafted";
  return userTerm === null ? `approved earlier, ${how}: ${value}` : `${termName(userTerm)}, approved earlier, ${how}: ${value}`;
}

/** The visible wording of a slot whose approved definition cannot be shown. */
export function promptSlotNotShownLabel(userTerm: string | null): string {
  return userTerm === null ? "approved definition cannot be shown" : `${termName(userTerm)}, approved definition cannot be shown`;
}

function definitionNotLoaded(userTerm: string | null): string {
  return `The approved definition of ${termName(userTerm)} is not loaded, so this prompt cannot be approved. Reload the session to load it.`;
}

function definitionMismatch(userTerm: string | null): string {
  return `The approved definition of ${termName(userTerm)} does not match this prompt review, so it cannot be approved. Reload the session to see the current review.`;
}

const RENDERED_PROMPT_MISMATCH =
  "The approved definitions do not reproduce the reviewed prompt, so it cannot be approved. Reload the session to see the current review.";

/** Pending and resolved events by id; resolution is terminal, so a resolved row wins. */
function eventsById(
  pending: Readonly<Record<string, InterpretationEvent>> | undefined,
  resolved: readonly InterpretationEvent[] | undefined,
): Map<string, InterpretationEvent> {
  const events = new Map<string, InterpretationEvent>();
  for (const event of Object.values(pending ?? {})) events.set(event.id, event);
  for (const event of resolved ?? []) events.set(event.id, event);
  return events;
}

/**
 * Whether the snapshot's parts, with each named event's value at its slots
 * and the pending wording at every slot with a null event, reproduce the
 * snapshot's rendered `prompt_template` exactly. That string is what the
 * server rendered from the requirement rows when it froze the card, and it
 * is under the anchor, so equality means the definitions shown are the ones
 * the approval covers. Reads the raw stored surface, not the readable copy.
 */
function reproducesRenderedPrompt(
  snapshot: PromptReviewSnapshotResponse,
  values: ReadonlyMap<string, string>,
): boolean {
  const rendered = snapshot.authored_surface.prompt_template;
  const segments = slotSegmentsFromSurface(snapshot.authored_surface);
  if (typeof rendered !== "string" || segments === null) return false;
  const pieces: string[] = [];
  for (const segment of segments) {
    if (segment.kind === "text") {
      pieces.push(segment.text);
    } else if (segment.eventId === null) {
      pieces.push(PENDING_INTERPRETATION_DISPLAY_TEXT);
    } else {
      const value = values.get(segment.eventId);
      if (value === undefined) return false;
      pieces.push(value);
    }
  }
  return pieces.join("") === rendered;
}

/**
 * What the card shows at each slot of `display`, from the approval-bound
 * snapshot's slot → that event in the interpretation events. Never from the
 * event's bounded `llm_draft`, never from live requirement rows, and never
 * from the rendered `prompt_template` alone (it is the check, not the source).
 *
 * A slot naming an event shows the event's `accepted_value` when the event is
 * loaded, is the vague_term review of the slot's term, and was accepted as
 * drafted or amended with an accepted value, and when the values together
 * reproduce the snapshot's rendered prompt. Anything else fails closed: every
 * slot naming an event is "not shown" and `failure` says why. A slot with a
 * null event is reviewed in its own card and carries its live status.
 */
export function promptSlotViews(
  display: PromptDisplayResult,
  snapshot: PromptReviewSnapshotResponse,
  nodeId: string | null,
  pending: Readonly<Record<string, InterpretationEvent>> | undefined,
  resolved: readonly InterpretationEvent[] | undefined,
): PromptSlotViews {
  const slots = display.segments.flatMap((segment) => segment.kind === "slot" ? [segment] : []);
  const events = eventsById(pending, resolved);
  const definitions = new Map<string, { value: string; choice: "accepted_as_drafted" | "amended" }>();
  let failure: string | null = null;
  for (const slot of slots) {
    if (slot.eventId === null || definitions.has(slot.eventId)) continue;
    const event = events.get(slot.eventId);
    if (event === undefined) {
      failure = definitionNotLoaded(slot.userTerm);
      break;
    }
    const { choice, accepted_value: value } = event;
    if (
      event.kind !== "vague_term" ||
      event.user_term !== slot.userTerm ||
      (choice !== "accepted_as_drafted" && choice !== "amended") ||
      typeof value !== "string"
    ) {
      failure = definitionMismatch(slot.userTerm);
      break;
    }
    definitions.set(slot.eventId, { value, choice });
  }
  if (failure === null && definitions.size > 0) {
    const values = new Map([...definitions].map(([eventId, definition]) => [eventId, definition.value]));
    if (!reproducesRenderedPrompt(snapshot, values)) failure = RENDERED_PROMPT_MISMATCH;
  }
  return {
    failure,
    slots: slots.map((slot): PromptSlotView => {
      if (slot.eventId === null) {
        return { kind: "own_card", userTerm: slot.userTerm, status: promptSlotStatus(slot, nodeId, pending, resolved) };
      }
      const definition = definitions.get(slot.eventId);
      return failure === null && definition !== undefined
        ? { kind: "definition", userTerm: slot.userTerm, ...definition }
        : { kind: "not_shown", userTerm: slot.userTerm };
    }),
  };
}
