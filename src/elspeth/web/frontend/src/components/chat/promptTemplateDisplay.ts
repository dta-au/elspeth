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
  segments: PromptDisplaySegment[];
  /**
   * True when the structured parts could not be rendered and the result is
   * a single flat text segment from the fallback chain (node
   * prompt_template, then event llm_draft).
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

/**
 * Resolve the display segments for a prompt-template review card.
 *
 * Fallback chain: structured parts → node's current `prompt_template`
 * string → the event's frozen `llm_draft` ("" when null).
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
    const requirements = parseRequirements(options.interpretation_requirements);
    if (requirements !== null) {
      const segments = segmentsFromParts(
        options.prompt_template_parts,
        requirements,
      );
      if (segments !== null) return { segments, usedFallback: false };
    }
    const template = options.prompt_template;
    if (typeof template === "string" && template !== "") {
      return {
        segments: [{ kind: "text", text: template }],
        usedFallback: true,
      };
    }
  }
  return {
    segments: [{ kind: "text", text: event.llm_draft ?? "" }],
    usedFallback: true,
  };
}
