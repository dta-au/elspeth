"""Detached policy parity and authority controls through real profile resolvers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.plugin_policy.validation import validate_authored_composition_state
from elspeth.web.sessions.interpretation_validation import (
    SessionInterpretationValidationInputs,
    build_interpretation_validation_inputs,
    validate_composition_state_with_interpretation_inputs,
)


def _context() -> tuple[CatalogServiceImpl, OperatorProfileRegistry, PluginAvailabilitySnapshot]:
    settings = WebSettings.model_validate(
        {
            "composer_max_composition_turns": 4,
            "composer_max_discovery_turns": 4,
            "composer_timeout_seconds": 60,
            "composer_rate_limit_per_minute": 20,
            "shareable_link_signing_key": b"0123456789abcdef0123456789abcdef",
            "plugin_allowlist": [
                "transform:llm",
                "source:aws_s3",
                "transform:azure_ai_search",
                "transform:aws_textract_document_analysis",
                "transform:aws_bedrock_prompt_shield",
            ],
            "deployment_aws_region": "ap-southeast-1",
            "llm_profiles": {
                "text-model": {
                    "provider": "openrouter",
                    "model": "openai/gpt-5-mini",
                    "credential_scope": "server",
                    "credential_ref": "PROFILE_TEST_KEY",
                }
            },
            "default_llm_profile": "text-model",
            "aws_s3_source_profiles": [{"alias": "storage", "bucket": "private-bucket", "prefix": "private/prefix"}],
            "aws_textract_profiles": [{"alias": "documents", "bucket": "private-bucket", "key_prefix": "private/documents"}],
            "bedrock_guardrail_profiles": [
                {
                    "alias": "shield",
                    "plugin": "aws_bedrock_prompt_shield",
                    "guardrail_identifier": "abcdef1234",
                    "guardrail_version": "1",
                    "region": "ap-southeast-1",
                }
            ],
            "azure_search_profiles": [
                {
                    "alias": "search",
                    "endpoint": "https://private-search.search.windows.net",
                    "auth": "api_key",
                    "credential_ref": "SEARCH_TEST_KEY",
                    "indexes": ["admitted"],
                }
            ],
        }
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    catalog = create_catalog_service()
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
    registry = OperatorProfileRegistry(policy=policy, settings=runtime)
    inventory = (
        (PluginId("source", "aws_s3"), ("storage",)),
        (PluginId("transform", "azure_ai_search"), ("search",)),
        (PluginId("transform", "aws_textract_document_analysis"), ("documents",)),
        (PluginId("transform", "aws_bedrock_prompt_shield"), ("shield",)),
        (PluginId("transform", "llm"), ("text-model",)),
    )
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash=policy.policy_hash,
        principal_scope="user:alice",
        available=frozenset(plugin_id for plugin_id, _aliases in inventory),
        unavailable=(),
        selected=(),
        usable_profile_aliases=inventory,
        selected_profile_aliases=(),
        binding_generation_fingerprint="captured-profile-generation",
    )
    return catalog, registry, snapshot


def _capture(
    catalog: CatalogServiceImpl, registry: OperatorProfileRegistry, snapshot: PluginAvailabilitySnapshot
) -> SessionInterpretationValidationInputs:
    return build_interpretation_validation_inputs(profile_aware=True, plugin_snapshot=snapshot, profile_registry=registry, catalog=catalog)


@pytest.mark.parametrize(
    ("plugin_id", "alias", "options"),
    [
        (PluginId("transform", "llm"), "text-model", {"queries": [{"name": "first", "prompt_template": "{{ row.text }}"}]}),
        (PluginId("source", "aws_s3"), "storage", {"key": "in/rows.csv", "on_validation_failure": "discard"}),
        (PluginId("transform", "aws_textract_document_analysis"), "documents", {"key_field": "object_key"}),
        (PluginId("transform", "azure_ai_search"), "search", {"index": "admitted", "query_field": "text"}),
        (PluginId("transform", "aws_bedrock_prompt_shield"), "shield", {"fields": "all"}),
    ],
)
def test_detached_bindings_preserve_dynamic_lowering_and_audit_identities(
    plugin_id: PluginId, alias: str, options: dict[str, object]
) -> None:
    catalog, registry, snapshot = _context()
    inputs = _capture(catalog, registry, snapshot)
    expected = registry.lower_options(plugin_id, alias=alias, safe_options=options)
    detached = inputs.lower_options(plugin_id, alias=alias, safe_options=options)
    assert detached == expected
    registry._resolvers.clear()
    assert inputs.lower_options(plugin_id, alias=alias, safe_options=options) == expected
    assert inputs.plugin_snapshot == snapshot
    assert "private-bucket" not in repr(inputs)
    assert "private-search" not in repr(inputs)
    with pytest.raises(FrozenInstanceError):
        inputs.snapshot_json = None


@pytest.mark.parametrize(
    ("plugin_id", "alias", "options", "error"),
    [
        (PluginId("source", "aws_s3"), "storage", {"key": "../escape.csv"}, "unsafe_s3_object_key"),
        (PluginId("transform", "azure_ai_search"), "search", {"index": "denied"}, "profile_index_not_admitted"),
        (PluginId("transform", "llm"), "text-model", {"model": "forged"}, "private_profile_option"),
    ],
)
def test_detached_bindings_preserve_option_dependent_rejections(
    plugin_id: PluginId, alias: str, options: dict[str, object], error: str
) -> None:
    catalog, registry, snapshot = _context()
    inputs = _capture(catalog, registry, snapshot)
    with pytest.raises(ValueError, match=error):
        registry.lower_options(plugin_id, alias=alias, safe_options=options)
    with pytest.raises(ValueError, match=error):
        inputs.lower_options(plugin_id, alias=alias, safe_options=options)


def test_detached_policy_validation_matches_live_validation_concurrently() -> None:
    catalog, registry, snapshot = _context()
    inputs = _capture(catalog, registry, snapshot)
    state = CompositionState(
        metadata=PipelineMetadata(name="Detached"),
        nodes=(
            NodeSpec(
                id="search",
                node_type="transform",
                plugin="azure_ai_search",
                options={
                    "profile": "search",
                    "index": "denied",
                    "query_field": "question",
                    "output_prefix": "search",
                    "schema": {"mode": "observed"},
                },
                input="rows",
                on_success="results",
                on_error="discard",
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        version=1,
    )
    expected = validate_authored_composition_state(state, snapshot=snapshot, profile_registry=registry, catalog=catalog)
    assert [finding.error_code for finding in expected.policy_findings] == ["plugin_options_invalid"]
    assert expected.policy_findings[0].message.endswith("['index'].")
    admitted = replace(state, nodes=(replace(state.nodes[0], options={**state.nodes[0].options, "index": "admitted"}),))
    admitted_expected = validate_authored_composition_state(admitted, snapshot=snapshot, profile_registry=registry, catalog=catalog)
    assert admitted_expected.policy_findings == ()
    registry._resolvers.clear()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(lambda candidate: validate_composition_state_with_interpretation_inputs(candidate, inputs), (state, admitted) * 4)
        )
    assert results[::2] == [expected] * 4
    assert results[1::2] == [admitted_expected] * 4


def test_detached_source_policy_validation_matches_dynamic_key_and_route_lowering() -> None:
    catalog, registry, snapshot = _context()
    inputs = _capture(catalog, registry, snapshot)
    state = CompositionState(
        metadata=PipelineMetadata(name="Source"),
        nodes=(),
        edges=(),
        outputs=(),
        version=1,
        sources={
            "primary": SourceSpec(
                plugin="aws_s3",
                options={"profile": "storage", "key": "in/rows.csv", "format": "csv", "schema": {"mode": "observed"}},
                on_success="results",
                on_validation_failure="discard",
            )
        },
    )
    expected = validate_authored_composition_state(state, snapshot=snapshot, profile_registry=registry, catalog=catalog)
    assert expected.policy_findings == ()
    assert deep_thaw(expected.executable_state.sources["primary"].options)["key"] == "private/prefix/in/rows.csv"
    assert validate_composition_state_with_interpretation_inputs(state, inputs) == expected


def test_detachment_rejects_non_nominal_services_before_traversal() -> None:
    class Carrier:
        def __getattribute__(self, name: str) -> Any:
            raise AssertionError(f"carrier must not be traversed: {name}")

    with pytest.raises(TypeError, match="catalog must be an exact CatalogServiceImpl"):
        build_interpretation_validation_inputs(profile_aware=False, plugin_snapshot=None, profile_registry=None, catalog=Carrier())
    with pytest.raises(TypeError, match="profile_registry must be an exact OperatorProfileRegistry"):
        build_interpretation_validation_inputs(profile_aware=False, plugin_snapshot=None, profile_registry=Carrier(), catalog=None)
    with pytest.raises(TypeError, match="snapshot must be an exact JSON string"):
        SessionInterpretationValidationInputs(Carrier(), ())


def test_detachment_rejects_corrupted_snapshot_authority() -> None:
    catalog, registry, snapshot = _context()
    with pytest.raises(AuditIntegrityError, match="invalid canonical authority"):
        _capture(catalog, registry, replace(snapshot, snapshot_hash="forged"))


def test_unconfigured_validation_and_trained_operator_keep_existing_semantics() -> None:
    catalog, registry, _snapshot = _context()
    state = CompositionState(metadata=PipelineMetadata(name="Empty"), nodes=(), edges=(), outputs=(), version=1)
    unconfigured = build_interpretation_validation_inputs(profile_aware=False, plugin_snapshot=None, profile_registry=None, catalog=None)
    assert unconfigured.plugin_snapshot is None
    assert validate_composition_state_with_interpretation_inputs(state, unconfigured).validation == state.validate()
    trained = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    inputs = _capture(catalog, registry, trained)
    assert inputs.plugins == ()
    assert validate_composition_state_with_interpretation_inputs(state, inputs) == validate_authored_composition_state(
        state, snapshot=trained, profile_registry=registry, catalog=catalog
    )
