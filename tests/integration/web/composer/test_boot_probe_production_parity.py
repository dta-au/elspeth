"""Pure boot request descriptions match fenced production requests (plan S0).

A request description with different options from the real composer proves
nothing about the route production uses. These tests capture the exact LiteLLM
kwargs of two real ``compose()`` turns (the freeform tool loop and the pipeline
planner) through the ``litellm.acompletion`` seam, and compare them with the
descriptions built from the same settings. Sessionless probes themselves skip
paid verification: neither mode has durable lifecycle audit custody at boot.
The expected wire includes the gateway's explicit OpenRouter identity and usage
opt-in, asserted here independently of the gateway's shaping functions.

Excluded from the comparison, and why:

- ``messages``: the probe sends its own one-line prompt.
- ``max_tokens`` on the loop request only: ``_call_llm`` sends none, and the
  description caps its reply at 16 tokens. The planner request describes the
  planner's own cap, so it is compared. These tests make no live acceptance claim.
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
import elspeth.web.composer.provider_gateway as gateway_module
from elspeth.plugins.transforms.llm.providers.openrouter import OPENROUTER_APP_REFERER, OPENROUTER_APP_TITLE
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory
from elspeth.web.composer.boot_probe import build_composer_probe_requests, probe_composer_config
from elspeth.web.composer.pipeline_planner import PLANNER_DISCOVERY_TOOL_NAMES
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
        "composer_prompt_guard_mode": "debug-when-unconfigured",
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
    reach_the_hatch: bool = False,
    existing_graph: bool = False,
) -> tuple[list[dict[str, Any]], ComposerServiceImpl, Any]:
    """Run one real ``compose()`` turn; return its LiteLLM requests, the service and the result.

    With ``reach_the_hatch``, every ordinary planner request is answered
    with a discovery call, so the planner exhausts its discovery budget and
    takes the escape-hatch turn on the configured advisor model, which is answered
    with the terminal proposal.
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
        if reach_the_hatch and "max_tokens" in kwargs and kwargs["model"] != settings.composer_advisor_model:
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
    """Describe planner wire options without invoking a sessionless probe."""
    described: dict[str, dict[str, Any]] = {}
    for request in build_composer_probe_requests(settings):
        if request.role == "planner":
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
    assert [request.surface for request in requests] == ["loop_tools", "planner_tools", "hatch_terminal", "advisor"]

    for request in requests:
        assert await probe_composer_config(request) is None

    assert sdk_calls == guard_calls == []


def _without(kwargs: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if key not in keys}


def _loop_request(requests: list[dict[str, Any]]) -> dict[str, Any]:
    loop_requests = [request for request in requests if "tools" in request and len(request["tools"]) == 46]
    assert loop_requests, "the ordinary compose loop made no 46-tool request"
    return loop_requests[0]


def _planner_request(requests: list[dict[str, Any]]) -> dict[str, Any]:
    planner_requests = [request for request in requests if "max_tokens" in request]
    assert planner_requests, "the pipeline planner made no request"
    return planner_requests[0]


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
async def test_planner_tools_description_matches_the_fenced_pipeline_planner_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    production = _planner_request(
        await _capture_production_requests(tmp_path, monkeypatch, settings, message="Build a CSV to JSONL pipeline.")
    )
    probe = _expected_planner_wire_requests(settings)["planner_tools"]

    assert "reasoning" in production, "control: the compared request must carry a reasoning kwarg"
    assert production["max_tokens"] == settings.composer_planner_max_completion_tokens
    assert _without(probe, "messages", "tools") == _without(production, "messages", "tools")
    production_names = {tool["function"]["name"] for tool in production["tools"]}
    probe_names = {tool["function"]["name"] for tool in probe["tools"]}
    assert "emit_pipeline_proposal" in production_names
    assert production_names <= probe_names


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


@pytest.mark.asyncio
async def test_mutation_control_dropped_reasoning_makes_the_planner_comparison_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    production = _planner_request(
        await _capture_production_requests(tmp_path, monkeypatch, settings, message="Build a CSV to JSONL pipeline.")
    )
    monkeypatch.setattr(planner_module, "apply_reasoning_kwargs", lambda _kwargs, **_ignored: None)
    probe = _expected_planner_wire_requests(settings)["planner_tools"]

    assert "reasoning" not in probe
    assert _without(probe, "messages", "tools") != _without(production, "messages", "tools")


@pytest.mark.asyncio
async def test_mutation_control_changed_planner_token_cap_makes_the_comparison_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    production = _planner_request(
        await _capture_production_requests(tmp_path, monkeypatch, settings, message="Build a CSV to JSONL pipeline.")
    )
    mutated = _settings(tmp_path, composer_planner_max_completion_tokens=settings.composer_planner_max_completion_tokens - 1)
    probe = _expected_planner_wire_requests(mutated)["planner_tools"]

    assert probe["max_tokens"] != production["max_tokens"]
    assert _without(probe, "messages", "tools") != _without(production, "messages", "tools")


# --- S1 T8: stamped routes ------------------------------------------------------

_ADVISOR = "openrouter/z-ai/glm-5.3"
_PROXY = "https://proxy.example/v1"
_PROXY_KEY = "parity-proxy-key"  # secret-scan: allow-this-line


def _strict_map(tools: list[dict[str, Any]]) -> dict[str, object]:
    return {tool["function"]["name"]: (tool["function"]["strict"] if "strict" in tool["function"] else "omitted") for tool in tools}


def _hatch_request(requests: list[dict[str, Any]]) -> dict[str, Any]:
    hatch = [request for request in requests if "max_tokens" in request and request["model"] == _ADVISOR]
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
async def test_description_and_production_have_the_same_strict_flags_on_a_forwarding_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both routes resolve ``openai_strict``; described stamps equal production's on every surface.

    Controls: stamp the probe's loop list with ``NONE`` and it goes red; stamp
    the probe's planner list with ``NONE`` and it goes red.
    """
    import os

    from elspeth.web.composer.strict_transport import resolve_composer_tool_contract

    settings = _settings(tmp_path, composer_endpoint_base_url=None, composer_endpoint_api_key=None, composer_advisor_model=_ADVISOR)
    loop_production = _loop_request(
        await _capture_production_requests(tmp_path, monkeypatch, settings, message="What can you help me build?", existing_graph=True)
    )
    planner_requests, composer, _result = await _run_production_turn(
        tmp_path, monkeypatch, settings, message="Build a CSV to JSONL pipeline.", reach_the_hatch=True
    )
    probe = _expected_planner_wire_requests(settings)

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
    hatch_strict = _strict_map(_hatch_request(planner_requests)["tools"])
    assert hatch_strict == {
        **dict.fromkeys(PLANNER_DISCOVERY_TOOL_NAMES, True),
        "handoff_to_user": True,
        "restore_parked_graph": True,
        "emit_pipeline_proposal": False,
    }
    probe_hatch_strict = _strict_map(probe["hatch_terminal"]["tools"])
    assert probe_hatch_strict == {"handoff_to_user": True, "restore_parked_graph": True, "emit_pipeline_proposal": False}
    assert probe_hatch_strict == {name: hatch_strict[name] for name in probe_hatch_strict}


@pytest.mark.asyncio
async def test_hatch_probe_and_hatch_turn_follow_the_hatch_route_not_the_planner_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex finding 3, direction (i): planner row 4 (FORWARDING) + advisor row 5 (NONE, a proxy base).

    Each ``NONE`` side comes from the routing table, not from D8's Anthropic
    short-circuit. No hatch request is described, and the hatch turn goes to the
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
    probe_i = _expected_planner_wire_requests(settings_i)
    assert set(probe_i) == {"loop_tools", "planner_tools"}
    assert True in _strict_map(probe_i["loop_tools"]["tools"]).values()
    assert True in _strict_map(probe_i["planner_tools"]["tools"]).values()
    recorded_i = _record_planner_llm_calls(monkeypatch)
    requests_i, _composer_i, _result_i = await _run_production_turn(
        tmp_path, monkeypatch, settings_i, message="Build a CSV to JSONL pipeline.", reach_the_hatch=True
    )
    hatch_i = _hatch_request(requests_i)
    assert hatch_i["api_base"] == _PROXY
    assert _strict_map(hatch_i["tools"]) == {
        **dict.fromkeys(PLANNER_DISCOVERY_TOOL_NAMES, "omitted"),
        "handoff_to_user": "omitted",
        "restore_parked_graph": "omitted",
        "emit_pipeline_proposal": "omitted",
    }
    assert _hatch_call(recorded_i).tool_contract_dialect.value == "none"


@pytest.mark.asyncio
async def test_hatch_probe_and_hatch_turn_follow_the_hatch_route_when_only_the_hatch_forwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex finding 3, direction (ii): planner row 5 (NONE, a proxy base) + advisor row 4 (FORWARDING).

    The hatch request describes the advisor's effective endpoint (no
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
    probe_ii = _expected_planner_wire_requests(settings_ii)
    assert set(probe_ii) == {"loop_tools", "planner_tools", "hatch_terminal"}
    assert set(_strict_map(probe_ii["loop_tools"]["tools"]).values()) == {"omitted"}
    assert set(_strict_map(probe_ii["planner_tools"]["tools"]).values()) == {"omitted"}
    assert "api_base" not in probe_ii["hatch_terminal"]
    assert _strict_map(probe_ii["hatch_terminal"]["tools"]) == {
        "handoff_to_user": True,
        "restore_parked_graph": True,
        "emit_pipeline_proposal": False,
    }
    recorded_ii = _record_planner_llm_calls(monkeypatch)
    requests_ii, _composer_ii, _result_ii = await _run_production_turn(
        tmp_path, monkeypatch, settings_ii, message="Build a CSV to JSONL pipeline.", reach_the_hatch=True
    )
    hatch_ii = _hatch_request(requests_ii)
    assert "api_base" not in hatch_ii
    assert _strict_map(hatch_ii["tools"]) == {
        **dict.fromkeys(PLANNER_DISCOVERY_TOOL_NAMES, True),
        "handoff_to_user": True,
        "restore_parked_graph": True,
        "emit_pipeline_proposal": False,
    }
    assert _hatch_call(recorded_ii).tool_contract_dialect.value == "openai_strict"
