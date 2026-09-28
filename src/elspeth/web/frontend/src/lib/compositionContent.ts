
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
