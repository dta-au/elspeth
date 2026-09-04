// src/components/chat/ProposalDiff.tsx
//
// Fragment-level before/after projection for mutating composer proposals
// (elspeth-10f76f9250). Mutating tool calls used to render only a raw
// JSON.stringify of their arguments while RecoveryDiff sat one directory over
// with a full added/changed/removed diff UI. This module reuses that diff
// rendering (DiffEntryRow + the recovery-diff row styles, loaded globally via
// styles/index.css) on the standard proposal-approval surface.
//
// Honesty contract: this is a DISPLAY projection, not a client-side replay of
// server mutation semantics.
// - The "after" side of every row is literally what the proposal's arguments
//   say (identity + summary derived from the args), never a client-side
//   simulation of the committed result.
// - Those arguments are REDACTED. The input is `arguments_redacted_json`, so
//   structural identity (ids, plugin names, sink names) survives while open
//   LLM-authored surfaces — plugin options and metadata values — arrive as
//   summaries. utils/redactedArguments is the single authority for reading
//   them, and a row may only claim what actually survived: see
//   optionPatchEntries and metadataPatchEntries for what each form supports.
// - The "before" side is the matching fragment of the CURRENT composition
//   state. Callers must only render this for pending, non-stale proposals —
//   for stale or already-resolved proposals the current state is no longer
//   the state the proposal targets, and ToolCallCard falls back to the
//   structured argument-field rendering instead.
// - Tools whose arguments do not map onto state fragments (session tools,
//   blob tools, unknown names) return null: "no projection", not "no change".

import type { CompositionState, EdgeSpec, NodeSpec } from "@/types/api";
import type { OutputSpec, SourceSpec } from "@/types/index";
import {
  DiffEntryRow,
  edgeSummary,
  nodeSummary,
  outputSummary,
  sourceEntrySummary,
  stableStringify,
  type DiffEntry,
  type DiffSection,
} from "@/components/recovery/RecoveryDiff";
import { plural } from "@/utils/plural";
import {
  decodeMetadataPatchSummary,
  decodeRedactedOptionSummary,
  describeRedactedOptionSummary,
  isRedactedOptionSummary,
} from "@/utils/redactedArguments";

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/** Bounded single-line rendering of an option/metadata value for row summaries. */
function valueSummary(value: unknown): string {
  const rendered = JSON.stringify(value);
  if (rendered === undefined) return "(not set)";
  return rendered.length > 60 ? `${rendered.slice(0, 57)}...` : rendered;
}

function sourceSummaryFromArgs(name: string, args: Record<string, unknown>): string | null {
  const plugin = asString(args.plugin);
  if (plugin === null) return null;
  return name === "source" ? plugin : `${name} (${plugin})`;
}

function nodeSummaryFromArgs(args: Record<string, unknown>): string | null {
  const nodeType = asString(args.node_type);
  if (nodeType === null) return null;
  return [nodeType, asString(args.plugin) ?? "no plugin"].join(" ");
}

function edgeSummaryFromArgs(args: Record<string, unknown>): string | null {
  const fromNode = asString(args.from_node);
  const toNode = asString(args.to_node);
  const edgeType = asString(args.edge_type);
  if (fromNode === null || toNode === null || edgeType === null) return null;
  return `${fromNode} -> ${toNode} (${edgeType})`;
}

function outputSummaryFromArgs(args: Record<string, unknown>): string | null {
  const name = asString(args.sink_name);
  const plugin = asString(args.plugin);
  if (name === null || plugin === null) return null;
  return `${name} (${plugin})`;
}

function upsertEntry(
  section: DiffSection,
  identity: string,
  before: unknown,
  beforeSummary: string | null,
  afterSummary: string,
  after: unknown,
): DiffEntry {
  if (before === undefined) {
    return {
      kind: "added",
      section,
      identity,
      before: undefined,
      after,
      beforeSummary: "",
      afterSummary,
    };
  }
  return {
    kind: "changed",
    section,
    identity,
    before,
    after,
    beforeSummary: beforeSummary ?? "",
    afterSummary,
  };
}

function removeEntry(
  section: DiffSection,
  identity: string,
  before: unknown,
  beforeSummary: string,
): DiffEntry {
  return {
    kind: "removed",
    section,
    identity,
    before,
    after: undefined,
    beforeSummary,
    afterSummary: "",
  };
}

/**
 * The single option row for a patch_*_options proposal.
 *
 * PER-KEY ROWS ARE NOT DERIVABLE HERE (elspeth-b1c14dd3c2). This projection
 * reads `arguments_redacted_json`, and the redactor replaces the patch with a
 * shape summary — entry count and value-shape counts, no key names and no
 * values (`_summarize_set_source_options`). Plugin options carry filesystem
 * paths, connection strings and API keys, so that summarisation is the
 * correct behaviour and the consumer's job is to say honestly what survives
 * it. An earlier version of this function walked `Object.entries(patch)` as
 * though the raw patch arrived; on the live path it never did, `asRecord`
 * returned null for the summary string, and the arm was dead.
 *
 * The BEFORE side is not redacted — it is the frontend's own composition
 * state — so the row still contrasts the real current option set against the
 * measured size of the proposed patch.
 *
 * Returns null when the patch is not a summary this module recognises ("no
 * projection"), and [] for an empty patch, which merges to a no-op.
 */
function optionPatchEntries(
  target: string,
  currentOptions: Record<string, unknown>,
  patch: unknown,
): DiffEntry[] | null {
  const summary = decodeRedactedOptionSummary(patch);
  // A patch is always a mapping (the tools' argument models require a dict);
  // any other root shape means this is not the payload we think it is.
  if (summary === null || summary.rootShape !== "mapping") return null;
  if (summary.entryCount === 0) return [];
  return [
    {
      kind: "changed",
      section: "option",
      identity: `${target}.options`,
      before: currentOptions,
      // No "after" value exists to hold: the proposed options were redacted
      // before they reached this surface.
      after: undefined,
      beforeSummary: `${plural(Object.keys(currentOptions).length, "option")} set`,
      afterSummary: `patch of ${describeRedactedOptionSummary(summary)}`,
    },
  ];
}

/**
 * Metadata rows for a set_metadata proposal.
 *
 * Unlike options, key IDENTITY survives redaction: the sentinel names which
 * of {name, description} the patch touches (`_summarize_set_metadata_patch`).
 * Values do not survive, which constrains what a row may claim:
 *
 *   * Every row is "changed", never "added". "Added" would assert the patch
 *     sets a non-null value, and a patch that clears a field to null is
 *     indistinguishable from one that sets it.
 *   * No row is suppressed as a no-op. The old code skipped keys whose new
 *     value equalled the current one; that comparison is no longer possible,
 *     so a row means "the proposal writes this field", not "this field
 *     changes value".
 *
 * A patch touching a key outside {name, description} gets its own row rather
 * than being dropped: this is an approval surface, and silently omitting part
 * of what the operator is approving is the failure mode to avoid. The field
 * cannot be named because unrecognised key names are LLM-controlled text and
 * the producer collapses them all to one token.
 *
 * Returns null for `<metadata-patch:invalid>` and any unrecognised form, and
 * [] for `<metadata-patch:empty>` — a projection that found nothing to report.
 */
function metadataPatchEntries(
  current: CompositionState,
  patch: unknown,
): DiffEntry[] | null {
  const summary = decodeMetadataPatchSummary(patch);
  if (summary === null) return null;
  if (summary.kind === "empty") return [];

  const entries: DiffEntry[] = [];
  for (const key of summary.keys) {
    const before = current.metadata[key];
    entries.push({
      kind: "changed",
      section: "metadata",
      identity: key,
      before,
      after: undefined,
      beforeSummary:
        before === null || before === undefined ? "(not set)" : valueSummary(before),
      afterSummary: "new value redacted",
    });
  }
  if (summary.touchesUnknownField) {
    entries.push({
      kind: "changed",
      section: "metadata",
      identity: "(unrecognised field)",
      before: undefined,
      after: undefined,
      beforeSummary: "not a pipeline metadata field",
      afterSummary: "field name and value redacted",
    });
  }
  return entries;
}

/**
 * set_pipeline replaces the whole pipeline; project the args' collections
 * against the current state by identity. For identities present on both
 * sides, only the keys carried by the args are compared — a key the state
 * fragment does not hold at all is skipped rather than called a change.
 *
 * WHAT A "Changed" ROW HERE DOES AND DOES NOT MEAN. It reports that a key
 * present on both sides compares unequal. It does NOT prove the planner
 * authored that difference: this projection reads `arguments_redacted_json`,
 * whose payload has been through the redactor's argument model, and pydantic
 * materialises every unset optional field as an explicit null. "Omitted" and
 * "explicitly set to null" are therefore indistinguishable at this point, so
 * a default-filled null can present as a change (elspeth-d6147d73ed — the
 * information is destroyed upstream of this file and recovering it needs a
 * producer-side decision).
 *
 * Redacted option summaries are excluded from the comparison entirely; see
 * providedKeysDiffer.
 */
function setPipelineEntries(
  current: CompositionState,
  args: Record<string, unknown>,
): DiffEntry[] {
  const entries: DiffEntry[] = [];

  // Sources: named map (args.sources) or the legacy single source (args.source).
  const proposedSources = new Map<string, Record<string, unknown>>();
  const namedSources = asRecord(args.sources);
  if (namedSources !== null) {
    for (const [name, spec] of Object.entries(namedSources)) {
      const record = asRecord(spec);
      if (record !== null) proposedSources.set(name, record);
    }
  }
  const legacySource = asRecord(args.source);
  if (legacySource !== null && !proposedSources.has("source")) {
    proposedSources.set("source", legacySource);
  }

  const currentSources = current.sources ?? {};
  const sourceNames = Array.from(
    new Set([...Object.keys(currentSources), ...proposedSources.keys()]),
  ).sort((left, right) => left.localeCompare(right));
  for (const name of sourceNames) {
    const before: SourceSpec | undefined = currentSources[name];
    const after = proposedSources.get(name);
    if (after === undefined) {
      if (before !== undefined) {
        entries.push(removeEntry("source", name, before, sourceEntrySummary([name, before])));
      }
      continue;
    }
    const afterSummary = sourceSummaryFromArgs(name, after) ?? name;
    if (before === undefined) {
      entries.push(upsertEntry("source", name, undefined, null, afterSummary, after));
    } else if (providedKeysDiffer(before as unknown as Record<string, unknown>, after)) {
      entries.push(upsertEntry("source", name, before, sourceEntrySummary([name, before]), afterSummary, after));
    }
  }

  entries.push(
    ...replaceCollectionEntries<NodeSpec>(
      "node",
      current.nodes,
      (node) => node.id,
      nodeSummary,
      args.nodes,
      (item) => asString(item.id),
      (item) => nodeSummaryFromArgs(item) ?? "node",
      new Map([["id", "id"]]),
    ),
    ...replaceCollectionEntries<EdgeSpec>(
      "edge",
      current.edges,
      (edge) => edge.id,
      edgeSummary,
      args.edges,
      (item) => asString(item.id),
      (item) => edgeSummaryFromArgs(item) ?? "edge",
      new Map([["id", "id"]]),
    ),
    ...replaceCollectionEntries<OutputSpec>(
      "output",
      current.outputs,
      (output) => output.name,
      outputSummary,
      args.outputs,
      (item) => asString(item.sink_name),
      (item) => outputSummaryFromArgs(item) ?? "output",
      // set_pipeline output args key their identity as sink_name; the state
      // spec calls the same field name.
      new Map([["sink_name", "name"]]),
    ),
  );
  return entries;
}

/**
 * Compare only the keys the proposal actually provides against the current
 * fragment. Keys the fragment does not carry at all (e.g. blob_id /
 * inline_blob on source args) are skipped — we cannot honestly call them a
 * change to state the state model does not hold.
 *
 * Redacted option summaries are skipped for the same reason, one step
 * further on. The proposal's `options` is a shape-summary STRING while the
 * state's is the real mapping, so they never compare equal: before this skip,
 * every set_pipeline source/node/output row reported "Changed" on every
 * proposal, whether or not the options differed. A skip under-reports a real
 * options change; the alternative asserted a change that may not exist, on
 * the surface an operator uses to approve one.
 */
function providedKeysDiffer(
  before: Record<string, unknown>,
  provided: Record<string, unknown>,
  keyAliases: Map<string, string> = new Map(),
): boolean {
  for (const [key, value] of Object.entries(provided)) {
    const beforeKey = keyAliases.get(key) ?? key;
    if (!(beforeKey in before)) continue;
    if (isRedactedOptionSummary(value)) continue;
    if (stableStringify(before[beforeKey]) !== stableStringify(value)) {
      return true;
    }
  }
  return false;
}

function replaceCollectionEntries<T>(
  section: DiffSection,
  currentItems: T[],
  identityOf: (item: T) => string,
  summarize: (item: T) => string,
  proposedRaw: unknown,
  proposedIdentityOf: (item: Record<string, unknown>) => string | null,
  proposedSummarize: (item: Record<string, unknown>) => string,
  keyAliases: Map<string, string>,
): DiffEntry[] {
  const entries: DiffEntry[] = [];
  const proposedById = new Map<string, Record<string, unknown>>();
  if (Array.isArray(proposedRaw)) {
    for (const raw of proposedRaw) {
      const record = asRecord(raw);
      if (record === null) continue;
      const identity = proposedIdentityOf(record);
      if (identity !== null) proposedById.set(identity, record);
    }
  }
  const currentById = new Map(currentItems.map((item) => [identityOf(item), item]));
  const identities = Array.from(
    new Set([...currentById.keys(), ...proposedById.keys()]),
  ).sort((left, right) => left.localeCompare(right));

  for (const identity of identities) {
    const before = currentById.get(identity);
    const after = proposedById.get(identity);
    if (after === undefined) {
      if (before !== undefined) {
        entries.push(removeEntry(section, identity, before, summarize(before)));
      }
      continue;
    }
    if (before === undefined) {
      entries.push(upsertEntry(section, identity, undefined, null, proposedSummarize(after), after));
      continue;
    }
    if (providedKeysDiffer(before as Record<string, unknown>, after, keyAliases)) {
      entries.push(
        upsertEntry(section, identity, before, summarize(before), proposedSummarize(after), after),
      );
    }
  }
  return entries;
}

/**
 * Project a mutating proposal's arguments onto before/after diff entries
 * against the current composition state.
 *
 * Returns null when no structured projection exists — unknown tool, malformed
 * arguments, or no current state to diff against. Callers fall back to the
 * structured argument-field rendering. Returns [] when a projection exists
 * but finds nothing to report (e.g. a patch whose keys are all no-ops).
 */
export function buildProposalDiff(
  toolName: string,
  args: Record<string, unknown>,
  currentState: CompositionState | null,
): DiffEntry[] | null {
  if (currentState === null) return null;

  switch (toolName) {
    case "set_source": {
      const name = asString(args.source_name) ?? "source";
      const afterSummary = sourceSummaryFromArgs(name, args);
      if (afterSummary === null) return null;
      const before = currentState.sources?.[name];
      return [
        upsertEntry(
          "source",
          name,
          before,
          before === undefined ? null : sourceEntrySummary([name, before]),
          afterSummary,
          args,
        ),
      ];
    }
    case "clear_source": {
      const name = asString(args.source_name) ?? "source";
      const before = currentState.sources?.[name];
      if (before === undefined) return [];
      return [removeEntry("source", name, before, sourceEntrySummary([name, before]))];
    }
    case "upsert_node": {
      const id = asString(args.id);
      const afterSummary = nodeSummaryFromArgs(args);
      if (id === null || afterSummary === null) return null;
      const before = currentState.nodes.find((node) => node.id === id);
      return [
        upsertEntry(
          "node",
          id,
          before,
          before === undefined ? null : nodeSummary(before),
          afterSummary,
          args,
        ),
      ];
    }
    case "remove_node": {
      const id = asString(args.id);
      if (id === null) return null;
      const before = currentState.nodes.find((node) => node.id === id);
      if (before === undefined) return [];
      return [removeEntry("node", id, before, nodeSummary(before))];
    }
    case "upsert_edge": {
      const id = asString(args.id);
      const afterSummary = edgeSummaryFromArgs(args);
      if (id === null || afterSummary === null) return null;
      const before = currentState.edges.find((edge) => edge.id === id);
      return [
        upsertEntry(
          "edge",
          id,
          before,
          before === undefined ? null : edgeSummary(before),
          afterSummary,
          args,
        ),
      ];
    }
    case "remove_edge": {
      const id = asString(args.id);
      if (id === null) return null;
      const before = currentState.edges.find((edge) => edge.id === id);
      if (before === undefined) return [];
      return [removeEntry("edge", id, before, edgeSummary(before))];
    }
    case "set_output": {
      const name = asString(args.sink_name);
      const afterSummary = outputSummaryFromArgs(args);
      if (name === null || afterSummary === null) return null;
      const before = currentState.outputs.find((output) => output.name === name);
      return [
        upsertEntry(
          "output",
          name,
          before,
          before === undefined ? null : outputSummary(before),
          afterSummary,
          args,
        ),
      ];
    }
    case "remove_output": {
      const name = asString(args.sink_name);
      if (name === null) return null;
      const before = currentState.outputs.find((output) => output.name === name);
      if (before === undefined) return [];
      return [removeEntry("output", name, before, outputSummary(before))];
    }
    // The four arms below read `args.patch`, which the redactor replaces with
    // a summary before it reaches this surface. They hand it to the shared
    // decoders in utils/redactedArguments rather than to asRecord, which
    // returns null for every string and left all four arms unreachable
    // (elspeth-b1c14dd3c2).
    case "set_metadata": {
      return metadataPatchEntries(currentState, args.patch);
    }
    case "patch_source_options": {
      const name = asString(args.source_name) ?? "source";
      const fragment = currentState.sources?.[name];
      if (fragment === undefined) return null;
      return optionPatchEntries(name, fragment.options, args.patch);
    }
    case "patch_node_options": {
      const nodeId = asString(args.node_id);
      if (nodeId === null) return null;
      const fragment = currentState.nodes.find((node) => node.id === nodeId);
      if (fragment === undefined) return null;
      return optionPatchEntries(nodeId, fragment.options, args.patch);
    }
    case "patch_output_options": {
      const name = asString(args.sink_name);
      if (name === null) return null;
      const fragment = currentState.outputs.find((output) => output.name === name);
      if (fragment === undefined) return null;
      return optionPatchEntries(name, fragment.options, args.patch);
    }
    case "set_pipeline": {
      return setPipelineEntries(currentState, args);
    }
    default:
      return null;
  }
}

interface ProposalChangesProps {
  entries: DiffEntry[];
}

/**
 * Renders projected proposal diff entries with the shared recovery-diff row
 * styling. The caller (ToolCallCard) owns the derivability/staleness gate and
 * passes only entries it already computed.
 */
export function ProposalChanges({ entries }: ProposalChangesProps) {
  return (
    <div className="proposal-diff" data-testid="proposal-diff">
      <div className="proposal-diff-heading">Proposed changes</div>
      {entries.length === 0 ? (
        <p className="proposal-diff-empty">
          No difference from the current pipeline.
        </p>
      ) : (
        <ul className="recovery-diff-list proposal-diff-list">
          {entries.map((entry) => (
            <DiffEntryRow
              entry={entry}
              key={`${entry.kind}:${entry.section}:${entry.identity}`}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

interface ArgumentFieldsProps {
  args: Record<string, unknown>;
}

/**
 * Structured field-level rendering of a proposal's (redacted) arguments —
 * the fallback surface when no before/after projection is derivable (stale
 * or resolved proposals, unknown tools, missing state). One row per
 * top-level argument; nested objects render as bounded, formatted JSON.
 */
export function ArgumentFields({ args }: ArgumentFieldsProps) {
  const fields = Object.entries(args);
  if (fields.length === 0) {
    return (
      <p className="tool-call-arg-empty" data-testid="proposal-arg-fields">
        No settings change in this step.
      </p>
    );
  }
  return (
    <dl className="tool-call-arg-fields" data-testid="proposal-arg-fields">
      {fields.map(([key, value]) => (
        <div className="tool-call-arg-field" key={key}>
          <dt>
            <code>{key}</code>
          </dt>
          <dd>
            {typeof value === "object" && value !== null ? (
              <pre className="tool-call-arg-nested">
                {JSON.stringify(value, null, 2)}
              </pre>
            ) : (
              <code>{valueSummary(value)}</code>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}
