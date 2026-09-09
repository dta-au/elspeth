# tests/unit/engine/orchestrator/test_abandon_leaderless_run.py
"""Operator finalization of a leaderless RUNNING run (elspeth-5dd23f4df9).

A leader that dies mid-run leaves the run RUNNING with an expired seat. Its
sources are still ``loading`` (the ingest loop records EXHAUSTED only after
the last row's traversal returns), so ``elspeth resume`` refuses with
``IncompleteSourceResumeError`` — correctly — and before this module no verb
could finalize the run. ``abandon_leaderless_run`` takes the dead seat through
the same takeover CAS resume uses, finalizes INTERRUPTED under that token (so
the ADR-038 sweep records ``(NULL, ABANDONED)`` for every undecided token
exactly when resume would refuse), departs the followers, and vacates the
seat.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from elspeth.contracts import RunStatus
from elspeth.contracts.checkpoint import CheckpointDraft
from elspeth.contracts.enums import TerminalPath
from elspeth.contracts.errors import AbandonRefusedError
from elspeth.core.checkpoint import CheckpointManager
from elspeth.core.checkpoint.recovery import NonResumableRunError
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.schema import (
    RunSourceLifecycleState,
    run_coordination_events_table,
    run_workers_table,
    runs_table,
    token_outcomes_table,
)
from elspeth.engine.orchestrator.abandon import abandon_leaderless_run, inspect_leaderless_run
from tests.fixtures.landscape import (
    RecorderSetup,
    expire_leader_seat,
    leader_coordination_token,
    make_recorder_with_run,
    register_test_worker,
)

_TOPOLOGY_HASH = "a" * 64
_LEADER_WORKER_ID = "worker:run-leaderless:dead-leader"
_FOLLOWER_WORKER_ID = "worker:run-leaderless:follower"


def _leaderless_run(
    *,
    lifecycle_state: RunSourceLifecycleState = RunSourceLifecycleState.LOADING,
    with_checkpoint: bool = True,
    token_count: int = 3,
    expire_seat: bool = True,
) -> tuple[RecorderSetup, list[str]]:
    """RUNNING run + ``loading`` source + undecided tokens + active follower, leader seat lapsed."""
    setup = make_recorder_with_run(run_id="run-leaderless", leader_worker_id=_LEADER_WORKER_ID)
    setup.factory.run_lifecycle.record_run_source(
        source_node_id=setup.source_node_id,
        source_name="primary",
        plugin_name="source",
        config_hash="c" * 64,
        lifecycle_state=lifecycle_state,
        coordination_token=leader_coordination_token(setup.factory, setup.run_id),
    )
    if with_checkpoint:
        CheckpointManager(setup.db).create_checkpoint(
            draft=CheckpointDraft(run_id=setup.run_id, sequence_number=0, upstream_topology_hash=_TOPOLOGY_HASH),
            coordination_token=setup.coordination_token,
        )
    token_ids: list[str] = []
    for index in range(token_count):
        _row, token = setup.factory.data_flow.create_row_with_token(
            setup.run_id,
            setup.source_node_id,
            index,
            {"value": index},
            source_row_index=index,
            ingest_sequence=index,
        )
        token_ids.append(token.token_id)
    register_test_worker(setup.db, run_id=setup.run_id, worker_id=_FOLLOWER_WORKER_ID)
    if expire_seat:
        expire_leader_seat(setup.db, setup.run_id)
    return setup, token_ids


def _run_status(setup: RecorderSetup) -> RunStatus:
    run = setup.factory.run_lifecycle.get_run(setup.run_id)
    assert run is not None
    return run.status


def _abandoned_token_ids(setup: RecorderSetup) -> list[str]:
    with setup.db.connection() as conn:
        return list(
            conn.execute(
                select(token_outcomes_table.c.token_id)
                .where(token_outcomes_table.c.run_id == setup.run_id)
                .where(token_outcomes_table.c.path == TerminalPath.ABANDONED.value)
                .order_by(token_outcomes_table.c.token_id)
            )
            .scalars()
            .all()
        )


def _worker_statuses(setup: RecorderSetup) -> dict[str, str]:
    with setup.db.connection() as conn:
        rows = conn.execute(
            select(run_workers_table.c.worker_id, run_workers_table.c.status).where(run_workers_table.c.run_id == setup.run_id)
        ).fetchall()
    return {str(row.worker_id): str(row.status) for row in rows}


class TestInspectLeaderlessRun:
    def test_dead_seat_loading_source_is_admissible_and_not_resumable(self) -> None:
        setup, token_ids = _leaderless_run()

        preflight = inspect_leaderless_run(setup.db, setup.run_id)

        assert preflight.admissible
        assert preflight.refusal is None
        assert preflight.run_status is RunStatus.RUNNING
        assert preflight.leader_worker_id == _LEADER_WORKER_ID
        assert preflight.seat_live is False
        assert dict(preflight.source_lifecycle) == {"primary": "loading"}
        assert preflight.resume_check.can_resume is False
        assert preflight.resume_check.reason is not None
        assert "primary=loading" in preflight.resume_check.reason
        assert preflight.undecided_tokens == len(token_ids)
        # Inspection is read-only: nothing moved.
        assert _run_status(setup) is RunStatus.RUNNING
        assert _abandoned_token_ids(setup) == []
        assert _worker_statuses(setup) == {_LEADER_WORKER_ID: "active", _FOLLOWER_WORKER_ID: "active"}

    def test_dead_seat_exhausted_source_is_admissible_and_resumable(self) -> None:
        setup, _token_ids = _leaderless_run(lifecycle_state=RunSourceLifecycleState.EXHAUSTED)

        preflight = inspect_leaderless_run(setup.db, setup.run_id)

        assert preflight.admissible
        assert preflight.resume_check.can_resume is True

    def test_missing_run_is_refused(self) -> None:
        setup, _token_ids = _leaderless_run()

        preflight = inspect_leaderless_run(setup.db, "run-does-not-exist")

        assert not preflight.admissible
        assert preflight.run_status is None
        assert preflight.refusal is not None
        assert "not found" in preflight.refusal

    def test_live_seat_is_refused_and_names_the_leader(self) -> None:
        setup, _token_ids = _leaderless_run(expire_seat=False)

        preflight = inspect_leaderless_run(setup.db, setup.run_id)

        assert not preflight.admissible
        assert preflight.seat_live is True
        assert preflight.refusal is not None
        assert _LEADER_WORKER_ID in preflight.refusal
        assert "elspeth join" in preflight.refusal

    @pytest.mark.parametrize("terminal", [RunStatus.INTERRUPTED, RunStatus.FAILED, RunStatus.COMPLETED])
    def test_terminal_run_is_refused(self, terminal: RunStatus) -> None:
        setup, _token_ids = _leaderless_run()
        with setup.db.engine.begin() as conn:
            conn.execute(
                update(runs_table).where(runs_table.c.run_id == setup.run_id).values(status=terminal.value, completed_at=datetime.now(UTC))
            )

        preflight = inspect_leaderless_run(setup.db, setup.run_id)

        assert not preflight.admissible
        assert preflight.run_status is terminal
        assert preflight.refusal is not None
        assert terminal.value in preflight.refusal


class TestAbandonLeaderlessRun:
    def test_finalizes_interrupted_abandons_undecided_tokens_and_vacates_the_seat(self) -> None:
        setup, token_ids = _leaderless_run()

        outcome = abandon_leaderless_run(setup.db, setup.run_id)

        assert outcome.run_id == setup.run_id
        assert outcome.run_status is RunStatus.INTERRUPTED
        assert outcome.leader_epoch == 2
        assert outcome.abandoned_tokens == len(token_ids)
        assert _run_status(setup) is RunStatus.INTERRUPTED
        assert _abandoned_token_ids(setup) == sorted(token_ids)
        # The dead leader is identity-evicted by the takeover CAS; the follower
        # is departed by the fenced finalize; the abandoning worker departs
        # with the seat release. Nobody is left ``active`` on a terminal run.
        statuses = _worker_statuses(setup)
        assert statuses[_LEADER_WORKER_ID] == "evicted"
        assert statuses[_FOLLOWER_WORKER_ID] == "departed"
        assert statuses[outcome.worker_id] == "departed"
        assert RunCoordinationRepository(setup.db.engine).live_leader(run_id=setup.run_id) is None
        with setup.db.connection() as conn:
            event_types = list(
                conn.execute(
                    select(run_coordination_events_table.c.event_type)
                    .where(run_coordination_events_table.c.run_id == setup.run_id)
                    .order_by(run_coordination_events_table.c.seq)
                )
                .scalars()
                .all()
            )
        assert "leader_acquire" in event_types
        assert "finalize" in event_types

    def test_resumable_run_is_finalized_without_abandoning_tokens(self) -> None:
        """Sources complete + checkpoint present: resume could still deliver, so
        ADR-038 keeps the tokens honestly pending under INTERRUPTED."""
        setup, _token_ids = _leaderless_run(lifecycle_state=RunSourceLifecycleState.EXHAUSTED)

        outcome = abandon_leaderless_run(setup.db, setup.run_id)

        assert outcome.run_status is RunStatus.INTERRUPTED
        assert outcome.abandoned_tokens == 0
        assert _abandoned_token_ids(setup) == []

    def test_refusal_mutates_nothing(self) -> None:
        setup, _token_ids = _leaderless_run(expire_seat=False)

        with pytest.raises(AbandonRefusedError) as exc_info:
            abandon_leaderless_run(setup.db, setup.run_id)

        assert exc_info.value.run_id == setup.run_id
        assert _LEADER_WORKER_ID in exc_info.value.reason
        assert _run_status(setup) is RunStatus.RUNNING
        assert _abandoned_token_ids(setup) == []
        assert _worker_statuses(setup) == {_LEADER_WORKER_ID: "active", _FOLLOWER_WORKER_ID: "active"}

    def test_seat_revived_between_preflight_and_cas_surfaces_the_cas_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The preflight is advisory; the takeover CAS is the arbiter."""
        setup, _token_ids = _leaderless_run()
        real_inspect = inspect_leaderless_run

        def inspect_then_revive(db: object, run_id: str) -> object:
            preflight = real_inspect(db, run_id)  # type: ignore[arg-type]
            with setup.db.engine.begin() as conn:
                from elspeth.core.landscape.database_clock import read_landscape_transaction_time
                from elspeth.core.landscape.schema import run_coordination_table

                revived = read_landscape_transaction_time(conn).replace(year=2999)
                conn.execute(
                    update(run_coordination_table)
                    .where(run_coordination_table.c.run_id == run_id)
                    .values(leader_heartbeat_expires_at=revived)
                )
            return preflight

        monkeypatch.setattr("elspeth.engine.orchestrator.abandon.inspect_leaderless_run", inspect_then_revive)

        with pytest.raises(NonResumableRunError):
            abandon_leaderless_run(setup.db, setup.run_id)

        assert _run_status(setup) is RunStatus.RUNNING
        assert _abandoned_token_ids(setup) == []
