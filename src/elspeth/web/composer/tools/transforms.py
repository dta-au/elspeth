"""Composer transforms plane — node, edge, and metadata graph-mutation handlers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Annotated, Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from elspeth.core.config import RuntimeNodeName
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import (
    PatchNodeOptionsArgumentsModel,
    SpliceTransformArgumentsModel,
    _StrictTimeoutSeconds,
)
from elspeth.web.composer.state import (
    CoalesceBranches,
    CompositionState,
    EdgeSpec,
    EdgeType,
    NodeSpec,
    NodeType,
    SourceSpec,
    _batch_aware_placement_error,
    _batch_aware_required_input_fields_error,
    _validate_gate_expression,
    _validate_gate_route_parity,
    composer_component_kind,
    edge_lowering_error,
    queue_node_contract_error,
)
from elspeth.web.composer.tools._common import (
    _STEP_DESCRIPTION_DESCRIPTION,
    ToolContext,
    ToolResult,
    _apply_merge_patch,
    _attach_post_call_hints,
    _canonical_interpretation_requirement_error,
    _canonicalize_authored_interpretation_requirements,
    _composition_canonical_interpretation_requirement_error,
    _credential_wiring_contract_failure,
    _discovery_result,
    _failure_result,
    _mutation_result,
    _options_with_default_llm_reviews,
    _plugin_policy_failure,
    _post_mutation_invariant_error,
    _prevalidate_transform_for_context,
    _prohibited_section,
    _reserved_connection_names,
    _row_union_node_contract_error,
    _runtime_owned_llm_option_error,
    _validate_aggregation_trigger,
    _validate_mutation_arguments,
    _validate_plugin_name,
    _validate_transform_provider_config_path,
    _validate_transform_provider_config_policy,
)
from elspeth.web.composer.tools.declarations import (
    ToolDeclaration,
    ToolKind,
)
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    composition_review_contract_error,
    reconcile_authoritative_reviews,
    serialize_authoring_review_options,
)

_NODE_ROUTING_OPTION_PATCH_KEYS: Final[frozenset[str]] = frozenset({"input", "on_success", "on_error", "routes", "fork_to"})


class _UpsertNodeArgumentsModel(BaseModel):
    id: str
    node_type: NodeType
    input: str
    plugin: str | None = None
    on_success: str | None = None
    on_error: Annotated[str, Field(min_length=1)] | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    condition: str | None = None
    routes: dict[str, str] | None = None
    fork_to: list[str] | None = None
    branches: list[str] | dict[str, str] | None = None
    policy: str | None = None
    merge: str | None = None
    trigger: dict[str, Any] | None = None
    output_mode: str | None = None
    expected_output_count: int | None = None
    timeout_seconds: _StrictTimeoutSeconds | None = None
    description: str | None = None
    scope_name: str | None = None
    scope_opener: str | None = None
    scope_policy: str | None = None

    model_config = ConfigDict(extra="forbid")


class _UpsertEdgeArgumentsModel(BaseModel):
    id: str
    from_node: str
    to_node: str
    edge_type: EdgeType
    label: str | None = None

    model_config = ConfigDict(extra="forbid")


class _RemoveByIdArgumentsModel(BaseModel):
    id: str

    model_config = ConfigDict(extra="forbid")


class _SetMetadataPatchModel(BaseModel):
    name: str | None = None
    description: str | None = None

    model_config = ConfigDict(extra="forbid")


class _SetMetadataArgumentsModel(BaseModel):
    patch: _SetMetadataPatchModel

    model_config = ConfigDict(extra="forbid")


def _handle_list_transforms(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    return _discovery_result(
        state,
        {
            "available": context.catalog.list_transforms(),
            "prohibited": _prohibited_section(context.catalog.list_prohibited_transforms()),
        },
    )


_LIST_TRANSFORMS_DECLARATION = ToolDeclaration(
    name="list_transforms",
    handler=_handle_list_transforms,
    kind=ToolKind.DISCOVERY,
    description=(
        "List available transform plugins. Each entry carries its full `config_fields` "
        "(name, type, required, description, default per option), usage guidance, "
        "`composer_hints`, and `secret_requirements` — not just a name and blurb. "
        "The result's `prohibited` array names any transform categorically banned from "
        "the web authoring surface by security policy, with its closed reason and "
        "explanation — cite it when a user asks why a specific plugin is unavailable. "
        "Call get_plugin_schema only for enum values, nested option shapes, or the "
        "raw JSON schema; this listing already answers ordinary configuration questions."
    ),
    json_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    cacheable=True,
)


def _handle_list_sinks(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    return _discovery_result(
        state,
        {
            "available": context.catalog.list_sinks(),
            "prohibited": _prohibited_section(context.catalog.list_prohibited_sinks()),
        },
    )


_LIST_SINKS_DECLARATION = ToolDeclaration(
    name="list_sinks",
    handler=_handle_list_sinks,
    kind=ToolKind.DISCOVERY,
    description=(
        "List available sink plugins. Each entry carries its full `config_fields` "
        "(name, type, required, description, default per option), usage guidance, "
        "`composer_hints`, and `secret_requirements` — not just a name and blurb. "
        "The result's `prohibited` array names any sink categorically banned from "
        "the web authoring surface by security policy, with its closed reason and "
        "explanation — cite it when a user asks why a specific plugin is unavailable. "
        "Call get_plugin_schema only for enum values, nested option shapes, or the "
        "raw JSON schema; this listing already answers ordinary configuration questions."
    ),
    json_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    cacheable=True,
)


_UPSERT_NODE_DECLARATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Unique node identifier."},
        "node_type": {
            "type": "string",
            "enum": ["transform", "gate", "aggregation", "coalesce", "row_union", "queue"],
        },
        "plugin": {
            "type": ["string", "null"],
            "description": "Plugin name. Required for transform/aggregation. Null for gate/coalesce/row_union/queue.",
        },
        "input": {
            "type": "string",
            "description": (
                "Connection-name string this node CONSUMES: must equal an upstream's on_success "
                "(or routes value, or on_error), NOT the upstream node's id — connections match "
                "by string, not by graph topology."
            ),
        },
        "on_success": {
            "type": ["string", "null"],
            "description": (
                "Output connection, consumed by a downstream input/sink_name (matched by string). "
                "Required for transform/aggregation/row_union; null for gates (they route via "
                "condition/routes). A row_union MUST publish to a processing connection, never "
                "directly to a sink. A coalesce normally publishes under its own node id; its "
                "optional on_success may name only a sink."
            ),
        },
        "on_error": {
            "type": ["string", "null"],
            "minLength": 1,
            "description": (
                "Node-level error policy (transform/aggregation/gate): 'discard' or a declared sink name. "
                "For a gate it covers row expression-evaluation errors and is authored here, never as an "
                "edge; omit it to preserve fail-fast behavior."
            ),
        },
        "options": {
            "type": "object",
            "description": (
                "Plugin-specific config (transform/aggregation only). The schema: block declares what "
                "ARRIVES at the node, never its transformed result; declare arriving types on the "
                "SOURCE schema or via an upstream type_coerce (observed CSV fields arrive as str)."
            ),
        },
        "condition": {"type": ["string", "null"], "description": "Boolean expression (gate only). Evaluated per row."},
        "routes": {
            "type": ["object", "null"],
            "description": (
                "Gate route mapping {true: ..., false: ...}; each value is a sink, a connection, or "
                "'discard' for an audited gate_discarded terminal drop. Mutually exclusive with fork_to."
            ),
        },
        "fork_to": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "Fork destinations — row is copied to all listed paths (gate only, mutually exclusive with routes).",
        },
        "branches": {
            "type": ["array", "object", "null"],
            "items": {"type": "string"},
            "additionalProperties": {"type": "string"},
            "description": (
                "Branch inputs for coalesce or row_union — list form, or {branch_name: input_connection} "
                "when a branch flows through transforms. A row_union consumes EVERY branches value as a "
                "real input and releases the original rows without merging fields."
            ),
        },
        "policy": {
            "type": ["string", "null"],
            "description": "Arrival policy (coalesce only). Omitting it means 'require_all', the runtime default — every branch must arrive.",
        },
        "merge": {
            "type": ["string", "null"],
            "description": "Field merge strategy (coalesce only). Omitting it means 'union', the runtime default — union's schema rules are enforced either way.",
        },
        "trigger": {
            "type": ["object", "null"],
            "description": "Optional early batch trigger config (aggregation only). Omit, null, or {} for end-of-source-only aggregation.",
            "additionalProperties": False,
            "properties": {
                "count": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "description": "Flush after this many accepted rows.",
                },
                "timeout_seconds": {
                    "type": ["number", "null"],
                    "exclusiveMinimum": 0,
                    "description": "Flush after this many seconds since the first accepted row.",
                },
                "condition": {
                    "type": ["string", "null"],
                    "description": "Boolean expression over row['batch_count'] and row['batch_age_seconds']; do not use end_of_source here.",
                },
            },
        },
        "output_mode": {
            "type": ["string", "null"],
            "enum": ["passthrough", "transform", None],
            "description": "Aggregation output mode (aggregation only). Defaults to 'transform' if omitted.",
        },
        "expected_output_count": {
            "type": ["integer", "null"],
            "description": "Expected aggregation output row count; omit when output count depends on group_by distinct values.",
        },
        "timeout_seconds": {
            "type": ["number", "null"],
            "exclusiveMinimum": 0,
            "description": "A finite positive structural-barrier timeout in seconds (coalesce/row_union only).",
        },
        "description": {
            "type": ["string", "null"],
            "description": _STEP_DESCRIPTION_DESCRIPTION,
        },
        "scope_name": {
            "type": ["string", "null"],
            "description": "Scope identifier for the EXPAND group this collector closes (collector only).",
        },
        "scope_opener": {
            "type": ["string", "null"],
            "description": "Node id of the multi-row transform that opens the collector's EXPAND group (collector only).",
        },
        "scope_policy": {
            "type": ["string", "null"],
            "enum": ["require_all", "best_effort", None],
            "description": (
                "Group arrival policy (collector only). REQUIRED for collectors — no default; the author "
                "decides whether a lost member fails the group."
            ),
        },
    },
    "required": ["id", "node_type", "input"],
    "additionalProperties": False,
}


def _handle_upsert_node(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    # _execute_upsert_node validates arguments via _validate_mutation_arguments,
    # which raises ToolArgumentError on Pydantic failure — a
    # PydanticValidationError can never escape into this caller. Re-validation
    # on the success branch is deterministic by the same model; we only need
    # the validated.id to look up the post-mutation node for hint resolution.
    result = _execute_upsert_node(arguments, state, context)
    if not result.success:
        return result
    validated = _UpsertNodeArgumentsModel.model_validate(arguments)
    node_id = validated.id
    node = next((n for n in result.updated_state.nodes if n.id == node_id), None)
    # Offensive programming: _execute_upsert_node succeeded above, so the
    # node it just upserted MUST be on the post-mutation state. Absence
    # here would be a bug in state.with_node, not a runtime condition.
    if node is None:
        raise AssertionError(
            f"_execute_upsert_node succeeded for node '{node_id}' but the post-mutation state does not contain it — invariant violation."
        )
    return _attach_post_call_hints(
        result,
        context.catalog,
        plugin_type="transform",
        tool_name="upsert_node",
        plugin_name=node.plugin,
        config_snapshot=node.options,
    )


_UPSERT_NODE_DECLARATION = ToolDeclaration(
    name="upsert_node",
    handler=_handle_upsert_node,
    kind=ToolKind.MUTATION,
    description=(
        "Add or update a pipeline node. "
        "Fields are node_type-dependent: "
        "transform/aggregation use plugin+options; "
        "gate uses condition+routes (or fork_to) and optional node-level on_error; "
        "coalesce uses branches+policy+merge; "
        "row_union is a plugin-free require_all N-to-N barrier: provide at least "
        "two ordered branches, set input to the first branch connection, set "
        "on_success to a processing connection (never a sink), omit policy/merge/"
        "options/routing fields, and optionally set a finite positive timeout_seconds; "
        "queue is a structural fan-in point — set id == input to the shared "
        "connection name, omit plugin and every routing field (on_success/"
        "on_error/routes/fork_to), and options accepts only an optional "
        "description. Multiple producers may publish that name precisely "
        "because the queue is declared. "
        "collector closes a declared EXPAND scope with a batch-aware plugin: "
        "set scope_name, scope_opener (the multi-row transform that opens the "
        "group), and scope_policy ('require_all' or 'best_effort', no default). "
        "Omit fields that don't apply to your node_type."
    ),
    json_schema=_UPSERT_NODE_DECLARATION_JSON_SCHEMA,
    augments_on_failure=True,
)


def _handle_upsert_edge(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    return _execute_upsert_edge(arguments, state, context)


_UPSERT_EDGE_DECLARATION = ToolDeclaration(
    name="upsert_edge",
    handler=_handle_upsert_edge,
    kind=ToolKind.MUTATION,
    description=(
        "Add or update a connection between nodes. When the edge targets a sink, "
        "this also updates the source/node routing field used by runtime. "
        "edge_type='on_error' sink wiring is supported for transform/aggregation nodes only; "
        "a gate's expression-error policy is node-level and must be set with upsert_node.on_error. "
        "Gate success routing uses route_true, route_false, or fork."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Unique edge identifier."},
            "from_node": {"type": "string", "description": "Source node ID or 'source'."},
            "to_node": {"type": "string", "description": "Destination node ID or sink name."},
            "edge_type": {
                "type": "string",
                "enum": ["on_success", "on_error", "route_true", "route_false", "fork"],
            },
            "label": {"type": ["string", "null"], "description": "Display label."},
        },
        "required": ["id", "from_node", "to_node", "edge_type"],
        "additionalProperties": False,
        "examples": [
            {
                "id": "e_judge_layers_error",
                "from_node": "judge_layers",
                "to_node": "llm_failures",
                "edge_type": "on_error",
                "label": "LLM failures",
            }
        ],
    },
)


def _handle_remove_node(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    return _execute_remove_node(arguments, state, context)


_REMOVE_NODE_DECLARATION = ToolDeclaration(
    name="remove_node",
    handler=_handle_remove_node,
    kind=ToolKind.MUTATION,
    description="Remove a node and all its edges.",
    json_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Node ID to remove."},
        },
        "required": ["id"],
        "additionalProperties": False,
    },
)


def _handle_remove_edge(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    return _execute_remove_edge(arguments, state, context)


_REMOVE_EDGE_DECLARATION = ToolDeclaration(
    name="remove_edge",
    handler=_handle_remove_edge,
    kind=ToolKind.MUTATION,
    description="Remove an edge by ID.",
    json_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Edge ID to remove."},
        },
        "required": ["id"],
        "additionalProperties": False,
    },
)


def _handle_set_metadata(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    return _execute_set_metadata(arguments, state, context)


_SET_METADATA_DECLARATION = ToolDeclaration(
    name="set_metadata",
    handler=_handle_set_metadata,
    kind=ToolKind.MUTATION,
    description="Update pipeline metadata (name and description only).",
    json_schema={
        "type": "object",
        "properties": {
            "patch": {
                "type": "object",
                "description": "Partial metadata update. Only included fields are changed.",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
        "required": ["patch"],
        "additionalProperties": False,
    },
)


def _execute_upsert_queue_node(
    validated: _UpsertNodeArgumentsModel,
    state: CompositionState,
) -> ToolResult:
    """Insert/update a canonical structural queue node.

    Construct the NodeSpec from the validated arguments verbatim, run ONLY the
    intrinsic ``queue_node_contract_error`` guard, and mutate (``with_node``)
    only after that check passes. A malformed queue (id != input, any
    forbidden field, unknown/typed option) returns an ordinary
    ``_failure_result`` and leaves the exact prior state/version unchanged —
    ``with_node`` is never reached, so the mutation is atomic. A canonical
    queue succeeds and persists even when the resulting pipeline is
    incomplete (missing producers/downstream); completeness is validation
    telemetry surfaced on the returned ``ToolResult.validation``, not a
    mutation rejection.
    """
    fork_to: tuple[str, ...] | None = tuple(validated.fork_to) if validated.fork_to is not None else None
    branches: CoalesceBranches | None = None
    if validated.branches is not None:
        branches = dict(validated.branches) if isinstance(validated.branches, Mapping) else tuple(validated.branches)
    node = NodeSpec(
        id=validated.id,
        node_type="queue",
        plugin=validated.plugin,
        input=validated.input,
        on_success=validated.on_success,
        on_error=validated.on_error,
        options=dict(validated.options),
        condition=validated.condition,
        routes=validated.routes,
        fork_to=fork_to,
        branches=branches,
        policy=validated.policy,
        merge=validated.merge,
        trigger=validated.trigger,
        output_mode=validated.output_mode,
        expected_output_count=validated.expected_output_count,
        timeout_seconds=validated.timeout_seconds,
        description=validated.description,
    )
    contract_error = queue_node_contract_error(node)
    if contract_error is not None:
        return _failure_result(state, contract_error)

    new_state = state.with_node(node)
    affected = {validated.id}
    for edge in new_state.edges:
        if edge.from_node == validated.id or edge.to_node == validated.id:
            affected.add(edge.from_node)
            affected.add(edge.to_node)
    return _mutation_result(new_state, tuple(sorted(affected)))


def _execute_upsert_node(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Add or update a pipeline node."""
    validated = cast(_UpsertNodeArgumentsModel, _validate_mutation_arguments(_UpsertNodeArgumentsModel, args, "upsert_node arguments"))
    node_id = validated.id
    node_type = validated.node_type
    plugin = validated.plugin
    node_options: Mapping[str, Any] = validated.options
    existing_node = next((node for node in state.nodes if node.id == node_id), None)
    runtime_owned_error = _runtime_owned_llm_option_error(
        plugin,
        node_options,
        tool_name="upsert_node",
        component_id=node_id,
    )
    if runtime_owned_error is not None:
        error_code = "interpretation_requirements_invalid" if INTERPRETATION_REQUIREMENTS_KEY in node_options else None
        return _failure_result(state, f"Node '{node_id}': {runtime_owned_error}", error_code=error_code)
    node_options = _canonicalize_authored_interpretation_requirements(
        node_options,
        component_id=node_id,
        existing_options=existing_node.options if existing_node is not None else None,
    )
    review_options = _options_with_default_llm_reviews(
        node_id=node_id,
        plugin=plugin,
        options=node_options,
        existing_options=existing_node.options if existing_node is not None else None,
    )
    canonical_error = _canonical_interpretation_requirement_error(
        review_options,
        tool_name="upsert_node",
    )
    if canonical_error is not None:
        return _failure_result(
            state,
            f"Node '{node_id}': {canonical_error}",
            error_code="interpretation_requirements_invalid",
        )
    if node_type == "queue":
        # Canonical invariant B applies to structural queues too; the queue
        # contract then decides whether those canonical options are permitted.
        return _execute_upsert_queue_node(
            validated.model_copy(update={"options": dict(review_options)}),
            state,
        )
    credential_error = _credential_wiring_contract_failure(
        state,
        component_id=node_id,
        component_type="node",
        plugin_type="transform" if plugin is not None else None,
        plugin_name=plugin,
        options=review_options,
    )
    if credential_error is not None:
        return credential_error

    # Validate plugin for types that require one.
    # Gates and coalesces intentionally have plugin=None (they're expression-based or
    # structural, not plugin-driven), so the "and plugin is not None" guard covers them.
    # NodeSpec documents this: "plugin: Plugin name. None for gates and coalesces."
    # Collectors reuse the batch-transform plugin contract, so their plugin
    # runs the same policy/prevalidation gates (barrier-scopes spec §3).
    if node_type in ("transform", "aggregation", "collector") and plugin is not None:
        plugin_error = _validate_plugin_name(context, "transform", plugin)
        if plugin_error is not None:
            return _plugin_policy_failure(state, plugin_error)

        batch_placement_error = _batch_aware_placement_error(node_id, node_type, plugin, validated.output_mode)
        if batch_placement_error is not None:
            return _failure_result(state, batch_placement_error)

        batch_required_error = _batch_aware_required_input_fields_error(node_id, plugin, node_options)
        if batch_required_error is not None:
            return _failure_result(state, batch_required_error)

        prevalidation_error = _prevalidate_transform_for_context(context, plugin, review_options)
        if prevalidation_error is not None:
            return _failure_result(state, prevalidation_error, error_code="plugin_options_invalid")

        # Operator-profiled nodes carry their private provider config (retry
        # budget / provider binding) in the profile, injected only at lowering;
        # ``_prevalidate_transform_for_context`` above already validated the
        # LOWERED executable. Running the raw provider-config policy on the
        # authored options would false-positive on the absent private retry
        # budget (see the fuller rationale at set_pipeline in sessions.py).
        if "profile" not in review_options:
            provider_policy_error = _validate_transform_provider_config_policy(review_options, plugin=plugin)
            if provider_policy_error is not None:
                return _failure_result(state, f"Node '{node_id}': {provider_policy_error}")

        provider_path_error = _validate_transform_provider_config_path(review_options, context.data_dir, session_id=context.session_id)
        if provider_path_error is not None:
            return _failure_result(state, f"Node '{node_id}': {provider_path_error}")

    condition = validated.condition
    if node_type == "gate" and condition is not None:
        expr_error = _validate_gate_expression(condition)
        if expr_error is not None:
            return _failure_result(state, f"Node '{node_id}': {expr_error}")
        parity_error = _validate_gate_route_parity(condition, validated.routes)
        if parity_error is not None:
            return _failure_result(state, f"Node '{node_id}': {parity_error}", error_code="gate_route_labels_mismatch")
    if node_type == "aggregation":
        trigger_error = _validate_aggregation_trigger(validated.trigger)
        if trigger_error is not None:
            return _failure_result(state, f"Node '{node_id}': {trigger_error}")

    fork_to: tuple[str, ...] | None = tuple(validated.fork_to) if validated.fork_to is not None else None

    branches: CoalesceBranches | None = None
    if validated.branches is not None:
        branches = dict(validated.branches) if isinstance(validated.branches, Mapping) else tuple(validated.branches)

    node = NodeSpec(
        id=node_id,
        node_type=node_type,
        plugin=plugin,
        input=validated.input,
        on_success=validated.on_success,
        on_error=validated.on_error or ("discard" if node_type in ("transform", "aggregation") else None),
        options=review_options,
        condition=validated.condition,
        routes=validated.routes,
        fork_to=fork_to,
        branches=branches,
        policy=validated.policy,
        merge=validated.merge,
        trigger=validated.trigger,
        output_mode=validated.output_mode,
        expected_output_count=validated.expected_output_count,
        timeout_seconds=validated.timeout_seconds,
        description=validated.description,
        scope_name=validated.scope_name,
        scope_opener=validated.scope_opener,
        scope_policy=validated.scope_policy,
    )

    row_union_contract_error = _row_union_node_contract_error(
        node,
        output_names=frozenset(output.name for output in state.outputs),
    )
    if row_union_contract_error is not None:
        message, error_code = row_union_contract_error
        return _failure_result(state, message, error_code=error_code)

    proposed_state = state.with_node(node)
    # Scalar routes committed here are the runtime authority; any visual edge
    # that mirrored the node's previous sink routes must follow in the same
    # commit or the graph keeps drawing the old route (elspeth-372e18e365).
    proposed_state = _reconcile_node_sink_mirror_edges(proposed_state, node)
    invariant_error = _post_mutation_invariant_error(proposed_state)
    if invariant_error is not None:
        message, error_code = invariant_error
        return _failure_result(state, message, error_code=error_code)
    try:
        new_state = reconcile_authoritative_reviews(state, proposed_state)
    except (KeyError, TypeError, ValueError):
        return _failure_result(
            state,
            "Authoritative interpretation-review reconciliation failed. Re-inspect the pipeline and retry.",
            error_code="review_reconciliation_failed",
        )
    review_contract_error = composition_review_contract_error(new_state)
    if review_contract_error is not None:
        return _failure_result(state, review_contract_error)

    # Affected: the node itself plus nodes with edges referencing it
    affected = {node_id}
    for edge in new_state.edges:
        if edge.from_node == node_id or edge.to_node == node_id:
            affected.add(edge.from_node)
            affected.add(edge.to_node)

    return _mutation_result(new_state, tuple(sorted(affected)))


def _splice_connection_name(node_id: str, state: CompositionState) -> str | None:
    reserved = (
        _reserved_connection_names(state)
        | set(state.sources)
        | {node.id for node in state.nodes}
        | {output.name for output in state.outputs}
    )
    for attempt in range(1, _SPLICE_CONNECTION_ATTEMPTS + 1):
        suffix = "" if attempt == 1 else f"_{attempt}"
        stem_length = _SPLICE_CONNECTION_MAX_LENGTH - len("_out") - len(suffix)
        candidate = f"{node_id[:stem_length]}_out{suffix}"
        if candidate not in reserved:
            return candidate
    return None


def _splice_edge_id(direct_edge_id: str, node_id: str) -> str:
    marker = "__splice__"
    node_fragment_length = max(1, _SPLICE_EDGE_ID_MAX_LENGTH - len(marker) - 1)
    suffix = f"{marker}{node_id[:node_fragment_length]}"
    stem_length = max(1, _SPLICE_EDGE_ID_MAX_LENGTH - len(suffix))
    return f"{direct_edge_id[:stem_length]}{suffix}"


def _normalized_splice_node_projection(node: NodeSpec) -> dict[str, Any]:
    return {
        "id": node.id,
        "plugin": node.plugin,
        "on_error": node.on_error or "discard",
        "options": serialize_authoring_review_options(node.options),
    }


def _sink_route_still_expressed(state: CompositionState, edge: EdgeSpec) -> bool:
    """Return whether another edge in ``state`` expresses the same sink route.

    Legacy persisted states can carry semantic duplicates from before the
    slot-uniqueness admission rule; clearing a scalar mirror while a surviving
    duplicate still draws the route would desynchronise graph and runtime.
    """
    return any(
        candidate.id != edge.id
        and candidate.from_node == edge.from_node
        and candidate.edge_type == edge.edge_type
        and candidate.to_node == edge.to_node
        for candidate in state.edges
    )


def _apply_sink_edge_route(state: CompositionState, edge: EdgeSpec) -> CompositionState:
    """Write the scalar route a sink-targeting edge expresses.

    Exact dual of :func:`_clear_removed_sink_edge_route`. Callers must have
    admitted the edge through :func:`edge_lowering_error` first — this maps an
    already-legal edge onto its scalar slot; it decides nothing. Edges whose
    target is not a declared output are advisory and left unmirrored.
    """
    output_names = {output.name for output in state.outputs}
    if edge.to_node not in output_names:
        return state

    if edge.from_node in state.sources:
        source = state.sources[edge.from_node]
        if source.on_success != edge.to_node:
            return state.with_named_source(edge.from_node, replace(source, on_success=edge.to_node))
        return state

    node = next((candidate for candidate in state.nodes if candidate.id == edge.from_node), None)
    if node is None:
        return state
    if edge.edge_type == "on_success":
        if node.on_success != edge.to_node:
            return state.with_node(replace(node, on_success=edge.to_node))
        return state
    if edge.edge_type == "on_error":
        if node.on_error != edge.to_node:
            return state.with_node(replace(node, on_error=edge.to_node))
        return state
    if edge.edge_type in ("route_true", "route_false"):
        route_key = "true" if edge.edge_type == "route_true" else "false"
        routes = dict(node.routes or {})
        if route_key not in routes or routes[route_key] != edge.to_node:
            routes[route_key] = edge.to_node
            return state.with_node(replace(node, routes=routes))
        return state
    # fork
    fork_targets = tuple(dict.fromkeys((*(node.fork_to or ()), edge.to_node)))
    if node.fork_to != fork_targets:
        return state.with_node(replace(node, fork_to=fork_targets))
    return state


def _reconcile_node_sink_mirror_edges(state: CompositionState, node: NodeSpec) -> CompositionState:
    """Converge this node's sink-mirror edges onto its scalar routes.

    upsert_node commits scalar routing directly; any visual edge that mirrored
    the previous scalars would otherwise keep drawing the old route. Each
    sink-targeting edge from the node is retargeted to the scalar's current
    sink or removed when the slot no longer routes to a sink. Missing edges
    are never invented — the graph view infers undrawn routes from the
    scalars, so absence cannot lie the way a stale edge does.
    """
    output_names = {output.name for output in state.outputs}
    routes = node.routes or {}

    def _slot_sink(value: str | None) -> str | None:
        return value if value is not None and value in output_names else None

    slot_sinks: dict[str, str | None] = {
        "on_success": _slot_sink(node.on_success),
        "on_error": _slot_sink(node.on_error),
        "route_true": _slot_sink(routes["true"] if "true" in routes else None),
        "route_false": _slot_sink(routes["false"] if "false" in routes else None),
    }
    fork_sinks = {target for target in (node.fork_to or ()) if target in output_names}

    claimed_slots: set[str] = set()
    new_state = state
    for edge in state.edges:
        if edge.from_node != node.id or edge.to_node not in output_names:
            continue
        if edge.edge_type == "fork":
            if edge.to_node not in fork_sinks:
                without = new_state.without_edge(edge.id)
                if without is not None:
                    new_state = without
            continue
        desired = slot_sinks[edge.edge_type]
        if desired is None or edge.edge_type in claimed_slots:
            without = new_state.without_edge(edge.id)
            if without is not None:
                new_state = without
            continue
        claimed_slots.add(edge.edge_type)
        if edge.to_node != desired:
            new_state = new_state.with_edge(replace(edge, to_node=desired))
    return new_state


def _clear_removed_sink_edge_route(state: CompositionState, edge: EdgeSpec) -> CompositionState:
    """Clear runtime routing that was written for a removed sink edge."""
    output_names = {output.name for output in state.outputs}
    if edge.to_node not in output_names:
        return state

    if edge.from_node in state.sources:
        if edge.edge_type != "on_success":
            return state
        source = state.sources[edge.from_node]
        if source.on_success == edge.to_node:
            return state.with_named_source(edge.from_node, replace(source, on_success="discard"))
        return state

    node = next((candidate for candidate in state.nodes if candidate.id == edge.from_node), None)
    if node is None:
        return state
    if edge.edge_type == "on_success":
        if node.on_success == edge.to_node:
            return state.with_node(replace(node, on_success=None))
        return state
    if edge.edge_type == "on_error":
        if node.on_error == edge.to_node:
            return state.with_node(replace(node, on_error=None))
        return state
    if edge.edge_type in ("route_true", "route_false"):
        route_key = "true" if edge.edge_type == "route_true" else "false"
        routes = dict(node.routes or {})
        if routes.get(route_key) != edge.to_node:
            return state
        del routes[route_key]
        return state.with_node(replace(node, routes=routes or None))
    if edge.edge_type == "fork":
        fork_to = tuple(target for target in (node.fork_to or ()) if target != edge.to_node)
        if fork_to == (node.fork_to or ()):
            return state
        return state.with_node(replace(node, fork_to=fork_to or None))
    return state


def _splice_predecessor(
    state: CompositionState,
    predecessor_id: str,
) -> SourceSpec | NodeSpec | None:
    if predecessor_id in state.sources:
        return state.sources[predecessor_id]
    return next((node for node in state.nodes if node.id == predecessor_id), None)


def _splice_topology_error(
    state: CompositionState,
    *,
    predecessor_id: str,
    successor: NodeSpec,
    inserted: NodeSpec | None = None,
) -> str | None:
    predecessor = _splice_predecessor(state, predecessor_id)
    if predecessor is None:
        return f"Splice predecessor '{predecessor_id}' not found."
    if type(predecessor) is NodeSpec and predecessor.node_type != "transform":
        return "splice_transform supports only source or transform predecessors on a direct linear path."
    if successor.node_type != "transform":
        return "splice_transform requires a transform successor on a direct linear path."
    for node in (predecessor, successor):
        if type(node) is NodeSpec and (
            node.condition is not None
            or node.routes is not None
            or node.fork_to is not None
            or node.branches is not None
            or node.node_type in {"gate", "coalesce", "queue"}
        ):
            return "splice_transform does not support gates, forks, queues, coalesces, or branched paths."
        if type(node) is NodeSpec and node.on_error not in (None, "discard"):
            return "splice_transform does not support predecessors or successors with routed error branches."

    if inserted is None:
        predecessor_output = predecessor.on_success
        if type(predecessor_output) is not str or not predecessor_output or predecessor_output == "discard":
            return "Splice predecessor has no direct on_success connection."
        if predecessor_output != successor.input:
            return "Splice predecessor and successor do not share one direct on_success connection."
        matching = [
            edge
            for edge in state.edges
            if edge.from_node == predecessor_id and edge.to_node == successor.id and edge.edge_type == "on_success"
        ]
        if len(matching) != 1:
            return "Splice path must have exactly one direct visual on_success edge."
        if any(edge.from_node == predecessor_id and edge is not matching[0] for edge in state.edges):
            return "Splice predecessor has an ambiguous or branched visual path."
        if any(edge.to_node == successor.id and edge is not matching[0] for edge in state.edges):
            return "Splice successor has an ambiguous or branched visual path."
        other_consumers = [node.id for node in state.nodes if node.id != successor.id and node.input == predecessor_output]
        if other_consumers or predecessor_output in {output.name for output in state.outputs}:
            return "Splice connection has multiple consumers or terminates at a sink."
        return None

    if (
        inserted.node_type != "transform"
        or inserted.condition is not None
        or inserted.routes is not None
        or inserted.fork_to is not None
        or inserted.branches is not None
        or inserted.policy is not None
        or inserted.merge is not None
        or inserted.trigger is not None
        or inserted.output_mode is not None
        or inserted.expected_output_count is not None
    ):
        return "Existing splice node is not a canonical transform."
    if predecessor.on_success != inserted.input or inserted.on_success != successor.input:
        return "Existing splice topology does not match the server-derived direct path."
    predecessor_consumers = [node.id for node in state.nodes if node.id != inserted.id and node.input == predecessor.on_success]
    inserted_consumers = [node.id for node in state.nodes if node.id != successor.id and node.input == inserted.on_success]
    output_names = {output.name for output in state.outputs}
    if predecessor_consumers or inserted_consumers or predecessor.on_success in output_names or inserted.on_success in output_names:
        return "Existing splice topology contains multiple consumers or terminates at a sink."
    try:
        inserted_index = next(index for index, node in enumerate(state.nodes) if node is inserted)
        successor_index = next(index for index, node in enumerate(state.nodes) if node is successor)
    except StopIteration:
        return "Existing splice topology is incomplete."
    if inserted_index + 1 != successor_index:
        return "Existing splice node is not immediately before its successor."
    predecessor_edges = [
        (index, edge)
        for index, edge in enumerate(state.edges)
        if edge.from_node == predecessor_id and edge.to_node == inserted.id and edge.edge_type == "on_success"
    ]
    successor_edges = [
        (index, edge)
        for index, edge in enumerate(state.edges)
        if edge.from_node == inserted.id and edge.to_node == successor.id and edge.edge_type == "on_success"
    ]
    if len(predecessor_edges) != 1 or len(successor_edges) != 1:
        return "Existing splice topology does not have exactly two direct visual edges."
    predecessor_edge_index, predecessor_edge = predecessor_edges[0]
    successor_edge_index, successor_edge = successor_edges[0]
    if successor_edge_index != predecessor_edge_index + 1:
        return "Existing splice edges are not in canonical adjacent order."
    if successor_edge.id != _splice_edge_id(predecessor_edge.id, inserted.id):
        return "Existing splice edge identity differs from the server-derived identity."
    allowed_edge_ids = {predecessor_edge.id, successor_edge.id}
    if any(
        edge.id not in allowed_edge_ids and (edge.from_node in {predecessor_id, inserted.id} or edge.to_node in {inserted.id, successor.id})
        for edge in state.edges
    ):
        return "Existing splice topology contains a conflicting visual path."
    return None


def _execute_splice_transform(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Atomically insert one transform on an existing direct linear path."""
    validated = cast(
        SpliceTransformArgumentsModel,
        _validate_mutation_arguments(SpliceTransformArgumentsModel, args, "splice_transform arguments"),
    )
    predecessor_id = validated.predecessor_id
    successor_id = validated.successor_id
    node_args = validated.node
    if predecessor_id == successor_id or node_args.id in {predecessor_id, successor_id}:
        return _failure_result(state, "Splice predecessor, successor, and inserted node IDs must be distinct.")
    if len({node.id for node in state.nodes}) != len(state.nodes):
        return _failure_result(state, "splice_transform refuses a state with duplicate node IDs.")
    if len({edge.id for edge in state.edges}) != len(state.edges):
        return _failure_result(state, "splice_transform refuses a state with duplicate edge IDs.")
    successor = next((node for node in state.nodes if node.id == successor_id), None)
    if successor is None:
        return _failure_result(state, f"Splice successor '{successor_id}' not found or is not a transform.")

    existing = next((node for node in state.nodes if node.id == node_args.id), None)
    if existing is not None:
        topology_error = _splice_topology_error(
            state,
            predecessor_id=predecessor_id,
            successor=successor,
            inserted=existing,
        )
        if topology_error is not None:
            return _failure_result(state, topology_error)
        prepared_replay = _prepare_transform_candidate(
            state=state,
            context=context,
            tool_name="splice_transform",
            node_id=node_args.id,
            node_type="transform",
            plugin=node_args.plugin,
            input_name=existing.input,
            on_success=existing.on_success,
            on_error=node_args.on_error,
            options=node_args.options,
            existing_options=existing.options,
            description=node_args.description,
        )
        if type(prepared_replay) is ToolResult:
            return prepared_replay
        replay_node = cast(NodeSpec, prepared_replay)
        try:
            identical = _normalized_splice_node_projection(replay_node) == _normalized_splice_node_projection(existing)
        except (KeyError, TypeError, ValueError):
            return _failure_result(state, f"Node '{node_args.id}' already exists with a divergent splice definition.")
        if not identical:
            return _failure_result(state, f"Node '{node_args.id}' already exists with a divergent splice definition.")
        canonical_error = _composition_canonical_interpretation_requirement_error(
            state,
            tool_name="splice_transform",
        )
        if canonical_error is not None:
            return _failure_result(
                state,
                canonical_error,
                error_code="interpretation_requirements_invalid",
            )
        return _mutation_result(
            state,
            (predecessor_id, node_args.id, successor_id),
            data={
                "already_applied": True,
                "predecessor_id": predecessor_id,
                "successor_id": successor_id,
                "inserted_node_id": node_args.id,
                "derived_connection": existing.on_success,
            },
        )

    if node_args.id in state.sources or node_args.id in {output.name for output in state.outputs}:
        return _failure_result(state, f"Inserted node ID '{node_args.id}' collides with an existing source or sink.")
    topology_error = _splice_topology_error(state, predecessor_id=predecessor_id, successor=successor)
    if topology_error is not None:
        return _failure_result(state, topology_error)
    predecessor = _splice_predecessor(state, predecessor_id)
    assert predecessor is not None
    predecessor_output = predecessor.on_success
    assert type(predecessor_output) is str
    direct_edge_index, direct_edge = next(
        (index, edge)
        for index, edge in enumerate(state.edges)
        if edge.from_node == predecessor_id and edge.to_node == successor_id and edge.edge_type == "on_success"
    )
    connection_name = _splice_connection_name(node_args.id, state)
    if connection_name is None:
        return _failure_result(state, "No bounded collision-free splice connection name is available.")
    new_edge_id = _splice_edge_id(direct_edge.id, node_args.id)
    if new_edge_id in {edge.id for edge in state.edges}:
        return _failure_result(state, f"Derived splice edge ID '{new_edge_id}' collides with an existing edge.")
    prepared = _prepare_transform_candidate(
        state=state,
        context=context,
        tool_name="splice_transform",
        node_id=node_args.id,
        node_type="transform",
        plugin=node_args.plugin,
        input_name=predecessor_output,
        on_success=connection_name,
        on_error=node_args.on_error,
        options=node_args.options,
        description=node_args.description,
    )
    if type(prepared) is ToolResult:
        return prepared
    prepared_node = cast(NodeSpec, prepared)

    successor_rewired = replace(successor, input=connection_name)
    successor_index = next(index for index, node in enumerate(state.nodes) if node is successor)
    nodes = (*state.nodes[:successor_index], prepared_node, successor_rewired, *state.nodes[successor_index + 1 :])
    predecessor_edge = replace(direct_edge, to_node=prepared_node.id)
    successor_edge = EdgeSpec(
        id=new_edge_id,
        from_node=prepared_node.id,
        to_node=successor.id,
        edge_type="on_success",
        label=None,
    )
    edges = (*state.edges[:direct_edge_index], predecessor_edge, successor_edge, *state.edges[direct_edge_index + 1 :])
    proposed = CompositionState(
        sources=state.sources,
        nodes=nodes,
        edges=edges,
        outputs=state.outputs,
        metadata=state.metadata,
        version=state.version,
        guided_session=state.guided_session,
    )
    canonical_error = _composition_canonical_interpretation_requirement_error(
        proposed,
        tool_name="splice_transform",
    )
    if canonical_error is not None:
        return _failure_result(
            state,
            canonical_error,
            error_code="interpretation_requirements_invalid",
        )
    try:
        reconciled = reconcile_authoritative_reviews(state, proposed)
    except (KeyError, TypeError, ValueError):
        return _failure_result(
            state,
            "Authoritative interpretation-review reconciliation failed. Re-inspect the pipeline and retry.",
            error_code="review_reconciliation_failed",
        )
    canonical_error = _composition_canonical_interpretation_requirement_error(
        reconciled,
        tool_name="splice_transform",
    )
    if canonical_error is not None:
        return _failure_result(
            state,
            canonical_error,
            error_code="interpretation_requirements_invalid",
        )
    review_contract_error = composition_review_contract_error(reconciled)
    if review_contract_error is not None:
        return _failure_result(state, review_contract_error)
    profile_validation = context.catalog.validate_composition_state(reconciled)
    if not profile_validation.validation.is_valid:
        return _failure_result(
            state,
            "Spliced pipeline failed context-aware validation.",
            error_code="splice_validation_failed",
        )
    new_state = replace(reconciled, version=state.version + 1)
    return _mutation_result(
        new_state,
        (predecessor_id, prepared_node.id, successor.id),
        data={
            "already_applied": False,
            "predecessor_id": predecessor_id,
            "successor_id": successor.id,
            "inserted_node_id": prepared_node.id,
            "derived_connection": connection_name,
            "replaced_edge_id": direct_edge.id,
            "new_edge_id": new_edge_id,
        },
    )


def _execute_upsert_edge(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Add or update an edge.

    When the edge targets an output (sink), synchronises the source
    node's connection field so that generate_yaml() produces a
    working pipeline.  Edges to non-output nodes are visual only.
    """
    del context  # unused; signature uniformity with the other handlers.
    validated = cast(_UpsertEdgeArgumentsModel, _validate_mutation_arguments(_UpsertEdgeArgumentsModel, args, "upsert_edge arguments"))
    from_node = validated.from_node
    to_node = validated.to_node
    edge_type = validated.edge_type

    edge = EdgeSpec(
        id=validated.id,
        from_node=from_node,
        to_node=to_node,
        edge_type=edge_type,
        label=validated.label,
    )

    # Admission: the shared lowering matrix decides whether this
    # (component kind, edge type, target kind) combination has any runtime
    # meaning — the same predicate Stage 1 applies to bulk entry paths.
    from_kind = composer_component_kind(from_node, state.sources, state.nodes, state.outputs)
    to_kind = composer_component_kind(to_node, state.sources, state.nodes, state.outputs)
    lowering_error = edge_lowering_error(edge, from_kind=from_kind, to_kind=to_kind)
    if lowering_error is not None:
        return _failure_result(state, lowering_error, error_code="edge_not_lowerable")

    # Slot uniqueness: one sink-routing slot, one edge. A second edge id
    # claiming the same slot would make removal ambiguous and let the graph
    # draw a route the scalar cannot carry.
    if to_kind == "output":
        output_names = {output.name for output in state.outputs}
        for existing in state.edges:
            if existing.id == edge.id or existing.from_node != from_node or existing.to_node not in output_names:
                continue
            if edge_type == "fork":
                conflict = existing.edge_type == "fork" and existing.to_node == to_node
            else:
                conflict = existing.edge_type == edge_type
            if conflict:
                return _failure_result(
                    state,
                    f"Edge '{existing.id}' already expresses the '{edge_type}' sink route of '{from_node}'. "
                    f"Update or remove edge '{existing.id}' instead of adding '{edge.id}'.",
                    error_code="edge_route_conflict",
                )

    # Atomic commit: the visual edge and its scalar mirror derive from one
    # model. Replacing an edge id first releases the route the old edge
    # expressed (unless a legacy duplicate still draws it), then writes the
    # route the new edge expresses — no ordering leaves a stale mirror.
    old_edge = next((candidate for candidate in state.edges if candidate.id == edge.id), None)
    new_state = state.with_edge(edge)
    if (
        old_edge is not None
        and (old_edge.from_node, old_edge.edge_type, old_edge.to_node)
        != (
            edge.from_node,
            edge.edge_type,
            edge.to_node,
        )
        and not _sink_route_still_expressed(new_state, old_edge)
    ):
        new_state = _clear_removed_sink_edge_route(new_state, old_edge)
    new_state = _apply_sink_edge_route(new_state, edge)

    invariant_error = _post_mutation_invariant_error(new_state)
    if invariant_error is not None:
        message, error_code = invariant_error
        return _failure_result(state, message, error_code=error_code)
    return _mutation_result(new_state, (from_node, to_node))


def _execute_remove_node(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Remove a node and its edges."""
    del context  # unused; signature uniformity with the other handlers.
    validated = cast(_RemoveByIdArgumentsModel, _validate_mutation_arguments(_RemoveByIdArgumentsModel, args, "remove_node arguments"))
    node_id = validated.id

    # Collect affected nodes before removal (edges that reference this node)
    affected = {node_id}
    for edge in state.edges:
        if edge.from_node == node_id or edge.to_node == node_id:
            affected.add(edge.from_node)
            affected.add(edge.to_node)

    new_state = state.without_node(node_id)
    if new_state is None:
        return _failure_result(state, f"Node '{node_id}' not found.")

    return _mutation_result(new_state, tuple(sorted(affected)))


def _execute_remove_edge(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Remove an edge."""
    del context  # unused; signature uniformity with the other handlers.
    validated = cast(_RemoveByIdArgumentsModel, _validate_mutation_arguments(_RemoveByIdArgumentsModel, args, "remove_edge arguments"))
    edge_id = validated.id

    # Find the edge to get affected nodes
    edge = next((e for e in state.edges if e.id == edge_id), None)
    if edge is None:
        return _failure_result(state, f"Edge '{edge_id}' not found.")

    affected = (edge.from_node, edge.to_node)
    new_state = state.without_edge(edge_id)
    if new_state is None:
        return _failure_result(state, f"Edge '{edge_id}' not found.")
    if not _sink_route_still_expressed(new_state, edge):
        new_state = _clear_removed_sink_edge_route(new_state, edge)

    return _mutation_result(new_state, affected)


def _execute_set_metadata(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Update pipeline metadata."""
    del context  # unused; signature uniformity with the other handlers.
    validated = cast(_SetMetadataArgumentsModel, _validate_mutation_arguments(_SetMetadataArgumentsModel, args, "set_metadata arguments"))
    patch = validated.patch.model_dump(exclude_none=True)

    new_state = state.with_metadata(patch)
    return _mutation_result(new_state, ())


def _node_routing_option_patch_error(patch: Mapping[str, Any], *, node_type: NodeType) -> str | None:
    """Return guidance when plugin-option patches contain node routing fields."""
    if not (_NODE_ROUTING_OPTION_PATCH_KEYS & patch.keys()):
        return None
    for key in ("on_error", "on_success", "input", "routes", "fork_to"):
        if key not in patch:
            continue
        if key == "on_error":
            if node_type == "gate":
                return (
                    "on_error is a node-level gate error policy, not a plugin option. "
                    "Use upsert_node with on_error as a sibling of options; set it to 'discard' or a declared sink name."
                )
            return (
                "on_error is a node-level routing field, not a plugin option. "
                "Use upsert_edge with edge_type='on_error' when routing failures to an existing sink, "
                "or use upsert_node with on_error as a sibling of options for other routing edits."
            )
        if key == "on_success":
            return (
                "on_success is a node-level routing field, not a plugin option. "
                "Use upsert_edge with edge_type='on_success' when routing success rows to an existing sink, "
                "or use upsert_node with on_success as a sibling of options for other routing edits."
            )
        if key == "input":
            return (
                "input is a node-level connection field, not a plugin option. "
                "Use upsert_node with input as a sibling of options to change the connection this node consumes."
            )
        if key in {"routes", "fork_to"}:
            return (
                f"{key} is a gate-level routing field, not a plugin option. "
                "Use upsert_edge with edge_type='route_true', edge_type='route_false', or edge_type='fork' "
                f"for sink routing, or use upsert_node with {key} as a sibling of options."
            )
    return None


def _execute_patch_node_options(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Apply a merge-patch to a node's plugin options.

    Tier-3 boundary: ``args`` is an LLM-supplied dict.  Validated via the
    Pydantic redaction-bearing model :class:`PatchNodeOptionsArgumentsModel`
    (the single source of truth for the argument schema — supersedes the
    deleted ``_TOOL_REQUIRED_PATHS["patch_node_options"]`` entry in
    ``service.py``, rev-3 N7 / rev-4 M1).

    On :class:`pydantic.ValidationError` the handler re-raises as
    :class:`ToolArgumentError` so the compose loop's ARG_ERROR routing at
    ``service.py:2480`` receives the right exception class.

    Routing-key guard: :func:`_node_routing_option_patch_error` rejects
    routing-field keys in ``patch`` (on_error, on_success, input, routes,
    fork_to).  This is a value-domain check that Pydantic cannot express;
    it runs AFTER Pydantic validation — same discipline as
    ``set_pipeline``'s blob_id/inline_blob mutual-exclusion check.
    """
    try:
        validated = PatchNodeOptionsArgumentsModel.model_validate(args)
    except PydanticValidationError as exc:
        raise ToolArgumentError(
            argument="patch_node_options arguments",
            expected="object conforming to PatchNodeOptionsArgumentsModel",
            actual_type=type(exc).__name__,
        ) from exc
    node_id = validated.node_id
    patch: Mapping[str, Any] = validated.patch
    current = next((n for n in state.nodes if n.id == node_id), None)
    if current is None:
        return _failure_result(state, f"Node '{node_id}' not found.")
    routing_patch_error = _node_routing_option_patch_error(patch, node_type=current.node_type)
    if routing_patch_error is not None:
        return _failure_result(state, routing_patch_error)
    runtime_owned_error = _runtime_owned_llm_option_error(
        current.plugin,
        patch,
        tool_name="patch_node_options",
        component_id=node_id,
    )
    if runtime_owned_error is not None:
        error_code = "interpretation_requirements_invalid" if INTERPRETATION_REQUIREMENTS_KEY in patch else None
        return _failure_result(state, f"Node '{node_id}': {runtime_owned_error}", error_code=error_code)
    patch = _canonicalize_authored_interpretation_requirements(
        patch,
        component_id=node_id,
        existing_options=current.options,
    )
    new_options: Mapping[str, Any] = _apply_merge_patch(current.options, dict(patch))
    new_options = _options_with_default_llm_reviews(
        node_id=node_id,
        plugin=current.plugin,
        options=new_options,
        existing_options=current.options,
    )
    canonical_error = _canonical_interpretation_requirement_error(
        new_options,
        tool_name="patch_node_options",
    )
    if canonical_error is not None:
        return _failure_result(
            state,
            f"Node '{node_id}': {canonical_error}",
            error_code="interpretation_requirements_invalid",
        )
    credential_error = _credential_wiring_contract_failure(
        state,
        component_id=node_id,
        component_type="node",
        plugin_type="transform" if current.plugin is not None else None,
        plugin_name=current.plugin,
        options=new_options,
    )
    if credential_error is not None:
        return credential_error

    if current.node_type in ("transform", "aggregation", "collector") and current.plugin is not None:
        prevalidation_error = _prevalidate_transform_for_context(context, current.plugin, new_options)
        if prevalidation_error is not None:
            return _failure_result(state, prevalidation_error)

        # Operator-profiled nodes carry their private provider config (retry
        # budget / provider binding) in the profile, injected only at lowering;
        # the prevalidation above already validated the LOWERED executable. The
        # raw provider-config policy would false-positive on the absent private
        # retry budget (see set_pipeline in sessions.py for the full rationale).
        if "profile" not in new_options:
            provider_policy_error = _validate_transform_provider_config_policy(new_options, plugin=current.plugin)
            if provider_policy_error is not None:
                return _failure_result(state, f"Node '{node_id}': {provider_policy_error}")

        # S2: confine nested provider_config persist_directory (RAG retrieval).
        # A merge-patch can introduce an escaping path just as upsert_node can.
        provider_path_error = _validate_transform_provider_config_path(new_options, context.data_dir, session_id=context.session_id)
        if provider_path_error is not None:
            return _failure_result(state, f"Node '{node_id}': {provider_path_error}")

    new_node = replace(current, options=new_options)
    # Third canonical mutation boundary: a patch that would break a queue's
    # intrinsic contract (unknown option, non-string description) is rejected
    # by the single shared guard before with_node, leaving state atomically
    # unchanged. Returns None for every non-queue node, so this is a no-op for
    # transform/gate/aggregation/coalesce patches.
    queue_contract_error = queue_node_contract_error(new_node)
    if queue_contract_error is not None:
        return _failure_result(state, queue_contract_error)
    proposed_state = state.with_node(new_node)
    invariant_error = _post_mutation_invariant_error(proposed_state)
    if invariant_error is not None:
        message, error_code = invariant_error
        return _failure_result(state, message, error_code=error_code)
    try:
        new_state = reconcile_authoritative_reviews(state, proposed_state)
    except (KeyError, TypeError, ValueError):
        return _failure_result(
            state,
            "Authoritative interpretation-review reconciliation failed. Re-inspect the pipeline and retry.",
            error_code="review_reconciliation_failed",
        )
    review_contract_error = composition_review_contract_error(new_state)
    if review_contract_error is not None:
        return _failure_result(state, review_contract_error)
    return _mutation_result(new_state, (node_id,))


def _handle_patch_node_options(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    # _execute_patch_node_options validates arguments via the Pydantic model
    # and re-raises as ToolArgumentError; PydanticValidationError cannot
    # escape into this caller. Re-validation on the success branch is
    # deterministic by the same model.
    result = _execute_patch_node_options(arguments, state, context)
    if not result.success:
        return result
    validated = PatchNodeOptionsArgumentsModel.model_validate(arguments)
    node_id = validated.node_id
    node = next((n for n in result.updated_state.nodes if n.id == node_id), None)
    # Offensive programming: _execute_patch_node_options succeeded above, so
    # the node it just upserted MUST be on the post-mutation state. Absence
    # here would be a bug in state.with_node, not a runtime condition.
    if node is None:
        raise AssertionError(
            f"_execute_patch_node_options succeeded for node '{node_id}' but the post-mutation state does not contain it — invariant violation."
        )
    return _attach_post_call_hints(
        result,
        context.catalog,
        plugin_type="transform",
        tool_name="patch_node_options",
        plugin_name=node.plugin,
        config_snapshot=node.options,
    )


_PATCH_NODE_OPTIONS_DECLARATION = ToolDeclaration(
    name="patch_node_options",
    handler=_handle_patch_node_options,
    kind=ToolKind.MUTATION,
    description="Apply a shallow merge-patch to a node's options. Use this for option-only edits. "
    "Keys in the patch overwrite existing keys. "
    "Keys set to null are deleted. Missing keys are unchanged. "
    "Do not use this for node routing fields such as on_success/on_error/input/routes; "
    "use upsert_edge or upsert_node for routing edits. Gate on_error is node-level and must use upsert_node.",
    json_schema={
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "ID of the node to patch.",
            },
            "patch": {
                "type": "object",
                "description": (
                    "Merge-patch to apply to plugin options only. "
                    "Node-level routing fields such as on_success, on_error, input, routes, "
                    "and fork_to are siblings of options; edit them with upsert_edge or upsert_node. "
                    "For a gate, edit on_error only with upsert_node. "
                    "A patched schema: block declares what ARRIVES at the node, never its transformed "
                    "result; to change what arrives, declare the type on the SOURCE schema "
                    "(patch_source_options) or insert a type_coerce upstream."
                ),
            },
        },
        "required": ["node_id", "patch"],
        "additionalProperties": False,
    },
    augments_on_failure=True,
)


_SPLICE_CONNECTION_ATTEMPTS: Final[int] = 32
_SPLICE_CONNECTION_MAX_LENGTH: Final[int] = 64
_SPLICE_EDGE_ID_MAX_LENGTH: Final[int] = 160


def _prepare_transform_candidate(
    *,
    state: CompositionState,
    context: ToolContext,
    tool_name: str,
    node_id: str,
    node_type: NodeType,
    plugin: str | None,
    input_name: str,
    on_success: str | None,
    on_error: str | None,
    options: Mapping[str, Any],
    existing_options: Mapping[str, Any] | None = None,
    trigger: Mapping[str, Any] | None = None,
    output_mode: str | None = None,
    expected_output_count: int | None = None,
    description: str | None = None,
) -> NodeSpec | ToolResult:
    """Validate and prepare one transform candidate without mutating state."""
    if plugin is None:
        return _failure_result(state, f"Node '{node_id}': transform plugin is required.")
    runtime_owned_error = _runtime_owned_llm_option_error(
        plugin,
        options,
        tool_name=tool_name,
        component_id=node_id,
    )
    if runtime_owned_error is not None:
        error_code = "interpretation_requirements_invalid" if INTERPRETATION_REQUIREMENTS_KEY in options else None
        return _failure_result(state, f"Node '{node_id}': {runtime_owned_error}", error_code=error_code)
    options = _canonicalize_authored_interpretation_requirements(
        options,
        component_id=node_id,
        existing_options=existing_options,
    )
    review_options = _options_with_default_llm_reviews(
        node_id=node_id,
        plugin=plugin,
        options=options,
        existing_options=existing_options,
    )
    canonical_error = _canonical_interpretation_requirement_error(
        review_options,
        tool_name=tool_name,
    )
    if canonical_error is not None:
        return _failure_result(
            state,
            f"Node '{node_id}': {canonical_error}",
            error_code="interpretation_requirements_invalid",
        )
    credential_error = _credential_wiring_contract_failure(
        state,
        component_id=node_id,
        component_type="node",
        plugin_type="transform",
        plugin_name=plugin,
        options=review_options,
    )
    if credential_error is not None:
        return credential_error
    plugin_error = _validate_plugin_name(context, "transform", plugin)
    if plugin_error is not None:
        return _plugin_policy_failure(state, plugin_error)
    batch_placement_error = _batch_aware_placement_error(node_id, node_type, plugin, output_mode)
    if batch_placement_error is not None:
        return _failure_result(state, batch_placement_error)
    batch_required_error = _batch_aware_required_input_fields_error(node_id, plugin, review_options)
    if batch_required_error is not None:
        return _failure_result(state, batch_required_error)

    prevalidation_error = _prevalidate_transform_for_context(context, plugin, review_options)
    if prevalidation_error is not None:
        return _failure_result(state, prevalidation_error)
    # Operator-profiled nodes carry their private provider config (retry budget /
    # provider binding) in the profile, injected only at lowering; the
    # prevalidation above already validated the LOWERED executable. The raw
    # provider-config policy would false-positive on the absent private retry
    # budget (see set_pipeline in sessions.py for the full rationale).
    if "profile" not in review_options:
        provider_policy_error = _validate_transform_provider_config_policy(review_options, plugin=plugin)
        if provider_policy_error is not None:
            return _failure_result(state, f"Node '{node_id}': {provider_policy_error}")
    provider_path_error = _validate_transform_provider_config_path(review_options, context.data_dir, session_id=context.session_id)
    if provider_path_error is not None:
        return _failure_result(state, f"Node '{node_id}': {provider_path_error}")
    if node_type == "aggregation":
        trigger_error = _validate_aggregation_trigger(dict(trigger) if trigger is not None else None)
        if trigger_error is not None:
            return _failure_result(state, f"Node '{node_id}': {trigger_error}")

    return NodeSpec(
        id=node_id,
        node_type=node_type,
        plugin=plugin,
        input=input_name,
        on_success=on_success,
        on_error=on_error or "discard",
        options=review_options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        trigger=trigger,
        output_mode=output_mode,
        expected_output_count=expected_output_count,
        description=description,
    )


def _handle_splice_transform(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    result = _execute_splice_transform(arguments, state, context)
    if not result.success:
        return result
    validated = SpliceTransformArgumentsModel.model_validate(arguments)
    return _attach_post_call_hints(
        result,
        context.catalog,
        plugin_type="transform",
        tool_name="splice_transform",
        plugin_name=validated.node.plugin,
        config_snapshot=validated.node.options,
    )


_SPLICE_TRANSFORM_DECLARATION = ToolDeclaration(
    name="splice_transform",
    handler=_handle_splice_transform,
    kind=ToolKind.MUTATION,
    description=(
        "Insert one transform between a predecessor and successor on an existing direct linear on_success path. "
        "Use this for insert/between/before/after edits; the server derives input, on_success, connection, and edge IDs."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "predecessor_id": {
                "type": "string",
                "description": "Existing source or transform immediately before the insertion point.",
            },
            "successor_id": {
                "type": "string",
                "description": "Existing transform immediately after the insertion point.",
            },
            "node": {
                "type": "object",
                "properties": {
                    "id": {
                        **TypeAdapter(RuntimeNodeName).json_schema(),
                        "description": "Unique ID for the inserted transform.",
                    },
                    "plugin": {"type": "string", "description": "Transform plugin name."},
                    "options": {"type": "object", "description": "Plugin-specific authored options."},
                    "on_error": {
                        "type": ["string", "null"],
                        "description": "Optional error route; defaults to discard.",
                    },
                    "description": {
                        "type": ["string", "null"],
                        "description": _STEP_DESCRIPTION_DESCRIPTION,
                    },
                },
                "required": ["id", "plugin", "options"],
                "additionalProperties": False,
            },
        },
        "required": ["predecessor_id", "successor_id", "node"],
        "additionalProperties": False,
    },
    augments_on_failure=True,
)


TOOLS_IN_MODULE: tuple[ToolDeclaration, ...] = (
    _LIST_TRANSFORMS_DECLARATION,
    _LIST_SINKS_DECLARATION,
    _UPSERT_NODE_DECLARATION,
    _SPLICE_TRANSFORM_DECLARATION,
    _UPSERT_EDGE_DECLARATION,
    _REMOVE_NODE_DECLARATION,
    _REMOVE_EDGE_DECLARATION,
    _SET_METADATA_DECLARATION,
    _PATCH_NODE_OPTIONS_DECLARATION,
)
"""Every tool declared in this module, in stable order.

``_dispatch.py`` aggregates this tuple alongside every other plane's
TOOLS_IN_MODULE to build the registered-tool universe."""
