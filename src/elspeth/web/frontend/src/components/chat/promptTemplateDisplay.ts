// ============================================================================
// promptTemplateDisplay.ts — pure resolver for the prompt-template approval
// card's displayed prompt (elspeth-990f5ea562).
//
// The event's `llm_draft` is frozen at staging time to the AUTHORING render,
// in which every unresolved interpretation slot is masked as the literal
// "pending interpretation".  Substitution of accepted values happens
// backend-side (on each vague-term resolve, and finally at execution
// materialisation) and reaches the client only through the session store's
// compositionState — the node's `prompt_template_parts` +
// `interpretation_requirements` options arrive verbatim.  This helper
// re-renders the prompt from that live state so the card shows what will
// actually run, with each substituted slot marked for highlighting.
//
// Attestation note: the llm_prompt_template review approves the prompt
// SKELETON (`prompt_structure_hash`) and is invariant under slot resolution;
// slot values are attested by their own vague_term reviews.  Showing the
// substituted values is therefore a display fix, not an attestation change.
//
// Tier discipline: the node options are untyped `Record<string, unknown>`
// round-tripped from the backend, so parsing is lenient — any malformed or
// missing shape falls back to the node's current `prompt_template` string
// (which the backend also rewrites on resolve, covering legacy no-parts
// nodes), then to the event's frozen `llm_draft`.
// ============================================================================

import type { CompositionState } from "@/types/index";
import type { InterpretationEvent } from "@/types/interpretation";

/**
 * The literal the backend authoring render substitutes for an unresolved
 * slot (interpretation_state.py PENDING_INTERPRETATION_AUTHORING_TEXT).
 * Used here as the last-resort text for a pending slot with no draft.
 */
export const PENDING_INTERPRETATION_DISPLAY_TEXT = "pending interpretation";

export interface PromptDisplaySegment {
  /**
   * "text" — fixed prompt text, rendered verbatim.
   * "resolved" — an interpretation slot carrying its accepted value.
   * "pending" — a slot still awaiting its own review (shows the draft).
   */
  kind: "text" | "resolved" | "pending";
  text: string;
}

export interface PromptDisplayResult {
  /** False when only the bounded event preview is available. */
  reviewAvailable: boolean;
  segments: PromptDisplaySegment[];
  /** Single-prompt nodes expose the separate system message; undefined for multi-query or missing state. */
  systemPrompt?: string | null;
  /**
   * True when the structured parts could not be rendered: the result is a
   * single flat text segment from the fallback chain (node prompt_template,
   * then event llm_draft), or, for a multi-query surface, the node-level
   * template section shows the stored prompt_template text as-is.
   */
  usedFallback: boolean;
}

interface ParsedRequirement {
  status: "pending" | "resolved";
  draft: string | null;
  acceptedValue: string | null;
}

/** Leniently parse `options.interpretation_requirements`; null on any malformed shape. */
function parseRequirements(raw: unknown): Map<string, ParsedRequirement> | null {
  if (!Array.isArray(raw)) return null;
  const byId = new Map<string, ParsedRequirement>();
  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null) return null;
    const row = entry as Record<string, unknown>;
    if (typeof row.id !== "string") return null;
    if (row.status !== "pending" && row.status !== "resolved") return null;
    byId.set(row.id, {
      status: row.status,
      draft: typeof row.draft === "string" ? row.draft : null,
      acceptedValue:
        typeof row.accepted_value === "string" ? row.accepted_value : null,
    });
  }
  return byId;
}

/** Leniently render `options.prompt_template_parts`; null on any malformed shape. */
function segmentsFromParts(
  raw: unknown,
  requirements: Map<string, ParsedRequirement>,
): PromptDisplaySegment[] | null {
  if (!Array.isArray(raw) || raw.length === 0) return null;
  const segments: PromptDisplaySegment[] = [];
  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null) return null;
    const part = entry as Record<string, unknown>;
    if (part.kind === "text") {
      if (typeof part.text !== "string") return null;
      segments.push({ kind: "text", text: part.text });
      continue;
    }
    if (part.kind === "interpretation_ref") {
      if (typeof part.requirement_id !== "string") return null;
      const requirement = requirements.get(part.requirement_id);
      if (requirement === undefined) return null;
      if (requirement.status === "resolved") {
        // A resolved requirement with no accepted value is malformed.
        if (requirement.acceptedValue === null) return null;
        segments.push({ kind: "resolved", text: requirement.acceptedValue });
      } else {
        segments.push({
          kind: "pending",
          text:
            requirement.draft !== null && requirement.draft !== ""
              ? requirement.draft
              : PENDING_INTERPRETATION_DISPLAY_TEXT,
        });
      }
      continue;
    }
    return null;
  }
  return segments;
}

/** A query whose `template` is present but is not text (backend InvalidQueryTemplate). */
const INVALID_QUERY_TEMPLATE = Symbol("invalid query template");

interface SurfaceQuery {
  name: string;
  /** Own template text; null = uses the node-level template; or not text. */
  override: string | null | typeof INVALID_QUERY_TEMPLATE;
}

interface MultiQuerySurface {
  nodePromptTemplate: string;
  systemPrompt: string | null;
  queries: SurfaceQuery[];
}

/** A JSON object value (not an array, not null). */
function isJsonObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
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
 * `multi_query_prompt_surface_from_options`: null unless a string
 * `prompt_template` sits beside a `queries` value with at least one
 * well-formed entry (every other shape is a single-prompt node there too).
 */
function multiQuerySurfaceFromOptions(
  options: Record<string, unknown>,
): MultiQuerySurface | null {
  const rawQueries = options.queries;
  const promptTemplate = options.prompt_template;
  if (
    rawQueries === undefined ||
    rawQueries === null ||
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
    nodePromptTemplate: promptTemplate,
    systemPrompt: typeof systemPrompt === "string" ? systemPrompt : null,
    queries,
  };
}

/**
 * Render the COMPLETE multi-query surface from live node options.
 *
 * Covers exactly what the review anchor covers
 * (`MultiQueryPromptSurface.anchor_hash`): the system prompt, every query's
 * name and template override in order, and the node-level template. Labels
 * are the backend `render_for_review` wording, so an unbounded surface with
 * no resolved slot renders byte-identical to that draft, with two deliberate
 * differences: nothing is shortened or omitted (the draft is bounded to 8000
 * chars while the anchor covers every query), and the node-level template's
 * text is shown even when no query uses it (the anchor covers it either
 * way). The node-level template goes through the structured-parts
 * substitution so resolved slots show their accepted values and pending
 * slots stay marked; when its parts cannot be broken out it shows the stored
 * `prompt_template` text and `usedFallback` is true.
 */
function segmentsFromSurface(
  surface: MultiQuerySurface,
  options: Record<string, unknown>,
): PromptDisplayResult {
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

  const requirements = parseRequirements(options.interpretation_requirements);
  const templateSegments =
    requirements !== null
      ? segmentsFromParts(options.prompt_template_parts, requirements)
      : null;
  const segments: PromptDisplaySegment[] = [
    { kind: "text", text: `${lines.join("\n")}\n` },
    ...(templateSegments ?? [
      { kind: "text" as const, text: surface.nodePromptTemplate },
    ]),
  ];
  return { segments, usedFallback: templateSegments === null, reviewAvailable: true };
}

/**
 * Resolve the display segments for a prompt-template review card.
 *
 * Multi-query nodes (see `multiQuerySurfaceFromOptions`) render the complete
 * prompt surface from live node options (`segmentsFromSurface`): the review
 * attests that whole surface, not the node-level `prompt_template` alone,
 * which every query may override (session 94f6f00c), and the event's bounded
 * `llm_draft` may omit query text the anchor covers.
 *
 * Single-prompt fallback chain: structured parts → node's current
 * `prompt_template` string → the event's frozen `llm_draft` ("" when null).
 * The `llm_draft` is also the last resort for multi-query options that do
 * not describe a surface.
 */
export function resolvePromptDisplaySegments(
  state: CompositionState | null,
  event: InterpretationEvent,
): PromptDisplayResult {
  const node =
    event.affected_node_id !== null
      ? (state?.nodes.find((n) => n.id === event.affected_node_id) ?? null)
      : null;
  const options = node?.options ?? null;
  if (options !== null) {
    const surface = multiQuerySurfaceFromOptions(options);
    if (surface !== null) return segmentsFromSurface(surface, options);
  }
  if (options !== null) {
    const systemPrompt = typeof options.system_prompt === "string" ? options.system_prompt : null;
    const requirements = parseRequirements(options.interpretation_requirements);
    if (requirements !== null) {
      const segments = segmentsFromParts(
        options.prompt_template_parts,
        requirements,
      );
      if (segments !== null) return { segments, usedFallback: false, systemPrompt, reviewAvailable: true };
    }
    const template = options.prompt_template;
    if (typeof template === "string") {
      return {
        segments: [{ kind: "text", text: template }],
        usedFallback: true,
        systemPrompt,
        reviewAvailable: true,
      };
    }
  }
  return {
    segments: [{ kind: "text", text: event.llm_draft ?? "" }],
    usedFallback: true,
    reviewAvailable: false,
  };
}
