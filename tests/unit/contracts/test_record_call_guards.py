"""Audit parent guards at the context and row-client ownership boundaries.

PluginContext records operation calls. Row clients carry a scheduler claim,
and the database verifies that the state belongs to that exact claimed token.
"""

import pytest

from elspeth.contracts import CallStatus, CallType, FrameworkBugError
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.core.landscape.scheduler import serialize_row_payload
from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_operation_context, make_token_info
from tests.fixtures.landscape import make_recorder_with_run


def test_record_call_requires_landscape() -> None:
    ctx = PluginContext(run_id="run-1", config={}, state_id="state-1")
    with pytest.raises(FrameworkBugError, match=r"record_call.*without landscape"):
        ctx.record_call(call_type=CallType.LLM, status=CallStatus.SUCCESS, request_data={"prompt": "test"})


@pytest.mark.parametrize(
    ("state_id", "operation_id", "token_id"),
    [
        ("state-1", "op-1", "token-1"),
        (None, None, None),
        ("state-orphan", None, "token-1"),
        ("state-1", None, "token-STALE"),
        ("state-1", None, "token-1"),
        ("state-1", None, None),
    ],
)
def test_context_refuses_row_or_missing_parent_before_writing(state_id, operation_id, token_id) -> None:
    """Even matching row identities cannot route around the row audit client."""
    setup = make_recorder_with_run()
    ctx = PluginContext(
        run_id=setup.run_id,
        config={},
        landscape=setup.factory.plugin_audit_writer(),
        coordination_token=setup.coordination_token,
        state_id=state_id,
        operation_id=operation_id,
        token=make_token_info(token_id=token_id) if token_id is not None else None,
    )
    try:
        with pytest.raises(FrameworkBugError, match="requires an operation parent"):
            ctx.record_call(call_type=CallType.LLM, status=CallStatus.SUCCESS, request_data={"prompt": "test"})
        assert setup.query.get_all_calls_for_run(setup.run_id) == []
        assert setup.execution.get_all_operation_calls_for_run(setup.run_id) == []
    finally:
        setup.db.close()


def test_operation_call_without_row_token_preserves_parent_and_payload() -> None:
    ctx = make_operation_context()
    assert ctx.token is None
    call = ctx.record_call(
        call_type=CallType.LLM,
        status=CallStatus.SUCCESS,
        request_data={"prompt": "test"},
        response_data={"answer": "ok"},
        latency_ms=100.0,
    )
    assert call is not None
    assert call.operation_id == ctx.operation_id
    assert call.state_id is None
    assert call.request_hash is not None
    assert call.response_hash is not None
    assert call.latency_ms == 100.0


@pytest.mark.parametrize("parent", ["missing", "other-token", "matching-token"])
def test_row_client_checks_state_against_actual_claim(parent: str) -> None:
    """Preserve orphan/mismatch refusal and matching-token success in the DB owner."""
    setup = make_recorder_with_run()
    member = setup.coordination_token.membership
    row_data = make_pipeline_row({"value": 1})
    try:
        row, token = setup.data_flow.create_row_with_token(
            coordination_token=setup.coordination_token,
            source_node_id=setup.source_node_id,
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=row_data.to_dict(),
        )
        item = setup.factory.scheduler.enqueue_ready_claimed(
            member_token=member,
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=setup.source_node_id,
            step_index=0,
            ingest_sequence=0,
            row_payload_json=serialize_row_payload(row_data),
            lease_owner=member.worker_id,
            lease_seconds=300,
        )
        state_id = "state-orphan"
        if parent != "missing":
            state_token_id = token.token_id
            if parent == "other-token":
                _, other = setup.data_flow.create_row_with_token(
                    coordination_token=setup.coordination_token,
                    source_node_id=setup.source_node_id,
                    row_index=1,
                    source_row_index=1,
                    ingest_sequence=1,
                    data={"value": 2},
                )
                state_token_id = other.token_id
            state = setup.execution.begin_node_state(
                token_id=state_token_id,
                node_id=setup.source_node_id,
                step_index=0,
                input_data=row_data.to_dict(),
                member_token=member,
            )
            state_id = state.state_id
        client = AuditedHTTPClient(
            execution=setup.execution,
            state_id=state_id,
            run_id=setup.run_id,
            member_token=member,
            work_item=item,
            telemetry_emit=lambda _: None,
        )
        try:
            if parent == "matching-token":
                assert client._next_call_index() == 0
                assert client._next_call_index() == 1
                recorded = client._record_call(
                    call_index=0,
                    call_type=CallType.HTTP,
                    status=CallStatus.SUCCESS,
                    request_data=RawCallPayload({"url": "https://example.com"}),
                    response_data=RawCallPayload({"status": 200}),
                )
                calls = setup.query.get_all_calls_for_run(setup.run_id)
                assert len(calls) == 1
                assert calls[0].call_id == recorded.call_id
                assert calls[0].state_id == state_id
                assert calls[0].status is CallStatus.SUCCESS
                assert calls[0].response_hash is not None
            else:
                with pytest.raises(AuditIntegrityError, match="state does not belong to the claimed work item"):
                    client._next_call_index()
                with pytest.raises(AuditIntegrityError, match="state does not belong to the claimed work item"):
                    client._record_call(
                        call_index=0,
                        call_type=CallType.HTTP,
                        status=CallStatus.SUCCESS,
                        request_data=RawCallPayload({"url": "https://example.com"}),
                    )
                assert setup.query.get_all_calls_for_run(setup.run_id) == []
        finally:
            client.close()
    finally:
        setup.db.close()
