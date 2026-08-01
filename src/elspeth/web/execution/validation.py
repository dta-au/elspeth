"""Dry-run validation using real engine code paths.

Calls the same functions as `elspeth run`: load_settings(),
instantiate_runtime_plugins(), build_runtime_graph(),
graph.validate(), assemble_and_validate_pipeline_config() (route targets),
graph.validate_edge_compatibility().

W18 fix: Only typed exceptions are caught. Bare except Exception is forbidden.
Unknown exception types propagate as 500 Internal Server Error, signalling
that this function needs updating — not that the error should be swallowed.

Settings loading uses load_settings_from_yaml_string() — the same in-memory
loader as the execution service. This ensures validation exercises the exact
same code path as execution, and resolved secrets never touch disk.

Route-target validation (issue elspeth-127de6865a) closes the parity gap
where the orchestrator's four pre-init validators
(validate_route_destinations, validate_transform_error_sinks,
validate_source_quarantine_destination, validate_sink_failsink_destinations)
were not reached by /validate, letting dangling references pass preflight
only to be rejected pre-token at /execute.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from pydantic import ValidationError as PydanticValidationError

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.core.config import load_bounded_pipeline_yaml, load_settings_from_config_dict, load_settings_from_yaml_string
from elspeth.core.dag.models import EdgeContractError, GraphValidationWarning
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.manager import PluginNotFoundError
from elspeth.web.composer.state import (
    CompositionState,
)
from elspeth.web.execution._validation_authoring import (
    _DEFAULT_PLUGIN_POLICY_SUGGESTION as _AUTHORING_DEFAULT_PLUGIN_POLICY_SUGGESTION,
)
from elspeth.web.execution._validation_authoring import (
    _collect_secret_refs as _authoring_collect_secret_refs,
)
from elspeth.web.execution._validation_authoring import (
    lower_plugin_policy,
    review_interpretations,
    validate_batch_options,
    validate_path_policy,
    validate_secret_evidence,
    validate_semantic_evidence,
    validate_web_network_policy,
    validate_web_resource_policy,
)
from elspeth.web.execution._validation_ledger import ValidationLedger
from elspeth.web.execution._validation_materialization import (
    materialize_validation_yaml,
    validate_aws_s3_endpoint_url_policy,
    validate_aws_s3_source_policy,
    validate_llm_base_url_policy,
    validate_llm_retry_budget_policy,
    validate_llm_tracing_policy,
    validate_managed_identity_policy,
)
from elspeth.web.execution._validation_model import PhaseFailure, PhaseReport
from elspeth.web.execution._validation_pipeline import ValidationDependencies, ValidationPipeline
from elspeth.web.execution._validation_runtime import (
    build_identity_advisory_checks,
    load_runtime_settings,
    validate_graph_structure,
    validate_route_targets,
    validate_runtime_plugins,
    validate_schema_compatibility,
    validate_value_source_compliance,
)
from elspeth.web.execution.preflight import (
    RUNTIME_CHECK_GRAPH_STRUCTURE,
    RUNTIME_CHECK_PLUGIN_INSTANTIATION,
    RUNTIME_CHECK_SCHEMA_COMPATIBILITY,
    RUNTIME_GRAPH_VALIDATION_CHECKS,
    build_runtime_graph,
    instantiate_runtime_plugins,
)
from elspeth.web.execution.protocol import ValidationSettings, YamlGenerator
from elspeth.web.execution.schemas import (
    CHECK_BATCH_TRANSFORM_OPTIONS,
    CHECK_IDENTITY_NODE_ADVISORY,
    CHECK_INTERPRETATION_REVIEW,
    CHECK_OPERATOR_PROFILE_OPTIONS,
    CHECK_OUTCOME_SKIPPED_AFTER_FAILURE,
    CHECK_PATH_ALLOWLIST,
    CHECK_PLUGIN_ENABLEMENT,
    CHECK_REQUIRED_CONTROL_AVAILABILITY,
    CHECK_REQUIRED_CONTROL_COVERAGE,
    CHECK_ROUTE_TARGETS,
    CHECK_SECRET_REFS,
    CHECK_SEMANTIC_CONTRACTS,
    CHECK_SETTINGS,
    CHECK_VALUE_SOURCE_COMPLIANCE,
    CHECK_WEB_FETCH_RESOURCE_POLICY,
    CHECK_WEB_SCRAPE_NETWORK_POLICY,
    VALIDATION_BLOCKING_CHECK_NAMES,
    ValidationCheck,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
    ValidationWarning,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginSnapshotAuthority
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry

# ── Check names (ordered) ─────────────────────────────────────────────
_CHECK_PLUGIN_ENABLEMENT = CHECK_PLUGIN_ENABLEMENT
_CHECK_OPERATOR_PROFILE_OPTIONS = CHECK_OPERATOR_PROFILE_OPTIONS
_CHECK_REQUIRED_CONTROL_AVAILABILITY = CHECK_REQUIRED_CONTROL_AVAILABILITY
_CHECK_REQUIRED_CONTROL_COVERAGE = CHECK_REQUIRED_CONTROL_COVERAGE
_CHECK_PATH_ALLOWLIST = CHECK_PATH_ALLOWLIST
_CHECK_WEB_SCRAPE_NETWORK_POLICY = CHECK_WEB_SCRAPE_NETWORK_POLICY
_CHECK_WEB_FETCH_RESOURCE_POLICY = CHECK_WEB_FETCH_RESOURCE_POLICY
_CHECK_SECRET_REFS = CHECK_SECRET_REFS
_CHECK_SEMANTIC_CONTRACTS = CHECK_SEMANTIC_CONTRACTS
_CHECK_BATCH_TRANSFORM_OPTIONS = CHECK_BATCH_TRANSFORM_OPTIONS
_CHECK_INTERPRETATION_REVIEW = CHECK_INTERPRETATION_REVIEW
_CHECK_SETTINGS = CHECK_SETTINGS
_CHECK_PLUGINS = RUNTIME_CHECK_PLUGIN_INSTANTIATION
_CHECK_VALUE_SOURCE_COMPLIANCE = CHECK_VALUE_SOURCE_COMPLIANCE
_CHECK_GRAPH = RUNTIME_CHECK_GRAPH_STRUCTURE
_CHECK_ROUTE_TARGETS = CHECK_ROUTE_TARGETS
_CHECK_SCHEMA = RUNTIME_CHECK_SCHEMA_COMPATIBILITY
assert RUNTIME_GRAPH_VALIDATION_CHECKS == (_CHECK_PLUGINS, _CHECK_GRAPH, _CHECK_SCHEMA)


def _execution_ready() -> ValidationReadiness:
    return ValidationReadiness(
        authoring_valid=True,
        execution_ready=True,
        completion_ready=True,
        blockers=[],
    )


def _blocked_readiness(
    *,
    code: str,
    detail: str,
    component_id: str | None = None,
    component_type: str | None = None,
    authoring_valid: bool = False,
    completion_ready: bool = False,
) -> ValidationReadiness:
    return ValidationReadiness(
        authoring_valid=authoring_valid,
        execution_ready=False,
        completion_ready=completion_ready,
        blockers=[
            ValidationReadinessBlocker(
                code=code,
                component_id=component_id,
                component_type=component_type,
                detail=detail,
            )
        ],
    )


def _graph_warning_to_validation_warning(warning: GraphValidationWarning) -> ValidationWarning:
    component_id = warning.node_ids[0] if warning.node_ids else None
    return ValidationWarning(
        component_id=component_id,
        component_type="graph",
        message=warning.message,
        suggestion=None,
        warning_code=warning.code,
    )


# Advisory check — non-blocking, multi-entry (one ValidationCheck per
# detected node, all sharing this name).  Deliberately NOT included in
# _ALL_CHECKS: that list governs the "skipped check" propagation when an
# earlier pass/fail check fails.  This advisory uses ``passed=True`` for
# every entry and is emitted only on the happy-path return, so structural
# errors are never drowned in cosmetic noise.
_CHECK_IDENTITY_NODE_ADVISORY = CHECK_IDENTITY_NODE_ADVISORY

# _CHECK_VALUE_SOURCE_COMPLIANCE slots between _CHECK_PLUGINS (typed configs
# now exist) and _CHECK_GRAPH (so a hallucinated model fails before any DAG
# work). The position is asserted by tests/unit/web/execution/test_validation.py
# to prevent silent reordering.
_ALL_CHECKS = list(VALIDATION_BLOCKING_CHECK_NAMES)

# Compatibility exports retained until the diagnostics extraction task moves
# their implementation again.
_DEFAULT_PLUGIN_POLICY_SUGGESTION = _AUTHORING_DEFAULT_PLUGIN_POLICY_SUGGESTION
_collect_secret_refs = _authoring_collect_secret_refs


def _apply_phase[T](
    ledger: ValidationLedger,
    report: PhaseReport[T] | PhaseFailure,
) -> T | ValidationResult:
    """Apply immutable phase evidence to the run-owned validation ledger."""
    if isinstance(report, PhaseFailure):
        for check in report.passed_checks:
            ledger.record_pass(check)
        return ledger.finish_failure(
            report.failed_check,
            errors=report.errors,
            readiness=report.readiness,
            semantic_contracts=report.semantic_contracts,
        )
    for check in report.checks:
        ledger.record_pass(check)
    return report.artifact


@dataclass(frozen=True, slots=True)
class _EdgePatchTarget:
    component_id: str
    component_type: str | None
    display_name: str
    schema_patch_tool_call: str


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


def _source_name_for_dag_source(state: CompositionState, graph: Any, dag_source_id: str) -> str | None:
    node_info = graph.get_node_info(dag_source_id)
    config = node_info.config
    if "source_name" in config:
        source_name = cast(str, config["source_name"])
        if source_name in state.sources:
            return source_name
    if len(state.sources) == 1:
        return next(iter(state.sources))
    return None


def _edge_patch_targets_by_dag_id(state: CompositionState, graph: Any) -> dict[str, _EdgePatchTarget]:
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
    graph: Any | None = None,
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
    """Extract component type from plugin error metadata.

    Reads PluginConfigError.component_type directly — set by from_dict()
    from the config class hierarchy's _plugin_component_type attribute.
    Returns None for PluginNotFoundError or when component_type was not set.
    """
    if isinstance(exc, PluginConfigError):
        return exc.component_type
    return None


def _format_edge_contract_failure(
    exc: EdgeContractError,
    *,
    state: CompositionState | None = None,
    graph: Any | None = None,
) -> tuple[str, str]:
    """Build LLM-actionable (message, suggestion) pair from a structured edge-contract error.

    The composer surfaces both fields verbatim into the assistant's reply when
    runtime preflight rejects a completion claim. Empirically (cohort
    diagnosis 2026-05-07), models converge on retry only when the message
    names the producer/consumer node IDs and per-field issues, AND the
    suggestion lists concrete tool-call shapes for the fix. Prose like
    "Type mismatches: f (expected X, got Y)" by itself routinely caused the
    model to surrender mid-loop because there was no obvious next move.

    Format choices:
      - Producer/consumer are introduced by NODE ID first (the model uses
        these as ``node_id=`` arguments), then by SCHEMA NAME (informational
        — schema classes are baked-in plugin contracts, the model can't
        target them directly).
      - Each ``CompatibilityResult`` issue category gets its own bullet
        block. We keep the original "expected ... got ..." nomenclature
        from ``CompatibilityResult.error_message`` for continuity, but
        switch to "consumer requires ... producer emits ..." prose because
        empirically the composer LLM mis-grounds "expected/got" against
        the validator's perspective rather than the data-flow direction.
      - The suggestion leads with option (a) (patch consumer) because the
        dominant captured failure mode is consumer over-declaration. The
        producer-side option is listed second with the caveat that plugin
        output schemas are baked-in.
    """
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

    suggestion = _build_edge_contract_suggestion(exc, state=state, graph=graph)
    return message, suggestion


def _build_edge_contract_suggestion(
    exc: EdgeContractError,
    *,
    state: CompositionState | None = None,
    graph: Any | None = None,
) -> str:
    """Compose the action-oriented suggestion text for an edge-contract failure.

    Split out from ``_format_edge_contract_failure`` so the suggestion text
    can be unit-tested without exercising the full message-building flow,
    and so future tuning of the suggestion (e.g., emitting different prose
    for missing-field vs type-mismatch cases) keeps the message format
    stable.
    """
    result = exc.compatibility_result
    has_type_mismatch = bool(result.type_mismatches)
    has_missing = bool(result.missing_fields)
    has_extras = bool(result.extra_fields)
    consumer = _edge_patch_target_for_node_id(
        exc.to_node_id,
        state=state,
        graph=graph,
        component_type=exc.component_type,
    )
    producer = _edge_patch_target_for_node_id(
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


def _skipped_checks(from_check: str, *, already_emitted: frozenset[str] = frozenset()) -> list[ValidationCheck]:
    """Generate skipped check records for all checks after from_check."""
    skipping = False
    result: list[ValidationCheck] = []
    for name in _ALL_CHECKS:
        if name == from_check:
            skipping = True
            continue
        if skipping and name not in already_emitted:
            result.append(
                ValidationCheck(
                    name=name,
                    passed=False,
                    detail=f"Skipped: {from_check} failed",
                    affected_nodes=(),
                    outcome_code=CHECK_OUTCOME_SKIPPED_AFTER_FAILURE,
                )
            )
    return result


def _append_skipped_checks(checks: list[ValidationCheck], from_check: str) -> None:
    checks.extend(_skipped_checks(from_check, already_emitted=frozenset(check.name for check in checks)))


@dataclass(frozen=True, slots=True)
class _IdentityFinding:
    """One detected identity-shaped passthrough between a transform and a sink.

    Emitted by ``_find_identity_node_advisories``; consumed by the advisory
    block in ``validate_pipeline``.  All four fields are scalars, so
    ``frozen=True`` is sufficient (no ``deep_freeze`` guard needed).

    Attributes:
        node_id: ID of the passthrough node itself.
        upstream_id: ID of the producer feeding the passthrough's input
            (or "source" when the source feeds it directly).
        sink_name: Name of the downstream sink (output) the passthrough emits to.
        sink_schema_mode: Schema mode of the sink ("fixed" / "flexible" /
            "observed"), or ``None`` when the sink declares no schema mode.
            Used purely for the advisory's detail string — not a detection input.
    """

    node_id: str
    upstream_id: str
    sink_name: str
    sink_schema_mode: str | None


@observation_boundary(
    tier=3,
    source="composer-authored CompositionState sink/node options (Tier-3, operator/LLM-supplied)",
    source_param="state",
    suppresses=("R1",),
    invariant="returns advisory findings; every malformed-options branch returns/continues, never raises on state",
)
def _find_identity_node_advisories(state: CompositionState) -> list[_IdentityFinding]:
    """Detect identity-shaped passthrough transforms between a real transform and a sink.

    A node is flagged iff ALL of the following hold:

    1. ``node_type == "transform"`` and ``plugin == "passthrough"`` (literal
       string check — deliberately narrow per dispatch; broader registry-based
       detection of ``passes_through_input`` plugins is out of scope).
    2. Exactly one upstream producer feeds ``node.input`` (single inbound).
    3. ``on_success`` targets exactly one sink (output by name) — the
       downstream must be a sink, not another transform.
    4. The node has no fork machinery (``fork_to``, ``routes`` empty).
    5. ``options["schema"]["fields"]`` is missing or empty (not Concept-5
       schema-anchoring per ``pipeline_composer.md:758-768``).
    6. The upstream node is NOT a structural ``gate``, ``queue``, or
       ``row_union``. These boundaries make a downstream passthrough
       structurally meaningful even when it leaves row fields unchanged.

    Returns:
        List of :class:`_IdentityFinding`, one per detected node.  Empty when
        nothing was detected.
    """
    findings: list[_IdentityFinding] = []

    output_by_name = {output.name: output for output in state.outputs}
    nodes_by_id = {node.id: node for node in state.nodes}

    # Producer index: maps a connection-target name (the value carried by an
    # upstream's on_success / on_error / route value / fork_to entry) back to
    # the producer node id.  Used to find a node's upstream by matching its
    # input field.  Explicit "if key not in dict" preserves first-writer-wins
    # semantics; the schema validator rejects duplicate connection targets
    # earlier in the pipeline so collisions here would already have surfaced.
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
    # Canonicalize declared queues: a queue interleaves many producers under
    # its own id, so the queue itself — not whichever source registered first —
    # is the canonical producer of that connection (elspeth-a5b86149d4).
    for node in state.nodes:
        if node.node_type == "queue":
            producer_by_target[node.id] = node.id

    for node in state.nodes:
        # Rule 1: identity passthrough plugin (literal name).
        if node.node_type != "transform" or node.plugin != "passthrough":
            continue
        # Rule 4: no fork machinery on the node itself.
        if node.fork_to or node.routes:
            continue
        # Rule 3: on_success must point to exactly one sink (output).
        if node.on_success is None or node.on_success not in output_by_name:
            continue
        sink = output_by_name[node.on_success]
        # Rule 2: must have an upstream producer.  Absence means the pipeline
        # has a dangling input ref, which a structural check will already have
        # surfaced; the advisory simply skips the node.
        if node.input not in producer_by_target:
            continue
        upstream_id = producer_by_target[node.input]
        # Rule 6: upstream is not a gate, queue, or row union. A gate-fork's per-branch
        # passthrough is the documented legitimate pattern (skill lines
        # 1517-1518); a queue interleaves fan-in with an observed/unknown schema,
        # so a downstream passthrough is doing real structural work, not dead
        # weight (elspeth-a5b86149d4). A row union is likewise a real correlated
        # barrier. ``upstream_id == "source"`` is not in
        # nodes_by_id; the source is neither, so falling through is correct.
        if upstream_id in nodes_by_id and nodes_by_id[upstream_id].node_type in ("gate", "queue", "row_union"):
            continue
        # Rule 5: passthrough has no schema.fields anchor (Concept-5 exemption
        # per skill lines 758-768).  ``options`` values are Tier-3 (LLM- or
        # operator-supplied), so isinstance() dispatches the optional schema
        # block legitimately — a non-Mapping value means "no schema declared".
        schema_block = node.options["schema"] if "schema" in node.options else None
        if isinstance(schema_block, Mapping):
            fields = schema_block.get("fields")
            if isinstance(fields, (list, tuple)) and len(fields) > 0:
                continue
        # Compute sink schema mode for the advisory's detail string.  Same
        # Tier-3 dispatch: sink options are operator-supplied, schema may be
        # absent or shaped differently than expected.
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


# The two required-with-no-default top-level parts of ElspethSettings (see
# core/config.py). A composition missing either fails assembly with a raw
# pydantic "Field required" dump; ``_reframe_settings_missing_parts`` maps each
# to a novice-register finding keyed on this table.
_SETTINGS_MISSING_PART_REFRAMES: dict[str, tuple[str, str, str]] = {
    # pydantic loc field -> (error_code, message, suggestion)
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
    """Reframe ElspethSettings "Field required" failures on the required
    top-level parts (``sources`` / ``sinks``) into novice-register findings.

    Returns one finding per missing part, in a stable source-before-sink order
    (so a lone-transform composition surfaces two honest findings). Returns
    ``[]`` when the failure is anything other than a missing top-level part —
    the caller then falls back to ``str(exc)``. Detection is over the
    *structured* ``exc.errors()`` (``type == "missing"`` at the top of ``loc``),
    never the version-stamped human ``str(exc)`` text.
    """
    missing_parts: set[str] = set()
    for error in exc.errors():
        if error.get("type") != "missing":
            continue
        loc = error.get("loc") or ()
        # pydantic ``loc`` entries are ``int | str`` (ints index list fields);
        # our required parts are top-level string keys, so narrow to str.
        part = loc[0] if loc else None
        if isinstance(part, str) and part in _SETTINGS_MISSING_PART_REFRAMES:
            missing_parts.add(part)
    # Emit in the canonical source-before-sink order regardless of pydantic's
    # error ordering, so the paired findings read consistently.
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


@observation_boundary(
    tier=3,
    source="composer-authored CompositionState (pipeline nodes/options) re-read at dry-run validation",
    source_param="state",
    suppresses=("R1",),
    invariant=(
        "converts expected user/config validation failures into a ValidationResult and returns it; never raises "
        "on the shape of state (Tier-1 invariant crashes on the analyzer's own generated YAML still propagate)"
    ),
)
def validate_pipeline(
    state: CompositionState,
    settings: ValidationSettings,
    yaml_generator: YamlGenerator,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
    profile_registry: OperatorProfileRegistry | None,
    catalog: CatalogService,
    secret_service: WebSecretResolver | None = None,
    user_id: str | None = None,
    blob_get_metadata: Callable[[UUID], BlobRecord | None] | None = None,
    allow_pending_interpretation_placeholders: bool = False,
    session_id: str | None = None,
) -> ValidationResult:
    """Compatibility facade that captures patchable runtime dependencies per call."""
    dependencies = ValidationDependencies(
        load_yaml=load_bounded_pipeline_yaml,
        load_settings_yaml=load_settings_from_yaml_string,
        load_settings_dict=load_settings_from_config_dict,
        instantiate_plugins=instantiate_runtime_plugins,
        build_graph=build_runtime_graph,
        validate_routes=assemble_and_validate_pipeline_config,
    )
    return ValidationPipeline(dependencies).run(
        state,
        settings,
        yaml_generator,
        plugin_snapshot=plugin_snapshot,
        profile_registry=profile_registry,
        catalog=catalog,
        secret_service=secret_service,
        user_id=user_id,
        blob_get_metadata=blob_get_metadata,
        allow_pending_interpretation_placeholders=allow_pending_interpretation_placeholders,
        session_id=session_id,
    )


def _validate_pipeline_impl(
    state: CompositionState,
    settings: ValidationSettings,
    yaml_generator: YamlGenerator,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
    profile_registry: OperatorProfileRegistry | None,
    catalog: CatalogService,
    secret_service: WebSecretResolver | None = None,
    user_id: str | None = None,
    blob_get_metadata: Callable[[UUID], BlobRecord | None] | None = None,
    allow_pending_interpretation_placeholders: bool = False,
    session_id: str | None = None,
    dependencies: ValidationDependencies,
) -> ValidationResult:
    """Dry-run validation through the real engine code path.

    ``session_id`` scopes the sink-path allowlist to the caller's own
    ``blobs/<session>/`` subtree (elspeth-bdc17cfdb1). ``None`` fails
    closed: blob-targeted sink paths are rejected outright.

    Steps:
    1. Source path allowlist check (C3/S2 defense-in-depth)
    1b. Secret ref validation (all referenced secrets exist)
    2. Generate YAML from CompositionState
    3. Load settings via load_settings_from_yaml_string() — resolve secret
       refs first if present, matching the execution service path exactly
    4. instantiate_runtime_plugins(settings, plugin_snapshot=plugin_snapshot)
    5. build_runtime_graph(settings, bundle)
    6. graph.validate() + graph.validate_edge_compatibility()

    Catches and converts to structured ``ValidationResult(is_valid=False)``:
    ``PydanticValidationError``, ``ValueError``, ``TypeError`` (settings load),
    ``PluginNotFoundError``, ``PluginConfigError`` (plugin instantiation),
    ``FileExistsError`` (file-sink path collision under ``fail_if_exists`` /
    exhausted ``auto_increment`` — Tier 3 fs-boundary condition),
    ``GraphValidationError`` (structural), ``RouteValidationError`` (route
    target resolution). All other exceptions propagate (W18) — those are
    Tier 1 invariant breaks that must surface as a 500 to the composer
    failure-handling path.

    Args:
        state: CompositionState from the session.
        settings: ValidationSettings — exposes data_dir for path resolution and allowlist check.
        yaml_generator: YamlGenerator module/object with generate_yaml() method.
        secret_service: Optional secret resolver for validating secret refs.
        user_id: User ID for scoped secret resolution (required if secret_service is set).
        blob_get_metadata: Optional sync metadata lookup for validate-time
            inline-content blob checks. Runtime content reads stay in the
            execution preflight; validate checks metadata only.
        allow_pending_interpretation_placeholders: When true, composer
            authoring preflight masks unresolved ``{{interpretation:<term>}}``
            tokens before YAML generation. Runtime execution leaves this false.
        plugin_snapshot: Frozen request policy/availability snapshot. Local
            trained-operator callers may omit it; web callers must pass one.
        profile_registry: Frozen operator-profile resolver registry. Required
            when the snapshot exposes operator-profiled plugin aliases.
    """
    # Empty compositions have a deliberate legacy producer shape rather than
    # entering the ordered core ledger: no authored phase can make them
    # executable, and engine settings diagnostics would leak internal names.
    if not state.sources and not state.nodes and not state.outputs:
        return ValidationResult(
            is_valid=False,
            checks=[
                ValidationCheck(
                    name=_CHECK_SETTINGS,
                    passed=False,
                    detail="Pipeline has no source, transforms, or outputs.",
                    affected_nodes=(),
                    outcome_code=None,
                ),
                *_skipped_checks(_CHECK_SETTINGS),
            ],
            errors=[
                ValidationError(
                    component_id=None,
                    component_type=None,
                    message="Pipeline is empty. Add a data source and an output step to begin building.",
                    suggestion="Pick a data source like a CSV file or text input, and an output like CSV or JSON, then validate again.",
                    error_code="empty_pipeline",
                ),
            ],
            readiness=_blocked_readiness(code="empty_pipeline", detail="Pipeline is empty."),
        )

    ledger = ValidationLedger()

    policy = _apply_phase(
        ledger,
        lower_plugin_policy(
            state,
            plugin_snapshot=plugin_snapshot,
            profile_registry=profile_registry,
            catalog=catalog,
        ),
    )
    if isinstance(policy, ValidationResult):
        return policy

    path_validated = _apply_phase(
        ledger,
        validate_path_policy(
            policy,
            data_dir=settings.data_dir,
            session_id=session_id,
        ),
    )
    if isinstance(path_validated, ValidationResult):
        return path_validated

    network_validated = _apply_phase(
        ledger,
        validate_web_network_policy(path_validated, plugin_snapshot=plugin_snapshot),
    )
    if isinstance(network_validated, ValidationResult):
        return network_validated

    resource_validated = _apply_phase(
        ledger,
        validate_web_resource_policy(network_validated, plugin_snapshot=plugin_snapshot),
    )
    if isinstance(resource_validated, ValidationResult):
        return resource_validated

    secret_validated = _apply_phase(
        ledger,
        validate_secret_evidence(
            resource_validated,
            secret_service=secret_service,
            user_id=user_id,
        ),
    )
    if isinstance(secret_validated, ValidationResult):
        return secret_validated

    semantic_validated = _apply_phase(
        ledger,
        validate_semantic_evidence(secret_validated),
    )
    if isinstance(semantic_validated, ValidationResult):
        return semantic_validated

    batch_validated = _apply_phase(
        ledger,
        validate_batch_options(semantic_validated),
    )
    if isinstance(batch_validated, ValidationResult):
        return batch_validated

    interpretation_validated = _apply_phase(
        ledger,
        review_interpretations(
            batch_validated,
            allow_pending_placeholders=allow_pending_interpretation_placeholders,
        ),
    )
    if isinstance(interpretation_validated, ValidationResult):
        return interpretation_validated

    materialized = _apply_phase(
        ledger,
        materialize_validation_yaml(
            interpretation_validated,
            yaml_generator=yaml_generator,
            data_dir=settings.data_dir,
            session_id=session_id,
            blob_get_metadata=blob_get_metadata,
            load_yaml=dependencies.load_yaml,
        ),
    )
    if isinstance(materialized, ValidationResult):
        return materialized

    managed_identity_validated = _apply_phase(
        ledger,
        validate_managed_identity_policy(materialized),
    )
    if isinstance(managed_identity_validated, ValidationResult):
        return managed_identity_validated

    retry_budget_validated = _apply_phase(
        ledger,
        validate_llm_retry_budget_policy(managed_identity_validated),
    )
    if isinstance(retry_budget_validated, ValidationResult):
        return retry_budget_validated

    base_url_validated = _apply_phase(
        ledger,
        validate_llm_base_url_policy(retry_budget_validated),
    )
    if isinstance(base_url_validated, ValidationResult):
        return base_url_validated

    tracing_validated = _apply_phase(
        ledger,
        validate_llm_tracing_policy(base_url_validated),
    )
    if isinstance(tracing_validated, ValidationResult):
        return tracing_validated

    endpoint_validated = _apply_phase(
        ledger,
        validate_aws_s3_endpoint_url_policy(
            tracing_validated,
            plugin_snapshot=plugin_snapshot,
        ),
    )
    if isinstance(endpoint_validated, ValidationResult):
        return endpoint_validated

    provider_validated = _apply_phase(
        ledger,
        validate_aws_s3_source_policy(
            endpoint_validated,
            plugin_snapshot=plugin_snapshot,
        ),
    )
    if isinstance(provider_validated, ValidationResult):
        return provider_validated

    loaded = _apply_phase(
        ledger,
        load_runtime_settings(
            provider_validated,
            secret_service=secret_service,
            user_id=user_id,
            load_yaml=dependencies.load_yaml,
            load_settings_yaml=dependencies.load_settings_yaml,
            load_settings_dict=dependencies.load_settings_dict,
            reframe_missing_parts=_reframe_settings_missing_parts,
        ),
    )
    if isinstance(loaded, ValidationResult):
        return loaded

    instantiated = _apply_phase(
        ledger,
        validate_runtime_plugins(
            loaded,
            plugin_snapshot=plugin_snapshot,
            instantiate_plugins=dependencies.instantiate_plugins,
            infer_component_type=_infer_component_type_from_plugin_error,
        ),
    )
    if isinstance(instantiated, ValidationResult):
        return instantiated

    value_source_validated = _apply_phase(ledger, validate_value_source_compliance(instantiated))
    if isinstance(value_source_validated, ValidationResult):
        return value_source_validated

    graphed = _apply_phase(
        ledger,
        validate_graph_structure(
            value_source_validated,
            build_graph=dependencies.build_graph,
            warning_to_validation_warning=_graph_warning_to_validation_warning,
        ),
    )
    if isinstance(graphed, ValidationResult):
        return graphed

    routes_validated = _apply_phase(
        ledger,
        validate_route_targets(graphed, validate_routes=dependencies.validate_routes),
    )
    if isinstance(routes_validated, ValidationResult):
        return routes_validated

    schema_validated = _apply_phase(
        ledger,
        validate_schema_compatibility(
            routes_validated,
            edge_patch_target_for_node_id=_edge_patch_target_for_node_id,
            format_edge_contract_failure=_format_edge_contract_failure,
        ),
    )
    if isinstance(schema_validated, ValidationResult):
        return schema_validated

    for advisory in build_identity_advisory_checks(
        schema_validated,
        find_identity_node_advisories=_find_identity_node_advisories,
    ):
        ledger.record_advisory(advisory)

    authored = schema_validated.instantiated.loaded.materialized.authored
    return ledger.finish_success(
        readiness=_execution_ready(),
        warnings=schema_validated.graph_warnings,
        semantic_contracts=authored.semantic_contracts,
    )


def validate_pipeline_for_trained_operator(
    state: CompositionState,
    settings: ValidationSettings,
    yaml_generator: YamlGenerator,
    **kwargs: Any,
) -> ValidationResult:
    """Explicit non-web validation root preserving CLI and local-tool neutrality."""
    from elspeth.web.dependencies import create_catalog_service

    plugin_snapshot = kwargs.pop("plugin_snapshot", None)
    catalog, plugin_snapshot = _trained_operator_validation_context(kwargs, plugin_snapshot, create_catalog_service)
    profile_registry = kwargs.pop("profile_registry", None)
    return validate_pipeline(
        state,
        settings,
        yaml_generator,
        plugin_snapshot=plugin_snapshot,
        profile_registry=profile_registry,
        catalog=catalog,
        **kwargs,
    )


if TYPE_CHECKING:
    from elspeth.web.catalog.protocol import CatalogService


def _trained_operator_validation_context(
    kwargs: dict[str, Any],
    plugin_snapshot: PluginAvailabilitySnapshot | None,
    catalog_factory: Callable[[], CatalogService],
) -> tuple[CatalogService, PluginAvailabilitySnapshot]:
    """Consume the local catalog and normalize a caller-supplied snapshot."""
    catalog = kwargs.pop("catalog") if "catalog" in kwargs else catalog_factory()
    if plugin_snapshot is None:
        return catalog, PluginAvailabilitySnapshot.for_trained_operator(catalog)
    if plugin_snapshot.is_trained_operator:
        return catalog, plugin_snapshot
    return catalog, PluginAvailabilitySnapshot.create(
        policy_hash=plugin_snapshot.policy_hash,
        principal_scope="local:trained-operator",
        available=plugin_snapshot.available,
        unavailable=plugin_snapshot.unavailable,
        selected=plugin_snapshot.selected,
        usable_profile_aliases=plugin_snapshot.usable_profile_aliases,
        selected_profile_aliases=plugin_snapshot.selected_profile_aliases,
        control_modes=plugin_snapshot.control_modes,
        binding_generation_fingerprint=plugin_snapshot.binding_generation_fingerprint,
        authority=PluginSnapshotAuthority.TRAINED_OPERATOR,
    )
