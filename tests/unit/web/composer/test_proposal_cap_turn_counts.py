"""A pending-proposal cap refusal reports the compose loop's real turn accounting.

``run_tool_batch`` raises the explicit-approval proposal cap as a
``ComposerConvergenceError`` built from ``ToolBatchContext``. The route turns
``max_turns`` into the 422 ``turns_used`` and forwards ``failed_turn`` to the
recovery panel, so the context must carry the driver's counters and last
persisted tool-call turn, exactly as ``_enforce_tool_call_cap`` reports them.
A cap on the first model turn cannot tell the real counters from zero, so the
batch here follows one persisted discovery turn.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import update

from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.sessions.models import sessions_table
from tests.unit.web.composer.test_compose_loop_persistence import _run_one_turn, _text_response, _tool_batch_response


def _set_explicit_approve(service: ComposerServiceImpl, session_id: str) -> None:
    sessions_service = service._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(update(sessions_table).where(sessions_table.c.id == session_id).values(trust_mode="explicit_approve"))


@pytest.mark.asyncio
async def test_proposal_cap_after_a_discovery_turn_reports_turns_used_and_failed_turn(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    _set_explicit_approve(composer_service_with_real_sessions, result_session_id)
    responses = [
        _tool_batch_response(("call_discovery_before_cap", "get_pipeline_state", {})),
        _tool_batch_response(
            *((f"call_cap_proposal_{index}", "set_metadata", {"patch": {"name": f"proposal {index}"}}) for index in range(11))
        ),
        _text_response("Done."),
    ]

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    with pytest.raises(ComposerConvergenceError) as raised:
        await _run_one_turn(
            composer_service_with_real_sessions,
            llm=_fake_llm,
            session_id=result_session_id,
        )

    caught = raised.value
    assert dict(caught.evidence) == {"observed": 11, "cap": 10, "cap_kind": "pending_proposals"}
    # One discovery turn completed before the capped batch.
    assert caught.max_turns == 1
    assert str(caught).startswith("Composer did not converge within 1 turns")
    # The discovery turn was persisted, so the recovery metadata names it.
    assert caught.failed_turn is not None
    assert caught.failed_turn.assistant_message_id is not None
    assert caught.failed_turn.tool_calls_attempted == 1
    assert caught.failed_turn.tool_responses_persisted == 1
