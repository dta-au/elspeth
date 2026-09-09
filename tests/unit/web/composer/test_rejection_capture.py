"""Composer-side rejection capture (elspeth-3e28029d2f).

Two behaviors pinned here:

1. The compose loop persists a ``composition_rejection_events`` row for every
   refused mutation — carrying the UNREDACTED payload the planner saw (the
   text and the reasoning), while the ``chat_messages`` tool row stays
   redacted as before.
2. ``explain_validation_error``'s no-match response no longer carries the
   ``known_codes`` array whose width (>64) collapsed the whole audit row to a
   ``response_projection_limit`` stub. The codes still reach the planner via
   ``suggested_fix``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import text

from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata, ValidationEntry, ValidationSummary
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.tools.generation import _execute_explain_validation_error
from tests.unit.web.composer._helpers import _stub_advisor_end_gate_clean  # noqa: F401  (autouse end-gate CLEAN stub)


def _empty_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


REJECTION_MESSAGE = (
    "Pending vague_term review is not wired for resolution on node "
    "'score_lead': requirement 'lead quality:score_lead' has no resolvable "
    "prompt wiring."
)


def _failed_mutation_result(state: CompositionState) -> ToolResult:
    return ToolResult(
        success=False,
        updated_state=state,
        validation=ValidationSummary(
            is_valid=False,
            errors=(
                ValidationEntry(
                    component="transform:score_lead",
                    message=REJECTION_MESSAGE,
                    severity="high",
                    error_code="vague_term_unwired",
                ),
            ),
        ),
        affected_nodes=(),
    )


def _mutation_response(call_id: str) -> Any:
    tool_call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name="set_metadata",
            arguments=json.dumps({"patch": {"name": "Rated leads"}}),
        ),
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))])


def _text_response(content: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))])


async def _run_one_turn(service: ComposerServiceImpl, *, llm: Any, session_id: str) -> Any:
    driver: Any = service
    return await driver._run_one_turn_for_test(llm=llm, session_id=session_id, current_state_id=None)


def _rejection_rows(service: ComposerServiceImpl, session_id: str) -> list[Any]:
    sessions_service: Any = service._sessions_service
    with sessions_service._engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT tool_call_id, tool_name, error_code, message, planner_payload "
                    "FROM composition_rejection_events WHERE session_id=:sid"
                ),
                {"sid": session_id},
            ).fetchall()
        )


@pytest.mark.asyncio
async def test_failed_mutation_persists_unredacted_rejection_row(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    """A validation-rejected mutation persists a rejection row carrying the
    exact payload the planner saw; the chat tool row stays redacted."""
    service = composer_service_with_real_sessions
    responses = [_mutation_response("call_reject_1"), _text_response("Stopping.")]

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    with patch(
        "elspeth.web.composer.tool_batch.execute_tool",
        side_effect=lambda *a, **k: _failed_mutation_result(_empty_state()),
    ):
        await _run_one_turn(service, llm=_fake_llm, session_id=result_session_id)

    rows = _rejection_rows(service, result_session_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.tool_call_id == "call_reject_1"
    assert row.tool_name == "set_metadata"
    assert row.error_code == "vague_term_unwired"
    assert row.message == REJECTION_MESSAGE
    payload = json.loads(row.planner_payload)
    assert payload["success"] is False
    # The reasoning the planner saw is preserved verbatim — unredacted.
    assert REJECTION_MESSAGE in row.planner_payload


@pytest.mark.asyncio
async def test_argument_error_persists_rejection_row_with_raw_message(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    """A ToolArgumentError outcome also persists a rejection row carrying
    the message the planner saw. That message is ALREADY structurally
    leak-safe (ToolArgumentError composes it for verbatim LLM echo), so the
    record preserves it exactly — the chat row's redaction scrubs it to a
    placeholder, this row does not."""
    service = composer_service_with_real_sessions
    responses = [_mutation_response("call_arg_err"), _text_response("Stopping.")]

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    arg_error = ToolArgumentError(argument="patch", expected="valid metadata patch", actual_type="int")
    with patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=arg_error):
        await _run_one_turn(service, llm=_fake_llm, session_id=result_session_id)

    rows = _rejection_rows(service, result_session_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.tool_call_id == "call_arg_err"
    assert row.tool_name == "set_metadata"
    assert row.error_code == "ToolArgumentError"
    # Exactly the planner-visible message — not the chat row's
    # "<redacted-arg-error-message>" placeholder.
    assert row.message == str(arg_error)
    assert json.loads(row.planner_payload)["error_message"] == str(arg_error)


@pytest.mark.asyncio
async def test_successful_mutation_persists_no_rejection_row(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    """Successes leave the rejection table untouched."""
    service = composer_service_with_real_sessions
    responses = [_mutation_response("call_ok"), _text_response("Done.")]

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    await _run_one_turn(service, llm=_fake_llm, session_id=result_session_id)

    assert _rejection_rows(service, result_session_id) == []


class TestExplainNoMatchProjection:
    def test_no_match_response_omits_known_codes_array(self, tool_context: Any) -> None:
        """The no-match branch must fit the persisted projection: the
        known_codes array (117 wide > 64 cap) is dropped; the codes still
        reach the planner inside suggested_fix."""
        result = _execute_explain_validation_error(
            {"error_text": "complete gibberish that matches nothing zzqx"},
            _empty_state(),
            tool_context,
        )
        assert result.success is True
        assert "known_codes" not in result.data
        # The teaching content survives.
        assert "does not match any known" in result.data["explanation"]
        assert "error_code" in result.data["suggested_fix"]
