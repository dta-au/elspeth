"""Tests for compose-loop carrier contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, get_args, get_origin, get_type_hints
from unittest.mock import patch

import pytest

from elspeth.contracts.errors import FailedTurnMetadata
from elspeth.web.composer._compose_loop_carriers import _ToolOutcome, _ToolOutcomeResponse
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolResult
from tests.unit.web.composer._helpers import (
    _empty_state,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
)


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
