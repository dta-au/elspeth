"""Physical checkpoints retain inseparable public-call coverage evidence."""

from dataclasses import replace

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.visible_tool_event import validate_visible_response_gap, visible_response_gap
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.models import chat_messages_table, token_usage_ledger_table
from tests.unit.web.sessions.test_composer_provider_telemetry import _call, _service


@pytest.mark.asyncio
async def test_physical_checkpoint_and_later_cohort_keep_one_exact_call_gap_pair(engine):
    service = _service(engine)
    session = await service.create_session("alice", "atomic coverage", "local")
    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as lease:
        attempt = await service.begin_provider_attempt(session_operation_context=lease.context, source="composer")
        call = replace(_call(), call_id=attempt.attempt_id, started_at=attempt.started_at, finished_at=attempt.started_at)
        public = llm_call_audit_envelope(call)
        gap = visible_response_gap(call, lease.context, public)
        await service.finish_provider_attempt(session_operation_context=lease.context, call=call)
        initial = await service.get_messages(session.id, limit=None, include_private_evidence=True)
        assert len(initial) == 1
        assert deep_thaw(initial[0].tool_calls) == [public, gap]
        validate_visible_response_gap(initial[0].tool_calls[1], initial[0].tool_calls[0])

        await service.add_messages_atomic(
            session.id,
            (AuditMessageDraft(role="audit", content="later planner cohort", tool_calls=(public, gap)),),
            writer_principal="compose_loop",
            session_operation_context=lease.context,
        )
        assert await service.get_messages(session.id, limit=None, include_private_evidence=True) == initial
        with engine.connect() as conn:
            assert len(conn.execute(select(token_usage_ledger_table.c.entry_id)).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("missing_gap", "gap_hash", "public_metadata"))
async def test_checkpoint_replay_refuses_changed_durable_companion_without_writing(engine, mutation):
    service = _service(engine)
    session = await service.create_session("alice", "changed coverage", "local")
    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as lease:
        attempt = await service.begin_provider_attempt(session_operation_context=lease.context, source="composer")
        call = replace(_call(), call_id=attempt.attempt_id, started_at=attempt.started_at, finished_at=attempt.started_at)
        await service.finish_provider_attempt(session_operation_context=lease.context, call=call)
        with engine.begin() as conn:
            row = conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id))).one()
            envelopes = deep_thaw(row.tool_calls)
            assert len(envelopes) == 2
            if mutation == "missing_gap":
                envelopes.pop()
            elif mutation == "gap_hash":
                envelopes[1]["public_call_hash"] = "0" * 64
            else:
                envelopes[0]["call"]["provider_request_id"] = "changed-durable-request"
            conn.execute(chat_messages_table.update().where(chat_messages_table.c.id == row.id).values(tool_calls=envelopes))
        with pytest.raises(AuditIntegrityError, match=r"checkpoint.*companion"):
            await service.finish_provider_attempt(session_operation_context=lease.context, call=call)
        with engine.connect() as conn:
            rows = conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id))).all()
            assert len(rows) == 1 and rows[0].tool_calls == envelopes
            assert len(conn.execute(select(token_usage_ledger_table.c.entry_id)).all()) == 1
