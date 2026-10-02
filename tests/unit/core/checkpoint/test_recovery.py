"""Unit tests for RecoveryManager resume and row-recovery behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, select, update

from elspeth.contracts import (
    Checkpoint,
    Determinism,
    NodeType,
    RunStatus,
)
from elspeth.contracts.barrier_scalars import AggregationNodeScalars, BarrierScalars
from elspeth.contracts.checkpoint import ResumeRefusalCause
from elspeth.contracts.contract_records import ContractAuditRecord
from elspeth.contracts.errors import EmptyResumeStateError, OrchestrationInvariantError
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.core.checkpoint import CheckpointCorruptionError, CheckpointManager, RecoveryManager
from elspeth.core.checkpoint import recovery as recovery_module
from elspeth.core.checkpoint.recovery import NonResumableRunError
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import (
    checkpoints_table,
    nodes_table,
    rows_table,
    run_sources_table,
    runs_table,
    token_work_items_table,
    tokens_table,
)
from tests.fixtures.audit_hashing import fake_sha256
from tests.fixtures.landscape import insert_crashed_leader_seat, make_landscape_db
from tests.helpers.checkpoint import create_checkpoint
from tests.helpers.run_coordination import register_run_leader


@pytest.fixture
def db() -> LandscapeDB:
    return make_landscape_db()


@pytest.fixture
def checkpoint_manager(db: LandscapeDB) -> CheckpointManager:
    return CheckpointManager(db)


@pytest.fixture
def recovery_manager(db: LandscapeDB, checkpoint_manager: CheckpointManager) -> RecoveryManager:
    return RecoveryManager(db, checkpoint_manager)


def _create_contract() -> tuple[str, str]:
    contract = SchemaContract(
        mode="FIXED",
        fields=(
            FieldContract(
                normalized_name="id",
                original_name="id",
                python_type=int,
                required=True,
                source="declared",
            ),
        ),
        locked=True,
    )
    return ContractAuditRecord.from_contract(contract).to_json(), contract.version_hash()


def _create_graph(*, node_id: str = "checkpoint-node", config: dict[str, Any] | None = None) -> ExecutionGraph:
    graph = ExecutionGraph()
    graph.add_node(node_id, node_type=NodeType.TRANSFORM, plugin_name="test", config=config or {})
    return graph


def _create_checkpoint(
    checkpoint_manager: CheckpointManager,
    *,
    run_id: str,
    sequence_number: int,
    graph: ExecutionGraph,
    barrier_scalars: BarrierScalars | None = None,
) -> Checkpoint:
    # Written under the run's own seat, read back (ADR-048 §5).
    return create_checkpoint(
        checkpoint_manager,
        run_id=run_id,
        sequence_number=sequence_number,
        graph=graph,
        barrier_scalars=barrier_scalars,
    )


def _insert_run(
    conn: Connection,
    run_id: str,
    *,
    status: RunStatus | str,
    with_contract: bool = False,
    contract_json_override: str | None = None,
    with_crashed_seat: bool = True,
) -> None:
    """Insert a ``runs`` row, plus a ``run_sources`` row when a contract is requested.

    ``with_crashed_seat`` leaves the lapsed ``run_coordination`` seat a
    crashed leader would have left (a raw-SQL run has none), so checkpoint
    writes can read the seat back (ADR-048 §5); a test that mints its own
    seat passes ``False``.

    ADR-025 §3 Decision 5 (G6): the schema contract lives exclusively on
    ``run_sources.schema_contract_json``; the run-level singleton columns
    were deleted along with their accessors. The helper auto-creates a
    SOURCE node "source-node" when a contract is requested so the
    ``run_sources`` foreign-key constraint holds; callers that need to
    inspect the source node explicitly insert additional nodes via
    :func:`_insert_node` after this helper returns.
    """
    schema_contract_json: str | None = None
    schema_contract_hash: str | None = None
    if with_contract:
        schema_contract_json, schema_contract_hash = _create_contract()
    if contract_json_override is not None:
        schema_contract_json = contract_json_override
        # Intentionally mismatched when override is used for corruption tests.
        schema_contract_hash = "deadbeef" * 4

    conn.execute(
        runs_table.insert().values(
            run_id=run_id,
            started_at=datetime.now(UTC),
            config_hash=fake_sha256("cfg"),
            settings_json="{}",
            canonical_version="sha256-rfc8785-v1",
            status=status,
            openrouter_catalog_sha256="0" * 64,
            openrouter_catalog_source="bundled",
        )
    )
    if with_crashed_seat:
        insert_crashed_leader_seat(conn, run_id=run_id)

    if schema_contract_json is not None:
        # Ensure the SOURCE node exists before writing run_sources (FK constraint).
        # The node may already have been inserted by an earlier helper call;
        # check first instead of relying on ON CONFLICT semantics, since the
        # in-memory SQLite engine doesn't return rowcount reliably for
        # INSERT OR IGNORE under all dialects.
        existing_node = conn.execute(
            select(nodes_table.c.node_id).where(nodes_table.c.node_id == "source-node").where(nodes_table.c.run_id == run_id)
        ).fetchone()
        if existing_node is None:
            _insert_node(conn, run_id, "source-node", node_type=NodeType.SOURCE)
        conn.execute(
            run_sources_table.insert().values(
                run_id=run_id,
                source_node_id="source-node",
                source_name="primary",
                plugin_name="test_source",
                lifecycle_state="loaded",
                config_hash=fake_sha256("src_cfg"),
                schema_json="{}",
                schema_contract_json=schema_contract_json,
                schema_contract_hash=schema_contract_hash,
                field_resolution_json=None,
                recorded_at=datetime.now(UTC),
            )
        )


def _insert_node(conn: Connection, run_id: str, node_id: str, *, node_type: NodeType = NodeType.TRANSFORM) -> None:
    conn.execute(
        nodes_table.insert().values(
            node_id=node_id,
            run_id=run_id,
            plugin_name="test",
            node_type=node_type,
            plugin_version="1.0.0",
            determinism=Determinism.DETERMINISTIC,
            config_hash=fake_sha256("node_cfg"),
            config_json="{}",
            registered_at=datetime.now(UTC),
        )
    )


def _insert_row(conn: Connection, run_id: str, row_id: str, *, row_index: int, source_data_ref: str | None) -> None:
    conn.execute(
        rows_table.insert().values(
            row_id=row_id,
            run_id=run_id,
            source_node_id="source-node",
            row_index=row_index,
            source_row_index=row_index,
            ingest_sequence=row_index,
            source_data_hash=fake_sha256(f"hash-{row_id}"),
            source_data_ref=source_data_ref,
            created_at=datetime.now(UTC),
        )
    )


def _insert_token(conn: Connection, run_id: str, token_id: str, row_id: str) -> None:
    conn.execute(
        tokens_table.insert().values(
            token_id=token_id,
            row_id=row_id,
            run_id=run_id,
            created_at=datetime.now(UTC),
        )
    )


def _insert_blocked_work_item(
    conn: Connection,
    run_id: str,
    token_id: str,
    row_id: str,
    *,
    barrier_key: str | None,
    queue_key: str | None = None,
    status: TokenWorkStatus = TokenWorkStatus.BLOCKED,
) -> None:
    """Insert a scheduler-journal work item (F1: journal owns buffered tokens).

    ``barrier_key`` non-NULL marks a barrier hold (aggregation node_id or
    coalesce name); ``barrier_key=None`` with a ``queue_key`` models an
    ADR-028 queue-hold, which must stay IN the re-drive work set.
    """
    now = datetime.now(UTC)
    conn.execute(
        token_work_items_table.insert().values(
            work_item_id=f"wi-{token_id}",
            run_id=run_id,
            token_id=token_id,
            row_id=row_id,
            node_id=None,
            step_index=0,
            ingest_sequence=0,
            row_payload_json="{}",
            status=status.value,
            queue_key=queue_key,
            barrier_key=barrier_key,
            attempt=0,
            available_at=now,
            barrier_blocked_at=now if barrier_key is not None else None,
            created_at=now,
            updated_at=now,
            lineage_path_json="[]",
        )
    )


def _create_failed_run_with_checkpoint(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
    run_id: str,
    *,
    status: RunStatus | str = RunStatus.FAILED,
    checkpoint_node_id: str = "checkpoint-node",
    with_contract: bool = True,
    barrier_scalars: BarrierScalars | None = None,
    graph: ExecutionGraph | None = None,
) -> ExecutionGraph:
    active_graph = graph or _create_graph(node_id=checkpoint_node_id)

    with db.write_connection() as conn:
        _insert_run(conn, run_id, status=status, with_contract=with_contract)
        # _insert_run auto-creates "source-node" + run_sources when with_contract=True
        # (per ADR-025 §3 Decision 5). Only insert explicitly when no contract is set.
        if not with_contract:
            _insert_node(conn, run_id, "source-node", node_type=NodeType.SOURCE)
        _insert_node(conn, run_id, checkpoint_node_id)
        _insert_row(conn, run_id, "row-0", row_index=0, source_data_ref=None)
        _insert_token(conn, run_id, "tok-0", "row-0")

    _create_checkpoint(
        checkpoint_manager,
        run_id=run_id,
        sequence_number=1,
        barrier_scalars=barrier_scalars,
        graph=active_graph,
    )
    return active_graph


def test_can_resume_returns_false_for_missing_run(recovery_manager: RecoveryManager) -> None:
    check = recovery_manager.can_resume("missing", _create_graph())
    assert check.can_resume is False
    assert check.reason == "Run missing not found"
    assert check.cause is ResumeRefusalCause.RUN_NOT_FOUND


def test_can_resume_rejects_completed_run(db: LandscapeDB, recovery_manager: RecoveryManager) -> None:
    with db.write_connection() as conn:
        _insert_run(conn, "run-completed", status=RunStatus.COMPLETED)

    check = recovery_manager.can_resume("run-completed", _create_graph())
    assert check.can_resume is False
    assert check.reason == "Run already completed successfully"
    assert check.cause is ResumeRefusalCause.RUN_TERMINAL


def test_can_resume_rejects_running_run_with_live_seat(db: LandscapeDB, recovery_manager: RecoveryManager) -> None:
    """RUNNING + LIVE seat is refused: run is held by an active leader.

    Slice-4 flip (§H test #2(c)): RUNNING + absent/expired seat is now
    RESUMABLE (dead-leader takeover).  This test pins the live-seat refusal
    that must remain — only the expired-seat arm flipped.
    """
    from elspeth.contracts.coordination import mint_worker_id
    from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository

    with db.write_connection() as conn:
        _insert_run(conn, "run-running", status=RunStatus.RUNNING, with_crashed_seat=False)
    # Register a live leader seat so the guard fires the refusal.
    leader_id = mint_worker_id("run-running")
    register_run_leader(
        RunCoordinationRepository(db.engine),
        run_id="run-running",
        worker_id=leader_id,
        window_seconds=80.0,
    )

    check = recovery_manager.can_resume("run-running", _create_graph())
    assert check.can_resume is False
    assert check.reason is not None
    assert "in progress under live leader" in check.reason


@pytest.mark.parametrize(
    "status",
    [RunStatus.COMPLETED_WITH_FAILURES, RunStatus.EMPTY],
)
def test_can_resume_rejects_terminal_statuses_even_when_checkpoint_exists(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
    recovery_manager: RecoveryManager,
    status: RunStatus,
) -> None:
    run_id = f"run-terminal-{status.value}"
    graph = _create_failed_run_with_checkpoint(db, checkpoint_manager, run_id, status=status)

    check = recovery_manager.can_resume(run_id, graph)

    assert check.can_resume is False
    assert check.reason == f"Run status {status.value!r} is not resumable"


def test_can_resume_rejects_corrupt_stored_run_status(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
    recovery_manager: RecoveryManager,
) -> None:
    run_id = "run-corrupt-status"
    graph = _create_failed_run_with_checkpoint(db, checkpoint_manager, run_id, status="bogus")

    with pytest.raises(CheckpointCorruptionError, match="invalid status 'bogus'"):
        recovery_manager.can_resume(run_id, graph)


def test_can_resume_rejects_failed_run_without_checkpoint(db: LandscapeDB, recovery_manager: RecoveryManager) -> None:
    with db.write_connection() as conn:
        _insert_run(conn, "run-no-checkpoint", status=RunStatus.FAILED)

    check = recovery_manager.can_resume("run-no-checkpoint", _create_graph())
    assert check.can_resume is False
    assert check.reason == "Run has no resume baseline (run predates run-start checkpointing or checkpointing was disabled)"
    assert check.cause is ResumeRefusalCause.CHECKPOINT_MISSING


def test_can_resume_reason_for_missing_baseline_is_journal_flavoured(db: LandscapeDB, recovery_manager: RecoveryManager) -> None:
    """F1 Task 3.2: the missing-checkpoint refuse speaks journal semantics.

    Post-F1 the checkpoint row is only the resume BASELINE (scalars +
    topology anchor); buffered work lives in journal BLOCKED rows. The
    refuse reason must not present the checkpoint as the recovery store —
    if it mentions checkpointing at all, it is in the baseline framing.
    """
    with db.write_connection() as conn:
        _insert_run(conn, "run-no-baseline", status=RunStatus.FAILED)

    check = recovery_manager.can_resume("run-no-baseline", _create_graph())
    assert not check.can_resume
    assert check.reason is not None
    assert "checkpoint" not in check.reason.lower() or "baseline" in check.reason.lower()


def test_can_resume_returns_reason_when_checkpoint_format_is_incompatible(
    db: LandscapeDB, checkpoint_manager: CheckpointManager, recovery_manager: RecoveryManager
) -> None:
    run_id = "run-incompatible"
    graph = _create_failed_run_with_checkpoint(db, checkpoint_manager, run_id)
    with db.engine.begin() as conn:
        conn.execute(
            update(checkpoints_table)
            .where(checkpoints_table.c.run_id == run_id)
            .values(format_version=Checkpoint.CURRENT_FORMAT_VERSION + 1)
        )
    check = recovery_manager.can_resume(run_id, graph)
    assert check.can_resume is False
    assert check.reason is not None
    assert "incompatible format version" in check.reason
    assert check.cause is ResumeRefusalCause.CHECKPOINT_FORMAT_INCOMPATIBLE


def test_can_resume_rejects_topology_mismatch(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
    recovery_manager: RecoveryManager,
) -> None:
    run_id = "run-topology-mismatch"
    original_graph = _create_failed_run_with_checkpoint(
        db,
        checkpoint_manager,
        run_id,
        graph=_create_graph(node_id="checkpoint-node", config={"version": 1}),
    )
    assert original_graph.has_node("checkpoint-node")

    changed_graph = _create_graph(node_id="checkpoint-node", config={"version": 2})
    check = recovery_manager.can_resume(run_id, changed_graph)
    assert check.can_resume is False
    assert check.reason is not None
    assert check.cause is ResumeRefusalCause.CHECKPOINT_TOPOLOGY_CHANGED


def test_can_resume_true_for_failed_run_with_valid_checkpoint(
    db: LandscapeDB, checkpoint_manager: CheckpointManager, recovery_manager: RecoveryManager
) -> None:
    run_id = "run-resumable"
    graph = _create_failed_run_with_checkpoint(db, checkpoint_manager, run_id)

    check = recovery_manager.can_resume(run_id, graph)
    assert check.can_resume is True
    assert check.reason is None


def test_can_resume_refuses_web_search_post_without_effect_reservation(
    db: LandscapeDB, checkpoint_manager: CheckpointManager, recovery_manager: RecoveryManager
) -> None:
    graph = ExecutionGraph()
    graph.add_node("checkpoint-node", node_type=NodeType.TRANSFORM, plugin_name="web_scrape", config={"method": "POST"})
    run_id = "run-post-search-uncertain-effect"
    _create_failed_run_with_checkpoint(db, checkpoint_manager, run_id, graph=graph)

    check = recovery_manager.can_resume(run_id, graph)
    assert not check.can_resume
    assert check.cause is ResumeRefusalCause.UNCERTAIN_REMOTE_EFFECT
    assert check.reason is not None and "POST" in check.reason


@pytest.mark.parametrize("lifecycle_state", ["ready", "loading", "interrupted"])
def test_can_resume_rejects_incomplete_source_lifecycle(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
    recovery_manager: RecoveryManager,
    lifecycle_state: str,
) -> None:
    """elspeth-1f5b83cd28: the advisory gate must refuse what resume() refuses.

    ``resume()`` raises ``IncompleteSourceResumeError`` for any source whose
    lifecycle never reached ``SOURCE_COMPLETE_LIFECYCLE_STATES`` — resume
    replays only persisted row payloads, so unread source rows would be
    silently lost. ``can_resume`` answering True for such a run is a false
    green: the operator is told the run is recoverable, then every resume
    attempt raises.
    """
    run_id = f"run-incomplete-source-{lifecycle_state}"
    graph = _create_failed_run_with_checkpoint(db, checkpoint_manager, run_id)
    with db.engine.begin() as conn:
        conn.execute(update(run_sources_table).where(run_sources_table.c.run_id == run_id).values(lifecycle_state=lifecycle_state))

    check = recovery_manager.can_resume(run_id, graph)
    assert check.can_resume is False
    assert check.reason is not None
    assert f"primary={lifecycle_state}" in check.reason

    # get_resume_point delegates to can_resume, so it must refuse too.
    assert check.cause is ResumeRefusalCause.SOURCE_NOT_EXHAUSTED
    with pytest.raises(NonResumableRunError) as exc_info:
        recovery_manager.get_resume_point(run_id, graph)
    assert exc_info.value.cause is check.cause
    assert exc_info.value.reason == check.reason


@pytest.mark.parametrize("lifecycle_state", ["exhausted", "loaded"])
def test_can_resume_accepts_complete_source_lifecycle(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
    recovery_manager: RecoveryManager,
    lifecycle_state: str,
) -> None:
    """Both ADR-038 complete states pass the source-lifecycle gate."""
    run_id = f"run-complete-source-{lifecycle_state}"
    graph = _create_failed_run_with_checkpoint(db, checkpoint_manager, run_id)
    with db.engine.begin() as conn:
        conn.execute(update(run_sources_table).where(run_sources_table.c.run_id == run_id).values(lifecycle_state=lifecycle_state))

    check = recovery_manager.can_resume(run_id, graph)
    assert check.can_resume is True
    assert check.reason is None


def test_get_resume_point_refuses_when_run_cannot_resume(recovery_manager: RecoveryManager) -> None:
    with pytest.raises(NonResumableRunError) as exc_info:
        recovery_manager.get_resume_point("missing", _create_graph())
    assert exc_info.value.cause is ResumeRefusalCause.RUN_NOT_FOUND


def test_get_resume_point_refuses_if_checkpoint_missing_after_can_resume(
    db: LandscapeDB,
    recovery_manager: RecoveryManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with db.write_connection() as conn:
        _insert_run(conn, "run-race", status=RunStatus.FAILED, with_contract=True)

    monkeypatch.setattr(recovery_manager, "can_resume", lambda _run_id, _graph: type("Check", (), {"can_resume": True})())
    monkeypatch.setattr(recovery_manager._checkpoint_manager, "get_latest_checkpoint", lambda _run_id: None)

    with pytest.raises(NonResumableRunError) as exc_info:
        recovery_manager.get_resume_point("run-race", _create_graph())
    assert exc_info.value.cause is ResumeRefusalCause.CHECKPOINT_MISSING


def test_get_resume_point_propagates_checkpoint_corruption(
    db: LandscapeDB,
    recovery_manager: RecoveryManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: persisted-checkpoint corruption CRASHES get_resume_point.

    Pins the elspeth-ca0a7e71b1 fix: the dead compatibility-exception
    swallow around ``get_latest_checkpoint`` is gone — the raw persistence read
    raises CheckpointCorruptionError on malformed data and nothing converts a
    checkpoint-load failure into a silent "no resume point".
    """
    with db.write_connection() as conn:
        _insert_run(conn, "run-corrupt", status=RunStatus.FAILED, with_contract=True)

    monkeypatch.setattr(recovery_manager, "can_resume", lambda _run_id, _graph: type("Check", (), {"can_resume": True})())

    def _corrupt(_run_id: str) -> None:
        raise CheckpointCorruptionError("Corrupted checkpoint row")

    monkeypatch.setattr(recovery_manager._checkpoint_manager, "get_latest_checkpoint", _corrupt)

    with pytest.raises(CheckpointCorruptionError, match="Corrupted checkpoint row"):
        recovery_manager.get_resume_point("run-corrupt", _create_graph())


def test_get_resume_point_restores_barrier_scalars(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
    recovery_manager: RecoveryManager,
) -> None:
    """F1: the checkpoint contributes only the underivable barrier scalars."""
    run_id = "run-resume-point"
    graph = _create_failed_run_with_checkpoint(
        db,
        checkpoint_manager,
        run_id,
        barrier_scalars=BarrierScalars(
            aggregation={"agg-node": AggregationNodeScalars(count_fire_offset=1.5, condition_fire_offset=None)},
            coalesce={},
        ),
    )

    resume_point = recovery_manager.get_resume_point(run_id, graph)
    assert resume_point is not None
    assert resume_point.sequence_number == 1
    assert resume_point.barrier_scalars is not None
    assert resume_point.barrier_scalars.aggregation["agg-node"].count_fire_offset == 1.5


def test_get_resume_point_caches_barrier_scalars_parse(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
    recovery_manager: RecoveryManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume inspection should not parse the same barrier-scalars payload twice."""
    import importlib

    recovery_module: Any = importlib.import_module("elspeth.core.checkpoint.recovery")

    run_id = "run-scalars-parse"
    graph = _create_failed_run_with_checkpoint(
        db,
        checkpoint_manager,
        run_id,
        barrier_scalars=BarrierScalars(
            aggregation={"agg-node": AggregationNodeScalars(count_fire_offset=2.0, condition_fire_offset=None)},
            coalesce={},
        ),
    )

    original_checkpoint_loads = recovery_module.checkpoint_loads
    parsed_payloads: list[str] = []

    def counting_checkpoint_loads(payload: str) -> Any:
        parsed_payloads.append(payload)
        return original_checkpoint_loads(payload)

    monkeypatch.setattr(recovery_module, "checkpoint_loads", counting_checkpoint_loads)

    first = recovery_manager.get_resume_point(run_id, graph)
    second = recovery_manager.get_resume_point(run_id, graph)
    assert first is not None
    assert second is not None
    assert first.barrier_scalars is not None
    assert parsed_payloads == [first.checkpoint.barrier_scalars_json]


def test_count_blocked_barrier_items_counts_only_barrier_holds(
    db: LandscapeDB,
    recovery_manager: RecoveryManager,
) -> None:
    """Public journal-count surface: barrier holds in, queue-holds and READY out.

    Consumed by the resume coordinator's quiescence gate and the CLI resume
    preflight (the inline CLI query was absorbed here in F1 Task 3.2).
    """
    run_id = "run-blocked-count"
    with db.write_connection() as conn:
        _insert_run(conn, run_id, status=RunStatus.FAILED, with_contract=True)
        _insert_row(conn, run_id, "row-1", row_index=0, source_data_ref=None)
        _insert_token(conn, run_id, "tok-barrier-1", "row-1")
        _insert_token(conn, run_id, "tok-barrier-2", "row-1")
        _insert_token(conn, run_id, "tok-queue", "row-1")
        _insert_token(conn, run_id, "tok-ready", "row-1")
        _insert_blocked_work_item(conn, run_id, "tok-barrier-1", "row-1", barrier_key="agg-node")
        _insert_blocked_work_item(conn, run_id, "tok-barrier-2", "row-1", barrier_key="merge1")
        _insert_blocked_work_item(conn, run_id, "tok-queue", "row-1", barrier_key=None, queue_key="llm-rate-limit")
        _insert_blocked_work_item(conn, run_id, "tok-ready", "row-1", barrier_key=None, status=TokenWorkStatus.READY)

    assert recovery_manager.count_blocked_barrier_items(run_id) == 2
    assert recovery_manager.count_blocked_barrier_items("other-run") == 0


def test_recovery_uses_scheduler_barrier_resume_boundary(
    db: LandscapeDB,
    recovery_manager: RecoveryManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeBarrierJournalRepository:
        def __init__(self, engine: Any, *, events: Any) -> None:
            assert engine is db.engine

        def count_blocked_barrier_items(self, *, run_id: str) -> int:
            calls.append(("count", run_id))
            return 7

    monkeypatch.setattr(recovery_module, "BarrierJournalRepository", FakeBarrierJournalRepository)

    assert recovery_manager.count_blocked_barrier_items("run-boundary") == 7
    assert calls == [("count", "run-boundary")]


def test_recovery_barrier_resume_boundary_supports_read_only_handles(tmp_path: Path) -> None:
    db_path = tmp_path / "landscape.db"
    writable = LandscapeDB.from_url(f"sqlite:///{db_path}")
    try:
        run_id = "run-read-only-boundary"
        with writable.write_connection() as conn:
            _insert_run(conn, run_id, status=RunStatus.FAILED, with_contract=True)
            _insert_row(conn, run_id, "row-1", row_index=0, source_data_ref=None)
            _insert_token(conn, run_id, "tok-barrier", "row-1")
            _insert_token(conn, run_id, "tok-queue", "row-1")
            _insert_blocked_work_item(conn, run_id, "tok-barrier", "row-1", barrier_key="agg-node")
            _insert_blocked_work_item(conn, run_id, "tok-queue", "row-1", barrier_key=None, queue_key="llm-rate-limit")
    finally:
        writable.close()

    read_only = LandscapeDB.from_url(f"sqlite:///{db_path}", read_only=True, create_tables=False)
    try:
        assert read_only.is_read_only is True
        recovery = RecoveryManager(read_only, CheckpointManager(read_only))

        assert recovery.count_blocked_barrier_items(run_id) == 1
        assert recovery.count_active_scheduler_work(run_id) == 2
    finally:
        read_only.close()


def test_verify_contract_integrity_returns_contract(
    db: LandscapeDB,
    recovery_manager: RecoveryManager,
) -> None:
    with db.write_connection() as conn:
        _insert_run(conn, "run-contract-ok", status=RunStatus.FAILED, with_contract=True)

    contract = recovery_manager.verify_contract_integrity("run-contract-ok")
    assert isinstance(contract, SchemaContract)
    assert contract.mode == "FIXED"
    assert len(contract.fields) == 1


def test_verify_contract_integrity_raises_empty_resume_state_when_no_sources_recorded(
    db: LandscapeDB,
    recovery_manager: RecoveryManager,
) -> None:
    """Per ADR-025 §3 (elspeth-241608388f), absence of ``run_sources`` rows
    is the interpretable ``EmptyResumeStateError`` ("nothing to resume")
    case — NOT ``CheckpointCorruptionError``. The previous mapping caused
    the CLI's outer ``try`` (which has no audit-corruption handler) to
    bubble an unhandled traceback for what should be a clean exit-1
    "this run is not resumable" message.
    """
    with db.write_connection() as conn:
        _insert_run(conn, "run-contract-missing", status=RunStatus.FAILED, with_contract=False)

    with pytest.raises(EmptyResumeStateError) as exc_info:
        recovery_manager.verify_contract_integrity("run-contract-missing")
    assert exc_info.value.run_id == "run-contract-missing"
    # Subclass relationship is load-bearing: every ``except OrchestrationInvariantError``
    # catch must still match this exception so callers without explicit
    # EmptyResumeStateError handling do not silently miss it.
    assert isinstance(exc_info.value, OrchestrationInvariantError)


def test_verify_contract_integrity_raises_on_hash_mismatch(
    db: LandscapeDB,
    recovery_manager: RecoveryManager,
) -> None:
    valid_contract_json, _ = _create_contract()
    tampered = valid_contract_json.replace('"version_hash":"', '"version_hash":"deadbeef')
    with db.write_connection() as conn:
        _insert_run(
            conn,
            "run-contract-bad-hash",
            status=RunStatus.FAILED,
            contract_json_override=tampered,
        )

    with pytest.raises(CheckpointCorruptionError, match="Contract integrity verification failed"):
        recovery_manager.verify_contract_integrity("run-contract-bad-hash")


def test_verify_contract_integrity_raises_on_malformed_json(
    db: LandscapeDB,
    recovery_manager: RecoveryManager,
) -> None:
    """Malformed contract JSON must raise CheckpointCorruptionError, not raw JSONDecodeError.

    When schema_contract_json is garbage (not valid JSON), the recorder's
    get_run_contract will raise json.JSONDecodeError (via ContractAuditRecord.from_json).
    verify_contract_integrity must catch this and wrap it as CheckpointCorruptionError
    for consistent corruption handling.
    """
    with db.write_connection() as conn:
        _insert_run(
            conn,
            "run-contract-malformed",
            status=RunStatus.FAILED,
            contract_json_override="not valid json {{{",
        )

    with pytest.raises(CheckpointCorruptionError, match="Contract integrity verification failed"):
        recovery_manager.verify_contract_integrity("run-contract-malformed")


def test_verify_contract_integrity_raises_on_missing_keys(
    db: LandscapeDB,
    recovery_manager: RecoveryManager,
) -> None:
    """Contract JSON missing required keys must raise CheckpointCorruptionError.

    If the stored JSON is valid but missing 'mode' or 'fields', the KeyError
    from ContractAuditRecord.from_json must be wrapped as CheckpointCorruptionError.
    """
    with db.write_connection() as conn:
        _insert_run(
            conn,
            "run-contract-missing-keys",
            status=RunStatus.FAILED,
            contract_json_override='{"unexpected": "schema"}',
        )

    with pytest.raises(CheckpointCorruptionError, match="Contract integrity verification failed"):
        recovery_manager.verify_contract_integrity("run-contract-missing-keys")


def test_get_run_private_helper_returns_none_for_missing_run(recovery_manager: RecoveryManager) -> None:
    assert recovery_manager._get_run("missing-run") is None


def test_get_run_private_helper_returns_row_for_existing_run(
    db: LandscapeDB,
    recovery_manager: RecoveryManager,
) -> None:
    with db.write_connection() as conn:
        _insert_run(conn, "run-present", status=RunStatus.FAILED)

    row = recovery_manager._get_run("run-present")
    assert row is not None
    assert row.run_id == "run-present"


def test_get_resume_point_reads_latest_checkpoint_after_can_resume(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
    recovery_manager: RecoveryManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-latest-checkpoint"
    graph = _create_failed_run_with_checkpoint(db, checkpoint_manager, run_id)
    _create_checkpoint(
        checkpoint_manager,
        run_id=run_id,
        sequence_number=99,
        barrier_scalars=None,
        graph=graph,
    )

    # Force can_resume to succeed so we exercise the second get_latest_checkpoint call path.
    monkeypatch.setattr(recovery_manager, "can_resume", lambda _run_id, _graph: type("Check", (), {"can_resume": True})())
    point = recovery_manager.get_resume_point(run_id, graph)

    assert point is not None
    assert point.sequence_number == 99


def test_get_resume_point_revalidates_checkpoint_loaded_after_can_resume(
    db: LandscapeDB,
    checkpoint_manager: CheckpointManager,
    recovery_manager: RecoveryManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-latest-checkpoint-revalidate"
    graph = _create_failed_run_with_checkpoint(db, checkpoint_manager, run_id)
    with db.write_connection() as conn:
        conn.execute(
            checkpoints_table.insert().values(
                checkpoint_id="cp-later-incompatible",
                run_id=run_id,
                sequence_number=99,
                barrier_scalars_json=None,
                created_at=datetime.now(UTC),
                upstream_topology_hash=fake_sha256("later-incompatible-topology"),
                format_version=Checkpoint.CURRENT_FORMAT_VERSION,
            )
        )

    # Simulate a checkpoint appearing after can_resume validated an earlier checkpoint.
    monkeypatch.setattr(recovery_manager, "can_resume", lambda _run_id, _graph: type("Check", (), {"can_resume": True})())

    with pytest.raises(NonResumableRunError) as exc_info:
        recovery_manager.get_resume_point(run_id, graph)
    assert exc_info.value.cause is ResumeRefusalCause.CHECKPOINT_TOPOLOGY_CHANGED
