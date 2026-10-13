"""Required audit preparation and physical SQL share the exact registered owner."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.audit import begin_dispatch, finish_success
from elspeth.web.composer.diagnostic_evidence import DIAGNOSTIC_EVIDENCE_KIND, ComposerDiagnosticEvidence
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkCoordinator,
    RequiredWorkSource,
    RequiredWorkSubphase,
)
from elspeth.web.sessions.models import chat_messages_table
from elspeth.web.sessions.routes import _helpers
from tests.unit.web.sessions.test_token_usage_adapters import _call, _quota_service


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["llm", "cohort", "dispatch"])
@pytest.mark.parametrize("failure_phase", [None, "preparation", "sql"])
async def test_required_audit_owner_prepares_and_joins_actual_sql(tmp_path, monkeypatch, helper, failure_phase):
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Audit", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    preparation_error = AuditIntegrityError("owned audit preparation failed")
    sql_error = OperationalError("LOCAL_SQL_CANARY", {}, RuntimeError("local physical SQL fault"))
    entered_sql = []
    original_write = service._write_audit_cohort_on_connection

    def write(*args, **kwargs):
        entered_sql.append(True)
        if failure_phase == "sql":
            raise sql_error
        return original_write(*args, **kwargs)

    def preparation(*args, **kwargs):
        raise preparation_error

    monkeypatch.setattr(service, "_write_audit_cohort_on_connection", write)
    if failure_phase == "preparation":
        monkeypatch.setattr(
            _helpers,
            "redacted_tool_invocation_content_and_envelope" if helper == "dispatch" else "llm_call_audit_summary",
            preparation,
        )
    call = _call()
    invocation = finish_success(
        begin_dispatch(str(uuid4()), "list_sources", {}, version_before=0, actor="composer-web:user:alice"),
        result_payload={"success": True},
        version_after=0,
    )
    if helper == "llm":
        operation = _helpers._persist_llm_calls(
            service,
            session.id,
            (call,),
            None,
            plugin_crash_pending=True,
            required_audit=True,
            required_work=coordinator,
            session_operation_context=context,
        )
    elif helper == "cohort":
        operation = _helpers._persist_turn_audit_cohort(
            service,
            session.id,
            (),
            (call,),
            tool_composition_state_id=None,
            llm_composition_state_id=None,
            plugin_crash_pending=True,
            required_audit=True,
            required_work=coordinator,
            session_operation_context=context,
        )
    else:
        operation = _helpers._persist_tool_invocations(
            service,
            session.id,
            (invocation,),
            None,
            plugin_crash_pending=True,
            required_audit=True,
            required_work=coordinator,
            session_operation_context=context,
        )
    if failure_phase is None:
        await operation
    else:
        with pytest.raises(AuditIntegrityError) as caught:
            await operation
        if failure_phase == "preparation":
            assert caught.value is preparation_error
        else:
            assert caught.value.__cause__ is sql_error
    coordinator.assert_completed()
    expected_sql = RequiredWorkSource.DISPATCH_AUDIT_SQL if helper == "dispatch" else RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL
    expected_projection = (
        RequiredWorkSource.DISPATCH_AUDIT_PROJECTION if helper == "dispatch" else RequiredWorkSource.REQUIRED_UNWIND_AUDIT_PROJECTION
    )
    assert {(ticket.key.source, ticket.key.subphase) for ticket in coordinator.tickets} == {
        (expected_sql, RequiredWorkSubphase.PRODUCER),
        (expected_sql, RequiredWorkSubphase.SQL_INITIAL),
        (expected_projection, RequiredWorkSubphase.PROJECTION),
    }
    assert len(entered_sql) == (0 if failure_phase == "preparation" else 1)
    with engine.connect() as connection:
        rows = connection.execute(select(chat_messages_table).order_by(chat_messages_table.c.sequence_no)).all()
    expected_count = (2 if helper == "dispatch" else 1) if failure_phase is None else 0
    assert len(rows) == expected_count
    if failure_phase is None:
        assert all(row.role == "audit" and row.writer_principal == "compose_loop" and len(row.tool_calls) == 1 for row in rows)
        envelopes = [row.tool_calls[0] for row in rows]
        if helper == "dispatch":
            assert [envelope["_kind"] for envelope in envelopes] == [DIAGNOSTIC_EVIDENCE_KIND, "audit"]
            evidence = ComposerDiagnosticEvidence.from_envelope(
                envelopes[0],
                expected_session_id=str(session.id),
                expected_operation_id=context.fence.operation_id,
                expected_tool_call_id=invocation.tool_call_id,
            )
            assert evidence.operation_epoch == context.fence.operation_epoch
            assert evidence.tool_name == invocation.tool_name and evidence.status == invocation.status.value
            assert envelopes[1]["invocation"]["tool_call_id"] == invocation.tool_call_id
        else:
            assert [envelope["_kind"] for envelope in envelopes] == ["llm_call_audit"]
            assert envelopes[0]["call"]["call_id"] == call.call_id
    authority.release(context)
    engine.dispose()
