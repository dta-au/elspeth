"""A call record refused by a fence still leaves the call's identity and usage in the audit.

A provider call that completes after its writer lost authority has already
happened and may have been billed. The fence keeps the stale writer out of the
``calls`` table, so the refusal event is the only durable record of that call;
it must carry enough to identify the call and its usage.
"""

import json
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, ClassVar

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


def test_refused_call_bool_field_is_recorded_as_a_bool_not_an_integer() -> None:
    """Normalising the record's fields to exact built-ins keeps a bool a bool (true, never 1)."""
    setup = _setup()
    with setup.recorder.db.write_connection() as conn:
        conn.execute(
            token_work_items_table.update()
            .where(token_work_items_table.c.work_item_id == setup.item.work_item_id)
            .values(status="terminal", lease_owner=None)
        )

    with pytest.raises(SchedulerLeaseLostError):
        _record_state_call(setup, latency_ms=True)

    refusals = _fence_refusals(setup)
    assert len(refusals) == 1
    refused_call = refusals[0]["refused_call"]
    assert isinstance(refused_call, dict)
    assert refused_call["latency_ms"] is True


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


class _RaisingReprInt(int):
    """An ``int`` subclass ``TokenUsage`` admits, whose own ``repr`` raises."""

    def __repr__(self) -> str:
        raise RuntimeError("repr exploded")


class _RaisingReprFloat(float):
    """A ``float`` subclass an untyped ``latency_ms`` admits, whose own ``repr`` raises."""

    def __repr__(self) -> str:
        raise RuntimeError("repr exploded")


@pytest.mark.parametrize(
    ("latency_ms", "token_usage", "logged"),
    [
        pytest.param(float("nan"), _USAGE, ("latency_ms=nan", "prompt_tokens=120"), id="non_finite_latency"),
        pytest.param(
            12.5,
            TokenUsage(prompt_tokens=2**53, completion_tokens=45),
            ("latency_ms=12.5", f"prompt_tokens={hex(2**53)}"),
            id="token_count_beyond_canonical_integer_range",
        ),
        pytest.param(
            12.5,
            TokenUsage(prompt_tokens=10**5000, completion_tokens=45),
            ("latency_ms=12.5", f"prompt_tokens={hex(10**5000)}"),
            id="token_count_beyond_the_decimal_conversion_limit",
        ),
        pytest.param(
            12.5,
            TokenUsage(prompt_tokens=_RaisingReprInt(2**53), completion_tokens=45),
            ("latency_ms=12.5", f"prompt_tokens={hex(2**53)}"),
            id="int_subclass_with_raising_repr_beyond_canonical_range",
        ),
        pytest.param(
            _RaisingReprFloat("nan"),
            _USAGE,
            ("latency_ms=nan", "prompt_tokens=120"),
            id="non_finite_float_subclass_with_raising_repr",
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
    # The encoder's own refusal is kept, not just its name: an encoder defect
    # raising ValueError or TypeError stays diagnosable from this log alone.
    assert lost[0].exc_info is not None
    refusal_type = lost[0].exc_info[0]
    assert refusal_type is not None
    assert issubclass(refusal_type, (ValueError, TypeError))
    assert f"(not canonical JSON: {refusal_type.__name__})" in message
    assert "call_id='attempt-late-1'" in message
    for fragment in logged:
        assert fragment in message


class _HostileInt(int):
    def __repr__(self) -> str:
        raise RuntimeError("repr exploded")

    def __str__(self) -> str:
        raise RuntimeError("str exploded")

    def __format__(self, spec: str) -> str:
        raise RuntimeError("format exploded")


class _HostileFloat(float):
    def __repr__(self) -> str:
        raise RuntimeError("repr exploded")

    def __str__(self) -> str:
        raise RuntimeError("str exploded")

    def __format__(self, spec: str) -> str:
        raise RuntimeError("format exploded")


class _HostileStr(str):
    def __repr__(self) -> str:
        raise RuntimeError("repr exploded")

    def __str__(self) -> str:
        raise RuntimeError("str exploded")

    def __format__(self, spec: str) -> str:
        raise RuntimeError("format exploded")


@pytest.mark.parametrize(
    ("value", "formatted"),
    [
        pytest.param(None, "None", id="none"),
        pytest.param(True, "True", id="bool_true"),
        pytest.param(False, "False", id="bool_false"),
        pytest.param(120, "120", id="int_in_range"),
        pytest.param(-(2**53), hex(-(2**53)), id="negative_int_beyond_range"),
        pytest.param(_HostileInt(7), "7", id="hostile_int_subclass_in_range"),
        pytest.param(_HostileInt(2**53), hex(2**53), id="hostile_int_subclass_beyond_range"),
        pytest.param(_HostileFloat("nan"), "nan", id="hostile_float_subclass_nan"),
        pytest.param(_HostileFloat(12.5), "12.5", id="hostile_float_subclass"),
        pytest.param(_HostileStr("attempt-late-1"), "'attempt-late-1'", id="hostile_str_subclass"),
    ],
)
def test_last_resort_log_value_formats_through_the_builtin_base_class(value: str | int | float | None, formatted: str) -> None:
    """The last-resort formatter never reaches a subclass's own repr, str or format, and formats bool as itself."""
    from elspeth.core.landscape.run_coordination_repository import _last_resort_log_value

    assert _last_resort_log_value(value) == formatted


@pytest.mark.parametrize(
    ("value", "expected_type"),
    [
        pytest.param(_HostileInt(2**53), int, id="int_subclass"),
        pytest.param(_HostileFloat("nan"), float, id="float_subclass"),
        pytest.param(_HostileStr("attempt-late-1"), str, id="str_subclass"),
        pytest.param(True, bool, id="bool"),
        pytest.param(None, type(None), id="none"),
    ],
)
def test_refused_call_fields_are_exact_builtins(value: object, expected_type: type) -> None:
    """Every refused-call field reaches canonical JSON and the log as an exact built-in, never a subclass."""
    from elspeth.core.landscape.run_coordination_repository import _exact_builtin

    assert type(_exact_builtin(value)) is expected_type


def test_refused_call_field_of_another_type_is_refused() -> None:
    """A container (or any other type) in a refused-call field is a programming error, refused by type."""
    from elspeth.core.landscape.run_coordination_repository import _exact_builtin

    with pytest.raises(TypeError, match="refused-call field holds list"):
        _exact_builtin([12.5])


class _AttributeTrapInt(int):
    """An int subclass whose every attribute read raises, including ``__class__``."""

    def __getattribute__(self, name: str) -> object:
        raise RuntimeError(f"attribute read {name}")


class _AttributeTrapFloat(float):
    def __getattribute__(self, name: str) -> object:
        raise RuntimeError(f"attribute read {name}")


class _AttributeTrapStr(str):
    def __getattribute__(self, name: str) -> object:
        raise RuntimeError(f"attribute read {name}")


class _HostileMeta(type):
    """A metaclass that refuses every subclass/instance check and every MRO or name lookup."""

    def __subclasscheck__(cls, subclass: type) -> bool:
        raise RuntimeError("metaclass __subclasscheck__")

    def __instancecheck__(cls, instance: object) -> bool:
        raise RuntimeError("metaclass __instancecheck__")

    def __getattribute__(cls, name: str) -> object:
        if name in {"__mro__", "__bases__", "__class__", "__name__", "__qualname__", "mro"}:
            raise RuntimeError(f"metaclass attribute read {name}")
        return super().__getattribute__(name)


class _MetaTrapFloat(float, metaclass=_HostileMeta):
    pass


@pytest.mark.parametrize(
    ("value", "expected_type", "formatted"),
    [
        pytest.param(_AttributeTrapInt(7), int, "7", id="int_attribute_trap"),
        pytest.param(_AttributeTrapInt(2**53), int, hex(2**53), id="int_attribute_trap_beyond_range"),
        pytest.param(_AttributeTrapFloat(12.5), float, "12.5", id="float_attribute_trap"),
        pytest.param(_AttributeTrapFloat("nan"), float, "nan", id="float_attribute_trap_nan"),
        pytest.param(_AttributeTrapStr("attempt-late-1"), str, "'attempt-late-1'", id="str_attribute_trap"),
    ],
)
def test_refused_call_field_dispatch_reads_only_the_type(value: str | int | float, expected_type: type, formatted: str) -> None:
    """Neither the normaliser nor the last-resort formatter reads an attribute of the value or consults its metaclass."""
    from elspeth.core.landscape.run_coordination_repository import _exact_builtin, _last_resort_log_value

    assert type(_exact_builtin(value)) is expected_type
    assert _last_resort_log_value(value) == formatted


def test_refused_call_field_dispatch_never_consults_the_value_metaclass() -> None:
    """A metaclass that refuses subclass checks, MRO and name lookups is never consulted.

    The value is built here, not passed as a parameter, and the verdicts are
    plain values: a failure report never has to repr an object whose type
    refuses its own name.
    """
    from elspeth.core.landscape.run_coordination_repository import _exact_builtin, _last_resort_log_value

    value = _MetaTrapFloat(2.5)
    normalised_exactly = type(_exact_builtin(value)) is float
    formatted = _last_resort_log_value(value)
    assert normalised_exactly
    assert formatted == "2.5"


def _terminate_item(setup: _Setup) -> None:
    with setup.recorder.db.write_connection() as conn:
        conn.execute(
            token_work_items_table.update()
            .where(token_work_items_table.c.work_item_id == setup.item.work_item_id)
            .values(status="terminal", lease_owner=None)
        )


def test_refused_call_with_an_attribute_trap_latency_keeps_the_refusal_and_records_the_call() -> None:
    """A latency whose attribute reads raise is recorded as an exact float; the lease refusal still propagates."""
    setup = _setup()
    _terminate_item(setup)

    with pytest.raises(SchedulerLeaseLostError):
        _record_state_call(setup, latency_ms=_AttributeTrapFloat(12.5))

    refusals = _fence_refusals(setup)
    assert len(refusals) == 1
    _assert_refused_call(refusals[0]["refused_call"], call_id="attempt-late-1", state_id=setup.state_id, operation_id=None)


def test_refused_call_with_an_attribute_trap_attempt_keeps_the_refusal_and_records_the_attempt() -> None:
    """The work item's attempt is normalised like every record field; the membership refusal still propagates.

    The membership fence refuses before the item fence builds its predicate, so
    the work item reaches the refused record untouched: this is the path where
    the attempt meets canonical JSON inside the unwind.
    """
    setup = _setup()
    setup.recorder.factory.run_coordination.depart_worker(member_token=setup.member)
    trapped = replace(setup.item, attempt=_AttributeTrapInt(int.__int__(setup.item.attempt)))
    before = len(_fence_refusals(setup))

    with pytest.raises(RunMembershipLostError):
        setup.recorder.execution.record_call(
            setup.state_id,
            0,
            CallType.LLM,
            CallStatus.SUCCESS,
            RawCallPayload(_REQUEST),
            RawCallPayload(_RESPONSE),
            latency_ms=12.5,
            token_usage=_USAGE,
            call_id="attempt-late-1",
            member_token=setup.member,
            work_item=trapped,
        )

    refusals = _fence_refusals(setup)[before:]
    assert len(refusals) == 2
    context = refusals[1]
    assert context["attempt"] == setup.item.attempt
    _assert_refused_call(context["refused_call"], call_id="attempt-late-1", state_id=setup.state_id, operation_id=None)


class _ArmedUsage(TokenUsage):
    """A TokenUsage subclass whose token fields raise on read once armed."""

    __slots__ = ()
    armed: ClassVar[bool] = False
    reads: ClassVar[list[str]] = []

    def __getattribute__(self, name: str) -> object:
        if name.endswith("_tokens") and type(self).armed:
            type(self).reads.append(name)
            raise RuntimeError(f"usage field {name}")
        return super().__getattribute__(name)


def _armed_usage() -> _ArmedUsage:
    usage = _ArmedUsage(prompt_tokens=120, completion_tokens=45)
    _ArmedUsage.reads = []
    _ArmedUsage.armed = True
    return usage


def _coordination_state(setup: _Setup) -> tuple[int, int, tuple[object, ...]]:
    """Every coordination event of the run, the calls count, and the work item's lease row."""
    with setup.recorder.db.read_only_connection() as conn:
        events = conn.execute(
            select(func.count())
            .select_from(run_coordination_events_table)
            .where(run_coordination_events_table.c.run_id == setup.recorder.run_id)
        ).scalar_one()
        item_row = conn.execute(
            select(token_work_items_table.c.status, token_work_items_table.c.lease_owner, token_work_items_table.c.attempt).where(
                token_work_items_table.c.work_item_id == setup.item.work_item_id
            )
        ).one()
    return events, _call_count(setup), tuple(item_row)


def test_token_usage_subclass_is_refused_at_operation_call_entry_before_any_fence() -> None:
    """At index allocation the refused record would read the usage inside the unwind; the subclass never gets that far."""
    setup = _setup()
    repo = setup.recorder.execution
    operation = repo.begin_operation(setup.recorder.source_node_id, "source_load", coordination_token=setup.recorder.coordination_token)
    stale = replace(setup.recorder.coordination_token, leader_epoch=setup.recorder.coordination_token.leader_epoch + 1)
    before = _coordination_state(setup)
    usage = _armed_usage()

    try:
        with pytest.raises(TypeError, match="token_usage must be TokenUsage"):
            repo.record_operation_call(
                operation.operation_id,
                CallType.LLM,
                CallStatus.SUCCESS,
                RawCallPayload(_REQUEST),
                RawCallPayload(_RESPONSE),
                latency_ms=12.5,
                token_usage=usage,
                coordination_token=stale,
            )
    finally:
        _ArmedUsage.armed = False

    assert _ArmedUsage.reads == []
    assert _coordination_state(setup) == before


def test_token_usage_subclass_is_refused_at_state_call_entry_before_any_fence() -> None:
    """The item-lease path refuses the subclass at entry too: no field read, no refusal event, no lease change."""
    setup = _setup()
    _terminate_item(setup)
    before = _coordination_state(setup)
    usage = _armed_usage()

    try:
        with pytest.raises(TypeError, match="token_usage must be TokenUsage"):
            _record_state_call(setup, token_usage=usage)
    finally:
        _ArmedUsage.armed = False

    assert _ArmedUsage.reads == []
    assert _coordination_state(setup) == before


class _LyingMroMeta(type):
    """A metaclass whose computed MRO omits the real base, so ``issubclass(type(v), float)`` is false."""

    def mro(cls) -> list[type]:
        return [cls, object]


def _unrecognised(kind: str) -> Any:
    """A value no type-only check recognises; deliberately outside every annotation it is passed to."""
    if kind == "decimal":
        return Decimal("12.5")

    class _LyingFloat(float, metaclass=_LyingMroMeta):
        pass

    return _LyingFloat(12.5)


_UNRECOGNISED = pytest.mark.parametrize("kind", ["decimal", "lying_float"])


def _assert_raised_at_entry(excinfo: pytest.ExceptionInfo[BaseException]) -> None:
    """The error was not raised while handling a refusal (it would then chain to it)."""
    raised_inside_a_refusal = excinfo.value.__context__ is not None
    assert not raised_inside_a_refusal


@_UNRECOGNISED
def test_unrecognised_latency_is_refused_at_state_call_entry_before_the_item_fence(kind: str) -> None:
    """A latency no type check recognises fails at entry: the item-lease refusal never starts, so none is replaced."""
    setup = _setup()
    _terminate_item(setup)
    before = _coordination_state(setup)

    with pytest.raises(TypeError, match="refused-call field holds") as excinfo:
        _record_state_call(setup, latency_ms=_unrecognised(kind))

    _assert_raised_at_entry(excinfo)
    assert _coordination_state(setup) == before


@_UNRECOGNISED
def test_unrecognised_latency_is_refused_at_state_call_entry_before_the_membership_fence(kind: str) -> None:
    """The membership fence writes its own refusal event when it runs; here it never runs."""
    setup = _setup()
    setup.recorder.factory.run_coordination.depart_worker(member_token=setup.member)
    before = _coordination_state(setup)

    with pytest.raises(TypeError, match="refused-call field holds") as excinfo:
        _record_state_call(setup, latency_ms=_unrecognised(kind))

    _assert_raised_at_entry(excinfo)
    assert _coordination_state(setup) == before


def test_unrecognised_attempt_is_refused_at_state_call_entry_before_the_membership_fence() -> None:
    """The work item's attempt is normalised at entry like the record's own fields."""
    setup = _setup()
    setup.recorder.factory.run_coordination.depart_worker(member_token=setup.member)
    unrecognised = replace(setup.item, attempt=_unrecognised("decimal"))
    before = _coordination_state(setup)

    with pytest.raises(TypeError, match="refused-call field holds") as excinfo:
        setup.recorder.execution.record_call(
            setup.state_id,
            0,
            CallType.LLM,
            CallStatus.SUCCESS,
            RawCallPayload(_REQUEST),
            RawCallPayload(_RESPONSE),
            latency_ms=12.5,
            token_usage=_USAGE,
            call_id="attempt-late-1",
            member_token=setup.member,
            work_item=unrecognised,
        )

    _assert_raised_at_entry(excinfo)
    assert _coordination_state(setup) == before


def test_status_that_is_not_the_enum_is_refused_at_state_call_entry_before_the_membership_fence() -> None:
    """``status.value`` would otherwise be read inside the unwind; a plain string fails at entry instead."""
    setup = _setup()
    setup.recorder.factory.run_coordination.depart_worker(member_token=setup.member)
    before = _coordination_state(setup)
    status: Any = "success"

    with pytest.raises(TypeError, match="refused-call status must be CallStatus") as excinfo:
        setup.recorder.execution.record_call(
            setup.state_id,
            0,
            CallType.LLM,
            status,
            RawCallPayload(_REQUEST),
            RawCallPayload(_RESPONSE),
            latency_ms=12.5,
            token_usage=_USAGE,
            call_id="attempt-late-1",
            member_token=setup.member,
            work_item=setup.item,
        )

    _assert_raised_at_entry(excinfo)
    assert _coordination_state(setup) == before


def _begin_operation(setup: _Setup) -> str:
    repo = setup.recorder.execution
    return repo.begin_operation(
        setup.recorder.source_node_id, "source_load", coordination_token=setup.recorder.coordination_token
    ).operation_id


@_UNRECOGNISED
@pytest.mark.parametrize("call_index", [0, None], ids=["leader_fence", "index_allocation"])
def test_unrecognised_latency_is_refused_at_operation_call_entry_before_any_leader_fence(kind: str, call_index: int | None) -> None:
    """Both leader fences (the call's own, and the index allocation's) write their own refusal event when they run."""
    setup = _setup()
    operation_id = _begin_operation(setup)
    token = setup.recorder.coordination_token
    stale = replace(token, leader_epoch=token.leader_epoch + 1)
    before = _coordination_state(setup)

    with pytest.raises(TypeError, match="refused-call field holds") as excinfo:
        setup.recorder.execution.record_operation_call(
            operation_id,
            CallType.HTTP,
            CallStatus.SUCCESS,
            RawCallPayload(_REQUEST),
            RawCallPayload(_RESPONSE),
            latency_ms=_unrecognised(kind),
            call_index=call_index,
            coordination_token=stale,
        )

    _assert_raised_at_entry(excinfo)
    assert _coordination_state(setup) == before


def test_unrecognised_latency_is_refused_before_an_operation_call_index_is_allocated() -> None:
    """With a live leader, the refusal at entry comes before the allocation, so no index is consumed."""
    setup = _setup()
    repo = setup.recorder.execution
    operation = repo.begin_operation(setup.recorder.source_node_id, "source_load", coordination_token=setup.recorder.coordination_token)

    with pytest.raises(TypeError, match="refused-call field holds"):
        repo.record_operation_call(
            operation.operation_id,
            CallType.HTTP,
            CallStatus.SUCCESS,
            RawCallPayload(_REQUEST),
            RawCallPayload(_RESPONSE),
            latency_ms=_unrecognised("decimal"),
            coordination_token=setup.recorder.coordination_token,
        )
    recorded = repo.record_operation_call(
        operation.operation_id,
        CallType.HTTP,
        CallStatus.SUCCESS,
        RawCallPayload(_REQUEST),
        RawCallPayload(_RESPONSE),
        latency_ms=12.5,
        coordination_token=setup.recorder.coordination_token,
    )

    assert recorded.call_index == 0
    assert _fence_refusals(setup) == []


@_UNRECOGNISED
def test_unrecognised_latency_is_refused_when_no_fence_refuses_either(kind: str) -> None:
    """The type a refused record cannot hold is refused on every path, not only under a refusal."""
    setup = _setup()
    before = _coordination_state(setup)

    with pytest.raises(TypeError, match="refused-call field holds"):
        _record_state_call(setup, latency_ms=_unrecognised(kind))

    assert _coordination_state(setup) == before
