"""Direct tests for validation materialization and provider-policy phases."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
import yaml

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.enums import CreationModality
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution._validation_materialization import (
    materialize_validation_yaml,
    validate_aws_s3_endpoint_url_policy,
    validate_aws_s3_source_policy,
    validate_llm_base_url_policy,
    validate_llm_retry_budget_policy,
    validate_llm_tracing_policy,
    validate_managed_identity_policy,
)
from elspeth.web.execution._validation_model import (
    AuthoredValidatedState,
    InterpretationValidatedState,
    MaterializedYaml,
    PhaseFailure,
    PhaseReport,
    PolicyLoweredState,
)
from elspeth.web.execution.schemas import SemanticEdgeContractResponse
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

_BLOB_ID = UUID("5b7a4e0e-9e4a-4f0b-8d3e-2c0e1f0d3a4b")
_BLOB_HASH = "a" * 64


class _YamlGenerator:
    def __init__(self, pipeline_yaml: str) -> None:
        self.pipeline_yaml = pipeline_yaml
        self.seen_state: CompositionState | None = None

    def generate_yaml(self, state: CompositionState) -> str:
        self.seen_state = state
        return self.pipeline_yaml


def _source(*, plugin: str = "csv", options: dict[str, object] | None = None) -> SourceSpec:
    return SourceSpec(plugin=plugin, on_success="node_in", options=options or {}, on_validation_failure="discard")


def _node(*, plugin: str, options: dict[str, object]) -> NodeSpec:
    return NodeSpec(
        id="node",
        node_type=cast(Any, "transform"),
        plugin=plugin,
        input="node_in",
        on_success="primary",
        on_error="discard",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _state(
    *,
    source: SourceSpec | None = None,
    nodes: tuple[NodeSpec, ...] = (),
) -> CompositionState:
    return CompositionState(
        source=source or _source(),
        nodes=nodes,
        edges=(),
        outputs=(OutputSpec(name="primary", plugin="csv", options={}, on_write_failure="discard"),),
        metadata=PipelineMetadata(),
        version=1,
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


def _interpretation(
    policy_state: CompositionState,
    *,
    materialized_state: CompositionState | None = None,
    contracts: tuple[SemanticEdgeContractResponse, ...] = (),
) -> InterpretationValidatedState:
    authored = AuthoredValidatedState(
        policy=PolicyLoweredState(state=policy_state, operator_resolved_model_node_ids=frozenset()),
        all_secret_refs=(),
        env_ref_names=frozenset(),
        semantic_contracts=contracts,
    )
    return InterpretationValidatedState(authored=authored, materialized_state=materialized_state or policy_state)


def _materialized(policy_state: CompositionState, *, materialized_state: CompositionState | None = None) -> MaterializedYaml:
    report = materialize_validation_yaml(
        _interpretation(policy_state, materialized_state=materialized_state, contracts=(_contract(),)),
        yaml_generator=_YamlGenerator("sources: {}\nsinks: {}\n"),
        data_dir=Path("/tmp/test_data"),
        session_id="test-session",
        blob_get_metadata=None,
        load_yaml=yaml.safe_load,
    )
    assert isinstance(report, PhaseReport)
    return report.artifact


def _web_snapshot() -> PluginAvailabilitySnapshot:
    unrestricted = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
    return PluginAvailabilitySnapshot.create(
        policy_hash="materialization-phase-test",
        principal_scope="local:alice",
        available=unrestricted.available,
        unavailable=(),
        selected=unrestricted.selected,
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="materialization-phase-test-generation",
    )


def _ready_blob() -> BlobRecord:
    return BlobRecord(
        id=_BLOB_ID,
        session_id=UUID("8cf34f4c-27c3-4c51-953a-f679852516a2"),
        filename="prompt.txt",
        mime_type="text/plain",
        size_bytes=32,
        content_hash=_BLOB_HASH,
        storage_path="/tmp/prompt.txt",
        created_at=datetime.now(UTC),
        created_by="user",
        source_description=None,
        status="ready",
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=None,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )


def _blob_config() -> dict[str, object]:
    return {
        "transforms": [
            {
                "name": "node",
                "options": {
                    "prompt_template": {
                        "blob_ref": str(_BLOB_ID),
                        "mode": "inline_content",
                        "sha256": _BLOB_HASH,
                    }
                },
            }
        ]
    }


def test_interpretation_state_detaches_authored_semantic_evidence_on_construction() -> None:
    authored = _interpretation(_state(), contracts=(_contract(),)).authored

    interpretation = InterpretationValidatedState(authored=authored, materialized_state=_state())
    authored.semantic_contracts[0].outcome = "conflict"

    assert interpretation.authored is not authored
    assert interpretation.authored.policy is authored.policy
    assert interpretation.authored.semantic_contracts[0].outcome == "satisfied"


def test_materialization_detaches_interpretation_semantic_evidence() -> None:
    interpretation = _interpretation(_state(), contracts=(_contract(),))

    result = materialize_validation_yaml(
        interpretation,
        yaml_generator=_YamlGenerator("sources: {}\nsinks: {}\n"),
        data_dir=Path("/tmp/test_data"),
        session_id="test-session",
        blob_get_metadata=None,
        load_yaml=yaml.safe_load,
    )
    assert isinstance(result, PhaseReport)
    interpretation.authored.semantic_contracts[0].outcome = "conflict"

    assert result.artifact.authored is not interpretation.authored
    assert result.artifact.authored.policy is interpretation.authored.policy
    assert result.artifact.authored.semantic_contracts[0].outcome == "satisfied"


def test_provider_pass_through_retains_detached_materialized_semantic_evidence() -> None:
    interpretation = _interpretation(_state(), contracts=(_contract(),))
    materialization = materialize_validation_yaml(
        interpretation,
        yaml_generator=_YamlGenerator("sources: {}\nsinks: {}\n"),
        data_dir=Path("/tmp/test_data"),
        session_id="test-session",
        blob_get_metadata=None,
        load_yaml=yaml.safe_load,
    )
    assert isinstance(materialization, PhaseReport)

    provider_report = validate_managed_identity_policy(materialization.artifact)
    assert isinstance(provider_report, PhaseReport)
    interpretation.authored.semantic_contracts[0].outcome = "conflict"

    assert provider_report.artifact.authored.semantic_contracts[0].outcome == "satisfied"


def test_materialization_uses_only_interpretation_state_and_preserves_exact_yaml() -> None:
    policy_state = _state(nodes=(_node(plugin="llm", options={"base_url": "https://evil.example/v1"}),))
    materialized_state = _state()
    generator = _YamlGenerator("sources: {}\nsinks: {}\n")

    result = materialize_validation_yaml(
        _interpretation(policy_state, materialized_state=materialized_state),
        yaml_generator=generator,
        data_dir=Path("/tmp/test_data"),
        session_id="test-session",
        blob_get_metadata=lambda _blob_id: None,
        load_yaml=yaml.safe_load,
    )

    assert isinstance(result, PhaseReport)
    assert result.artifact.materialized_state is materialized_state
    assert result.artifact.authored.policy.state is policy_state
    assert result.artifact.pipeline_yaml == "sinks: {}\nsources: {}\n"
    assert generator.seen_state is materialized_state
    assert [(check.name, check.detail) for check in result.checks] == [("blob_inline_refs", "No inline-content blob references found")]


def test_materialization_validates_blob_metadata_and_substitutes_exact_yaml() -> None:
    config = _blob_config()
    requested: list[UUID] = []

    def get_metadata(blob_id: UUID) -> BlobRecord:
        requested.append(blob_id)
        return _ready_blob()

    result = materialize_validation_yaml(
        _interpretation(_state()),
        yaml_generator=_YamlGenerator("blob_ref: inline_content\n"),
        data_dir=Path("/tmp/test_data"),
        session_id="test-session",
        blob_get_metadata=get_metadata,
        load_yaml=lambda pipeline_yaml: config,
    )

    assert isinstance(result, PhaseReport)
    expected_config = _blob_config()
    expected_config["transforms"][0]["options"]["prompt_template"] = "validated blob-backed inline content placeholder"  # type: ignore[index]
    assert result.artifact.pipeline_yaml == yaml.dump(expected_config, default_flow_style=False)
    assert result.checks[0].detail == "All inline-content blob references are valid"
    assert requested == [_BLOB_ID]


def test_materialization_blob_failure_preserves_diagnostics_and_semantic_evidence() -> None:
    result = materialize_validation_yaml(
        _interpretation(_state(), contracts=(_contract(),)),
        yaml_generator=_YamlGenerator("blob_ref: inline_content\n"),
        data_dir=Path("/tmp/test_data"),
        session_id="test-session",
        blob_get_metadata=lambda _blob_id: None,
        load_yaml=lambda pipeline_yaml: _blob_config(),
    )

    assert isinstance(result, PhaseFailure)
    assert result.passed_checks == ()
    assert result.failed_check.name == "blob_inline_refs"
    assert result.failed_check.detail == "node:node.options.prompt_template: missing"
    assert result.errors[0].component_id == "node"
    assert result.errors[0].component_type == "transform"
    assert result.errors[0].error_code == "missing_inline_blob_content"
    assert result.readiness.blockers[0].code == "blob_inline_refs"
    assert result.semantic_contracts == (_contract(),)


@pytest.mark.parametrize("stage", ["generate", "path", "load_yaml"])
def test_materialization_does_not_catch_yaml_path_or_bounded_yaml_exceptions(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExpectedError(RuntimeError):
        pass

    generator = _YamlGenerator("blob_ref: inline_content\n" if stage == "load_yaml" else "sources: {}\n")
    if stage == "generate":
        monkeypatch.setattr(generator, "generate_yaml", lambda state: (_ for _ in ()).throw(ExpectedError("generate")))
    if stage == "path":
        monkeypatch.setattr(
            "elspeth.web.execution._validation_materialization.resolve_runtime_yaml_paths",
            lambda pipeline_yaml, data_dir, *, session_id: (_ for _ in ()).throw(ExpectedError("path")),
        )

    with pytest.raises(ExpectedError, match=stage):
        materialize_validation_yaml(
            _interpretation(_state()),
            yaml_generator=generator,
            data_dir=Path("/tmp/test_data"),
            session_id="test-session",
            blob_get_metadata=lambda _blob_id: None,
            load_yaml=lambda pipeline_yaml: (_ for _ in ()).throw(ExpectedError("load_yaml")),
        )


def test_materialization_rejects_non_dict_yaml_as_an_uncaught_invariant() -> None:
    with pytest.raises(TypeError, match=r"generate_yaml\(\) produced non-dict YAML"):
        materialize_validation_yaml(
            _interpretation(_state()),
            yaml_generator=_YamlGenerator("blob_ref: inline_content\n"),
            data_dir=Path("/tmp/test_data"),
            session_id="test-session",
            blob_get_metadata=lambda _blob_id: None,
            load_yaml=lambda pipeline_yaml: ["not", "a", "dict"],
        )


@pytest.mark.parametrize(
    ("phase_name", "policy_state", "check_name", "error_code"),
    [
        (
            "managed",
            _state(
                nodes=(
                    _node(
                        plugin="rag_retrieval",
                        options={"provider": "azure_search", "provider_config": {"use_managed_identity": True}},
                    ),
                )
            ),
            "managed_identity_policy",
            None,
        ),
        ("retry", _state(nodes=(_node(plugin="llm", options={"queries": [{}]}),)), "llm_retry_budget_policy", None),
        (
            "base_url",
            _state(nodes=(_node(plugin="llm", options={"base_url": "https://evil.example/v1"}),)),
            "llm_base_url_policy",
            "llm_base_url_not_allowed",
        ),
        (
            "tracing",
            _state(nodes=(_node(plugin="llm", options={"tracing": {"endpoint": "https://trace.example"}}),)),
            "llm_tracing_policy",
            "llm_tracing_not_allowed",
        ),
        (
            "aws_endpoint",
            _state(source=_source(plugin="aws_s3", options={"endpoint_url": "https://storage.example"})),
            "aws_s3_endpoint_url_policy",
            "aws_s3_endpoint_url_not_allowed",
        ),
        ("aws_source", _state(source=_source(plugin="aws_s3")), "aws_s3_source_policy", "aws_s3_source_not_allowed"),
    ],
)
def test_provider_policy_phases_read_authored_policy_and_preserve_failure_evidence(
    phase_name: str,
    policy_state: CompositionState,
    check_name: str,
    error_code: str | None,
) -> None:
    artifact = _materialized(policy_state, materialized_state=_state())
    snapshot = _web_snapshot()
    if phase_name == "managed":
        result = validate_managed_identity_policy(artifact)
    elif phase_name == "retry":
        result = validate_llm_retry_budget_policy(artifact)
    elif phase_name == "base_url":
        result = validate_llm_base_url_policy(artifact)
    elif phase_name == "tracing":
        result = validate_llm_tracing_policy(artifact)
    elif phase_name == "aws_endpoint":
        result = validate_aws_s3_endpoint_url_policy(artifact, plugin_snapshot=snapshot)
    else:
        result = validate_aws_s3_source_policy(artifact, plugin_snapshot=snapshot)

    assert isinstance(result, PhaseFailure)
    assert result.passed_checks == ()
    assert result.failed_check.name == check_name
    assert result.errors[0].error_code == error_code
    assert result.readiness.blockers[0].code == check_name
    artifact.authored.semantic_contracts[0].outcome = "conflict"
    assert result.semantic_contracts == (_contract(),)


@pytest.mark.parametrize(
    ("phase_name", "check_name"),
    [
        ("managed", "managed_identity_policy"),
        ("retry", "llm_retry_budget_policy"),
        ("base_url", "llm_base_url_policy"),
        ("tracing", "llm_tracing_policy"),
        ("aws_endpoint", "aws_s3_endpoint_url_policy"),
        ("aws_source", "aws_s3_source_policy"),
    ],
)
def test_provider_policy_successes_detach_materialized_semantic_evidence(phase_name: str, check_name: str) -> None:
    artifact = _materialized(_state())
    snapshot = _web_snapshot()
    if phase_name == "managed":
        result = validate_managed_identity_policy(artifact)
    elif phase_name == "retry":
        result = validate_llm_retry_budget_policy(artifact)
    elif phase_name == "base_url":
        result = validate_llm_base_url_policy(artifact)
    elif phase_name == "tracing":
        result = validate_llm_tracing_policy(artifact)
    elif phase_name == "aws_endpoint":
        result = validate_aws_s3_endpoint_url_policy(artifact, plugin_snapshot=snapshot)
    else:
        result = validate_aws_s3_source_policy(artifact, plugin_snapshot=snapshot)

    assert isinstance(result, PhaseReport)
    artifact.authored.semantic_contracts[0].outcome = "conflict"

    assert result.artifact is not artifact
    assert result.artifact.authored.policy is artifact.authored.policy
    assert result.artifact.materialized_state is artifact.materialized_state
    assert result.artifact.pipeline_yaml == artifact.pipeline_yaml
    assert result.artifact.authored.semantic_contracts[0].outcome == "satisfied"
    assert [(check.name, check.passed) for check in result.checks] == [(check_name, True)]
