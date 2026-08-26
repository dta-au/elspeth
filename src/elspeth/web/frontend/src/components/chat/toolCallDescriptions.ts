import type { ToolCall } from "@/types/api";

/**
 * Generic, audience-facing descriptions of what each composer tool call does.
 *
 * These are deliberately **kind-of-operation** descriptions, not instance
 * descriptions — they tell a user what a tool call of this type does in
 * general, without referring to the specific arguments the LLM passed.
 *
 * If a tool name is not in this map, ToolCallCard falls back to a generic
 * "Composer tool call." string so the tooltip always has content.
 *
 * The read-only / mutating split is load-bearing: liveToolCallLabel derives
 * its no-stamp prefix from which half a tool name lives in, so a mid-flight
 * mutation is never mislabelled as a lookup.
 */
const READ_ONLY_TOOL_CALL_DESCRIPTIONS: Record<string, string> = {
  // Read-only lookups (rendered as the "i" ribbon, no proposal).
  list_sources:
    "Browses the catalog of available source plugins on this deployment.",
  list_transforms:
    "Browses the catalog of available transform and gate plugins.",
  list_sinks: "Browses the catalog of available sink plugins.",
  list_models:
    "Looks up the LLM models available on this deployment's credentials.",
  list_sessions: "Looks up your previous composer sessions.",
  get_plugin_schema:
    "Reads a plugin's configuration schema to understand its options.",
  get_plugin_assistance:
    "Looks up usage guidance and worked examples for a specific plugin.",
  get_pipeline_state:
    "Reads the current pipeline state being composed in this session.",
  get_audit_info:
    "Looks up audit metadata for the current session (run IDs, lineage anchors).",
  get_expression_grammar:
    "Looks up the expression-language grammar used by routing and templating.",
  diff_pipeline:
    "Compares two pipeline configurations to show what changed between them.",
  preview_pipeline:
    "Builds the pipeline in memory to check for errors before applying.",
  explain_validation_error:
    "Looks up a plain-language explanation for a validation error code.",
  generate_yaml: "Renders the current pipeline as a YAML document.",
};

const MUTATING_TOOL_CALL_DESCRIPTIONS: Record<string, string> = {
  // Mutating tool calls (rendered as a proposal card with Accept / Reject).
  set_source:
    "Sets the pipeline's data source — what records the pipeline starts from.",
  clear_source: "Clears the pipeline's data source.",
  set_pipeline:
    "Replaces the entire pipeline configuration in a single operation.",
  set_output: "Adds or replaces an output sink — where results are written.",
  set_metadata:
    "Sets the pipeline's metadata (name, description, tags).",
  upsert_node:
    "Adds a new transform or gate node, or replaces an existing one with the same id.",
  upsert_edge:
    "Adds or replaces a connection between two nodes in the pipeline.",
  remove_node: "Removes a transform or gate node from the pipeline.",
  remove_edge: "Removes a connection between two nodes.",
  remove_output: "Removes an output sink from the pipeline.",
  patch_source_options:
    "Updates one or more configuration options on the source without replacing it.",
  patch_node_options:
    "Updates one or more configuration options on a transform or gate node.",
  patch_output_options:
    "Updates one or more configuration options on an output sink.",
  new_session: "Starts a fresh composer session.",
  save_session: "Saves the current composer session so it can be resumed later.",
  load_session: "Resumes a previously-saved composer session.",
  delete_session: "Removes a saved composer session.",
};

export const TOOL_CALL_DESCRIPTIONS: Record<string, string> = {
  ...READ_ONLY_TOOL_CALL_DESCRIPTIONS,
  ...MUTATING_TOOL_CALL_DESCRIPTIONS,
};

export function describeToolCall(name: string): string {
  return TOOL_CALL_DESCRIPTIONS[name] ?? "Composer tool call.";
}

/**
 * True when `name` is a KNOWN read-only lookup.
 *
 * Exported because the read-only/mutating split is the honesty guarantee this
 * module exists to keep, and it had no membership oracle: the map was
 * module-private, so adding `set_pipeline` to the read-only half would ship
 * "Looked up: set_pipeline" with the whole suite green (test-quality review
 * 2026-08-13, TQ-5).  Both label functions route through this predicate, so
 * the test that pins its membership is pinning the code production actually
 * runs rather than a parallel declaration.
 *
 * Unknown names are NOT read-only: absence of evidence is not evidence of a
 * lookup, and the fallbacks ("Ran", "Running") claim dispatch only.
 */
export function isReadOnlyToolCall(name: string): boolean {
  return name in READ_ONLY_TOOL_CALL_DESCRIPTIONS;
}

/** The exact read-only membership, for the oracle that pins it. */
export const READ_ONLY_TOOL_CALL_NAMES: readonly string[] = Object.keys(
  READ_ONLY_TOOL_CALL_DESCRIPTIONS,
);

/** The exact mutating membership, for the disjointness oracle. */
export const MUTATING_TOOL_CALL_NAMES: readonly string[] = Object.keys(
  MUTATING_TOOL_CALL_DESCRIPTIONS,
);

/**
 * Outcome→label vocabulary for a proposal-less tool call, extracted from
 * ToolCallCard so the post-turn ribbon and the mid-flight live tool log
 * cannot drift apart. The outcome is server-stamped by GET /messages from
 * the Tier-1 tool rows (elspeth-f5e6723133).
 *
 * "Looked up" is reserved for names in the read-only description map: the
 * server stamps "completed" — not "applied" — for successful calls that
 * create no composition-state version, and that class includes durable
 * blob-store mutations (create_blob / update_blob / delete_blob /
 * wire_blob_inline_ref) and interpretation-event writes
 * (request_interpretation_review). Labelling those "Looked up" would
 * mislabel a durable write as a read, so a "completed" stamp on a name
 * outside the read-only map falls to the neutral "Completed" — a claim
 * the server confirmed the call finished, not that it was a lookup.
 *
 * A missing stamp (`outcome === undefined`, e.g. history rows written
 * before outcome stamping existed) is a different fact: there is NO server
 * evidence the call finished. "Completed" would fabricate success, so an
 * unstamped non-read-only name renders "Ran" — the call was dispatched,
 * nothing more is claimed. Unstamped read-only names keep "Looked up":
 * a lookup has no failure mode worth distinguishing on the ribbon and the
 * label makes no success claim about state.
 */
export interface ToolCallLabelParts {
  /** Evidential prefix — "Running", "Applied", "Failed", "Looked up", … */
  prefix: string;
  /** Trailing qualifier, e.g. " (not applied)". "" for every other case. */
  qualifier: string;
}

function outcomeLabelParts(
  name: string,
  outcome: ToolCall["outcome"],
): ToolCallLabelParts {
  const prefix =
    outcome === "applied"
      ? "Applied"
      : outcome === "rejected"
        ? "Attempted"
        : outcome === "failed"
          ? "Failed"
          : outcome === "cancelled"
            ? "Cancelled"
            : isReadOnlyToolCall(name)
              ? "Looked up"
              : outcome === "completed"
                ? "Completed"
                : "Ran";
  return {
    prefix,
    qualifier: outcome === "rejected" ? " (not applied)" : "",
  };
}

export function toolCallOutcomeLabel(
  name: string,
  outcome: ToolCall["outcome"],
): string {
  const { prefix, qualifier } = outcomeLabelParts(name, outcome);
  return `${prefix}: ${name}${qualifier}`;
}

/**
 * Label for a MID-FLIGHT tool call in the live tool log. Stamped outcomes
 * reuse toolCallOutcomeLabel verbatim. Without a stamp, the fabrication
 * doctrine forbids claiming "Applied" — but "Looked up" would be equally
 * dishonest for a mutating call, so anything not known to be a read-only
 * lookup (mutating tools and unknown names alike) gets the "Running"
 * prefix: mid-flight, the call is literally in progress. The settled
 * post-turn ribbon renders the same no-stamp case as "Ran" (see
 * toolCallOutcomeLabel) — past tense, still claiming dispatch only.
 *
 * DELIBERATE TENSE EXCEPTION — do not "fix" this in the wrong direction.
 * An unstamped READ-ONLY name keeps the past-tense "Looked up:" here rather
 * than diverting to a "Looking up:" progressive. The prefixes in this module
 * are graded by what is KNOWN, not by wall-clock tense, and "Looked up"
 * already makes no success claim about state (see toolCallOutcomeLabel) — so
 * it is honest mid-flight as well as after. Minting a live-log-only
 * progressive would put two labels on one evidential state and give a future
 * maintainer a second vocabulary to keep in sync with the ribbon for no gain:
 * a lookup has no state effect an operator could be misled about. The
 * MUTATING half is exactly where the distinction earns its keep, and it has
 * it.
 */
export function liveToolCallLabelParts(
  name: string,
  outcome: ToolCall["outcome"],
): ToolCallLabelParts {
  if (outcome === undefined && !isReadOnlyToolCall(name)) {
    return { prefix: "Running", qualifier: "" };
  }
  return outcomeLabelParts(name, outcome);
}

export function liveToolCallLabel(
  name: string,
  outcome: ToolCall["outcome"],
): string {
  const { prefix, qualifier } = liveToolCallLabelParts(name, outcome);
  return `${prefix}: ${name}${qualifier}`;
}
