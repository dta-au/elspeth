"""Direct tests for authored-state execution-validation phases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from elspeth.contracts.secrets import ResolvedSecret, SecretInventoryItem, SecretScope
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution._validation_authoring import (
    SecretValidatedState,
    _ParsedResourceLimit,
    lower_plugin_policy,
    review_interpretations,
    validate_batch_options,
    validate_path_policy,
    validate_secret_evidence,
    validate_semantic_evidence,
    validate_web_network_policy,
    validate_web_resource_policy,
)
from elspeth.web.execution._validation_model import (
    AuthoredValidatedState,
    InterpretationValidatedState,
    PhaseFailure,
    PhaseReport,
    PolicyLoweredState,
)
from elspeth.web.execution.schemas import SemanticEdgeContractResponse, ValidationError
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY, PROMPT_TEMPLATE_PARTS_KEY
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.secrets.service import ScopedSecretResolver


def _source(options: dict[str, object] | None = None, *, plugin: str = "csv", on_success: str = "node_in") -> SourceSpec:
    return SourceSpec(
        plugin=plugin,
        on_success=on_success,
        options=options or {},
        on_validation_failure="discard",
    )


def _node(
    *,
    node_id: str = "node",
    plugin: str = "value_transform",
    node_type: str = "transform",
    input_name: str = "node_in",
    on_success: str = "primary",
    options: dict[str, object] | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type=cast(Any, node_type),
        plugin=plugin,
        input=input_name,
        on_success=on_success,
        on_error="discard",
        options=options or {},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _output(options: dict[str, object] | None = None, *, name: str = "primary", plugin: str = "csv") -> OutputSpec:
    return OutputSpec(name=name, plugin=plugin, options=options or {}, on_write_failure="discard")


def _state(
    *,
    source: SourceSpec | None = None,
    nodes: tuple[NodeSpec, ...] = (),
    outputs: tuple[OutputSpec, ...] = (),
) -> CompositionState:
    return CompositionState(
        source=source or _source(),
        nodes=nodes,
        edges=(),
        outputs=outputs,
        metadata=PipelineMetadata(),
        version=1,
    )


def _trained_snapshot() -> PluginAvailabilitySnapshot:
    return PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())


def _web_snapshot() -> PluginAvailabilitySnapshot:
    unrestricted = _trained_snapshot()
    return PluginAvailabilitySnapshot.create(
        policy_hash="authoring-phase-test",
        principal_scope="local:alice",
        available=unrestricted.available,
        unavailable=(),
        selected=unrestricted.selected,
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="authoring-phase-test-generation",
    )


def _policy(state: CompositionState) -> PolicyLoweredState:
    return PolicyLoweredState(
        authored_state=state,
        state=state,
        profiled_s3_audit_identities=(),
        operator_resolved_model_node_ids=frozenset(),
    )


def _authored(state: CompositionState, *, contracts: tuple[SemanticEdgeContractResponse, ...] = ()) -> AuthoredValidatedState:
    return AuthoredValidatedState(
        policy=_policy(state),
        all_secret_refs=(),
        env_ref_names=frozenset(),
        semantic_contracts=contracts,
    )


def _contract() -> SemanticEdgeContractResponse:
    return SemanticEdgeContractResponse(
        from_id="producer",
        to_id="consumer",
        consumer_plugin="line_explode",
        producer_plugin="web_scrape",
        producer_field="content",
        consumer_field="content",
        outcome="satisfied",
        requirement_code="line_explode.source_field.line_framed_text",
    )


@dataclass
class _SecretService:
    available: frozenset[str]

    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        return [SecretInventoryItem(name=name, scope="user", available=True, reason=None) for name in sorted(self.available)]

    def has_ref(self, user_id: str, name: str) -> bool:
        return name in self.available

    def resolve(self, user_id: str, name: str) -> ResolvedSecret | None:
        if name not in self.available:
            return None
        return ResolvedSecret(name=name, value="test", scope="user", fingerprint="a" * 64)

    def resolve_scoped(self, user_id: str, name: str, scope: SecretScope) -> ResolvedSecret | None:
        del scope
        return self.resolve(user_id, name)


class _OwnedScopedSecretService(ScopedSecretResolver):
    def __init__(self, available: frozenset[str]) -> None:
        self.available = available

    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        return [SecretInventoryItem(name=name, scope="server", available=True, reason=None) for name in sorted(self.available)]

    def has_ref(self, user_id: str, name: str) -> bool:
        return name in self.available

    def resolve(self, user_id: str, name: str) -> ResolvedSecret | None:
        return self.resolve_scoped(user_id, name, "server")

    def resolve_scoped(self, user_id: str, name: str, scope: str) -> ResolvedSecret | None:
        if name not in self.available:
            return None
        return ResolvedSecret(name=name, value="test", scope=scope, fingerprint="a" * 64)


def test_scoped_secret_marker_rejects_miswired_owned_resolver_loudly() -> None:
    state = _state(source=_source({"api_key": {"secret_ref": "API_KEY", "secret_scope": "server"}}))

    with pytest.raises(TypeError, match="resolve_scoped"):
        validate_secret_evidence(
            _policy(state),
            secret_service=_SecretService(frozenset({"API_KEY"})),
            user_id="alice",
        )


def test_scoped_secret_marker_accepts_owned_nominal_resolver() -> None:
    state = _state(source=_source({"api_key": {"secret_ref": "API_KEY", "secret_scope": "server"}}))

    result = validate_secret_evidence(
        _policy(state),
        secret_service=_OwnedScopedSecretService(frozenset({"API_KEY"})),
        user_id="alice",
    )

    assert isinstance(result, PhaseReport)
    assert result.artifact.all_secret_refs == (("API_KEY", "server"),)


@pytest.mark.parametrize("bad_path", (123, {"nested": "path"}, "bad\x00path"))
def test_path_phase_returns_typed_failure_for_malformed_authored_paths(bad_path: object) -> None:
    result = validate_path_policy(
        _policy(_state(source=_source({"path": bad_path}))),
        data_dir=Path("/tmp/test_data"),
        session_id="test-session",
    )

    assert isinstance(result, PhaseFailure)
    assert result.failed_check.name == "path_allowlist"
    assert result.errors[0].component_id == "source"


def test_policy_lowering_returns_typed_state_and_four_canonical_checks() -> None:
    state = _state(outputs=(_output(),))

    report = lower_plugin_policy(
        state,
        plugin_snapshot=_trained_snapshot(),
        profile_registry=None,
        catalog=create_catalog_service(),
    )

    assert isinstance(report, PhaseReport)
    assert isinstance(report.artifact, PolicyLoweredState)
    assert report.artifact.state == state
    assert report.artifact.operator_resolved_model_node_ids == frozenset()
    assert [check.name for check in report.checks] == [
        "plugin_enablement",
        "operator_profile_options",
        "required_control_availability",
        "required_control_coverage",
    ]
    assert all(check.passed for check in report.checks)


def test_policy_artifact_composition_state_is_deeply_frozen_and_detached() -> None:
    caller_options: dict[str, object] = {"nested": {"values": ["original"]}}
    state = _state(source=_source(caller_options))
    report = PhaseReport(artifact=_policy(state), checks=())

    nested = cast(dict[str, object], caller_options["nested"])
    values = cast(list[str], nested["values"])
    values.append("mutated")

    frozen_nested = cast(dict[str, object], report.artifact.state.sources["source"].options["nested"])
    assert frozen_nested["values"] == ("original",)
    with pytest.raises(TypeError):
        report.artifact.state.sources["source"].options["nested"] = {"values": ()}


def test_authored_artifact_snapshots_semantic_contract_evidence() -> None:
    contract = _contract()

    authored = _authored(_state(), contracts=(contract,))
    contract.outcome = "conflict"

    assert authored.semantic_contracts[0].outcome == "satisfied"


@pytest.mark.parametrize(
    ("state", "detail", "affected_nodes", "component_id", "component_type"),
    [
        (
            _state(source=_source({"path": "/outside/source.csv"})),
            "Source 'source' path '/outside/source.csv' is outside allowed source directories",
            ("source",),
            "source",
            "source",
        ),
        (
            _state(outputs=(_output({"path": "/outside/sink.csv"}, name="sink"),)),
            "Sink 'sink' path '/outside/sink.csv' is outside allowed output directories",
            (),
            "sink",
            "sink",
        ),
        (
            _state(
                nodes=(
                    _node(
                        node_id="rag",
                        plugin="rag_retrieval",
                        options={"provider": "chroma", "provider_config": {"persist_directory": "/outside/chroma"}},
                    ),
                )
            ),
            "Transform 'rag' persist_directory '/outside/chroma' is outside allowed output directories",
            (),
            "rag",
            "transform",
        ),
    ],
    ids=("source", "sink", "nested-transform"),
)
def test_path_phase_preserves_each_failure_shape(
    state: CompositionState,
    detail: str,
    affected_nodes: tuple[str, ...],
    component_id: str,
    component_type: str,
) -> None:
    result = validate_path_policy(
        _policy(state),
        data_dir=Path("/tmp/test_data"),
        session_id="test-session",
    )

    assert isinstance(result, PhaseFailure)
    assert result.passed_checks == ()
    assert result.failed_check.name == "path_allowlist"
    assert result.failed_check.detail == detail
    assert result.failed_check.affected_nodes == affected_nodes
    assert result.errors[0].component_id == component_id
    assert result.errors[0].component_type == component_type
    assert result.readiness.blockers[0].code == "path_allowlist"


def test_web_network_phase_returns_typed_failure() -> None:
    state = _state(
        nodes=(
            _node(
                plugin="web_scrape",
                options={"http": {"allowed_hosts": "allow_private"}},
            ),
        )
    )

    result = validate_web_network_policy(_policy(state), plugin_snapshot=_web_snapshot())

    assert isinstance(result, PhaseFailure)
    assert result.failed_check.name == "web_scrape_network_policy"
    assert result.failed_check.affected_nodes == ("node",)
    assert result.errors[0].error_code == "web_scrape_private_network_not_allowed"


def test_web_resource_phase_returns_typed_failure() -> None:
    state = _state(
        nodes=(
            _node(
                plugin="blob_fetch",
                options={"http": {"timeout": 31, "max_body_bytes": 10 * 1024 * 1024 + 1}},
            ),
        )
    )

    result = validate_web_resource_policy(_policy(state), plugin_snapshot=_web_snapshot())

    assert isinstance(result, PhaseFailure)
    assert result.failed_check.name == "web_fetch_resource_policy"
    assert result.failed_check.affected_nodes == ("node", "node")
    assert [error.error_code for error in result.errors] == [
        "web_fetch_resource_limit_exceeded",
        "web_fetch_resource_limit_exceeded",
    ]


def test_resource_limit_outcome_rejects_ambiguous_state() -> None:
    error = ValidationError(
        component_id="node",
        component_type="transform",
        message="bad limit",
        suggestion=None,
        error_code="web_fetch_resource_config_invalid",
    )

    with pytest.raises(ValueError, match="exactly one"):
        _ParsedResourceLimit(value=None, error=None)
    with pytest.raises(ValueError, match="exactly one"):
        _ParsedResourceLimit(value=1, error=error)


def test_web_resource_phase_accumulates_conversion_and_limit_errors_in_option_order() -> None:
    state = _state(
        nodes=(
            _node(
                plugin="blob_fetch",
                options={"http": {"timeout": "not-an-int", "max_body_bytes": 10 * 1024 * 1024 + 1}},
            ),
        )
    )

    result = validate_web_resource_policy(_policy(state), plugin_snapshot=_web_snapshot())

    assert isinstance(result, PhaseFailure)
    assert [error.error_code for error in result.errors] == [
        "web_fetch_resource_config_invalid",
        "web_fetch_resource_limit_exceeded",
    ]


def test_secret_phase_returns_typed_evidence() -> None:
    state = _state(source=_source({"api_key": {"secret_ref": "API_KEY"}}))

    result = validate_secret_evidence(
        _policy(state),
        secret_service=_SecretService(frozenset({"API_KEY"})),
        user_id="alice",
    )

    assert isinstance(result, PhaseReport)
    assert isinstance(result.artifact, SecretValidatedState)
    assert result.artifact.all_secret_refs == (("API_KEY", None),)
    assert result.artifact.env_ref_names == frozenset({"API_KEY"})
    assert result.checks[0].name == "secret_refs"
    assert result.checks[0].outcome_code == "secret_refs.resolved"


def test_secret_phase_preserves_missing_reference_failure() -> None:
    state = _state(source=_source({"api_key": {"secret_ref": "MISSING"}}))

    result = validate_secret_evidence(
        _policy(state),
        secret_service=_SecretService(frozenset()),
        user_id="alice",
    )

    assert isinstance(result, PhaseFailure)
    assert result.failed_check.name == "secret_refs"
    assert result.failed_check.outcome_code == "secret_refs.unresolved"
    assert result.errors[0].error_code == "missing_secret_ref"


def test_semantic_phase_returns_serialized_evidence_on_failure() -> None:
    state = _web_scrape_line_explode_state()
    secret_state = SecretValidatedState(policy=_policy(state), all_secret_refs=(), env_ref_names=frozenset())

    result = validate_semantic_evidence(secret_state)

    assert isinstance(result, PhaseFailure)
    assert result.failed_check.name == "semantic_contracts"
    assert result.semantic_contracts
    assert result.semantic_contracts[0].consumer_plugin == "line_explode"
    assert result.semantic_contracts[0].outcome == "conflict"


def test_batch_phase_returns_typed_failure_with_semantic_evidence() -> None:
    batch_node = _node(
        node_id="stats",
        plugin="batch_stats",
        node_type="aggregation",
        input_name="stats",
        options={"value_field": "amount", "required_input_fields": ["amount"]},
    )
    state = _state(source=_source(on_success="stats"), nodes=(batch_node,), outputs=(_output(),))
    contract = _contract()

    result = validate_batch_options(_authored(state, contracts=(contract,)))

    assert isinstance(result, PhaseFailure)
    assert result.failed_check.name == "batch_transform_options"
    assert result.errors[0].component_id == "stats"
    assert "required_input_fields" in result.errors[0].message
    assert result.semantic_contracts == (contract,)


def test_interpretation_phase_preserves_pending_readiness_axes_and_semantics() -> None:
    state = _state(
        nodes=(
            _node(
                plugin="llm",
                options={
                    "prompt_template": "Rate pending interpretation: {{ row.text }}",
                    PROMPT_TEMPLATE_PARTS_KEY: [
                        {"kind": "text", "text": "Rate "},
                        {"kind": "interpretation_ref", "requirement_id": "coolness"},
                        {"kind": "text", "text": ": {{ row.text }}"},
                    ],
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "id": "coolness",
                            "kind": "vague_term",
                            "user_term": "coolness",
                            "status": "pending",
                            "draft": "well-designed and useful",
                            "event_id": "event-1",
                            "accepted_value": None,
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": None,
                        }
                    ],
                },
            ),
        )
    )
    contract = _contract()

    result = review_interpretations(_authored(state, contracts=(contract,)), allow_pending_placeholders=False)

    assert isinstance(result, PhaseFailure)
    assert result.failed_check.name == "interpretation_review"
    assert result.readiness.authoring_valid is True
    assert result.readiness.execution_ready is False
    assert result.readiness.completion_ready is True
    assert result.semantic_contracts == (contract,)
    assert all(error.error_code == "interpretation_review_pending" for error in result.errors)


def test_interpretation_phase_selects_authoring_materialized_state_without_mutating_policy_state() -> None:
    state = _state(
        nodes=(
            _node(
                plugin="llm",
                options={"prompt_template": "Rate how {{ interpretation: cool }} this row is."},
            ),
        )
    )

    result = review_interpretations(_authored(state), allow_pending_placeholders=True)

    assert isinstance(result, PhaseReport)
    assert isinstance(result.artifact, InterpretationValidatedState)
    assert result.artifact.materialized_state.nodes[0].options["prompt_template"] == "Rate how pending interpretation this row is."
    assert result.artifact.authored.policy.state.nodes[0].options["prompt_template"] == "Rate how {{ interpretation: cool }} this row is."
    assert result.checks[0].name == "interpretation_review"
    assert result.checks[0].passed is True


def _web_scrape_line_explode_state() -> CompositionState:
    return _state(
        source=_source(
            {
                "path": "/tmp/test_data/blobs/test-session/urls.txt",
                "column": "url",
                "schema": {"mode": "fixed", "fields": ["url: str"]},
            },
            plugin="text",
            on_success="scrape_in",
        ),
        nodes=(
            _node(
                node_id="scrape_page",
                plugin="web_scrape",
                input_name="scrape_in",
                on_success="explode_in",
                options={
                    "schema": {"mode": "flexible", "fields": ["url: str"]},
                    "required_input_fields": ["url"],
                    "url_field": "url",
                    "content_field": "content",
                    "fingerprint_field": "content_fingerprint",
                    "format": "text",
                    "fingerprint_mode": "content",
                    "http": {
                        "abuse_contact": "pipeline@example.com",
                        "scraping_reason": "test scrape",
                        "allowed_hosts": "public_only",
                    },
                },
            ),
            _node(
                node_id="split_lines",
                plugin="line_explode",
                input_name="explode_in",
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": ["url: str", "content: str", "content_fingerprint: str"],
                    },
                    "required_input_fields": ["content"],
                    "source_field": "content",
                    "output_field": "line",
                    "include_index": True,
                    "index_field": "line_index",
                },
            ),
        ),
        outputs=(_output({"path": "/tmp/test_data/outputs/test-session/lines.json"}, plugin="json"),),
    )
