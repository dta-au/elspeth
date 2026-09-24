"""The strict flip on the compose loop and the planner, resolved from settings (S1 T8).

From T8 the service resolves each route's dialect from ``composer_model`` /
``composer_advisor_model``, their endpoints and ``composer_strict_tools``
(``strict_transport.resolve_composer_tool_contract``). Under the default
``preferred``:

- an OpenRouter planner with no custom base (row 4) sends the 32 mechanical
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
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.guided.profile import TUTORIAL_PROFILE
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.composer.pipeline_planner import PipelinePlannerError, PlannerOriginatingMessage
from elspeth.web.composer.pipeline_proposal import PresentBase
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.config import WebSettings
from elspeth.web.sessions.protocol import GuidedOperationFence
from tests.helpers.session_fences import fenced_operation_context
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
from tests.unit.web.composer.test_pipeline_planner import _response
from tests.unit.web.conftest import _make_session

_OPENROUTER_PLANNER = "openrouter/deepseek/deepseek-v4.1-flash"
_STRICT = ToolContractDialect.OPENAI_STRICT
_NONE = ToolContractDialect.NONE


@pytest.fixture(autouse=True)
def _advisor_end_gate_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The END advisor gate is not under test; make it a CLEAN no-op."""
    monkeypatch.setattr(ComposerServiceImpl, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)


@pytest.fixture(autouse=True)
def _composer_available(monkeypatch: pytest.MonkeyPatch) -> None:
    def _available(self: ComposerServiceImpl) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=self._model, provider="test")

    monkeypatch.setattr(ComposerServiceImpl, "_compute_availability", _available)


def _settings(**overrides: Any) -> WebSettings:
    values: dict[str, Any] = {
        "data_dir": Path("/data"),
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": b"\x00" * 32,
    }
    values.update(overrides)
    return WebSettings(**values)


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
        ({"composer_model": _OPENROUTER_PLANNER}, _STRICT, 32),
        ({"composer_model": _OPENROUTER_PLANNER, "composer_strict_tools": "off"}, _NONE, 0),
        ({}, _NONE, 0),
        ({"composer_strict_tools": "forward_to_endpoint"}, _STRICT, 32),
    ],
    ids=["openrouter_preferred", "openrouter_off", "default_gpt_5_5", "gpt_5_5_forward_to_endpoint"],
)
async def test_compose_turn_sends_and_records_the_resolved_dialect(
    overrides: dict[str, Any], dialect: ToolContractDialect, strict_count: int
) -> None:
    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_settings(**overrides))

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=_text()) as completion:
        result = await service.compose("Build a CSV pipeline", [], _empty_state(), session_id=session_id)

    sent = completion.call_args.kwargs["tools"]
    assert len(sent) == 42
    if dialect is _STRICT:
        assert _strict_flags(sent).count(True) == 32
        assert _strict_flags(sent).count(False) == 10
    else:
        assert set(_strict_flags(sent)) == {"omitted"}
    [call] = result.llm_calls
    assert (call.tool_contract_dialect, call.strict_tool_count) == (dialect, strict_count)


# ---------------------------------------------------------------- no provider call added (composer invariant)


@pytest.mark.asyncio
async def test_a_freeform_compose_turn_makes_the_same_provider_calls_on_both_dialects() -> None:
    """Control: a second provider call on the strict path turns this red."""
    counts: dict[str, int] = {}
    for label, overrides in (("strict", {}), ("off", {"composer_strict_tools": "off"})):
        service, session_id = _composer_service_with_session(
            catalog=_mock_catalog(), settings=_settings(composer_model=_OPENROUTER_PLANNER, **overrides)
        )
        responses = [_tool_turn("list_models", {}), _text()]
        with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=responses) as completion:
            await service.compose("Which models can I use?", [], _empty_state(), session_id=session_id)
        flags = _strict_flags(completion.call_args_list[0].kwargs["tools"])
        assert (True in flags) is (label == "strict")
        counts[label] = completion.await_count

    assert counts["strict"] == counts["off"] == 2


@pytest.fixture
def _tutorial_authority(composer_service_with_real_sessions: ComposerServiceImpl) -> Any:
    sessions = composer_service_with_real_sessions._require_sessions_service()
    session_id = uuid4()
    with sessions._engine.begin() as conn:
        _make_session(conn, session_id=str(session_id), user_id="test-user")
    with fenced_operation_context(sessions._engine, session_id) as context:
        yield session_id, context


def _tutorial_session() -> GuidedSession:
    source_id = "11111111-1111-4111-8111-111111111111"
    output_id = "22222222-2222-4222-8222-222222222222"
    return GuidedSession(
        step=GuidedStep.STEP_3_TRANSFORMS,
        profile=TUTORIAL_PROFILE,
        source_order=(source_id,),
        reviewed_sources={
            source_id: SourceResolved(
                name="input",
                plugin="csv",
                options={"path": "/data/input.csv"},
                observed_columns=("id",),
                sample_rows=(),
                on_validation_failure="discard",
            )
        },
        output_order=(output_id,),
        reviewed_outputs={
            output_id: SinkOutputResolved(
                name="results",
                plugin="json",
                options={"path": "/data/results.jsonl"},
                required_fields=("id",),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
    )


@pytest.mark.asyncio
async def test_a_tutorial_entry_planner_turn_makes_the_same_provider_calls_on_both_dialects(
    composer_service_with_real_sessions: ComposerServiceImpl,
    _tutorial_authority: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tutorial runs the same backend (ADR-031); the strict route adds no call to it.

    The scripted provider answers every request with the same discovery call,
    so the run ends on the planner's own budget; the count of provider calls
    must not depend on the dialect. Control: a second provider call on the
    strict path turns this red.
    """
    session_id, operation_context = _tutorial_authority
    sessions = composer_service_with_real_sessions._require_sessions_service()
    base_settings = composer_service_with_real_sessions._settings
    counts: dict[str, int] = {}
    for label, strict_setting in (("strict", "preferred"), ("off", "off")):
        settings = base_settings.model_copy(update={"composer_model": _OPENROUTER_PLANNER, "composer_strict_tools": strict_setting})
        service = ComposerServiceImpl.for_trained_operator(
            composer_service_with_real_sessions._catalog, settings, sessions_service=sessions, session_engine=sessions._engine
        )
        requests: list[dict[str, Any]] = []

        async def completion(
            *,
            _requests: list[dict[str, Any]] = requests,
            _dialect: ToolContractDialect = service._planner_dialect,
            **kwargs: Any,
        ) -> Any:
            _requests.append(kwargs)
            return _response(("list_sources", encode_semantic_arguments("list_sources", _dialect, {})))

        monkeypatch.setattr("litellm.acompletion", completion)
        with pytest.raises(PipelinePlannerError):
            await service.plan_guided_pipeline(
                session_operation_context=operation_context,
                intent="Build the reviewed pipeline.",
                current_state=_empty_state(),
                guided=_tutorial_session(),
                originating_message=PlannerOriginatingMessage(
                    session_id=str(session_id), message_id=None, content="Build the reviewed pipeline.", user_id="test-user"
                ),
                base=PresentBase(state_id=UUID("55555555-5555-4555-8555-555555555555"), composition_content_hash="0" * 64),
                user_id="test-user",
                supersedes_draft_hash=None,
                recorder=BufferingRecorder(),
                operation_fence=GuidedOperationFence(session_id=session_id, operation_id=str(uuid4()), lease_token=uuid4().hex, attempt=1),
            )
        planner_flags = _strict_flags(requests[0]["tools"])
        assert (True in planner_flags) is (label == "strict")
        counts[label] = len(requests)

    assert counts["strict"] == counts["off"]
    assert counts["strict"] > 1


# ---------------------------------------------------------------- openai_strict counterparts of NONE-only pins

# ``test_tool_schema_contract.py`` and ``test_provider_cache_markers.py`` pin
# the ``none`` list, which is true only for today's bytes; these are their
# ``openai_strict`` counterparts (the pins themselves are not edited).


def test_openai_strict_set_pipeline_keeps_the_envelope_and_an_explicit_strict_false() -> None:
    """set_pipeline is an option tool: same enveloped parameters as ``none``, stamped ``strict: false``.

    Control: stamp every tool ``strict: true`` in ``wire_tool_definitions`` and this goes red.
    """
    from elspeth.web.composer.service import composer_loop_tool_definitions

    none_tools = {tool["function"]["name"]: tool["function"] for tool in composer_loop_tool_definitions(_NONE)}
    strict_tools = {tool["function"]["name"]: tool["function"] for tool in composer_loop_tool_definitions(_STRICT)}

    assert strict_tools["set_pipeline"]["strict"] is False
    assert strict_tools["set_pipeline"]["parameters"] == none_tools["set_pipeline"]["parameters"]
    assert list(strict_tools["set_pipeline"]["parameters"]["properties"]) == ["pipeline"]


def test_openai_strict_list_keeps_registry_order_and_cache_marker_placement() -> None:
    """Names and order are the registry's; the Anthropic cache marker lands beside ``function`` on the last tool.

    D8 means no production route carries both; this pins the function only.
    """
    from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers
    from elspeth.web.composer.service import composer_loop_tool_definitions
    from elspeth.web.composer.tools import get_tool_definitions

    tools = composer_loop_tool_definitions(_STRICT)
    assert [tool["function"]["name"] for tool in tools] == [definition["name"] for definition in get_tool_definitions()]
    _, marked = apply_anthropic_cache_markers([], tools)
    assert marked is not None
    assert ["cache_control" in tool for tool in marked] == [False] * (len(marked) - 1) + [True]
    assert [tool["function"] for tool in marked] == [tool["function"] for tool in tools]
