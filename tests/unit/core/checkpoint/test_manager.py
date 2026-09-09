"""Unit tests for CheckpointManager unhappy paths and ordering behavior."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Connection, select, update
from sqlalchemy.engine import Row

from elspeth.contracts import Checkpoint, CheckpointDraft, Determinism, NodeType, RunStatus
from elspeth.contracts.barrier_scalars import (
    AggregationNodeScalars,
    BarrierScalars,
    CoalescePendingScalars,
)
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.core.checkpoint import manager as checkpoint_manager_module
from elspeth.core.checkpoint.manager import CheckpointCorruptionError, CheckpointManager, _validate_barrier_scalars_json_size
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.schema import checkpoints_table, nodes_table, rows_table, run_coordination_table, runs_table, tokens_table
from tests.fixtures.landscape import make_landscape_db
from tests.helpers.run_coordination import register_run_leader


@pytest.fixture
def db() -> LandscapeDB:
    return make_landscape_db()


@pytest.fixture
def checkpoint_manager(db: LandscapeDB) -> CheckpointManager:
    return CheckpointManager(db)


def _insert_checkpoint_prereqs(
    conn: Connection,
    *,
    run_id: str = "run-001",
    node_id: str = "node-001",
    row_id: str = "row-001",
    token_id: str = "tok-001",
) -> None:
    now = datetime.now(UTC)
    conn.execute(
        runs_table.insert().values(
            run_id=run_id,
            started_at=now,
            config_hash="cfg",
            settings_json="{}",
            canonical_version="sha256-rfc8785-v1",
            status=RunStatus.RUNNING,
            openrouter_catalog_sha256="0" * 64,
            openrouter_catalog_source="bundled",
        )
    )
    conn.execute(
        nodes_table.insert().values(
            node_id=node_id,
            run_id=run_id,
            plugin_name="test",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0.0",
            determinism=Determinism.DETERMINISTIC,
            config_hash="node_cfg",
            config_json="{}",
            registered_at=now,
        )
    )
    conn.execute(
        rows_table.insert().values(
            row_id=row_id,
            run_id=run_id,
            source_node_id=node_id,
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            source_data_hash="hash",
            created_at=now,
        )
    )
    conn.execute(
        tokens_table.insert().values(
            token_id=token_id,
            row_id=row_id,
            run_id=run_id,
            created_at=now,
        )
    )


def _seed_run(db: LandscapeDB, run_id: str = "run-001") -> CoordinationToken:
    """Seed the checkpoint FK prerequisites and mint the run's epoch-1 leader seat.

    The returned token is the seat's own image (what ``begin_run`` hands the
    leader); every fenced checkpoint write in this module presents it.
    """
    with db.write_connection() as conn:
        _insert_checkpoint_prereqs(conn, run_id=run_id, node_id=f"node-{run_id}", row_id=f"row-{run_id}", token_id=f"tok-{run_id}")
    return register_run_leader(
        RunCoordinationRepository(db.engine),
        run_id=run_id,
        worker_id=f"worker:{run_id}:leader",
        window_seconds=80.0,
    )


def _select_checkpoint(db: LandscapeDB, checkpoint_id: str) -> Row[Any]:
    with db.engine.connect() as conn:
        row = conn.execute(select(checkpoints_table).where(checkpoints_table.c.checkpoint_id == checkpoint_id)).fetchone()
    assert row is not None
    return row


def _checkpoint_rows(db: LandscapeDB) -> list[Row[Any]]:
    with db.engine.connect() as conn:
        return list(conn.execute(select(checkpoints_table)).fetchall())


def _draft(
    *,
    run_id: str = "run-001",
    sequence_number: int = 1,
    barrier_scalars: BarrierScalars | None = None,
    upstream_topology_hash: str = "a" * 64,
) -> CheckpointDraft:
    return CheckpointDraft(
        run_id=run_id,
        sequence_number=sequence_number,
        barrier_scalars=barrier_scalars,
        upstream_topology_hash=upstream_topology_hash,
    )


def test_create_checkpoint_persists_precomputed_topology_hash_without_graph(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
) -> None:
    token = _seed_run(db, "run-draft")

    draft = _draft(
        run_id="run-draft",
        sequence_number=1,
        barrier_scalars=None,
        upstream_topology_hash="f" * 64,
    )

    checkpoint = checkpoint_manager.create_checkpoint(draft=draft, coordination_token=token)

    assert checkpoint.upstream_topology_hash == "f" * 64
    row = _select_checkpoint(db, checkpoint.checkpoint_id)
    assert row.upstream_topology_hash == "f" * 64


def test_create_checkpoint_requires_draft(checkpoint_manager: CheckpointManager) -> None:
    token = CoordinationToken(run_id="run-001", worker_id="worker:run-001:leader", leader_epoch=1)
    with pytest.raises(TypeError, match="draft must be CheckpointDraft"):
        checkpoint_manager.create_checkpoint(draft=None, coordination_token=token)  # type: ignore[arg-type]


def test_create_checkpoint_refuses_a_draft_for_another_run_before_any_database_effect(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
) -> None:
    """ADR-048 §2: the token's run is the only run a checkpoint can belong to.

    The refusal precedes the fence, so the seat is neither extended nor
    refused (no ``fence_refusal`` event) and no checkpoint row exists.
    """
    token = _seed_run(db, "run-001")
    with db.engine.connect() as conn:
        seat_before = conn.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == "run-001")).one()

    with pytest.raises(OrchestrationInvariantError, match=r"run-elsewhere.*run-001"):
        checkpoint_manager.create_checkpoint(draft=_draft(run_id="run-elsewhere"), coordination_token=token)

    assert _checkpoint_rows(db) == []
    with db.engine.connect() as conn:
        seat_after = conn.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == "run-001")).one()
    assert tuple(seat_after) == tuple(seat_before), "a refusal before the fence leaves the seat untouched"


def test_validate_barrier_scalars_json_size_rejects_large_payload() -> None:
    """Serialized barrier-scalars size guard lives at the manager boundary (hard-fail)."""
    with pytest.raises(OrchestrationInvariantError, match="exceeds 10MB limit"):
        _validate_barrier_scalars_json_size("x" * 10_000_001)


def test_create_checkpoint_persists_barrier_scalars(db: LandscapeDB, checkpoint_manager: CheckpointManager) -> None:
    """F1 Task 1.2: the checkpoint row carries BarrierScalars only (format_version 5)."""
    token = _seed_run(db)

    scalars = BarrierScalars(
        aggregation={"agg-1": AggregationNodeScalars(count_fire_offset=2.25, condition_fire_offset=None)},
        coalesce={},
    )
    cp = checkpoint_manager.create_checkpoint(draft=_draft(barrier_scalars=scalars), coordination_token=token)

    row = _select_checkpoint(db, cp.checkpoint_id)
    assert row.barrier_scalars_json is not None
    persisted = json.loads(row.barrier_scalars_json)
    # Wire shape: per-node envelope carries _version alongside the offsets.
    assert persisted["aggregation"]["agg-1"]["count_fire_offset"] == 2.25
    assert persisted["aggregation"]["agg-1"]["condition_fire_offset"] is None
    assert cp.format_version == 5
    assert cp.barrier_scalars_json == row.barrier_scalars_json


def test_create_checkpoint_empty_scalars_persist_null(db: LandscapeDB, checkpoint_manager: CheckpointManager) -> None:
    """Empty BarrierScalars (has_state=False) persists NULL, same as None."""
    token = _seed_run(db)

    cp_none = checkpoint_manager.create_checkpoint(draft=_draft(sequence_number=1, barrier_scalars=None), coordination_token=token)
    cp_empty = checkpoint_manager.create_checkpoint(
        draft=_draft(sequence_number=2, barrier_scalars=BarrierScalars(aggregation={}, coalesce={})),
        coordination_token=token,
    )

    assert cp_none.barrier_scalars_json is None
    assert cp_empty.barrier_scalars_json is None
    assert _select_checkpoint(db, cp_none.checkpoint_id).barrier_scalars_json is None
    assert _select_checkpoint(db, cp_empty.checkpoint_id).barrier_scalars_json is None


def test_get_checkpoints_returns_ascending_sequence_order(db: LandscapeDB, checkpoint_manager: CheckpointManager) -> None:
    token = _seed_run(db)

    checkpoint_manager.create_checkpoint(draft=_draft(sequence_number=5), coordination_token=token)
    checkpoint_manager.create_checkpoint(draft=_draft(sequence_number=1), coordination_token=token)
    checkpoint_manager.create_checkpoint(draft=_draft(sequence_number=3), coordination_token=token)

    checkpoints = checkpoint_manager.get_checkpoints("run-001")
    assert [cp.sequence_number for cp in checkpoints] == [1, 3, 5]


def test_create_checkpoint_rejects_duplicate_sequence_for_run(db: LandscapeDB, checkpoint_manager: CheckpointManager) -> None:
    """Duplicate per-run checkpoint sequence numbers would make resume order ambiguous."""
    token = _seed_run(db)

    checkpoint_manager.create_checkpoint(draft=_draft(sequence_number=1), coordination_token=token)

    with pytest.raises(OrchestrationInvariantError, match="Duplicate checkpoint sequence_number"):
        checkpoint_manager.create_checkpoint(draft=_draft(sequence_number=1), coordination_token=token)


def test_create_checkpoint_round_trips_coalesce_scalars(db: LandscapeDB, checkpoint_manager: CheckpointManager) -> None:
    """Coalesce lost-branch scalars round-trip through persistence unchanged."""
    token = _seed_run(db)

    checkpoint = checkpoint_manager.create_checkpoint(
        draft=_draft(
            barrier_scalars=BarrierScalars(
                aggregation={},
                coalesce={("merge_paths", "row-001"): CoalescePendingScalars(lost_branches={"branch_b": "timeout"})},
            )
        ),
        coordination_token=token,
    )

    assert checkpoint.barrier_scalars_json is not None
    loaded = checkpoint_manager.get_latest_checkpoint("run-001")
    assert loaded is not None
    assert loaded.barrier_scalars_json == checkpoint.barrier_scalars_json
    restored = BarrierScalars.from_dict(json.loads(loaded.barrier_scalars_json))
    assert restored.coalesce[("merge_paths", "row-001")].lost_branches == {"branch_b": "timeout"}


@pytest.mark.parametrize(
    "format_version",
    [None, Checkpoint.CURRENT_FORMAT_VERSION - 1, Checkpoint.CURRENT_FORMAT_VERSION + 1],
)
def test_get_latest_checkpoint_loads_raw_format_versions(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
    format_version: int | None,
) -> None:
    """Repository reads expose stored checkpoint rows; resume policy lives elsewhere."""
    run_id = f"run-format-{format_version}"
    token = _seed_run(db, run_id)

    created = checkpoint_manager.create_checkpoint(
        draft=_draft(run_id=run_id, sequence_number=1, barrier_scalars=None),
        coordination_token=token,
    )

    with db.engine.begin() as conn:
        conn.execute(update(checkpoints_table).where(checkpoints_table.c.run_id == run_id).values(format_version=format_version))

    loaded = checkpoint_manager.get_latest_checkpoint(run_id)
    assert loaded is not None
    assert loaded.checkpoint_id == created.checkpoint_id
    assert loaded.format_version == format_version


def test_get_latest_checkpoint_rejects_non_string_barrier_scalars_json(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
) -> None:
    run_id = "run-tampered-type"
    token = _seed_run(db, run_id)

    checkpoint_manager.create_checkpoint(draft=_draft(run_id=run_id, sequence_number=1, barrier_scalars=None), coordination_token=token)

    with db.engine.begin() as conn:
        conn.execute(
            update(checkpoints_table)
            .where(checkpoints_table.c.run_id == run_id)
            .values(barrier_scalars_json=b'{"aggregation": {}, "coalesce": {}}')
        )

    with pytest.raises(CheckpointCorruptionError, match="barrier_scalars_json"):
        checkpoint_manager.get_latest_checkpoint(run_id)


@pytest.mark.parametrize("read_all", [False, True])
def test_checkpoint_reads_reject_oversized_persisted_barrier_scalars_json(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
    monkeypatch: pytest.MonkeyPatch,
    read_all: bool,
) -> None:
    run_id = "run-tampered-size"
    token = _seed_run(db, run_id)

    checkpoint_manager.create_checkpoint(draft=_draft(run_id=run_id, sequence_number=1, barrier_scalars=None), coordination_token=token)

    with db.engine.begin() as conn:
        conn.execute(
            update(checkpoints_table).where(checkpoints_table.c.run_id == run_id).values(barrier_scalars_json='{"too_big": "xxxx"}')
        )

    monkeypatch.setattr(checkpoint_manager_module, "_MAX_BARRIER_SCALARS_BYTES", 5)

    with pytest.raises(CheckpointCorruptionError, match="barrier_scalars_json"):
        if read_all:
            checkpoint_manager.get_checkpoints(run_id)
        else:
            checkpoint_manager.get_latest_checkpoint(run_id)


def test_delete_checkpoints_removes_all_for_the_token_run(db: LandscapeDB, checkpoint_manager: CheckpointManager) -> None:
    """The run purged is the token's run (ADR-048 §2); another run's anchors stay."""
    token = _seed_run(db)
    other = _seed_run(db, "run-002")

    checkpoint_manager.create_checkpoint(draft=_draft(sequence_number=1), coordination_token=token)
    checkpoint_manager.create_checkpoint(draft=_draft(sequence_number=2), coordination_token=token)
    checkpoint_manager.create_checkpoint(draft=_draft(run_id="run-002", sequence_number=1), coordination_token=other)

    deleted = checkpoint_manager.delete_checkpoints(coordination_token=token)
    assert deleted == 2
    assert checkpoint_manager.get_checkpoints("run-001") == []
    assert [cp.sequence_number for cp in checkpoint_manager.get_checkpoints("run-002")] == [1]
