"""Profile-aware prevalidation parity for Composer source mutation seams.

The LLM source is deliberately registered only in this test's isolated plugin
manager until Task 10 publishes built-in discovery.  These tests exercise the
same ``PolicyCatalogView`` and Task 8 operator-profile resolver used by the web
runtime while proving that Composer persists only the authored, audit-safe
source options.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.plugins.sinks.json_sink import JSONSink
from elspeth.plugins.sources.llm.source import LLMSource
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.composer.tools.sessions import build_set_pipeline_candidate
from elspeth.web.composer.tools.sources import _execute_patch_source_options, _execute_set_source
from elspeth.web.config import WebSettings
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId, WebPluginPolicy
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

_LLM_SOURCE = PluginId("source", "llm")
_JSON_SINK = PluginId("sink", "json")
_PROFILE_ALIAS = "briefing-role"


@dataclass(frozen=True)
class _ProfiledSourceHarness:
    context: ToolContext
    empty_state: CompositionState
    catalog: CatalogServiceImpl
    profiles: OperatorProfileRegistry
    policy: WebPluginPolicy


@pytest.fixture
def profiled_source_harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_ProfiledSourceHarness]:
    """Build one restricted web-policy view without enabling discovery."""
    manager = PluginManager()
    manager.register(create_dynamic_hookimpl([LLMSource], "elspeth_get_source"))
    manager.register(create_dynamic_hookimpl([JSONSink], "elspeth_get_sinks"))
    monkeypatch.setattr("elspeth.plugins.infrastructure.manager.get_shared_plugin_manager", lambda: manager)

    catalog = CatalogServiceImpl(manager)
    settings = WebSettings.model_validate(
        {
            "composer_max_composition_turns": 4,
            "composer_max_discovery_turns": 4,
            "composer_timeout_seconds": 60,
            "composer_rate_limit_per_minute": 20,
            "shareable_link_signing_key": b"0123456789abcdef0123456789abcdef",
            "llm_profiles": {
                _PROFILE_ALIAS: {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                    "region_name": "us-east-1",
                }
            },
            "default_llm_profile": _PROFILE_ALIAS,
        }
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = WebPluginPolicy.create(
        required=frozenset({_JSON_SINK}),
        configured_optional=frozenset({_LLM_SOURCE}),
        preferences=(),
        control_modes=(),
        plugin_code_identities=(),
    )
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash=policy.policy_hash,
        principal_scope="web:alice",
        available=frozenset({_LLM_SOURCE, _JSON_SINK}),
        unavailable=(),
        selected=(),
        usable_profile_aliases=((_LLM_SOURCE, (_PROFILE_ALIAS,)),),
        selected_profile_aliases=((_LLM_SOURCE, _PROFILE_ALIAS),),
        binding_generation_fingerprint="profiled-source-prevalidation",
    )
    context = ToolContext(
        catalog=PolicyCatalogView(catalog, snapshot, profiles),
        plugin_snapshot=snapshot,
    )
    yield _ProfiledSourceHarness(
        context=context,
        empty_state=CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        ),
        catalog=catalog,
        profiles=profiles,
        policy=policy,
    )


def _authored_options(**overrides: object) -> dict[str, object]:
    return {
        "profile": _PROFILE_ALIAS,
        "prompt_template": "Write one concise audit briefing.",
        "response_field": "briefing",
        "schema": {"mode": "observed"},
        **overrides,
    }


def _source_args(
    *,
    source_name: str = "briefing",
    options: dict[str, object] | None = None,
    on_validation_failure: str = "discard",
) -> dict[str, Any]:
    return {
        "source_name": source_name,
        "plugin": "llm",
        "on_success": "generated",
        "on_validation_failure": on_validation_failure,
        "options": _authored_options() if options is None else options,
    }


def _output() -> dict[str, object]:
    return {
        "sink_name": "generated",
        "plugin": "json",
        "options": {
            "path": "outputs/generated.jsonl",
            "schema": {"mode": "observed"},
            "mode": "write",
            "collision_policy": "auto_increment",
        },
        "on_write_failure": "discard",
    }


def _assert_audit_safe_source_options(options: Mapping[str, object]) -> None:
    assert dict(options) == _authored_options()
    for private_name in ("provider", "model", "api_key", "region_name", "profile_alias"):
        assert private_name not in options


def test_set_source_lowers_profile_transiently_for_arbitrary_source_name(
    profiled_source_harness: _ProfiledSourceHarness,
) -> None:
    result = _execute_set_source(
        _source_args(source_name="daily_briefing"),
        profiled_source_harness.empty_state,
        profiled_source_harness.context,
    )

    assert result.success is True, result.data
    source = result.updated_state.sources["daily_briefing"]
    _assert_audit_safe_source_options(source.options)
    assert source.on_validation_failure == "discard"


def test_set_source_prevalidation_does_not_require_an_unrelated_sink_plugin(
    profiled_source_harness: _ProfiledSourceHarness,
) -> None:
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash=profiled_source_harness.policy.policy_hash,
        principal_scope="web:alice",
        available=frozenset({_LLM_SOURCE}),
        unavailable=(),
        selected=(),
        usable_profile_aliases=((_LLM_SOURCE, (_PROFILE_ALIAS,)),),
        selected_profile_aliases=((_LLM_SOURCE, _PROFILE_ALIAS),),
        binding_generation_fingerprint="profiled-source-without-sink",
    )
    context = ToolContext(
        catalog=PolicyCatalogView(
            profiled_source_harness.catalog,
            snapshot,
            profiled_source_harness.profiles,
        ),
        plugin_snapshot=snapshot,
    )

    result = _execute_set_source(
        _source_args(),
        profiled_source_harness.empty_state,
        context,
    )

    assert result.success is True, result.data
    _assert_audit_safe_source_options(result.updated_state.sources["briefing"].options)


def test_set_source_validates_non_discard_route_without_duplicating_it_in_options(
    profiled_source_harness: _ProfiledSourceHarness,
) -> None:
    result = _execute_set_source(
        _source_args(on_validation_failure="quarantine"),
        profiled_source_harness.empty_state,
        profiled_source_harness.context,
    )

    assert result.success is True, result.data
    source = result.updated_state.sources["briefing"]
    assert source.on_validation_failure == "quarantine"
    assert "on_validation_failure" not in source.options
    _assert_audit_safe_source_options(source.options)


def test_set_source_rejects_conflicting_duplicate_validation_failure_route(
    profiled_source_harness: _ProfiledSourceHarness,
) -> None:
    result = _execute_set_source(
        _source_args(
            on_validation_failure="quarantine",
            options=_authored_options(on_validation_failure="discard"),
        ),
        profiled_source_harness.empty_state,
        profiled_source_harness.context,
    )

    assert result.success is False
    assert result.updated_state is profiled_source_harness.empty_state
    assert "on_validation_failure" in str(result.data)
    assert "conflicts" in str(result.data)


def test_patch_source_options_revalidates_profile_without_persisting_private_binding(
    profiled_source_harness: _ProfiledSourceHarness,
) -> None:
    created = _execute_set_source(
        _source_args(),
        profiled_source_harness.empty_state,
        profiled_source_harness.context,
    )
    assert created.success is True, created.data

    patched = _execute_patch_source_options(
        {"source_name": "briefing", "patch": {"temperature": 0.2}},
        created.updated_state,
        profiled_source_harness.context,
    )

    assert patched.success is True, patched.data
    source = patched.updated_state.sources["briefing"]
    assert dict(source.options) == _authored_options(temperature=0.2)
    assert source.on_validation_failure == "discard"
    for private_name in ("provider", "model", "api_key", "region_name", "profile_alias"):
        assert private_name not in source.options


@pytest.mark.parametrize("container", ["source", "sources"])
def test_set_pipeline_profiled_source_is_audit_safe_for_singular_and_plural_session_flows(
    profiled_source_harness: _ProfiledSourceHarness,
    container: str,
) -> None:
    source = {
        "plugin": "llm",
        "on_success": "generated",
        "on_validation_failure": "discard",
        "options": _authored_options(),
    }
    source_block: dict[str, object] = {"source": source} if container == "source" else {"sources": {"daily_briefing": source}}
    candidate = build_set_pipeline_candidate(
        {
            **source_block,
            "nodes": [],
            "edges": [],
            "outputs": [_output()],
            "metadata": {"name": "Profiled source", "description": ""},
        },
        profiled_source_harness.empty_state,
        profiled_source_harness.context,
    )

    assert candidate.acceptable is True, candidate.result.validation
    source_name = "source" if container == "source" else "daily_briefing"
    persisted = candidate.result.updated_state.sources[source_name]
    _assert_audit_safe_source_options(persisted.options)
    assert persisted.on_validation_failure == "discard"


def test_set_source_rejects_unknown_profile_alias_atomically(
    profiled_source_harness: _ProfiledSourceHarness,
) -> None:
    result = _execute_set_source(
        _source_args(options=_authored_options(profile="missing-role")),
        profiled_source_harness.empty_state,
        profiled_source_harness.context,
    )

    assert result.success is False
    assert result.updated_state is profiled_source_harness.empty_state
    assert "profile_unavailable" in str(result.data)


@pytest.mark.parametrize(
    ("private_name", "private_value"),
    [
        ("provider", "openrouter"),
        ("api_key", "author-controlled-secret"),
        ("profile_alias", "forged-alias"),
    ],
)
def test_set_pipeline_rejects_private_profile_binding_fields_atomically(
    profiled_source_harness: _ProfiledSourceHarness,
    private_name: str,
    private_value: str,
) -> None:
    candidate = build_set_pipeline_candidate(
        {
            "sources": {
                "daily_briefing": {
                    "plugin": "llm",
                    "on_success": "generated",
                    "on_validation_failure": "discard",
                    "options": _authored_options(**{private_name: private_value}),
                }
            },
            "nodes": [],
            "edges": [],
            "outputs": [_output()],
        },
        profiled_source_harness.empty_state,
        profiled_source_harness.context,
    )

    assert candidate.acceptable is False
    assert candidate.result.updated_state is profiled_source_harness.empty_state
    assert candidate.result.validation.errors
