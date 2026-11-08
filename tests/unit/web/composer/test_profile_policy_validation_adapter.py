"""Cross-seam contracts for profile-aware composer validation."""

from __future__ import annotations

from dataclasses import replace
from typing import NoReturn
from unittest.mock import patch

import pytest

from elspeth.contracts.plugin_capabilities import ControlMode
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.analysis import ComposerAnalysis
from elspeth.web.composer.session_cache import ComposerSessionCacheRegistry, SessionCacheKey
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec, ValidationSummary
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.plugin_policy.validation import (
    PluginPolicyFinding,
    PluginPolicyValidationResult,
    validate_authored_composition_state,
    validate_plugin_policy,
)
from tests.fixtures.composer_session_cache import count_composer_work

_SHIELD = PluginId("transform", "aws_bedrock_prompt_shield")
_PRIVATE_IDENTIFIER = "privateguardrailmarker"


class _AllSecretsInventory:
    def has_server_ref(self, name: str) -> bool:
        del name
        return True

    def has_user_ref(self, principal: str, name: str) -> bool:
        del principal, name
        return True

    def has_ref(self, principal: str, name: str) -> bool:
        del principal, name
        return True

    def server_generation(self, name: str) -> str:
        del name
        return "generation"

    def user_generation(self, principal: str, name: str) -> str:
        del principal, name
        return "generation"


def _context(
    *,
    control_mode: ControlMode = ControlMode.RECOMMEND,
) -> tuple[object, OperatorProfileRegistry, PluginAvailabilitySnapshot]:
    settings = WebSettings.model_validate(
        {
            "composer_max_composition_turns": 4,
            "composer_max_discovery_turns": 4,
            "composer_timeout_seconds": 60,
            "composer_rate_limit_per_minute": 20,
            "shareable_link_signing_key": b"0123456789abcdef0123456789abcdef",
            "plugin_allowlist": (str(_SHIELD),),
            "plugin_control_modes": {"prompt_shield": control_mode.value},
            "bedrock_guardrail_profiles": (
                {
                    "alias": "prompt-default",
                    "plugin": _SHIELD.name,
                    "guardrail_identifier": _PRIVATE_IDENTIFIER,
                    "guardrail_version": "7",
                    "region": "us-east-1",
                },
            ),
            "llm_profiles": {
                "llm-default": {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                    "region_name": "us-east-1",
                }
            },
            "default_llm_profile": "llm-default",
        }
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    catalog = create_catalog_service()
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=profiles,
        principal_scope="web:alice",
        secret_inventory=_AllSecretsInventory(),
        generation_key=b"profile-adapter-test-generation-key",
    )
    return catalog, profiles, snapshot


def _state(*, shield_options: dict[str, object] | None = None, include_shield: bool = True) -> CompositionState:
    nodes: tuple[NodeSpec, ...]
    source_success = "shield_in" if include_shield else "main"
    if include_shield:
        nodes = (
            NodeSpec(
                id="shield",
                node_type="transform",
                plugin=_SHIELD.name,
                input="shield_in",
                on_success="main",
                on_error="discard",
                options=shield_options
                or {
                    "profile": "prompt-default",
                    "fields": ["prompt"],
                    "schema": {"mode": "observed", "fields": None},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        )
    else:
        nodes = ()
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success=source_success,
            options={"path": "rows.csv", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=nodes,
        edges=(),
        outputs=(
            OutputSpec(
                name="main",
                plugin="json",
                options={"path": "out.jsonl", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _unshielded_llm_state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="llm_in",
            options={"path": "rows.csv", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="llm",
                node_type="transform",
                plugin="llm",
                input="llm_in",
                on_success="main",
                on_error="discard",
                options={
                    "profile": "llm-default",
                    "system_prompt": "You answer the prompt in each row. Reply concisely.",
                    "prompt_template": "{{ row['prompt'] }}",
                    "schema": {"mode": "observed", "fields": None},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="main",
                plugin="json",
                options={"path": "out.jsonl", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def test_profile_validation_reuses_authoritative_catalog_without_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog, profiles, snapshot = _context()

    def _unexpected_construction() -> NoReturn:
        raise AssertionError("validation constructed a second catalog")

    import elspeth.web.plugin_policy.validation as validation_module

    monkeypatch.setattr(validation_module, "create_catalog_service", _unexpected_construction, raising=False)
    result = validate_authored_composition_state(
        _state(),
        snapshot=snapshot,
        profile_registry=profiles,
        catalog=catalog,
    )

    assert result.validation.is_valid


def test_profile_validation_uses_executable_state_and_keeps_authored_alias() -> None:
    catalog, profiles, snapshot = _context()
    state = _state()

    result = validate_authored_composition_state(
        state,
        snapshot=snapshot,
        profile_registry=profiles,
        catalog=catalog,
    )

    assert result.authored_state is state
    assert result.authored_state.nodes[0].options == state.nodes[0].options
    assert result.authored_state.nodes[0].options["profile"] == "prompt-default"
    assert "guardrail_identifier" not in result.authored_state.nodes[0].options
    assert result.executable_state.nodes[0].options["guardrail_identifier"] == _PRIVATE_IDENTIFIER
    assert "profile" not in result.executable_state.nodes[0].options
    assert result.validation.is_valid


def test_web_profile_validation_fails_closed_for_empty_alias_registry() -> None:
    catalog, profiles, snapshot = _context()
    snapshot = replace(snapshot, usable_profile_aliases=(), selected_profile_aliases=())

    result = validate_authored_composition_state(
        _state(),
        snapshot=snapshot,
        profile_registry=profiles,
        catalog=catalog,
    )

    assert not result.validation.is_valid
    assert [(entry.component, entry.error_code) for entry in result.validation.errors] == [("node:shield", "profile_unavailable")]


def test_web_profile_validation_fails_closed_when_registry_is_missing() -> None:
    catalog, _profiles, snapshot = _context()

    result = validate_authored_composition_state(
        _state(),
        snapshot=snapshot,
        profile_registry=None,
        catalog=catalog,
    )

    assert not result.validation.is_valid
    assert [(entry.component, entry.error_code) for entry in result.validation.errors] == [("node:shield", "profile_unavailable")]


def _assert_profile_lowering_error_is_redacted_and_stable(
    monkeypatch: pytest.MonkeyPatch,
    exception: Exception,
    sentinel: str,
) -> None:
    catalog, profiles, snapshot = _context()

    def _fail(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise exception

    monkeypatch.setattr(profiles, "lower_options", _fail)
    result = validate_authored_composition_state(
        _state(),
        snapshot=snapshot,
        profile_registry=profiles,
        catalog=catalog,
    )

    assert not result.validation.is_valid
    assert result.validation.errors[0].error_code == "profile_unavailable"
    assert sentinel not in repr(result)


def test_profile_lowering_value_error_is_redacted_and_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_profile_lowering_error_is_redacted_and_stable(
        monkeypatch,
        ValueError("PRIVATE_SENTINEL_VALUE"),
        "PRIVATE_SENTINEL_VALUE",
    )


def test_profile_lowering_runtime_error_is_redacted_and_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_profile_lowering_error_is_redacted_and_stable(
        monkeypatch,
        RuntimeError("PRIVATE_SENTINEL_RUNTIME"),
        "PRIVATE_SENTINEL_RUNTIME",
    )


def test_required_control_coverage_does_not_block_incremental_authoring_validation() -> None:
    catalog, profiles, snapshot = _context(control_mode=ControlMode.REQUIRED)

    result = validate_authored_composition_state(
        _unshielded_llm_state(),
        snapshot=snapshot,
        profile_registry=profiles,
        catalog=catalog,
    )

    assert result.validation.is_valid
    assert result.policy_findings
    assert {finding.stage for finding in result.policy_findings} == {"required_control_coverage"}
    assert [warning.error_code for warning in result.validation.warnings if warning.error_code is not None] == ["required_control_coverage"]


def test_unrelated_pipeline_remains_valid_without_profile_aliases() -> None:
    catalog, profiles, snapshot = _context()
    snapshot = replace(snapshot, usable_profile_aliases=(), selected_profile_aliases=())

    result = validate_authored_composition_state(
        _state(include_shield=False),
        snapshot=snapshot,
        profile_registry=profiles,
        catalog=catalog,
    )

    assert result.validation.is_valid
    assert result.validation.errors == ()


def test_session_summary_reuse_keeps_every_policy_lowering_fresh() -> None:
    catalog, profiles, snapshot = _context()
    state = _state()
    expected = validate_authored_composition_state(state, snapshot=snapshot, profile_registry=profiles, catalog=catalog)
    caches = ComposerSessionCacheRegistry()
    with caches.borrow(SessionCacheKey("web:alice", "profile-session")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        view = PolicyCatalogView(catalog, snapshot, profiles, analysis=analysis)
        with patch.object(profiles, "lower_options", wraps=profiles.lower_options) as lower, count_composer_work() as work:
            first = view.validate_composition_state(state)
            second = view.validate_composition_state(state)
        assert first == second == expected
        assert lower.call_count == 2
        assert work.validation_walks == 1
        assert caches.snapshot().hits == 1
        assert lease.cache is not None
        retained = tuple((key, entry.value) for key, entry in lease.cache.validations._entries.items())
        assert len(retained) == 1
        assert _PRIVATE_IDENTIFIER not in repr(retained)
        assert "guardrail_identifier" not in repr(retained)
        assert isinstance(retained[0][1], ValidationSummary)
        assert retained[0][0].content_hash
        assert retained[0][0].profile_generation == profiles.generation


def test_profile_generation_and_snapshot_changes_separate_summaries() -> None:
    catalog, first_profiles, first_snapshot = _context()
    _other_catalog, second_profiles, changed_snapshot = _context(control_mode=ControlMode.REQUIRED)
    state = _state()
    caches = ComposerSessionCacheRegistry()
    with caches.borrow(SessionCacheKey("web:alice", "profile-session")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        with count_composer_work() as work:
            for snapshot, profiles in (
                (first_snapshot, first_profiles),
                (first_snapshot, first_profiles),
                (changed_snapshot, first_profiles),
                (changed_snapshot, second_profiles),
            ):
                result = validate_authored_composition_state(
                    state,
                    snapshot=snapshot,
                    profile_registry=profiles,
                    catalog=catalog,
                    analysis=analysis,
                    profile_generation=profiles.generation,
                )
                assert result.validation.is_valid
        assert first_snapshot.snapshot_hash != changed_snapshot.snapshot_hash
        assert first_profiles.generation != second_profiles.generation
        assert work.validation_walks == 3
        assert caches.snapshot().retained_entries == 3


def test_missing_profile_revision_bypasses_retention_without_hashing_lowered_state() -> None:
    catalog, profiles, snapshot = _context()
    state = _state()
    caches = ComposerSessionCacheRegistry()
    with caches.borrow(SessionCacheKey("web:alice", "profile-session")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        with count_composer_work() as work:
            for _ in range(2):
                result = validate_authored_composition_state(
                    state,
                    snapshot=snapshot,
                    profile_registry=profiles,
                    catalog=catalog,
                    analysis=analysis,
                )
                assert result.validation.is_valid
                assert result.executable_state._content_hash_memo is None
        assert work.validation_walks == 2
        assert caches.snapshot().retained_entries == 0


def test_evidence_and_blocking_findings_are_current_outside_retained_summary() -> None:
    catalog, profiles, snapshot = _context()
    state = _state()
    calls = [0]

    def current_policy(*args: object, **kwargs: object) -> PluginPolicyValidationResult:
        calls[0] += 1
        policy = validate_plugin_policy(state, snapshot=snapshot, profile_registry=profiles, catalog=catalog)
        if calls[0] < 3:
            finding = PluginPolicyFinding(
                stage="required_control_coverage",
                component_id=None,
                component_type=None,
                error_code="required_control_coverage",
                message=f"current evidence {calls[0]}",
            )
        else:
            finding = PluginPolicyFinding(
                stage="plugin_enablement",
                component_id="shield",
                component_type="transform",
                error_code="plugin_unavailable",
                message="current blocking finding",
            )
        return replace(policy, findings=(finding,))

    caches = ComposerSessionCacheRegistry()
    with caches.borrow(SessionCacheKey("web:alice", "profile-session")) as lease:
        view = PolicyCatalogView(catalog, snapshot, profiles, analysis=ComposerAnalysis(lease.cache))
        with (
            patch("elspeth.web.plugin_policy.validation.validate_plugin_policy", side_effect=current_policy),
            count_composer_work() as work,
        ):
            first = view.validate_composition_state(state)
            second = view.validate_composition_state(state)
            blocked = view.validate_composition_state(state)
        assert work.validation_walks == 1
        assert calls == [3]
        assert first.validation.warnings[0].message == "current evidence 1"
        assert second.validation.warnings[0].message == "current evidence 2"
        assert not blocked.validation.is_valid
        assert blocked.validation.errors[0].message == "current blocking finding"
        assert lease.cache is not None
        assert all(not entry.value.warnings for entry in lease.cache.validations._entries.values())


def test_lowered_langfuse_dependency_bypasses_shared_summary() -> None:
    catalog, profiles, snapshot = _context()
    state = _unshielded_llm_state()
    calls = [0]

    def current_policy(*args: object, **kwargs: object) -> PluginPolicyValidationResult:
        calls[0] += 1
        policy = validate_plugin_policy(state, snapshot=snapshot, profile_registry=profiles, catalog=catalog)
        node = policy.executable_state.nodes[0]
        private = replace(node, options={**node.options, "tracing": {"provider": "langfuse", "enabled": False}})
        return replace(policy, executable_state=replace(policy.executable_state, nodes=(private,)))

    caches = ComposerSessionCacheRegistry()
    with caches.borrow(SessionCacheKey("web:alice", "profile-session")) as lease:
        view = PolicyCatalogView(catalog, snapshot, profiles, analysis=ComposerAnalysis(lease.cache))
        with (
            patch("elspeth.web.plugin_policy.validation.validate_plugin_policy", side_effect=current_policy),
            patch.object(CompositionState, "validate", return_value=ValidationSummary(True, ())) as validate,
        ):
            view.validate_composition_state(state)
            view.validate_composition_state(state)
        assert calls == [2]
        assert validate.call_count == 2
        assert caches.snapshot().retained_entries == 0
        assert "tracing" not in state.nodes[0].options


def test_unlowered_policy_and_raw_consumers_share_same_analysis_key() -> None:
    catalog, profiles, snapshot = _context()
    state = _state(include_shield=False)
    caches = ComposerSessionCacheRegistry()
    with caches.borrow(SessionCacheKey("web:alice", "profile-session")) as lease:
        view = PolicyCatalogView(catalog, snapshot, profiles, analysis=ComposerAnalysis(lease.cache))
        with count_composer_work() as work:
            projected = view.validate_composition_state(state)
            assert view.validate_raw_state(state) == projected.validation
        assert work.validation_walks == 1
        assert caches.snapshot().retained_entries == 1
