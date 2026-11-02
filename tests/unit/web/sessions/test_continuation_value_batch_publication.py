"""Original multi-call P4 publication plan and copied-plan refusal."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
from elspeth.web.sessions.continuation_value_batch_store import plan_value_batch_publication_on_connection
from elspeth.web.sessions.continuation_value_batch_transitions import prepare_value_batch_transitions_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.service import SessionServiceImpl
from tests.unit.web.sessions import test_continuation_live_phases as phases
from tests.unit.web.sessions.test_continuation_v4_metadata_live import _v4_tool_case
from tests.unit.web.sessions.test_continuation_value_batch_sql import _prepare

operation_store = phases.operation_store


@pytest.mark.asyncio
async def test_original_two_call_publication_plan_has_full_ordered_fanout_and_refuses_copy(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(
        operation_store, tmp_path, monkeypatch, following_tool_call=("second_original", "set_metadata", {"patch": {"name": "second value"}})
    ) as (store, owner, running, _sdk):
        batch = await _prepare(owner, running)
        service = owner.service._sessions_service
        assert type(service) is SessionServiceImpl
        sid = str(store[4])
        published_at = datetime.now(UTC)
        with locked_session_transaction(store[0], sid) as conn, service._session_write_lock(conn, sid):
            transitions = prepare_value_batch_transitions_on_connection(conn, running, prepared=batch, published_at=published_at)
            plan = plan_value_batch_publication_on_connection(
                conn, running, service=service, batch=batch, transitions=transitions, created_at=published_at
            )
            plan.require_original()
        assert plan.source_identity.tool_call_ids == ("original_v4_tool", "second_original")
        assert len(plan.messages) == 7
        assert [message.role for message in plan.messages] == ["assistant", "tool", "audit", "tool", "audit", "audit", "audit"]
        assert [message.tool_call_id for message in plan.messages if message.role == "tool"] == list(plan.source_identity.tool_call_ids)
        assert len(plan.states) == len(plan.state_inputs) == 2
        assert plan.states[1].derived_from_state_id == plan.states[0].id
        assert plan.retained_outcome.checkpoint is not None
        assert plan.retained_outcome.checkpoint.inserted_state_ids == tuple(state.id for state in plan.states)
        assert len(plan.message_payload_bytes) == len(plan.messages)
        with pytest.raises(AuditIntegrityError, match="publication differs from its original source"):
            replace(plan, messages=plan.messages[:-1]).require_original()


@pytest.mark.asyncio
async def test_original_value_message_refuses_changed_worker_serialization(operation_store, tmp_path, monkeypatch):
    original = CurrentContinuationMessageProducers.register_prepared_value_batch_messages

    def changed(self, *, prepared, computation):
        actual = prepared.runtime.live.llm_messages[-1]
        actual["content"] += " "
        original(self, prepared=prepared, computation=computation)

    monkeypatch.setattr(CurrentContinuationMessageProducers, "register_prepared_value_batch_messages", changed)
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (_store, owner, running, _sdk):
        with pytest.raises(AuditIntegrityError, match="changed the original serialized worker output"):
            await _prepare(owner, running)
