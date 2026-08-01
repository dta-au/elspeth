"""Diagnostics and advisory helpers for execution validation.

This module owns presentation-only attribution, reframing, and advisory
detection.  It does not own validation order, ledger mutation, or stage-local
exception boundaries.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from pydantic import ValidationError as PydanticValidationError

from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.models import EdgeContractError, GraphValidationWarning
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.manager import PluginNotFoundError
from elspeth.web.composer.state import (
    CompositionState,
    _coalesce_branch_connections,
    _coalesce_branch_names,
    _parse_template_names,
    gate_condition_is_constant,
    gate_route_destinations,
)
from elspeth.web.execution.schemas import ValidationError, ValidationWarning

_SETTINGS_MISSING_PART_REFRAMES: dict[str, tuple[str, str, str]] = {
    "sources": (
        "missing_source",
        "Add a data source so your pipeline has data to read.",
        "Pick a data source like a CSV file or text input, then validate again.",
    ),
    "sinks": (
        "missing_sink",
        "Add an output step so your pipeline has somewhere to send its results.",
        "Pick an output like CSV or JSON and connect your last step to it, then validate again.",
    ),
}


def _reframe_settings_missing_parts(exc: PydanticValidationError) -> list[ValidationError]:
    """Reframe missing required source/sink settings without parsing prose.

    Deliberately NOT decorated with ``observation_boundary``: all three prior
    judge verdicts for this helper's R1/R5 sites (see
    ``config/cicd/enforce_tier_model/web.yaml``,
    ``_reframe_settings_missing_parts`` entries) ruled the suppressions valid
    as rule misfires but explicitly ruled the decorator mechanism
    inapplicable — a structured ``PydanticValidationError`` is not a raw
    external-data parameter requiring a function-scoped Tier-3 boundary. The
    move to this file staled those path-keyed signatures, so the findings run
    ACTIVE here as re-adjudication candidates for the release-end signing
    ceremony (pinned in ``test_validation_trust_tier.py``).
    """
    missing_parts: set[str] = set()
    for error in exc.errors():
        if error.get("type") != "missing":
            continue
        loc = error.get("loc") or ()
        part = loc[0] if loc else None
        if isinstance(part, str) and part in _SETTINGS_MISSING_PART_REFRAMES:
            missing_parts.add(part)
    return [
        ValidationError(
            component_id=None,
            component_type=None,
            message=_SETTINGS_MISSING_PART_REFRAMES[part][1],
            suggestion=_SETTINGS_MISSING_PART_REFRAMES[part][2],
            error_code=_SETTINGS_MISSING_PART_REFRAMES[part][0],
        )
        for part in _SETTINGS_MISSING_PART_REFRAMES
        if part in missing_parts
    ]


def _graph_warning_to_validation_warning(warning: GraphValidationWarning) -> ValidationWarning:
    component_id = warning.node_ids[0] if warning.node_ids else None
    return ValidationWarning(
        component_id=component_id,
        component_type="graph",
        message=warning.message,
        suggestion=None,
        warning_code=warning.code,
    )


@dataclass(frozen=True, slots=True)
class _EdgePatchTarget:
    component_id: str
    component_type: str | None
    display_name: str
    schema_patch_tool_call: str


class _EdgePatchTargetResolver(Protocol):
    def __call__(
        self,
        dag_node_id: str,
        *,
        state: CompositionState | None,
        graph: ExecutionGraph | None,
        component_type: str | None,
    ) -> _EdgePatchTarget: ...


def _node_schema_patch_target(component_id: str, component_type: str | None) -> _EdgePatchTarget:
    display_name = f"node '{component_id}' (row_union)" if component_type == "row_union" else f"{component_type or 'node'} '{component_id}'"
    return _EdgePatchTarget(
        component_id=component_id,
        component_type=component_type,
        display_name=display_name,
        schema_patch_tool_call=f"patch_node_options(node_id='{component_id}', patch={{'schema': {{...}}}})",
    )


def _source_schema_patch_target(source_name: str, plugin_name: str | None) -> _EdgePatchTarget:
    component_id = "source" if source_name == "source" else f"source:{source_name}"
    if source_name == "source":
        display = "source" if plugin_name is None else f"source '{plugin_name}'"
        schema_patch_tool_call = "patch_source_options(patch={'schema': {...}})"
    else:
        display = f"source '{source_name}'" if plugin_name is None else f"source '{source_name}' ({plugin_name})"
        schema_patch_tool_call = f"patch_source_options(source_name={source_name!r}, patch={{'schema': {{...}}}})"
    return _EdgePatchTarget(
        component_id=component_id,
        component_type="source",
        display_name=display,
        schema_patch_tool_call=schema_patch_tool_call,
    )


def _output_schema_patch_target(sink_name: str) -> _EdgePatchTarget:
    return _EdgePatchTarget(
        component_id=sink_name,
        component_type="sink",
        display_name=f"output '{sink_name}'",
        schema_patch_tool_call=f"patch_output_options(sink_name='{sink_name}', patch={{'schema': {{...}}}})",
    )


def _unmapped_schema_patch_target(dag_node_id: str, component_type: str | None) -> _EdgePatchTarget:
    return _EdgePatchTarget(
        component_id=dag_node_id,
        component_type=component_type,
        display_name=f"unmapped DAG node '{dag_node_id}'",
        schema_patch_tool_call="get_pipeline_state(component='all')  # inspect composer IDs before patching this DAG node",
    )


def _source_name_for_dag_source(state: CompositionState, graph: ExecutionGraph, dag_source_id: str) -> str | None:
    node_info = graph.get_node_info(dag_source_id)
    config = node_info.config
    if "source_name" in config:
        source_name = cast(str, config["source_name"])
        if source_name in state.sources:
            return source_name
    if len(state.sources) == 1:
        return next(iter(state.sources))
    return None


def _edge_patch_targets_by_dag_id(state: CompositionState, graph: ExecutionGraph) -> dict[str, _EdgePatchTarget]:
    """Map runtime DAG node IDs back to composer patch-tool targets."""
    targets: dict[str, _EdgePatchTarget] = {}
    nodes_by_id = {node.id: node for node in state.nodes}

    if state.sources:
        for source_id in graph.get_sources():
            dag_source_id = str(source_id)
            source_name = _source_name_for_dag_source(state, graph, dag_source_id)
            if source_name is None:
                continue
            source = state.sources[source_name]
            targets[dag_source_id] = _source_schema_patch_target(source_name, source.plugin)

    transform_nodes = [node for node in state.nodes if node.node_type == "transform"]
    transform_id_map = graph.get_transform_id_map()
    for sequence, dag_node_id in transform_id_map.items():
        if sequence >= len(transform_nodes):
            continue
        node = transform_nodes[sequence]
        targets[str(dag_node_id)] = _node_schema_patch_target(node.id, node.node_type)

    config_gate_id_map = graph.get_config_gate_id_map()
    for gate_name, dag_node_id in config_gate_id_map.items():
        component_id = str(gate_name)
        node_type = nodes_by_id[component_id].node_type if component_id in nodes_by_id else "gate"
        targets[str(dag_node_id)] = _node_schema_patch_target(component_id, node_type)

    aggregation_id_map = graph.get_aggregation_id_map()
    for aggregation_name, dag_node_id in aggregation_id_map.items():
        component_id = str(aggregation_name)
        node_type = nodes_by_id[component_id].node_type if component_id in nodes_by_id else "aggregation"
        targets[str(dag_node_id)] = _node_schema_patch_target(component_id, node_type)

    coalesce_id_map = graph.get_coalesce_id_map()
    for coalesce_name, dag_node_id in coalesce_id_map.items():
        component_id = str(coalesce_name)
        node_type = nodes_by_id[component_id].node_type if component_id in nodes_by_id else "coalesce"
        targets[str(dag_node_id)] = _node_schema_patch_target(component_id, node_type)

    row_union_id_map = graph.get_row_union_id_map()
    for row_union_name, dag_node_id in row_union_id_map.items():
        component_id = str(row_union_name)
        node_type = nodes_by_id[component_id].node_type if component_id in nodes_by_id else "row_union"
        targets[str(dag_node_id)] = _node_schema_patch_target(component_id, node_type)

    sink_id_map = graph.get_sink_id_map()
    for sink_name, dag_node_id in sink_id_map.items():
        targets[str(dag_node_id)] = _output_schema_patch_target(str(sink_name))

    return targets


def _edge_patch_target_for_node_id(
    dag_node_id: str,
    *,
    state: CompositionState | None = None,
    graph: ExecutionGraph | None = None,
    component_type: str | None = None,
) -> _EdgePatchTarget:
    """Resolve a DAG node ID to the composer component/tool that can patch it."""
    if state is None or graph is None:
        return _node_schema_patch_target(dag_node_id, component_type)

    targets = _edge_patch_targets_by_dag_id(state, graph)
    if not targets:
        return _node_schema_patch_target(dag_node_id, component_type)
    if dag_node_id in targets:
        return targets[dag_node_id]
    return _unmapped_schema_patch_target(dag_node_id, component_type)


def _infer_component_type_from_plugin_error(
    exc: PluginNotFoundError | PluginConfigError,
) -> str | None:
    """Return owned config-error attribution through the legacy import seam."""
    if isinstance(exc, PluginConfigError):
        return exc.component_type
    return None


def _format_edge_contract_message(exc: EdgeContractError) -> str:
    """Build the stable diagnostic message independently of patch advice."""
    result = exc.compatibility_result
    issue_lines: list[str] = []
    if result.missing_fields:
        issue_lines.append("Missing required fields (consumer requires, producer does not guarantee):")
        for field_name in result.missing_fields:
            issue_lines.append(f"  - '{field_name}'")
    if result.type_mismatches:
        issue_lines.append("Type mismatches:")
        for field_name, expected, actual in result.type_mismatches:
            issue_lines.append(f"  - field '{field_name}': consumer requires '{expected}', producer emits '{actual}'")
    if result.constraint_mismatches:
        issue_lines.append("Constraint mismatches:")
        for field_name, reason in result.constraint_mismatches:
            issue_lines.append(f"  - field '{field_name}': {reason}")
    if result.extra_fields:
        issue_lines.append("Extra fields forbidden by consumer (producer emits, consumer rejects):")
        for field_name in result.extra_fields:
            issue_lines.append(f"  - '{field_name}'")

    issues_block = "\n".join(issue_lines) if issue_lines else "(no per-field detail available)"

    message = (
        f"Edge contract violation between producer node '{exc.from_node_id}' "
        f"(schema '{exc.producer_schema_name}') and consumer node '{exc.to_node_id}' "
        f"(schema '{exc.consumer_schema_name}'):\n"
        f"{issues_block}"
    )

    return message


def _build_edge_contract_suggestion_with_resolver(
    exc: EdgeContractError,
    *,
    state: CompositionState | None,
    graph: ExecutionGraph | None,
    resolve_target: _EdgePatchTargetResolver,
) -> str:
    """Build concrete consumer-first patch advice through the supplied resolver.

    Composer models converge more reliably when diagnostics name producer and
    consumer node IDs and give exact patch-tool shapes. Consumer relaxation is
    option (a); producer repair remains option (b) because plugin output
    contracts are normally fixed.
    """
    result = exc.compatibility_result
    has_type_mismatch = bool(result.type_mismatches)
    has_missing = bool(result.missing_fields)
    has_extras = bool(result.extra_fields)
    consumer = resolve_target(
        exc.to_node_id,
        state=state,
        graph=graph,
        component_type=exc.component_type,
    )
    producer = resolve_target(
        exc.from_node_id,
        state=state,
        graph=graph,
        component_type=None,
    )

    if producer.component_type == "row_union":
        return "\n".join(
            (
                f"The plugin-free row_union '{producer.component_id}' exposes an engine-owned observed schema; "
                "it has no plugin schema options to patch.",
                f"Relax the real downstream consumer {consumer.display_name} so it accepts the released branch rows.",
                f"Tool: {consumer.schema_patch_tool_call}",
            )
        )
    if consumer.component_type == "row_union":
        return "\n".join(
            (
                f"The plugin-free row_union '{consumer.component_id}' has no consumer schema options to patch.",
                f"Repair the real branch producer {producer.display_name} whose output contract failed at the barrier.",
                f"Tool: {producer.schema_patch_tool_call}",
            )
        )

    parts: list[str] = []
    parts.append("Most edge-contract failures come from the consumer over-declaring fields it doesn't operate on. Try option (a) first.")
    parts.append("")
    parts.append(f"  (a) Relax the consumer's input schema on {consumer.display_name}. Either:")
    if has_type_mismatch:
        parts.append("      - Change the declared field type(s) to match what the producer emits (see Type mismatches above).")
    if has_missing:
        parts.append("      - Drop missing required fields from the consumer's required_fields if the consumer doesn't actually need them.")
    if has_extras:
        parts.append(
            "      - Switch the consumer's input schema mode to 'flexible' or 'observed' so it accepts the producer's extra fields."
        )
    parts.append(
        "      - Or switch the consumer's input schema mode to 'flexible' so it accepts the producer's full output without redeclaring every field."
    )
    parts.append(f"      Tool: {consumer.schema_patch_tool_call}")
    parts.append("")
    parts.append(
        f"  (b) Patch the producer {producer.display_name}. Note: plugin output schemas are largely baked-in by the plugin's contract — "
        f"this option only works if you mis-declared the producer's schema in your initial set_pipeline / upsert_node call. "
        f"If the producer is using its plugin's default output contract, option (a) is the only fix."
    )
    parts.append(f"      Tool: {producer.schema_patch_tool_call}")

    return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class _IdentityFinding:
    """One detected identity-shaped passthrough between a transform and a sink."""

    node_id: str
    upstream_id: str
    sink_name: str
    sink_schema_mode: str | None


@dataclass(frozen=True, slots=True)
class _GateFanOutFinding:
    """One constant gate that sends every row to the same terminal outputs."""

    node_id: str
    condition: str
    destinations: tuple[str, ...]


@observation_boundary(
    tier=3,
    source="composer-authored CompositionState sink/node options (Tier-3, operator/LLM-supplied)",
    source_param="state",
    suppresses=("R1", "R5"),
    invariant="returns advisory findings; every malformed-options branch returns/continues, never raises on state",
)
def _find_identity_node_advisories(state: CompositionState) -> list[_IdentityFinding]:
    """Detect identity-shaped passthrough transforms between a real transform and a sink.

    A node is flagged iff it is a literal passthrough with one real upstream,
    one sink downstream, no fork machinery, and no schema-fields anchor.
    Structural gate, queue, and row-union predecessors are exempt.
    """
    findings: list[_IdentityFinding] = []

    output_by_name = {output.name: output for output in state.outputs}
    nodes_by_id = {node.id: node for node in state.nodes}
    producer_by_target: dict[str, str] = {}

    def _record(target: str, producer_id: str) -> None:
        if target not in producer_by_target:
            producer_by_target[target] = producer_id

    for source_name, source in state.sources.items():
        if source.on_success:
            producer_id = "source" if source_name == "source" else f"source:{source_name}"
            producer_by_target[source.on_success] = producer_id
    for upstream in state.nodes:
        if upstream.on_success:
            _record(upstream.on_success, upstream.id)
        if upstream.on_error:
            _record(upstream.on_error, upstream.id)
        if upstream.routes:
            for route_target in upstream.routes.values():
                _record(route_target, upstream.id)
        if upstream.fork_to:
            for fork_target in upstream.fork_to:
                _record(fork_target, upstream.id)
    for node in state.nodes:
        if node.node_type == "queue":
            producer_by_target[node.id] = node.id

    for node in state.nodes:
        if node.node_type != "transform" or node.plugin != "passthrough":
            continue
        if node.fork_to or node.routes:
            continue
        if node.on_success is None or node.on_success not in output_by_name:
            continue
        sink = output_by_name[node.on_success]
        if node.input not in producer_by_target:
            continue
        upstream_id = producer_by_target[node.input]
        if upstream_id in nodes_by_id and nodes_by_id[upstream_id].node_type in ("gate", "queue", "row_union"):
            continue
        schema_block = node.options["schema"] if "schema" in node.options else None
        if isinstance(schema_block, Mapping):
            fields = schema_block.get("fields")
            if isinstance(fields, (list, tuple)) and len(fields) > 0:
                continue
        sink_schema_mode: str | None = None
        sink_schema_block = sink.options.get("schema")
        if isinstance(sink_schema_block, Mapping):
            mode = sink_schema_block.get("mode")
            if isinstance(mode, str):
                sink_schema_mode = mode
        findings.append(
            _IdentityFinding(
                node_id=node.id,
                upstream_id=upstream_id,
                sink_name=sink.name,
                sink_schema_mode=sink_schema_mode,
            )
        )
    return findings


@observation_boundary(
    tier=3,
    source="composer-authored CompositionState gate routes (Tier-3, operator/LLM-supplied)",
    source_param="state",
    suppresses=("R5",),
    invariant="returns advisory findings for resolved legal fan-out shapes and never raises on malformed authored routes",
)
def _find_gate_fan_out_advisories(state: CompositionState) -> list[_GateFanOutFinding]:
    """Detect constant gates that deliver every row to the same outputs.

    The legal fan-out macro and a dropped routing condition have the same graph
    shape, so this remains advisory. Per-branch work and barrier rejoins are
    differentiated uses and are excluded.
    """
    consumed: set[str] = set()
    for node in state.nodes:
        if node.node_type in ("coalesce", "row_union"):
            consumed.update(_coalesce_branch_names(node.branches))
            consumed.update(_coalesce_branch_connections(node.branches))
        elif node.input:
            consumed.add(node.input)

    findings: list[_GateFanOutFinding] = []
    for node in state.nodes:
        if node.node_type != "gate" or node.plugin is not None:
            continue
        if node.condition is None or not gate_condition_is_constant(node.condition):
            continue
        if node.routes is None or set(node.routes) != {"true", "false"}:
            continue
        destinations = gate_route_destinations(node, node.routes["true"])
        if destinations != gate_route_destinations(node, node.routes["false"]):
            continue
        if len(destinations) < 2 or destinations & consumed:
            continue
        findings.append(
            _GateFanOutFinding(
                node_id=node.id,
                condition=node.condition,
                destinations=tuple(sorted(destinations)),
            )
        )
    return findings


@dataclass(frozen=True, slots=True)
class _StaticLLMPromptFinding:
    """One llm transform node whose prompt_template interpolates no row data.

    Attributes:
        node_id: ID of the offending node.
    """

    node_id: str


@observation_boundary(
    tier=3,
    source="composer-authored CompositionState node options (Tier-3, operator/LLM-supplied prompt_template)",
    source_param="state",
    suppresses=("R1", "R5"),
    invariant="returns advisory findings; unparseable or absent templates are skipped, never raised on",
)
def _find_static_llm_prompt_advisories(state: CompositionState) -> list[_StaticLLMPromptFinding]:
    """Detect llm transform nodes whose prompt_template references no row data.

    A node is flagged iff ALL of the following hold:

    1. ``node_type == "transform"`` and ``plugin == "llm"``.
    2. ``options["queries"]`` is absent (single-prompt mode). Multi-query
       nodes render each query's own per-query context
       (``build_template_context`` in multi_query.py), so the node-level
       ``prompt_template`` is out of scope here — the same ``queries is
       None`` boundary ``_validate_prompt_template_variable_bindings`` uses.
    3. ``options["prompt_template"]`` is a string that parses (after masking
       ``{{interpretation:...}}`` placeholders, which resolve to
       operator-accepted text upstream of rendering and are not Jinja2
       names — same masking the sibling unbound-variable guard applies).
    4. The parsed template's top-level names (``_parse_template_names``,
       reused from ``composer/state.py`` rather than re-parsing) do not
       include ``"row"``. This catches every access shape — ``row.field``,
       ``row["field"]``, ``row[expr]``, and ``{% for x in row %}`` — because
       any reference to ``row`` makes it an undeclared top-level name,
       whether or not it resolves to a concrete field. A template with no
       ``row`` reference renders byte-identical for every input row.

    ``lookup.*``-only templates ARE flagged: ``PromptTemplate`` binds
    ``lookup`` once at construction from static YAML data (see
    ``plugins/transforms/llm/templates.py`` — "Static lookup data from YAML
    file") and never re-binds it per row, so a lookup-only template is just
    as static as a template with no interpolation at all.

    Returns:
        List of :class:`_StaticLLMPromptFinding`, one per detected node.
        Empty when nothing was detected.
    """
    findings: list[_StaticLLMPromptFinding] = []
    for node in state.nodes:
        if node.node_type != "transform" or node.plugin != "llm":
            continue
        if node.options.get("queries") is not None:
            continue
        template = node.options.get("prompt_template")
        if not isinstance(template, str):
            continue
        parsed = _parse_template_names(template)
        if parsed is None:
            continue
        top_level_names, _row_fields = parsed
        if "row" in top_level_names:
            continue
        findings.append(_StaticLLMPromptFinding(node_id=node.id))
    return findings
