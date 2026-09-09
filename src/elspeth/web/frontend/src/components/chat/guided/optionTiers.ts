import type { FieldTier, NodeOptionSummary } from "@/types/guided";

/**
 * The presentational tier one projected node-option pair renders at.
 *
 * Absent tier = "common": durable guided turns written before the backend
 * started emitting `tier` replay through the same decoder and must keep
 * rendering inline rather than silently sinking into the Technical details
 * disclosure (elspeth-ca456d9d8d).
 */
export function optionTier(entry: NodeOptionSummary): FieldTier {
  return entry.tier ?? "common";
}
