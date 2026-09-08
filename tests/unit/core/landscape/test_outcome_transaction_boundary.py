"""Public outcome writers own transaction errors and prepare context before locks."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from elspeth.contracts.audit import TokenRef
from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, CoordinationToken
from elspeth.contracts.enums import TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError, RunLeadershipLostError, RunMembershipLostError
from elspeth.core.landscape import run_coordination_repository
from elspeth.core.landscape.database import Tier1Engine
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from tests.fixtures.landscape import make_recorder_with_run


@dataclass
class OutcomeWriter:
    factory: RecorderFactory
    coordination_token: CoordinationToken
    ref: TokenRef
    record: Callable[..., str]
    engine: Tier1Engine
    authority: str


@pytest.fixture(params=("item", "leader"))
def writer(request: pytest.FixtureRequest) -> OutcomeWriter:
    setup = make_recorder_with_run()
    member = setup.coordination_token.membership
    row, token = setup.data_flow.create_row_with_token(
        setup.source_node_id,
        0,
        {"value": 1},
        coordination_token=setup.coordination_token,
        source_row_index=0,
        ingest_sequence=0,
    )
    ref = TokenRef(run_id=setup.run_id, token_id=token.token_id)
    if request.param == "item":
        claim = setup.factory.scheduler.enqueue_ready_claimed(
            member_token=member,
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=None,
            step_index=0,
            ingest_sequence=0,
            row_payload_json='{"value":1}',
            lease_owner=member.worker_id,
            lease_seconds=300,
        )
        record = partial(setup.data_flow.record_token_outcome, member_token=member, work_item=claim)
    else:
        record = partial(setup.data_flow.record_token_outcome_leader, coordination_token=setup.coordination_token)
    return OutcomeWriter(
        setup.factory,
        setup.coordination_token,
        ref,
        partial(record, ref, TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW, sink_name="output"),
        setup.db.engine,
        request.param,
    )


def test_invalid_context_opens_no_owned_transaction(writer: OutcomeWriter, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[bool] = []
    original_begin = run_coordination_repository.begin_write

    def track_begin(engine: Tier1Engine):
        opened.append(True)
        return original_begin(engine)

    monkeypatch.setattr(run_coordination_repository, "begin_write", track_begin)
    with pytest.raises(ValueError, match="Cannot canonicalize non-finite float"):
        writer.record(context={"invalid": float("nan")})
    assert opened == []
    assert writer.factory.data_flow.get_token_outcome(writer.ref.token_id) is None


def test_context_is_prepared_before_transaction_and_not_read_again(writer: OutcomeWriter, monkeypatch: pytest.MonkeyPatch) -> None:
    context = {"value": 1}
    original_begin = run_coordination_repository.begin_write

    @contextmanager
    def mutate_context_at_begin(engine: Tier1Engine) -> Iterator[Connection]:
        context["value"] = 2
        with original_begin(engine) as conn:
            yield conn

    monkeypatch.setattr(run_coordination_repository, "begin_write", mutate_context_at_begin)
    writer.record(context=context)
    outcome = writer.factory.data_flow.get_token_outcome(writer.ref.token_id)
    assert outcome is not None
    assert outcome.context_json == '{"value":1}'


@pytest.mark.parametrize("boundary", ("begin", "commit"))
def test_owned_boundary_failure_is_translated_and_rolls_back(
    writer: OutcomeWriter,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    original_begin = run_coordination_repository.begin_write
    failure = OperationalError(boundary.upper(), {}, RuntimeError("injected transaction failure"))

    @contextmanager
    def fail_boundary(engine: Tier1Engine) -> Iterator[Connection]:
        if boundary == "begin":
            raise failure
        with original_begin(engine) as conn:
            yield conn
            raise failure

    monkeypatch.setattr(run_coordination_repository, "begin_write", fail_boundary)
    with pytest.raises(LandscapeRecordError, match=r"transaction boundary.*OperationalError") as caught:
        writer.record()
    assert caught.value.__cause__ is failure
    assert writer.factory.data_flow.get_token_outcome(writer.ref.token_id) is None


@pytest.mark.parametrize("failure", (AuditIntegrityError("authority refused"), RuntimeError("programmer bug")))
def test_non_database_refusals_are_not_translated(
    writer: OutcomeWriter,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    @contextmanager
    def refuse_boundary(engine: Tier1Engine) -> Iterator[Connection]:
        raise failure
        yield  # pragma: no cover - contextmanager shape only

    monkeypatch.setattr(run_coordination_repository, "begin_write", refuse_boundary)
    with pytest.raises(type(failure)) as caught:
        writer.record()
    assert caught.value is failure
    assert writer.factory.data_flow.get_token_outcome(writer.ref.token_id) is None


def test_real_authority_refusal_retains_its_domain_error(writer: OutcomeWriter) -> None:
    if writer.authority == "item":
        writer.factory.run_coordination.depart_worker(member_token=writer.coordination_token.membership)
        with pytest.raises(RunMembershipLostError):
            writer.record()
    else:
        stale_authority = replace(writer.coordination_token, leader_epoch=writer.coordination_token.leader_epoch + 1)
        with pytest.raises(RunLeadershipLostError):
            writer.record(coordination_token=stale_authority)
    assert writer.factory.data_flow.get_token_outcome(writer.ref.token_id) is None


def test_caller_owned_commit_failure_stays_raw_and_rolls_back(writer: OutcomeWriter) -> None:
    failure = OperationalError("COMMIT", {}, RuntimeError("caller-owned commit failure"))
    with (
        pytest.raises(OperationalError) as caught,
        fenced_leader_transaction(
            writer.engine,
            token=writer.coordination_token,
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
            verb="test_caller_outcome_boundary",
        ) as conn,
    ):
        writer.factory.data_flow.outcomes.record_token_outcome_on(
            writer.ref,
            TerminalOutcome.SUCCESS,
            TerminalPath.DEFAULT_FLOW,
            conn=conn,
            sink_name="output",
        )
        raise failure
    assert caught.value is failure
    assert writer.factory.data_flow.get_token_outcome(writer.ref.token_id) is None
