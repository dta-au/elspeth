"""Guarantee propagation and contract-field reads over the execution graph.

Implements the ADR-007 propagation-aware effective-guarantee walk (with the
ADR-009 §Clause 1 ``compose_propagation`` aggregation step) plus the raw
per-node contract reads it builds on. Extracted from graph.py
(elspeth-b2c6ab6db8): guarantee policy is schema/contract logic, not graph
topology. ExecutionGraph keeps thin delegators with the historical
signatures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from elspeth.contracts.enums import NodeType, RoutingMode
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.guarantee_propagation import compose_propagation
from elspeth.contracts.schema import get_raw_node_required_fields
from elspeth.core.dag.models import GraphValidationError

if TYPE_CHECKING:
    from elspeth.contracts.schema import SchemaConfig
    from elspeth.core.dag.graph import ExecutionGraph


@dataclass(frozen=True, slots=True)
class EffectiveGuaranteeVote:
    """Propagation result that preserves fields AND participation state.

    ``fields`` alone is not enough for sink validation, because an empty
    effective guarantee set can mean either:

    - no node participated in the guarantee vote (abstain), or
    - one or more nodes participated and the result collapsed to empty

    Carrying ``participated`` through the recursive walk keeps that contract
    mechanical for downstream validators.
    """

    fields: frozenset[str]
    participated: bool


def get_schema_config_from_node(graph: ExecutionGraph, node_id: str) -> SchemaConfig | None:
    """Extract SchemaConfig from node.

    Returns output_schema_config directly — all nodes with schemas have
    this populated at construction time by the builder.

    Args:
        graph: The execution graph to read from
        node_id: Node ID to get schema config from

    Returns:
        SchemaConfig if available, None if node has no schema
    """
    node_info = graph.get_node_info(node_id)
    return node_info.output_schema_config


def get_guaranteed_fields(graph: ExecutionGraph, node_id: str) -> frozenset[str]:
    """Get fields that a node guarantees in its output.

    Priority:
    1. Explicit guaranteed_fields in schema config
    2. Declared fields in flexible/fixed mode schemas
    3. Empty set for observed schemas

    Args:
        graph: The execution graph to read from
        node_id: Node ID to get guarantees from

    Returns:
        Frozenset of field names the node guarantees to output
    """
    schema_config = get_schema_config_from_node(graph, node_id)

    if schema_config is None:
        return frozenset()

    return schema_config.get_effective_guaranteed_fields()


def get_required_fields(graph: ExecutionGraph, node_id: str) -> frozenset[str]:
    """Get fields that a node EXPLICITLY requires in its input.

    This returns only explicit contract declarations, not implicit
    requirements from typed schemas. The existing type validation
    handles typed schema compatibility separately.

    Priority:
    1. Explicit required_input_fields from plugin config (TransformDataConfig)
    2. Explicit required_fields in schema config

    Note: This deliberately does NOT include implicit requirements from
    strict/free mode schemas. Those are handled by type validation, which
    correctly skips validation when either side is dynamic.

    Args:
        graph: The execution graph to read from
        node_id: Node ID to get requirements from

    Returns:
        Frozenset of field names explicitly required
    """
    node_info = graph.get_node_info(node_id)
    try:
        return get_raw_node_required_fields(
            node_info.config,
            owner=f"node:{node_id}",
            node_type=node_info.node_type.value,
        )
    except ValueError as exc:
        raise GraphValidationError(
            f"Invalid contract config: {exc}",
            component_id=str(node_id),
            component_type=node_info.node_type.value,
        ) from exc


def get_effective_guaranteed_fields(graph: ExecutionGraph, node_id: str) -> frozenset[str]:
    """Get effective output guarantees for a node (propagation-aware).

    Per ADR-007, this method is the propagation-aware implementation.
    For a TRANSFORM node whose plugin declared ``passes_through_input=True``,
    the effective guarantees are the intersection of its participating
    predecessors' guarantees unioned with the node's own declared fields.
    For all other nodes, returns the node's own declarations (same as
    ``get_guaranteed_fields``).

    Callers that want the raw per-node declarations (without propagation)
    must call ``get_guaranteed_fields`` instead.

    For coalesce nodes, builder.py pre-computes strategy-aware guarantees:
    - **union** with require_all: union of branch guarantees (all branches arrive)
    - **union** with other policies: intersection (only fields in ALL branches)
    - **nested**: the node's own guarantees (branch names, not inner fields)
    - **select**: the node's own guarantees (selected branch's schema)

    Tests that construct graphs directly must set output_schema_config
    on nodes to match what the builder would compute.

    Args:
        graph: The execution graph to read from
        node_id: Node to get effective guarantees for

    Returns:
        Frozenset of field names effectively guaranteed at this point
    """
    return walk_effective_guaranteed_fields(graph, node_id, {})


def walk_effective_guarantee_vote(
    graph: ExecutionGraph,
    node_id: str,
    cache: dict[str, EffectiveGuaranteeVote],
    field_cache: dict[str, frozenset[str]] | None = None,
) -> EffectiveGuaranteeVote:
    """Recursive implementation that preserves participation state.

    Pass-through propagation needs more than the final field set. A sink
    validator must be able to distinguish "nobody voted" from "the vote
    participated and collapsed to empty", so this helper carries both.
    """
    if node_id in cache:
        return cache[node_id]

    node_info = graph.get_node_info(node_id)
    own_participates = node_info.output_schema_config is not None and node_info.output_schema_config.participates_in_propagation
    own_fields = (
        node_info.output_schema_config.get_effective_guaranteed_fields() if node_info.output_schema_config is not None else frozenset()
    )

    # Gates are pure routing: rows pass through unchanged (Rule 0 in
    # validate_edge_schemas enforces input==output schema), so a gate's
    # effective guarantee is composed from its predecessors exactly like a
    # pass-through transform. The builder assigns each gate its upstream
    # producer's RAW output_schema_config; when that producer is itself a
    # pass-through (e.g. an llm forwarding source columns), the raw set
    # under-computes and stopping here falsely rejected runnable
    # source → pass-through → gate → branch pipelines. The composer preview
    # (_connection_propagation_vote, web/composer/state.py) already recurses
    # through gates; this pins the engine walker to the same result —
    # sibling of the coalesce fix 6b431fd03 (elspeth-0b14977817). Monotonic:
    # compose_propagation unions the gate's own (raw-inherited) fields with
    # its predecessors' effective vote, so the set can only grow.
    is_transparent_gate = node_info.node_type is NodeType.GATE
    if node_info.node_type is NodeType.QUEUE:
        # Queues are pass-through coordination (builder assigns them a bare
        # observed schema), but they are also the sanctioned fan-in point for
        # ordinary nodes (graph invariant 7), so stopping at the queue's own
        # empty declaration falsely rejected runnable source → queue →
        # consumer pipelines with "(none - dynamic schema)"
        # (elspeth-5a372d3267) — sibling of the transparent-gate walk below.
        # Fan-in aggregation is NOT compose_propagation: its abstainer-skip is
        # sound only when every predecessor delivers the same row. Queue rows
        # arrive from exactly one arm, so a field is guaranteed only if EVERY
        # arm vouches for it, and one abstaining (dynamic) arm collapses the
        # whole vote to abstention — preserving sink deferral of dynamic
        # upstreams to per-row enforcement (elspeth-3283f2eaec).
        predecessors = list(graph._graph.predecessors(node_id))
        if not predecessors:
            # The builder rejects unwired queues at construction; hand-built
            # test graphs fall back to the queue's own (empty, abstaining)
            # declaration rather than crashing.
            result = EffectiveGuaranteeVote(fields=own_fields, participated=own_participates)
        else:
            arm_votes = [walk_effective_guarantee_vote(graph, pred_id, cache, field_cache) for pred_id in predecessors]
            if all(vote.participated for vote in arm_votes):
                result = EffectiveGuaranteeVote(
                    fields=frozenset.intersection(*[vote.fields for vote in arm_votes]),
                    participated=True,
                )
            else:
                result = EffectiveGuaranteeVote(fields=frozenset(), participated=False)
    elif node_info.node_type is NodeType.ROW_UNION:
        # row_union is the correlated UNION ALL barrier: every released row
        # is exactly one branch's payload, unchanged (elspeth-a5b86149d4).
        # The builder stamps it observed ("no schema synthesis"), so stopping
        # at the node's own declaration falsely rejected runnable
        # fork → branches → row_union → consumer pipelines with
        # "(none - dynamic schema)" (elspeth-41bcaa882e) — sibling of the
        # queue fan-in walk above, governed by the same rule: a field is
        # guaranteed on the released stream only if EVERY branch vouches for
        # it, and one abstaining (dynamic) branch collapses the whole vote to
        # abstention. compose_propagation's abstainer-skip stays unsound here
        # for the same reason it is for queues: rows arrive from exactly one
        # branch, so promoting a single branch's guarantee would over-claim.
        #
        # A DIVERT in-edge forces abstention rather than being skipped: a
        # divert payload is an error envelope, not the producer's declared
        # row, so a stream containing one cannot vouch for any field. The
        # builder never wires DIVERT into a row_union today (error routing is
        # terminal — see _live_predecessors in schema_validation.py); this
        # guards the public add_edge surface.
        in_edges = list(graph._graph.in_edges(node_id, data=True))
        if not in_edges:
            # The builder rejects unwired row_unions at construction;
            # hand-built test graphs fall back to the node's own (empty,
            # abstaining) declaration rather than crashing.
            result = EffectiveGuaranteeVote(fields=own_fields, participated=own_participates)
        elif any(edge_data["mode"] == RoutingMode.DIVERT for _from_id, _to_id, edge_data in in_edges):
            result = EffectiveGuaranteeVote(fields=frozenset(), participated=False)
        else:
            branch_votes = [
                walk_effective_guarantee_vote(graph, pred_id, cache, field_cache) for pred_id in graph._graph.predecessors(node_id)
            ]
            if all(vote.participated for vote in branch_votes):
                result = EffectiveGuaranteeVote(
                    fields=frozenset.intersection(*[vote.fields for vote in branch_votes]),
                    participated=True,
                )
            else:
                result = EffectiveGuaranteeVote(fields=frozenset(), participated=False)
    elif node_info.passes_through_input or is_transparent_gate:
        predecessors = list(graph._graph.predecessors(node_id))
        if not predecessors and is_transparent_gate:
            # Hand-built test graphs may install a gate without wiring its
            # upstream edge; fall back to the gate's own (inherited)
            # declaration rather than crashing — the builder always wires
            # gates, so real graphs never take this branch.
            result = EffectiveGuaranteeVote(fields=own_fields, participated=own_participates)
        elif not predecessors:
            raise FrameworkBugError(
                f"Pass-through transform {node_id!r} has no predecessors. Builder must wire transforms with at least one upstream edge."
            )
        else:
            predecessor_votes = [walk_effective_guarantee_vote(graph, pred_id, cache, field_cache) for pred_id in predecessors]
            result_fields = compose_propagation(
                own_fields,
                [vote.fields if vote.participated else None for vote in predecessor_votes],
            )
            result = EffectiveGuaranteeVote(
                fields=result_fields,
                participated=own_participates or any(vote.participated for vote in predecessor_votes),
            )
    else:
        result = EffectiveGuaranteeVote(
            fields=own_fields,
            participated=own_participates,
        )

    cache[node_id] = result
    if field_cache is not None:
        field_cache[node_id] = result.fields
    return result


def walk_effective_guaranteed_fields(
    graph: ExecutionGraph,
    node_id: str,
    cache: dict[str, frozenset[str]],
) -> frozenset[str]:
    """Recursive implementation of get_effective_guaranteed_fields.

    Explicit (non-optional) cache parameter — internal callers with
    bulk-validation scope (e.g., ``validate_sink_required_fields``)
    pass a shared cache across their loop to avoid per-node re-allocation
    and re-walking. Public callers go through ``get_effective_guaranteed_fields``
    which allocates a fresh per-call cache.

    For pass-through transforms, delegates the aggregation step to
    ``compose_propagation`` (ADR-009 §Clause 1). Predecessor
    participation is checked via ``SchemaConfig.participates_in_propagation``
    — the canonical predicate that both this walker and the composer's
    preview walker consult.

    Raises ``FrameworkBugError`` if a pass-through transform has no
    predecessors — per the NodeInfo guard, pass-through nodes are
    transforms, and transforms in a built DAG always have at least one
    upstream edge.
    """
    if node_id in cache:
        return cache[node_id]

    result = walk_effective_guarantee_vote(graph, node_id, {}, cache)
    cache[node_id] = result.fields
    return result.fields


def walk_definite_emitted_fields(
    graph: ExecutionGraph,
    node_id: str,
    cache: dict[str, frozenset[str]],
) -> frozenset[str]:
    """Return fields that DEFINITELY arrive on ``node_id``'s output rows.

    The extras-direction counterpart to ``walk_effective_guaranteed_fields``,
    and deliberately a separate walk rather than a widening of it — the two
    ask questions with opposite safety polarities, the distinction
    ``_connection_definite_emits`` (``web/composer/state.py``) already draws on
    the composer side. The presence direction asks "is every required field
    guaranteed?", and its consumers read a wider answer as PERMISSION: sink
    required-fields clearance, ``check_compatibility``'s missing-arm
    forgiveness (elspeth-7d68b04878), ``validate_forgiven_field_ancestor_types``.
    Adding fields there would loosen a gate. The extras direction asks "does
    any field arrive that a locked consumer forbids?", where a wider answer
    only ever REJECTS more.

    Result = the node's own effective guarantees UNION, for a node declaring
    ``forwards_input_fields``, its predecessors' definite emits minus its
    ``removed_input_fields``. Strictly ADDITIVE over the presence walk's
    answer at the same node, so no graph rejected today stops being rejected
    and no error message relocates — this can only close the gap
    elspeth-15c72686f2 reports, never open a new one.

    Fan-in UNIONS rather than intersects, for the reason
    ``_connection_definite_emits`` documents: a field carried by ONE arm
    definitely arrives on that arm's rows, and a locked consumer forbidding it
    is a definite runtime rejection. That is the opposite of
    ``merge_guaranteed_fields``' intersection, which is correct for the
    presence question and wrong for this one.

    DIVERT in-edges are excluded: a divert payload is an error envelope, not
    the producer's declared row, so it vouches for nothing. This mirrors the
    exclusion ``validate_typed_producer_guaranteed_extras`` applies at its loop
    head rather than re-deriving it.

    The result is a LOWER BOUND — a node that forwards but cannot name its
    removals declares nothing and contributes only its own guarantees. That
    makes it sound to raise an error ON this set but never sound to clear a
    graph WITH it, the same asymmetry the composer's walk carries.
    """
    if node_id in cache:
        return cache[node_id]

    node_info = graph.get_node_info(node_id)
    own_fields = walk_effective_guaranteed_fields(graph, node_id, {})

    if not node_info.forwards_input_fields:
        cache[node_id] = own_fields
        return own_fields

    # Seed the cache before recursing so a hand-built cyclic graph terminates
    # with the node's own (forward-free) contribution rather than recursing
    # forever. Built graphs are DAGs, so this is defensive only — the same
    # posture resolve_guaranteed_field_type takes on its visited guard.
    cache[node_id] = own_fields

    forwarded: frozenset[str] = frozenset()
    for from_id, _to_id, edge_data in graph._graph.in_edges(node_id, data=True):
        if edge_data["mode"] == RoutingMode.DIVERT:
            continue
        forwarded |= walk_definite_emitted_fields(graph, from_id, cache)

    result = own_fields | (forwarded - node_info.removed_input_fields)
    cache[node_id] = result
    return result


def get_definite_emitted_fields(graph: ExecutionGraph, node_id: str) -> frozenset[str]:
    """Public entry point for ``walk_definite_emitted_fields`` with a fresh cache."""
    return walk_definite_emitted_fields(graph, node_id, {})


@dataclass(frozen=True, slots=True)
class ResolvedGuaranteeType:
    """Nearest ancestor declaration for a guarantee-carried field.

    ``field_type`` is the SchemaConfig vocabulary (never ``"any"`` — an
    ``any`` declaration abstains to ``None`` at the resolution site, because
    it states no type to check). BASE TYPE ONLY, deliberately: the two
    pydantic materializations disagree about nullability
    (``build_coalesce_schema`` folds ``nullable or not required`` into
    ``| None``; the plugin factory's ``_get_python_type`` reads ``required``
    alone), so any nullability bit carried here would make the walk stricter
    than one declared arm or the other and break monotonicity — declaring
    the identical field must never flip a verdict (elspeth-85e8afa2f5 panel
    review; the composer's ``_edge_field_type_conflict`` documents abandoning
    model reconstruction for the same reason). None-at-runtime deaths stay
    with the per-row preflight. ``declared_by`` names the declaring node(s) —
    more than one when several branches independently declare the same type —
    so a rejection can point at the declaration it is enforcing.
    """

    field_type: str
    declared_by: frozenset[str]


def resolve_guaranteed_field_type(
    graph: ExecutionGraph,
    node_id: str,
    field_name: str,
    *,
    _visited: frozenset[str] = frozenset(),
    cache: dict[tuple[str, str], ResolvedGuaranteeType | None] | None = None,
) -> ResolvedGuaranteeType | None:
    """Resolve the nearest ancestor DECLARED type of a guarantee-carried field.

    The guarantee walk (``walk_effective_guarantee_vote``) proves presence and
    deliberately strips types; this walk recovers the type where an ancestor
    stated one, so build-time validation of a forgiven field
    (elspeth-85e8afa2f5) can check what is knowable and keep forgiving what is
    not. ``None`` is ABSTENTION — the type is not provable — and every
    uncertain arm below collapses to it rather than guessing.

    Nearest declaration wins: a node whose ``output_schema_config`` declares
    the field settles it (the node may have rewritten or coerced the field —
    a farther declaration describes a value that no longer flows), and the
    walk recurses only through nodes that did NOT declare it, following the
    same topology the guarantee walk traverses (transparent gates,
    pass-through transforms, queue/row_union fan-in, coalesce branches).

    Soundness for the REJECTING caller is unanimity: every path a runtime
    value can take must resolve to the same base ``field_type``.
    The contributor sets below may over-include paths that cannot actually
    supply the value (e.g. every coalesce branch, regardless of collision
    policy); that is safe by construction — the true path is always among
    those considered, so an extra path can only force abstention or agree,
    never manufacture a wrong unanimous type. Any DIVERT in-edge on a
    recursed node abstains outright (the row_union guarantee-walk posture):
    a divert payload is an error envelope, so a stream carrying one has no
    provable field types — unreachable on builder-produced graphs, where
    error routing is terminal (see ``_live_predecessors``), and defensive on
    the public ``add_edge`` surface.

    ``cache`` memoizes on ``(node_id, field_name)`` — same discipline as
    ``walk_effective_guaranteed_fields``'s cache parameter, and load-bearing
    for the same reason: without it, nested fan-in re-resolves shared
    ancestors once per path, which is exponential in fan-in depth (panel
    F5). Sound to share across a validation pass because built graphs are
    DAGs, so a resolution is path-independent; on a hand-built cyclic graph
    the visited-guard's ``None`` is conservative (abstention) either way.
    """
    if node_id in _visited:
        # A cycle-break is a fact about this PATH, not about the node —
        # never cached.
        return None
    if cache is not None and (node_id, field_name) in cache:
        return cache[(node_id, field_name)]
    result = _resolve_guaranteed_field_type_uncached(graph, node_id, field_name, _visited=_visited, cache=cache)
    if cache is not None:
        cache[(node_id, field_name)] = result
    return result


def _resolve_guaranteed_field_type_uncached(
    graph: ExecutionGraph,
    node_id: str,
    field_name: str,
    *,
    _visited: frozenset[str],
    cache: dict[tuple[str, str], ResolvedGuaranteeType | None] | None,
) -> ResolvedGuaranteeType | None:
    """Worker for ``resolve_guaranteed_field_type`` — see its docstring."""
    node_info = graph.get_node_info(node_id)
    config = node_info.output_schema_config
    # A gate's own config is its upstream producer's RAW config copied in by
    # the builder (``_assign_schema(gate_id, _best_schema_config(producer_id))``),
    # not a declaration the gate made. Recursing past it reaches the identical
    # declaration at its true owner, so ``declared_by`` attributes the type to
    # a node the author actually wrote a schema on.
    if config is not None and config.fields is not None and node_info.node_type is not NodeType.GATE:
        for field_def in config.fields:
            if field_def.name == field_name:
                if field_def.field_type == "any":
                    return None
                return ResolvedGuaranteeType(
                    field_type=field_def.field_type,
                    declared_by=frozenset({node_id}),
                )

    recurses = (
        node_info.node_type in (NodeType.GATE, NodeType.QUEUE, NodeType.ROW_UNION, NodeType.COALESCE) or node_info.passes_through_input
    )
    if not recurses:
        return None

    # A pass-through TRANSFORM runs plugin code that may rewrite a field's
    # type in place — recursion past it is sound only under the declaration
    # discipline: a transform that rewrites a field declares it in its output
    # config (``value_transform`` declares operation targets as ``any``;
    # ``type_coerce`` declares conversion targets as their target type), so
    # the own-declaration check above settles rewritten fields before this
    # point. A pass-through with NO field declarations (observed mode, or no
    # config at all) has opted out of that discipline and states nothing
    # about its output — treating its silence as type-preservation readmitted
    # elspeth-85e8afa2f5 through an observed-mode ``type_coerce`` (panel
    # review), so it abstains. ``not config.fields`` rather than
    # ``fields is None``: a declaring config with an EMPTY fields tuple has
    # equally declared nothing — ``blob_csv_expand`` with ``columns: null``
    # and ``include_row_index: false`` builds exactly that shape while
    # merging data-derived CSV headers onto the row (panel sweep), so the
    # empty tuple must abstain too. Gates, queues, row_unions, and coalesces
    # stay recursable: they are pure routing/merge and run no row-rewriting
    # code.
    if node_info.node_type is NodeType.TRANSFORM and node_info.passes_through_input and (config is None or not config.fields):
        return None

    in_edges = list(graph._graph.in_edges(node_id, data=True))
    if any(edge_data["mode"] == RoutingMode.DIVERT for _from_id, _to_id, edge_data in in_edges):
        return None
    contributors = sorted({from_id for from_id, _to_id, _edge_data in in_edges})
    if not contributors:
        return None

    visited = _visited | {node_id}
    resolutions = [
        resolve_guaranteed_field_type(graph, contributor, field_name, _visited=visited, cache=cache) for contributor in contributors
    ]
    if any(resolution is None for resolution in resolutions):
        return None
    first, *rest = [r for r in resolutions if r is not None]
    if any(other.field_type != first.field_type for other in rest):
        return None
    return ResolvedGuaranteeType(
        field_type=first.field_type,
        declared_by=frozenset().union(*(resolution.declared_by for resolution in [first, *rest])),
    )
