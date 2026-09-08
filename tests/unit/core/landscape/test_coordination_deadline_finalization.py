"""Authority-ending and pure membership controls for deadline finalization."""

from __future__ import annotations

from datetime import UTC

import pytest
from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from elspeth.core.landscape import run_coordination_repository
from elspeth.core.landscape.database_clock import read_landscape_decision_time
from elspeth.core.landscape.lease_deadlines import DeadlineKey, DeadlineKind
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction, fenced_member_transaction
from elspeth.core.landscape.schema import run_coordination_events_table, run_coordination_table, run_workers_table, runs_table
from tests.fixtures.landscape import expire_leader_seat, make_recorder_with_run


def test_member_check_does_not_renew_either_liveness_deadline() -> None:
    setup = make_recorder_with_run()
    token = setup.coordination_token
    with setup.db.engine.connect() as conn:
        seat_before = conn.execute(select(run_coordination_table.c.leader_heartbeat_expires_at)).scalar_one()
        member_before = conn.execute(select(run_workers_table.c.heartbeat_expires_at)).scalar_one()
    with fenced_member_transaction(setup.db.engine, member_token=token.membership, verb="pure_member_probe") as conn:
        assert conn.execute(select(runs_table.c.run_id)).scalar_one() == setup.run_id
    with setup.db.engine.connect() as conn:
        assert conn.execute(select(run_coordination_table.c.leader_heartbeat_expires_at)).scalar_one() == seat_before
        assert conn.execute(select(run_workers_table.c.heartbeat_expires_at)).scalar_one() == member_before


def test_release_does_not_resurrect_seat_or_departed_member() -> None:
    setup = make_recorder_with_run()
    setup.factory.run_coordination.release_seat(token=setup.coordination_token)
    with setup.db.engine.connect() as conn:
        seat = conn.execute(select(run_coordination_table.c.leader_worker_id, run_coordination_table.c.leader_heartbeat_expires_at)).one()
        status = conn.execute(select(run_workers_table.c.status)).scalar_one()
    assert seat.leader_worker_id is None
    assert seat.leader_heartbeat_expires_at is None
    assert status == "departed"


def test_leader_body_renews_only_seat_not_worker_heartbeat() -> None:
    setup = make_recorder_with_run()
    with setup.db.engine.connect() as conn:
        member_before = conn.execute(select(run_workers_table.c.heartbeat_expires_at)).scalar_one()
        seat_before = conn.execute(select(run_coordination_table.c.leader_heartbeat_expires_at)).scalar_one()
    with fenced_leader_transaction(setup.db.engine, token=setup.coordination_token, window_seconds=160, verb="leader_only_probe"):
        pass
    with setup.db.engine.connect() as conn:
        assert conn.execute(select(run_workers_table.c.heartbeat_expires_at)).scalar_one() == member_before
        assert conn.execute(select(run_coordination_table.c.leader_heartbeat_expires_at)).scalar_one() > seat_before


def test_release_cancels_only_the_successfully_released_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    setup = make_recorder_with_run()
    predecessor = setup.coordination_token
    expire_leader_seat(setup.db, setup.run_id)
    successor = setup.factory.run_coordination.acquire_run_leadership(run_id=setup.run_id, worker_id="successor", window_seconds=80)
    forgotten: list[DeadlineKey] = []
    original = run_coordination_repository.forget_issued_deadline

    def record_forget(conn: Connection, *, key: DeadlineKey) -> None:
        forgotten.append(key)
        original(conn, key=key)

    monkeypatch.setattr(run_coordination_repository, "forget_issued_deadline", record_forget)
    with setup.db.engine.connect() as conn:
        successor_expiry = conn.execute(select(run_coordination_table.c.leader_heartbeat_expires_at)).scalar_one()
    setup.factory.run_coordination.release_seat(token=predecessor)
    assert forgotten == []
    with setup.db.engine.connect() as conn:
        seat = conn.execute(select(run_coordination_table)).one()
        assert seat.leader_worker_id == successor.worker_id
        assert seat.leader_epoch == successor.leader_epoch
        assert seat.leader_heartbeat_expires_at == successor_expiry

    setup.factory.run_coordination.release_seat(token=successor)
    assert forgotten == [DeadlineKey(DeadlineKind.LEADER, (successor.run_id, successor.worker_id, str(successor.leader_epoch)))]


def test_failed_body_rolls_back_entry_renewal_and_payload() -> None:
    setup = make_recorder_with_run()
    with setup.db.engine.connect() as conn:
        before = conn.execute(select(run_coordination_table.c.leader_heartbeat_expires_at)).scalar_one()
    with (
        pytest.raises(RuntimeError, match="body failed"),
        fenced_leader_transaction(setup.db.engine, token=setup.coordination_token, window_seconds=160, verb="failed_body_probe") as conn,
    ):
        conn.execute(update(runs_table).values(source_schema_json='{"probe":true}'))
        raise RuntimeError("body failed")
    with setup.db.engine.connect() as conn:
        assert conn.execute(select(run_coordination_table.c.leader_heartbeat_expires_at)).scalar_one() == before
        assert conn.execute(select(runs_table.c.source_schema_json)).scalar_one() is None


@pytest.mark.parametrize("role", ["leader", "follower"])
def test_transition_stamps_use_the_same_precision_as_registration(role: str) -> None:
    """Bracket this transition with actual DB samples; no clock-jump guarantee."""
    setup = make_recorder_with_run()
    repo = setup.factory.run_coordination
    if role == "leader":
        member = setup.coordination_token.membership
        event_type = "leader_release"
    else:
        run = setup.factory.run_lifecycle.get_run(setup.run_id)
        assert run is not None
        member = repo.admit_follower(run_id=setup.run_id, worker_id="follower", config_hash=run.config_hash, window_seconds=80)
        event_type = "worker_depart"
    with setup.db.engine.connect() as conn:
        registered = conn.execute(
            select(run_workers_table.c.registered_at).where(run_workers_table.c.worker_id == member.worker_id)
        ).scalar_one()
        before = read_landscape_decision_time(conn)
    if role == "leader":
        repo.release_seat(token=setup.coordination_token)
    else:
        repo.depart_worker(member_token=member)
    with setup.db.engine.connect() as conn:
        after = read_landscape_decision_time(conn)
        departed = conn.execute(
            select(run_workers_table.c.departed_at).where(run_workers_table.c.worker_id == member.worker_id)
        ).scalar_one()
        recorded = conn.execute(
            select(run_coordination_events_table.c.recorded_at).where(
                run_coordination_events_table.c.worker_id == member.worker_id,
                run_coordination_events_table.c.event_type == event_type,
            )
        ).scalar_one()
    assert departed >= registered
    assert before <= departed.replace(tzinfo=UTC) <= after
    assert recorded == departed
