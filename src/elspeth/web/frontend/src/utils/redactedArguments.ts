// src/utils/redactedArguments.ts
//
// The single frontend authority for reading the SUMMARY FORMS that the
// composer's argument redactor emits (elspeth-b1c14dd3c2).
//
// Every proposal the approval surface renders carries
// `arguments_redacted_json` — the post-redaction payload, never the arguments
// the planner authored. Two argument shapes do not survive that pass as
// structured data, because plugin options and metadata are open,
// LLM-authored surfaces that routinely carry filesystem paths, connection
// strings, and API keys:
//
//   * `options` / `patch` on set_source, upsert_node, set_output,
//     set_pipeline, and the three patch_*_options tools becomes a canonical
//     JSON string describing only the payload's SHAPE:
//       {"_option_shape":"mapping","entry_count":2,
//        "value_shape_counts":{"mapping":0,"scalar":2,"sequence":0,"set":0}}
//     Producer: `_summarize_set_source_options` in
//     src/elspeth/web/composer/redaction.py.
//
//   * `patch` on set_metadata becomes a closed sentinel naming only which
//     recognised keys the patch touches: `<metadata-patch:description,name>`,
//     `<metadata-patch:empty>`, `<metadata-patch:invalid>`, and `unknown`
//     appended when the patch carries a key outside {description, name}.
//     Producer: `_summarize_set_metadata_patch` in the same module.
//
// That redaction is deliberate and correct; consumers must accept what the
// producer emits. This module exists so the decision "what does this
// redacted value actually tell us" is made ONCE. Four call sites in
// ProposalDiff.tsx previously each read `patch` as a plain object, which the
// producer never emits, and all four projections were silently dead.
//
// Honesty contract: every decoder returns null for a form it does not
// recognise. Callers must treat null as "no projection", never as "no
// change" — the same distinction ProposalDiff.tsx draws. These decoders
// recover SHAPE ONLY. No decoder can recover an option key, an option value,
// or a metadata value, and none should ever appear to.

import { plural } from "@/utils/plural";

/**
 * The closed value-shape vocabulary `_option_shape_class` classifies into.
 * Mirrors `_OPTION_SHAPE_CLASSES` in redaction.py; a shape outside this set
 * means the grammar moved and the summary is not ours to read.
 */
export const OPTION_SHAPE_CLASSES = ["mapping", "scalar", "sequence", "set"] as const;
export type OptionShapeClass = (typeof OPTION_SHAPE_CLASSES)[number];

/** The recognised metadata keys the sentinel can name, plus its catch-all. */
const METADATA_PATCH_KEYS = ["description", "name"] as const;
export type MetadataPatchKey = (typeof METADATA_PATCH_KEYS)[number];
const METADATA_UNKNOWN_TOKEN = "unknown";

const METADATA_SENTINEL_PREFIX = "<metadata-patch:";
const METADATA_SENTINEL_SUFFIX = ">";
const METADATA_SENTINEL_EMPTY = "<metadata-patch:empty>";
const METADATA_SENTINEL_INVALID = "<metadata-patch:invalid>";

/** What a redacted option summary discloses: shape and counts, nothing more. */
export interface RedactedOptionSummary {
  /** Container kind of the payload root. */
  rootShape: OptionShapeClass;
  /** Number of immediate entries — for a mapping, how many keys. */
  entryCount: number;
  /** Counts of the immediate values by shape class. */
  shapeCounts: Readonly<Record<OptionShapeClass, number>>;
}

/**
 * What a set_metadata patch sentinel discloses. Key IDENTITY survives (unlike
 * options, whose keys do not) but no value does — so a caller can say WHICH
 * metadata field a proposal touches and must never claim what it becomes.
 *
 * `<metadata-patch:invalid>` decodes to null rather than a variant: the
 * producer emits it for a patch that is not a mapping at all, which is a
 * malformed proposal, not a described change.
 */
export type MetadataPatchSummary =
  | {
      kind: "keys";
      /** Recognised metadata fields the patch sets. Sorted, deduplicated. */
      keys: readonly MetadataPatchKey[];
      /** The patch also carries a key outside the recognised set. */
      touchesUnknownField: boolean;
    }
  | { kind: "empty" };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isOptionShapeClass(value: unknown): value is OptionShapeClass {
  return (
    typeof value === "string" &&
    (OPTION_SHAPE_CLASSES as readonly string[]).includes(value)
  );
}

/** A count is only meaningful as a non-negative integer. */
function asCount(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

function decodeShapeCounts(value: unknown): Record<OptionShapeClass, number> | null {
  if (!isRecord(value)) return null;
  const counts = {} as Record<OptionShapeClass, number>;
  for (const shapeClass of OPTION_SHAPE_CLASSES) {
    const count = asCount(value[shapeClass]);
    if (count === null) return null;
    counts[shapeClass] = count;
  }
  return counts;
}

/**
 * Decode a redacted option/patch summary.
 *
 * Returns null for anything that is not one — including a raw options object
 * (which the live path never produces), a non-JSON string, and the producer's
 * `"<invalid-options>"` marker, which is emitted as a JSON *string* and so
 * parses to text rather than to the summary object.
 */
export function decodeRedactedOptionSummary(value: unknown): RedactedOptionSummary | null {
  if (typeof value !== "string") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return null;
  }
  if (!isRecord(parsed)) return null;

  const rootShape = parsed["_option_shape"];
  const entryCount = asCount(parsed["entry_count"]);
  const shapeCounts = decodeShapeCounts(parsed["value_shape_counts"]);
  if (!isOptionShapeClass(rootShape) || entryCount === null || shapeCounts === null) {
    return null;
  }
  return { rootShape, entryCount, shapeCounts };
}

/**
 * True when a value is a redacted option summary rather than comparable data.
 *
 * Call sites that diff a proposal's arguments against unredacted composition
 * state use this to SKIP the key: an options mapping in state can never equal
 * the summary string standing in for the proposed one, so comparing them
 * reports a difference on every proposal, whether or not one exists.
 */
export function isRedactedOptionSummary(value: unknown): boolean {
  return decodeRedactedOptionSummary(value) !== null;
}

/**
 * One-line description of what a redacted option summary actually says.
 *
 * Deliberately phrased as a count of ENTRIES IN THE PAYLOAD, not as the
 * resulting option set: for a patch, merge semantics mean the entry count is
 * not the number of options the target ends up with.
 */
export function describeRedactedOptionSummary(summary: RedactedOptionSummary): string {
  if (summary.entryCount === 0) return "no entries";
  return `${plural(summary.entryCount, "entry", "entries")}, keys and values redacted`;
}

function isMetadataPatchKey(value: string): value is MetadataPatchKey {
  return (METADATA_PATCH_KEYS as readonly string[]).includes(value);
}

/**
 * Decode a set_metadata patch sentinel.
 *
 * Returns null for `<metadata-patch:invalid>`, for any token outside the
 * closed vocabulary, and for any value that is not a sentinel string at all
 * (including a raw patch object, which the live path never produces).
 * Token ORDER is not enforced: the producer sorts and appends `unknown` last,
 * but a consumer that also depended on the ordering would break on a
 * cosmetic producer change without gaining any safety.
 */
export function decodeMetadataPatchSummary(value: unknown): MetadataPatchSummary | null {
  if (typeof value !== "string") return null;
  if (value === METADATA_SENTINEL_INVALID) return null;
  if (value === METADATA_SENTINEL_EMPTY) return { kind: "empty" };
  if (!value.startsWith(METADATA_SENTINEL_PREFIX) || !value.endsWith(METADATA_SENTINEL_SUFFIX)) {
    return null;
  }

  const body = value.slice(
    METADATA_SENTINEL_PREFIX.length,
    value.length - METADATA_SENTINEL_SUFFIX.length,
  );
  if (body === "") return null;

  const keys = new Set<MetadataPatchKey>();
  let touchesUnknownField = false;
  for (const token of body.split(",")) {
    if (token === METADATA_UNKNOWN_TOKEN) {
      touchesUnknownField = true;
      continue;
    }
    if (!isMetadataPatchKey(token)) return null;
    keys.add(token);
  }
  return {
    kind: "keys",
    keys: METADATA_PATCH_KEYS.filter((key) => keys.has(key)),
    touchesUnknownField,
  };
}
