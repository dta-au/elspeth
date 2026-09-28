import { TOOL_CALL_DESCRIPTIONS } from "@/components/chat/toolCallDescriptions";
import type { ChatMessage, CompositionStateVersion } from "@/types/index";

/**
 * Pure derivation of per-version history labels for HeaderVersionSelector.
 *
 * Labels come only from server-authenticated facts already on the client:
 * the tool-call outcome stamps on messages (elspeth-f5e6723133) and the
 * version rows' own lineage ids. Tool names on rejected or unstamped calls
 * never label a version — mirroring the backend rule that a tool call's
 * name is a claim, not an outcome.
 */

/** The pipeline-content axes of a version row. Bookkeeping axes
 *  (composer_meta, is_valid, validation_*, plugin_policy_findings) are
 *  deliberately excluded: they can change without changing pipeline content. */
const CONTENT_FIELDS = [
  "sources",
  "nodes",
  "edges",
  "outputs",
  "metadata",
] as const;


/** JSON stringify with recursively sorted object keys, so two semantically
 *  equal states serialize identically regardless of key order. */
function stableStringify(value: unknown): string {
  if (value === undefined) {
    return "undefined";
  }
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(stableStringify).join(",")}]`;
  }
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, entryValue]) => entryValue !== undefined)
    .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0));
  return `{${entries
    .map(([key, entryValue]) => `${JSON.stringify(key)}:${stableStringify(entryValue)}`)
    .join(",")}}`;
}

function hasContentPayload(version: CompositionStateVersion): boolean {
  return CONTENT_FIELDS.every((field) => version[field] !== undefined);
}


/**
 * Name of the applied tool call that created this version, or null.
 * Joins on the server-derived outcome stamp only: outcome === "applied"
 * with a matching applied_state_version. Rejected/failed/unstamped calls
 * never match.
 */
export function appliedToolCallName(
  version: CompositionStateVersion,
  messages: ChatMessage[],
): string | null {
  for (const message of messages) {
    if (!message.tool_calls) {
      continue;
    }
    for (const call of message.tool_calls) {
      if (
        call.outcome === "applied" &&
        call.applied_state_version === version.version
      ) {
        return call.function.name;
      }
    }
  }
  return null;
}

/**
 * The raw applied-tool identifier behind this version, as "Applied: <name>",
 * or null when the version has no applied tool-call stamp. The visible row
 * carries the audience-facing sentence (deriveVersionLabel); this is the
 * `title` for operators who need the exact tool name (elspeth-af559a0bab).
 */
export function versionOperationIdentifier(
  version: CompositionStateVersion,
  messages: ChatMessage[],
): string | null {
  const name = appliedToolCallName(version, messages);
  return name === null ? null : `Applied: ${name}`;
}

/**
 * The lineage row a revert-shaped version points at, or null.
 *
 * derived_from_state_id is generic lineage, not a revert marker: every
 * non-revert writer in web/sessions/service.py persists either null, the
 * current head's id, or an `expected_current_state_id` that the same
 * transaction has just proved equal to the head under the write lock.
 * Proposal settlement additionally hard-requires base == current head (it
 * raises StaleComposeStateError otherwise) before writing the base as the
 * lineage. The one writer that points strictly older
 * than the adjacent predecessor is the state-revert copy path
 * (`provenance="session_seed"`), which persists its revert target.
 *
 * That writer-set invariant — not anything on the wire — is what makes the
 * strictly-older heuristic safe; re-audit the `_insert_composition_state`
 * call sites before relying on it more heavily. An unresolvable target
 * (outside the paginated window) yields null rather than a guess.
 */
function revertLineageTarget(
  version: CompositionStateVersion,
  allVersions: CompositionStateVersion[],
): CompositionStateVersion | null {
  const derivedFrom = version.derived_from_state_id;
  if (!derivedFrom) {
    return null;
  }
  const target = allVersions.find((candidate) => candidate.id === derivedFrom);
  if (target && target.version < version.version - 1) {
    return target;
  }
  return null;
}

/** The structural fact deriveVersionLabel's copy stands for. */
export type VersionLabelKind = "applied" | "revert" | "seed" | "edited";

/**
 * The structural fact PLUS the row it was derived from, so the copy layer
 * never has to re-derive (and re-null-check) what the discriminant already
 * proved. `versionLabelKind` projects this to the bare kind; the visible
 * label switches over it.
 */
type VersionLabelFacts =
  | { readonly kind: "applied"; readonly toolName: string }
  | { readonly kind: "revert"; readonly target: CompositionStateVersion }
  | { readonly kind: "seed" }
  | { readonly kind: "edited" };

/**
 * Priority: applied tool-call stamp, then revert lineage, then the v1 seed,
 * then a generic edit — unchanged from the pre-split implementation.
 *
 * The applied join runs FIRST because an applied stamp is a direct
 * server-authenticated statement about what produced this version, while
 * strictly-older lineage is only an inference from the writer-set invariant
 * documented on `revertLineageTarget` above. Ordering the direct evidence
 * ahead of the inference is what keeps the inference from ever having to be
 * load-bearing. Do not reorder these two without re-auditing the
 * derived_from_state_id writers in web/sessions/service.py.
 */
function versionLabelFacts(
  version: CompositionStateVersion,
  allVersions: CompositionStateVersion[],
  messages: ChatMessage[],
): VersionLabelFacts {
  const toolName = appliedToolCallName(version, messages);
  if (toolName !== null) {
    return { kind: "applied", toolName };
  }
  const target = revertLineageTarget(version, allVersions);
  if (target !== null) {
    return { kind: "revert", target };
  }
  return version.version === 1 ? { kind: "seed" } : { kind: "edited" };
}

/**
 * The structural fact deriveVersionLabel's copy stands for. Grouping in the
 * history tree keys on this, never on the visible string
 * (elspeth-c8a402a9a4): a copy rewrite must not be able to turn grouping
 * off, and a label the register batch rephrases must not silently change
 * which rows collapse.
 */
export function versionLabelKind(
  version: CompositionStateVersion,
  allVersions: CompositionStateVersion[],
  messages: ChatMessage[],
): VersionLabelKind {
  return versionLabelFacts(version, allVersions, messages).kind;
}

/**
 * Derive the history label for a version row — the audience-facing copy for
 * the structural kind above. Every arm is reachable and carries the row it
 * needs, so no branch guesses at data the discriminant already resolved.
 */
export function deriveVersionLabel(
  version: CompositionStateVersion,
  allVersions: CompositionStateVersion[],
  messages: ChatMessage[],
): string {
  const facts = versionLabelFacts(version, allVersions, messages);
  switch (facts.kind) {
    case "applied": {
      const sentence = TOOL_CALL_DESCRIPTIONS[facts.toolName];
      return sentence !== undefined
        ? `Applied: ${sentence}`
        : `Applied: ${facts.toolName}`;
    }
    case "revert":
      return `Reverted to v${facts.target.version}`;
    case "seed":
      return "Session created";
    case "edited":
      return "Edited";
  }
}

/**
 * True when nothing this projection can see distinguishes this version from
 * the adjacent previous version — the signature of a bookkeeping snapshot
 * rather
 * than a user edit. The label it drives says "no VISIBLE change" for exactly
 * this reason: the claim this predicate can support is about the projection,
 * not about the owned state.
 *
 * Compares the content fields via stable stringify. Bookkeeping axes such as
 * is_valid, validation_*, and composer_meta cannot flip the verdict. A missing
 * previous row (first version, or the pagination-window edge), or a row without
 * content payloads (the synthesized current entry), returns false: absence of
 * data is not evidence of "no change".
 *
 * This compares the visible wire projection, not the owned state. If a future
 * redaction hides content without a retained discriminator, a backend-shipped
 * per-version content hash would be needed for a stronger claim than "visible".
 */
export function isSnapshotOnly(
  version: CompositionStateVersion,
  previousVersion: CompositionStateVersion | null | undefined,
): boolean {
  if (!previousVersion) {
    return false;
  }
  if (!hasContentPayload(version) || !hasContentPayload(previousVersion)) {
    return false;
  }
  return CONTENT_FIELDS.every(
    (field) =>
      stableStringify(version[field] ?? null) ===
      stableStringify(previousVersion[field] ?? null),
  );
}
