"""Tests for compose-loop carrier contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import fields
from types import MappingProxyType, SimpleNamespace
from typing import Any, get_args, get_origin, get_type_hints
from unittest.mock import patch

import pytest

from elspeth.contracts.errors import FailedTurnMetadata
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome, _ToolOutcome, _ToolOutcomeResponse
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.service import ComposerServiceImpl, _MalformedLLMResponseError
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolResult
from tests.unit.web.composer._helpers import (
    _empty_state,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
)


class _UnstableProviderMessage:
    """Provider message whose fields drift if ELSPETH reads them twice."""

    def __init__(self, first_tool_call: Any, later_tool_call: Any) -> None:
        self._first_tool_call = first_tool_call
        self._later_tool_call = later_tool_call
        self.content_reads = 0
        self.tool_calls_reads = 0

    @property
    def content(self) -> str:
        self.content_reads += 1
        return "admitted content" if self.content_reads == 1 else "drifted content"

    @property
    def tool_calls(self) -> list[Any]:
        self.tool_calls_reads += 1
        return [self._first_tool_call] if self.tool_calls_reads == 1 else [self._later_tool_call]


class _MalformedUnstableProviderMessage:
    """Malformed content that would look valid if read a second time."""

    def __init__(self) -> None:
        self.content_reads = 0
        self.tool_calls_reads = 0

    @property
    def content(self) -> Any:
        self.content_reads += 1
        return ["malformed"] if self.content_reads == 1 else "drifted valid content"

    @property
    def tool_calls(self) -> list[Any]:
        self.tool_calls_reads += 1
        return []


class _UnstableProviderResponse(Mapping[str, Any]):
    """Attribute/mapping provider response with observable repeated reads."""

    def __init__(self, first_choice: Any, later_choice: Any) -> None:
        self._first_choice = first_choice
        self._later_choice = later_choice
        self.choices_reads = 0
        self.usage_reads = 0

    @property
    def choices(self) -> list[Any]:
        self.choices_reads += 1
        return [self._first_choice] if self.choices_reads == 1 else [self._later_choice]

    def __getitem__(self, key: str) -> Any:
        if key == "choices":
            return self.choices
        if key == "model":
            return "provider/admitted-model"
        if key == "id":
            return "provider-request-1"
        if key == "usage":
            self.usage_reads += 1
            cost = 1.25 if self.usage_reads == 1 else 99.0
            return {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "cost": cost,
            }
        raise KeyError(key)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(("choices", "model", "id", "usage"))

    def __len__(self) -> int:
        return 4


def test_tool_outcome_response_has_named_sum_type_contract() -> None:
    """The P3/P4 response serializer dispatches on a declared union."""

    hints = get_type_hints(_ToolOutcome)
    assert hints["response"] is _ToolOutcomeResponse

    response_members = set(get_args(_ToolOutcomeResponse))
    assert ToolResult in response_members
    assert type(None) in response_members
    assert any(get_origin(member) is Mapping for member in response_members)


def test_tool_outcome_freezes_mapping_response() -> None:
    """Mapping response payloads must be frozen before P4 redaction reads them."""

    response_dict: dict[str, Any] = {"ok": True, "nested": {"k": "v"}}
    outcome = _ToolOutcome(
        call={"id": "tc_x", "function": {"name": "request_advisor_hint"}},
        response=response_dict,
        error_class=None,
        error_message=None,
        pre_version=1,
        post_version=1,
    )

    assert isinstance(outcome.response, MappingProxyType)
    assert isinstance(outcome.call, MappingProxyType)
    with pytest.raises(TypeError):
        outcome.response["ok"] = False  # type: ignore[index]
    response_dict["ok"] = False
    assert outcome.response["ok"] is True


@pytest.mark.asyncio
async def test_model_turn_admits_one_snapshot_and_discards_raw_provider_objects() -> None:
    """Execution and LLM audit consume one immutable provider snapshot."""

    from elspeth.web.composer.anti_anchor import AntiAnchorTracker
    from elspeth.web.composer.audit import BufferingRecorder

    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    state = _empty_state()
    first_call = (
        _make_llm_response(
            tool_calls=[{"id": "call-admitted", "name": "set_source", "arguments": _SET_SOURCE_ARGUMENTS}],
        )
        .choices[0]
        .message.tool_calls[0]
    )
    later_call = (
        _make_llm_response(
            tool_calls=[{"id": "call-drifted", "name": "set_metadata", "arguments": {"patch": {"name": "drifted"}}}],
        )
        .choices[0]
        .message.tool_calls[0]
    )
    message = _UnstableProviderMessage(first_call, later_call)
    first_choice = SimpleNamespace(message=message, finish_reason="tool_calls")
    later_choice = SimpleNamespace(message=message, finish_reason="drifted")
    provider_response = _UnstableProviderResponse(first_choice, later_choice)
    recorder = BufferingRecorder()
    executed_tool_names: list[str] = []

    def _execute_admitted_tool(tool_name: str, *args: Any, **kwargs: Any) -> ToolResult:
        executed_tool_names.append(tool_name)
        return _mutating_set_source_tool(tool_name, *args, **kwargs)

    with (
        patch("elspeth.web.composer.service._litellm_acompletion", return_value=provider_response),
        patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=_execute_admitted_tool),
    ):
        outcome = await service._call_model_turn(
            llm_messages=[{"role": "user", "content": "build it"}],
            tools=[],
            state=state,
            initial_version=state.version,
            deadline=asyncio.get_event_loop().time() + 60.0,
            recorder=recorder,
            progress=None,
            message="build it",
            composition_turns_used=0,
            discovery_turns_used=0,
            failed_turn=None,
        )

        assert provider_response.choices_reads == 1
        assert provider_response.usage_reads == 1
        assert message.content_reads == 1
        assert message.tool_calls_reads == 1
        assert [field.name for field in fields(_CallModelOutcome)] == ["completion"]
        assert not hasattr(outcome, "response")
        assert not hasattr(outcome, "assistant_message")
        assert outcome.completion.message.content == "admitted content"
        assert [call.id for call in outcome.completion.tool_batch.calls] == ["call-admitted"]

        plugin_snapshot, policy_catalog = service._plugin_policy_context(None)
        llm_messages: list[dict[str, Any]] = []
        dispatch, _advisor_calls_used = await service._dispatch_tool_batch(
            call_model=outcome,
            state=state,
            last_validation=None,
            last_runtime_preflight=None,
            llm_messages=llm_messages,
            recorder=recorder,
            anti_anchor=AntiAnchorTracker(),
            discovery_cache={},
            runtime_preflight_cache={},
            session_id=None,
            user_id=None,
            user_message_id=None,
            user_message_content="build it",
            current_state_id=None,
            actor="composer-web:test",
            initial_version=state.version,
            deadline=asyncio.get_event_loop().time() + 60.0,
            progress=None,
            session_scope="test",
            advisor_calls_used=0,
            cancellation_requested=asyncio.Event(),
            plugin_snapshot=plugin_snapshot,
            policy_catalog=policy_catalog,
        )

    assert executed_tool_names == ["set_source"]
    assert dispatch.raw_assistant_content == "admitted content"
    assert llm_messages[0]["content"] == "admitted content"
    assert recorder.llm_calls[0].model_returned == "provider/admitted-model"
    assert recorder.llm_calls[0].prompt_tokens == 11
    assert recorder.llm_calls[0].completion_tokens == 7
    assert recorder.llm_calls[0].provider_cost == 1.25
    assert provider_response.choices_reads == 1
    assert provider_response.usage_reads == 1
    assert message.content_reads == 1
    assert message.tool_calls_reads == 1


@pytest.mark.asyncio
async def test_malformed_model_turn_retains_only_admitted_provider_facts() -> None:
    """Malformed audit evidence never re-reads or retains the raw response."""

    from elspeth.web.composer.audit import BufferingRecorder

    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    state = _empty_state()
    message = _MalformedUnstableProviderMessage()
    choice = SimpleNamespace(message=message, finish_reason="stop")
    provider_response = _UnstableProviderResponse(choice, choice)
    recorder = BufferingRecorder()

    with (
        patch("elspeth.web.composer.service._litellm_acompletion", return_value=provider_response),
        pytest.raises(_MalformedLLMResponseError) as exc_info,
    ):
        await service._call_model_turn(
            llm_messages=[{"role": "user", "content": "build it"}],
            tools=[],
            state=state,
            initial_version=state.version,
            deadline=asyncio.get_event_loop().time() + 60.0,
            recorder=recorder,
            progress=None,
            message="build it",
            composition_turns_used=0,
            discovery_turns_used=0,
            failed_turn=None,
        )

    assert not hasattr(exc_info.value, "response")
    assert exc_info.value.provider_metadata.provider_cost == 1.25
    assert provider_response.choices_reads == 1
    assert provider_response.usage_reads == 1
    assert message.content_reads == 1
    assert message.tool_calls_reads == 0
    assert recorder.llm_calls[0].status.value == "malformed_response"
    assert recorder.llm_calls[0].provider_cost == 1.25


# ── Wall-clock timeout carrier (R2-F9, elspeth-114dd261bc) ──────────────────
#
# Both raise sites in ``_call_llm_before_deadline`` used to hardcode
# ``max_turns=0`` and omit ``failed_turn``. The 422 body then told the user
# the composer gave up "within 0 turns" after a multi-turn build, and the
# SPA's RecoveryPanel — which gates on ``failed_turn != null`` — never opened
# even though the route handler HAD persisted the salvaged partial pipeline.
# The turn counters and the failed-turn metadata are owned by the callers, so
# they are threaded in as parameters.


def _timeout_service() -> ComposerServiceImpl:
    return ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())


def _failed_turn() -> FailedTurnMetadata:
    return FailedTurnMetadata(
        assistant_message_id="assistant-42",
        tool_calls_attempted=3,
        tool_responses_persisted=3,
    )


@pytest.mark.asyncio
async def test_elapsed_deadline_timeout_carries_caller_turn_context() -> None:
    """The pre-call deadline check reports the turns actually spent."""

    service = _timeout_service()
    state = _empty_state()
    failed_turn = _failed_turn()

    with pytest.raises(ComposerConvergenceError) as exc_info:
        await service._call_llm_before_deadline(
            [],
            [],
            state,
            0,
            asyncio.get_event_loop().time() - 1.0,
            composition_turns_used=2,
            discovery_turns_used=1,
            failed_turn=failed_turn,
        )

    exc = exc_info.value
    assert exc.budget_exhausted == "timeout"
    assert exc.reason == "convergence_wall_clock_timeout"
    assert exc.max_turns == 3, "the elapsed-deadline raise must report real turns, not 0"
    assert exc.failed_turn is failed_turn, "RecoveryPanel gates on failed_turn != null"
    assert exc.partial_state is state


@pytest.mark.asyncio
async def test_provider_timeout_carries_caller_turn_context() -> None:
    """The in-flight ``TimeoutError`` raise reports the same turn context."""

    service = _timeout_service()
    state = _empty_state()
    failed_turn = _failed_turn()

    async def _time_out(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError

    with (
        patch.object(service, "_call_llm_with_audit", new=_time_out),
        pytest.raises(ComposerConvergenceError) as exc_info,
    ):
        await service._call_llm_before_deadline(
            [],
            [],
            state,
            0,
            asyncio.get_event_loop().time() + 60.0,
            composition_turns_used=4,
            discovery_turns_used=0,
            failed_turn=failed_turn,
        )

    exc = exc_info.value
    assert exc.budget_exhausted == "timeout"
    assert exc.max_turns == 4
    assert exc.failed_turn is failed_turn
    assert exc.partial_state is state


_SET_SOURCE_ARGUMENTS: dict[str, Any] = {
    "plugin": "csv",
    "on_success": "out",
    "options": {"path": "/data/blobs/f.csv", "schema": {"mode": "observed"}},
    "on_validation_failure": "quarantine",
}


def _mutating_set_source_tool(
    _tool_name: str,
    _arguments: dict[str, Any],
    current_state: CompositionState,
    _catalog: Any,
    **_kwargs: Any,
) -> ToolResult:
    from elspeth.web.composer.state import SourceSpec

    new_state = current_state.with_source(SourceSpec(**_SET_SOURCE_ARGUMENTS))
    return ToolResult(
        success=True,
        updated_state=new_state,
        validation=new_state.validate(),
        affected_nodes=("source",),
        data=None,
    )


async def _run_mutate_then_timeout(service: ComposerServiceImpl) -> tuple[ComposerConvergenceError, int]:
    """Drive a real ``_compose_loop``: one mutating turn, then a timeout.

    The fake raises ``TimeoutError`` from inside ``_call_llm``, which
    ``_call_llm_with_audit`` re-raises after stamping the audit sidecar — so
    the ``except TimeoutError`` arm of ``_call_llm_before_deadline`` is the one
    that fires, not some other unwind path.
    """

    call_count = 0

    async def _first_tool_then_timeout(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_llm_response(
                tool_calls=[{"id": "c1", "name": "set_source", "arguments": _SET_SOURCE_ARGUMENTS}],
            )
        raise TimeoutError

    with (
        patch.object(service, "_call_llm", new=_first_tool_then_timeout),
        patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=_mutating_set_source_tool),
        pytest.raises(ComposerConvergenceError) as exc_info,
    ):
        await service.compose("Build pipeline", [], _empty_state())
    return exc_info.value, call_count


@pytest.mark.asyncio
async def test_compose_loop_timeout_reports_the_turns_already_spent() -> None:
    """Call-site wiring for the start-of-turn model call.

    Turn 1 commits a source mutation and the driver charges the composition
    counter; the turn-2 model call times out inside ``_call_model_turn``.
    Exactly one composition turn had been spent, so the body must say 1 —
    ``>= 1`` would also pass on a value that arrived from somewhere else,
    which is precisely what a wiring test has to exclude.
    """

    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())

    exc, call_count = await _run_mutate_then_timeout(service)

    assert exc.budget_exhausted == "timeout"
    assert call_count == 2
    assert exc.max_turns == 1, f"expected the one charged composition turn; got max_turns={exc.max_turns}"
    assert exc.partial_state is not None
    assert "within 0 turns" not in str(exc)


@pytest.mark.asyncio
async def test_bonus_call_timeout_reports_the_charged_composition_turn() -> None:
    """Call-site wiring for the B-4D-3 last-chance model call.

    With a composition budget of 1, turn 1's mutation exhausts it, so
    ``_classify_and_charge_turn`` makes the bonus call instead of returning to
    the driver. That site is the one place where the counter passed in is the
    POST-increment ``new_composition_turns_used``, so it is wired separately
    from :meth:`_call_model_turn` and needs its own pin.

    ``budget_exhausted`` discriminates the two sites: reaching the bonus call
    means the composition budget was already exhausted, and had the bonus call
    returned normally the raise would have been ``"composition"``. Seeing
    ``"timeout"`` proves the raise came from inside the bonus call.
    """

    service = ComposerServiceImpl.for_trained_operator(
        catalog=_mock_catalog(),
        settings=_make_settings(composer_max_composition_turns=1),
    )

    exc, call_count = await _run_mutate_then_timeout(service)

    assert exc.budget_exhausted == "timeout"
    assert call_count == 2
    assert exc.max_turns == 1, f"expected the charged composition turn; got max_turns={exc.max_turns}"
    assert exc.partial_state is not None
    assert "within 0 turns" not in str(exc)
