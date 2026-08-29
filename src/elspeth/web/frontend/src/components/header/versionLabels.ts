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
 *  deliberately excluded: they are exactly what changes on snapshot-only
 *  persists (guided turn pointers, transition flips, crash captures).
 *
 *  ONE narrow exception lives outside this list and is compared separately
 *  by `guidedBlobBindings`: composer_meta.guided_session.reviewed_sources'
 *  blob bindings. Those are pipeline content — which file the pipeline
 *  reads — that the guided redaction erases from `sources` and preserves
 *  only there. Nothing else under composer_meta may move the verdict. */
const CONTENT_FIELDS = [
  "sources",
  "nodes",
  "edges",
  "outputs",
  "metadata",
] as const;

/** Wire keys on a reviewed guided source whose value carries a storage path.
 *  Mirrors GUIDED_REVIEWED_BLOB_PATH_KEYS in
 *  web/composer/guided_blob_refs.py. */
const GUIDED_BLOB_PATH_KEYS = ["path", "file"] as const;

/** Prefix of a PUBLIC blob sentinel path (BLOB_REF_PATH_PREFIX in
 *  web/composer/guided/protocol.py). A carrier bearing it names the blob and
 *  is left intact by the redaction; a carrier without it is either an
 *  ordinary path or the constant redaction sentinel, neither of which
 *  identifies a blob. */
const BLOB_SENTINEL_PREFIX = "blob:";

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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A readable projection and its canonical key, or an explicit "could not
 *  read this row" — never a key that silently stands in for absent data. */
type GuidedBlobProjection =
  | { readonly readable: true; readonly key: string }
  | { readonly readable: false };

/**
 * Canonical projection of the guided reviewed-source blob bindings on a
 * version row — the one axis of "which file does this pipeline read" that
 * survives the guided redaction.
 *
 * Why this is needed at all: a guided commit goes through the manual
 * set_source path, which cannot prove `path == storage_path`, so it strips
 * `blob_ref` from the executable source. `_state_response`
 * (web/sessions/routes/_helpers.py) then runs
 * `redact_guided_snapshot_storage_paths`, which overwrites that source's
 * `path`/`file` carriers with a CONSTANT sentinel. Two versions whose only
 * difference is a swapped input file are therefore byte-identical across
 * every CONTENT_FIELD. The retained discriminator is the reviewed snapshot's
 * own binding, and it takes two shapes on the wire:
 *
 *   - explicit ref: `options.blob_ref` (a canonical UUID) survives redaction
 *     while the snapshot's path carriers are collapsed to the sentinel;
 *   - public sentinel: the carriers are already `blob:<uuid>` strings and the
 *     snapshot passes through untouched (that arm also leaves the live
 *     source's carriers naming the blob, so `sources` discriminates it too —
 *     reading it here is belt-and-braces, not the load-bearing arm).
 *
 * `composer_meta` is untrusted wire data, so it is parsed rather than typed
 * (ADR-032): anything that does not match the shape above yields
 * `readable: false`, and the caller refuses to claim "no change" — the same
 * stance `hasContentPayload` takes for absent content.
 */
function guidedBlobBindings(
  version: CompositionStateVersion,
): GuidedBlobProjection {
  const meta = (version as CompositionStateVersion & { composer_meta?: unknown })
    .composer_meta;
  if (meta === undefined || meta === null) {
    return { readable: true, key: "" };
  }
  if (!isRecord(meta)) {
    return { readable: false };
  }
  const guided = meta.guided_session;
  if (guided === undefined || guided === null) {
    return { readable: true, key: "" };
  }
  if (!isRecord(guided)) {
    return { readable: false };
  }
  // A guided_session without reviewed_sources carries no binding to compare —
  // that is absence of a blob claim, not unreadable data.
  const reviewed = guided.reviewed_sources;
  if (reviewed === undefined || reviewed === null) {
    return { readable: true, key: "" };
  }
  if (!isRecord(reviewed)) {
    return { readable: false };
  }

  const bindings: Array<readonly [string, unknown]> = [];
  for (const [stableId, snapshot] of Object.entries(reviewed)) {
    if (!isRecord(snapshot)) {
      return { readable: false };
    }
    const options = snapshot.options;
    if (options === undefined || options === null) {
      continue;
    }
    if (!isRecord(options)) {
      return { readable: false };
    }
    const refs: string[] = [];
    if (typeof options.blob_ref === "string") {
      refs.push(options.blob_ref);
    }
    for (const key of GUIDED_BLOB_PATH_KEYS) {
      const carrier = options[key];
      if (
        typeof carrier === "string" &&
        carrier.startsWith(BLOB_SENTINEL_PREFIX)
      ) {
        refs.push(carrier);
      }
    }
    if (refs.length === 0) {
      continue;
    }
    bindings.push([
      stableId,
      {
        name: typeof snapshot.name === "string" ? snapshot.name : null,
        refs: [...refs].sort(),
      },
    ] as const);
  }
  bindings.sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0));
  return { readable: true, key: stableStringify(bindings) };
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
 * lineage. The `wire_review` widening of `allowed_base_state_ids` relaxes
 * only which base a PROPOSAL may name; the guided back-edit that follows it
 * still persists the current head as its lineage, so that widening never
 * reaches derived_from_state_id. The one writer that points strictly older
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
 * (guided turn pointer, transition flip, crash/preflight capture) rather
 * than a user edit. The label it drives says "no VISIBLE change" for exactly
 * this reason: the claim this predicate can support is about the projection,
 * not about the owned state.
 *
 * Compares the content fields via stable stringify, plus the guided
 * reviewed-source blob bindings (see `guidedBlobBindings`) which are content
 * the redaction moves out of `sources`. Every other bookkeeping axis —
 * is_valid, validation_*, and the rest of composer_meta — cannot flip the
 * verdict. A missing previous row (first version, or the pagination-window
 * edge), a row without content payloads (the synthesized current entry), or
 * an unreadable composer_meta returns false: absence of data is not evidence
 * of "no change".
 *
 * Residual narrowing: the comparison still sees the REDACTED wire projection,
 * not the owned state. Folding in the blob bindings closes the guided
 * blob-swap case — previously the wire's only discriminator for it, and
 * excluded — but legacy pre-`sources` rows still arrive with sources null,
 * and any future redaction that collapses a field without leaving a retained
 * discriminator would be invisible here in the same way. The client can only
 * compare what the wire shows; a backend-shipped per-version content hash
 * would be the exact fix and would earn a stronger word than "visible".
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
  const bindings = guidedBlobBindings(version);
  const previousBindings = guidedBlobBindings(previousVersion);
  if (!bindings.readable || !previousBindings.readable) {
    return false;
  }
  if (bindings.key !== previousBindings.key) {
    return false;
  }
  return CONTENT_FIELDS.every(
    (field) =>
      stableStringify(version[field] ?? null) ===
      stableStringify(previousVersion[field] ?? null),
  );
}
