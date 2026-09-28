
import { sortedSourceEntries, sourceComponentId } from "@/utils/compositionState";
import type { CompositionState } from "@/types/index";

export const DISCARD_CONNECTION = "discard";

/**
 * The co-sentinel to DISCARD_CONNECTION: a `routes` alias whose target is
 * this fork marker is not itself a connection — it is expanded via the
 * node's `fork_to` array into one producer registration per branch.
 */
export const FORK_CONNECTION = "fork";

export const COALESCE_POLICIES = ["require_all", "quorum", "best_effort", "first"] as const;
export type CoalescePolicy = (typeof COALESCE_POLICIES)[number];

export const COALESCE_MERGES = ["union", "nested", "select"] as const;
export type CoalesceMerge = (typeof COALESCE_MERGES)[number];

// Node kinds that publish their success output IMPLICITLY, under their own
// node id, when they declare no `on_success`. A downstream node reaches them
// by naming the node id in its `input`.
//
// This mirrors `_producer_resolver.published_success_connection`, which is the
// backend authority and the ONLY place the rule is decided:
//
//     if node.on_success is not None: return node.on_success
//     if node.node_type in {"queue", "coalesce", "aggregation"}: return node.id
//     return None
//
// `aggregation` is in that set for the same reason coalesce is:
// `AggregationSettings.on_success` is `str | None = None`, and
// `core/dag/builder.py` registers `agg_settings.name` when it is omitted.
// It was missed on the first pass here and in the Python.
//
// Do not re-derive it from `on_success` here. `CoalesceSettings.on_success` is
// OPTIONAL ("Required when coalesce is terminal"), and a queue never declares
// one at all, so asking `node.on_success` directly reports a correctly-wired
// node as publishing nothing — which drew a working fork/coalesce pipeline as
// two disconnected fragments (session 3f02c8fa). row_union and collector both
// REQUIRE on_success and so are deliberately NOT here: giving them an implicit
// id would invent a connection the DAG builder does not resolve.
export const IMPLICIT_SELF_PUBLISHING_NODE_TYPES: ReadonlySet<string> = new Set([
  "queue",
  "coalesce",
  "aggregation",
]);

export function publishedSuccessConnection(node: {
  id: string;
  node_type: string;
  on_success: string | null;
}): string | null {
  if (node.on_success !== null && node.on_success !== undefined) {
    return node.on_success;
  }
  return IMPLICIT_SELF_PUBLISHING_NODE_TYPES.has(node.node_type)
    ? node.id
    : null;
}

/**
 * Node kinds whose INBOUND topology is declared by `branches` — an
 * alias -> connection-name mapping — rather than by the scalar `input`, which
 * carries only the backend-compatible first-branch placeholder.
 *
 * Both kinds share this shape in the RUNTIME; they do NOT share it on the
 * wire. `branches` is legally a list as well as a map, and the composer
 * normalises list -> identity mapping only for row_union
 * (composer/state.py `_row_union_normalized_branches`), while
 * `_serialize_branches` deliberately "preserves list-vs-mapping semantics"
 * for a coalesce. So a coalesce reaches this component still holding a list,
 * and `branchEntries` below applies the rule rather than assuming it away.
 *
 * Only row_union was ever read through `branches` at all, so a coalesce fell
 * through to ordinary `input` inference and rendered a single arm from
 * whichever producer happened to own the placeholder connection
 * (elspeth-625e85c59b). `coalesce` is the kind the composer's planner
 * actually authors — every fan-in node in the saved corpus is one, and no
 * saved session has ever held a row_union. That is a COVERAGE fact, not a
 * disuse one: row_union is taught to the planner
 * (composer/planner_authoring_aids.py ships a fork_row_union exemplar), is
 * used by examples/row_union_ab_experiment, and reaches this component
 * directly through Import YAML. Neither arm is dead; only one is exercised.
 *
 * This set governs INBOUND inference only. The outbound-semantics rewrite
 * stays row_union-scoped on purpose — see the comment at
 * components/inspector/GraphView.tsx, above `authoritativeRowUnionOutboundSemantics`.
 */
export const FAN_IN_NODE_TYPES: ReadonlySet<string> = new Set([
  "row_union",
  "coalesce",
]);

/**
 * A fan-in node's alias -> connection pairs, in declaration order.
 *
 * The list form is not a second meaning, it is shorthand for the identity
 * mapping: `CoalesceSettings.normalize_branches` (core/config.py:991-1005)
 * returns `{b: b for b in v}`, so `["a","b"]` IS `{a: "a", b: "b"}`. That
 * rule belongs to the runtime; this reads it rather than restating it, and
 * rather than declining it — bailing out on a list silently reproduced the
 * very defect this machinery exists to fix, on a composition that validates
 * green.
 *
 * A duplicate entry is an authoring error the backend rejects
 * (normalize_branches raises); here the alias-key dedup in phase 1 collapses
 * it to one arm rather than drawing two identical ones.
 */
export function branchEntries(
  branches: string[] | Record<string, string> | null | undefined,
): [string, string][] {
  if (branches === null || branches === undefined) return [];
  return Array.isArray(branches)
    ? branches.map((name) => [name, name])
    : Object.entries(branches);
}

export function buildConnectionProducers(
  state: CompositionState,
): Map<string, string[]> {
  return indexConnectionProducers(state).producers;
}

/**
 * The registration arms the walk below makes. Exported so the cross-check
 * fixture can assert that it exercises EVERY one of them.
 *
 * Why the list exists at all: the four producer-registration loops live twice
 * — here and in GraphView's `buildProducerRegistry` — and their equivalence is
 * guarded by one shared fixture. A new node KIND fails loudly (the Python
 * parity test compares the sets), but a new publication FIELD added to one
 * copy and not the other fails SILENTLY unless the fixture happens to
 * exercise it, reproducing the Graph-tab-vs-Spec-tab divergence this module
 * exists to prevent (systems I-2; the full lift stays ticketed
 * elspeth-fcb0637b07).
 *
 * `push` takes an arm kind from this union, so a new arm cannot be added
 * without naming it here, and naming it here fails the fixture's coverage
 * assertion until the fixture actually exercises it. An un-exercised arm
 * trips the guard instead of passing it.
 */
export const PRODUCER_ARM_KINDS = [
  "source_on_success",
  "published",
  "on_error",
  "routes",
  "fork_to",
] as const;

export type ProducerArmKind = (typeof PRODUCER_ARM_KINDS)[number];

export interface ConnectionProducerIndex {
  /** connection → ids of the components that write it. */
  producers: Map<string, string[]>;
  /** Which registration arms actually fired for this composition. Read by the
   *  cross-check's vacuity guard; production callers want `producers` and use
   *  `buildConnectionProducers`. */
  arms: ReadonlySet<ProducerArmKind>;
}

/** `buildConnectionProducers` plus the record of which arms it took. */
export function indexConnectionProducers(
  state: CompositionState,
): ConnectionProducerIndex {
  const producers = new Map<string, string[]>();
  const arms = new Set<ProducerArmKind>();
  const push = (arm: ProducerArmKind, connection: string, producerId: string): void => {
    arms.add(arm);
    const existing = producers.get(connection);
    if (existing === undefined) producers.set(connection, [producerId]);
    else if (!existing.includes(producerId)) existing.push(producerId);
  };
  // sortedSourceEntries, not Object.entries: this is what
  // GraphView.tsx's `buildProducerRegistry` source loop uses, and a faithful lift keeps the deterministic ordering. Importing it
  // — and sourceComponentId, the other half of that source registration —
  // does not break the leaf contract: utils/compositionState.ts imports only
  // types (`import type { CompositionState, SourceSpec } from "@/types/index"`
  // is its whole import block), so no React and no store enters this module's
  // dependency graph.
  for (const [sourceName, source] of sortedSourceEntries(state)) {
    if (source.on_success && source.on_success !== DISCARD_CONNECTION) {
      push("source_on_success", source.on_success, sourceComponentId(sourceName));
    }
  }
  for (const node of state.nodes) {
    const published = publishedSuccessConnection(node);
    if (published && published !== DISCARD_CONNECTION) push("published", published, node.id);
    // CompositionState has one universal nullable on_error slot, but a
    // collector rejects non-null values: its failure is a whole-group verdict.
    // Fail closed for legacy/malformed states so no phantom error edge reaches
    // either the Spec routing view or the canvas producer index.
    if (node.node_type !== "collector" && node.on_error && node.on_error !== DISCARD_CONNECTION) {
      push("on_error", node.on_error, node.id);
    }
    if (node.routes) {
      for (const target of Object.values(node.routes)) {
        if (target !== FORK_CONNECTION && target !== DISCARD_CONNECTION) {
          push("routes", target, node.id);
        }
      }
    }
    if (
      node.node_type === "gate"
      && node.routes
      && Object.values(node.routes).includes(FORK_CONNECTION)
      && node.fork_to
    ) {
      for (const branchConnection of node.fork_to) push("fork_to", branchConnection, node.id);
    }
  }
  return { producers, arms };
}
