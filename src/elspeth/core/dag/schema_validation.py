"""Build-time schema/contract validation for the execution graph.

PHASE 2 (cross-plugin) validation: per-edge contract + type compatibility,
coalesce branch compatibility, sink required-field satisfaction, and the
effective-producer-schema resolution they share. Extracted from graph.py
(elspeth-b2c6ab6db8): validation policy is contract logic, not graph
topology. ExecutionGraph keeps thin delegators with the historical
signatures.
"""

from __future__ import annotations

from itertools import combinations
from typing import TYPE_CHECKING

from elspeth.contracts import PluginSchema, RoutingMode, check_compatibility
from elspeth.contracts.enums import NodeType
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.types import NodeID
from elspeth.core.dag.guarantees import (
    EffectiveGuaranteeVote,
    get_effective_guaranteed_fields,
    get_required_fields,
    walk_effective_guarantee_vote,
)
from elspeth.core.dag.models import EdgeContractError, GraphValidationError
from elspeth.core.dag.schema_factory import build_coalesce_schema

if TYPE_CHECKING:
    from elspeth.core.dag.graph import ExecutionGraph


def validate_edge_compatibility(graph: ExecutionGraph) -> None:
    """Validate schema compatibility for all edges in the graph.

    Called AFTER graph construction is complete. Validates that each edge
    connects compatible schemas.

    Raises:
        ValueError: If any edge has incompatible schemas

    Note:
        This is PHASE 2 validation (cross-plugin compatibility). Plugin
        SELF-validation happens in PHASE 1 during plugin construction.
    """
    # Schema resolution cache shared across all edge validations.
    # Eliminates redundant recursion through long gate chains (O(N^2) → O(N)).
    schema_cache: dict[str, type[PluginSchema] | None] = {}

    # Validate each edge (skip divert edges — quarantine/error data doesn't
    # conform to producer schemas because it failed validation or errored)
    for from_id, to_id, edge_data in graph._graph.edges(data=True):
        if edge_data["mode"] == RoutingMode.DIVERT:
            continue
        validate_single_edge(graph, from_id, to_id, _schema_cache=schema_cache)

    # Validate all coalesce nodes (must have compatible schemas from all branches)
    coalesce_nodes = [node_id for node_id, data in graph._graph.nodes(data=True) if data["info"].node_type == NodeType.COALESCE]
    for coalesce_id in coalesce_nodes:
        validate_coalesce_compatibility(graph, coalesce_id, _schema_cache=schema_cache)

    # row_union releases every branch payload unchanged into one long-format
    # stream. Any branch contracts Composer/runtime can prove fixed must
    # therefore be mutually compatible; observed/unknown branches abstain.
    row_union_nodes = [node_id for node_id, data in graph._graph.nodes(data=True) if data["info"].node_type == NodeType.ROW_UNION]
    for row_union_id in row_union_nodes:
        validate_row_union_compatibility(graph, row_union_id, _schema_cache=schema_cache)

    # Validate sink required-field satisfaction against direct predecessors.
    # Catches mismatches between sink.declared_required_fields and upstream
    # output optionality at build time, rather than at runtime with a
    # generic PluginContractViolation. Walking sinks backward (rather than
    # coalesces forward) handles COALESCE → TRANSFORM → SINK topologies
    # and any future multi-hop shape.
    validate_sink_required_fields(graph)

    # Validate that no transform declares an output field its input already
    # carries. Output-side twin of the sink check above; runs last so a graph
    # tripping both keeps reporting the pre-existing sink error
    # (elspeth-cfcd333f83).
    validate_transform_output_field_collisions(graph)

    # Validate that every field a transform declares as REQUIRED input is
    # guaranteed by a participating upstream. Input-side twin of the two checks
    # above; runs last for the same reason they each cite — a graph tripping
    # several checks keeps reporting the pre-existing error
    # (elspeth-ada5a60249).
    validate_transform_declared_input_fields(graph)


def validate_single_edge(
    graph: ExecutionGraph,
    from_node_id: str,
    to_node_id: str,
    *,
    _schema_cache: dict[str, type[PluginSchema] | None] | None = None,
) -> None:
    """Validate schema compatibility for a single edge.

    Validation is performed in two phases:
    1. CONTRACT VALIDATION: Check required/guaranteed field names
    2. TYPE VALIDATION: Check field type compatibility

    Contract validation catches missing fields even for dynamic schemas,
    which is critical for template-based transforms (e.g., LLM) that
    reference specific row fields.

    Args:
        graph: The execution graph to validate against
        from_node_id: Source node ID
        to_node_id: Destination node ID
        _schema_cache: Shared memoization dict for schema resolution.

    Raises:
        GraphValidationError: If schemas are incompatible or contracts violated
    """
    to_info = graph.get_node_info(to_node_id)

    # Skip edge validation for correlated barriers: their dedicated validators
    # compare all incoming branches together. row_union branch payloads are
    # released untouched, so consumer-style single-edge derivation does not
    # apply even though its known fixed branches must agree with each other.
    if to_info.node_type in (NodeType.COALESCE, NodeType.ROW_UNION):
        return

    # Rule 0: Gates must preserve schema (input == output)
    if (
        to_info.node_type == NodeType.GATE
        and to_info.input_schema is not None
        and to_info.output_schema is not None
        and to_info.input_schema != to_info.output_schema
    ):
        raise GraphValidationError(
            f"Gate '{to_node_id}' must preserve schema: "
            f"input_schema={to_info.input_schema.__name__}, "
            f"output_schema={to_info.output_schema.__name__}",
            component_id=str(to_node_id),
            component_type="gate",
        )

    # ===== PHASE 1: CONTRACT VALIDATION (field name requirements) =====
    # This catches missing fields even for dynamic schemas.
    #
    # SINK consumers are deliberately excluded: their required fields are
    # validated by validate_sink_required_fields, whose EffectiveGuaranteeVote
    # walk distinguishes an ABSTAINING upstream (no fields, no participation —
    # defer to SinkExecutor's per-row declared_required_fields enforcement)
    # from a participating upstream that genuinely misses fields (reject at
    # build). Running the name check here too would pre-empt that abstention
    # with a hard failure and make the dedicated sink check unreachable for
    # its target scenario (elspeth-3283f2eaec).
    consumer_required = frozenset() if to_info.node_type == NodeType.SINK else get_required_fields(graph, to_node_id)

    if consumer_required:
        # Get effective guaranteed fields (walks through pass-through nodes)
        producer_guaranteed = get_effective_guaranteed_fields(graph, from_node_id)

        missing = consumer_required - producer_guaranteed
        if missing:
            # Build actionable error message
            from_info = graph.get_node_info(from_node_id)
            raise GraphValidationError(
                f"Schema contract violation: edge '{from_node_id}' → '{to_node_id}'\n"
                f"  Consumer ({to_info.plugin_name}) requires fields: {sorted(consumer_required)}\n"
                f"  Producer ({from_info.plugin_name}) guarantees: "
                f"{sorted(producer_guaranteed) if producer_guaranteed else '(none - dynamic schema)'}\n"
                f"  Missing fields: {sorted(missing)}\n"
                f"\n"
                f"Fix: Either:\n"
                f"  1. Add missing fields to producer's schema or guaranteed_fields, or\n"
                f"  2. Remove from consumer's required_input_fields if truly optional",
                component_id=str(to_node_id),
                component_type=to_info.node_type.value,
            )

    # ===== PHASE 2: TYPE VALIDATION (schema compatibility) =====
    # Get EFFECTIVE producer schema (walks through gates if needed)
    producer_schema = get_effective_producer_schema(graph, from_node_id, _cache=_schema_cache)
    consumer_schema = to_info.input_schema

    # Rule 1: Dynamic schemas (None) bypass type validation
    if producer_schema is None or consumer_schema is None:
        return  # Observed schema - compatible with anything

    # Handle observed schemas (no explicit fields + extra='allow')
    # These are created by _create_dynamic_schema and accept anything
    # NOTE: We control all schemas via PluginSchema base class which sets model_config["extra"].
    # Direct access is correct per Tier 1 trust model - missing key would be our bug.
    producer_is_observed = len(producer_schema.model_fields) == 0 and producer_schema.model_config["extra"] == "allow"
    consumer_is_observed = len(consumer_schema.model_fields) == 0 and consumer_schema.model_config["extra"] == "allow"
    if producer_is_observed or consumer_is_observed:
        return  # Observed schemas bypass static type validation

    # Rule 2: Full compatibility check (missing fields, type mismatches, extra fields)
    result = check_compatibility(producer_schema, consumer_schema)
    if not result.compatible:
        # Raise the structured subclass so downstream layers (composer
        # runtime preflight error formatter at
        # web/execution/validation.py) can build LLM-actionable
        # suggestions without re-parsing the prose form. The prose
        # message remains backwards-compatible for legacy str(exc)
        # consumers.
        raise EdgeContractError(
            f"Edge from '{from_node_id}' to '{to_node_id}' invalid: "
            f"producer schema '{producer_schema.__name__}' incompatible with "
            f"consumer schema '{consumer_schema.__name__}': {result.error_message}",
            from_node_id=str(from_node_id),
            to_node_id=str(to_node_id),
            producer_schema_name=producer_schema.__name__,
            consumer_schema_name=consumer_schema.__name__,
            compatibility_result=result,
            component_type=to_info.node_type.value,
        )


def get_effective_producer_schema(
    graph: ExecutionGraph,
    node_id: str,
    _cache: dict[str, type[PluginSchema] | None] | None = None,
) -> type[PluginSchema] | None:
    """Get effective output schema, walking through pass-through nodes (gates, coalesce).

    Gates and coalesce nodes don't transform data - they inherit schema from their
    upstream producers. This method walks backwards through the graph to find the
    nearest schema-carrying producer.

    Results are memoized in ``_cache`` to avoid redundant recursion through
    long gate chains (O(N) depth per gate → O(1) with cache).

    Args:
        graph: The execution graph to resolve against
        node_id: Node to get effective schema for
        _cache: Internal memoization dict, created on first call.

    Returns:
        Output schema type, or None if dynamic

    Raises:
        GraphValidationError: If pass-through node has no incoming edges (graph construction bug)
    """
    if _cache is None:
        _cache = {}

    if node_id in _cache:
        return _cache[node_id]

    node_info = graph.get_node_info(node_id)

    # If node has output_schema, return it directly
    if node_info.output_schema is not None:
        _cache[node_id] = node_info.output_schema
        return node_info.output_schema

    # Coalesce nodes are NOT pass-throughs — they transform data via merge
    # strategy (nested wraps in {branch: data}, union merges fields, select
    # picks a branch).  Strategy-aware handling:
    #   - select: passes through one branch unchanged → trace that branch's schema
    #   - union:  builder synthesizes typed field schema → build PluginSchema class
    #   - nested: all fields are "any" type → return None (nothing useful to validate)
    if node_info.node_type == NodeType.COALESCE:
        merge_strategy = node_info.config["merge"]
        if merge_strategy == "select":
            # Select merge passes through the selected branch's data unchanged.
            # Trace back to that branch's producer schema for type validation.
            if "select_branch" not in node_info.config:
                raise GraphValidationError(
                    f"Coalesce node '{node_id}' has merge strategy 'select' but "
                    "no 'select_branch' in config. This indicates a graph construction bug.",
                    component_id=str(node_id),
                    component_type="coalesce",
                )
            select_branch = node_info.config["select_branch"]
            # Identity branch: COPY edge from gate to coalesce with label == select_branch
            for from_id, _, _key, edge_data in graph._graph.in_edges(node_id, keys=True, data=True):
                if edge_data["mode"] == RoutingMode.COPY and edge_data["label"] == select_branch:
                    result = get_effective_producer_schema(graph, from_id, _cache)
                    _cache[node_id] = result
                    return result
            # Transform branch: last transform's edge has label "continue", not
            # the branch name. Trace backward to find the last transform node.
            _first, last = graph._trace_branch_endpoints(NodeID(node_id), select_branch)
            result = get_effective_producer_schema(graph, last, _cache)
            _cache[node_id] = result
            return result
        if merge_strategy == "union" and node_info.output_schema_config is not None and not node_info.output_schema_config.is_observed:
            # Union merge: the builder sets output_schema_config with concrete
            # field types.  Build a PluginSchema class from it so PHASE 2 edge
            # validation can type-check downstream.
            result = build_coalesce_schema(node_info.output_schema_config, coalesce_id=str(node_id))
            _cache[node_id] = result
            return result
        # nested merge, observed union, or no synthesized schema: return None (dynamic)
        _cache[node_id] = None
        return None

    # Gates are true pass-throughs — inherit schema from upstream producers
    if node_info.node_type == NodeType.GATE:
        incoming = list(graph._graph.in_edges(node_id, keys=True, data=True))

        if not incoming:
            # Pass-through node with no inputs is a graph construction bug - CRASH
            raise GraphValidationError(
                f"{node_info.node_type.capitalize()} node '{node_id}' has no incoming edges - this indicates a bug in graph construction",
                component_id=str(node_id),
                component_type=node_info.node_type.value,
            )

        # Gather all input schemas for validation
        all_schemas: list[tuple[str, type[PluginSchema] | None]] = []
        for from_id, _, _key, _ in incoming:
            schema = get_effective_producer_schema(graph, from_id, _cache)
            all_schemas.append((from_id, schema))

        # For multi-input nodes, check for mixed observed/explicit schemas first
        # Mixed observed/explicit branches create semantic mismatches that cause runtime failures
        if len(all_schemas) > 1:
            observed_branches = [(nid, s) for nid, s in all_schemas if is_observed_schema(s)]
            explicit_branches = [(nid, s) for nid, s in all_schemas if not is_observed_schema(s)]

            if observed_branches and explicit_branches:
                # Mixed observed/explicit - reject with clear error
                observed_names = [nid for nid, _ in observed_branches]
                # Schema is guaranteed non-None here (explicit_branches filtered out observed/None)
                explicit_names = [f"{nid} ({s.__name__})" for nid, s in explicit_branches if s is not None]
                raise GraphValidationError(
                    f"{node_info.node_type.capitalize()} '{node_id}' has mixed observed/explicit schemas - "
                    f"this is not allowed because observed branches may produce rows missing fields "
                    f"expected by downstream consumers. "
                    f"Observed branches: {observed_names}, explicit branches: {explicit_names}. "
                    f"Fix: ensure all branches produce explicit schemas with compatible fields, "
                    f"or all branches produce observed schemas.",
                    component_id=str(node_id),
                    component_type=node_info.node_type.value,
                )

            # All explicit - verify structural compatibility
            if len(explicit_branches) > 1:
                _first_id, first_schema = explicit_branches[0]
                for _other_id, other_schema in explicit_branches[1:]:
                    compatible, error_msg = schemas_structurally_compatible(first_schema, other_schema)
                    if not compatible:
                        # Schemas are guaranteed non-None here (explicit_branches filtered out observed/None)
                        first_name = first_schema.__name__ if first_schema is not None else "observed"
                        other_name = other_schema.__name__ if other_schema is not None else "observed"
                        raise GraphValidationError(
                            f"{node_info.node_type.capitalize()} '{node_id}' receives incompatible schemas from "
                            f"multiple inputs - this is a graph construction bug. "
                            f"First input: {first_name}, other input: {other_name}. {error_msg}",
                            component_id=str(node_id),
                            component_type=node_info.node_type.value,
                        )

        # Return first schema (all are now either all-observed or all-explicit-compatible)
        result = all_schemas[0][1]
        _cache[node_id] = result
        return result

    # Not a pass-through node and no schema - return None (observed)
    _cache[node_id] = None
    return None


def is_observed_schema(schema: type[PluginSchema] | None) -> bool:
    """Check if a schema is observed (accepts any fields, types inferred from data).

    A schema is observed if:
    - It is None (unspecified output_schema)
    - It has no fields and allows extra fields (structural observed)

    Args:
        schema: Schema class or None

    Returns:
        True if schema is observed, False if explicit (fixed/flexible)
    """
    if schema is None:
        return True

    # Structural observed: no fields + extra="allow"
    # NOTE: We control all schemas via PluginSchema base class which sets model_config["extra"].
    # Direct access is correct per Tier 1 trust model - missing key would be our bug.
    return len(schema.model_fields) == 0 and schema.model_config["extra"] == "allow"


def schemas_structurally_compatible(schema_a: type[PluginSchema] | None, schema_b: type[PluginSchema] | None) -> tuple[bool, str]:
    """Check if two schemas are structurally compatible (not by class identity).

    Uses check_compatibility() for structural comparison. Handles observed schemas
    which are compatible with anything.

    Args:
        schema_a: First schema (or None for observed)
        schema_b: Second schema (or None for observed)

    Returns:
        Tuple of (is_compatible, error_message). If compatible, error_message is empty.
    """
    # Both observed - compatible
    if is_observed_schema(schema_a) and is_observed_schema(schema_b):
        return True, ""

    # One observed, one explicit - for general compatibility, allow this
    # (Pass-through nodes use stricter checking via _check_passthrough_schema_homogeneity)
    if is_observed_schema(schema_a) or is_observed_schema(schema_b):
        return True, ""

    # Both explicit schemas - same class is trivially compatible
    if schema_a is schema_b:
        return True, ""

    # At this point both schemas are explicit (not None, not dynamic)
    # Type narrowing for mypy: we've already returned if either is dynamic/None
    assert schema_a is not None and schema_b is not None

    # Both explicit schemas - use bidirectional structural comparison
    # For coalesce/pass-through nodes, schemas must be mutually compatible
    result_ab = check_compatibility(schema_a, schema_b)
    result_ba = check_compatibility(schema_b, schema_a)

    if result_ab.compatible and result_ba.compatible:
        return True, ""

    # Build error message showing what's incompatible
    errors = []
    if not result_ab.compatible:
        errors.append(f"{schema_a.__name__} -> {schema_b.__name__}: {result_ab.error_message}")
    if not result_ba.compatible:
        errors.append(f"{schema_b.__name__} -> {schema_a.__name__}: {result_ba.error_message}")
    return False, "; ".join(errors)


def validate_coalesce_compatibility(
    graph: ExecutionGraph,
    coalesce_id: str,
    *,
    _schema_cache: dict[str, type[PluginSchema] | None] | None = None,
) -> None:
    """Validate all inputs to coalesce node have compatible schemas.

    Strategy-aware: only ``union`` requires cross-branch schema compatibility.
    ``nested`` and ``select`` strategies have no cross-branch constraint because
    branches are keyed separately (nested) or only one branch is used (select).

    Args:
        graph: The execution graph to validate against
        coalesce_id: Coalesce node ID
        _schema_cache: Shared memoization dict for schema resolution.

    Raises:
        GraphValidationError: If branches have incompatible schemas
    """
    incoming = list(graph._graph.in_edges(coalesce_id, keys=True, data=True))

    if not incoming:
        raise GraphValidationError(
            f"Coalesce '{coalesce_id}' has no incoming edges — this is a graph construction bug",
            component_id=str(coalesce_id),
            component_type="coalesce",
        )
    if len(incoming) < 2:
        return  # Degenerate case (1 branch) - always compatible

    # Determine merge strategy from node config.
    # Config is populated by the builder — direct access is correct (Tier 1).
    node_info = graph.get_node_info(coalesce_id)
    merge_strategy = node_info.config["merge"]

    # nested/select strategies have no cross-branch schema constraint
    if merge_strategy in ("nested", "select"):
        return

    # union strategy: gather all branch schemas and validate
    all_schemas: list[tuple[str, type[PluginSchema] | None]] = []
    for from_id, _, _key, _ in incoming:
        schema = get_effective_producer_schema(graph, from_id, _cache=_schema_cache)
        all_schemas.append((from_id, schema))

    # Reject mixed observed/explicit schemas
    observed_branches = [(nid, s) for nid, s in all_schemas if is_observed_schema(s)]
    explicit_branches = [(nid, s) for nid, s in all_schemas if not is_observed_schema(s)]

    if observed_branches and explicit_branches:
        observed_names = [nid for nid, _ in observed_branches]
        explicit_names = [f"{nid} ({s.__name__})" for nid, s in explicit_branches if s is not None]
        raise GraphValidationError(
            f"Coalesce '{coalesce_id}' has mixed observed/explicit schemas - "
            f"this is not allowed because observed branches may produce rows missing fields "
            f"expected by downstream consumers. "
            f"Observed branches: {observed_names}, explicit branches: {explicit_names}. "
            f"Fix: ensure all branches produce explicit schemas with compatible fields, "
            f"or all branches produce observed schemas.",
            component_id=str(coalesce_id),
            component_type="coalesce",
        )

    # All explicit: verify structural compatibility across branches
    if len(explicit_branches) > 1:
        _first_id, first_schema = explicit_branches[0]
        for other_id, other_schema in explicit_branches[1:]:
            compatible, error_msg = schemas_structurally_compatible(first_schema, other_schema)
            if not compatible:
                first_name = first_schema.__name__ if first_schema else "observed"
                other_name = other_schema.__name__ if other_schema else "observed"
                raise GraphValidationError(
                    f"Coalesce '{coalesce_id}' receives incompatible schemas from "
                    f"multiple branches: first branch has {first_name}, "
                    f"branch from '{other_id}' has {other_name}. {error_msg}",
                    component_id=str(coalesce_id),
                    component_type="coalesce",
                )


def validate_row_union_compatibility(
    graph: ExecutionGraph,
    row_union_id: str,
    *,
    _schema_cache: dict[str, type[PluginSchema] | None] | None = None,
) -> None:
    """Validate all provably fixed row_union branch schemas are compatible.

    row_union is a correlated ``UNION ALL``: it preserves every branch row
    unchanged and publishes them as one long-format stream. Two known fixed
    schemas that are not mutually compatible therefore make the stream's
    declared contract false. Observed/unknown schemas continue to abstain
    because their runtime rows may still be compatible.
    """
    incoming = list(graph._graph.in_edges(row_union_id, keys=True, data=True))
    explicit_branches: list[tuple[str, type[PluginSchema], SchemaConfig | None]] = []
    for from_id, _, _key, edge_data in incoming:
        if edge_data["mode"] == RoutingMode.DIVERT:
            # Failure/quarantine payloads do not conform to the producer's
            # success schema, so that declaration proves nothing here.
            continue
        schema = get_effective_producer_schema(graph, from_id, _cache=_schema_cache)
        if is_observed_schema(schema):
            continue
        assert schema is not None
        explicit_branches.append(
            (
                from_id,
                schema,
                get_effective_producer_schema_config(graph, from_id),
            )
        )

    if len(explicit_branches) < 2:
        return

    for (first_id, first_schema, first_config), (other_id, other_schema, other_config) in combinations(explicit_branches, 2):
        if first_config is not None and other_config is not None:
            compatible, conflicting_fields, error_msg = row_union_schema_configs_compatible(first_config, other_config)
        else:
            # An explicit model without its originating SchemaConfig cannot be
            # proven flexible. Preserve the historical fail-closed full-shape
            # comparison for that incomplete Tier-1 graph metadata.
            compatible, error_msg = schemas_structurally_compatible(first_schema, other_schema)
            conflicting_fields = ()
        if compatible:
            continue
        conflict_suffix = f" Conflicting fields: {list(conflicting_fields)}." if conflicting_fields else ""
        raise GraphValidationError(
            f"row_union '{row_union_id}' receives incompatible schemas from multiple branches: "
            f"branch from '{first_id}' has {first_schema.__name__}, "
            f"branch from '{other_id}' has {other_schema.__name__}.{conflict_suffix} {error_msg}",
            component_id=str(row_union_id),
            component_type="row_union",
        )


def get_effective_producer_schema_config(
    graph: ExecutionGraph,
    node_id: str,
    *,
    _visited: frozenset[str] = frozenset(),
) -> SchemaConfig | None:
    """Resolve a producer's schema mode through true pass-through gates."""
    if node_id in _visited:
        return None
    node_info = graph.get_node_info(node_id)
    if node_info.output_schema_config is not None:
        return node_info.output_schema_config
    if node_info.node_type != NodeType.GATE:
        return None

    incoming_configs = [
        get_effective_producer_schema_config(
            graph,
            from_id,
            _visited=_visited | {node_id},
        )
        for from_id, _, _key, edge_data in graph._graph.in_edges(node_id, keys=True, data=True)
        if edge_data["mode"] != RoutingMode.DIVERT
    ]
    known_configs = [config for config in incoming_configs if config is not None]
    if not known_configs:
        return None
    first = known_configs[0]
    return first if all(config == first for config in known_configs[1:]) else None


def row_union_schema_configs_compatible(
    first: SchemaConfig,
    second: SchemaConfig,
) -> tuple[bool, tuple[str, ...], str]:
    """Compare two explicit row_union branch declarations.

    Fixed/fixed declarations are exact contracts and therefore require full
    mutual structural compatibility. If either side is flexible, undeclared
    fields remain possible on that branch; only incompatible types on fields
    both sides explicitly declare are mechanically provable.
    """
    if first.fields is None or second.fields is None:
        return True, (), ""

    first_fields = {field.name: field for field in first.fields}
    second_fields = {field.name: field for field in second.fields}
    if first.mode == "fixed" and second.mode == "fixed":
        first_schema = build_coalesce_schema(first)
        second_schema = build_coalesce_schema(second)
        compatible, error_msg = schemas_structurally_compatible(first_schema, second_schema)
        if compatible:
            return True, (), ""
        conflicting_fields = tuple(
            sorted(name for name in first_fields.keys() | second_fields.keys() if first_fields.get(name) != second_fields.get(name))
        )
        return False, conflicting_fields, error_msg

    conflicting_fields = tuple(
        sorted(
            name
            for name in first_fields.keys() & second_fields.keys()
            if first_fields[name].field_type != "any"
            and second_fields[name].field_type != "any"
            and first_fields[name].field_type != second_fields[name].field_type
        )
    )
    if not conflicting_fields:
        return True, (), ""
    conflict_details = ", ".join(
        f"{name} ({first_fields[name].field_type} vs {second_fields[name].field_type})" for name in conflicting_fields
    )
    return False, conflicting_fields, f"Conflicting shared field types: {conflict_details}"


def validate_sink_required_fields(graph: ExecutionGraph) -> None:
    """Validate each sink's declared_required_fields against upstream guarantees.

    For every SINK node with a non-empty declared_required_fields, check
    every direct predecessor's effective guaranteed fields (via the existing
    get_effective_guaranteed_fields() API). A sink's required fields must
    be a subset of its upstream guaranteed fields.

    This catches cases where:
    - A coalesce marks a field optional (branch-exclusive or AND-downgraded)
      and feeds a sink that requires it (direct or through a transform)
    - A transform's declared_output_fields don't include a field the sink requires
    - A source doesn't guarantee a field the sink requires

    Uses get_effective_guaranteed_fields() rather than reading
    output_schema_config.fields directly. The fields tuple is unreliable
    for shape-changing transforms that use the base _build_output_schema_config()
    — that base method copies INPUT fields into the output config, with
    only guaranteed_fields recomputed correctly. The effective-guarantees
    API is the single source of truth: it handles aggregations (dynamic
    output → no guarantees), coalesce strategies (union intersection,
    nested branch keys, select pass-through), and the base-class
    guaranteed_fields recomputation in one place.

    Sink predecessors are visited via .predecessors() which yields each
    unique source node once, even when the underlying MultiDiGraph has
    parallel edges (e.g., a gate routing two labels to the same sink).

    Runs at build time rather than failing at runtime with a generic
    PluginContractViolation.

    Raises:
        GraphValidationError: if a sink's direct predecessor does not
            guarantee a field the sink requires.
    """
    # Shared cache across all sinks' predecessor walks. Mirrors the
    # schema_cache pattern at validate_edge_compatibility — avoids
    # re-walking common upstream nodes in fan-in topologies.
    effective_fields_cache: dict[str, EffectiveGuaranteeVote] = {}

    for node_id, data in graph._graph.nodes(data=True):
        info = data["info"]
        if info.node_type != NodeType.SINK:
            continue

        sink_required = info.declared_required_fields
        if not sink_required:
            continue

        for predecessor_id in graph._graph.predecessors(node_id):
            vote = walk_effective_guarantee_vote(graph, predecessor_id, effective_fields_cache)
            guaranteed = vote.fields
            if not guaranteed and not vote.participated:
                continue

            missing = sink_required - guaranteed
            if not missing:
                continue

            raise GraphValidationError(
                f"Sink '{info.plugin_name}' requires fields {sorted(missing)} "
                f"but its upstream '{predecessor_id}' does not guarantee them. "
                f"Likely causes: a coalesce union marked these fields optional "
                f"(branch-exclusive or AND-downgraded), or an upstream transform "
                f"did not declare them as guaranteed output. "
                f"Fix: ensure the upstream node guarantees these fields, "
                f"or remove them from the sink's declared_required_fields.",
                component_id=str(node_id),
                component_type="sink",
            )


def _live_predecessors(graph: ExecutionGraph, node_id: str) -> list[str]:
    """Predecessors reachable by a routing edge rows actually traverse.

    DIVERT edges are structural markers: rows reach their destination through
    exception handling, not by traversing the edge, and the payload is an
    error envelope rather than the producer's declared output. A guarantee
    inherited across a DIVERT edge is therefore not a guarantee about what
    arrives — the same reason ``validate_edge_compatibility`` and
    ``get_effective_producer_schema_config`` already skip them.

    A MultiDiGraph can carry both a MOVE and a DIVERT edge between the same
    pair, so a predecessor counts as live when ANY of its edges is non-DIVERT;
    filtering edge-wise without regrouping would drop a live predecessor.

    REACHABILITY, stated honestly: ``build_execution_graph`` cannot currently
    produce a DIVERT edge INTO a transform. Error routing in ELSPETH is
    terminal — every DIVERT edge the builder creates lands on a SINK (source
    quarantine :1126, transform ``on_error`` :1146, gate ``on_error`` :1169,
    sink failsink :1189), and a transform's ``on_error`` is rejected outright
    unless it names a sink (builder.py:1139, mirroring
    ``TransformSettings.validate_on_error``). So on today's production path
    this filter is equivalent to bare ``.predecessors()``. It is kept as
    defence-in-depth for the public ``add_edge`` surface — which tests and any
    future "route errors into a repair transform" topology use — and pinned by
    ``test_divert_only_predecessor_is_not_checked``. Do not read it as
    guarding a live production path.

    Deliberately NOT shared with ``validate_sink_required_fields``, which uses
    bare ``.predecessors()`` and IS DIVERT-reachable (sink → failsink sink):
    there, DIVERT-blindness only widens the set of graphs it rejects for
    missing guarantees, whereas here a spurious guarantee would reject a
    runnable pipeline. Do not "harmonize" these by giving this check the
    looser walk (elspeth-cfcd333f83).
    """
    live: dict[str, None] = {}
    for from_id, _to_id, edge_data in graph._graph.in_edges(node_id, data=True):
        # `==` not `is`: RoutingMode is a StrEnum and add_edge() stores whatever
        # it is handed without coercing, so an edge carrying the plain string
        # "divert" must still be recognised. Identity comparison would treat it
        # as live and reject a runnable pipeline. Matches the comparisons in
        # validate_edge_compatibility and get_effective_producer_schema_config.
        if edge_data["mode"] == RoutingMode.DIVERT:
            continue
        live[str(from_id)] = None
    return list(live)


def validate_transform_output_field_collisions(graph: ExecutionGraph) -> None:
    """Reject transforms that declare an output field their input already carries.

    Output-side twin of ``validate_sink_required_fields``. A transform's
    ``declared_output_fields`` are fields it ADDS to the row;
    ``TransformExecutor._run_preflight`` raises ``PluginContractViolation``
    ("would overwrite existing input fields") on the first row whose keys
    intersect that set. That is a pipeline *configuration* error, so it
    belongs on the build-time surface both ``elspeth run`` and the web
    ``POST /validate`` reach, not after execution has started
    (elspeth-cfcd333f83).

    Scope is TRANSFORM nodes only, and the honest reason is narrower than
    "aggregations cannot hit this". ``AggregationExecutor.execute_flush``
    indeed runs no centralized collision check — but two aggregation-eligible
    plugins HAND-ROLL the identical one in their own bodies and raise the same
    message: ``batch_replicate`` (batch_replicate.py:273-279, and it ships
    wired under ``aggregations:`` in examples/deaggregation/settings.yaml) and
    ``batch_outlier_annotator`` (batch_outlier_annotator.py:278-286). So an
    aggregation-wired instance of those DOES have a runtime failure, and this
    check does not pre-empt it.

    Widening is deliberately left out of elspeth-cfcd333f83 because it needs a
    soundness argument this one does not have, not because the failure is
    absent: aggregations are reductive, so a predecessor's guarantees describe
    the rows going IN to the batch, not the row coming OUT. For a
    ``batch_stats`` emitting count/sum/mean that relationship simply does not
    hold, and intersecting upstream guarantees with declared outputs there
    would reject pipelines the engine runs. Only pass-through-shaped
    aggregations (``output_mode: transform``) carry the input row forward, and
    separating those is its own piece of work.

    ``NodeInfo`` enforces the same boundary by guarding
    ``declared_output_fields`` to TRANSFORM nodes.

    Soundness: reject only where the collision is certain. A predecessor is
    checked only when its effective-guarantee vote actually participated —
    an abstaining (dynamic/observed) upstream may or may not carry the field,
    and speculating would refuse runnable pipelines. Those stay enforced
    per-row by the executor preflight, mirroring the abstention discipline
    ``validate_sink_required_fields`` documents.

    Per-predecessor rejection is the correct granularity: if ONE live
    predecessor definitely guarantees a colliding field, then every row
    arriving from that predecessor crashes the preflight, whatever the other
    inbound paths carry.

    Raises:
        GraphValidationError: if a transform declares an output field that a
            live predecessor definitely guarantees on the arriving row.
    """
    # Shared cache across all transforms' predecessor walks — same pattern as
    # validate_sink_required_fields and the schema_cache in
    # validate_edge_compatibility.
    effective_fields_cache: dict[str, EffectiveGuaranteeVote] = {}

    for node_id, data in graph._graph.nodes(data=True):
        info = data["info"]
        if info.node_type != NodeType.TRANSFORM:
            continue

        declared_output = info.declared_output_fields
        if not declared_output:
            continue

        for predecessor_id in _live_predecessors(graph, node_id):
            vote = walk_effective_guarantee_vote(graph, predecessor_id, effective_fields_cache)
            # Abstention skip. Today every walk branch that reports
            # participated=False also reports an empty field set, so this is
            # implied by the intersection below rather than load-bearing —
            # unlike validate_sink_required_fields, where set DIFFERENCE makes
            # abstention indistinguishable from "guarantees nothing" without
            # the flag. Stated explicitly anyway: it is the soundness rule this
            # check rests on, so a future vote shape that reported
            # possibly-present (rather than guaranteed) fields cannot silently
            # turn a build-time rejection into a guess (elspeth-cfcd333f83).
            if not vote.participated:
                continue

            collisions = sorted(declared_output & vote.fields)
            if not collisions:
                continue

            raise GraphValidationError(
                f"Transform '{info.plugin_name}' (node '{node_id}') declares output "
                f"fields {collisions} that its upstream '{predecessor_id}' already "
                f"guarantees on every row reaching it. The transform would overwrite "
                f"existing row data, which the engine rejects at run time as a "
                f"pipeline configuration error. "
                f"Fix: give the transform a different output field name (for the llm "
                f"transform, change 'response_field'), or drop/rename the existing "
                f"field upstream of this node before it arrives.",
                component_id=str(node_id),
                component_type="transform",
            )


def validate_transform_declared_input_fields(graph: ExecutionGraph) -> None:
    """Reject transforms whose declared input fields no upstream guarantees.

    Input-side twin of ``validate_transform_output_field_collisions``. A
    transform's ``declared_input_fields`` are fields it REQUIRES on every
    arriving row; ``DeclaredRequiredFieldsContract.pre_emission_check``
    (engine/executors/declared_required_fields.py) subtracts them from the
    row's effective fields and raises ``DeclaredRequiredInputFieldsViolation``
    before ``process()`` runs. A declaration the upstream cannot satisfy
    therefore fails 100% of rows, first row onward — a pipeline
    *configuration* error, so it belongs on the build-time surface both
    ``elspeth run`` and the web ``POST /validate`` reach (elspeth-ada5a60249).

    Why a dedicated validator rather than folding the set into the Phase-1
    ``consumer_required`` computation in ``validate_single_edge``: PARTICIPATION.
    Phase 1 fails closed — it compares an explicit, author-written
    ``required_input_fields`` against the producer's guarantees and rejects
    when they are missing, including against an observed producer that
    guarantees nothing. That is right for a promise the author made by hand.
    It is wrong for the six declarations projected here, which are DERIVED
    from ordinary options (web_scrape's ``url_field``, blob_csv_expand's
    ``blob_ref_field`` — which defaults to ``blob_ref``, so the set is
    non-empty even when the author wrote nothing at all). Enforcing those
    against an abstaining upstream would reject every observed-source pipeline
    that names an input column, which the engine runs successfully today.

    Soundness: reject only where the miss is certain. A predecessor is checked
    only when its effective-guarantee vote actually PARTICIPATED. Here that
    flag is load-bearing rather than implied — unlike the output-side twin,
    whose set INTERSECTION is empty for an abstainer anyway, this check uses
    set DIFFERENCE, which cannot distinguish "abstained" from "participated
    and guarantees nothing" without it (the distinction ``EffectiveGuaranteeVote``
    exists to carry, and the one ``validate_sink_required_fields`` rests on).
    An abstaining upstream stays enforced per-row by the executor contract.

    ``_live_predecessors`` filters DIVERT edges for the same never-reject-a-
    runnable-pipeline reason the output twin cites, reached by the opposite
    route: a DIVERT payload is an error envelope rather than the producer's
    declared row, so subtracting its fields would report a miss that the rows
    actually traversing the graph never see.

    The vote this rests on is not firewall-aware, and the resulting divergence
    from the composer runs in ONE direction only (elspeth-9c5ff8fa7d).
    ``walk_effective_guarantee_vote`` unions a predecessor's guarantees through
    every ``passes_through_input`` node without consulting that node's own
    extras firewall, so a ``mode: fixed`` transform mid-pipeline contributes
    upstream fields its ``extra='forbid'`` input model would never admit. The
    walk therefore OVER-promises; against set DIFFERENCE that can only shrink
    ``missing``, so this validator may under-reject through a firewall and can
    never over-reject through one. The composer's declared-input block shares
    the same un-gated union — what rejects there is the neighbouring Rule A
    (``locked_input_extras``, web/composer/state.py), whose emit profile stops
    propagation at a non-extras-allowing contract and reports the extras
    against the firewall node itself.

    Scope that before gating the walk. The union exceeds the firewall node's
    own effective guarantees only by fields its fixed schema does not declare,
    and a row carrying one of those dies AT that node — so wherever rows still
    flow the walk's answer is already correct, and the over-promise names a
    field at a point no row reaches. Firewall-gating this walk is therefore
    defensive; the substantive gap is that no build-time DAG check mirrors
    Rule A at all, so the locked-input death itself builds green. Until
    elspeth-9c5ff8fa7d settles both, treat DAG-accept with composer-reject as
    the expected asymmetry and the reverse as a defect.

    Per-predecessor rejection is the correct granularity: if ONE live
    participating predecessor definitely omits a declared field, every row
    arriving from it fails the executor's pre-emission check, whatever the
    other inbound paths carry.

    ``NodeInfo`` enforces the same boundary by guarding
    ``declared_input_fields`` to TRANSFORM nodes.

    Raises:
        GraphValidationError: if a transform declares an input field that a
            live, participating predecessor does not guarantee.
    """
    # Shared cache across all transforms' predecessor walks — same pattern as
    # validate_sink_required_fields and validate_transform_output_field_collisions.
    effective_fields_cache: dict[str, EffectiveGuaranteeVote] = {}

    for node_id, data in graph._graph.nodes(data=True):
        info = data["info"]
        if info.node_type != NodeType.TRANSFORM:
            continue

        declared_input = info.declared_input_fields
        if not declared_input:
            continue

        for predecessor_id in _live_predecessors(graph, node_id):
            vote = walk_effective_guarantee_vote(graph, predecessor_id, effective_fields_cache)
            if not vote.participated:
                continue

            missing = sorted(declared_input - vote.fields)
            if not missing:
                continue

            raise GraphValidationError(
                f"Transform '{info.plugin_name}' (node '{node_id}') requires input "
                f"fields {missing} that its upstream '{predecessor_id}' does not "
                f"guarantee on the rows reaching it. The transform declares these "
                f"from its own options, and the engine rejects every row missing "
                f"one before the transform runs, so this pipeline fails on the "
                f"first row. "
                f"Fix: point the option that names the column at a field the "
                f"upstream actually emits (for web_scrape that is 'url_field', "
                f"for blob_csv_expand 'blob_ref_field'), or add the field to the "
                f"upstream's schema or guaranteed_fields.",
                component_id=str(node_id),
                component_type="transform",
            )
