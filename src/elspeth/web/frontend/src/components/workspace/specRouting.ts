// ============================================================================
// specRouting — reader-register phrases for the Spec tab's routing <dd>s
// (elspeth-93f5621f18, Wave 3; carries the elspeth-b9ebdf9011 branches-as-
// prose fix from PipelineSpecView.routingValue). A routing value is one of
// three things:
//   * a CONNECTION name (`on_success: raw_rows`) — the reader wants the
//     component on the other end, resolved from the composition itself;
//   * a closed POLICY enum (`policy: require_all`) — a fixed phrase;
//   * a passthrough (`discard`, numbers) — left to the caller.
// Every phrase carries `raw` so the renderer can put the wire form in
// `title`. Pure: reads a CompositionState, never mutates.
//
// `componentPhrase` has a SECOND consumer outside this tab: the run-consent
// dialog (sidebar/ExecuteButton.tsx) names each component in its egress
// disclosure through it, so a described source cannot read one way on the
// Spec tab and another on the surface the user actually consents from
// (ux M-4). Its cautions below therefore bind on a CONSENT surface, not just
// a review one — weigh that before "simplifying" it. `routingPhrase` is
// PipelineSpecView's alone.
//
// The topology rules — what a node publishes, and what a fan-in node reads —
// are NOT decided here. They live in lib/graphTopology.ts, lifted out
// of GraphView so the Spec tab and the Graph tab cannot disagree about the
// same pipeline. In particular `branches` resolves UPSTREAM: a fan-in node's
// branches are its inputs (core/config.py CoalesceSettings.branches), and its
// own `input` is only a first-branch placeholder, so resolving them
// downstream makes the node name itself.
// ============================================================================

import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";
import {
  descriptionLabel,
  isComponentPresent,
  stepLabelForNodeId,
} from "@/components/chat/interpretationStepLabel";
import {
  branchEntries,
  buildConnectionProducers,
  DISCARD_CONNECTION,
  FAN_IN_NODE_TYPES,
  FORK_CONNECTION,
  type CoalesceMerge,
  type CoalescePolicy,
} from "@/lib/graphTopology";
import { sortedSourceEntries, sourceComponentId } from "@/utils/compositionState";
import type { CompositionState, NodeSpec } from "@/types/index";

export interface RoutingPhrase {
  text: string;
  raw: string;
  /**
   * Which register `text` is in. Absent (the default) means PHRASE: `text` is
   * prose the renderer shows as prose, with `raw` demoted to `title`.
   * "identifier" means `text` is a raw wire value nobody has phrased, and the
   * renderer must show it as code — the diagnosticPhrases.ts rule ("an
   * unknown identifier must never be dressed up as a sentence"), which this
   * file's OPEN map now follows too.
   *
   * Optional rather than required on purpose: exactly one construction site
   * sets it, and every other phrase in this module is prose by construction.
   */
  register?: "phrase" | "identifier";
}

export interface ConnectionIndex {
  /** connection → ids of the components that READ it (node.input, a fan-in
   *  node's branch connections, output.name) */
  consumers: Map<string, string[]>;
  /** connection → ids of the components that WRITE it (published success,
   *  on_error, routes, fork_to, plus the two failure lanes added below) */
  producers: Map<string, string[]>;
}

/**
 * Fields whose value the node WRITES and whose shape is a scalar or an array
 * of connection names; resolve through `consumers`.
 *
 * This is a SHAPE partition, not a direction one, and it is deliberately NOT
 * the same set as the backend's routing-field Literal
 * (web/composer/guided/planning.py, which enumerates
 * on_success | on_error | on_validation_failure | on_write_failure | routes |
 * fork_to). `routes` is genuinely outbound and is missing here only because
 * it is MAP-shaped and handled below alongside `branches`; `branches` is
 * absent for the same shape reason, and is inbound as well. Do not "fix" this
 * set to match planning.py's — that would send a map-shaped value down the
 * scalar path — and do not read its omissions as statements about direction.
 * The direction rule's authority is connection_consumers.py, not this set.
 */
const DOWNSTREAM_FIELDS: ReadonlySet<string> = new Set([
  "on_success",
  "on_error",
  "on_validation_failure",
  "on_write_failure",
  "fork_to",
]);

// The three CLOSED maps below are keyed by a UNION type, not by `string`, so
// adding a member to the frontend member set without phrasing it here is a
// BUILD failure rather than a silent title-cased leak at runtime. Membership
// comes from lib/graphTopology (policy, merge) and from types/index.ts's
// NodeSpec.output_mode; this file states PHRASES and never membership.
//
// Be exact about which drift this catches: it is a compile error to add a
// member to COALESCE_POLICIES/COALESCE_MERGES or to the output_mode union
// without a phrase. It does NOT catch a backend addition — `tsc` cannot see
// Python. That half is the parity assertion in
// tests/unit/web/composer/test_graph_topology_parity.py. Both are needed;
// neither does the other's job.
export const POLICY_PHRASES: Record<CoalescePolicy, string> = {
  require_all: "wait for every branch",
  quorum: "wait for a quorum of branches",
  best_effort: "use whichever branches arrive",
  first: "take the first branch to arrive",
};

export const MERGE_PHRASES: Record<CoalesceMerge, string> = {
  union: "combine every branch's fields",
  nested: "keep each branch's fields under its own name",
  select: "keep the selected branch's fields",
};

export const OUTPUT_MODE_PHRASES: Record<NonNullable<NodeSpec["output_mode"]>, string> = {
  // "default" is a sentence like its siblings, not the bare enum word: a <dd>
  // reading "default" tells the reader nothing they could not see in the YAML.
  default: "use the plugin's own behaviour",
  passthrough: "pass rows through unchanged",
  transform: "emit transformed rows",
};

// scope_policy is the ONE open map here: it has no backend Literal and no
// frontend member set to close against (types/index.ts types NodeSpec
// .scope_policy as `string | null`, and a collector's arrival policy is not a
// coalesce's). Left as a Map over `string` deliberately — closing a map
// against a member set that does not exist would mean inventing the set here.
export const SCOPE_POLICY_PHRASES: ReadonlyMap<string, string> = new Map([
  ["require_all", "wait for every row in the group"],
  ["best_effort", "close the group with whichever rows arrive"],
]);

function closedPhrase(
  map: Readonly<Record<string, string>>,
  value: string,
): string | undefined {
  return Object.prototype.hasOwnProperty.call(map, value) ? map[value] : undefined;
}

/**
 * How one enum field is phrased, and — the half that is NOT uniform — what to
 * do with a value the map does not phrase.
 *
 * A CLOSED map (policy, merge, output_mode) closes against a compile-time
 * union, so its unphrased arm is reachable only because types/index.ts still
 * types the wire field as `string`; it keeps title case, which is what its
 * own test pins. `scope_policy` is the ONE open map — no backend Literal, no
 * frontend member set — so its unphrased arm is genuinely reachable in
 * production, and title-casing it rendered fake prose: "Someday Maybe" reads
 * as a phrase the product chose for a value nobody has phrased at all. That
 * arm renders in the identifier register instead, matching
 * diagnosticPhrases.ts (systems M-A).
 *
 * The two live in one map so the choice is stated beside the phrasing rather
 * than in a second structure keyed by the same field.
 */
interface EnumFieldPhrasing {
  /** The phrase for a known member, or undefined for a value outside the map. */
  phrase: (value: string) => string | undefined;
  /** The register an unphrased value renders in. */
  unphrased: "title-case" | "identifier";
}

const ENUM_FIELDS: ReadonlyMap<string, EnumFieldPhrasing> = new Map([
  ["policy", { phrase: (value) => closedPhrase(POLICY_PHRASES, value), unphrased: "title-case" }],
  ["merge", { phrase: (value) => closedPhrase(MERGE_PHRASES, value), unphrased: "title-case" }],
  ["scope_policy", { phrase: (value) => SCOPE_POLICY_PHRASES.get(value), unphrased: "identifier" }],
  ["output_mode", { phrase: (value) => closedPhrase(OUTPUT_MODE_PHRASES, value), unphrased: "title-case" }],
]);

function push(map: Map<string, string[]>, key: string, id: string): void {
  const existing = map.get(key);
  if (existing === undefined) map.set(key, [id]);
  else if (!existing.includes(id)) existing.push(id);
}

/** Connections this node READS. For a fan-in kind that is its branch
 *  connections and NOT its `input` — the canonical consumer projection
 *  (web/composer/guided/connection_consumers.py) skips `input` for
 *  coalesce/row_union because it is only the first-branch placeholder. A
 *  fan-in node with no branches at all keeps ordinary `input` inference,
 *  matching GraphView's `aliasMappedFanInIds` guard. */
function nodeInputs(node: NodeSpec): string[] {
  if (FAN_IN_NODE_TYPES.has(node.node_type)) {
    const entries = branchEntries(node.branches);
    if (entries.length > 0) return entries.map(([, connection]) => connection);
  }
  return node.input ? [node.input] : [];
}

export function buildConnectionIndex(state: CompositionState): ConnectionIndex {
  // PRODUCERS: the shared rule, from lib/graphTopology. Do NOT re-derive the
  // four registration rules here — GraphView.tsx owned them privately once and
  // that is the defect the graphTopology lift closed. The ids it emits are
  // COMPONENT ids: `sourceComponentId(name)` for a source, the node id for a
  // node — which is why the two source registrations below use the same
  // helper rather than the bare source key.
  const producers = buildConnectionProducers(state);
  const consumers = new Map<string, string[]>();

  // The two engine-level failure routes are layered on top of the shared
  // NAMED-connection producer index. GraphView projects them independently as
  // direct error edges to configured outputs; they do not belong in
  // buildConnectionProducers because the runtime never exposes them as
  // ordinary connections that processing nodes can consume. Both are needed
  // here because the Spec tab prints every routing field as prose, including
  // the failure lanes; omitting either would be asymmetric for no reason a
  // reader could infer — a source's validation-failure lane would resolve
  // upstream while an output's write-failure lane title-cased.
  //
  // The DISCARD_CONNECTION guard is deliberate: `discard` is not a connection
  // (_producer_resolver.py), so registering an output that discards write
  // failures as the PRODUCER of a connection named `discard` would let some
  // other component's `input: discard` resolve to it. RoutingDd tests the
  // sentinel before ever consulting the index, so this is belt-and-braces —
  // but a shared index holding a sentinel as a key is a trap for the next
  // consumer.
  for (const [name, source] of sortedSourceEntries(state)) {
    if (source.on_validation_failure && source.on_validation_failure !== DISCARD_CONNECTION) {
      push(producers, source.on_validation_failure, sourceComponentId(name));
    }
  }
  for (const output of state.outputs) {
    if (output.on_write_failure && output.on_write_failure !== DISCARD_CONNECTION) {
      push(producers, output.on_write_failure, output.name);
    }
  }

  // CONSUMERS: genuinely new — GraphView has no consumer map, it resolves the
  // consumer direction by looking node.input UP against the producer registry.
  for (const node of state.nodes) {
    for (const connection of nodeInputs(node)) push(consumers, connection, node.id);
  }
  for (const output of state.outputs) {
    // An output IS its own connection: OutputSpec carries no `input` field
    // (types/index.ts) — its name is the connection it reads.
    push(consumers, output.name, output.name);
  }
  return { consumers, producers };
}

/** The reader-register name for a source, node or output.
 *
 *  The ladder is description -> acronym-aware title case of the id, matching
 *  lib/validationHumaniser.ts's makePhraseFor, which overlays a description
 *  for all THREE component kinds. `stepLabelForNodeId` alone consults a
 *  description for nodes only (interpretationStepLabel.ts), so a described
 *  source would read "Invoices Csv" here and "Quarterly invoices" in the
 *  validation summary, the audit panel and the chat.
 *
 *  The accepted vocabulary is COMPONENT ids: `sourceComponentId(name)` for a
 *  source (`source`, or `source:<name>`), a node id, an output name. That is
 *  what buildConnectionProducers registers and what the index therefore
 *  holds, and it is the only thing callers pass. A BARE non-default source
 *  key is NOT accepted — it would resolve through the plugin rung and lose
 *  the description. There is no inverse of `sourceComponentId`, so the source
 *  is found by matching the helper's own output over the composition's
 *  sources — never by splitting the prefix off the string.
 *
 *  Callers: PipelineSpecView's routing <dd>s, and ExecuteButton's run-consent
 *  egress lines. Both pass COMPONENT ids; neither may pass a bare non-default
 *  source key (see above).
 *
 *  Never "Removed": this is called with ids the index already resolved, and
 *  with connection names, where absence means dangling rather than deleted.
 *  Not for the <h4> either — the description has its own <p> directly beneath
 *  the heading in PipelineSpecView, so a described structural node would
 *  print the same sentence twice. */
export function componentPhrase(state: CompositionState, id: string): string {
  const sourceEntry = sortedSourceEntries(state).find(
    ([name]) => sourceComponentId(name) === id,
  );
  if (sourceEntry !== undefined) {
    const [name, source] = sourceEntry;
    // The ladder applies to the source NAME, not the prefixed component id.
    return (
      descriptionLabel(source.description)
      ?? stepLabelForNodeId(state, name)
      ?? titleCaseLabel(name)
    );
  }
  const described = descriptionLabel(
    state.outputs.find((output) => output.name === id)?.description,
  );
  return described ?? stepLabelForNodeId(state, id) ?? titleCaseLabel(id);
}

function connectionPhrase(
  state: CompositionState,
  index: ConnectionIndex,
  connection: string,
  direction: "downstream" | "upstream",
): string {
  const ids = (direction === "downstream" ? index.consumers : index.producers).get(connection);
  if (ids === undefined || ids.length === 0) {
    // A DANGLING connection: nothing at the far end. Title-cased alone it is
    // indistinguishable from a resolved one — "Then: Nowhere Yet" reads as a
    // step actually named "Nowhere Yet" — so the card asserted a connection
    // that does not exist, on the surface this wave asks non-engineers to
    // review instead of the YAML. Plain-language suffix, not a <code>
    // register: it matches the phrase register around it and serves the
    // reader this tab is for.
    //
    // The two SENTINELS are excluded because they are not connections at all
    // (_producer_resolver.py), so nothing ever registers them and they would
    // otherwise be permanently marked: `fork` reaches here from a partly-fork
    // routes map, and `discard` from a map entry (the scalar arm returns null
    // earlier). "Every → Fork (not connected)" would be a false statement
    // about a deliberately-routed branch.
    if (connection === FORK_CONNECTION || connection === DISCARD_CONNECTION) {
      return titleCaseLabel(connection);
    }
    return `${titleCaseLabel(connection)} (not connected)`;
  }
  return ids.map((id) => componentPhrase(state, id)).join(", ");
}

function mapPhrase(
  state: CompositionState,
  index: ConnectionIndex,
  entries: [string, string][],
  direction: "downstream" | "upstream",
): RoutingPhrase {
  return {
    text: entries
      .map(
        ([alias, target]) =>
          `${titleCaseLabel(alias)} → ${connectionPhrase(state, index, target, direction)}`,
      )
      .join("; "),
    raw: entries.map(([alias, target]) => `${alias} → ${target}`).join("; "),
  };
}

/**
 * The reader-register phrase for one routing field, or null when the value
 * is not a connection or enum (`discard`, numbers, nulls) and the caller's
 * existing rendering applies.
 */
export function routingPhrase(
  state: CompositionState,
  index: ConnectionIndex,
  field: string,
  value: unknown,
): RoutingPhrase | null {
  if (value === DISCARD_CONNECTION || value === null || value === undefined) return null;
  const enumField = ENUM_FIELDS.get(field);
  if (enumField !== undefined && typeof value === "string") {
    // This runtime arm exists because types/index.ts types policy, merge and
    // scope_policy as `string | null`, so a wire value outside the union is
    // representable. What an unphrased value BECOMES is the field's own
    // choice — see EnumFieldPhrasing.
    const phrased = enumField.phrase(value);
    if (phrased !== undefined) return { text: phrased, raw: value };
    return enumField.unphrased === "identifier"
      ? { text: value, raw: value, register: "identifier" }
      : { text: titleCaseLabel(value), raw: value };
  }
  if (field === "scope_opener" && typeof value === "string") {
    // The one routing field naming a COMPONENT rather than a connection, so
    // the one where absence honestly means "removed". Every other routing
    // value names a connection, where a missing far end is dangling — and
    // "Removed" would assert a deletion that may never have happened.
    return {
      text: isComponentPresent(state, value) ? componentPhrase(state, value) : "Removed",
      raw: value,
    };
  }
  if (field === "input" && typeof value === "string") {
    return { text: connectionPhrase(state, index, value, "upstream"), raw: value };
  }
  if (DOWNSTREAM_FIELDS.has(field)) {
    if (typeof value === "string") {
      return { text: connectionPhrase(state, index, value, "downstream"), raw: value };
    }
    if (Array.isArray(value)) {
      const names = value.map(String);
      return {
        text: names.map((name) => connectionPhrase(state, index, name, "downstream")).join(", "),
        raw: names.join(", "),
      };
    }
  }
  if (field === "routes" || field === "branches") {
    // `routes` is fan-OUT (the gate writes these), `branches` is fan-IN (the
    // coalesce reads these). Resolving branches downstream makes the node
    // name itself, because its own `input` is one of its branch connections.
    const direction = field === "branches" ? "upstream" : "downstream";
    if (Array.isArray(value)) {
      // A list-form `branches` IS the identity mapping (config.py
      // normalize_branches); branchEntries owns that rule. A list-form
      // `routes` is not a thing, but the array arm is kept for both so an
      // unexpected shape still renders as prose rather than JSON.
      const entries: [string, string][] =
        field === "branches"
          ? branchEntries(value.map(String))
          : value.map((name): [string, string] => [String(name), String(name)]);
      return mapPhrase(state, index, entries, direction);
    }
    if (typeof value === "object") {
      const entries = Object.entries(value as Record<string, unknown>).map(
        ([alias, target]): [string, string] => [alias, String(target)],
      );
      if (field === "routes" && entries.every(([, target]) => target === FORK_CONNECTION)) {
        return {
          text: "every row continues to all branches",
          raw: entries.map(([alias]) => `${alias} → fork`).join("; "),
        };
      }
      return mapPhrase(state, index, entries, direction);
    }
  }
  return null;
}
