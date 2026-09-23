"""The composer boot probe sends the requests production sends (plan S0, boot probe).

A boot probe that sends different request options from the real composer proves
nothing about the route production uses. These tests capture the exact LiteLLM
kwargs of two real ``compose()`` turns (the freeform tool loop and the pipeline
planner) through the ``litellm.acompletion`` seam, and compare them with the
probe's requests built from the same settings.

Excluded from the comparison, and why:

- ``messages``: the probe sends its own one-line prompt.
- ``max_tokens`` on the loop request only: ``_call_llm`` sends none, and the
  probe caps its reply at 16 tokens (a 200 with a truncated reply still proves
  acceptance). The planner request sends the planner's own cap, so it is
  compared.
- ``tools`` on the planner request: production sends a request-scoped
  discovery subset and terminal contract, and the probe sends the default
  ``planner_tool_definitions()``. The planner tool *names* production sent are
  asserted to be a subset of the probe's instead.

Mutation controls prove the comparison can fail: dropping the reasoning kwarg
from one side, and changing the planner-side token cap, both go red.
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

import elspeth.web.composer.pipeline_planner as planner_module
from elspeth.web.composer.boot_probe import ComposerProbeRequest, build_composer_probe_requests
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.sessions.guided_test_authority import DualFencedSessionServiceHarness

_MODEL = "openrouter/deepseek/deepseek-v4.1-flash"
_ENDPOINT = "https://planner-gateway.example.test/api/v1"
_ENDPOINT_KEY = "parity-endpoint-key"  # secret-scan: allow-this-line


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
        "composer_max_composition_turns": 3,
        "composer_max_discovery_turns": 2,
        "composer_timeout_seconds": 20.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": b"\x00" * 32,
    }
    values.update(overrides)
    return WebSettings(**values)


def _pipeline(data_dir: Path, session_id: str) -> dict[str, Any]:
    return {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": str(data_dir / "blobs" / session_id / "input.csv"), "schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
        },
        "nodes": [],
        "edges": [],
        "outputs": [
            {
                "sink_name": "rows",
                "plugin": "json",
                "options": {
                    "path": str(data_dir / "outputs" / session_id / "result.jsonl"),
                    "schema": {"mode": "observed"},
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
    }


def _usage() -> dict[str, object]:
    return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01}


async def _capture_production_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: WebSettings,
    *,
    message: str,
) -> list[dict[str, Any]]:
    """Run one real ``compose()`` turn and return every LiteLLM request it made."""
    requests, _composer, _result = await _run_production_turn(tmp_path, monkeypatch, settings, message=message)
    return requests


async def _run_production_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: WebSettings,
    *,
    message: str,
    reach_the_hatch: bool = False,
) -> tuple[list[dict[str, Any]], ComposerServiceImpl, Any]:
    """Run one real ``compose()`` turn; return its LiteLLM requests, the service and the result.

    With ``reach_the_hatch``, every full-palette planner request is answered
    with a discovery call, so the planner exhausts its discovery budget and
    takes the escape-hatch turn (a terminal-only tool list), which is answered
    with the terminal proposal.
    """
    engine = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="parity-user")
    sessions = DualFencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    session = await sessions.create_session("parity-user", "Parity", "local")
    user_message = await sessions.add_message(session.id, "user", message, writer_principal="route_user_message")
    monkeypatch.setattr(
        ComposerServiceImpl,
        "_compute_availability",
        lambda _self: ComposerAvailability(available=True, provider="test", model=_MODEL, reason=None),
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
        if reach_the_hatch and "num_retries" in kwargs and len(kwargs["tools"]) > 1:
            discovery = _ToolCall(id=f"parity-discovery-{len(requests)}", function=_Function(name="list_sources", arguments="{}"))
            return _Response(choices=[_Choice(message=_Message(content=None, tool_calls=[discovery]))], usage=_usage())
        if "tools" in kwargs and any(tool["function"]["name"] == "emit_pipeline_proposal" for tool in kwargs["tools"]):
            call = _ToolCall(
                id="parity-terminal",
                function=_Function(
                    name="emit_pipeline_proposal",
                    arguments=json.dumps({"pipeline": _pipeline(tmp_path, str(session.id))}),
                ),
            )
            return _Response(choices=[_Choice(message=_Message(content=None, tool_calls=[call]))], usage=_usage())
        return _Response(choices=[_Choice(message=_Message(content="I can help you build a pipeline.", tool_calls=[]))], usage=_usage())

    monkeypatch.setattr("litellm.acompletion", completion)
    result = await composer.compose(
        message,
        [],
        CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        session_id=str(session.id),
        user_id="parity-user",
        user_message_id=str(user_message.id),
    )
    return requests, composer, result


async def _capture_probe_requests(monkeypatch: pytest.MonkeyPatch, settings: WebSettings) -> dict[str, dict[str, Any]]:
    """The probe's requests, after the same ``_litellm_acompletion`` shaping production gets."""
    import elspeth.web.composer.boot_probe as boot_probe

    captured: dict[str, dict[str, Any]] = {}
    requests: tuple[ComposerProbeRequest, ...] = build_composer_probe_requests(settings)

    for request in requests:

        async def completion(*, _surface: str = request.surface, **kwargs: Any) -> _Response:
            captured[_surface] = kwargs
            return _Response(choices=[_Choice(message=_Message(content="ok", tool_calls=[]))], usage=_usage())

        monkeypatch.setattr("litellm.acompletion", completion)
        if request.role == "planner":
            await boot_probe.probe_composer_config(request)
    return captured


def _without(kwargs: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if key not in keys}


def _loop_request(requests: list[dict[str, Any]]) -> dict[str, Any]:
    loop_requests = [request for request in requests if "tools" in request and len(request["tools"]) == 42]
    assert loop_requests, "the ordinary compose loop made no 42-tool request"
    return loop_requests[0]


def _planner_request(requests: list[dict[str, Any]]) -> dict[str, Any]:
    planner_requests = [request for request in requests if "num_retries" in request]
    assert planner_requests, "the pipeline planner made no request"
    return planner_requests[0]


@pytest.mark.asyncio
async def test_loop_tools_probe_sends_the_compose_loop_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    production = _loop_request(await _capture_production_requests(tmp_path, monkeypatch, settings, message="What can you help me build?"))
    probe = (await _capture_probe_requests(monkeypatch, settings))["loop_tools"]

    assert "reasoning" in production, "control: the compared request must carry a reasoning kwarg"
    assert probe["max_tokens"] == 16
    assert _without(probe, "messages", "max_tokens") == _without(production, "messages")


@pytest.mark.asyncio
async def test_planner_tools_probe_sends_the_pipeline_planner_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    production = _planner_request(
        await _capture_production_requests(tmp_path, monkeypatch, settings, message="Build a CSV to JSONL pipeline.")
    )
    probe = (await _capture_probe_requests(monkeypatch, settings))["planner_tools"]

    assert "reasoning" in production, "control: the compared request must carry a reasoning kwarg"
    assert production["max_tokens"] == settings.composer_planner_max_completion_tokens
    assert _without(probe, "messages", "tools") == _without(production, "messages", "tools")
    production_names = {tool["function"]["name"] for tool in production["tools"]}
    probe_names = {tool["function"]["name"] for tool in probe["tools"]}
    assert "emit_pipeline_proposal" in production_names
    assert production_names <= probe_names


@pytest.mark.asyncio
async def test_mutation_control_dropped_reasoning_makes_the_loop_comparison_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import elspeth.web.composer.service as service_module

    settings = _settings(tmp_path)
    production = _loop_request(await _capture_production_requests(tmp_path, monkeypatch, settings, message="What can you help me build?"))
    monkeypatch.setattr(service_module, "apply_reasoning_kwargs", lambda _kwargs, **_ignored: None)
    probe = (await _capture_probe_requests(monkeypatch, settings))["loop_tools"]

    assert "reasoning" not in probe
    assert _without(probe, "messages", "max_tokens") != _without(production, "messages")


@pytest.mark.asyncio
async def test_mutation_control_dropped_reasoning_makes_the_planner_comparison_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    production = _planner_request(
        await _capture_production_requests(tmp_path, monkeypatch, settings, message="Build a CSV to JSONL pipeline.")
    )
    monkeypatch.setattr(planner_module, "apply_reasoning_kwargs", lambda _kwargs, **_ignored: None)
    probe = (await _capture_probe_requests(monkeypatch, settings))["planner_tools"]

    assert "reasoning" not in probe
    assert _without(probe, "messages", "tools") != _without(production, "messages", "tools")


@pytest.mark.asyncio
async def test_mutation_control_changed_planner_token_cap_makes_the_comparison_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    production = _planner_request(
        await _capture_production_requests(tmp_path, monkeypatch, settings, message="Build a CSV to JSONL pipeline.")
    )
    mutated = _settings(tmp_path, composer_planner_max_completion_tokens=settings.composer_planner_max_completion_tokens - 1)
    probe = (await _capture_probe_requests(monkeypatch, mutated))["planner_tools"]

    assert probe["max_tokens"] != production["max_tokens"]
    assert _without(probe, "messages", "tools") != _without(production, "messages", "tools")


# --- S1 T8: stamped routes ------------------------------------------------------

_ADVISOR = "openrouter/z-ai/glm-5.3"
_PROXY = "https://proxy.example/v1"
_PROXY_KEY = "parity-proxy-key"  # secret-scan: allow-this-line


def _strict_map(tools: list[dict[str, Any]]) -> dict[str, object]:
    return {tool["function"]["name"]: (tool["function"]["strict"] if "strict" in tool["function"] else "omitted") for tool in tools}


def _hatch_request(requests: list[dict[str, Any]]) -> dict[str, Any]:
    hatch = [request for request in requests if "num_retries" in request and request["model"] == _ADVISOR]
    assert len(hatch) == 1, "the planner took no escape-hatch turn"
    return hatch[0]


def _record_planner_llm_calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Capture every ``ComposerLLMCall`` the pipeline planner builds (its audited call records)."""
    recorded: list[Any] = []
    real_builder = planner_module.build_llm_call_record

    def recording_builder(**kwargs: Any) -> Any:
        call = real_builder(**kwargs)
        recorded.append(call)
        return call

    monkeypatch.setattr(planner_module, "build_llm_call_record", recording_builder)
    return recorded


def _hatch_call(recorded: list[Any]) -> Any:
    calls = [call for call in recorded if call.model_requested == _ADVISOR]
    assert len(calls) == 1
    return calls[0]


@pytest.mark.asyncio
async def test_probe_and_production_send_the_same_strict_flags_on_a_forwarding_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both routes resolve ``openai_strict``; the probe's stamps equal production's on every surface.

    Controls: stamp the probe's loop list with ``NONE`` and it goes red; stamp
    the probe's planner list with ``NONE`` and it goes red.
    """
    import os

    from elspeth.web.composer.strict_transport import resolve_composer_tool_contract

    settings = _settings(tmp_path, composer_endpoint_base_url=None, composer_endpoint_api_key=None, composer_advisor_model=_ADVISOR)
    loop_production = _loop_request(
        await _capture_production_requests(tmp_path, monkeypatch, settings, message="What can you help me build?")
    )
    planner_requests, composer, _result = await _run_production_turn(
        tmp_path, monkeypatch, settings, message="Build a CSV to JSONL pipeline.", reach_the_hatch=True
    )
    probe = await _capture_probe_requests(monkeypatch, settings)

    assert composer.tool_contract_summary.contract == resolve_composer_tool_contract(settings, env=os.environ)
    assert [tool["function"]["strict"] for tool in probe["loop_tools"]["tools"]] == [
        tool["function"]["strict"] for tool in loop_production["tools"]
    ]
    assert True in [tool["function"]["strict"] for tool in probe["loop_tools"]["tools"]]
    production_planner = _strict_map(_planner_request(planner_requests)["tools"])
    probe_planner = _strict_map(probe["planner_tools"]["tools"])
    shared = production_planner.keys() & probe_planner.keys()
    assert shared
    assert {name: production_planner[name] for name in shared} == {name: probe_planner[name] for name in shared}
    assert set(production_planner.values()) == {True, False}
    assert (
        _strict_map(probe["hatch_terminal"]["tools"])
        == _strict_map(_hatch_request(planner_requests)["tools"])
        == {"emit_pipeline_proposal": False}
    )


@pytest.mark.asyncio
async def test_hatch_probe_and_hatch_turn_follow_the_hatch_route_not_the_planner_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex finding 3, direction (i): planner row 4 (FORWARDING) + advisor row 5 (NONE, a proxy base).

    Each ``NONE`` side comes from the routing table, not from D8's Anthropic
    short-circuit. No hatch probe is sent, and the hatch turn goes to the
    proxy with today's bytes. Direction (ii) is the next test. Control:
    substitute the planner's dialect for the hatch dialect (the probe's
    inclusion check and the service's escape-hatch dialect) and both
    directions go red.
    """
    settings_i = _settings(
        tmp_path,
        composer_endpoint_base_url=None,
        composer_endpoint_api_key=None,
        composer_advisor_model=_ADVISOR,
        composer_advisor_endpoint_base_url=_PROXY,
        composer_advisor_endpoint_api_key=_PROXY_KEY,
    )
    probe_i = await _capture_probe_requests(monkeypatch, settings_i)
    assert set(probe_i) == {"loop_tools", "planner_tools"}
    assert True in _strict_map(probe_i["loop_tools"]["tools"]).values()
    assert True in _strict_map(probe_i["planner_tools"]["tools"]).values()
    recorded_i = _record_planner_llm_calls(monkeypatch)
    requests_i, _composer_i, _result_i = await _run_production_turn(
        tmp_path, monkeypatch, settings_i, message="Build a CSV to JSONL pipeline.", reach_the_hatch=True
    )
    hatch_i = _hatch_request(requests_i)
    assert hatch_i["api_base"] == _PROXY
    assert _strict_map(hatch_i["tools"]) == {"emit_pipeline_proposal": "omitted"}
    assert _hatch_call(recorded_i).tool_contract_dialect.value == "none"


@pytest.mark.asyncio
async def test_hatch_probe_and_hatch_turn_follow_the_hatch_route_when_only_the_hatch_forwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex finding 3, direction (ii): planner row 5 (NONE, a proxy base) + advisor row 4 (FORWARDING).

    The hatch probe is sent to the advisor's effective endpoint (no
    ``api_base``: OpenRouter's default), not the planner's proxy, with the
    terminal as ``strict: false``; the planner lists carry no ``strict`` key;
    a production hatch turn sends the same terminal to the same endpoint.
    """
    settings_ii = _settings(
        tmp_path,
        composer_endpoint_base_url=_PROXY,
        composer_endpoint_api_key=_PROXY_KEY,
        composer_advisor_model=_ADVISOR,
    )
    probe_ii = await _capture_probe_requests(monkeypatch, settings_ii)
    assert set(probe_ii) == {"loop_tools", "planner_tools", "hatch_terminal"}
    assert set(_strict_map(probe_ii["loop_tools"]["tools"]).values()) == {"omitted"}
    assert set(_strict_map(probe_ii["planner_tools"]["tools"]).values()) == {"omitted"}
    assert "api_base" not in probe_ii["hatch_terminal"]
    assert _strict_map(probe_ii["hatch_terminal"]["tools"]) == {"emit_pipeline_proposal": False}
    recorded_ii = _record_planner_llm_calls(monkeypatch)
    requests_ii, _composer_ii, _result_ii = await _run_production_turn(
        tmp_path, monkeypatch, settings_ii, message="Build a CSV to JSONL pipeline.", reach_the_hatch=True
    )
    hatch_ii = _hatch_request(requests_ii)
    assert "api_base" not in hatch_ii
    assert _strict_map(hatch_ii["tools"]) == {"emit_pipeline_proposal": False}
    assert _hatch_call(recorded_ii).tool_contract_dialect.value == "openai_strict"
