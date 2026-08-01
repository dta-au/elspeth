"""Direct tests for runtime-admission validation phases C19-C24."""

from __future__ import annotations

from typing import Any, Never, cast
from unittest.mock import ANY, MagicMock, create_autospec

import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from elspeth.contracts.data import CompatibilityResult
from elspeth.core.config import ElspethSettings, load_bounded_pipeline_yaml, load_settings_from_config_dict, load_settings_from_yaml_string
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.models import EdgeContractError, GraphValidationError, GraphValidationWarning
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from elspeth.engine.orchestrator.types import RouteValidationError
from elspeth.engine.orchestrator.value_source_validation import ValueSourceFinding, ValueSourceValidationError
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.manager import PluginNotFoundError
from elspeth.plugins.infrastructure.runtime_factory import PluginBundle
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.execution._validation_diagnostics import (
    _EdgePatchTarget,
    _GateFanOutFinding,
    _IdentityFinding,
    _StaticLLMPromptFinding,
)
from elspeth.web.execution._validation_model import (
    AuthoredValidatedState,
    GraphedRuntime,
    InstantiatedRuntime,
    LoadedRuntime,
    MaterializedYaml,
    PhaseFailure,
    PhaseReport,
    PolicyLoweredState,
)
from elspeth.web.execution._validation_runtime import (
    build_gate_fan_out_advisory_checks,
    build_identity_advisory_checks,
    build_static_llm_prompt_advisory_checks,
    load_runtime_settings,
    validate_graph_structure,
    validate_route_targets,
    validate_runtime_plugins,
    validate_schema_compatibility,
    validate_value_source_compliance,
)
from elspeth.web.execution.preflight import build_runtime_graph, instantiate_runtime_plugins
from elspeth.web.execution.schemas import (
    SemanticEdgeContractResponse,
    ValidationError,
    ValidationWarning,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot


def _state(*, version: int = 1) -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=version,
    )


def _contract() -> SemanticEdgeContractResponse:
    return SemanticEdgeContractResponse(
        from_id="source",
        to_id="sink",
        consumer_plugin="json",
        producer_plugin="csv",
        producer_field="value",
        consumer_field="value",
        outcome="satisfied",
        requirement_code="field.required",
    )


def _materialized(
    *,
    policy_state: CompositionState | None = None,
    materialized_state: CompositionState | None = None,
    pipeline_yaml: str = "sources: {}\nsinks: {}\n",
) -> MaterializedYaml:
    policy_state = policy_state or _state()
    return MaterializedYaml(
        authored=AuthoredValidatedState(
            policy=PolicyLoweredState(state=policy_state, operator_resolved_model_node_ids=frozenset()),
            all_secret_refs=(),
            env_ref_names=frozenset(),
            semantic_contracts=(_contract(),),
        ),
        materialized_state=materialized_state or policy_state,
        pipeline_yaml=pipeline_yaml,
    )


def _loaded(materialized: MaterializedYaml | None = None) -> LoadedRuntime:
    return LoadedRuntime(materialized=materialized or _materialized(), settings=_settings())


def _settings() -> ElspethSettings:
    return cast(ElspethSettings, create_autospec(ElspethSettings, instance=True))


def _bundle() -> PluginBundle:
    return cast(PluginBundle, create_autospec(PluginBundle, instance=True))


def _instantiated(loaded: LoadedRuntime | None = None) -> InstantiatedRuntime:
    return InstantiatedRuntime(loaded=loaded or _loaded(), bundle=_bundle())


def _autospec_callable(target: object, **configuration: Any) -> MagicMock:
    return cast(MagicMock, create_autospec(target, **configuration))


def _graph(*, warnings: tuple[GraphValidationWarning, ...] = ()) -> MagicMock:
    graph = cast(MagicMock, create_autospec(ExecutionGraph, instance=True))
    graph.validation_warnings = warnings
    return graph


def _unexpected_edge_target(
    dag_node_id: str,
    *,
    state: CompositionState,
    graph: ExecutionGraph,
    component_type: str | None,
) -> Never:
    del dag_node_id, state, graph, component_type
    raise AssertionError("edge target resolver must not be called")


def _unexpected_edge_formatter(
    exc: EdgeContractError,
    *,
    state: CompositionState,
    graph: ExecutionGraph,
) -> tuple[str, str]:
    del exc, state, graph
    raise AssertionError("edge formatter must not be called")


def _graphed(graph: ExecutionGraph | None = None) -> GraphedRuntime:
    return GraphedRuntime(instantiated=_instantiated(), graph=graph or _graph(), graph_warnings=())


def _snapshot() -> PluginAvailabilitySnapshot:
    return PluginAvailabilitySnapshot.create(
        policy_hash="runtime-phase-test",
        principal_scope="local:test",
        available=frozenset(),
        unavailable=(),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="runtime-phase-test-generation",
    )


def _warning(warning: GraphValidationWarning) -> ValidationWarning:
    return ValidationWarning(
        component_id=warning.node_ids[0] if warning.node_ids else None,
        component_type="graph",
        message=warning.message,
        suggestion=None,
        warning_code=warning.code,
    )


def _reframe_missing(exc: PydanticValidationError) -> list[ValidationError]:
    del exc
    return [
        ValidationError(
            component_id=None,
            component_type=None,
            message="Add a data source.",
            suggestion="Pick a source.",
            error_code="missing_source",
        )
    ]


def test_settings_phase_loads_exact_yaml_without_host_expansion_and_detaches_evidence() -> None:
    materialized = _materialized(pipeline_yaml="sources: {}\nsinks: {}\n")
    parsed_settings = _settings()
    load_yaml = _autospec_callable(load_bounded_pipeline_yaml)
    load_settings_yaml = _autospec_callable(load_settings_from_yaml_string, return_value=parsed_settings)
    load_settings_dict = _autospec_callable(load_settings_from_config_dict)

    result = load_runtime_settings(
        materialized,
        secret_service=None,
        user_id=None,
        load_yaml=load_yaml,
        load_settings_yaml=load_settings_yaml,
        load_settings_dict=load_settings_dict,
        reframe_missing_parts=_reframe_missing,
    )
    assert isinstance(result, PhaseReport)
    materialized.authored.semantic_contracts[0].outcome = "conflict"

    assert result.artifact.settings is parsed_settings
    assert result.artifact.materialized is not materialized
    assert result.artifact.materialized.authored.policy is materialized.authored.policy
    assert result.artifact.materialized.materialized_state is materialized.materialized_state
    assert result.artifact.materialized.authored.semantic_contracts[0].outcome == "satisfied"
    assert [(check.name, check.detail) for check in result.checks] == [("settings_load", "Settings loaded successfully")]
    load_settings_yaml.assert_called_once_with(materialized.pipeline_yaml, expand_env_vars=False)
    load_yaml.assert_not_called()
    load_settings_dict.assert_not_called()


def test_settings_phase_reframes_missing_parts_but_retains_raw_check_detail() -> None:
    class _RequiredParts(BaseModel):
        sources: dict[str, object]
        sinks: dict[str, object]

    with pytest.raises(PydanticValidationError) as exc_info:
        _RequiredParts.model_validate({})

    result = load_runtime_settings(
        _materialized(),
        secret_service=None,
        user_id=None,
        load_yaml=_autospec_callable(load_bounded_pipeline_yaml),
        load_settings_yaml=_autospec_callable(load_settings_from_yaml_string, side_effect=exc_info.value),
        load_settings_dict=_autospec_callable(load_settings_from_config_dict),
        reframe_missing_parts=_reframe_missing,
    )

    assert isinstance(result, PhaseFailure)
    assert result.failed_check.name == "settings_load"
    assert "Field required" in result.failed_check.detail
    assert [error.error_code for error in result.errors] == ["missing_source"]
    assert result.readiness.blockers[0].code == "incomplete_pipeline"
    assert result.semantic_contracts[0].outcome == "satisfied"


@pytest.mark.parametrize("error", [ValueError("invalid settings"), TypeError("wrong settings type")])
def test_settings_phase_converts_only_expected_non_pydantic_errors(error: Exception) -> None:
    result = load_runtime_settings(
        _materialized(),
        secret_service=None,
        user_id=None,
        load_yaml=_autospec_callable(load_bounded_pipeline_yaml),
        load_settings_yaml=_autospec_callable(load_settings_from_yaml_string, side_effect=error),
        load_settings_dict=_autospec_callable(load_settings_from_config_dict),
        reframe_missing_parts=_reframe_missing,
    )

    assert isinstance(result, PhaseFailure)
    assert result.failed_check.detail == str(error)
    assert result.errors[0].message == str(error)
    assert result.readiness.blockers[0].code == "settings_load"


def test_settings_phase_propagates_unexpected_exceptions() -> None:
    with pytest.raises(RuntimeError, match="loader invariant"):
        load_runtime_settings(
            _materialized(),
            secret_service=None,
            user_id=None,
            load_yaml=_autospec_callable(load_bounded_pipeline_yaml),
            load_settings_yaml=_autospec_callable(load_settings_from_yaml_string, side_effect=RuntimeError("loader invariant")),
            load_settings_dict=_autospec_callable(load_settings_from_config_dict),
            reframe_missing_parts=_reframe_missing,
        )


def test_plugin_and_value_source_successes_are_separate_ordered_phases() -> None:
    loaded = _loaded()
    bundle = _bundle()
    instantiate = _autospec_callable(instantiate_runtime_plugins, return_value=bundle)

    plugins = validate_runtime_plugins(
        loaded,
        plugin_snapshot=_snapshot(),
        instantiate_plugins=instantiate,
    )
    assert isinstance(plugins, PhaseReport)
    values = validate_value_source_compliance(plugins.artifact)
    assert isinstance(values, PhaseReport)

    assert [check.name for check in plugins.checks] == ["plugin_instantiation"]
    assert [check.name for check in values.checks] == ["value_source_compliance"]
    assert values.artifact is not plugins.artifact
    assert values.artifact.bundle is bundle
    assert values.artifact.loaded.settings is loaded.settings
    instantiate.assert_called_once_with(loaded.settings, plugin_snapshot=ANY)


def test_value_source_failure_records_plugin_pass_and_attributed_errors() -> None:
    finding = ValueSourceFinding(component_id="llm_1", field_name="model", reason="not in catalog")
    error = ValueSourceValidationError("bad model", findings=(finding,))

    result = validate_runtime_plugins(
        _loaded(),
        plugin_snapshot=_snapshot(),
        instantiate_plugins=_autospec_callable(instantiate_runtime_plugins, side_effect=error),
    )

    assert isinstance(result, PhaseFailure)
    assert [check.name for check in result.passed_checks] == ["plugin_instantiation"]
    assert result.failed_check.name == "value_source_compliance"
    assert result.errors[0].component_id == "llm_1"
    assert result.errors[0].component_type == "transform"
    assert result.readiness.blockers[0].code == "value_source_compliance"


@pytest.mark.parametrize(
    ("error", "component_type"),
    [
        (PluginNotFoundError("missing plugin"), None),
        (
            PluginConfigError(
                "Invalid configuration for CSVSourceConfig",
                cause="path missing",
                plugin_class="CSVSourceConfig",
                plugin_name="csv",
                component_type="source",
            ),
            "source",
        ),
        (FileExistsError("Output path already exists"), "sink"),
    ],
)
def test_plugin_phase_discriminates_expected_construction_failures(error: Exception, component_type: str | None) -> None:
    result = validate_runtime_plugins(
        _loaded(),
        plugin_snapshot=_snapshot(),
        instantiate_plugins=_autospec_callable(instantiate_runtime_plugins, side_effect=error),
    )

    assert isinstance(result, PhaseFailure)
    assert result.passed_checks == ()
    assert result.failed_check.name == "plugin_instantiation"
    assert result.errors[0].component_type == component_type
    assert result.readiness.blockers[0].code == "plugin_instantiation"
    if isinstance(error, PluginConfigError):
        assert result.failed_check.detail == "Invalid configuration for source 'csv': path missing"
    if isinstance(error, FileExistsError):
        assert result.errors[0].suggestion is not None
        assert "collision_policy='auto_increment'" in result.errors[0].suggestion


def test_plugin_phase_propagates_unexpected_exceptions() -> None:
    with pytest.raises(RuntimeError, match="plugin invariant"):
        validate_runtime_plugins(
            _loaded(),
            plugin_snapshot=_snapshot(),
            instantiate_plugins=_autospec_callable(
                instantiate_runtime_plugins,
                side_effect=RuntimeError("plugin invariant"),
            ),
        )


def test_graph_phase_preserves_warning_order_and_first_node_attribution() -> None:
    warning_a = GraphValidationWarning(code="A", message="first", node_ids=("node_a", "node_b"))
    warning_b = GraphValidationWarning(code="B", message="second", node_ids=())
    graph = _graph(warnings=(warning_a, warning_b))
    build_graph = _autospec_callable(build_runtime_graph, return_value=graph)
    instantiated = _instantiated()

    result = validate_graph_structure(instantiated, build_graph=build_graph, warning_to_validation_warning=_warning)
    assert isinstance(result, PhaseReport)

    assert result.artifact.graph is graph
    assert [warning.warning_code for warning in result.artifact.graph_warnings] == ["A", "B"]
    assert result.artifact.graph_warnings[0].component_id == "node_a"
    assert result.artifact.graph_warnings[0].message == "first"
    assert result.checks[0].detail == "Graph structure is valid with 2 warning(s)"
    assert result.checks[0].affected_nodes == ("node_a",)
    build_graph.assert_called_once_with(instantiated.loaded.settings, instantiated.bundle)
    graph.validate.assert_called_once_with()


def test_graph_phase_converts_graph_errors_and_propagates_unexpected_errors() -> None:
    graph_error = GraphValidationError("bad graph", component_id="gate_1", component_type="gate")
    failure = validate_graph_structure(
        _instantiated(),
        build_graph=_autospec_callable(build_runtime_graph, side_effect=graph_error),
        warning_to_validation_warning=_warning,
    )
    assert isinstance(failure, PhaseFailure)
    assert failure.failed_check.name == "graph_structure"
    assert failure.errors[0].component_id == "gate_1"
    assert failure.readiness.blockers[0].code == "graph_structure"

    with pytest.raises(RuntimeError, match="graph invariant"):
        validate_graph_structure(
            _instantiated(),
            build_graph=_autospec_callable(build_runtime_graph, side_effect=RuntimeError("graph invariant")),
            warning_to_validation_warning=_warning,
        )


def test_route_phase_calls_runtime_validator_with_exact_admitted_artifacts() -> None:
    graphed = _graphed()
    validate_routes = _autospec_callable(assemble_and_validate_pipeline_config, return_value=object())

    result = validate_route_targets(graphed, validate_routes=validate_routes)
    assert isinstance(result, PhaseReport)
    assert result.artifact is not graphed
    assert result.artifact.graph is graphed.graph
    assert result.artifact.instantiated.bundle is graphed.instantiated.bundle
    validate_routes.assert_called_once_with(
        sources=graphed.instantiated.bundle.sources,
        transforms=graphed.instantiated.bundle.transforms,
        sinks=graphed.instantiated.bundle.sinks,
        aggregations=graphed.instantiated.bundle.aggregations,
        settings=graphed.instantiated.loaded.settings,
        graph=graphed.graph,
    )
    assert result.checks[0].name == "route_target_resolution"


def test_route_phase_catches_only_route_validation_error() -> None:
    failure = validate_route_targets(
        _graphed(),
        validate_routes=_autospec_callable(
            assemble_and_validate_pipeline_config,
            side_effect=RouteValidationError("missing sink"),
        ),
    )
    assert isinstance(failure, PhaseFailure)
    assert failure.failed_check.name == "route_target_resolution"
    assert failure.readiness.blockers[0].code == "route_target_resolution"

    with pytest.raises(RuntimeError, match="route invariant"):
        validate_route_targets(
            _graphed(),
            validate_routes=_autospec_callable(
                assemble_and_validate_pipeline_config,
                side_effect=RuntimeError("route invariant"),
            ),
        )


def test_schema_phase_uses_authored_policy_state_for_rich_edge_diagnostics() -> None:
    # Distinct by value, not just identity: version=2 makes an accidental
    # swap to the materialized state fail the equality assertions below.
    policy_state = _state()
    materialized_state = _state(version=2)
    graph = _graph()
    edge_error = EdgeContractError(
        "edge mismatch",
        from_node_id="producer",
        to_node_id="consumer",
        producer_schema_name="Producer",
        consumer_schema_name="Consumer",
        compatibility_result=CompatibilityResult(compatible=False, missing_fields=("value",)),
        component_type="transform",
    )
    graph.validate_edge_compatibility.side_effect = edge_error
    graphed = GraphedRuntime(
        instantiated=_instantiated(_loaded(_materialized(policy_state=policy_state, materialized_state=materialized_state))),
        graph=graph,
        graph_warnings=(),
    )
    seen: list[CompositionState] = []

    def edge_target(
        dag_node_id: str,
        *,
        state: CompositionState,
        graph: ExecutionGraph,
        component_type: str | None,
    ) -> _EdgePatchTarget:
        del dag_node_id, graph, component_type
        seen.append(state)
        return _EdgePatchTarget(
            component_id="composer_consumer",
            component_type="transform",
            display_name="composer consumer",
            schema_patch_tool_call="patch_node_options()",
        )

    def format_edge(exc: EdgeContractError, *, state: CompositionState, graph: ExecutionGraph) -> tuple[str, str]:
        del exc, graph
        seen.append(state)
        return "rich edge message", "rich edge suggestion"

    result = validate_schema_compatibility(
        graphed,
        edge_patch_target_for_node_id=edge_target,
        format_edge_contract_failure=format_edge,
    )

    assert isinstance(result, PhaseFailure)
    assert seen == [policy_state, policy_state]
    assert seen[0] is policy_state
    assert seen[1] is policy_state
    assert result.failed_check.detail == "edge mismatch"
    assert result.errors[0].component_id == "composer_consumer"
    assert result.errors[0].message == "rich edge message"
    assert result.errors[0].suggestion == "rich edge suggestion"


def test_schema_phase_plain_error_and_success_preserve_exact_graph() -> None:
    failing_graph = _graph()
    failing_graph.validate_edge_compatibility.side_effect = GraphValidationError(
        "schema mismatch",
        component_id="sink",
        component_type="sink",
    )
    failure = validate_schema_compatibility(
        _graphed(failing_graph),
        edge_patch_target_for_node_id=_unexpected_edge_target,
        format_edge_contract_failure=_unexpected_edge_formatter,
    )
    assert isinstance(failure, PhaseFailure)
    assert failure.errors[0].message == "schema mismatch"
    assert failure.errors[0].suggestion is None

    graph = _graph()
    graphed = _graphed(graph)
    success = validate_schema_compatibility(
        graphed,
        edge_patch_target_for_node_id=_unexpected_edge_target,
        format_edge_contract_failure=_unexpected_edge_formatter,
    )
    assert isinstance(success, PhaseReport)
    assert success.artifact is not graphed
    assert success.artifact.graph is graph
    assert success.checks[0].name == "schema_compatibility"


def test_identity_advisory_checks_use_policy_state_and_preserve_exact_prose() -> None:
    # materialized_state differs by value (version=2) so this test can tell
    # the two states apart; the identity assertion below then proves the
    # finder received the authored policy state, never the materialized one.
    policy_state = _state()
    graphed = GraphedRuntime(
        instantiated=_instantiated(_loaded(_materialized(policy_state=policy_state, materialized_state=_state(version=2)))),
        graph=_graph(),
        graph_warnings=(),
    )
    finding = _IdentityFinding(
        node_id="passthrough",
        upstream_id="producer",
        sink_name="primary",
        sink_schema_mode="observed",
    )
    seen: list[CompositionState] = []

    def finder(state: CompositionState) -> list[_IdentityFinding]:
        seen.append(state)
        return [finding]

    checks = build_identity_advisory_checks(graphed, find_identity_node_advisories=finder)

    assert seen == [policy_state]
    assert seen[0] is policy_state
    assert len(checks) == 1
    assert checks[0].name == "identity_node_advisory"
    assert checks[0].affected_nodes == ("passthrough",)
    assert "between 'producer' and sink 'primary', which uses schema.mode: observed" in checks[0].detail
    assert "Consider removing it" in checks[0].detail


def test_gate_fan_out_advisory_checks_use_policy_state_and_preserve_exact_prose() -> None:
    # Same authored-vs-materialized discrimination as the identity sibling:
    # value-distinct states plus an identity assertion, so a silent switch to
    # the masked materialized state fails here rather than nowhere.
    policy_state = _state()
    graphed = GraphedRuntime(
        instantiated=_instantiated(_loaded(_materialized(policy_state=policy_state, materialized_state=_state(version=2)))),
        graph=_graph(),
        graph_warnings=(),
    )
    finding = _GateFanOutFinding(node_id="fanout", condition="True", destinations=("archive", "primary"))
    seen: list[CompositionState] = []

    def finder(state: CompositionState) -> list[_GateFanOutFinding]:
        seen.append(state)
        return [finding]

    checks = build_gate_fan_out_advisory_checks(graphed, find_gate_fan_out_advisories=finder)

    assert seen == [policy_state]
    assert seen[0] is policy_state
    assert len(checks) == 1
    assert checks[0].name == "gate_fan_out_advisory"
    assert checks[0].affected_nodes == ("fanout",)
    assert "constant condition 'True'" in checks[0].detail
    assert "both of its routes deliver to the same destinations ('archive', 'primary')" in checks[0].detail


def test_static_llm_prompt_advisory_checks_use_policy_state_and_preserve_exact_prose() -> None:
    policy_state = _state()
    graphed = GraphedRuntime(
        instantiated=_instantiated(_loaded(_materialized(policy_state=policy_state, materialized_state=_state(version=2)))),
        graph=_graph(),
        graph_warnings=(),
    )
    finding = _StaticLLMPromptFinding(node_id="classify")
    seen: list[CompositionState] = []

    def finder(state: CompositionState) -> list[_StaticLLMPromptFinding]:
        seen.append(state)
        return [finding]

    checks = build_static_llm_prompt_advisory_checks(graphed, find_static_llm_prompt_advisories=finder)

    assert seen == [policy_state]
    assert seen[0] is policy_state
    assert len(checks) == 1
    assert checks[0].name == "static_llm_prompt_advisory"
    assert checks[0].affected_nodes == ("classify",)
    assert "has a prompt_template that interpolates no row" in checks[0].detail
    assert "did you mean to use an LLM source instead?" in checks[0].detail


def test_runtime_carriers_snapshot_envelopes_but_preserve_executable_identities() -> None:
    materialized = _materialized()
    settings = _settings()
    loaded = LoadedRuntime(materialized=materialized, settings=settings)
    materialized.authored.semantic_contracts[0].outcome = "conflict"
    assert loaded.materialized is not materialized
    assert loaded.materialized.authored.semantic_contracts[0].outcome == "satisfied"
    assert loaded.settings is settings

    bundle = _bundle()
    instantiated = InstantiatedRuntime(loaded=loaded, bundle=bundle)
    loaded.materialized.authored.semantic_contracts[0].outcome = "conflict"
    assert instantiated.loaded is not loaded
    assert instantiated.loaded.materialized.authored.semantic_contracts[0].outcome == "satisfied"
    assert instantiated.loaded.settings is settings
    assert instantiated.bundle is bundle

    graph = _graph()
    warning = ValidationWarning(
        component_id="node",
        component_type="graph",
        message="original",
        suggestion=None,
        warning_code="WARN",
    )
    graphed = GraphedRuntime(instantiated=instantiated, graph=graph, graph_warnings=(warning,))
    instantiated.loaded.materialized.authored.semantic_contracts[0].outcome = "unknown"
    warning.message = "mutated"
    assert graphed.instantiated is not instantiated
    assert graphed.instantiated.loaded.materialized.authored.semantic_contracts[0].outcome == "satisfied"
    assert graphed.instantiated.loaded.settings is settings
    assert graphed.instantiated.bundle is bundle
    assert graphed.graph is graph
    assert graphed.graph_warnings[0].message == "original"
