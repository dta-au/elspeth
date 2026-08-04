"""Guided chat solvers thread the PRIMARY-role endpoint affordance (Phase 3
Task 2). Mirrors the structure of test_chat_solver_sampling_config.py.

Guided solvers never see the advisor's endpoint — there is no advisor
parameter on any of these functions, so the role asymmetry is structural,
not just behavioural: callers (guided_chat_atomic.py) always pass the
primary's (api_base, api_key) pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.guided import chat_solver
from elspeth.web.composer.guided.chat_solver import DeferredIntentManagementChatRequest
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.sessions.routes.composer import guided_chat_atomic as guided_chat_atomic_module

_SENTINEL_CREDENTIAL = "sk-guided-endpoint-affordance-sentinel"  # secret-scan: allow-this-line


@dataclass
class _FakeMessage:
    content: str | None
    tool_calls: list[Any] | None = None


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]


def _text_response(text: str = "reply") -> _FakeResponse:
    return _FakeResponse(choices=[_FakeChoice(message=_FakeMessage(content=text))])


@pytest.mark.asyncio
async def test_solve_step_chat_omits_endpoint_kwargs_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _text_response("reply")

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", fake_acompletion)

    await chat_solver.solve_step_chat(
        model="gpt-5",
        step=GuidedStep.STEP_1_SOURCE,
        user_message="hi",
        temperature=None,
        seed=None,
        timeout_seconds=30.0,
    )

    assert "api_base" not in captured
    assert "api_key" not in captured


@pytest.mark.asyncio
async def test_solve_step_chat_sends_configured_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _text_response("reply")

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", fake_acompletion)

    await chat_solver.solve_step_chat(
        model="gpt-5",
        step=GuidedStep.STEP_1_SOURCE,
        user_message="hi",
        temperature=None,
        seed=None,
        timeout_seconds=30.0,
        api_base="https://primary-gateway.example.test/v1",
        api_key=_SENTINEL_CREDENTIAL,
    )

    assert captured["api_base"] == "https://primary-gateway.example.test/v1"
    assert captured["api_key"] == _SENTINEL_CREDENTIAL


@pytest.mark.asyncio
async def test_step_1_source_chat_omits_endpoint_kwargs_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _text_response("advice")

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", fake_acompletion)

    await chat_solver.maybe_resolve_step_1_source_chat(
        model="gpt-4o",
        user_message="how should I configure csv?",
        plugin_hint="csv",
        current_source=None,
        available_source_plugins=("csv",),
        temperature=None,
        seed=None,
        timeout_seconds=30.0,
    )

    assert "api_base" not in captured
    assert "api_key" not in captured


@pytest.mark.asyncio
async def test_step_1_source_chat_sends_configured_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _text_response("advice")

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", fake_acompletion)

    await chat_solver.maybe_resolve_step_1_source_chat(
        model="gpt-4o",
        user_message="how should I configure csv?",
        plugin_hint="csv",
        current_source=None,
        available_source_plugins=("csv",),
        temperature=None,
        seed=None,
        timeout_seconds=30.0,
        api_base="http://127.0.0.1:8787/v1",
        api_key=_SENTINEL_CREDENTIAL,
    )

    assert captured["api_base"] == "http://127.0.0.1:8787/v1"
    assert captured["api_key"] == _SENTINEL_CREDENTIAL


@pytest.mark.asyncio
async def test_step_2_sink_chat_omits_endpoint_kwargs_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _text_response("advice")

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", fake_acompletion)

    await chat_solver.maybe_resolve_step_2_sink_chat(
        model="gpt-4o",
        user_message="what sink should I use?",
        current_sink=None,
        temperature=None,
        seed=None,
        timeout_seconds=30.0,
    )

    assert "api_base" not in captured
    assert "api_key" not in captured


@pytest.mark.asyncio
async def test_step_2_sink_chat_sends_configured_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _text_response("advice")

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", fake_acompletion)

    await chat_solver.maybe_resolve_step_2_sink_chat(
        model="gpt-4o",
        user_message="what sink should I use?",
        current_sink=None,
        temperature=None,
        seed=None,
        timeout_seconds=30.0,
        api_base="https://primary-gateway.example.test/v1",
        api_key=_SENTINEL_CREDENTIAL,
    )

    assert captured["api_base"] == "https://primary-gateway.example.test/v1"
    assert captured["api_key"] == _SENTINEL_CREDENTIAL


@pytest.mark.asyncio
async def test_deferred_intent_management_chat_omits_endpoint_kwargs_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _text_response("ok")

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", fake_acompletion)

    await chat_solver.maybe_manage_deferred_intent_chat(
        request=DeferredIntentManagementChatRequest(
            model="gpt-4o",
            step=GuidedStep.STEP_3_TRANSFORMS,
            user_message="cancel one saved instruction",
            temperature=None,
            seed=None,
            timeout_seconds=30.0,
            context_block="safe context",
        ),
        recorder=None,
    )

    assert "api_base" not in captured
    assert "api_key" not in captured


@pytest.mark.asyncio
async def test_deferred_intent_management_chat_sends_configured_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _text_response("ok")

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", fake_acompletion)

    await chat_solver.maybe_manage_deferred_intent_chat(
        request=DeferredIntentManagementChatRequest(
            model="gpt-4o",
            step=GuidedStep.STEP_3_TRANSFORMS,
            user_message="cancel one saved instruction",
            temperature=None,
            seed=None,
            timeout_seconds=30.0,
            context_block="safe context",
            api_base="https://primary-gateway.example.test/v1",
            api_key=_SENTINEL_CREDENTIAL,
        ),
        recorder=None,
    )

    assert captured["api_base"] == "https://primary-gateway.example.test/v1"
    assert captured["api_key"] == _SENTINEL_CREDENTIAL


@pytest.mark.asyncio
async def test_guided_chat_route_uses_primary_endpoint_not_advisor(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through run_guided_chat_provider_attempt (guided_chat_atomic.py):
    the resolved (api_base, api_key) passed to the wire call must be the
    PRIMARY role's, even when an advisor endpoint is ALSO configured — proving
    the asymmetry survives the full route -> solver call chain, not just the
    solver function signature.
    """
    source = SourceResolved(
        name="source",
        plugin="csv",
        options={"schema": {"mode": "observed"}},
        observed_columns=("col",),
        sample_rows=({"col": "value"},),
        on_validation_failure="discard",
    )
    outputs = {
        "output-a": SinkOutputResolved(
            name="first",
            plugin="json",
            options={},
            required_fields=("field_a",),
            schema_mode="observed",
            on_write_failure="discard",
        ),
    }
    guided = SimpleNamespace(
        active_edit_target=SimpleNamespace(kind="output", stable_id="output-a"),
        source_order=("source-a",),
        reviewed_sources={"source-a": source},
        output_order=("output-a",),
        reviewed_outputs=outputs,
        pending_output_intents={},
        deferred_intents=(),
    )
    captured: dict[str, Any] = {}

    async def capture_provider(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _text_response("The selected output is ready.")

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", capture_provider)

    await guided_chat_atomic_module.run_guided_chat_provider_attempt(
        session_id=uuid4(),
        user=SimpleNamespace(user_id="user"),
        step=GuidedStep.STEP_2_SINK,
        guided=guided,
        state=SimpleNamespace(sources={}, nodes=(), outputs=(), edges=()),
        message="Explain this output.",
        settings=SimpleNamespace(
            composer_model="test/model",
            composer_temperature=None,
            composer_discovery_reasoning_effort="none",
            composer_seed=None,
            composer_max_discovery_turns=1,
            composer_max_tool_calls_per_turn=16,
            composer_timeout_seconds=30.0,
            composer_endpoint_base_url="https://primary-gateway.example.test/v1",
            composer_endpoint_api_key=SimpleNamespace(get_secret_value=lambda: _SENTINEL_CREDENTIAL),
            # ADVISOR endpoint also configured, deliberately different, to
            # prove the guided route never picks it up.
            composer_advisor_endpoint_base_url="https://advisor-gateway.example.test/v1",
            composer_advisor_endpoint_api_key=SimpleNamespace(get_secret_value=lambda: "advisor-only-secret"),
        ),
        catalog=SimpleNamespace(),
        plugin_snapshot=None,
        secret_service=None,
        recorder=BufferingRecorder(),
        progress=None,
    )

    assert captured["api_base"] == "https://primary-gateway.example.test/v1"
    assert captured["api_key"] == _SENTINEL_CREDENTIAL


# --- reasoning-effort threading (elspeth-dc459d438e) --------------------------


@pytest.mark.asyncio
async def test_solve_step_chat_threads_the_discovery_reasoning_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _text_response("reply")

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", fake_acompletion)

    await chat_solver.solve_step_chat(
        model="gpt-5",
        step=GuidedStep.STEP_1_SOURCE,
        user_message="hi",
        temperature=None,
        seed=None,
        timeout_seconds=30.0,
        reasoning_effort="low",
    )

    assert captured["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_solve_step_chat_uses_the_native_object_for_openrouter_models(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _text_response("reply")

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", fake_acompletion)

    await chat_solver.solve_step_chat(
        model="openrouter/anthropic/claude-sonnet-5",
        step=GuidedStep.STEP_1_SOURCE,
        user_message="hi",
        temperature=None,
        seed=None,
        timeout_seconds=30.0,
        reasoning_effort="low",
    )

    assert captured["reasoning"] == {"effort": "low"}
    assert "reasoning_effort" not in captured


@pytest.mark.asyncio
async def test_solve_step_chat_default_is_unhinted(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _text_response("reply")

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", fake_acompletion)

    await chat_solver.solve_step_chat(
        model="gpt-5",
        step=GuidedStep.STEP_1_SOURCE,
        user_message="hi",
        temperature=None,
        seed=None,
        timeout_seconds=30.0,
    )

    assert "reasoning_effort" not in captured
    assert "reasoning" not in captured


@pytest.mark.asyncio
async def test_deferred_intent_management_request_carries_the_reasoning_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _text_response("ok")

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", fake_acompletion)

    await chat_solver.maybe_manage_deferred_intent_chat(
        request=DeferredIntentManagementChatRequest(
            model="gpt-4o",
            step=GuidedStep.STEP_3_TRANSFORMS,
            user_message="cancel one saved instruction",
            temperature=None,
            seed=None,
            timeout_seconds=30.0,
            context_block="safe context",
            reasoning_effort="low",
        ),
        recorder=None,
    )

    assert captured["reasoning_effort"] == "low"
