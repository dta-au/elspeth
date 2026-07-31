// ============================================================================
// interpretationStepLabel.ts — humanise an interpretation event's
// affected_node_id into an operator-facing step label.
//
// Internal node ids (e.g. `guided_xform_1`) must not leak into user-facing
// copy — those still resolve to a per-plugin label ("Summarise", "Fetch",
// "Output", …). A user-meaningful node id (e.g. `extract_invoice`, chosen by
// the author) is preferred instead: title-casing the author's own name for
// the step reads better than a generic plugin verb once the pipeline has more
// than one node of the same plugin. The raw id is the fallback when the node
// is absent from the composition entirely.
//
// Presentational only — reads existing store state, never mutates.
// ============================================================================

import type { CompositionState } from "@/types/index";

/**
 * Well-known plugin → step-label map.  Other plugins present in a composition
 * are humanised from the plugin name (see `titleCase`).
 */
const PLUGIN_STEP_LABELS: Record<string, string> = {
  llm: "Summarise",
  web_scrape: "Fetch",
  field_mapper: "Output",
};

/**
 * Ids the Composer generates itself (`guided_xform_1`, `guided_llm_2`, …) —
 * never author-chosen, so never worth title-casing as a "name". Any id NOT
 * matching this pattern is treated as user-meaningful.
 */
const GENERATED_NODE_ID_RE = /^guided_[a-z]+_\d+$/;

/** Title-case a snake/space-delimited string ("field_mapper" → "Field Mapper"). */
function titleCase(value: string): string {
  return value
    .split(/[_\s]+/)
    .filter((part) => part.length > 0)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

/**
 * Humanised step label for a bare plugin name — the SAME mapping the
 * acknowledgement cards use ("Summarise step · prompt"), exposed for surfaces
 * that hold a plugin directly (e.g. the wire-stage topology) rather than a
 * composition node id. Keeping every surface on this one mapping means the
 * wiring list, the problems strip, and the acknowledge cards all name a step
 * identically.
 */
export function stepLabelForPlugin(plugin: string): string {
  return PLUGIN_STEP_LABELS[plugin] ?? titleCase(plugin);
}

/**
 * Resolve the plugin backing an affected_node_id by searching the
 * composition's nodes, then sources, then outputs.  Returns null when the
 * id is absent (or the composition is unavailable).
 */
export function resolveNodePlugin(
  state: CompositionState | null,
  nodeId: string | null,
): string | null {
  if (state === null || nodeId === null) return null;
  const node = state.nodes.find((candidate) => candidate.id === nodeId);
  if (node && node.plugin) return node.plugin;
  const source = state.sources[nodeId];
  if (source) return source.plugin;
  const output = state.outputs.find((candidate) => candidate.name === nodeId);
  if (output) return output.plugin;
  return null;
}

/**
 * Step label for a composition node id, or null when the id does not resolve
 * (component absent from the composition). THE single choke point for the
 * node-name preference: a user-meaningful id (anything but the Composer's
 * own `guided_<plugin>_<n>` generated form) is title-cased and used as-is —
 * the author's own name for the step beats a generic plugin verb. A
 * generated id falls back to `stepLabelForPlugin`.
 *
 * Returns null (not the raw id) on an unresolved id so callers can choose
 * their own "unknown" phrasing — `humaniseStepLabel` below falls back to the
 * raw id; `validationHumaniser`'s callers (PipelineValidationSummary,
 * ReadinessRowDetail) fall back to a generic phrase instead, since an
 * internal id must never leak into that prose.
 */
export function stepLabelForNodeId(
  state: CompositionState | null,
  nodeId: string | null,
): string | null {
  const plugin = resolveNodePlugin(state, nodeId);
  if (plugin === null || nodeId === null) return null;
  if (GENERATED_NODE_ID_RE.test(nodeId)) {
    return stepLabelForPlugin(plugin);
  }
  return titleCase(nodeId);
}

/**
 * Humanised step label for an affected_node_id. Falls back to the raw id
 * when the node is absent, and to a generic phrase when there is no id at
 * all. See `stepLabelForNodeId` for the node-name preference this builds on.
 */
export function humaniseStepLabel(
  state: CompositionState | null,
  nodeId: string | null,
): string {
  return stepLabelForNodeId(state, nodeId) ?? nodeId ?? "this step";
}

/**
 * Build a stable pipeline-step ordering index over the composition:
 * sources (object order) → nodes (array order) → outputs.  Used to order the
 * acknowledgement cards by pipeline step before created_at.
 */
export function buildStepOrder(
  state: CompositionState | null,
): Map<string, number> {
  const order = new Map<string, number>();
  if (state === null) return order;
  let index = 0;
  for (const key of Object.keys(state.sources)) order.set(key, index++);
  for (const node of state.nodes) order.set(node.id, index++);
  for (const output of state.outputs) order.set(output.name, index++);
  return order;
}
