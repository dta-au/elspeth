"""Mocked provider requests preserve operator sampling through readiness."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.errors import RuntimePreflightFailedError
from elspeth.core.config import _lower_llm_profile_node_options
from elspeth.core.llm_profiles import LLMProfileSettings, RuntimeLLMProfile
from elspeth.plugins.infrastructure.clients.llm import LLMClientError
from elspeth.plugins.sources.llm import LLMSource
from elspeth.plugins.transforms.llm.provider import LLMAuditParent
from elspeth.plugins.transforms.llm.transform import LLMTransform
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.application_policy import PluginPolicyContextFactory
from elspeth.web.composer.composer_preflight import ComposerPreflight
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.execution.runtime_preflight import RuntimePreflightCoordinator
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import _LLMProfileResolver
from elspeth.web.secrets.wiring_policy import SecretWiringPolicy
from tests.fixtures.factories import make_context, make_operation_context
from tests.fixtures.llm_sampling import _Harness
from tests.unit.web.composer.conftest import _make_settings


@pytest.mark.parametrize("temperature", ["omitted", None, 0.0, 0.7])
def test_transform_preflight_matches_normal_completion_sampling(harness: _Harness, temperature: str | float | None) -> None:
    config = dict(harness.config)
    if temperature != "omitted":
        config["temperature"] = temperature
    transform = LLMTransform(config)
    transform._provider = harness.provider
    ctx = make_context()
    ctx.operation_id = "sampling-preflight"
    resolved = transform.provider_config.temperature
    transform.runtime_preflight(ctx)
    harness.provider.execute_query(
        [ChatMessage(role="user", content="hello")],
        model=transform.provider_config.model,
        temperature=resolved,
        max_tokens=64,
        audit_parent=LLMAuditParent.for_operation(operation_id=ctx.operation_id, coordination_token=ctx.require_coordination_token()),
    )
    assert len(harness.requests) == 2
    for payload in harness.requests:
        if resolved is None:
            assert "temperature" not in payload
        else:
            assert payload["temperature"] == resolved
    expected = None if temperature == "omitted" else temperature
    assert resolved == expected


@pytest.mark.parametrize("temperature", [None, 0.0, 0.7])
def test_source_normal_completion_preserves_sampling(harness: _Harness, temperature: float | None, monkeypatch: pytest.MonkeyPatch) -> None:
    config = {**harness.config, "temperature": temperature, "on_validation_failure": "discard"}
    source = LLMSource(config)
    monkeypatch.setattr(source, "_create_provider", lambda recorder, llm_call_governance=None: harness.provider)
    ctx = make_operation_context(plugin_name="llm")
    source.on_start(ctx)
    try:
        assert len(list(source.load(ctx))) == 1
    finally:
        source.close()
    assert len(harness.requests) == 1
    if temperature is None:
        assert "temperature" not in harness.requests[0]
    else:
        assert harness.requests[0]["temperature"] == temperature


@pytest.mark.parametrize("temperature", [None, 0.0, 0.7])
def test_profile_binding_generation_distinguishes_sampling_and_model(temperature: float | None, tmp_path: Path) -> None:
    base = {"provider": "bedrock", "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0"}
    profiles = [
        RuntimeLLMProfile.from_settings("operator", LLMProfileSettings.model_validate(values))
        for values in (
            base,
            {**base, "temperature": None},
            {**base, "temperature": 0.0},
            {**base, "temperature": 0.7},
            {**base, "model": "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0", "temperature": temperature},
        )
    ]
    generations = [_LLMProfileResolver._binding_generation(profile, credential_generation=None) for profile in profiles]
    assert len(set(generations)) == len(profiles)
    preflight = ComposerPreflight(
        catalog=Mock(spec=CatalogService),
        settings=_make_settings(tmp_path),
        policy_context=Mock(spec=PluginPolicyContextFactory),
        blob_service=None,
        secret_service=None,
        secret_wiring_policy=Mock(spec=SecretWiringPolicy),
        operator_profile_registry=None,
        coordinator=RuntimePreflightCoordinator(),
    )
    keys = []
    for generation in generations:
        snapshot = PluginAvailabilitySnapshot.create(
            policy_hash="policy",
            principal_scope="test",
            available=frozenset(),
            unavailable=(),
            selected=(),
            usable_profile_aliases=(),
            selected_profile_aliases=(),
            binding_generation_fingerprint=generation,
        )
        state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
        keys.append(preflight.key(state, session_scope="session", plugin_snapshot=snapshot))
    assert len(set(keys)) == len(profiles)


def test_rejected_numeric_preflight_does_not_retry_with_changed_sampling(harness: _Harness) -> None:
    harness.reject_sampling[0] = True
    transform = LLMTransform({**harness.config, "temperature": 0.7})
    transform._provider = harness.provider
    ctx = make_context()
    ctx.operation_id = "sampling-preflight"
    with pytest.raises(RuntimePreflightFailedError) as caught:
        transform.runtime_preflight(ctx)
    assert isinstance(caught.value.__cause__, LLMClientError)
    assert not caught.value.__cause__.retryable
    assert len(harness.requests) == 1
    assert harness.requests[0]["temperature"] == 0.7


@pytest.mark.parametrize("temperature", ["omitted", None, 0.0, 0.7])
def test_lowered_profile_preserves_provider_default_and_explicit_omission(temperature: str | float | None) -> None:
    values: dict[str, Any] = {"provider": "bedrock", "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0"}
    if temperature != "omitted":
        values["temperature"] = temperature
    settings = LLMProfileSettings.model_validate(values)
    options, _audit = _lower_llm_profile_node_options(
        "operator",
        settings,
        {"profile": "operator", "prompt_template": "hello", "schema": {"mode": "observed"}},
    )
    expected = None if temperature == "omitted" else temperature
    assert LLMTransform(options).provider_config.temperature == expected
    assert LLMSource({**options, "on_validation_failure": "discard"}).provider_config.temperature == expected
