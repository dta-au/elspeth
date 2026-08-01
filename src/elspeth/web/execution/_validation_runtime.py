"""Typed runtime-admission phases for execution validation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Protocol, cast

from pydantic import ValidationError as PydanticValidationError

from elspeth.contracts import SinkProtocol, SourceProtocol, TransformProtocol
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.core.config import AggregationSettings, ElspethSettings
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.models import EdgeContractError, GraphValidationError, GraphValidationWarning
from elspeth.core.dag.wiring import WiredTransform
from elspeth.core.secrets import redact_secret_refs_for_validation, resolve_secret_refs
from elspeth.engine.orchestrator.types import PipelineConfig, RouteValidationError
from elspeth.engine.orchestrator.value_source_validation import ValueSourceValidationError
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.manager import PluginNotFoundError
from elspeth.plugins.infrastructure.runtime_factory import PluginBundle
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution._validation_model import (
    GraphedRuntime,
    InstantiatedRuntime,
    LoadedRuntime,
    MaterializedYaml,
    PhaseFailure,
    PhaseReport,
)
from elspeth.web.execution.schemas import (
    CHECK_GATE_FAN_OUT_ADVISORY,
    CHECK_IDENTITY_NODE_ADVISORY,
    CHECK_ROUTE_TARGETS,
    CHECK_SETTINGS,
    CHECK_STATIC_LLM_PROMPT_ADVISORY,
    CHECK_VALUE_SOURCE_COMPLIANCE,
    RUNTIME_CHECK_GRAPH_STRUCTURE,
    RUNTIME_CHECK_PLUGIN_INSTANTIATION,
    RUNTIME_CHECK_SCHEMA_COMPATIBILITY,
    SemanticEdgeContractResponse,
    ValidationCheck,
    ValidationCheckName,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationWarning,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

if TYPE_CHECKING:
    from elspeth.web.execution._validation_diagnostics import (
        _EdgePatchTarget,
        _GateFanOutFinding,
        _IdentityFinding,
        _StaticLLMPromptFinding,
    )


class _YamlLoader(Protocol):
    def __call__(self, yaml_content: str) -> object: ...


class _YamlSettingsLoader(Protocol):
    def __call__(self, yaml_content: str, *, expand_env_vars: bool = False) -> ElspethSettings: ...


class _DictSettingsLoader(Protocol):
    def __call__(self, config_dict: Mapping[str, object], *, expand_env_vars: bool = False) -> ElspethSettings: ...


class _PluginInstantiator(Protocol):
    def __call__(self, settings: ElspethSettings, *, plugin_snapshot: PluginAvailabilitySnapshot) -> PluginBundle: ...


class _GraphBuilder(Protocol):
    def __call__(self, settings: ElspethSettings, bundle: PluginBundle) -> ExecutionGraph: ...


class _RouteValidator(Protocol):
    def __call__(
        self,
        *,
        sources: Mapping[str, SourceProtocol],
        transforms: Sequence[WiredTransform],
        sinks: Mapping[str, SinkProtocol],
        aggregations: Mapping[str, tuple[TransformProtocol, AggregationSettings]],
        settings: ElspethSettings,
        graph: ExecutionGraph,
    ) -> PipelineConfig: ...


class _EdgePatchTargetResolver(Protocol):
    def __call__(
        self,
        dag_node_id: str,
        *,
        state: CompositionState,
        graph: ExecutionGraph,
        component_type: str | None,
    ) -> _EdgePatchTarget: ...


class _EdgeFormatter(Protocol):
    def __call__(
        self,
        exc: EdgeContractError,
        *,
        state: CompositionState,
        graph: ExecutionGraph,
    ) -> tuple[str, str]: ...


class _IdentityFinder(Protocol):
    def __call__(self, state: CompositionState) -> Sequence[_IdentityFinding]: ...


class _GateFanOutFinder(Protocol):
    def __call__(self, state: CompositionState) -> Sequence[_GateFanOutFinding]: ...


class _StaticLLMPromptFinder(Protocol):
    def __call__(self, state: CompositionState) -> Sequence[_StaticLLMPromptFinding]: ...


def _blocked_readiness(
    *,
    code: str,
    detail: str,
    component_id: str | None = None,
    component_type: str | None = None,
) -> ValidationReadiness:
    return ValidationReadiness(
        authoring_valid=False,
        execution_ready=False,
        completion_ready=False,
        blockers=[
            ValidationReadinessBlocker(
                code=code,
                component_id=component_id,
                component_type=component_type,
                detail=detail,
            )
        ],
    )


def _check(name: ValidationCheckName, *, passed: bool, detail: str, affected_nodes: tuple[str, ...] = ()) -> ValidationCheck:
    return ValidationCheck(
        name=name,
        passed=passed,
        detail=detail,
        affected_nodes=affected_nodes,
        outcome_code=None,
    )


def _semantic_contracts(materialized: MaterializedYaml) -> tuple[SemanticEdgeContractResponse, ...]:
    return materialized.authored.semantic_contracts


def _load_yaml_mapping(load_yaml: _YamlLoader, pipeline_yaml: str) -> dict[str, object]:
    """Admit only the owned concrete mapping shape produced by bounded YAML loading."""
    loaded = load_yaml(pipeline_yaml)
    if type(loaded) is not dict:
        raise TypeError(f"generate_yaml() produced non-dict YAML (got {type(loaded).__name__}) — this is a bug in the YAML generator")
    return cast(dict[str, object], loaded)


def _settings_failure(
    materialized: MaterializedYaml,
    exc: PydanticValidationError | ValueError | TypeError,
    *,
    reframed: tuple[ValidationError, ...] = (),
) -> PhaseFailure:
    if reframed:
        errors = reframed
        readiness = _blocked_readiness(code="incomplete_pipeline", detail="Pipeline is missing a required source or output.")
    else:
        errors = (
            ValidationError(
                component_id=None,
                component_type=None,
                message=str(exc),
                suggestion=None,
                error_code=None,
            ),
        )
        readiness = _blocked_readiness(code="settings_load", detail="Settings failed to load.")
    return PhaseFailure(
        passed_checks=(),
        failed_check=_check(CHECK_SETTINGS, passed=False, detail=str(exc)),
        errors=errors,
        readiness=readiness,
        semantic_contracts=materialized.authored.semantic_contracts,
    )


def load_runtime_settings(
    materialized: MaterializedYaml,
    *,
    secret_service: WebSecretResolver | None,
    user_id: str | None,
    load_yaml: _YamlLoader,
    load_settings_yaml: _YamlSettingsLoader,
    load_settings_dict: _DictSettingsLoader,
    reframe_missing_parts: Callable[[PydanticValidationError], list[ValidationError]],
) -> PhaseReport[LoadedRuntime] | PhaseFailure:
    """Load the exact materialized YAML through the execution settings path."""
    pipeline_yaml = materialized.pipeline_yaml
    authored = materialized.authored
    try:
        settings_config: dict[str, object] | None = None
        if secret_service is not None and user_id is not None and authored.all_secret_refs:
            config_dict = _load_yaml_mapping(load_yaml, pipeline_yaml)
            resolved_dict, _resolutions = resolve_secret_refs(
                config_dict,
                secret_service,
                user_id,
                env_ref_names=set(authored.env_ref_names),
            )
            settings_config = resolved_dict
        elif secret_service is None and "secret_ref" in pipeline_yaml:
            config_dict = _load_yaml_mapping(load_yaml, pipeline_yaml)
            settings_config = redact_secret_refs_for_validation(config_dict)

        if settings_config is None:
            runtime_settings = load_settings_yaml(pipeline_yaml, expand_env_vars=False)
        else:
            runtime_settings = load_settings_dict(settings_config, expand_env_vars=False)
    except PydanticValidationError as exc:
        return _settings_failure(materialized, exc, reframed=tuple(reframe_missing_parts(exc)))
    except (ValueError, TypeError) as exc:
        return _settings_failure(materialized, exc)

    return PhaseReport(
        artifact=LoadedRuntime(materialized=materialized, settings=runtime_settings),
        checks=(_check(CHECK_SETTINGS, passed=True, detail="Settings loaded successfully"),),
    )


def validate_runtime_plugins(
    loaded: LoadedRuntime,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
    instantiate_plugins: _PluginInstantiator,
) -> PhaseReport[InstantiatedRuntime] | PhaseFailure:
    """Instantiate plugins, discriminating the embedded value-source walker."""
    semantic_contracts = _semantic_contracts(loaded.materialized)
    try:
        bundle = instantiate_plugins(loaded.settings, plugin_snapshot=plugin_snapshot)
    except ValueSourceValidationError as exc:
        errors = tuple(
            ValidationError(
                component_id=finding.component_id,
                component_type="transform",
                message=finding.format(),
                suggestion=(
                    "Use the list_models composer tool to pick a known model identifier; "
                    "for Azure transforms, leave 'model' empty so it inherits from 'deployment_name'."
                ),
                error_code=None,
            )
            for finding in exc.findings
        )
        return PhaseFailure(
            passed_checks=(_check(RUNTIME_CHECK_PLUGIN_INSTANTIATION, passed=True, detail="All plugins instantiated"),),
            failed_check=_check(CHECK_VALUE_SOURCE_COMPLIANCE, passed=False, detail=str(exc)),
            errors=errors,
            readiness=_blocked_readiness(
                code="value_source_compliance",
                detail="Value-source compliance failed.",
                component_id=errors[-1].component_id if errors else None,
                component_type=errors[-1].component_type if errors else None,
            ),
            semantic_contracts=semantic_contracts,
        )
    except PluginConfigError as exc:
        component_type = exc.component_type
        plugin_name = exc.plugin_name
        if exc.cause is not None and plugin_name is not None:
            detail = f"Invalid configuration for {component_type} '{plugin_name}': {exc.cause}"
        else:
            detail = str(exc)
        return PhaseFailure(
            passed_checks=(),
            failed_check=_check(RUNTIME_CHECK_PLUGIN_INSTANTIATION, passed=False, detail=detail),
            errors=(
                ValidationError(
                    component_id=plugin_name,
                    component_type=component_type,
                    message=detail,
                    suggestion=None,
                    error_code=None,
                ),
            ),
            readiness=_blocked_readiness(
                code="plugin_instantiation",
                detail="Plugin instantiation failed.",
                component_id=plugin_name,
                component_type=component_type,
            ),
            semantic_contracts=semantic_contracts,
        )
    except PluginNotFoundError as exc:
        detail = str(exc)
        return PhaseFailure(
            passed_checks=(),
            failed_check=_check(RUNTIME_CHECK_PLUGIN_INSTANTIATION, passed=False, detail=detail),
            errors=(
                ValidationError(
                    component_id=None,
                    component_type=None,
                    message=detail,
                    suggestion=None,
                    error_code=None,
                ),
            ),
            readiness=_blocked_readiness(code="plugin_instantiation", detail="Plugin instantiation failed."),
            semantic_contracts=semantic_contracts,
        )
    except FileExistsError as exc:
        detail = str(exc)
        return PhaseFailure(
            passed_checks=(),
            failed_check=_check(RUNTIME_CHECK_PLUGIN_INSTANTIATION, passed=False, detail=detail),
            errors=(
                ValidationError(
                    component_id=None,
                    component_type="sink",
                    message=detail,
                    suggestion=(
                        "Set collision_policy='auto_increment' to pick a free sibling path automatically, or choose a different path."
                    ),
                    error_code=None,
                ),
            ),
            readiness=_blocked_readiness(
                code="plugin_instantiation",
                detail="Plugin filesystem target validation failed.",
                component_type="sink",
            ),
            semantic_contracts=semantic_contracts,
        )

    return PhaseReport(
        artifact=InstantiatedRuntime(loaded=loaded, bundle=bundle),
        checks=(_check(RUNTIME_CHECK_PLUGIN_INSTANTIATION, passed=True, detail="All plugins instantiated"),),
    )


def validate_value_source_compliance(instantiated: InstantiatedRuntime) -> PhaseReport[InstantiatedRuntime]:
    """Record the embedded walker's successful completion as its own phase."""
    return PhaseReport(
        artifact=InstantiatedRuntime(loaded=instantiated.loaded, bundle=instantiated.bundle),
        checks=(_check(CHECK_VALUE_SOURCE_COMPLIANCE, passed=True, detail="All declared value sources satisfied"),),
    )


def validate_graph_structure(
    instantiated: InstantiatedRuntime,
    *,
    build_graph: _GraphBuilder,
    warning_to_validation_warning: Callable[[GraphValidationWarning], ValidationWarning],
) -> PhaseReport[GraphedRuntime] | PhaseFailure:
    """Build and structurally validate the exact runtime graph."""
    try:
        graph = build_graph(instantiated.loaded.settings, instantiated.bundle)
        graph.validate()
        graph_warnings = tuple(warning_to_validation_warning(warning) for warning in graph.validation_warnings)
    except GraphValidationError as exc:
        return PhaseFailure(
            passed_checks=(),
            failed_check=_check(RUNTIME_CHECK_GRAPH_STRUCTURE, passed=False, detail=str(exc)),
            errors=(
                ValidationError(
                    component_id=exc.component_id,
                    component_type=exc.component_type,
                    message=str(exc),
                    suggestion=None,
                    error_code=None,
                ),
            ),
            readiness=_blocked_readiness(
                code="graph_structure",
                detail="Graph validation failed.",
                component_id=exc.component_id,
                component_type=exc.component_type,
            ),
            semantic_contracts=_semantic_contracts(instantiated.loaded.materialized),
        )

    return PhaseReport(
        artifact=GraphedRuntime(instantiated=instantiated, graph=graph, graph_warnings=graph_warnings),
        checks=(
            _check(
                RUNTIME_CHECK_GRAPH_STRUCTURE,
                passed=True,
                detail=(
                    "Graph structure is valid" if not graph_warnings else f"Graph structure is valid with {len(graph_warnings)} warning(s)"
                ),
                affected_nodes=tuple(warning.component_id for warning in graph_warnings if warning.component_id is not None),
            ),
        ),
    )


def validate_route_targets(
    graphed: GraphedRuntime,
    *,
    validate_routes: _RouteValidator,
) -> PhaseReport[GraphedRuntime] | PhaseFailure:
    """Validate every configured runtime route target."""
    bundle = graphed.instantiated.bundle
    settings = graphed.instantiated.loaded.settings
    try:
        validate_routes(
            sources=bundle.sources,
            transforms=bundle.transforms,
            sinks=bundle.sinks,
            aggregations=bundle.aggregations,
            settings=settings,
            graph=graphed.graph,
        )
    except RouteValidationError as exc:
        return PhaseFailure(
            passed_checks=(),
            failed_check=_check(CHECK_ROUTE_TARGETS, passed=False, detail=str(exc)),
            errors=(
                ValidationError(
                    component_id=None,
                    component_type=None,
                    message=str(exc),
                    suggestion="Use 'discard' to drop rows without routing, or wire the destination to an existing sink.",
                    error_code=None,
                ),
            ),
            readiness=_blocked_readiness(
                code="route_target_resolution",
                detail="Route target validation failed.",
            ),
            semantic_contracts=_semantic_contracts(graphed.instantiated.loaded.materialized),
        )

    return PhaseReport(
        artifact=GraphedRuntime(
            instantiated=graphed.instantiated,
            graph=graphed.graph,
            graph_warnings=graphed.graph_warnings,
        ),
        checks=(_check(CHECK_ROUTE_TARGETS, passed=True, detail="All route targets resolve to existing sinks"),),
    )


def validate_schema_compatibility(
    graphed: GraphedRuntime,
    *,
    edge_patch_target_for_node_id: _EdgePatchTargetResolver,
    format_edge_contract_failure: _EdgeFormatter,
) -> PhaseReport[GraphedRuntime] | PhaseFailure:
    """Validate edge schemas using authored policy state for diagnostics."""
    try:
        graphed.graph.validate_edge_compatibility()
    except EdgeContractError as exc:
        policy_state = graphed.instantiated.loaded.materialized.authored.policy.state
        consumer_target = edge_patch_target_for_node_id(
            exc.to_node_id,
            state=policy_state,
            graph=graphed.graph,
            component_type=exc.component_type,
        )
        message, suggestion = format_edge_contract_failure(exc, state=policy_state, graph=graphed.graph)
        error = ValidationError(
            component_id=consumer_target.component_id,
            component_type=consumer_target.component_type,
            message=message,
            suggestion=suggestion,
            error_code=None,
        )
        return PhaseFailure(
            passed_checks=(),
            failed_check=_check(RUNTIME_CHECK_SCHEMA_COMPATIBILITY, passed=False, detail=str(exc)),
            errors=(error,),
            readiness=_blocked_readiness(
                code="schema_compatibility",
                detail="Schema compatibility failed.",
                component_id=error.component_id,
                component_type=error.component_type,
            ),
            semantic_contracts=_semantic_contracts(graphed.instantiated.loaded.materialized),
        )
    except GraphValidationError as exc:
        error = ValidationError(
            component_id=exc.component_id,
            component_type=exc.component_type,
            message=str(exc),
            suggestion=None,
            error_code=None,
        )
        return PhaseFailure(
            passed_checks=(),
            failed_check=_check(RUNTIME_CHECK_SCHEMA_COMPATIBILITY, passed=False, detail=str(exc)),
            errors=(error,),
            readiness=_blocked_readiness(
                code="schema_compatibility",
                detail="Schema compatibility failed.",
                component_id=error.component_id,
                component_type=error.component_type,
            ),
            semantic_contracts=_semantic_contracts(graphed.instantiated.loaded.materialized),
        )

    return PhaseReport(
        artifact=GraphedRuntime(
            instantiated=graphed.instantiated,
            graph=graphed.graph,
            graph_warnings=graphed.graph_warnings,
        ),
        checks=(_check(RUNTIME_CHECK_SCHEMA_COMPATIBILITY, passed=True, detail="All edge schemas compatible"),),
    )


def build_identity_advisory_checks(
    graphed: GraphedRuntime,
    *,
    find_identity_node_advisories: _IdentityFinder,
) -> tuple[ValidationCheck, ...]:
    """Build non-blocking advisories after all runtime checks succeed."""
    policy_state = graphed.instantiated.loaded.materialized.authored.policy.state
    checks: list[ValidationCheck] = []
    for finding in find_identity_node_advisories(policy_state):
        sink_mode_text = f", which uses schema.mode: {finding.sink_schema_mode}" if finding.sink_schema_mode is not None else ""
        checks.append(
            _check(
                CHECK_IDENTITY_NODE_ADVISORY,
                passed=True,
                detail=(
                    f"Node '{finding.node_id}' is an identity-shaped passthrough "
                    f"between '{finding.upstream_id}' and sink '{finding.sink_name}'"
                    f"{sink_mode_text}.  The sink accepts the upstream row directly; "
                    "the passthrough adds an audit hop with no contract benefit.  "
                    f"Consider removing it and wiring '{finding.upstream_id}'.on_success "
                    f"directly to '{finding.sink_name}'."
                ),
                affected_nodes=(finding.node_id,),
            )
        )
    return tuple(checks)


def build_gate_fan_out_advisory_checks(
    graphed: GraphedRuntime,
    *,
    find_gate_fan_out_advisories: _GateFanOutFinder,
) -> tuple[ValidationCheck, ...]:
    """Build non-blocking constant-gate fan-out advisories after validation."""
    policy_state = graphed.instantiated.loaded.materialized.authored.policy.state
    checks: list[ValidationCheck] = []
    for finding in find_gate_fan_out_advisories(policy_state):
        destination_text = ", ".join(f"'{destination}'" for destination in finding.destinations)
        checks.append(
            _check(
                CHECK_GATE_FAN_OUT_ADVISORY,
                passed=True,
                detail=(
                    f"Gate '{finding.node_id}' has the constant condition {finding.condition!r}, "
                    "so it makes no per-row decision, and both of its routes deliver to the same "
                    f"destinations ({destination_text}). Every row will be written to all "
                    f"{len(finding.destinations)} of them. If that fan-out is intended, nothing "
                    "needs to change. If each row was meant to land in exactly one destination, "
                    "replace the condition with the rule that decides between them (for example "
                    "\"row['amount'] > 500\") and point the 'true' and 'false' routes at "
                    "different destinations."
                ),
                affected_nodes=(finding.node_id,),
            )
        )
    return tuple(checks)


def build_static_llm_prompt_advisory_checks(
    graphed: GraphedRuntime,
    *,
    find_static_llm_prompt_advisories: _StaticLLMPromptFinder,
) -> tuple[ValidationCheck, ...]:
    """Build non-blocking static-prompt llm advisories after validation.

    Same happy-path-only contract as the identity and gate fan-out advisories.
    Operator direction (elspeth-6bdb7e7736): a static-prompt llm transform is
    essentially a *source* shape (generating rows) mis-declared as a
    transform — so this never escalates to a rejection, now or later. Wording
    deliberately asks rather than asserts ("did you mean...?") because an LLM
    source plugin is in development elsewhere but does not exist yet: naming a
    specific plugin id here would promise a capability that may not be
    installed or allowlisted on a given deployment even once it lands. Revisit
    this string once that plugin ships and its id is verifiable.
    """
    policy_state = graphed.instantiated.loaded.materialized.authored.policy.state
    checks: list[ValidationCheck] = []
    for finding in find_static_llm_prompt_advisories(policy_state):
        checks.append(
            _check(
                CHECK_STATIC_LLM_PROMPT_ADVISORY,
                passed=True,
                detail=(
                    f"Node '{finding.node_id}' has a prompt_template that interpolates no row "
                    "data, so every row receives an identical prompt to the model — one call would do the "
                    "same work as looping over every row. If per-row transformation was intended, add a "
                    "'row.*' reference so each row's prompt reflects its own data. If the intent is "
                    "generation rather than transformation, did you mean to use an LLM source instead?"
                ),
                affected_nodes=(finding.node_id,),
            )
        )
    return tuple(checks)
