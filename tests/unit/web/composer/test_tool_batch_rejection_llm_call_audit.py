"""Pre-effect tool-batch rejections keep the carrying provider call on the audit path.

``run_tool_batch`` refuses two batch shapes before any handler, proposal, or
blob effect runs: an explicit-approval batch with more approval-requiring
mutations than the per-turn proposal cap, and a batch that reuses a provider
tool-call ID a durable row already owns. Both refusals happen after the
provider completion was recorded, so the completion must ride out on the
exception for the route's LLM-call persistence (``_llm_calls_from_exception``)
to write it. The proposal-cap refusal is a convergence failure (the model's
turn exceeded a composer budget), not a composer-service outage.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.routes._helpers import _llm_calls_from_exception
from tests.unit.web.composer.test_compose_loop_persistence import _capture_tool_batch_rejection, _tool_batch_response


def _set_explicit_approve(service: ComposerServiceImpl, session_id: str) -> None:
    sessions_service = service._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(update(sessions_table).where(sessions_table.c.id == session_id).values(trust_mode="explicit_approve"))


@pytest.mark.asyncio
async def test_proposal_cap_rejection_is_convergence_error_carrying_llm_calls(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    _set_explicit_approve(composer_service_with_real_sessions, result_session_id)
    response = _tool_batch_response(
        *((f"call_cap_proposal_{index}", "set_metadata", {"patch": {"name": f"proposal {index}"}}) for index in range(11))
    )

    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        response=response,
    )

    assert type(caught) is ComposerConvergenceError
    assert caught.budget_exhausted == "composition"
    assert caught.reason == "tool_call_cap_exceeded"
    assert dict(caught.evidence) == {"observed": 11, "cap": 10, "cap_kind": "pending_proposals"}
    # No handler ran and nothing mutated: no partial state, and the rejected
    # batch has no tool invocations of its own to replay.
    assert caught.partial_state is None
    assert caught.tool_invocations == ()
    llm_calls = _llm_calls_from_exception(caught)
    assert len(llm_calls) == 1


@pytest.mark.asyncio
async def test_reused_tool_call_id_rejection_carries_llm_calls(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    await sessions_service.create_composition_proposal(
        session_id=UUID(result_session_id),
        tool_call_id="call_reused_for_audit",
        tool_name="set_metadata",
        summary="Prior durable proposal.",
        rationale="Owns the provider tool-call ID the next batch reuses.",
        affects=("metadata",),
        arguments_json={"patch": {"name": "prior"}},
        arguments_redacted_json={"patch": {"name": "prior"}},
        base_state_id=None,
        actor="test",
    )

    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        response=_tool_batch_response(("call_reused_for_audit", "set_metadata", {"patch": {"name": "reuse"}})),
    )

    assert type(caught) is AuditIntegrityError
    assert str(caught) == "Composer tool batch reuses a provider tool-call ID already persisted in this session"
    llm_calls = _llm_calls_from_exception(caught)
    assert len(llm_calls) == 1
