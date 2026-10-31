"""Boot descriptions match real fenced common-loop and END advisor requests.

Only the physical SDK is scripted. Sessionless descriptions remain unqualified.
The loop probe has a 16-token cap; an initially empty logical request uses its
configured paid output allowance, while existing-graph requests have no such
legacy cap. Those caps are asserted independently, then excluded with messages
from the remaining option comparison.
Mutation controls alter actual live request options and stamps.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy.pool import StaticPool

import elspeth.web.composer.provider_gateway as gateway_module
from elspeth.plugins.transforms.llm.providers.openrouter import OPENROUTER_APP_REFERER, OPENROUTER_APP_TITLE
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory
from elspeth.web.composer.boot_probe import build_composer_probe_requests, probe_composer_config
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.helpers.advisor_review import parse_advisor_publication_review
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness

_MODEL = "openrouter/deepseek/deepseek-v4.1-flash"

_ENDPOINT = "https://planner-gateway.example.test/api/v1"

_ENDPOINT_KEY = "parity-endpoint-key"


@dataclass
class _Function:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _Function


@dataclass
class _Message:
    content: str | None
    tool_calls: list[_ToolCall]


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Response:
    choices: list[_Choice]
    usage: Mapping[str, object]
    model: str = "deepseek/deepseek-v4.1-flash"
    id: str = "parity-request-1"


def _settings(tmp_path: Path, **overrides: Any) -> WebSettings:
    values: dict[str, Any] = {
        "data_dir": tmp_path,
        "composer_model": _MODEL,
        "composer_endpoint_base_url": _ENDPOINT,
        "composer_endpoint_api_key": _ENDPOINT_KEY,
        "composer_temperature": 0.2,
        "composer_seed": 7,
        # Equal efforts make the planner's first (discovery-effort) call
        # comparable with the probe's candidate-effort request; the unit tests
        # pin that the probe uses the candidate effort when they differ.
        "composer_discovery_reasoning_effort": "medium",
        "composer_candidate_reasoning_effort": "medium",
        "composer_boot_probe_enabled": False,
        "composer_prompt_guard_mode": "debug-when-unconfigured",
        "composer_max_composition_turns": 3,
        "composer_max_discovery_turns": 2,
        "composer_timeout_seconds": 20.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": b"\x00" * 32,
    }
    values.update(overrides)
    return WebSettings(**values)


def _usage() -> dict[str, object]:
    return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01}


async def _capture_production_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: WebSettings,
    *,
    message: str,
    existing_graph: bool = False,
) -> list[dict[str, Any]]:
    """Run one real ``compose()`` turn and return every LiteLLM request it made."""
    requests, _composer, _result = await _run_production_turn(
        tmp_path, monkeypatch, settings, message=message, existing_graph=existing_graph
    )
    return requests


async def _run_production_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: WebSettings,
    *,
    message: str,
    existing_graph: bool = False,
) -> tuple[list[dict[str, Any]], ComposerServiceImpl, Any]:
    """Run one real ``compose()`` turn; return its LiteLLM requests, the service and the result.

    The common primary returns prose and the real END advisor reviews it.
    """
    engine = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="parity-user")
        grant_test_pipeline_user(conn, identity_id="parity-user")
    sessions = FencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    session = await sessions.create_session("parity-user", "Parity", "local")
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    current_state_id = None
    if existing_graph:
        source_path = tmp_path / "existing.csv"
        source_path.write_text("name\nAda\n", encoding="utf-8")
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": str(source_path), "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        serialized = state.to_dict()
        saved = await sessions.save_composition_state(
            session.id,
            CompositionStateData(
                nodes=serialized["nodes"], sources=serialized["sources"], metadata_=serialized["metadata"], is_valid=False
            ),
            provenance="session_seed",
        )
        current_state_id = str(saved.id)
    user_message = await sessions.add_message(session.id, "user", message, writer_principal="route_user_message")
    advisor_history = AdvisorTranscriptHistory.from_records(
        session_id=str(session.id),
        actor_user_id="parity-user",
        current_request_id=str(user_message.id),
        request=message,
        records=await sessions.get_messages(session.id),
    )
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **_kwargs: ComposerAvailability(available=True, provider="test", model=_MODEL, reason=None),
    )
    composer = ComposerServiceImpl.for_trained_operator(
        create_catalog_service(),
        settings,
        sessions_service=sessions,
        session_engine=engine,
    )
    requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> _Response:
        requests.append(kwargs)
        if "response_format" in kwargs:
            assert kwargs["model"] == settings.composer_advisor_model
            assert kwargs["response_format"]["json_schema"]["name"] == "advisor_end_response"
            review = parse_advisor_publication_review(kwargs["messages"][1]["content"])
            assert review["current_request"] == message
            assert review["candidate_reply"] == "I can help you build a pipeline."
            content = json.dumps(
                {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None, "publication_verdict": "ADMIT"}
            )
            return _Response(choices=[_Choice(message=_Message(content=content, tool_calls=[]))], usage=_usage())
        return _Response(choices=[_Choice(message=_Message(content="I can help you build a pipeline.", tool_calls=[]))], usage=_usage())

    monkeypatch.setattr("litellm.acompletion", completion)
    # The integration conftest's real lease adapter binds this exact persisted
    # session/service to required work and provider custody, then joins before
    # closing the COMPOSE lease. Only the physical SDK is replaced above.
    try:
        result = await composer.compose(
            message,
            [],
            state,
            session_id=str(session.id),
            current_state_id=current_state_id,
            user_id="parity-user",
            user_message_id=str(user_message.id),
            advisor_history=advisor_history,
        )
    finally:
        engine.dispose()
    return requests, composer, result


def _expected_planner_wire_requests(settings: WebSettings) -> dict[str, dict[str, Any]]:
    """Describe both live surfaces without invoking a sessionless probe."""
    described: dict[str, dict[str, Any]] = {}
    for request in build_composer_probe_requests(settings):
        kwargs = request.to_litellm_kwargs()
        if request.model.startswith("openrouter/"):
            kwargs["extra_headers"] = {
                "HTTP-Referer": OPENROUTER_APP_REFERER,
                "X-OpenRouter-Title": OPENROUTER_APP_TITLE,
                "X-Title": OPENROUTER_APP_TITLE,
            }
            kwargs["usage"] = {"include": True}
        described[request.surface] = kwargs
    return described


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["required", "debug-when-unconfigured"])
async def test_sessionless_probe_is_not_qualified_and_calls_neither_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    settings = _settings(tmp_path, composer_prompt_guard_mode=mode, composer_advisor_model=_ADVISOR)
    sdk_calls: list[object] = []
    guard_calls: list[object] = []

    async def forbid_sdk(**kwargs):
        sdk_calls.append(kwargs)
        raise AssertionError("sessionless probe called the model")

    async def forbid_guard(*args, **kwargs):
        guard_calls.append((args, kwargs))
        raise AssertionError("sessionless probe called the guard")

    monkeypatch.setattr("litellm.acompletion", forbid_sdk)
    monkeypatch.setattr("elspeth.web.composer.prompt_guard_clients.AzurePromptGuardClient.inspect", forbid_guard)
    monkeypatch.setattr(gateway_module, "enforce_prompt_guard", forbid_guard)
    requests = build_composer_probe_requests(settings)
    assert [request.surface for request in requests] == ["loop_tools", "advisor"]

    for request in requests:
        assert await probe_composer_config(request) is None

    assert sdk_calls == guard_calls == []


def _without(kwargs: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if key not in keys}


def _loop_request(requests: list[dict[str, Any]]) -> dict[str, Any]:
    loop_requests = [
        request
        for request in requests
        if "tools" in request and any(tool["function"]["name"] == "set_pipeline" for tool in request["tools"])
    ]
    assert len(loop_requests) == 1, "the common loop must make exactly one primary request"
    return loop_requests[0]


@pytest.mark.asyncio
async def test_loop_tools_description_matches_the_fenced_compose_loop_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    production = _loop_request(
        await _capture_production_requests(tmp_path, monkeypatch, settings, message="What can you help me build?", existing_graph=True)
    )
    probe = _expected_planner_wire_requests(settings)["loop_tools"]

    assert "reasoning" in production, "control: the compared request must carry a reasoning kwarg"
    assert probe["max_tokens"] == 16
    assert _without(probe, "messages", "max_tokens") == _without(production, "messages")


@pytest.mark.asyncio
async def test_mutation_control_dropped_reasoning_makes_the_loop_comparison_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    production = _loop_request(
        await _capture_production_requests(tmp_path, monkeypatch, settings, message="What can you help me build?", existing_graph=True)
    )
    monkeypatch.setattr(gateway_module, "apply_reasoning_kwargs", lambda _kwargs, **_ignored: None)
    probe = _expected_planner_wire_requests(settings)["loop_tools"]

    assert "reasoning" not in probe
    assert _without(probe, "messages", "max_tokens") != _without(production, "messages")


_ADVISOR = "openrouter/z-ai/glm-5.3"

_PROXY = "https://proxy.example/v1"

_PROXY_KEY = "parity-proxy-key"


def _strict_map(tools: list[dict[str, Any]]) -> dict[str, object]:
    return {tool["function"]["name"]: (tool["function"]["strict"] if "strict" in tool["function"] else "omitted") for tool in tools}


def _advisor_request(requests: list[dict[str, Any]]) -> dict[str, Any]:
    advisor = [request for request in requests if "response_format" in request]
    assert len(advisor) == 1, "the actual END advisor must run once"
    assert advisor[0]["response_format"]["json_schema"]["name"] == "advisor_end_response"
    return advisor[0]


def _assert_live_options_match(probe, production, surface, *, expected_primary_tokens=None):
    if surface == "loop_tools":
        assert probe["max_tokens"] == 16
        if expected_primary_tokens is None:
            assert "max_tokens" not in production
        else:
            assert production["max_tokens"] == expected_primary_tokens
        assert _without(probe, "messages", "max_tokens") == _without(production, "messages", "max_tokens")
    else:
        assert surface == "advisor"
        assert _without(probe, "messages") == _without(production, "messages")


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_graph", [False, True], ids=["empty", "existing"])
async def test_both_live_descriptions_match_actual_common_transition_requests(tmp_path, monkeypatch, existing_graph):
    settings = _settings(tmp_path)
    requests = await _capture_production_requests(
        tmp_path, monkeypatch, settings, message="Describe the supported authoring capabilities.", existing_graph=existing_graph
    )
    assert len(requests) == 2
    descriptions = _expected_planner_wire_requests(settings)
    assert set(descriptions) == {"loop_tools", "advisor"}
    _assert_live_options_match(
        descriptions["loop_tools"],
        _loop_request(requests),
        "loop_tools",
        expected_primary_tokens=None if existing_graph else settings.composer_planner_max_completion_tokens,
    )
    _assert_live_options_match(descriptions["advisor"], _advisor_request(requests), "advisor")


@pytest.mark.asyncio
async def test_advisor_token_cap_mutation_is_detected_on_the_real_surface(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    actual = _advisor_request(
        await _capture_production_requests(tmp_path, monkeypatch, settings, message="Describe the current capability.")
    )
    probe = _expected_planner_wire_requests(settings)["advisor"]
    _assert_live_options_match(probe, actual, "advisor")
    changed = _settings(tmp_path, composer_advisor_max_completion_tokens=settings.composer_advisor_max_completion_tokens - 1)
    mutant = _expected_planner_wire_requests(changed)["advisor"]
    assert mutant["max_tokens"] != actual["max_tokens"]
    with pytest.raises(AssertionError):
        _assert_live_options_match(mutant, actual, "advisor")


@pytest.mark.asyncio
async def test_advisor_structured_format_mutation_is_detected_on_the_real_surface(tmp_path, monkeypatch):
    import elspeth.web.composer.boot_probe as boot_module

    settings = _settings(tmp_path)
    actual = _advisor_request(
        await _capture_production_requests(tmp_path, monkeypatch, settings, message="Describe the current capability.")
    )
    _assert_live_options_match(_expected_planner_wire_requests(settings)["advisor"], actual, "advisor")
    original = boot_module.build_advisor_request_options

    def without_format(**kwargs):
        result = dict(original(**kwargs))
        del result["response_format"]
        return result

    monkeypatch.setattr(boot_module, "build_advisor_request_options", without_format)
    mutant = _expected_planner_wire_requests(settings)["advisor"]
    assert "response_format" not in mutant
    with pytest.raises(AssertionError):
        _assert_live_options_match(mutant, actual, "advisor")


@pytest.mark.asyncio
async def test_loop_stamp_mutation_is_detected_on_the_real_surface(tmp_path, monkeypatch):
    import elspeth.web.composer.boot_probe as boot_module
    from elspeth.contracts.composer_llm_audit import ToolContractDialect
    from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions

    settings = _settings(tmp_path, composer_endpoint_base_url=None, composer_endpoint_api_key=None)
    actual = _loop_request(await _capture_production_requests(tmp_path, monkeypatch, settings, message="Describe the current capability."))
    probe = _expected_planner_wire_requests(settings)["loop_tools"]
    _assert_live_options_match(probe, actual, "loop_tools", expected_primary_tokens=settings.composer_planner_max_completion_tokens)
    assert set(_strict_map(actual["tools"]).values()) == {True, False}
    monkeypatch.setattr(
        boot_module, "composer_loop_tool_definitions", lambda _dialect: composer_loop_tool_definitions(ToolContractDialect.NONE)
    )
    mutant = _expected_planner_wire_requests(settings)["loop_tools"]
    assert set(_strict_map(mutant["tools"]).values()) == {"omitted"}
    with pytest.raises(AssertionError):
        _assert_live_options_match(mutant, actual, "loop_tools", expected_primary_tokens=settings.composer_planner_max_completion_tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_proxy", [False, True], ids=["advisor-proxy", "primary-proxy"])
async def test_loop_and_advisor_keep_independent_effective_endpoints(tmp_path, monkeypatch, primary_proxy):
    settings = _settings(
        tmp_path,
        composer_endpoint_base_url=_PROXY if primary_proxy else None,
        composer_endpoint_api_key=_PROXY_KEY if primary_proxy else None,
        composer_advisor_model=_ADVISOR,
        composer_advisor_endpoint_base_url=None if primary_proxy else _PROXY,
        composer_advisor_endpoint_api_key=None if primary_proxy else _PROXY_KEY,
    )
    requests, composer, _result = await _run_production_turn(tmp_path, monkeypatch, settings, message="Describe the current capability.")
    assert len(requests) == 2
    loop, advisor = _loop_request(requests), _advisor_request(requests)
    descriptions = _expected_planner_wire_requests(settings)
    _assert_live_options_match(
        descriptions["loop_tools"], loop, "loop_tools", expected_primary_tokens=settings.composer_planner_max_completion_tokens
    )
    _assert_live_options_match(descriptions["advisor"], advisor, "advisor")
    proxied, direct = (loop, advisor) if primary_proxy else (advisor, loop)
    assert proxied["api_base"] == _PROXY and proxied["api_key"] == _PROXY_KEY
    assert "api_base" not in direct and "api_key" not in direct
    assert set(_strict_map(loop["tools"]).values()) == ({"omitted"} if primary_proxy else {True, False})
    assert composer.tool_contract_summary.contract.planner.dialect.value == ("none" if primary_proxy else "openai_strict")
