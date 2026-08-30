// ============================================================================
// graphTopology — THE frontend's single model of how composition components
// join up: what a node publishes, what a fan-in node reads, and what is not
// a connection at all. Every surface that needs to answer "which component is
// on the other end of this connection?" imports from here. If you are about
// to write a second one, this is the one you were looking for.
//
// Deliberately a LEAF module — it imports only types and the type-only
// utils/compositionState.ts — so the Graph tab
// (components/inspector/GraphView.tsx) and the Spec tab
// (components/workspace/specRouting.ts) can both import it with no cycle and
// no React in the dependency graph. Same contract as lib/validationHumaniser.ts
// and components/chat/guided/stepLabels.ts, and it exists for the same reason
// stepLabels.ts does: hand-mirrored copies of one rule drift
// (elspeth-93f5621f18, Wave 3).
//
// Every rule here mirrors a NAMED backend authority, by path, so that a
// `git grep _producer_resolver` or `git grep connection_consumers` run from
// the Python side finds this mirror. Do not re-derive one from the wire shape:
//
//   * src/elspeth/web/composer/_producer_resolver.py:98
//     `published_success_connection(node)` decides what a node publishes:
//     on_success if set, else the node id for queue/coalesce/aggregation,
//     else nothing. `publishedSuccessConnection` below is its mirror.
//     Also :208 — `if connection_name is None or connection_name == "discard"`
//     is the statement that `discard` is a sentinel, not a connection;
//     DISCARD_CONNECTION below is that literal, named.
//   * src/elspeth/web/composer/guided/connection_consumers.py:31-40
//     the canonical consumer projection: it registers each BRANCH connection
//     as consumed by the coalesce/row_union and then `continue`s, never
//     registering `node.input` for a fan-in node — that scalar is only the
//     backend-compatible first-branch placeholder.
//   * src/elspeth/core/config.py `CoalesceSettings.branches`
//     is a "Branch identity -> INPUT connection mapping". A fan-in node's
//     branches are what it READS, never what it publishes. Reading them as
//     outbound makes a coalesce name ITSELF, because its own `input` is one
//     of its branch connections.
//   * src/elspeth/core/config.py `CoalesceSettings.policy` / `.merge`
//     the coalesce policy and merge Literals, mirrored below as tuples.
//
// The comments on each declaration are the record of the two incidents that
// produced these rules (session 3f02c8fa; elspeth-625e85c59b). They moved
// here verbatim and must stay that way.
// ============================================================================

import { sortedSourceEntries, sourceComponentId } from "@/utils/compositionState";
import type { CompositionState } from "@/types/index";

/**
 * Not a connection: the backend's sentinel for "drop this, and record the
 * drop in the audit trail" (_producer_resolver.py:208 refuses to register a
 * producer for it). Named here because two frontend sites spelled it as a
 * bare literal and nothing tied them to that rule:
 * components/workspace/PipelineSpecView.tsx:52 and
 * components/chat/guided/SchemaFormTurn.tsx:74.
 *
 * NOT the same word as `ProposalEndpointKind`'s "discard"
 * (api/guidedDecoder.ts:250, types/guided.ts:600). That is a guided-proposal
 * ENDPOINT KIND — a discriminated-union literal whose narrowing REQUIRES the
 * literal in the type position, so this constant cannot and must not replace
 * it. Two vocabularies, one word. Do not merge them.
 */
export const DISCARD_CONNECTION = "discard";

/**
 * The coalesce member sets, mirrored from `CoalesceSettings.policy` and
 * `.merge` in core/config.py and lifted out of api/guidedDecoder.ts:75-76,
 * which held the frontend's only copy privately.
 *
 * `as const` so consumers get a union type. Be precise about what that buys:
 * a display map keyed `Record<CoalescePolicy, string>` fails the BUILD when
 * a member is added to or removed from THIS TUPLE without a phrase. It does
 * NOT fail when core/config.py gains a member — `tsc` cannot see Python.
 * What catches that is the parity assertion in
 * tests/unit/web/composer/test_graph_topology_parity.py, which
 * regexes these two tuples and compares them against the Literals. Both
 * halves are needed; neither is the other.
 */
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

/**
 * Connection name -> ids of the components that PUBLISH it.
 *
 * Lifted from GraphView.tsx:1245-1260 (ProducerInfo, the map, registerProducer)
 * and :1298-1351 (the five registration blocks), which together held the only
 * statement of the rule "which fields publish a connection". A MULTIMAP, not one producer per
 * connection: ELSPETH allows many producers on one connection name under a
 * declared queue (structural fan-in, ADR-028), and overwriting would silently
 * drop every producer but the last and misrender the intentional fan-in.
 *
 * This is the registration rule ONLY. GraphView decorates each entry with its
 * own `edgeType`/`label` for ReactFlow, and applies three further DRAWING
 * rules on top that are NOT topology and deliberately stay there:
 * queue-as-sole-canonical-producer (GraphView.tsx:1262-1269), the row_union
 * authoritative-outbound semantics (:1270-1296), and the phase-1 alias dedup
 * (:1353-1417).
 *
 * The ids are COMPONENT ids, the one namespace sources and nodes share:
 * a source publishes under `sourceComponentId(name)` ("source" for the default
 * source, `source:<name>` otherwise), a node under its own id. That is the
 * vocabulary GraphView registers (:1302), the vocabulary
 * `buildProducerRegistry` is cross-checked against in graphTopology.test.ts,
 * and the vocabulary lib/validationHumaniser.ts and chat/guided/pipelineGloss.ts
 * key their phrase maps on. A bare source NAME would collide with a node whose
 * id happens to match it, and would make the cross-check pin unable to compare
 * the two indexes on the source axis at all.
 */
export function buildConnectionProducers(
  state: CompositionState,
): Map<string, string[]> {
  const producers = new Map<string, string[]>();
  const push = (connection: string, producerId: string): void => {
    const existing = producers.get(connection);
    if (existing === undefined) producers.set(connection, [producerId]);
    else if (!existing.includes(producerId)) existing.push(producerId);
  };
  // sortedSourceEntries, not Object.entries: this is what GraphView.tsx:1299
  // uses, and a faithful lift keeps the deterministic ordering. Importing it
  // — and sourceComponentId, the other half of that source registration —
  // does not break the leaf contract: utils/compositionState.ts imports only
  // types (`import type { CompositionState, SourceSpec } from "@/types/index"`
  // is its whole import block), so no React and no store enters this module's
  // dependency graph.
  for (const [sourceName, source] of sortedSourceEntries(state)) {
    if (source.on_success && source.on_success !== DISCARD_CONNECTION) {
      push(source.on_success, sourceComponentId(sourceName));
    }
  }
  for (const node of state.nodes) {
    const published = publishedSuccessConnection(node);
    if (published && published !== DISCARD_CONNECTION) push(published, node.id);
    if (node.on_error && node.on_error !== DISCARD_CONNECTION) push(node.on_error, node.id);
    if (node.routes) {
      for (const target of Object.values(node.routes)) {
        if (target !== "fork" && target !== DISCARD_CONNECTION) push(target, node.id);
      }
    }
    if (
      node.node_type === "gate"
      && node.routes
      && Object.values(node.routes).includes("fork")
      && node.fork_to
    ) {
      for (const branchConnection of node.fork_to) push(branchConnection, node.id);
    }
  }
  return producers;
}
