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
    await composer.compose(
        message,
        [],
        CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        session_id=str(session.id),
        user_id="parity-user",
        user_message_id=str(user_message.id),
    )
    return requests


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
