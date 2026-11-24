"""A call record refused by a fence still leaves the call's identity and usage in the audit.

A provider call that completes after its writer lost authority has already
happened and may have been billed. The fence keeps the stale writer out of the
``calls`` table, so the refusal event is the only durable record of that call;
it must carry enough to identify the call and its usage.
"""

import json
from dataclasses import dataclass, replace

import pytest
from sqlalchemy import func, select

from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.coordination import WorkerMembershipToken
from elspeth.contracts.errors import RunLeadershipLostError, RunMembershipLostError, SchedulerLeaseLostError
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.token_usage import TokenUsage
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.scheduler.payload_codec import serialize_row_payload
from elspeth.core.landscape.schema import calls_table, run_coordination_events_table, token_work_items_table
from tests.fixtures.landscape import RecorderSetup, make_recorder_with_run, register_test_node

_USAGE = TokenUsage(prompt_tokens=120, completion_tokens=45, cached_prompt_tokens=7, reasoning_tokens=3)
_REQUEST = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
_RESPONSE = {"content": "y"}


@dataclass
class _Setup:
    recorder: RecorderSetup
    member: WorkerMembershipToken
    item: TokenWorkItem
    state_id: str


def _setup() -> _Setup:
    recorder = make_recorder_with_run(leader_worker_id="leader")
    register_test_node(recorder.data_flow, recorder.run_id, "transform")
    run = recorder.run_lifecycle.get_run(recorder.run_id)
    assert run is not None
    member = recorder.factory.run_coordination.admit_follower(
        run_id=recorder.run_id,
        worker_id="follower",
        config_hash=run.config_hash,
        window_seconds=300,
    )
    row, token = recorder.data_flow.create_row_with_token(
        recorder.source_node_id,
        0,
        {"value": 1},
        source_row_index=0,
        ingest_sequence=0,
        coordination_token=recorder.coordination_token,
    )
    item = recorder.factory.scheduler.enqueue_ready_claimed(
        member_token=member,
        token_id=token.token_id,
        row_id=row.row_id,
        node_id="transform",
        step_index=1,
        ingest_sequence=0,
        row_payload_json=serialize_row_payload(PipelineRow({"value": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True))),
        lease_owner=member.worker_id,
        lease_seconds=300,
    )
    state_id = recorder.execution.begin_node_state(item.token_id, "transform", 1, {"value": 1}, member_token=member).state_id
    return _Setup(recorder, member, item, state_id)


def _record_state_call(setup: _Setup, *, latency_ms: float = 12.5, token_usage: TokenUsage = _USAGE) -> None:
    setup.recorder.execution.record_call(
        setup.state_id,
        0,
        CallType.LLM,
        CallStatus.SUCCESS,
        RawCallPayload(_REQUEST),
        RawCallPayload(_RESPONSE),
        latency_ms=latency_ms,
        token_usage=token_usage,
        call_id="attempt-late-1",
        member_token=setup.member,
        work_item=setup.item,
    )


def _fence_refusals(setup: _Setup) -> list[dict[str, object]]:
    with setup.recorder.db.read_only_connection() as conn:
        rows = conn.execute(
            select(run_coordination_events_table.c.context_json)
            .where(
                run_coordination_events_table.c.run_id == setup.recorder.run_id,
                run_coordination_events_table.c.event_type == "fence_refusal",
            )
            .order_by(run_coordination_events_table.c.seq)
        ).scalars()
        return [json.loads(row) for row in rows]


def _call_count(setup: _Setup) -> int:
    with setup.recorder.db.read_only_connection() as conn:
        return conn.execute(select(func.count()).select_from(calls_table)).scalar_one()


def _assert_refused_call(refused: object, *, call_id: str, state_id: str | None, operation_id: str | None) -> None:
    assert refused == {
        "call_id": call_id,
        "state_id": state_id,
        "operation_id": operation_id,
        "call_index": 0,
        "call_type": "llm",
        "status": "success",
        "request_hash": stable_hash(_REQUEST),
        "response_hash": stable_hash(_RESPONSE),
        "prompt_tokens": 120,
        "completion_tokens": 45,
        "cached_prompt_tokens": 7,
        "reasoning_tokens": 3,
        "latency_ms": 12.5,
    }


def test_call_record_refused_by_item_lease_records_the_call() -> None:
    setup = _setup()
    with setup.recorder.db.write_connection() as conn:
        conn.execute(
            token_work_items_table.update()
            .where(token_work_items_table.c.work_item_id == setup.item.work_item_id)
            .values(status="terminal", lease_owner=None)
        )

    with pytest.raises(SchedulerLeaseLostError):
        _record_state_call(setup)

    assert _call_count(setup) == 0
    refusals = _fence_refusals(setup)
    assert len(refusals) == 1
    context = refusals[0]
    assert context["verb"] == "record_call"
    assert context["fence"] == "item"
    assert context["work_item_id"] == setup.item.work_item_id
    assert context["attempt"] == setup.item.attempt
    _assert_refused_call(context["refused_call"], call_id="attempt-late-1", state_id=setup.state_id, operation_id=None)


def test_call_record_refused_by_departed_membership_records_the_call() -> None:
    setup = _setup()
    setup.recorder.factory.run_coordination.depart_worker(member_token=setup.member)
    before = len(_fence_refusals(setup))

    with pytest.raises(RunMembershipLostError):
        _record_state_call(setup)

    assert _call_count(setup) == 0
    refusals = _fence_refusals(setup)[before:]
    # The membership fence's own refusal, then the refused call's record.
    assert refusals[0] == {"verb": "record_call", "fence": "membership"}
    assert len(refusals) == 2
    context = refusals[1]
    assert context["verb"] == "record_call"
    assert context["fence"] == "membership"
    assert context["work_item_id"] == setup.item.work_item_id
    assert context["attempt"] == setup.item.attempt
    _assert_refused_call(context["refused_call"], call_id="attempt-late-1", state_id=setup.state_id, operation_id=None)


def test_operation_call_record_refused_by_leader_fence_records_the_call() -> None:
    setup = _setup()
    repo = setup.recorder.execution
    operation = repo.begin_operation(setup.recorder.source_node_id, "source_load", coordination_token=setup.recorder.coordination_token)
    stale = replace(setup.recorder.coordination_token, leader_epoch=setup.recorder.coordination_token.leader_epoch + 1)
    before = len(_fence_refusals(setup))

    with pytest.raises(RunLeadershipLostError):
        repo.record_operation_call(
            operation.operation_id,
            CallType.LLM,
            CallStatus.SUCCESS,
            RawCallPayload(_REQUEST),
            RawCallPayload(_RESPONSE),
            latency_ms=12.5,
            token_usage=_USAGE,
            call_id="attempt-late-2",
            call_index=0,
            coordination_token=stale,
        )

    assert _call_count(setup) == 0
    refusals = _fence_refusals(setup)[before:]
    # The leader fence's own refusal, then the refused call's record.
    assert refusals[0] == {"verb": "record_operation_call"}
    assert len(refusals) == 2
    context = refusals[1]
    assert context["verb"] == "record_operation_call"
    assert context["fence"] == "leader"
    assert context["work_item_id"] is None
    assert context["attempt"] is None
    _assert_refused_call(context["refused_call"], call_id="attempt-late-2", state_id=None, operation_id=operation.operation_id)


def test_source_call_refused_at_index_allocation_records_the_call_without_an_index() -> None:
    """A source plugin's call (PluginContext.record_call allocates after the call) is not lost.

    The fence refuses the index allocation itself, so no index or generated id
    was ever assigned: the record carries both as absent, never a placeholder,
    and its verb names the allocation.
    """
    from elspeth.contracts.plugin_context import PluginContext
    from elspeth.core.landscape.plugin_audit_writer import PluginAuditWriterAdapter

    setup = _setup()
    recorder = setup.recorder
    operation = recorder.execution.begin_operation(recorder.source_node_id, "source_load", coordination_token=recorder.coordination_token)
    stale = replace(recorder.coordination_token, leader_epoch=recorder.coordination_token.leader_epoch + 1)
    ctx = PluginContext(
        run_id=recorder.run_id,
        config={},
        landscape=PluginAuditWriterAdapter(recorder.execution, recorder.data_flow, recorder.run_lifecycle),
        node_id=recorder.source_node_id,
        operation_id=operation.operation_id,
        coordination_token=stale,
    )
    before = len(_fence_refusals(setup))

    with pytest.raises(RunLeadershipLostError):
        ctx.record_call(CallType.HTTP, CallStatus.SUCCESS, {"url": "https://example.test/object"}, {"status": 200}, latency_ms=8.0)

    assert _call_count(setup) == 0
    refusals = _fence_refusals(setup)[before:]
    # The leader fence's own refusal of the allocation, then the call's record.
    assert refusals[0] == {"verb": "allocate_operation_call_index"}
    assert len(refusals) == 2
    record = refusals[1]
    assert record["verb"] == "allocate_operation_call_index"
    assert record["fence"] == "leader"
    refused = record["refused_call"]
    assert isinstance(refused, dict)
    assert refused["call_index"] is None
    assert refused["call_id"] is None
    assert refused["operation_id"] == operation.operation_id
    assert refused["state_id"] is None
    assert refused["call_type"] == "http"
    assert refused["status"] == "success"
    assert refused["request_hash"] == stable_hash({"url": "https://example.test/object"})
    assert refused["response_hash"] == stable_hash({"status": 200})
    assert refused["latency_ms"] == 8.0


def test_refused_call_index_allocation_adds_no_item_refusal() -> None:
    """A bare row-call index allocation names no call, so its item-lease refusal writes no record, as before."""
    setup = _setup()
    with setup.recorder.db.write_connection() as conn:
        conn.execute(
            token_work_items_table.update()
            .where(token_work_items_table.c.work_item_id == setup.item.work_item_id)
            .values(lease_owner="leader")
        )

    with pytest.raises(SchedulerLeaseLostError):
        setup.recorder.execution.allocate_call_index(setup.state_id, member_token=setup.member, work_item=setup.item)

    assert _fence_refusals(setup) == []


def test_recorded_call_adds_no_refusal() -> None:
    setup = _setup()
    _record_state_call(setup)

    assert _call_count(setup) == 1
    assert _fence_refusals(setup) == []


def test_refused_call_record_lost_to_a_db_fault_is_logged_with_its_identity(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When the audit store cannot hold the refusal either, the last-resort log names the call."""
    from sqlalchemy.exc import OperationalError

    from elspeth.core.landscape import run_coordination_repository

    setup = _setup()
    with setup.recorder.db.write_connection() as conn:
        conn.execute(
            token_work_items_table.update()
            .where(token_work_items_table.c.work_item_id == setup.item.work_item_id)
            .values(status="terminal", lease_owner=None)
        )

    def disk_fault(*args: object, **kwargs: object) -> None:
        raise OperationalError("INSERT INTO run_coordination_events", {}, Exception("disk I/O error"))

    monkeypatch.setattr(run_coordination_repository, "record_coordination_event", disk_fault)

    with caplog.at_level("ERROR", logger=run_coordination_repository.__name__), pytest.raises(SchedulerLeaseLostError):
        _record_state_call(setup)

    assert _call_count(setup) == 0
    assert _fence_refusals(setup) == []
    lost = [record for record in caplog.records if record.getMessage().startswith("refused call record for run")]
    assert len(lost) == 1
    assert lost[0].levelname == "ERROR"
    message = lost[0].getMessage()
    assert "(database fault)" in message
    assert "call_id='attempt-late-1'" in message
    assert "prompt_tokens=120" in message
    assert "completion_tokens=45" in message


@pytest.mark.parametrize(
    ("latency_ms", "token_usage", "logged"),
    [
        pytest.param(float("nan"), _USAGE, ("latency_ms=nan", "prompt_tokens=120"), id="non_finite_latency"),
        pytest.param(
            12.5,
            TokenUsage(prompt_tokens=2**53, completion_tokens=45),
            ("latency_ms=12.5", f"prompt_tokens={2**53}"),
            id="token_count_beyond_canonical_integer_range",
        ),
    ],
)
def test_refused_call_with_a_value_canonical_json_refuses_keeps_the_refusal_and_logs_the_call(
    caplog: pytest.LogCaptureFixture, latency_ms: float, token_usage: TokenUsage, logged: tuple[str, str]
) -> None:
    """A provider value the contracts admit but the audit store cannot hold must not replace the fence's refusal."""
    from elspeth.core.landscape import run_coordination_repository

    setup = _setup()
    with setup.recorder.db.write_connection() as conn:
        conn.execute(
            token_work_items_table.update()
            .where(token_work_items_table.c.work_item_id == setup.item.work_item_id)
            .values(status="terminal", lease_owner=None)
        )

    with caplog.at_level("ERROR", logger=run_coordination_repository.__name__), pytest.raises(SchedulerLeaseLostError):
        _record_state_call(setup, latency_ms=latency_ms, token_usage=token_usage)

    assert _call_count(setup) == 0
    assert _fence_refusals(setup) == []
    lost = [record for record in caplog.records if record.getMessage().startswith("refused call record for run")]
    assert len(lost) == 1
    assert lost[0].levelname == "ERROR"
    message = lost[0].getMessage()
    assert "(not canonical JSON: " in message
    assert "call_id='attempt-late-1'" in message
    for fragment in logged:
        assert fragment in message
