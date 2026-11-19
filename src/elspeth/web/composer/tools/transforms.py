"""Composer transforms plane — node, edge, and metadata graph-mutation handlers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from hashlib import sha256
from typing import Annotated, Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictFloat, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.enums import OutputMode
from elspeth.core.config import RuntimeNodeName
from elspeth.web.composer.inventory_response_contracts import PLUGIN_INVENTORY_RESPONSE_CONTRACT
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import (
    ChangeSubstringArgumentsModel,
    InspectPromptTargetsArgumentsModel,
    PatchNodeOptionsArgumentsModel,
    SpliceTransformArgumentsModel,
    _JsonInteger,
    _NodeTriggerModel,
    _OmittableString,
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
    ValidationEntry,
    _batch_aware_placement_error,
    _batch_aware_required_input_fields_error,
    _validate_gate_expression,
    _validate_gate_route_parity,
    composer_component_kind,
    edge_lowering_error,
    queue_node_contract_error,
)
from elspeth.web.composer.tools._common import (
    _LLM_OPTIONS_OWNERSHIP_SCHEMA_NOTE,
    _STEP_DESCRIPTION_DESCRIPTION,
    EmptyToolArgumentsModel,
    FailureRouteChoiceError,
    ToolContext,
    ToolResult,
    _apply_merge_patch,
    _attach_post_call_hints,
    _canonical_interpretation_requirement_error,
    _canonicalize_authored_interpretation_requirements,
    _composition_canonical_interpretation_requirement_error,
    _discovery_result,
    _echoed_metadata_note,
    _failure_result,
    _mutation_result,
    _normalize_echoed_interpretation_requirements,
    _options_with_default_llm_reviews,
    _path_policy_refusal_message,
    _plugin_selection_violation,
    _post_mutation_invariant_error,
    _prevalidate_transform_for_context,
    _prohibited_section,
    _reserved_connection_names,
    _row_union_node_contract_error,
    _runtime_owned_llm_option_error,
    _saved_graph_credential_wiring_contract_failure,
    _saved_graph_plugin_policy_failure,
    _saved_graph_rejection_result,
    _validate_aggregation_trigger,
    _validate_mutation_arguments,
    _validate_transform_provider_config_path,
    _validate_transform_provider_config_policy,
    resolve_failure_route_choice,
    review_reconciliation_failure_message,
)
from elspeth.web.composer.tools._naming_disclosure import _disclose_node_name_constraints
from elspeth.web.composer.tools.blobs import (
    PROMPT_UNCHANGED_NODE_IDS_KEY,
    PROMPT_UNCHANGED_NOTE,
    PROMPT_UNCHANGED_NOTE_KEY,
    _file_inject_node_option_error,
    _llm_authored_inline_prompt_surface_error,
    _PromptReplacement,
    _structured_prompt_template_refusal,
    _whole_prompt_text_replacement,
    _written_options_for_prompt_replacement,
    _written_options_keeping_an_unchanged_prompt,
)
from elspeth.web.composer.tools.declarations import (
    EffectDomain,
    ToolDeclaration,
    ToolEffects,
    ToolKind,
)
from elspeth.web.composer.tools.prompt_target_responses import (
    PROMPT_TARGETS_RESPONSE_CONTRACT,
    PromptTargetMetadata,
    PromptTargetsResponse,
)
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material_for_tool
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_TEMPLATE_PARTS_KEY,
    VAGUE_TERM_QUERY_OVERRIDE_PREFIX,
    approved_prompt_artifact_hash_from_options,
    composition_review_contract_error,
    reconcile_authoritative_reviews,
    serialize_authoring_review_options,
)

_NODE_ROUTING_OPTION_PATCH_KEYS: Final[frozenset[str]] = frozenset({"input", "on_success", "on_error", "routes", "fork_to"})


class _UpsertNodeTriggerModel(_NodeTriggerModel):
    """Admit the positive trigger bounds disclosed by incremental authoring.

    Full-state admission retains its separate shape-only trigger contract;
    TriggerConfig applies these domain bounds during semantic validation.
    """

    count: _JsonInteger | None = Field(default=None, ge=1)
    timeout_seconds: StrictFloat | None = Field(default=None, gt=0)


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
    trigger: _UpsertNodeTriggerModel | None = None
    output_mode: Literal["passthrough", "transform"] | None = None
    expected_output_count: _JsonInteger | None = None
    timeout_seconds: _StrictTimeoutSeconds | None = None
    description: str | None = None
    scope_name: str | None = None
    scope_opener: str | None = None
    scope_policy: Literal["require_all", "best_effort"] | None = None

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
    name: _OmittableString = None
    description: _OmittableString = None

    model_config = ConfigDict(extra="forbid")


class _SetMetadataArgumentsModel(BaseModel):
    patch: _SetMetadataPatchModel

    model_config = ConfigDict(extra="forbid")


def _handle_list_transforms(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    _validate_mutation_arguments(EmptyToolArgumentsModel, arguments, "list_transforms arguments")
    return _discovery_result(
        state,
        {
            "available": context.catalog.list_transforms(),
            "prohibited": _prohibited_section(context.catalog.list_prohibited_transforms()),
        },
    )


_LIST_TRANSFORMS_DECLARATION = ToolDeclaration(
    name="list_transforms",
    response_contract=PLUGIN_INVENTORY_RESPONSE_CONTRACT,
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
    _validate_mutation_arguments(EmptyToolArgumentsModel, arguments, "list_sinks arguments")
    return _discovery_result(
        state,
        {
            "available": context.catalog.list_sinks(),
            "prohibited": _prohibited_section(context.catalog.list_prohibited_sinks()),
        },
    )


_LIST_SINKS_DECLARATION = ToolDeclaration(
    name="list_sinks",
    response_contract=PLUGIN_INVENTORY_RESPONSE_CONTRACT,
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
            "enum": ["transform", "gate", "aggregation", "coalesce", "row_union", "queue", "collector"],
            "description": (
                "Node kind. Choose transform for a per-row plugin, aggregation for a batch-aware plugin with optional triggers, "
                "collector for a batch-aware plugin closing a declared EXPAND scope, gate for conditional routing or fan-out, "
                "queue for explicit fan-in, coalesce to merge branch fields, or row_union to release original branch rows "
                "without merging. Supply only fields supported by the chosen kind."
            ),
        },
        "plugin": {
            "type": ["string", "null"],
            "description": (
                "Plugin name. Required for transform/aggregation/collector. Explicitly supply it when replacing an existing "
                "configured plugin node, including routing-only edits. Null for gate/coalesce/row_union/queue."
            ),
        },
        "input": {
            "type": "string",
            "description": (
                "Connection-name string this node CONSUMES: must equal an upstream's on_success "
                "(or routes value or fork_to connection), NOT the upstream node's id — connections match "
                "by string, not by graph topology. For queue use its shared connection name; for coalesce "
                "and row_union use the first branch connection as the required placeholder, while branches "
                "provide the actual consuming bindings."
            ),
        },
        "on_success": {
            "type": ["string", "null"],
            "description": (
                "Output connection, consumed by a downstream input/sink_name (matched by string). "
                "Required for transform/collector/row_union. Aggregation may omit it to publish under its own node id. "
                "Gates use routes/fork_to; queues omit it. A row_union MUST publish to a processing connection, never "
                "directly to a sink. A coalesce normally publishes under its own node id; its "
                "optional on_success may name only a sink."
            ),
        },
        "on_error": {
            "type": ["string", "null"],
            "minLength": 1,
            "description": (
                "Node-level error policy (transform/aggregation/gate): 'discard' or an eligible named sink. "
                "New transforms/aggregations require an explicit non-empty choice; omission on an update preserves "
                "the same node kind's existing choice. Null/empty is invalid for transforms/aggregations. "
                "For a gate it covers row expression-evaluation errors and is authored here, never as an "
                "edge; omit it to preserve fail-fast behavior. A failed aggregation batch applies this policy to every buffered row; discard records quarantine outcomes."
            ),
        },
        "options": {
            "type": "object",
            "description": (
                "Complete author-owned plugin config for transform, aggregation, and collector nodes. Explicitly supply it "
                "when replacing an existing configured plugin node, even for routing-only edits; omitted options are not merged. "
                "A queue permits only its optional "
                "description. Other structural kinds do not take plugin configuration. The schema: block declares what "
                "ARRIVES for consumed fields and the OUTPUT type for fields the node creates. "
                "Convert arriving values at the source or with an upstream type_coerce; an output declaration checks values but does not convert them. "
                "An int satisfies float. Row-column options use the field name carried by upstream rows, normally a normalized header or source field_mapping target."
                + _LLM_OPTIONS_OWNERSHIP_SCHEMA_NOTE
            ),
        },
        "condition": {"type": ["string", "null"], "description": "Boolean expression (gate only). Evaluated per row."},
        "routes": {
            "type": ["object", "null"],
            "additionalProperties": {"type": "string"},
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
            "description": "Field merge strategy (coalesce only). Omitting it means 'union', the runtime default. A shared union field needs one compatible actual type on every branch; any counts as a type.",
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
            "description": "Aggregation output mode (aggregation only). Defaults to 'transform'. Passthrough keeps the same row tokens and admits only a batch plugin that flushes exactly one row per buffered row; check its catalogue aggregation_output_modes. Otherwise use transform.",
        },
        "expected_output_count": {
            "type": ["integer", "null"],
            "description": (
                "Expected aggregation output row count for output_mode='transform' (the default); omit for 'passthrough'. "
                "Also omit when output count depends on group_by distinct values."
            ),
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
        "upsert_node replaces the node: when updating an existing configured transform/aggregation/collector, "
        "explicitly supply plugin and complete author-owned options even for routing-only edits. "
        "Read get_pipeline_state(component='set_pipeline_arguments') for the authoring payload. "
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
        "Omit fields that don't apply to your node_type. "
        "For an llm node that has a structured prompt, options with prompt_template text and prompt_template_parts: null "
        "replace the whole prompt; `superseded_review_ids` names the old prompt's vague-term reviews that were removed. "
        "To keep that prompt, send the node whole and unedited as get_pipeline_state(component='set_pipeline_arguments') "
        "returns it; changed prompt_template text without prompt_template_parts is refused (prompt_template_parts_required)."
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
                "description": (
                    "Connection kind. Use on_success for a direct success relationship; on_error only from a transform or "
                    "aggregation to a sink; route_true or route_false for gate routes; fork for a gate fan-out relationship. "
                    "Gate expression errors use the gate node's on_error field instead. Edges targeting sinks also update "
                    "the producer's corresponding runtime routing field; other edges are display relationships and do not "
                    "replace node connection fields."
                ),
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
                "additionalProperties": False,
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
    saved-graph refusal and leaves the exact prior state/version unchanged —
    ``with_node`` is never reached, so the mutation is atomic. A canonical
    queue succeeds and persists even when the resulting pipeline is
    incomplete (missing producers/downstream); completeness is validation
    telemetry surfaced on the returned ``ToolResult.validation``, not a
    mutation rejection.
    """
    fork_to: tuple[str, ...] | None = tuple(validated.fork_to) if validated.fork_to is not None else None
    branches: CoalesceBranches | None = None
    if validated.branches is not None:
        # pydantic validated branches as exactly list[str] | dict[str, str]; dispatch on
        # the concrete constructed type rather than re-checking shape via an ABC.
        branches = dict(validated.branches) if type(validated.branches) is dict else tuple(validated.branches)
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
        trigger=validated.trigger.model_dump(exclude_unset=True) if validated.trigger is not None else None,
        output_mode=validated.output_mode,
        expected_output_count=validated.expected_output_count,
        timeout_seconds=validated.timeout_seconds,
        description=validated.description,
        scope_name=validated.scope_name,
        scope_opener=validated.scope_opener,
        scope_policy=validated.scope_policy,
    )
    contract_error = queue_node_contract_error(node)
    if contract_error is not None:
        return _saved_graph_rejection_result(
            state, contract_error, error_code="queue_config_invalid", rejected_component=f"node:{validated.id}"
        )

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
    validated = _validate_mutation_arguments(_UpsertNodeArgumentsModel, args, "upsert_node arguments")
    node_ref = f"node:{validated.id}"
    if validated.node_type == "aggregation" and (validated.output_mode is None or validated.output_mode in OutputMode):
        mode = OutputMode.TRANSFORM if validated.output_mode is None else OutputMode(validated.output_mode)
        count_error = mode.expected_output_count_error(validated.expected_output_count)
        if count_error is not None:
            return _saved_graph_rejection_result(
                state,
                count_error,
                error_code="aggregation_expected_output_count_mode_invalid",
                rejected_component=node_ref,
            )
    node_id = validated.id
    node_type = validated.node_type
    plugin = validated.plugin
    node_options: Mapping[str, Any] = validated.options
    existing_node = next((node for node in state.nodes if node.id == node_id), None)
    if (
        existing_node is not None
        and existing_node.plugin is not None
        and existing_node.node_type in ("transform", "aggregation", "collector")
        and node_type in ("transform", "aggregation", "collector")
        and not {"plugin", "options"} <= validated.model_fields_set
    ):
        return _saved_graph_rejection_result(
            state,
            "upsert_node replaces the node, including its plugin configuration. For an existing configured "
            "transform, aggregation, or collector, explicitly supply plugin and complete author-owned options, "
            "even for routing-only edits. Read get_pipeline_state(component='set_pipeline_arguments') for the "
            "authoring payload, then resubmit the complete node.",
            error_code="argument_error",
            rejected_component=node_ref,
        )
    prior_node = existing_node if existing_node is not None and existing_node.node_type == node_type else None
    on_error = validated.on_error
    if node_type in ("transform", "aggregation"):
        try:
            on_error = resolve_failure_route_choice(
                on_error,
                supplied="on_error" in validated.model_fields_set,
                field="on_error",
                previous=prior_node.on_error if prior_node is not None else None,
            )
        except FailureRouteChoiceError as exc:
            return _saved_graph_rejection_result(state, str(exc), error_code="failure_route_required", rejected_component=node_ref)
    elif node_type == "gate" and "on_error" not in validated.model_fields_set and prior_node is not None:
        on_error = prior_node.on_error
    # Echo tolerance (elspeth-c67fbbbd83): requirement rows echoed verbatim
    # from the stored node reduce to author shells; the reconciliation pass
    # below restores the server rows. Non-matching values still reject.
    node_options, requirement_echo = _normalize_echoed_interpretation_requirements(
        node_options,
        stored_options=existing_node.options if existing_node is not None else None,
    )
    runtime_owned_error = _runtime_owned_llm_option_error(
        plugin,
        node_options,
        tool_name="upsert_node",
        component_id=node_id,
    )
    if runtime_owned_error is not None:
        error_code = "interpretation_requirements_invalid" if INTERPRETATION_REQUIREMENTS_KEY in node_options else "plugin_options_invalid"
        return _saved_graph_rejection_result(
            state, f"Node '{node_id}': {runtime_owned_error}", error_code=error_code, rejected_component=node_ref
        )
    node_options = _canonicalize_authored_interpretation_requirements(
        node_options,
        component_id=node_id,
        existing_options=existing_node.options if existing_node is not None else None,
    )
    # prompt_template as text with prompt_template_parts: null REPLACES the
    # stored prompt (see _whole_prompt_text_replacement). Every refusal below
    # still returns the untouched ``state``, and reviews reconcile against it.
    replacement: _PromptReplacement | None = None
    prompt_unchanged_node_ids: tuple[str, ...] = ()
    if plugin == "llm" and existing_node is not None and existing_node.plugin == "llm":
        # The stored prompt's own text replaces nothing, whatever the parts key says.
        kept_options = _written_options_keeping_an_unchanged_prompt(existing_node.options, node_options, merge_patch=False)
        if kept_options is not None:
            node_options = kept_options
            prompt_unchanged_node_ids = (node_id,)
        replacement = _whole_prompt_text_replacement(existing_node.options, node_options)
    if replacement is not None:
        # A stored row that is already non-canonical is refused first, so the
        # replacement can never launder a malformed row away.
        stored_error = _composition_canonical_interpretation_requirement_error(state, tool_name="upsert_node")
        if stored_error is not None:
            return _saved_graph_rejection_result(
                state, stored_error, error_code="interpretation_requirements_invalid", rejected_component=node_ref
            )
        node_options = _written_options_for_prompt_replacement(node_options, replacement)
    review_options = _options_with_default_llm_reviews(
        node_id=node_id,
        plugin=plugin,
        options=node_options,
        existing_options=existing_node.options if existing_node is not None else None,
    )
    if plugin == "llm":
        structured_error = _structured_prompt_template_refusal(
            node_id,
            replacement.base_options if replacement is not None else existing_node.options if existing_node is not None else {},
            review_options,
        )
        if structured_error is not None:
            return _saved_graph_rejection_result(
                state, structured_error, error_code="prompt_template_parts_required", rejected_component=node_ref
            )
    canonical_error = _canonical_interpretation_requirement_error(
        review_options,
        tool_name="upsert_node",
    )
    if canonical_error is not None:
        return _saved_graph_rejection_result(
            state,
            f"Node '{node_id}': {canonical_error}",
            error_code="interpretation_requirements_invalid",
            rejected_component=node_ref,
        )
    if node_type == "queue":
        # Canonical invariant B applies to structural queues too; the queue
        # contract then decides whether those canonical options are permitted.
        return _execute_upsert_queue_node(
            validated.model_copy(update={"options": dict(review_options)}),
            state,
        )
    credential_error = _saved_graph_credential_wiring_contract_failure(
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
        plugin_error = _plugin_selection_violation(context, "transform", plugin)
        if plugin_error is not None:
            return _saved_graph_plugin_policy_failure(state, plugin_error, component=None, rejected_component=node_ref)
        context.catalog.get_schema("transform", plugin)

        batch_placement_error = _batch_aware_placement_error(
            node_id,
            node_type,
            plugin,
            validated.output_mode,
            validated.trigger.model_dump(exclude_unset=True) if validated.trigger is not None else None,
        )
        if batch_placement_error is not None:
            return _saved_graph_rejection_result(
                state, batch_placement_error, error_code="batch_transform_misplaced", rejected_component=node_ref
            )

        batch_required_error = _batch_aware_required_input_fields_error(node_id, plugin, node_options)
        if batch_required_error is not None:
            return _saved_graph_rejection_result(
                state, batch_required_error, error_code="batch_required_fields_invalid", rejected_component=node_ref
            )

        # Before prevalidation, which withholds a top-level inline_content marker
        # as a deferred value: an LLM-authored blob in an llm prompt surface or
        # model refuses with the wire_blob_inline_ref text on every path.
        prompt_surface_error = _llm_authored_inline_prompt_surface_error(
            context,
            tool_name="upsert_node",
            node_id=node_id,
            plugin=plugin,
            options=review_options,
        )
        if prompt_surface_error is not None:
            return _saved_graph_rejection_result(
                state, prompt_surface_error, error_code="llm_authored_inline_blob_content", rejected_component=node_ref
            )
        # file_inject content is a user-uploaded file on every authoring path.
        file_inject_error = _file_inject_node_option_error(
            context,
            tool_name="upsert_node",
            node_id=node_id,
            plugin=plugin,
            options=review_options,
        )
        if file_inject_error is not None:
            return _saved_graph_rejection_result(
                state, file_inject_error, error_code="file_inject_content_not_uploaded", rejected_component=node_ref
            )

        prevalidation_error = _prevalidate_transform_for_context(context, plugin, review_options)
        if prevalidation_error is not None:
            return _saved_graph_rejection_result(
                state,
                prevalidation_error[0],
                error_code=prevalidation_error[1],
                rejected_component=node_ref,
                plugin_identity=("transform", plugin),
            )

        # Operator-profiled nodes carry their private provider config (retry
        # budget / provider binding) in the profile, injected only at lowering;
        # ``_prevalidate_transform_for_context`` above already validated the
        # LOWERED executable. Running the raw provider-config policy on the
        # authored options would false-positive on the absent private retry
        # budget (see the fuller rationale at set_pipeline in sessions.py).
        if "profile" not in review_options:
            provider_policy_error = _validate_transform_provider_config_policy(review_options, plugin=plugin)
            if provider_policy_error is not None:
                return _saved_graph_rejection_result(
                    state, f"Node '{node_id}': {provider_policy_error}", error_code="llm_retry_budget_policy", rejected_component=node_ref
                )

        provider_path_error = _validate_transform_provider_config_path(
            review_options,
            context.data_dir,
            session_id=context.session_id,
            require_data_dir=context.require_data_dir_for_paths,
            mcp_root_authority=context.mcp_root_authority,
        )
        if provider_path_error is not None:
            return _saved_graph_rejection_result(
                state,
                _path_policy_refusal_message(
                    context,
                    "Node options.provider_config.persist_directory is outside this session's permitted locations. Use an authorized session-local directory.",
                ),
                error_code="provider_config_path_policy_invalid",
                rejected_component=node_ref,
            )

    condition = validated.condition
    if node_type == "gate" and condition is not None:
        expr_error = _validate_gate_expression(condition)
        if expr_error is not None:
            return _saved_graph_rejection_result(
                state, f"Node '{node_id}': {expr_error}", error_code="gate_condition_invalid", rejected_component=node_ref
            )
        parity_error = _validate_gate_route_parity(condition, validated.routes)
        if parity_error is not None:
            return _saved_graph_rejection_result(
                state, f"Node '{node_id}': {parity_error}", error_code="gate_route_labels_mismatch", rejected_component=node_ref
            )
    if node_type == "aggregation":
        trigger_error = _validate_aggregation_trigger(
            validated.trigger.model_dump(exclude_unset=True) if validated.trigger is not None else None
        )
        if trigger_error is not None:
            return _saved_graph_rejection_result(
                state, f"Node '{node_id}': {trigger_error}", error_code="aggregation_trigger_invalid", rejected_component=node_ref
            )

    fork_to: tuple[str, ...] | None = tuple(validated.fork_to) if validated.fork_to is not None else None

    branches: CoalesceBranches | None = None
    if validated.branches is not None:
        # pydantic validated branches as exactly list[str] | dict[str, str]; dispatch on
        # the concrete constructed type rather than re-checking shape via an ABC.
        branches = dict(validated.branches) if type(validated.branches) is dict else tuple(validated.branches)

    node = NodeSpec(
        id=node_id,
        node_type=node_type,
        plugin=plugin,
        input=validated.input,
        on_success=validated.on_success,
        on_error=on_error,
        options=review_options,
        condition=validated.condition,
        routes=validated.routes,
        fork_to=fork_to,
        branches=branches,
        policy=validated.policy,
        merge=validated.merge,
        trigger=validated.trigger.model_dump(exclude_unset=True) if validated.trigger is not None else None,
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
        return _saved_graph_rejection_result(state, message, error_code=error_code, rejected_component=node_ref)

    proposed_state = state.with_node(node)
    # Scalar routes committed here are the runtime authority; any visual edge
    # that mirrored the node's previous sink routes must follow in the same
    # commit or the graph keeps drawing the old route (elspeth-372e18e365).
    proposed_state = _reconcile_node_sink_mirror_edges(proposed_state, node)
    invariant_error = _post_mutation_invariant_error(proposed_state)
    if invariant_error is not None:
        message, error_code = invariant_error
        return _saved_graph_rejection_result(state, message, error_code=error_code, rejected_component=None)
    try:
        new_state = reconcile_authoritative_reviews(state, proposed_state)
    except (KeyError, TypeError, ValueError) as exc:
        return _saved_graph_rejection_result(
            state,
            review_reconciliation_failure_message(exc, retry_hint="Re-inspect the pipeline and retry."),
            error_code="review_reconciliation_failed",
            rejected_component=None,
        )
    review_contract_error = composition_review_contract_error(new_state)
    if review_contract_error is not None:
        return _saved_graph_rejection_result(
            state,
            review_contract_error,
            error_code=(
                "vague_term_query_override"
                if review_contract_error.startswith(VAGUE_TERM_QUERY_OVERRIDE_PREFIX)
                else "interpretation_review_contract_unsatisfied"
            ),
            rejected_component=None,
        )

    # Affected: the node itself plus nodes with edges referencing it
    affected = {node_id}
    for edge in new_state.edges:
        if edge.from_node == node_id or edge.to_node == node_id:
            affected.add(edge.from_node)
            affected.add(edge.to_node)

    echo_note = _echoed_metadata_note(requirement_echo=requirement_echo)
    data: dict[str, JsonValue] = {}
    if echo_note is not None:
        data["server_owned_metadata_note"] = echo_note
    if replacement is not None and replacement.superseded_review_ids:
        # The replaced prompt's vague-term reviews, named so the model can tell
        # the user which definitions no longer apply.
        data["superseded_review_ids"] = list(replacement.superseded_review_ids)
    if prompt_unchanged_node_ids:
        # The write carried the stored prompt's own text without its parts, so
        # the stored prompt structure and reviews were kept.
        data[PROMPT_UNCHANGED_NODE_IDS_KEY] = list(prompt_unchanged_node_ids)
        data[PROMPT_UNCHANGED_NOTE_KEY] = PROMPT_UNCHANGED_NOTE
    return _mutation_result(new_state, tuple(sorted(affected)), data=data or None)


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
        "on_error": node.on_error,
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
        if state.edges:
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
    if not state.edges:
        return None
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
    validated = _validate_mutation_arguments(SpliceTransformArgumentsModel, args, "splice_transform arguments")
    predecessor_id = validated.predecessor_id
    successor_id = validated.successor_id
    node_args = validated.node
    node_ref = f"node:{node_args.id}"
    if predecessor_id == successor_id or node_args.id in {predecessor_id, successor_id}:
        return _saved_graph_rejection_result(
            state,
            "Splice predecessor, successor, and inserted node IDs must be distinct.",
            error_code="splice_ids_not_distinct",
            rejected_component=None,
        )
    if len({node.id for node in state.nodes}) != len(state.nodes):
        return _saved_graph_rejection_result(
            state, "splice_transform refuses a state with duplicate node IDs.", error_code="duplicate_node_id", rejected_component=None
        )
    if len({edge.id for edge in state.edges}) != len(state.edges):
        return _saved_graph_rejection_result(
            state, "splice_transform refuses a state with duplicate edge IDs.", error_code="duplicate_edge_id", rejected_component=None
        )
    successor = next((node for node in state.nodes if node.id == successor_id), None)
    if successor is None:
        return _saved_graph_rejection_result(
            state,
            f"Splice successor '{successor_id}' not found or is not a transform.",
            error_code="node_not_found",
            rejected_component=f"node:{successor_id}",
        )

    existing = next((node for node in state.nodes if node.id == node_args.id), None)
    if existing is not None:
        topology_error = _splice_topology_error(
            state,
            predecessor_id=predecessor_id,
            successor=successor,
            inserted=existing,
        )
        if topology_error is not None:
            return _saved_graph_rejection_result(state, topology_error, error_code="splice_topology_unsupported", rejected_component=None)
        prepared_replay = _prepare_transform_candidate(
            state=state,
            context=context,
            tool_name="splice_transform",
            node_id=node_args.id,
            node_type="transform",
            plugin=node_args.plugin,
            input_name=existing.input,
            on_success=existing.on_success,
            on_error=node_args.on_error if "on_error" in node_args.model_fields_set else existing.on_error,
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
            return _saved_graph_rejection_result(
                state,
                f"Node '{node_args.id}' already exists with a divergent splice definition.",
                error_code="splice_replay_conflict",
                rejected_component=node_ref,
            )
        if not identical:
            return _saved_graph_rejection_result(
                state,
                f"Node '{node_args.id}' already exists with a divergent splice definition.",
                error_code="splice_replay_conflict",
                rejected_component=node_ref,
            )
        canonical_error = _composition_canonical_interpretation_requirement_error(
            state,
            tool_name="splice_transform",
        )
        if canonical_error is not None:
            return _saved_graph_rejection_result(
                state,
                canonical_error,
                error_code="interpretation_requirements_invalid",
                rejected_component=None,
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
        return _saved_graph_rejection_result(
            state,
            f"Inserted node ID '{node_args.id}' collides with an existing source or sink.",
            error_code="node_id_collides_with_source_or_sink",
            rejected_component=node_ref,
        )
    topology_error = _splice_topology_error(state, predecessor_id=predecessor_id, successor=successor)
    if topology_error is not None:
        return _saved_graph_rejection_result(state, topology_error, error_code="splice_topology_unsupported", rejected_component=None)
    predecessor = _splice_predecessor(state, predecessor_id)
    assert predecessor is not None
    predecessor_output = predecessor.on_success
    assert type(predecessor_output) is str
    connection_name = _splice_connection_name(node_args.id, state)
    if connection_name is None:
        return _saved_graph_rejection_result(
            state,
            "No bounded collision-free splice connection name is available.",
            error_code="splice_derivation_conflict",
            rejected_component=node_ref,
        )
    edges = state.edges
    edge_data: dict[str, str] = {}
    if state.edges:
        direct_edge_index, direct_edge = next(
            (index, edge)
            for index, edge in enumerate(state.edges)
            if edge.from_node == predecessor_id and edge.to_node == successor_id and edge.edge_type == "on_success"
        )
        new_edge_id = _splice_edge_id(direct_edge.id, node_args.id)
        if new_edge_id in {edge.id for edge in state.edges}:
            return _saved_graph_rejection_result(
                state,
                f"Derived splice edge ID '{new_edge_id}' collides with an existing edge.",
                error_code="splice_derivation_conflict",
                rejected_component=node_ref,
            )
        predecessor_edge = replace(direct_edge, to_node=node_args.id)
        successor_edge = EdgeSpec(
            id=new_edge_id,
            from_node=node_args.id,
            to_node=successor.id,
            edge_type="on_success",
            label=None,
        )
        edges = (*state.edges[:direct_edge_index], predecessor_edge, successor_edge, *state.edges[direct_edge_index + 1 :])
        edge_data = {"replaced_edge_id": direct_edge.id, "new_edge_id": new_edge_id}
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
    proposed = CompositionState(
        sources=state.sources,
        nodes=nodes,
        edges=edges,
        outputs=state.outputs,
        metadata=state.metadata,
        version=state.version,
    )
    canonical_error = _composition_canonical_interpretation_requirement_error(
        proposed,
        tool_name="splice_transform",
    )
    if canonical_error is not None:
        return _saved_graph_rejection_result(
            state,
            canonical_error,
            error_code="interpretation_requirements_invalid",
            rejected_component=None,
        )
    try:
        reconciled = reconcile_authoritative_reviews(state, proposed)
    except (KeyError, TypeError, ValueError) as exc:
        return _saved_graph_rejection_result(
            state,
            review_reconciliation_failure_message(exc, retry_hint="Re-inspect the pipeline and retry."),
            error_code="review_reconciliation_failed",
            rejected_component=None,
        )
    canonical_error = _composition_canonical_interpretation_requirement_error(
        reconciled,
        tool_name="splice_transform",
    )
    if canonical_error is not None:
        return _saved_graph_rejection_result(
            state,
            canonical_error,
            error_code="interpretation_requirements_invalid",
            rejected_component=None,
        )
    review_contract_error = composition_review_contract_error(reconciled)
    if review_contract_error is not None:
        return _saved_graph_rejection_result(
            state,
            review_contract_error,
            error_code=(
                "vague_term_query_override"
                if review_contract_error.startswith(VAGUE_TERM_QUERY_OVERRIDE_PREFIX)
                else "interpretation_review_contract_unsatisfied"
            ),
            rejected_component=None,
        )
    profile_validation = context.catalog.validate_composition_state(reconciled)
    if not profile_validation.validation.is_valid:
        return _splice_validation_rejection(
            state,
            candidate_errors=profile_validation.validation.errors,
            prior_errors=context.catalog.validate_composition_state(state).validation.errors,
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
            **edge_data,
        },
    )


_SPLICE_VALIDATION_FAILED_MESSAGE: Final[str] = "Spliced pipeline failed context-aware validation."


def _rejection_subject_ref(component: str) -> str | None:
    """The ``rejected_component`` ref for a validation component, when it has one.

    The ref grammar covers only ``source`` / ``source:<name>`` /
    ``node:<id>`` / ``output:<name>``. A pipeline-wide entry has no single
    subject and stays unattributed.
    """
    if component == "source" or component.startswith(("source:", "node:", "output:")):
        return component
    return None


def _splice_validation_rejection(
    state: CompositionState,
    *,
    candidate_errors: Sequence[ValidationEntry],
    prior_errors: Sequence[ValidationEntry],
) -> ToolResult:
    """Reject a splice whose candidate fails context-aware validation, naming why.

    The leading ``splice_validation_failed`` entry is unchanged. After it come
    the candidate's errors that the pre-splice state did not already have,
    each re-filed as a ``rejected_mutation`` entry with its subject in
    ``rejected_component``. Two consumers keep only ``rejected_mutation``
    entries from a rejection: the dispatch-time
    ``normalize_tool_result_validation``, which rebuilds every other entry
    from the unchanged ``updated_state``, and the planner's
    ``_rejection_entries``. An ordinary candidate entry would reach neither
    surface. Before this, only the generic sentence survived, so no surface
    could see what the splice broke.

    ``plugin_identity`` is cleared: the entry was not produced from a name
    this handler resolved through the policy view, and absence is the safe
    value.
    """
    prior_keys = {(entry.component, entry.error_code, entry.message) for entry in prior_errors}
    forwarded = tuple(
        replace(
            entry,
            component="rejected_mutation",
            plugin_identity=None,
            rejected_component=_rejection_subject_ref(entry.component),
        )
        for entry in candidate_errors
        if (entry.component, entry.error_code, entry.message) not in prior_keys
    )
    rejection = _saved_graph_rejection_result(
        state, _SPLICE_VALIDATION_FAILED_MESSAGE, error_code="splice_validation_failed", rejected_component=None
    )
    leading, *standing = rejection.validation.errors
    return replace(
        rejection,
        validation=replace(rejection.validation, errors=(leading, *forwarded, *standing)),
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
    validated = _validate_mutation_arguments(_UpsertEdgeArgumentsModel, args, "upsert_edge arguments")
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
        return _saved_graph_rejection_result(state, lowering_error, error_code="edge_not_lowerable", rejected_component=None)

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
                return _saved_graph_rejection_result(
                    state,
                    f"Edge '{existing.id}' already expresses the '{edge_type}' sink route of '{from_node}'. "
                    f"Update or remove edge '{existing.id}' instead of adding '{edge.id}'.",
                    error_code="edge_route_conflict",
                    rejected_component=None,
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
        return _saved_graph_rejection_result(state, message, error_code=error_code, rejected_component=None)
    reconciled = _reconcile_graph_mutation_reviews(state, new_state)
    if type(reconciled) is ToolResult:
        return reconciled
    return _mutation_result(cast(CompositionState, reconciled), (from_node, to_node))


def _reconcile_graph_mutation_reviews(
    state: CompositionState,
    proposed: CompositionState,
) -> CompositionState | ToolResult:
    """Reconcile resolved reviews after a graph-only mutation.

    ``upsert_edge``, ``remove_node`` and ``remove_edge`` change graph facts
    that resolved review hashes bind: a gate's routes, and every predecessor
    on an LLM's upstream path. Skipping reconciliation left such a review
    ``resolved`` against a drifted anchor. No pending site was enumerated,
    and Execute raised a bare drift ``ValueError`` instead of reopening the
    review. The failure mapping is the one ``splice_transform`` and
    ``upsert_node`` use.
    """
    try:
        return reconcile_authoritative_reviews(state, proposed)
    except (KeyError, TypeError, ValueError) as exc:
        return _saved_graph_rejection_result(
            state,
            review_reconciliation_failure_message(exc, retry_hint="Re-inspect the pipeline and retry."),
            error_code="review_reconciliation_failed",
            rejected_component=None,
        )


def _execute_remove_node(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Remove a node and its edges."""
    del context  # unused; signature uniformity with the other handlers.
    validated = _validate_mutation_arguments(_RemoveByIdArgumentsModel, args, "remove_node arguments")
    node_id = validated.id

    # Collect affected nodes before removal (edges that reference this node)
    affected = {node_id}
    for edge in state.edges:
        if edge.from_node == node_id or edge.to_node == node_id:
            affected.add(edge.from_node)
            affected.add(edge.to_node)

    new_state = state.without_node(node_id)
    if new_state is None:
        return _saved_graph_rejection_result(
            state, f"Node '{node_id}' not found.", error_code="node_not_found", rejected_component=f"node:{node_id}"
        )

    reconciled = _reconcile_graph_mutation_reviews(state, new_state)
    if type(reconciled) is ToolResult:
        return reconciled
    return _mutation_result(cast(CompositionState, reconciled), tuple(sorted(affected)))


def _execute_remove_edge(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Remove an edge."""
    del context  # unused; signature uniformity with the other handlers.
    validated = _validate_mutation_arguments(_RemoveByIdArgumentsModel, args, "remove_edge arguments")
    edge_id = validated.id

    # Find the edge to get affected nodes
    edge = next((e for e in state.edges if e.id == edge_id), None)
    if edge is None:
        return _saved_graph_rejection_result(state, f"Edge '{edge_id}' not found.", error_code="edge_not_found", rejected_component=None)

    affected = (edge.from_node, edge.to_node)
    new_state = state.without_edge(edge_id)
    if new_state is None:
        return _saved_graph_rejection_result(state, f"Edge '{edge_id}' not found.", error_code="edge_not_found", rejected_component=None)
    if not _sink_route_still_expressed(new_state, edge):
        new_state = _clear_removed_sink_edge_route(new_state, edge)

    reconciled = _reconcile_graph_mutation_reviews(state, new_state)
    if type(reconciled) is ToolResult:
        return reconciled
    return _mutation_result(cast(CompositionState, reconciled), affected)


def _execute_set_metadata(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Update pipeline metadata."""
    del context  # unused; signature uniformity with the other handlers.
    validated = _validate_mutation_arguments(_SetMetadataArgumentsModel, args, "set_metadata arguments")
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


def _query_names(queries: object) -> tuple[str, ...] | None:
    """The query names a ``queries`` option carries, in either authoring form.

    A mapping is keyed by query name; a list holds entries with a ``name``. A
    list entry without a string ``name`` names no query. ``None`` when the
    value is neither form: its shape is the plugin schema's to refuse.
    """
    if isinstance(queries, Mapping):
        return tuple(name for name in queries if isinstance(name, str))
    if isinstance(queries, (list, tuple)):
        return tuple(
            entry["name"] for entry in queries if isinstance(entry, Mapping) and "name" in entry and isinstance(entry["name"], str)
        )
    return None


def _queries_omitted_by_patch(stored_queries: object, patched_queries: object) -> tuple[str, ...]:
    """Stored query names that a patch's ``queries`` value would remove without naming.

    The merge is shallow: a ``queries`` value in the patch replaces the stored
    one whole, so a patch naming one query silently dropped the rest. The caller
    passes the two ``queries`` values only when both the patch and the node have
    the key. ``queries: null`` is not this case: it deletes the option by name,
    and ``None`` is neither authoring form.
    """
    stored = _query_names(stored_queries)
    patched = _query_names(patched_queries)
    if stored is None or patched is None:
        return ()
    return tuple(name for name in stored if name not in patched)


def _queries_patch_omission_refusal(node_id: str, omitted: tuple[str, ...]) -> str:
    """The repairable refusal for a ``queries`` patch that leaves stored queries out. Names are written whole."""
    names = ", ".join(repr(name) for name in omitted)
    which = "that query" if len(omitted) == 1 else "those queries"
    return (
        f"Not applied: this patch's queries value leaves out {names}, which node {node_id!r} has, and a patch replaces "
        f"queries whole, so {which} would have been removed (queries_patch_omits_existing). Nothing was changed. "
        "To change one query, send queries with EVERY query the node has, each complete, changing only the one you mean. "
        "To remove a query, send the whole node with upsert_node."
    )


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
            category=ToolArgumentErrorCategory.MODEL_VALIDATION,
        ) from exc
    node_id = validated.node_id
    return _apply_guarded_node_option_patch(node_id, validated.patch, state, context)


def _apply_guarded_node_option_patch(
    node_id: str,
    option_patch: Mapping[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """One guarded candidate builder for whole-option and exact-text edits."""
    node_ref = f"node:{node_id}"
    patch = option_patch
    current = next((n for n in state.nodes if n.id == node_id), None)
    if current is None:
        return _saved_graph_rejection_result(
            state, f"Node '{node_id}' not found.", error_code="node_not_found", rejected_component=node_ref
        )
    routing_patch_error = _node_routing_option_patch_error(patch, node_type=current.node_type)
    if routing_patch_error is not None:
        return _saved_graph_rejection_result(
            state, routing_patch_error, error_code="node_routing_patch_invalid", rejected_component=node_ref
        )
    # Echo tolerance (elspeth-c67fbbbd83): requirement rows echoed verbatim
    # from the stored node reduce to author shells; the reconciliation pass
    # below restores the server rows. Non-matching values still reject.
    patch, requirement_echo = _normalize_echoed_interpretation_requirements(
        patch,
        stored_options=current.options,
    )
    runtime_owned_error = _runtime_owned_llm_option_error(
        current.plugin,
        patch,
        tool_name="patch_node_options",
        component_id=node_id,
    )
    if runtime_owned_error is not None:
        error_code = "interpretation_requirements_invalid" if INTERPRETATION_REQUIREMENTS_KEY in patch else "plugin_options_invalid"
        return _saved_graph_rejection_result(
            state, f"Node '{node_id}': {runtime_owned_error}", error_code=error_code, rejected_component=node_ref
        )
    # prompt_template as text with prompt_template_parts: null REPLACES the
    # stored prompt (see _whole_prompt_text_replacement): the patch then merges
    # into the stored options without the old prompt's parts and term reviews.
    # Every refusal below still returns the untouched ``state``, and reviews
    # reconcile against it.
    # The stored prompt's own text replaces nothing, whatever the parts key says.
    kept_patch = _written_options_keeping_an_unchanged_prompt(current.options, patch, merge_patch=True) if current.plugin == "llm" else None
    prompt_unchanged_node_ids: tuple[str, ...] = ()
    if kept_patch is not None:
        patch = kept_patch
        prompt_unchanged_node_ids = (node_id,)
    replacement = _whole_prompt_text_replacement(current.options, patch) if current.plugin == "llm" else None
    if replacement is not None:
        # A stored row that is already non-canonical is refused first, so the
        # replacement can never launder a malformed row away.
        stored_error = _composition_canonical_interpretation_requirement_error(state, tool_name="patch_node_options")
        if stored_error is not None:
            return _saved_graph_rejection_result(
                state, stored_error, error_code="interpretation_requirements_invalid", rejected_component=node_ref
            )
    base_options = replacement.base_options if replacement is not None else current.options
    structured_error = _structured_prompt_template_refusal(node_id, base_options, patch) if current.plugin == "llm" else None
    if structured_error is not None:
        return _saved_graph_rejection_result(
            state,
            structured_error,
            error_code="prompt_template_parts_required",
            rejected_component=node_ref,
        )
    patch = _canonicalize_authored_interpretation_requirements(
        patch,
        component_id=node_id,
        existing_options=current.options,
    )
    omitted_queries = (
        _queries_omitted_by_patch(current.options["queries"], patch["queries"])
        if "queries" in patch and "queries" in current.options
        else ()
    )
    if omitted_queries:
        return _saved_graph_rejection_result(
            state,
            _queries_patch_omission_refusal(node_id, omitted_queries),
            error_code="queries_patch_omits_existing",
            rejected_component=node_ref,
        )
    if replacement is not None:
        patch = _written_options_for_prompt_replacement(patch, replacement)
    new_options: Mapping[str, Any] = _apply_merge_patch(base_options, dict(patch))
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
        return _saved_graph_rejection_result(
            state,
            f"Node '{node_id}': {canonical_error}",
            error_code="interpretation_requirements_invalid",
            rejected_component=node_ref,
        )
    credential_error = _saved_graph_credential_wiring_contract_failure(
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
        # State-held plugin: resolve it through the request's policy view
        # before prevalidation stamps it (see _execute_patch_source_options).
        plugin_error = _plugin_selection_violation(context, "transform", current.plugin)
        if plugin_error is not None:
            return _saved_graph_plugin_policy_failure(state, plugin_error, component=None, rejected_component=node_ref)
        context.catalog.get_schema("transform", current.plugin)
        # The batch-aware guards upsert_node, splice_transform and set_pipeline
        # run, in the same order. Without them a required_input_fields patch
        # on a batch-aware node reached plugin construction, whose fail-closed
        # FrameworkBugError escaped the tool and ended the turn as a 500.
        batch_placement_error = _batch_aware_placement_error(
            node_id, current.node_type, current.plugin, current.output_mode, current.trigger
        )
        if batch_placement_error is not None:
            return _saved_graph_rejection_result(
                state, batch_placement_error, error_code="batch_transform_misplaced", rejected_component=node_ref
            )
        batch_required_error = _batch_aware_required_input_fields_error(node_id, current.plugin, new_options)
        if batch_required_error is not None:
            return _saved_graph_rejection_result(
                state, batch_required_error, error_code="batch_required_fields_invalid", rejected_component=node_ref
            )

        # Same refusal upsert_node and splice_transform apply, on the merged options.
        prompt_surface_error = _llm_authored_inline_prompt_surface_error(
            context,
            tool_name="patch_node_options",
            node_id=node_id,
            plugin=current.plugin,
            options=new_options,
        )
        if prompt_surface_error is not None:
            return _saved_graph_rejection_result(
                state, prompt_surface_error, error_code="llm_authored_inline_blob_content", rejected_component=node_ref
            )
        # file_inject content is a user-uploaded file on every authoring path.
        file_inject_error = _file_inject_node_option_error(
            context,
            tool_name="patch_node_options",
            node_id=node_id,
            plugin=current.plugin,
            options=new_options,
        )
        if file_inject_error is not None:
            return _saved_graph_rejection_result(
                state, file_inject_error, error_code="file_inject_content_not_uploaded", rejected_component=node_ref
            )

    new_node = replace(current, options=new_options)
    # Third canonical mutation boundary: a patch that would break a queue's
    # intrinsic contract is rejected before creating the candidate state.
    queue_contract_error = queue_node_contract_error(new_node)
    if queue_contract_error is not None:
        return _saved_graph_rejection_result(state, queue_contract_error, error_code="queue_config_invalid", rejected_component=node_ref)
    proposed_state = state.with_node(new_node)
    invariant_error = _post_mutation_invariant_error(proposed_state)
    if invariant_error is not None:
        message, error_code = invariant_error
        return _saved_graph_rejection_result(state, message, error_code=error_code, rejected_component=None)
    try:
        # Reconciliation invalidates inherited approval and renders structured
        # prompts. Verify the stored evidence first so an unrelated patch cannot
        # heal a corrupt digest, then validate the exact reconciled candidate.
        # A file-backed prompt has no options-derivable digest (None): its
        # stored hash is the reviewed file's effective artifact, which run
        # admission re-derives from the file bytes and the approval row, and
        # which execution materialization never trusts from options.
        if current.plugin == "llm" and "approved_prompt_artifact_hash" in current.options:
            recomputed_artifact_hash = approved_prompt_artifact_hash_from_options(current.options)
            if recomputed_artifact_hash is not None and current.options["approved_prompt_artifact_hash"] != recomputed_artifact_hash:
                raise ValueError("Stored approved_prompt_artifact_hash does not match the current prompt artifact")
        new_state = reconcile_authoritative_reviews(state, proposed_state)
    except (KeyError, TypeError, ValueError) as exc:
        return _saved_graph_rejection_result(
            state,
            review_reconciliation_failure_message(exc, retry_hint="Re-inspect the pipeline and retry."),
            error_code="review_reconciliation_failed",
            rejected_component=None,
        )
    reconciled_node = next(node for node in new_state.nodes if node.id == node_id)
    reconciled_options = reconciled_node.options
    if current.node_type in ("transform", "aggregation", "collector") and current.plugin is not None:
        prevalidation_error = _prevalidate_transform_for_context(context, current.plugin, reconciled_options)
        if prevalidation_error is not None:
            return _saved_graph_rejection_result(
                state,
                prevalidation_error[0],
                error_code=prevalidation_error[1],
                rejected_component=node_ref,
                plugin_identity=("transform", current.plugin),
            )

        # Operator-profiled nodes carry their private provider config (retry
        # budget / provider binding) in the profile, injected only at lowering;
        # the prevalidation above already validated the LOWERED executable. The
        # raw provider-config policy would false-positive on the absent private
        # retry budget (see set_pipeline in sessions.py for the full rationale).
        if "profile" not in reconciled_options:
            provider_policy_error = _validate_transform_provider_config_policy(reconciled_options, plugin=current.plugin)
            if provider_policy_error is not None:
                return _saved_graph_rejection_result(
                    state, f"Node '{node_id}': {provider_policy_error}", error_code="llm_retry_budget_policy", rejected_component=node_ref
                )

        # S2: confine nested provider_config persist_directory (RAG retrieval).
        # A merge-patch can introduce an escaping path just as upsert_node can.
        provider_path_error = _validate_transform_provider_config_path(
            reconciled_options,
            context.data_dir,
            session_id=context.session_id,
            require_data_dir=context.require_data_dir_for_paths,
            mcp_root_authority=context.mcp_root_authority,
        )
        if provider_path_error is not None:
            return _saved_graph_rejection_result(
                state,
                _path_policy_refusal_message(
                    context,
                    "Node options.provider_config.persist_directory is outside this session's permitted locations. Use an authorized session-local directory.",
                ),
                error_code="provider_config_path_policy_invalid",
                rejected_component=node_ref,
            )

    review_contract_error = composition_review_contract_error(new_state)
    if review_contract_error is not None:
        return _saved_graph_rejection_result(
            state,
            review_contract_error,
            error_code=(
                "vague_term_query_override"
                if review_contract_error.startswith(VAGUE_TERM_QUERY_OVERRIDE_PREFIX)
                else "interpretation_review_contract_unsatisfied"
            ),
            rejected_component=None,
        )
    echo_note = _echoed_metadata_note(requirement_echo=requirement_echo)
    data: dict[str, JsonValue] = {}
    if echo_note is not None:
        data["server_owned_metadata_note"] = echo_note
    if replacement is not None and replacement.superseded_review_ids:
        # The replaced prompt's vague-term reviews, named so the model can tell
        # the user which definitions no longer apply.
        data["superseded_review_ids"] = list(replacement.superseded_review_ids)
    if prompt_unchanged_node_ids:
        # The patch carried the stored prompt's own text with a null parts key,
        # so the stored prompt structure and reviews were kept.
        data[PROMPT_UNCHANGED_NODE_IDS_KEY] = list(prompt_unchanged_node_ids)
        data[PROMPT_UNCHANGED_NOTE_KEY] = PROMPT_UNCHANGED_NOTE
    return _mutation_result(new_state, (node_id,), data=data or None)


def _substring_refusal(state: CompositionState, message: str, code: str) -> ToolResult:
    return _failure_result(state, message, error_code=code)


def _utf8_bytes(value: str) -> bytes | None:
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None


def _apply_exact_substring_edits(text: str, edits: Sequence[Any]) -> tuple[str, int] | tuple[None, tuple[str, str]]:
    """Build the complete result in memory; overlapping starts count for uniqueness."""
    fragments = 0
    total_replacements = 0
    current = text
    for index, edit in enumerate(edits, start=1):
        old_bytes = _utf8_bytes(edit.old)
        new_bytes = _utf8_bytes(edit.replacement)
        if old_bytes is None or new_bytes is None:
            return None, ("invalid_utf8", "Edit text must be valid UTF-8.")
        fragments += len(old_bytes) + len(new_bytes)
        if fragments > 65536:
            return None, ("edit_too_large", "Edit fragments exceed the allowed byte limit.")
        if not edit.old:
            return None, ("empty_old", f"Edit {index} needs a non-empty old substring.")
        if edit.replace_all:
            count = current.count(edit.old)
        else:
            first = current.find(edit.old)
            if first < 0:
                count = 0
            elif current.find(edit.old, first + 1) >= 0:
                return None, (
                    "substring_not_unique",
                    f"Edit {index} matched more than one location; provide a longer exact substring or explicitly set replace_all.",
                )
            else:
                count = 1
        if count == 0:
            return None, ("substring_not_found", f"Edit {index} matched no text.")
        current_bytes = _utf8_bytes(current)
        assert current_bytes is not None
        if len(current_bytes) + count * (len(new_bytes) - len(old_bytes)) > 262144:
            return None, ("edited_text_too_large", "Edited text exceeds the allowed byte limit.")
        current = current.replace(edit.old, edit.replacement, count)
        current_bytes = _utf8_bytes(current)
        if current_bytes is None or len(current_bytes) > 262144:
            return None, ("edited_text_too_large", "Edited text exceeds the allowed byte limit.")
        total_replacements += count
    return current, total_replacements


def _execute_change_substring(args: dict[str, object], state: CompositionState, context: ToolContext) -> ToolResult:
    try:
        validated = ChangeSubstringArgumentsModel.model_validate(args)
    except PydanticValidationError:
        return _substring_refusal(state, "Invalid exact-edit arguments; inspect the target and retry.", "invalid_arguments")
    target = validated.target
    if target.kind == "blob":
        return _substring_refusal(
            state, "File text editing is not supported yet; edit an inline prompt or use update_blob.", "blob_substring_not_supported"
        )
    # Proposal acceptance already compares its captured base state ID with the
    # current DB record under the COMPOSE lease. Recheck the selected text hash
    # below as well; direct and preview calls receive the live state ID here.
    if context.executing_proposal_id is None and (
        context.current_state_id is None or validated.expected_state_id != context.current_state_id
    ):
        return _substring_refusal(state, "Text changed since inspection; inspect it and retry.", "target_changed")
    node = next((item for item in state.nodes if item.id == target.node_id), None)
    if node is None:
        return _substring_refusal(state, "Node not found; inspect the pipeline and retry.", "node_not_found")
    if node.plugin != "llm":
        return _substring_refusal(state, "Select an LLM prompt text target.", "unsupported_target")
    if target.kind == "node_option":
        if target.option == "prompt_template" and node.options.get(PROMPT_TEMPLATE_PARTS_KEY) is not None:
            return _substring_refusal(state, "This prompt has parts; select a text part.", "prompt_template_parts_required")
        selected = node.options.get(target.option)
    else:
        parts = node.options.get(PROMPT_TEMPLATE_PARTS_KEY)
        if not isinstance(parts, tuple | list) or target.part_index >= len(parts):
            return _substring_refusal(state, "Prompt part not found; inspect the node and retry.", "part_not_found")
        part = parts[target.part_index]
        if not isinstance(part, Mapping):
            return _substring_refusal(state, "Prompt part is invalid; inspect the node and retry.", "part_invalid")
        if part.get("kind") != "text":
            return _substring_refusal(state, "This target is an interpretation slot; select a text part.", "interpretation_slot")
        selected = part.get("text")
    if not isinstance(selected, str):
        return _substring_refusal(state, "Select a text prompt target.", "unsupported_target")
    selected_bytes = _utf8_bytes(selected)
    if selected_bytes is None or len(selected_bytes) > 262144:
        return _substring_refusal(state, "Target text is not valid UTF-8 within the allowed byte limit.", "invalid_target_text")
    if sha256(selected_bytes).hexdigest() != validated.expected_sha256:
        return _substring_refusal(state, "Text changed since inspection; inspect it and retry.", "target_changed")
    edited = _apply_exact_substring_edits(selected, validated.edits)
    if edited[0] is None:
        error = edited[1]
        return _substring_refusal(state, error[1], error[0])
    new_text, replacement_count = edited
    if target.kind == "node_option":
        patch: dict[str, str] = {target.option: new_text}
    else:
        from elspeth.contracts.freeze import deep_thaw

        updated_parts = deep_thaw(parts)
        updated_parts[target.part_index]["text"] = new_text
        patch = {PROMPT_TEMPLATE_PARTS_KEY: updated_parts}
    try:
        require_no_credential_material_for_tool(
            "patch_node_options",
            {"node_id": target.node_id, "patch": dict(patch)},
            surface="composer_tool_arguments",
            credential_state=state,
        )
    except CredentialMaterialRefused:
        return _substring_refusal(state, "Edited prompt contains disallowed credential material.", "credential_material_rejected")
    result = _apply_guarded_node_option_patch(target.node_id, patch, state, context)
    if not result.success:
        code = result.validation.errors[0].error_code if result.validation.errors else "node_options_invalid"
        return _substring_refusal(
            state, "Edited prompt did not pass node validation; inspect the node and retry.", code or "node_options_invalid"
        )
    raw = _utf8_bytes(new_text)
    assert raw is not None
    changed = new_text != selected
    metadata = {
        "target": target.model_dump(),
        "changed": changed,
        "replacement_count": replacement_count,
        "length_chars": len(new_text),
        "size_bytes": len(raw),
        "sha256": sha256(raw).hexdigest(),
    }
    if not changed:
        result = replace(result, updated_state=state, affected_nodes=())
    return replace(result, data=metadata, applied_component=None)


def _change_substring_effects(arguments: Mapping[str, Any]) -> ToolEffects:
    target = arguments.get("target")
    domains = (EffectDomain.GRAPH, EffectDomain.VALIDATION, EffectDomain.YAML, EffectDomain.INTERPRETATION)
    if isinstance(target, Mapping) and target.get("kind") == "blob":
        return ToolEffects(())
    return ToolEffects(domains)


def _inspect_prompt_targets(args: dict[str, object], state: CompositionState, context: ToolContext) -> ToolResult:
    try:
        validated = InspectPromptTargetsArgumentsModel.model_validate(args)
    except PydanticValidationError:
        return _substring_refusal(state, "Specify one node_id to inspect.", "invalid_arguments")
    node_id = validated.node_id
    node = next((item for item in state.nodes if item.id == node_id), None)
    if node is None:
        return _substring_refusal(state, "Node not found; inspect the pipeline and retry.", "node_not_found")
    if node.plugin != "llm":
        return _substring_refusal(state, "Select an LLM prompt text target.", "unsupported_target")
    targets: list[PromptTargetMetadata] = []
    for option in ("prompt_template", "system_prompt"):
        if option == "prompt_template" and node.options.get(PROMPT_TEMPLATE_PARTS_KEY) is not None:
            continue
        value = node.options.get(option)
        if isinstance(value, str):
            raw = _utf8_bytes(value)
            if raw is not None:
                targets.append(
                    PromptTargetMetadata.model_validate(
                        {
                            "target": {"kind": "node_option", "node_id": node_id, "option": option},
                            "length_chars": len(value),
                            "size_bytes": len(raw),
                            "sha256": sha256(raw).hexdigest(),
                        }
                    )
                )
    parts = node.options.get(PROMPT_TEMPLATE_PARTS_KEY)
    if isinstance(parts, tuple | list):
        for index, part in enumerate(parts):
            if isinstance(part, Mapping) and part.get("kind") == "text" and isinstance(part.get("text"), str):
                value = part["text"]
                raw = _utf8_bytes(value)
                if raw is not None:
                    targets.append(
                        PromptTargetMetadata.model_validate(
                            {
                                "target": {"kind": "prompt_part", "node_id": node_id, "part_index": index},
                                "length_chars": len(value),
                                "size_bytes": len(raw),
                                "sha256": sha256(raw).hexdigest(),
                            }
                        )
                    )
    response = PromptTargetsResponse(state_id=context.current_state_id, targets=tuple(targets))
    return _discovery_result(
        state,
        {"state_id": response.state_id, "targets": [target.model_dump(mode="json") for target in response.targets]},
    )


_INSPECT_PROMPT_TARGETS_DECLARATION = ToolDeclaration(
    name="inspect_prompt_targets",
    handler=_inspect_prompt_targets,
    kind=ToolKind.DISCOVERY,
    response_contract=PROMPT_TARGETS_RESPONSE_CONTRACT,
    description="Inspect an LLM node's inline prompt text targets. Result `state_id` is the inspected composition revision; `targets` lists text targets. In each entry, `target` identifies the prompt option or text part, `length_chars` counts characters, `size_bytes` counts UTF-8 bytes, and `sha256` hashes those bytes. No prompt text or interpretation slot values are returned.",
    json_schema={
        "type": "object",
        "properties": {"node_id": {"type": "string", "description": "ID of the LLM node whose prompt text targets should be inspected."}},
        "required": ["node_id"],
        "additionalProperties": False,
    },
)


_CHANGE_SUBSTRING_DECLARATION = ToolDeclaration(
    name="change_substring",
    handler=_execute_change_substring,
    kind=ToolKind.MUTATION,
    description="Change exact text in one LLM prompt target. Inspect the target hash and state ID first. Each old substring must match once, counting overlapping starts; replace_all replaces left to right without rematching inserted text. File targets are declared but not supported yet. Prompt review reopens after a change. Result `target` identifies the edited text, `changed` reports whether it differs, `replacement_count` counts replacements, `length_chars` and `size_bytes` measure the result, and `sha256` hashes it. Inspect in a later tool batch to obtain the committed revision ID.",
    json_schema={
        "type": "object",
        "properties": {
            "target": {
                "type": "object",
                "description": "One inline prompt text target, or a session file target (not supported yet).",
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "node_option"},
                            "node_id": {"type": "string"},
                            "option": {"enum": ["prompt_template", "system_prompt"]},
                        },
                        "required": ["kind", "node_id", "option"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "prompt_part"},
                            "node_id": {"type": "string"},
                            "part_index": {"type": "integer", "minimum": 0},
                        },
                        "required": ["kind", "node_id", "part_index"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {"kind": {"const": "blob"}, "blob_id": {"type": "string"}},
                        "required": ["kind", "blob_id"],
                        "additionalProperties": False,
                    },
                ],
            },
            "expected_sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": "SHA-256 of the selected text's UTF-8 bytes from inspection.",
            },
            "expected_state_id": {
                "type": ["string", "null"],
                "description": "Current composition state ID from inspection; required for inline targets.",
            },
            "edits": {
                "type": "array",
                "description": "One to 32 exact substring replacements, applied in order to this one target.",
                "minItems": 1,
                "maxItems": 32,
                "items": {
                    "type": "object",
                    "properties": {
                        "old": {"type": "string", "minLength": 1, "description": "Non-empty exact old substring."},
                        "replacement": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old", "replacement", "replace_all"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["target", "expected_sha256", "expected_state_id", "edits"],
        "additionalProperties": False,
    },
    argument_effects=_change_substring_effects,
    augments_on_failure=True,
)


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
    "Keys set to null are deleted. Top-level keys the patch does not name are unchanged; a nested value such as queries "
    "is replaced whole, so send every query (a queries value that omits an existing query is refused; queries: null removes every query). "
    "To change the wording of an existing structured LLM prompt, edit prompt_template_parts and preserve "
    "interpretation_ref entries; prompt_template is compiled from those parts. "
    "To REPLACE the whole prompt with new text, send prompt_template (the new text) with prompt_template_parts: null "
    "and no interpretation_requirements key: the parts and every vague-term review of the old prompt are removed, "
    "`superseded_review_ids` names the removed reviews, and the new text gets its own review card. "
    "Changed prompt context requires fresh review. "
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
                    "If prompt_template_parts exists, edit that structure, preserving interpretation_ref entries, to change the prompt's wording; "
                    "a changed prompt_template sent without prompt_template_parts is refused. "
                    "To REPLACE the whole prompt with new text, send prompt_template with prompt_template_parts: null; "
                    "that removes the old prompt's term reviews. "
                    "A patched schema declares arriving types for consumed fields and output types for created fields. "
                    "To convert an arriving value, change the source contract (patch_source_options) or insert a type_coerce upstream; a declaration does not convert."
                    + _LLM_OPTIONS_OWNERSHIP_SCHEMA_NOTE
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
    node_ref = f"node:{node_id}"
    try:
        on_error = resolve_failure_route_choice(on_error, supplied=True, field="on_error")
    except FailureRouteChoiceError as exc:
        return _saved_graph_rejection_result(state, str(exc), error_code="failure_route_required", rejected_component=node_ref)
    if plugin is None:
        return _saved_graph_rejection_result(
            state, f"Node '{node_id}': transform plugin is required.", error_code="transform_missing_plugin", rejected_component=node_ref
        )
    runtime_owned_error = _runtime_owned_llm_option_error(
        plugin,
        options,
        tool_name=tool_name,
        component_id=node_id,
    )
    if runtime_owned_error is not None:
        error_code = "interpretation_requirements_invalid" if INTERPRETATION_REQUIREMENTS_KEY in options else "plugin_options_invalid"
        return _saved_graph_rejection_result(
            state, f"Node '{node_id}': {runtime_owned_error}", error_code=error_code, rejected_component=node_ref
        )
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
    if plugin == "llm":
        structured_error = _structured_prompt_template_refusal(node_id, existing_options or {}, review_options)
        if structured_error is not None:
            return _saved_graph_rejection_result(
                state, structured_error, error_code="prompt_template_parts_required", rejected_component=node_ref
            )
    canonical_error = _canonical_interpretation_requirement_error(
        review_options,
        tool_name=tool_name,
    )
    if canonical_error is not None:
        return _saved_graph_rejection_result(
            state,
            f"Node '{node_id}': {canonical_error}",
            error_code="interpretation_requirements_invalid",
            rejected_component=node_ref,
        )
    credential_error = _saved_graph_credential_wiring_contract_failure(
        state,
        component_id=node_id,
        component_type="node",
        plugin_type="transform",
        plugin_name=plugin,
        options=review_options,
    )
    if credential_error is not None:
        return credential_error
    plugin_error = _plugin_selection_violation(context, "transform", plugin)
    if plugin_error is not None:
        return _saved_graph_plugin_policy_failure(state, plugin_error, component=None, rejected_component=node_ref)
    context.catalog.get_schema("transform", plugin)
    batch_placement_error = _batch_aware_placement_error(node_id, node_type, plugin, output_mode, trigger)
    if batch_placement_error is not None:
        return _saved_graph_rejection_result(
            state, batch_placement_error, error_code="batch_transform_misplaced", rejected_component=node_ref
        )
    batch_required_error = _batch_aware_required_input_fields_error(node_id, plugin, review_options)
    if batch_required_error is not None:
        return _saved_graph_rejection_result(
            state, batch_required_error, error_code="batch_required_fields_invalid", rejected_component=node_ref
        )

    # Same refusal upsert_node and patch_node_options apply (both splice arms).
    prompt_surface_error = _llm_authored_inline_prompt_surface_error(
        context,
        tool_name=tool_name,
        node_id=node_id,
        plugin=plugin,
        options=review_options,
    )
    if prompt_surface_error is not None:
        return _saved_graph_rejection_result(
            state, prompt_surface_error, error_code="llm_authored_inline_blob_content", rejected_component=node_ref
        )
    # file_inject content is a user-uploaded file on every authoring path.
    file_inject_error = _file_inject_node_option_error(
        context,
        tool_name=tool_name,
        node_id=node_id,
        plugin=plugin,
        options=review_options,
    )
    if file_inject_error is not None:
        return _saved_graph_rejection_result(
            state, file_inject_error, error_code="file_inject_content_not_uploaded", rejected_component=node_ref
        )

    prevalidation_error = _prevalidate_transform_for_context(context, plugin, review_options)
    if prevalidation_error is not None:
        return _saved_graph_rejection_result(
            state,
            prevalidation_error[0],
            error_code=prevalidation_error[1],
            rejected_component=node_ref,
            plugin_identity=("transform", plugin),
        )
    # Operator-profiled nodes carry their private provider config (retry budget /
    # provider binding) in the profile, injected only at lowering; the
    # prevalidation above already validated the LOWERED executable. The raw
    # provider-config policy would false-positive on the absent private retry
    # budget (see set_pipeline in sessions.py for the full rationale).
    if "profile" not in review_options:
        provider_policy_error = _validate_transform_provider_config_policy(review_options, plugin=plugin)
        if provider_policy_error is not None:
            return _saved_graph_rejection_result(
                state, f"Node '{node_id}': {provider_policy_error}", error_code="llm_retry_budget_policy", rejected_component=node_ref
            )
    provider_path_error = _validate_transform_provider_config_path(
        review_options,
        context.data_dir,
        session_id=context.session_id,
        require_data_dir=context.require_data_dir_for_paths,
        mcp_root_authority=context.mcp_root_authority,
    )
    if provider_path_error is not None:
        return _saved_graph_rejection_result(
            state,
            _path_policy_refusal_message(
                context,
                "Node options.provider_config.persist_directory is outside this session's permitted locations. Use an authorized session-local directory.",
            ),
            error_code="provider_config_path_policy_invalid",
            rejected_component=node_ref,
        )
    if node_type == "aggregation":
        trigger_error = _validate_aggregation_trigger(dict(trigger) if trigger is not None else None)
        if trigger_error is not None:
            return _saved_graph_rejection_result(
                state, f"Node '{node_id}': {trigger_error}", error_code="aggregation_trigger_invalid", rejected_component=node_ref
            )

    return NodeSpec(
        id=node_id,
        node_type=node_type,
        plugin=plugin,
        input=input_name,
        on_success=on_success,
        on_error=on_error,
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


def _splice_inserted_node_id_schema() -> dict[str, object]:
    """The inserted transform's id: RuntimeNodeName's schema plus the reserved labels it refuses.

    The same disclosure the canonical set_pipeline ``nodes[].id`` carries.
    """
    schema: dict[str, object] = dict(TypeAdapter(RuntimeNodeName).json_schema())
    _disclose_node_name_constraints(schema)
    schema["description"] = "Unique ID for the inserted transform."
    return schema


_SPLICE_TRANSFORM_DECLARATION = ToolDeclaration(
    name="splice_transform",
    handler=_handle_splice_transform,
    kind=ToolKind.MUTATION,
    description=(
        "Insert one transform between a predecessor and successor on an existing direct linear on_success path. "
        "Requires one direct logical on_success connection, no routed error branches on either endpoint, "
        "and no other consumers or visual branches on that path. When visual edges exist, exactly one must "
        "directly connect the endpoints; an entirely edge-free graph stays edge-free. "
        "Use this for insert/between/before/after edits; the server derives input, on_success, connection, and edge IDs. "
        "Returns `inserted_node_id`, `predecessor_id`, `successor_id`, `derived_connection` (the on_success carried "
        "over), plus `replaced_edge_id` and `new_edge_id` only when visual edges were split; "
        "repeating an identical splice returns `already_applied`: true with the node ids but no edge ids, instead of failing."
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
                "description": (
                    "The single transform to insert: provide id, plugin, options, and an explicit non-empty on_error choice for a NEW insertion. "
                    "An exact already-applied replay may omit on_error to preserve its existing choice. Description is optional. "
                    "Do not supply node_type, input, or on_success: the tool authors the transform kind and derives routing "
                    "from the insertion point."
                ),
                "properties": {
                    "id": _splice_inserted_node_id_schema(),
                    "plugin": {"type": "string", "description": "Transform plugin name."},
                    "options": {
                        "type": "object",
                        "description": "Plugin-specific authored options." + _LLM_OPTIONS_OWNERSHIP_SCHEMA_NOTE,
                    },
                    "on_error": {
                        "type": ["string", "null"],
                        "description": "Explicit non-empty failure choice required for a NEW insertion. Omit only on a verified identical replay to preserve its choice. Null/empty is invalid; no default.",
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
    _INSPECT_PROMPT_TARGETS_DECLARATION,
    _CHANGE_SUBSTRING_DECLARATION,
)
"""Every tool declared in this module, in stable order.

``_dispatch.py`` aggregates this tuple alongside every other plane's
TOOLS_IN_MODULE to build the registered-tool universe."""
