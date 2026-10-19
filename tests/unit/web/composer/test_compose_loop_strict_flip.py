"""The strict flip on the compose loop and the planner, resolved from settings (S1 T8).

From T8 the service resolves each route's dialect from ``composer_model`` /
``composer_advisor_model``, their endpoints and ``composer_strict_tools``
(``strict_transport.resolve_composer_tool_contract``). Under the default
``preferred``:

- an OpenRouter planner with no custom base (row 4) sends the 35 mechanical
  tools with ``strict: true`` and the 10 option tools with ``strict: false``,
  and each LLM call records ``openai_strict`` with its strict-tool count;
- ``composer_strict_tools="off"`` and the default ``gpt-5.5`` (row 7, ruling 7)
  keep today's bytes: no ``strict`` key, ``none`` / ``0``;
- ``forward_to_endpoint`` makes the default ``gpt-5.5`` route ``ENFORCING``.

The composer invariant (no provider call added to any compose transition) is
checked behaviourally: one freeform compose turn and one tutorial-entry
planner turn make the same number of provider calls on ``openai_strict`` and
on ``none``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.config import WebSettings
from elspeth.web.prompt_guard_config import PromptGuardSettings
from tests.unit.web.composer._helpers import (
    FakeChoice,
    FakeFunction,
    FakeLLMResponse,
    FakeMessage,
    FakeToolCall,
    _clean_advisor_checkpoint,
    _composer_service_with_session,
    _mock_catalog,
)

_OPENROUTER_PLANNER = "openrouter/deepseek/deepseek-v4.1-flash"
_STRICT = ToolContractDialect.OPENAI_STRICT
_NONE = ToolContractDialect.NONE


@pytest.fixture(autouse=True)
def _advisor_end_gate_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The END advisor gate is not under test; make it a CLEAN no-op."""
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)


@pytest.fixture(autouse=True)
def _composer_available(monkeypatch: pytest.MonkeyPatch) -> None:
    def _available(*, model: str, **_kwargs: object) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=model, provider="test")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", _available)


def _settings(**overrides: Any) -> WebSettings:
    values: dict[str, Any] = {
        "data_dir": Path("/data"),
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": b"\x00" * 32,
        "composer_prompt_guard_mode": "required",
        "composer_prompt_guard": PromptGuardSettings(
            endpoint="https://primary-shield.example.test", subscription_key="offline-primary-shield"
        ),
        "composer_advisor_prompt_guard": PromptGuardSettings(
            endpoint="https://advisor-shield.example.test", subscription_key="offline-advisor-shield"
        ),
    }
    values.update(overrides)
    return WebSettings(**values)


def _install_required_azure_guards(monkeypatch: pytest.MonkeyPatch, *, user_prompt: str) -> list[httpx.Request]:
    """Fake Azure HTTP only; retain the real guard, custody and SQL audit."""
    requests: list[httpx.Request] = []

    def shield_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host in {"primary-shield.example.test", "advisor-shield.example.test"}
        assert request.url.path == "/contentsafety/text:shieldPrompt"
        assert request.url.params["api-version"] == "2024-09-01"
        payload = json.loads(request.content)
        assert type(payload) is dict and set(payload) == {"userPrompt", "documents"}
        assert payload["userPrompt"] == user_prompt
        assert type(payload["documents"]) is list
        assert all(type(document) is str for document in payload["documents"])
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _document in payload["documents"]],
            },
        )

    def mount(settings: PromptGuardSettings | None) -> PromptGuardMount:
        assert type(settings) is PromptGuardSettings
        return PromptGuardMount(AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(shield_response)))

    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", mount, raising=True)
    monkeypatch.setattr("elspeth.web.composer.service.build_prompt_guard_mount", mount, raising=True)
    return requests


async def _assert_required_guard_audits(service: ComposerServiceImpl, session_id: str, *, expected: int) -> None:
    assert expected > 0
    sessions = service._sessions_service
    assert sessions is not None
    messages = await sessions.get_messages(UUID(session_id), limit=None, include_private_evidence=False)
    checks = [
        entry["check"]
        for message in messages
        if message.role == "audit" and message.tool_calls is not None
        for entry in message.tool_calls
        if entry["_kind"] == "prompt_guard_audit"
    ]
    assert len(checks) == expected
    assert len({check["operation_id"] for check in checks}) == 1
    assert len({check["operation_epoch"] for check in checks}) == 1
    assert all(
        (check["role"], check["adapter"], check["mode"], check["disposition"], check["reason"])
        == ("composer", "azure_prompt_shields", "required", "allow", "clean")
        for check in checks
    )


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _text(content: str = "Done.") -> FakeLLMResponse:
    return FakeLLMResponse(choices=[FakeChoice(message=FakeMessage(content=content, tool_calls=None))])


def _tool_turn(name: str, arguments: dict[str, Any]) -> FakeLLMResponse:
    call = FakeToolCall(id="call_1", function=FakeFunction(name=name, arguments=json.dumps(arguments)))
    return FakeLLMResponse(choices=[FakeChoice(message=FakeMessage(content=None, tool_calls=[call]))])


def _strict_flags(tools: list[dict[str, Any]]) -> list[object]:
    return [tool["function"]["strict"] if "strict" in tool["function"] else "omitted" for tool in tools]


# ---------------------------------------------------------------- the compose loop's audited dialect


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "dialect", "strict_count"),
    [
        ({"composer_model": _OPENROUTER_PLANNER}, _STRICT, 35),
        ({"composer_model": _OPENROUTER_PLANNER, "composer_strict_tools": "off"}, _NONE, 0),
        ({}, _NONE, 0),
        ({"composer_strict_tools": "forward_to_endpoint"}, _STRICT, 35),
    ],
    ids=["openrouter_preferred", "openrouter_off", "default_gpt_5_5", "gpt_5_5_forward_to_endpoint"],
)
async def test_compose_turn_sends_and_records_the_resolved_dialect(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], dialect: ToolContractDialect, strict_count: int
) -> None:
    shield_requests = _install_required_azure_guards(monkeypatch, user_prompt="Build a CSV pipeline")
    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_settings(**overrides))

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=_text()) as completion:
        result = await service.compose("Build a CSV pipeline", [], _empty_state(), session_id=session_id)

    assert shield_requests
    await _assert_required_guard_audits(service, session_id, expected=completion.await_count)
    # This supplied no-tool reply needs no repair. Check every actual request
    # rather than requiring the retired lexical retry for a build phrase.
    assert completion.call_count == len(result.llm_calls) == 1
    assert result.repair_turns_used == 0
    for request, call in zip(completion.call_args_list, result.llm_calls, strict=True):
        sent = request.kwargs["tools"]
        assert len(sent) == 45
        if dialect is _STRICT:
            assert _strict_flags(sent).count(True) == 35
            assert _strict_flags(sent).count(False) == 10
        else:
            assert set(_strict_flags(sent)) == {"omitted"}
        assert (call.tool_contract_dialect, call.strict_tool_count) == (dialect, strict_count)


# ---------------------------------------------------------------- no provider call added (composer invariant)


@pytest.mark.asyncio
async def test_a_freeform_compose_turn_makes_the_same_provider_calls_on_both_dialects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: a second provider call on the strict path turns this red."""
    shield_requests = _install_required_azure_guards(monkeypatch, user_prompt="Which models can I use?")
    counts: dict[str, int] = {}
    for label, overrides in (("strict", {}), ("off", {"composer_strict_tools": "off"})):
        service, session_id = _composer_service_with_session(
            catalog=_mock_catalog(), settings=_settings(composer_model=_OPENROUTER_PLANNER, **overrides)
        )
        responses = [_tool_turn("list_models", {}), _text()]
        with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=responses) as completion:
            await service.compose("Which models can I use?", [], _empty_state(), session_id=session_id)
        await _assert_required_guard_audits(service, session_id, expected=completion.await_count)
        flags = _strict_flags(completion.call_args_list[0].kwargs["tools"])
        assert (True in flags) is (label == "strict")
        counts[label] = completion.await_count

    assert counts["strict"] == counts["off"] == 2
    assert shield_requests


# ---------------------------------------------------------------- openai_strict counterparts of NONE-only pins

# ``test_tool_schema_contract.py`` and ``test_provider_cache_markers.py`` pin
# the ``none`` list, which is true only for today's bytes; these are their
# ``openai_strict`` counterparts (the pins themselves are not edited).


def test_openai_strict_set_pipeline_keeps_the_envelope_and_an_explicit_strict_false() -> None:
    """set_pipeline is an option tool: same enveloped parameters as ``none``, stamped ``strict: false``.

    Control: stamp every tool ``strict: true`` in ``wire_tool_definitions`` and this goes red.
    """
    from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions

    none_tools = {tool["function"]["name"]: tool["function"] for tool in composer_loop_tool_definitions(_NONE)}
    strict_tools = {tool["function"]["name"]: tool["function"] for tool in composer_loop_tool_definitions(_STRICT)}

    assert strict_tools["set_pipeline"]["strict"] is False
    assert strict_tools["set_pipeline"]["parameters"] == none_tools["set_pipeline"]["parameters"]
    assert list(strict_tools["set_pipeline"]["parameters"]["properties"]) == ["pipeline"]


def test_openai_strict_list_keeps_registry_order_and_cache_marker_placement() -> None:
    """Recovery controls preserve registry order and the final Anthropic cache anchor.

    D8 means no production route carries both; this pins the function only.
    """
    from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers
    from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
    from elspeth.web.composer.tools import get_tool_definitions

    tools = composer_loop_tool_definitions(_STRICT)
    registry_names = [definition["name"] for definition in get_tool_definitions()]
    recovery_names = ["handoff_to_user", "restore_parked_graph"]
    assert set(recovery_names).isdisjoint(registry_names)
    tool_names = [tool["function"]["name"] for tool in tools]
    assert len(tool_names) == len(set(tool_names))
    assert tool_names == [*registry_names[:-1], *recovery_names, registry_names[-1]]
    _, marked = apply_anthropic_cache_markers([], tools)
    assert marked is not None
    assert ["cache_control" in tool for tool in marked] == [False] * (len(marked) - 1) + [True]
    assert [tool["function"] for tool in marked] == [tool["function"] for tool in tools]
