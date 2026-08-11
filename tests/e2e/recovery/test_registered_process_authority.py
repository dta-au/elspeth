"""Registered OS-process authority proofs for scheduler and coordination state.

Every child opens its own :class:`LandscapeDB` handle through the Task 4
spawn harness.  Parent assertions use the complete durable-image helper so a
losing registered process cannot hide mutations outside the row under test.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import types
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import event, select

from elspeth.contracts import PipelineRow
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.errors import RunLeadershipLostError, RunWorkerEvictedError
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkStatus
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    run_coordination_events_table,
    run_coordination_table,
    run_workers_table,
    runs_table,
    scheduler_events_table,
    token_work_items_table,
)
from elspeth.engine.clock import MockClock
from elspeth.engine.orchestrator import Orchestrator, prepare_for_run
from tests.e2e.recovery.harness import (
    _DEFAULT_LEASE_SECONDS,
    _T0,
    SpawnedProcessAtSeam,
    _duplicate_terminal_outcome_tokens,
    _observed_contract,
    _run_to_interrupted_checkpoint,
    spawn_database_process_at_seam,
)
from tests.e2e.recovery.test_follower_join_and_drain import (
    _join_follower,
    _seat_run_with_live_leader,
    _seed_ready_row,
)
from tests.helpers.state_engine import capture_state_engine_image

_PROCESS_TIMEOUT_SECONDS = 20.0


def _release_and_assert_clean(*children: SpawnedProcessAtSeam) -> None:
    """Release every ready child and require a deterministic clean exit."""
    for child in children:
        child.release()
    for child in children:
        status = child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert status.exitcode == 0
        assert status.was_killed is False


def _pause_on_token_work_statement(
    db: LandscapeDB,
    before_update: Any,
    permit_update: Any,
    statement_prefix: str,
) -> Any:
    """Install a child-local hook after write intent, before a claim statement."""

    def pause(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = statement.lstrip().upper()
        if normalized.startswith(statement_prefix) and "TOKEN_WORK_ITEMS" in normalized:
            before_update.set()
            if not permit_update.wait(_PROCESS_TIMEOUT_SECONDS):
                raise TimeoutError("winner was not released from the contested token-work UPDATE")

    event.listen(db.engine, "before_cursor_execute", pause)
    return pause


def _claim_ready_as_registered_process(
    db: LandscapeDB,
    run_id: str,
    worker_id: str,
    now_iso: str,
    expected_token_id: str | None,
) -> None:
    """Child action: cross the registered READY-claim production repository."""
    claimed = RecorderFactory(db).scheduler.claim_ready(
        run_id=run_id,
        lease_owner=worker_id,
        lease_seconds=_DEFAULT_LEASE_SECONDS,
        now=datetime.fromisoformat(now_iso),
    )
    if expected_token_id is None:
        assert claimed is None
        return
    assert claimed is not None
    assert claimed.token_id == expected_token_id
    assert claimed.lease_owner == worker_id


def _contend_ready_claim_as_registered_process(
    db: LandscapeDB,
    run_id: str,
    worker_id: str,
    now_iso: str,
    expected_token_id: str | None,
    call_entered: Any,
    call_returned: Any,
    before_update: Any | None,
    permit_update: Any | None,
    pause_statement_prefix: str,
) -> None:
    """Enter a real claim while optional SQL hooks hold its writer lock."""
    listener = None
    if before_update is not None and permit_update is not None:
        listener = _pause_on_token_work_statement(db, before_update, permit_update, pause_statement_prefix)
    call_entered.set()
    try:
        _claim_ready_as_registered_process(db, run_id, worker_id, now_iso, expected_token_id)
        call_returned.set()
    finally:
        if listener is not None:
            event.remove(db.engine, "before_cursor_execute", listener)


def _park_pending_sink_as_registered_process(
    db: LandscapeDB,
    run_id: str,
    worker_id: str,
    now_iso: str,
    expected_token_id: str,
) -> None:
    """Child action: claim READY and park the exact result bundle."""
    factory = RecorderFactory(db)
    now = datetime.fromisoformat(now_iso)
    claimed = factory.scheduler.claim_ready(
        run_id=run_id,
        lease_owner=worker_id,
        lease_seconds=_DEFAULT_LEASE_SECONDS,
        now=now,
    )
    assert claimed is not None
    assert claimed.token_id == expected_token_id
    factory.scheduler.mark_pending_sink(
        work_item_id=claimed.work_item_id,
        row_payload_json=claimed.row_payload_json,
        sink_name="output",
        outcome="success",
        path="default_flow",
        error_hash=None,
        error_message=None,
        now=now,
        expected_lease_owner=worker_id,
        worker_id=worker_id,
    )


def _claim_pending_sink_as_registered_process(
    db: LandscapeDB,
    run_id: str,
    worker_id: str,
    now_iso: str,
    expected_token_id: str | None,
) -> None:
    """Child action: cross the registered complete-bundle claim boundary."""
    claimed = RecorderFactory(db).scheduler.claim_pending_sink(
        run_id=run_id,
        lease_owner=worker_id,
        lease_seconds=_DEFAULT_LEASE_SECONDS,
        now=datetime.fromisoformat(now_iso),
    )
    if expected_token_id is None:
        assert claimed is None
        return
    assert claimed is not None
    assert claimed.token_id == expected_token_id
    assert claimed.lease_owner == worker_id
    assert claimed.pending_sink_name == "output"
    assert claimed.pending_outcome == "success"
    assert claimed.pending_path == "default_flow"


def _contend_pending_sink_claim_as_registered_process(
    db: LandscapeDB,
    run_id: str,
    worker_id: str,
    now_iso: str,
    expected_token_id: str | None,
    call_entered: Any,
    call_returned: Any,
    before_update: Any | None,
    permit_update: Any | None,
    pause_statement_prefix: str,
) -> None:
    """Enter a real pending claim while optional SQL hooks hold its lock."""
    listener = None
    if before_update is not None and permit_update is not None:
        listener = _pause_on_token_work_statement(db, before_update, permit_update, pause_statement_prefix)
    call_entered.set()
    try:
        _claim_pending_sink_as_registered_process(db, run_id, worker_id, now_iso, expected_token_id)
        call_returned.set()
    finally:
        if listener is not None:
            event.remove(db.engine, "before_cursor_execute", listener)


def _recover_expired_as_registered_leader_process(
    db: LandscapeDB,
    run_id: str,
    leader_id: str,
    leader_epoch: int,
    now_iso: str,
    expected_count: int,
) -> None:
    """Child action: run the strict leader-fenced production recovery verb."""
    recovered = RecorderFactory(db).scheduler.recover_expired_leases(
        now=datetime.fromisoformat(now_iso),
        coordination_token=CoordinationToken(
            run_id=run_id,
            worker_id=leader_id,
            leader_epoch=leader_epoch,
        ),
    )
    assert recovered == expected_count


def _release_leader_process(
    db: LandscapeDB,
    run_id: str,
    worker_id: str,
    leader_epoch: int,
    now_iso: str,
) -> None:
    RunCoordinationRepository(db.engine).release_seat(
        token=CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=leader_epoch),
        now=datetime.fromisoformat(now_iso),
    )


def _acquire_leader_process(
    db: LandscapeDB,
    run_id: str,
    worker_id: str,
    now_iso: str,
    expected_epoch: int,
) -> None:
    token = RunCoordinationRepository(db.engine).acquire_run_leadership(
        run_id=run_id,
        worker_id=worker_id,
        now=datetime.fromisoformat(now_iso),
        window_seconds=10**9,
    )
    assert token == CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=expected_epoch)


def _refuse_live_seat_process(
    db: LandscapeDB,
    run_id: str,
    worker_id: str,
    now_iso: str,
) -> None:
    from elspeth.core.checkpoint.recovery import NonResumableRunError

    with pytest.raises(NonResumableRunError, match="leadership is held"):
        RunCoordinationRepository(db.engine).acquire_run_leadership(
            run_id=run_id,
            worker_id=worker_id,
            now=datetime.fromisoformat(now_iso),
            window_seconds=10**9,
        )


def _join_follower_process(
    db: LandscapeDB,
    run_id: str,
    now_iso: str,
    worker_id_path: str,
) -> None:
    with db.engine.connect() as conn:
        config_hash = str(conn.execute(select(runs_table.c.config_hash).where(runs_table.c.run_id == run_id)).scalar_one())
    with (
        patch("elspeth.engine.orchestrator.join_admission.resolve_config", return_value={}),
        patch("elspeth.engine.orchestrator.join_admission.stable_hash", return_value=config_hash),
    ):
        worker_id = Orchestrator(db).join_run(
            run_id=run_id,
            settings=types.SimpleNamespace(),  # type: ignore[arg-type]  # resolved settings are patched at this process boundary
            now=datetime.fromisoformat(now_iso),
            window_seconds=10**9,
        )
    Path(worker_id_path).write_text(worker_id, encoding="utf-8")


def _depart_follower_process(db: LandscapeDB, worker_id: str, now_iso: str) -> None:
    RunCoordinationRepository(db.engine).depart_worker(
        worker_id=worker_id,
        now=datetime.fromisoformat(now_iso),
    )


def _evict_follower_process(
    db: LandscapeDB,
    run_id: str,
    leader_id: str,
    leader_epoch: int,
    target_worker_id: str,
    now_iso: str,
) -> None:
    evicted = RunCoordinationRepository(db.engine).evict_worker(
        token=CoordinationToken(run_id=run_id, worker_id=leader_id, leader_epoch=leader_epoch),
        target_worker_id=target_worker_id,
        now=datetime.fromisoformat(now_iso),
        grace_seconds=0,
        window_seconds=10**9,
    )
    assert evicted is True


def _begin_run_with_registered_leader_process(
    db: LandscapeDB,
    run_id: str,
    leader_id: str,
) -> None:
    """RC-01: create the run and epoch-1 leader registry atomically."""
    prepare_for_run()
    run = RecorderFactory(db).run_lifecycle.begin_run(
        config={"process_authority": True},
        canonical_version="v1",
        run_id=run_id,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
        leader_worker_id=leader_id,
    )
    assert run.run_id == run_id


def _worker_heartbeat_process(
    db: LandscapeDB,
    worker_id: str,
    now_iso: str,
    expected_role: str,
    expected_leader_id: str,
) -> None:
    """RC-03: heartbeat an active registered worker through production."""
    snapshot = RunCoordinationRepository(db.engine).worker_heartbeat(
        worker_id=worker_id,
        now=datetime.fromisoformat(now_iso),
        window_seconds=10**9,
    )
    assert snapshot.worker_active is True
    assert snapshot.worker_role == expected_role
    assert snapshot.leader_worker_id == expected_leader_id
    assert snapshot.seat_live is True


def _lease_heartbeat_process(
    db: LandscapeDB,
    run_id: str,
    work_item_id: str,
    worker_id: str,
    now_iso: str,
) -> None:
    """AUX-01: renew a registered worker's lease beyond its original TTL."""
    now = datetime.fromisoformat(now_iso)
    expires_at = RecorderFactory(db).scheduler.heartbeat_lease(
        run_id=run_id,
        work_item_id=work_item_id,
        lease_owner=worker_id,
        lease_seconds=2 * _DEFAULT_LEASE_SECONDS,
        now=now,
        membership_fenced=True,
    )
    assert expires_at == now + timedelta(seconds=2 * _DEFAULT_LEASE_SECONDS)


def _refuse_inactive_claim_process(
    db: LandscapeDB,
    run_id: str,
    worker_id: str,
    now_iso: str,
) -> None:
    """Refuse a registered-but-inactive worker before claim mutation."""
    with pytest.raises(RunWorkerEvictedError):
        RecorderFactory(db).scheduler.claim_ready(
            run_id=run_id,
            lease_owner=worker_id,
            lease_seconds=_DEFAULT_LEASE_SECONDS,
            now=datetime.fromisoformat(now_iso),
        )


def _refuse_inactive_disposition_process(
    db: LandscapeDB,
    work_item_id: str,
    worker_id: str,
    now_iso: str,
) -> None:
    """Refuse a registered-but-inactive worker before disposition mutation."""
    with pytest.raises(RunWorkerEvictedError):
        RecorderFactory(db).scheduler.mark_terminal(
            work_item_id=work_item_id,
            now=datetime.fromisoformat(now_iso),
            expected_lease_owner=worker_id,
            worker_id=worker_id,
        )


def _refuse_inactive_enqueue_process(
    db: LandscapeDB,
    run_id: str,
    token_id: str,
    row_id: str,
    node_id: str,
    step_index: int,
    ingest_sequence: int,
    row_payload_json: str,
    worker_id: str,
    now_iso: str,
) -> None:
    """Refuse inactive or absent membership before enqueue insertion."""
    with pytest.raises(RunWorkerEvictedError):
        RecorderFactory(db).scheduler.enqueue_ready(
            run_id=run_id,
            token_id=token_id,
            row_id=row_id,
            node_id=node_id,
            step_index=step_index,
            ingest_sequence=ingest_sequence,
            row_payload_json=row_payload_json,
            available_at=datetime.fromisoformat(now_iso),
            worker_id=worker_id,
        )


def _refuse_recovery_process(
    db: LandscapeDB,
    run_id: str,
    worker_id: str,
    leader_epoch: int,
    now_iso: str,
) -> None:
    """Refuse stale or foreign strict leader authority before recovery."""
    with pytest.raises(RunLeadershipLostError):
        RecorderFactory(db).scheduler.recover_expired_leases(
            now=datetime.fromisoformat(now_iso),
            coordination_token=CoordinationToken(
                run_id=run_id,
                worker_id=worker_id,
                leader_epoch=leader_epoch,
            ),
        )


def _refuse_evict_process(
    db: LandscapeDB,
    run_id: str,
    leader_id: str,
    leader_epoch: int,
    target_worker_id: str,
    now_iso: str,
) -> None:
    """RC-07: reject ineligible target role/run under valid authority."""
    assert (
        RunCoordinationRepository(db.engine).evict_worker(
            token=CoordinationToken(run_id=run_id, worker_id=leader_id, leader_epoch=leader_epoch),
            target_worker_id=target_worker_id,
            now=datetime.fromisoformat(now_iso),
            grace_seconds=0,
            window_seconds=10**9,
        )
        is False
    )


@pytest.mark.timeout(120)
def test_registered_process_ready_claim_has_one_owner_and_zero_mutation_loser(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    clock = MockClock(start=_T0)
    crashed = _run_to_interrupted_checkpoint(tmp_path, clock)
    request.addfinalizer(crashed.db.close)
    clock.advance(_DEFAULT_LEASE_SECONDS + 60)
    leader_token = _seat_run_with_live_leader(
        crashed,
        leader_id=f"worker:{crashed.run_id}:leader-ready-authority",
    )
    winner_id = _join_follower(crashed, leader_token)
    loser_id = _join_follower(crashed, leader_token)
    token_id, work_item_id = _seed_ready_row(crashed, ingest_sequence=100)

    spawn_context = multiprocessing.get_context("spawn")
    winner_entered = spawn_context.Event()
    winner_returned = spawn_context.Event()
    winner_before_update = spawn_context.Event()
    permit_winner_update = spawn_context.Event()
    loser_entered = spawn_context.Event()
    loser_returned = spawn_context.Event()
    loser_before_update = spawn_context.Event()
    permit_loser_update = spawn_context.Event()

    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-ready-winner",
        action=_contend_ready_claim_as_registered_process,
        action_args=(
            crashed.run_id,
            winner_id,
            clock.now_utc().isoformat(),
            token_id,
            winner_entered,
            winner_returned,
            winner_before_update,
            permit_winner_update,
            "UPDATE",
        ),
    ) as winner:
        assert winner_before_update.wait(_PROCESS_TIMEOUT_SECONDS), "winner did not reach the contested UPDATE"
        with spawn_database_process_at_seam(
            database_url=crashed.db.connection_string,
            seam="registered-ready-loser",
            action=_contend_ready_claim_as_registered_process,
            action_args=(
                crashed.run_id,
                loser_id,
                clock.now_utc().isoformat(),
                None,
                loser_entered,
                loser_returned,
                loser_before_update,
                permit_loser_update,
                "SELECT",
            ),
        ) as loser:
            assert winner_entered.is_set()
            assert loser_entered.wait(_PROCESS_TIMEOUT_SECONDS), "loser did not enter its repository call"
            assert not loser_returned.wait(0.25), "loser returned while the winner held SQLite write intent"
            assert not loser_before_update.is_set(), "loser reached SELECT before the winner released SQLite write intent"
            permit_winner_update.set()
            winner_ready = winner.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
            assert winner_returned.is_set()
            assert winner_ready.pid != os.getpid()
            assert loser_before_update.wait(_PROCESS_TIMEOUT_SECONDS), "loser did not reach its serialized UPDATE"
            winner_image = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
            permit_loser_update.set()
            loser_ready = loser.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
            assert loser_ready.pid not in {os.getpid(), winner_ready.pid}
            assert loser_returned.is_set()
            loser_image = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
            _release_and_assert_clean(winner, loser)

    assert loser_image == winner_image, "the clean losing claimant must mutate no durable plane"
    with crashed.db.engine.connect() as conn:
        item = dict(
            conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id)).mappings().one()
        )
        claim_events = tuple(
            dict(row)
            for row in conn.execute(
                select(scheduler_events_table).where(
                    scheduler_events_table.c.token_id == token_id,
                    scheduler_events_table.c.event_type == SchedulerEventType.CLAIM_READY.value,
                    scheduler_events_table.c.caller_owner.in_((winner_id, loser_id)),
                )
            ).mappings()
        )

    assert item["status"] == TokenWorkStatus.LEASED.value
    assert item["lease_owner"] == winner_id
    assert item["attempt"] == 1
    assert len(claim_events) == 1
    assert claim_events[0]["caller_owner"] == winner_id
    assert claim_events[0]["to_lease_owner"] == winner_id
    assert _duplicate_terminal_outcome_tokens(crashed.db, crashed.run_id) == []


@pytest.mark.timeout(120)
def test_registered_process_pending_sink_claim_preserves_bundle_and_has_clean_loser(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    clock = MockClock(start=_T0)
    crashed = _run_to_interrupted_checkpoint(tmp_path, clock)
    request.addfinalizer(crashed.db.close)
    clock.advance(_DEFAULT_LEASE_SECONDS + 60)
    leader_id = f"worker:{crashed.run_id}:leader-pending-authority"
    leader_token = _seat_run_with_live_leader(crashed, leader_id=leader_id)
    producer_id = _join_follower(crashed, leader_token)
    loser_id = _join_follower(crashed, leader_token)
    token_id, work_item_id = _seed_ready_row(crashed, ingest_sequence=101)

    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-pending-producer",
        action=_park_pending_sink_as_registered_process,
        action_args=(crashed.run_id, producer_id, clock.now_utc().isoformat(), token_id),
    ) as producer:
        producer.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(producer)

    with crashed.db.engine.connect() as conn:
        parked = dict(
            conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id)).mappings().one()
        )
    bundle_columns = (
        "row_payload_json",
        "pending_sink_name",
        "pending_outcome",
        "pending_path",
        "pending_error_hash",
        "pending_error_message",
    )
    parked_bundle = {column: parked[column] for column in bundle_columns}
    assert parked["status"] == TokenWorkStatus.PENDING_SINK.value

    spawn_context = multiprocessing.get_context("spawn")
    winner_entered = spawn_context.Event()
    winner_returned = spawn_context.Event()
    winner_before_update = spawn_context.Event()
    permit_winner_update = spawn_context.Event()
    loser_entered = spawn_context.Event()
    loser_returned = spawn_context.Event()
    loser_before_update = spawn_context.Event()
    permit_loser_update = spawn_context.Event()

    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-pending-winner",
        action=_contend_pending_sink_claim_as_registered_process,
        action_args=(
            crashed.run_id,
            leader_id,
            clock.now_utc().isoformat(),
            token_id,
            winner_entered,
            winner_returned,
            winner_before_update,
            permit_winner_update,
            "UPDATE",
        ),
    ) as winner:
        assert winner_before_update.wait(_PROCESS_TIMEOUT_SECONDS), "winner did not reach the contested UPDATE"
        with spawn_database_process_at_seam(
            database_url=crashed.db.connection_string,
            seam="registered-pending-loser",
            action=_contend_pending_sink_claim_as_registered_process,
            action_args=(
                crashed.run_id,
                loser_id,
                clock.now_utc().isoformat(),
                None,
                loser_entered,
                loser_returned,
                loser_before_update,
                permit_loser_update,
                "SELECT",
            ),
        ) as loser:
            assert winner_entered.is_set()
            assert loser_entered.wait(_PROCESS_TIMEOUT_SECONDS), "loser did not enter its repository call"
            assert not loser_returned.wait(0.25), "loser returned while the winner held SQLite write intent"
            assert not loser_before_update.is_set(), "loser reached SELECT before the winner released SQLite write intent"
            permit_winner_update.set()
            winner_ready = winner.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
            assert winner_returned.is_set()
            assert loser_before_update.wait(_PROCESS_TIMEOUT_SECONDS), "loser did not reach its serialized UPDATE"
            winner_image = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
            permit_loser_update.set()
            loser_ready = loser.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
            assert loser_ready.pid not in {os.getpid(), winner_ready.pid}
            assert loser_returned.is_set()
            loser_image = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
            _release_and_assert_clean(winner, loser)

    assert loser_image == winner_image, "the losing pending-sink claimant must mutate no durable plane"
    with crashed.db.engine.connect() as conn:
        claimed = dict(
            conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id)).mappings().one()
        )
        contender_events = tuple(
            dict(row)
            for row in conn.execute(
                select(scheduler_events_table).where(
                    scheduler_events_table.c.token_id == token_id,
                    scheduler_events_table.c.event_type == SchedulerEventType.CLAIM_PENDING_SINK.value,
                    scheduler_events_table.c.caller_owner.in_((leader_id, loser_id)),
                )
            ).mappings()
        )
    assert claimed["status"] == TokenWorkStatus.LEASED.value
    assert claimed["lease_owner"] == leader_id
    assert claimed["attempt"] == 1
    assert {column: claimed[column] for column in bundle_columns} == parked_bundle
    assert len(contender_events) == 1
    assert contender_events[0]["caller_owner"] == leader_id
    assert _duplicate_terminal_outcome_tokens(crashed.db, crashed.run_id) == []


def test_child_process_initial_run_registration_is_one_atomic_epoch_one_image(tmp_path: Path) -> None:
    """RC-01: a spawned initial leader creates exactly one registry image."""
    database_url = f"sqlite:///{tmp_path / 'initial-registration.db'}"
    db = LandscapeDB(database_url)
    run_id = "run:registered-process-initial"
    leader_id = f"worker:{run_id}:leader"
    try:
        with spawn_database_process_at_seam(
            database_url=database_url,
            seam="registered-initial-leader",
            action=_begin_run_with_registered_leader_process,
            action_args=(run_id, leader_id),
        ) as child:
            ready = child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
            assert ready.pid != os.getpid()
            _release_and_assert_clean(child)

        with db.engine.connect() as conn:
            run_rows = tuple(conn.execute(select(runs_table).where(runs_table.c.run_id == run_id)).mappings())
            seats = tuple(conn.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == run_id)).mappings())
            workers = tuple(conn.execute(select(run_workers_table).where(run_workers_table.c.run_id == run_id)).mappings())
            events = tuple(
                conn.execute(
                    select(run_coordination_events_table)
                    .where(run_coordination_events_table.c.run_id == run_id)
                    .order_by(run_coordination_events_table.c.seq)
                ).mappings()
            )
    finally:
        db.close()

    assert len(run_rows) == len(seats) == len(workers) == 1
    assert seats[0]["leader_worker_id"] == leader_id
    assert seats[0]["leader_epoch"] == 1
    assert workers[0]["worker_id"] == leader_id
    assert workers[0]["role"] == "leader"
    assert workers[0]["status"] == "active"
    assert [(row["event_type"], row["worker_id"], row["leader_epoch"]) for row in events] == [
        ("worker_register", leader_id, 1),
        ("leader_acquire", leader_id, 1),
    ]
    assert [json.loads(str(row["context_json"])) for row in events] == [
        {"entry_point": "run", "role": "leader"},
        {"entry_point": "run"},
    ]


@pytest.mark.timeout(120)
def test_child_process_worker_and_lease_heartbeats_have_exact_owned_deltas(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """RC-03/AUX-01 process heartbeats; AUX-02 is the long-plugin integration proof."""
    clock = MockClock(start=_T0)
    crashed = _run_to_interrupted_checkpoint(tmp_path, clock)
    request.addfinalizer(crashed.db.close)
    clock.advance(_DEFAULT_LEASE_SECONDS + 60)
    leader_id = f"worker:{crashed.run_id}:leader-heartbeat-process"
    leader_token = _seat_run_with_live_leader(crashed, leader_id=leader_id)
    follower_id = _join_follower(crashed, leader_token)

    leader_heartbeat_now = clock.now_utc() + timedelta(seconds=1)
    before_leader = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-leader-heartbeat",
        action=_worker_heartbeat_process,
        action_args=(leader_id, leader_heartbeat_now.isoformat(), "leader", leader_id),
    ) as leader_heartbeat:
        leader_heartbeat.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(leader_heartbeat)
    after_leader = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
    before_leader.diff(after_leader).assert_only(
        {
            "run_coordination": {"leader_heartbeat_expires_at", "updated_at"},
            "run_workers": {"heartbeat_expires_at"},
        }
    )

    follower_heartbeat_now = leader_heartbeat_now + timedelta(seconds=1)
    before_follower = after_leader
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-follower-heartbeat",
        action=_worker_heartbeat_process,
        action_args=(follower_id, follower_heartbeat_now.isoformat(), "follower", leader_id),
    ) as follower_heartbeat:
        follower_heartbeat.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(follower_heartbeat)
    after_follower = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
    before_follower.diff(after_follower).assert_only({"run_workers": {"heartbeat_expires_at"}})

    token_id, work_item_id = _seed_ready_row(crashed, ingest_sequence=104)
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-lease-heartbeat-claim",
        action=_claim_ready_as_registered_process,
        action_args=(crashed.run_id, follower_id, follower_heartbeat_now.isoformat(), token_id),
    ) as claimant:
        claimant.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(claimant)
    beyond_original_ttl = follower_heartbeat_now + timedelta(seconds=_DEFAULT_LEASE_SECONDS + 1)
    before_lease_heartbeat = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-lease-heartbeat-beyond-ttl",
        action=_lease_heartbeat_process,
        action_args=(crashed.run_id, work_item_id, follower_id, beyond_original_ttl.isoformat()),
    ) as lease_heartbeat:
        lease_heartbeat.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(lease_heartbeat)
    after_lease_heartbeat = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
    before_lease_heartbeat.diff(after_lease_heartbeat).assert_only({"token_work_items": {"lease_expires_at", "updated_at"}})
    assert before_lease_heartbeat.tables["scheduler_events"] == after_lease_heartbeat.tables["scheduler_events"]

    contender_now = beyond_original_ttl + timedelta(seconds=1)
    before_contender = after_lease_heartbeat
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-recovery-before-extended-expiry",
        action=_recover_expired_as_registered_leader_process,
        action_args=(
            crashed.run_id,
            leader_id,
            leader_token.leader_epoch,
            contender_now.isoformat(),
            0,
        ),
    ) as recovery_contender:
        recovery_contender.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(recovery_contender)
    after_contender = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
    before_contender.diff(after_contender).assert_only({"run_coordination": {"leader_heartbeat_expires_at", "updated_at"}})
    assert before_contender.tables["token_work_items"] == after_contender.tables["token_work_items"]
    assert before_contender.tables["scheduler_events"] == after_contender.tables["scheduler_events"]


@pytest.mark.timeout(120)
def test_inactive_registered_process_is_refused_for_claim_and_disposition_without_mutation(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    clock = MockClock(start=_T0)
    crashed = _run_to_interrupted_checkpoint(tmp_path, clock)
    request.addfinalizer(crashed.db.close)
    clock.advance(_DEFAULT_LEASE_SECONDS + 60)
    leader_token = _seat_run_with_live_leader(crashed, leader_id=f"worker:{crashed.run_id}:leader-inactive-fence")
    claim_worker = _join_follower(crashed, leader_token)
    disposition_worker = _join_follower(crashed, leader_token)
    disposition_token_id, disposition_work_item_id = _seed_ready_row(crashed, ingest_sequence=105)
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="active-disposition-owner-claim",
        action=_claim_ready_as_registered_process,
        action_args=(crashed.run_id, disposition_worker, clock.now_utc().isoformat(), disposition_token_id),
    ) as claimant:
        claimant.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(claimant)
    _seed_ready_row(crashed, ingest_sequence=106)
    with crashed.db.engine.begin() as conn:
        conn.execute(
            run_workers_table.update()
            .where(run_workers_table.c.worker_id.in_((claim_worker, disposition_worker)))
            .values(status="departed", departed_at=clock.now_utc())
        )

    before_claim = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="inactive-claim-refusal",
        action=_refuse_inactive_claim_process,
        action_args=(crashed.run_id, claim_worker, clock.now_utc().isoformat()),
    ) as refused_claim:
        refused_claim.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(refused_claim)
    assert capture_state_engine_image(crashed.factory, run_id=crashed.run_id) == before_claim

    before_disposition = before_claim
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="inactive-disposition-refusal",
        action=_refuse_inactive_disposition_process,
        action_args=(disposition_work_item_id, disposition_worker, clock.now_utc().isoformat()),
    ) as refused_disposition:
        refused_disposition.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(refused_disposition)
    assert capture_state_engine_image(crashed.factory, run_id=crashed.run_id) == before_disposition

    evicted_worker = _join_follower(crashed, leader_token)
    with crashed.db.engine.begin() as conn:
        conn.execute(
            run_workers_table.update()
            .where(run_workers_table.c.worker_id == evicted_worker)
            .values(status="evicted", evicted_at=clock.now_utc())
        )
    for sequence, worker_id, seam in (
        (107, evicted_worker, "evicted-enqueue-refusal"),
        (108, f"worker:{crashed.run_id}:absent", "absent-enqueue-refusal"),
    ):
        data = {"id": sequence, "value": sequence * 10}
        row = crashed.factory.data_flow.create_row(
            run_id=crashed.run_id,
            source_node_id=crashed.source_node_id,
            row_index=sequence,
            data=data,
            source_row_index=sequence,
            ingest_sequence=sequence,
        )
        token = crashed.factory.data_flow.create_token(row_id=row.row_id)
        payload = TokenSchedulerRepository.serialize_row_payload(PipelineRow(data, _observed_contract(data)))
        before_enqueue = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
        with spawn_database_process_at_seam(
            database_url=crashed.db.connection_string,
            seam=seam,
            action=_refuse_inactive_enqueue_process,
            action_args=(
                crashed.run_id,
                token.token_id,
                row.row_id,
                crashed.journal_node_id,
                crashed.journal_step_index,
                sequence,
                payload,
                worker_id,
                clock.now_utc().isoformat(),
            ),
        ) as refused_enqueue:
            refused_enqueue.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
            _release_and_assert_clean(refused_enqueue)
        assert capture_state_engine_image(crashed.factory, run_id=crashed.run_id) == before_enqueue


@pytest.mark.timeout(120)
def test_stale_and_foreign_recovery_authority_changes_only_refusal_evidence(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    clock = MockClock(start=_T0)
    crashed = _run_to_interrupted_checkpoint(tmp_path, clock)
    request.addfinalizer(crashed.db.close)
    clock.advance(_DEFAULT_LEASE_SECONDS + 60)
    leader_id = f"worker:{crashed.run_id}:leader-recovery-fence"
    leader_token = _seat_run_with_live_leader(crashed, leader_id=leader_id)
    owner_id = _join_follower(crashed, leader_token)
    token_id, work_item_id = _seed_ready_row(crashed, ingest_sequence=107)
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="recovery-fence-owner-claim",
        action=_claim_ready_as_registered_process,
        action_args=(crashed.run_id, owner_id, clock.now_utc().isoformat(), token_id),
    ) as claimant:
        claimant.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(claimant)
    with crashed.db.engine.connect() as conn:
        lease_expires_at = conn.execute(
            select(token_work_items_table.c.lease_expires_at).where(token_work_items_table.c.work_item_id == work_item_id)
        ).scalar_one()
    recovery_now = lease_expires_at + timedelta(seconds=1)
    with crashed.db.engine.begin() as conn:
        conn.execute(
            run_workers_table.update().where(run_workers_table.c.worker_id == owner_id).values(status="departed", departed_at=recovery_now)
        )

    before = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
    stale_epoch = leader_token.leader_epoch - 1
    for seam, worker_id, epoch in (
        ("stale-recovery-refusal", leader_id, stale_epoch),
        ("foreign-recovery-refusal", f"worker:{crashed.run_id}:foreign", leader_token.leader_epoch),
    ):
        with spawn_database_process_at_seam(
            database_url=crashed.db.connection_string,
            seam=seam,
            action=_refuse_recovery_process,
            action_args=(crashed.run_id, worker_id, epoch, recovery_now.isoformat()),
        ) as refusal:
            refusal.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
            _release_and_assert_clean(refusal)
    after = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
    before.diff(after).assert_only({"run_coordination_events": set(run_coordination_events_table.c.keys())})
    assert before.tables["token_work_items"] == after.tables["token_work_items"]
    refusal_events = [
        row
        for row in after.tables["run_coordination_events"]
        if row["event_type"] == "fence_refusal" and json.loads(str(row["context_json"])) == {"verb": "recover_expired_leases"}
    ]
    assert [(row["worker_id"], row["leader_epoch"]) for row in refusal_events[-2:]] == [
        (leader_id, stale_epoch),
        (f"worker:{crashed.run_id}:foreign", leader_token.leader_epoch),
    ]


@pytest.mark.timeout(120)
def test_registered_leader_process_recovers_transform_and_sink_redrive_exactly(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    clock = MockClock(start=_T0)
    crashed = _run_to_interrupted_checkpoint(tmp_path, clock)
    request.addfinalizer(crashed.db.close)
    clock.advance(_DEFAULT_LEASE_SECONDS + 60)
    leader_id = f"worker:{crashed.run_id}:leader-recovery-authority"
    leader_token = _seat_run_with_live_leader(crashed, leader_id=leader_id)
    transform_owner = _join_follower(crashed, leader_token)
    sink_owner = _join_follower(crashed, leader_token)

    transform_token_id, transform_work_item_id = _seed_ready_row(crashed, ingest_sequence=102)
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-transform-claim",
        action=_claim_ready_as_registered_process,
        action_args=(crashed.run_id, transform_owner, clock.now_utc().isoformat(), transform_token_id),
    ) as transform_claimant:
        transform_claimant.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(transform_claimant)

    sink_token_id, sink_work_item_id = _seed_ready_row(crashed, ingest_sequence=103)
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-sink-producer",
        action=_park_pending_sink_as_registered_process,
        action_args=(crashed.run_id, sink_owner, clock.now_utc().isoformat(), sink_token_id),
    ) as sink_producer:
        sink_producer.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(sink_producer)
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-sink-redrive-claim",
        action=_claim_pending_sink_as_registered_process,
        action_args=(crashed.run_id, sink_owner, clock.now_utc().isoformat(), sink_token_id),
    ) as sink_claimant:
        sink_claimant.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(sink_claimant)

    with crashed.db.engine.connect() as conn:
        leased_rows = {
            str(row["token_id"]): dict(row)
            for row in conn.execute(
                select(token_work_items_table).where(token_work_items_table.c.token_id.in_((transform_token_id, sink_token_id)))
            ).mappings()
        }
    sink_bundle_columns = (
        "row_payload_json",
        "pending_sink_name",
        "pending_outcome",
        "pending_path",
        "pending_error_hash",
        "pending_error_message",
    )
    sink_bundle = {column: leased_rows[sink_token_id][column] for column in sink_bundle_columns}

    recovery_now = max(
        leased_rows[transform_token_id]["lease_expires_at"],
        leased_rows[sink_token_id]["lease_expires_at"],
    ) + timedelta(seconds=1)
    with crashed.db.engine.begin() as conn:
        conn.execute(
            run_workers_table.update()
            .where(run_workers_table.c.worker_id.in_((transform_owner, sink_owner)))
            .values(status="departed", departed_at=recovery_now)
        )

    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-leader-recovery",
        action=_recover_expired_as_registered_leader_process,
        action_args=(
            crashed.run_id,
            leader_id,
            leader_token.leader_epoch,
            recovery_now.isoformat(),
            2,
        ),
    ) as recovery:
        recovery_ready = recovery.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert recovery_ready.pid != os.getpid()
        _release_and_assert_clean(recovery)

    with crashed.db.engine.connect() as conn:
        recovered_rows = {
            str(row["token_id"]): dict(row)
            for row in conn.execute(
                select(token_work_items_table).where(token_work_items_table.c.token_id.in_((transform_token_id, sink_token_id)))
            ).mappings()
        }
        recovery_events = tuple(
            dict(row)
            for row in conn.execute(
                select(scheduler_events_table).where(
                    scheduler_events_table.c.token_id.in_((transform_token_id, sink_token_id)),
                    scheduler_events_table.c.event_type == SchedulerEventType.RECOVER_EXPIRED_LEASE.value,
                )
            ).mappings()
        )

    transform_row = recovered_rows[transform_token_id]
    assert transform_row["status"] == TokenWorkStatus.READY.value
    assert transform_row["attempt"] == 2
    assert transform_row["lease_owner"] is None
    assert transform_row["work_item_id"] != transform_work_item_id

    sink_row = recovered_rows[sink_token_id]
    assert sink_row["status"] == TokenWorkStatus.PENDING_SINK.value
    assert sink_row["attempt"] == 1
    assert sink_row["lease_owner"] is None
    assert sink_row["work_item_id"] == sink_work_item_id
    assert {column: sink_row[column] for column in sink_bundle_columns} == sink_bundle
    assert len(recovery_events) == 2
    assert {event["from_lease_owner"] for event in recovery_events} == {transform_owner, sink_owner}
    events_by_token = {str(event["token_id"]): event for event in recovery_events}
    assert events_by_token[transform_token_id]["from_attempt"] == 1
    assert events_by_token[transform_token_id]["to_attempt"] == 2
    assert events_by_token[sink_token_id]["from_attempt"] == 1
    assert events_by_token[sink_token_id]["to_attempt"] == 1

    replacement_id = _join_follower(crashed, leader_token)
    losing_replacement_id = _join_follower(crashed, leader_token)
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-recovered-sink-winner",
        action=_claim_pending_sink_as_registered_process,
        action_args=(crashed.run_id, replacement_id, recovery_now.isoformat(), sink_token_id),
    ) as winner:
        winner.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        winner_image = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
        with spawn_database_process_at_seam(
            database_url=crashed.db.connection_string,
            seam="registered-recovered-sink-loser",
            action=_claim_pending_sink_as_registered_process,
            action_args=(crashed.run_id, losing_replacement_id, recovery_now.isoformat(), None),
        ) as loser:
            loser.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
            loser_image = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
            _release_and_assert_clean(winner, loser)
    assert loser_image == winner_image
    with crashed.db.engine.connect() as conn:
        reclaimed_sink = dict(
            conn.execute(select(token_work_items_table).where(token_work_items_table.c.token_id == sink_token_id)).mappings().one()
        )
        replacement_claim_events = tuple(
            dict(row)
            for row in conn.execute(
                select(scheduler_events_table).where(
                    scheduler_events_table.c.token_id == sink_token_id,
                    scheduler_events_table.c.event_type == SchedulerEventType.CLAIM_PENDING_SINK.value,
                    scheduler_events_table.c.caller_owner.in_((replacement_id, losing_replacement_id)),
                )
            ).mappings()
        )
    assert reclaimed_sink["status"] == TokenWorkStatus.LEASED.value
    assert reclaimed_sink["lease_owner"] == replacement_id
    assert reclaimed_sink["attempt"] == 1
    assert reclaimed_sink["work_item_id"] == sink_work_item_id
    assert {column: reclaimed_sink[column] for column in sink_bundle_columns} == sink_bundle
    assert len(replacement_claim_events) == 1
    assert replacement_claim_events[0]["caller_owner"] == replacement_id
    assert _duplicate_terminal_outcome_tokens(crashed.db, crashed.run_id) == []


@pytest.mark.timeout(120)
def test_registered_process_coordination_release_takeover_join_depart_and_evict(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    clock = MockClock(start=_T0)
    crashed = _run_to_interrupted_checkpoint(tmp_path, clock)
    request.addfinalizer(crashed.db.close)
    clock.advance(_DEFAULT_LEASE_SECONDS + 60)
    first_leader_id = f"worker:{crashed.run_id}:leader-process-one"
    first_token = _seat_run_with_live_leader(crashed, leader_id=first_leader_id)
    now_iso = clock.now_utc().isoformat()

    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-leader-release",
        action=_release_leader_process,
        action_args=(crashed.run_id, first_leader_id, first_token.leader_epoch, now_iso),
    ) as release:
        release.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(release)

    second_leader_id = f"worker:{crashed.run_id}:leader-process-two"
    second_epoch = first_token.leader_epoch + 1
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-leader-takeover",
        action=_acquire_leader_process,
        action_args=(crashed.run_id, second_leader_id, now_iso, second_epoch),
    ) as takeover:
        takeover_ready = takeover.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert takeover_ready.pid != os.getpid()
        _release_and_assert_clean(takeover)

    winner_image = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-live-seat-loser",
        action=_refuse_live_seat_process,
        action_args=(crashed.run_id, "worker:losing-leader", now_iso),
    ) as loser:
        loser.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(loser)
    assert capture_state_engine_image(crashed.factory, run_id=crashed.run_id) == winner_image

    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-stale-release",
        action=_release_leader_process,
        action_args=(crashed.run_id, first_leader_id, first_token.leader_epoch, now_iso),
    ) as stale_release:
        stale_release.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(stale_release)
    assert capture_state_engine_image(crashed.factory, run_id=crashed.run_id) == winner_image

    departing_path = tmp_path / "departing-worker-id"
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-follower-admit-depart",
        action=_join_follower_process,
        action_args=(crashed.run_id, now_iso, str(departing_path)),
    ) as admit_depart:
        admit_depart.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        departing_id = departing_path.read_text(encoding="utf-8")
        _release_and_assert_clean(admit_depart)
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-follower-depart",
        action=_depart_follower_process,
        action_args=(departing_id, now_iso),
    ) as depart:
        depart.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(depart)

    evicted_path = tmp_path / "evicted-worker-id"
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-follower-admit-evict",
        action=_join_follower_process,
        action_args=(crashed.run_id, now_iso, str(evicted_path)),
    ) as admit_evict:
        admit_evict.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        evicted_id = evicted_path.read_text(encoding="utf-8")
        _release_and_assert_clean(admit_evict)
    eviction_now = clock.now_utc() + timedelta(seconds=1)
    with crashed.db.engine.begin() as conn:
        conn.execute(
            run_workers_table.update()
            .where(run_workers_table.c.worker_id == evicted_id)
            .values(heartbeat_expires_at=clock.now_utc() - timedelta(seconds=1))
        )
    with spawn_database_process_at_seam(
        database_url=crashed.db.connection_string,
        seam="registered-follower-evict",
        action=_evict_follower_process,
        action_args=(
            crashed.run_id,
            second_leader_id,
            second_epoch,
            evicted_id,
            eviction_now.isoformat(),
        ),
    ) as evict:
        evict.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        _release_and_assert_clean(evict)

    foreign_run_id = f"{crashed.run_id}:foreign"
    foreign_leader_id = f"worker:{foreign_run_id}:leader"
    crashed.factory.run_lifecycle.begin_run(
        config={"foreign": True},
        canonical_version="v1",
        run_id=foreign_run_id,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
        leader_worker_id=foreign_leader_id,
    )
    same_run_non_current_leader_id = f"worker:{crashed.run_id}:non-current-leader-role"
    foreign_follower_id = f"worker:{foreign_run_id}:follower"
    with crashed.db.engine.begin() as conn:
        conn.execute(
            run_workers_table.insert(),
            (
                {
                    "worker_id": same_run_non_current_leader_id,
                    "run_id": crashed.run_id,
                    "role": "leader",
                    "status": "active",
                    "registered_at": eviction_now,
                    "heartbeat_expires_at": eviction_now - timedelta(seconds=1),
                    "entry_point": "rc07-role-fixture",
                },
                {
                    "worker_id": foreign_follower_id,
                    "run_id": foreign_run_id,
                    "role": "follower",
                    "status": "active",
                    "registered_at": eviction_now,
                    "heartbeat_expires_at": eviction_now - timedelta(seconds=1),
                    "entry_point": "rc07-run-fixture",
                },
            ),
        )
    before_ineligible_evictions = capture_state_engine_image(crashed.factory, run_id=crashed.run_id)
    before_foreign_run = capture_state_engine_image(crashed.factory, run_id=foreign_run_id)
    for seam, target_worker_id in (
        ("leader-role-eviction-refusal", same_run_non_current_leader_id),
        ("foreign-run-eviction-refusal", foreign_follower_id),
    ):
        with spawn_database_process_at_seam(
            database_url=crashed.db.connection_string,
            seam=seam,
            action=_refuse_evict_process,
            action_args=(
                crashed.run_id,
                second_leader_id,
                second_epoch,
                target_worker_id,
                eviction_now.isoformat(),
            ),
        ) as refusal:
            refusal.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
            _release_and_assert_clean(refusal)
    assert capture_state_engine_image(crashed.factory, run_id=crashed.run_id) == before_ineligible_evictions
    assert capture_state_engine_image(crashed.factory, run_id=foreign_run_id) == before_foreign_run

    with crashed.db.engine.connect() as conn:
        seat = dict(conn.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == crashed.run_id)).mappings().one())
        workers = {
            str(row["worker_id"]): dict(row)
            for row in conn.execute(
                select(run_workers_table).where(
                    run_workers_table.c.worker_id.in_((first_leader_id, second_leader_id, departing_id, evicted_id))
                )
            ).mappings()
        }
        events = tuple(
            dict(row)
            for row in conn.execute(
                select(run_coordination_events_table)
                .where(run_coordination_events_table.c.run_id == crashed.run_id)
                .where(run_coordination_events_table.c.worker_id.in_((first_leader_id, second_leader_id, departing_id, evicted_id)))
                .order_by(run_coordination_events_table.c.seq)
            ).mappings()
        )

    assert seat["leader_worker_id"] == second_leader_id
    assert seat["leader_epoch"] == second_epoch
    assert workers[first_leader_id]["status"] == "departed"
    assert workers[second_leader_id]["status"] == "active"
    assert workers[departing_id]["status"] == "departed"
    assert workers[evicted_id]["status"] == "evicted"
    event_types_by_worker = {
        worker_id: [event["event_type"] for event in events if event["worker_id"] == worker_id]
        for worker_id in (first_leader_id, second_leader_id, departing_id, evicted_id)
    }
    assert event_types_by_worker[first_leader_id] == ["worker_register", "leader_acquire", "leader_release"]
    assert event_types_by_worker[second_leader_id] == ["worker_register", "leader_acquire"]
    assert event_types_by_worker[departing_id] == ["worker_register", "worker_depart"]
    assert event_types_by_worker[evicted_id] == ["worker_register", "worker_evict"]
