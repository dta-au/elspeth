// src/lib/compositionContent.ts
//
// "Did the AUTHORED pipeline actually change?" — the content question the
// frontend previously answered with `compositionState.version`, which is a
// write counter, not a content identity.
//
// Why this exists (elspeth-986801d218). Every settlement writes a new
// `composition_states` row and bumps `version`, including settlements that
// author nothing: a post-completion guided chat persists a byte-identical
// row so the reply has a state to hang off. Two version-keyed subscribers in
// `stores/subscriptions.ts` then fired on that bump — one cleared the
// validation verdict, the other POSTed `/validate` — and until the
// re-validation landed `useCompletionOutcome` read `executionReady=false` and
// the completed heading flipped "Pipeline ready" → "Pipeline updated". Asking
// a question about a pipeline must not un-verify it.
//
// The field set is the SAME one the backend hashes into
// `composition_content_hash` (`web/composer/pipeline_proposal.py:796-805`):
// `sources`, `nodes`, `edges`, `outputs`, `metadata` — authored content,
// excluding `version`, `id`, timestamps, validation results and guided
// metadata. Keep the two in step: a field the backend starts hashing is a
// field a change to which must re-validate here.
//
// SAFE DIRECTION — when unsure, return FALSE. A false "not equal" costs one
// redundant `/validate` (exactly today's behaviour, so no regression); a
// false "equal" suppresses a clear + re-validate that a REAL edit needed and
// leaves a stale verdict on screen. Every branch below therefore resolves an
// unknown shape to "not equal". Skipping the frontend validate never widens
// admission either way: the server re-runs the full preflight at execute
// (`web/execution/service.py`), so the run gate is unaffected.

import type { CompositionState } from "@/types/index";

/** The authored-content projection of a composition state. */
type CompositionContent = Pick<
  CompositionState,
  "sources" | "nodes" | "edges" | "outputs" | "metadata"
>;

/**
 * Structural equality over JSON-shaped values.
 *
 * Object comparison is key-ORDER-insensitive (same keys, pairwise-equal
 * values) because both sides are decoded JSON whose key order is an artifact
 * of serialization, not of content. Arrays keep their order — node and output
 * order is authored meaning. Anything neither side can be read as (a
 * function, a Date, a Map) falls to `Object.is` and therefore compares equal
 * only by identity, which is the safe direction.
 */
function deepEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (typeof left !== "object" || typeof right !== "object") return false;
  if (left === null || right === null) return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  if (Array.isArray(left) && Array.isArray(right)) {
    if (left.length !== right.length) return false;
    return left.every((item, index) => deepEqual(item, right[index]));
  }
  const leftEntries = Object.entries(left as Record<string, unknown>);
  const rightRecord = right as Record<string, unknown>;
  const rightKeys = Object.keys(rightRecord);
  if (leftEntries.length !== rightKeys.length) return false;
  return leftEntries.every(
    ([key, value]) =>
      Object.prototype.hasOwnProperty.call(rightRecord, key) &&
      deepEqual(value, rightRecord[key]),
  );
}

/**
 * True when two composition states carry the SAME authored content — the
 * version bump between them wrote nothing a user authored.
 *
 * Returns false when either side is absent: "no state" and "a state" are not
 * the same content, and the callers' skip is only ever safe on a genuine
 * content match.
 */
export function compositionContentEqual(
  left: CompositionContent | null | undefined,
  right: CompositionContent | null | undefined,
): boolean {
  if (left === null || left === undefined) return false;
  if (right === null || right === undefined) return false;
  return (
    deepEqual(left.sources, right.sources) &&
    deepEqual(left.nodes, right.nodes) &&
    deepEqual(left.edges, right.edges) &&
    deepEqual(left.outputs, right.outputs) &&
    deepEqual(left.metadata, right.metadata)
  );
}
