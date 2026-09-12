"""Provider usage remains available after call payload retention expires."""

from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.token_usage import TokenUsage
from elspeth.core.landscape.export_read_model import open_export_read_transaction
from elspeth.core.landscape.exporter import LandscapeExporter
from elspeth.core.landscape.schema import calls_table
from elspeth.plugins.infrastructure.clients.llm import AuditedLLMClient, LLMClientError
from tests.fixtures.landscape import leader_coordination_token
from tests.unit.core.landscape.test_call_recording import _claim_state, _setup_with_operation
from tests.unit.core.landscape.test_exporter import (
    _NODE_STATE_COMPLETED,
    _OP_CALL,
    _OPERATION,
    _ROW,
    _STATE_CALL,
    _TOKEN,
    _make_exporter,
)
from tests.unit.plugins.clients.test_audited_llm_client import FakeOpenAIClient, provider_response


@pytest.mark.parametrize("operation", [False, True])
@pytest.mark.parametrize(
    "usage",
    [
        TokenUsage(),
        TokenUsage(prompt_tokens=0),
        TokenUsage(prompt_tokens=17, completion_tokens=9, cached_prompt_tokens=3, reasoning_tokens=2),
    ],
)
def test_call_usage_round_trip(operation: bool, usage: TokenUsage) -> None:
    db, factory, state_id, operation_id = _setup_with_operation()
    token = leader_coordination_token(factory, "run-1")
    if operation:
        call = factory.execution.record_operation_call(
            operation_id,
            CallType.LLM,
            CallStatus.SUCCESS,
            RawCallPayload({}),
            coordination_token=token,
            token_usage=usage,
        )
        loaded = factory.execution.get_operation_calls(operation_id)[0]
    else:
        call = factory.execution.record_call(
            state_id,
            0,
            CallType.LLM,
            CallStatus.SUCCESS,
            RawCallPayload({}),
            member_token=token.membership,
            work_item=_claim_state(factory, state_id, member_token=token.membership),
            token_usage=usage,
        )
        loaded = factory.query.get_calls(state_id)[0]
    with db.read_only_connection() as conn:
        row = conn.execute(select(calls_table).where(calls_table.c.call_id == call.call_id)).one()
    for actual in (call, loaded, row):
        assert actual.prompt_tokens == usage.prompt_tokens
        assert actual.completion_tokens == usage.completion_tokens
        assert actual.cached_prompt_tokens == usage.cached_prompt_tokens
        assert actual.reasoning_tokens == usage.reasoning_tokens
    # Exercise both call projections against a caller-owned snapshot; terminal
    # run admission is separately covered by the public exporter contracts.
    with open_export_read_transaction(db.engine) as read_model:
        records = list(LandscapeExporter(db, read_model=read_model).iter_unsigned_run_records("run-1"))
    exported = [record for record in records if record["record_type"] == "call"]
    assert len(exported) == 1
    assert exported[0]["prompt_tokens"] == usage.prompt_tokens
    assert exported[0]["completion_tokens"] == usage.completion_tokens
    assert exported[0]["cached_prompt_tokens"] == usage.cached_prompt_tokens
    assert exported[0]["reasoning_tokens"] == usage.reasoning_tokens


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "cached_prompt_tokens", "reasoning_tokens"])
def test_database_rejects_negative_call_usage(field: str) -> None:
    db, factory, _, operation_id = _setup_with_operation()
    call = factory.execution.record_operation_call(
        operation_id,
        CallType.LLM,
        CallStatus.SUCCESS,
        RawCallPayload({}),
        coordination_token=leader_coordination_token(factory, "run-1"),
    )
    with pytest.raises(IntegrityError), db.write_connection() as conn:
        conn.execute(calls_table.update().where(calls_table.c.call_id == call.call_id).values({field: -1}))


@pytest.mark.parametrize("operation", [False, True])
@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "cached_prompt_tokens", "reasoning_tokens"])
def test_signed_call_record_distinguishes_unknown_zero_and_reported_usage(operation: bool, field: str) -> None:
    signatures: list[str] = []
    for count in (None, 0, 5):
        call = replace(_OP_CALL if operation else _STATE_CALL, **{field: count})
        exporter = _make_exporter(
            signing_key=b"call-token-test-key",
            operations=[_OPERATION] if operation else [],
            operation_calls=[call] if operation else [],
            rows=[] if operation else [_ROW],
            tokens=[] if operation else [_TOKEN],
            node_states=[] if operation else [_NODE_STATE_COMPLETED],
            state_calls=[] if operation else [call],
        )
        records = list(exporter.export_run("run-1", sign=True))
        calls = [record for record in records if record["record_type"] == "call"]
        assert len(calls) == 1
        assert calls[0][field] == count
        signatures.append(calls[0]["signature"])
    assert len(set(signatures)) == 3


@pytest.mark.parametrize("operation", [False, True])
@pytest.mark.parametrize("serialization_failure", [False, True])
def test_provider_usage_reaches_database_despite_response_serialization_failure(operation: bool, serialization_failure: bool) -> None:
    db, factory, state_id, operation_id = _setup_with_operation()
    token = leader_coordination_token(factory, "run-1")
    raw_usage = {
        "prompt_tokens": 17,
        "completion_tokens": 9,
        "prompt_tokens_details": {"cached_tokens": 3},
        "completion_tokens_details": {"reasoning_tokens": 2},
        "total_tokens": 26,
        "cache_creation_input_tokens": 4,
        "cache_read_input_tokens": 5,
    }
    provider = FakeOpenAIClient(
        response=provider_response(
            usage=raw_usage,
            raw_response={"usage": raw_usage},
            model_dump_error=TypeError("cannot serialize") if serialization_failure else None,
        )
    )
    client = AuditedLLMClient(
        execution=factory.execution,
        state_id=None if operation else state_id,
        operation_id=operation_id if operation else None,
        run_id="run-1",
        coordination_token=token if operation else None,
        member_token=None if operation else token.membership,
        work_item=None if operation else _claim_state(factory, state_id, member_token=token.membership),
        telemetry_emit=lambda event: None,
        underlying_client=provider,
    )
    if serialization_failure:
        with pytest.raises(LLMClientError, match="serialize"):
            client.chat_completion(model="gpt-4", messages=[ChatMessage(role="user", content="hello")])
    else:
        client.chat_completion(model="gpt-4", messages=[ChatMessage(role="user", content="hello")])
    with db.read_only_connection() as conn:
        row = conn.execute(select(calls_table)).one()
    assert (row.prompt_tokens, row.completion_tokens, row.cached_prompt_tokens, row.reasoning_tokens) == (17, 9, 3, 2)
    assert row.status == ("error" if serialization_failure else "success")
    if not serialization_failure:
        payload = factory.execution.get_call_response_data(row.call_id)
        assert payload.data is not None
        assert payload.data["usage"]["total_tokens"] == 26
        assert payload.data["usage"]["cache_creation_input_tokens"] == 4
        assert payload.data["usage"]["cache_read_input_tokens"] == 5
