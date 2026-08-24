"""Group-loss ledger (spec §6.2): the unified replacement for the retired
``coalesce_branch_losses`` §E.5 hand-off ledger.

``record_group_loss`` rides the CALLER's lease-fenced disposition transaction
(record-then-notify uniformity rule); rows are append-only with an
``adopted_epoch`` replay cursor — never deleted, never consumed
destructively. ``list_unadopted_group_losses`` is the leader's per-iteration
replay read; ``adopt_group_losses`` is the fenced cursor mark;
``list_group_losses`` is the §E.4 takeover-restore full read.
``authenticate_adoption_loss`` is the adoption-context frame guard: no
claimed token exists on that path, so the spec authenticates against the
durable roster authority instead (declared FORK roster, or an EXPAND
``token_lineage_frames`` witness).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import insert

from elspeth.contracts import NodeType, RunStatus
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.scheduler import GroupLossSpec
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.scheduler import GroupLossRepository, authenticate_adoption_loss, record_group_loss
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    nodes_table,
    rows_table,
    runs_table,
    token_lineage_frames_table,
    tokens_table,
)
from tests.fixtures.landscape import make_landscape_db

RUN_ID = "run-group-loss-1"
WORKER = f"worker:{RUN_ID}:deadbeef"
_NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)
NODE_ID = "transform-1"
SOURCE_NODE_ID = "source-1"


def _payload_json() -> str:
    from elspeth.contracts.schema_contract import PipelineRow, SchemaContract

    return TokenSchedulerRepository.serialize_row_payload(PipelineRow({"id": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True)))


def _spec(**overrides: object) -> GroupLossSpec:
    base = {
        "closer_name": "merge_paths",
        "group_id": "fg_001",
        "member_key": "path_a",
        "token_id": "tok_a",
        "reason": "quarantined",
    }
    base.update(overrides)
    return GroupLossSpec(**base)  # type: ignore[arg-type]


@pytest.fixture
def db() -> LandscapeDB:
    return make_landscape_db()


def _seed_run_and_nodes(db: LandscapeDB) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            insert(runs_table).values(
                run_id=RUN_ID,
                started_at=_NOW,
                config_hash="cfg",
                settings_json="{}",
                canonical_version="v1",
                status=RunStatus.RUNNING.value,
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        )
        for node_id, node_type in ((SOURCE_NODE_ID, NodeType.SOURCE), (NODE_ID, NodeType.TRANSFORM)):
            conn.execute(
                insert(nodes_table).values(
                    run_id=RUN_ID,
                    node_id=node_id,
                    plugin_name="test",
                    node_type=node_type.value,
                    plugin_version="1.0",
                    determinism="deterministic",
                    config_hash="cfg",
                    config_json="{}",
                    registered_at=_NOW,
                )
            )


def _seed_row_and_token(db: LandscapeDB, *, row_id: str, token_id: str, source_row_index: int) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            insert(rows_table).values(
                row_id=row_id,
                run_id=RUN_ID,
                source_node_id=SOURCE_NODE_ID,
                row_index=source_row_index,
                source_row_index=source_row_index,
                ingest_sequence=source_row_index,
                source_data_hash=f"hash-{row_id}",
                created_at=_NOW,
            )
        )
        conn.execute(insert(tokens_table).values(token_id=token_id, row_id=row_id, run_id=RUN_ID, created_at=_NOW))


@pytest.fixture
def seat_token(db: LandscapeDB) -> CoordinationToken:
    """Seed a RUNNING run + nodes, the natural-key tokens the test bodies
    reference (``tok_a``/``tok_b``/``tok_IMPOSTOR``), and mint the epoch-1
    leader seat."""
    _seed_run_and_nodes(db)
    _seed_row_and_token(db, row_id="row-a", token_id="tok_a", source_row_index=0)
    _seed_row_and_token(db, row_id="row-b", token_id="tok_b", source_row_index=1)
    _seed_row_and_token(db, row_id="row-impostor", token_id="tok_IMPOSTOR", source_row_index=2)
    return RunCoordinationRepository(db.engine).register_run_leader(run_id=RUN_ID, worker_id=WORKER, now=_NOW, window_seconds=80.0)


@pytest.fixture
def seeded_run(db: LandscapeDB, seat_token: CoordinationToken) -> tuple[LandscapeDB, str]:
    return db, RUN_ID


@pytest.fixture
def seeded_run_with_frames(db: LandscapeDB, seat_token: CoordinationToken) -> tuple[LandscapeDB, str]:
    """``seeded_run`` plus one durable ``token_lineage_frames`` row
    (run_id, group_id='eg_001', member_key='tok_child_1')."""
    _seed_row_and_token(db, row_id="row-child", token_id="tok_child_1", source_row_index=3)
    with db.engine.begin() as conn:
        conn.execute(
            insert(token_lineage_frames_table).values(
                token_id="tok_child_1",
                run_id=RUN_ID,
                depth=0,
                kind=FrameKind.EXPAND.value,
                group_id="eg_001",
                member_key="tok_child_1",
            )
        )
    return db, RUN_ID


@pytest.fixture
def seeded_claimed_item(db: LandscapeDB, seat_token: CoordinationToken):
    """One claimed (LEASED) work item, built via the real enqueue+claim path
    (mirrors the predecessor suite's ``TestDispositionComposition._leased_item``)."""
    repo = TokenSchedulerRepository(db.engine)
    _seed_row_and_token(db, row_id="row-claim", token_id="tok-claim", source_row_index=10)
    repo.enqueue_ready(
        run_id=RUN_ID,
        token_id="tok-claim",
        row_id="row-claim",
        node_id=NODE_ID,
        step_index=1,
        ingest_sequence=10,
        row_payload_json=_payload_json(),
        available_at=_NOW,
    )
    claimed = repo.claim_ready(run_id=RUN_ID, lease_owner=WORKER, lease_seconds=60, now=_NOW)
    assert claimed is not None
    return db, repo, claimed


def test_record_group_loss_is_idempotent_on_group_scoped_natural_key(seeded_run):
    db, run_id = seeded_run
    with db.engine.begin() as conn:
        assert record_group_loss(conn, run_id=run_id, spec=_spec(), recorded_by="w1", now=_NOW) is True
        assert record_group_loss(conn, run_id=run_id, spec=_spec(), recorded_by="w2", now=_NOW) is False


def test_same_key_different_token_raises_tier1(seeded_run):
    db, run_id = seeded_run
    with db.engine.begin() as conn:
        record_group_loss(conn, run_id=run_id, spec=_spec(), recorded_by="w1", now=_NOW)
        with pytest.raises(AuditIntegrityError, match="token lineage corruption"):
            record_group_loss(conn, run_id=run_id, spec=_spec(token_id="tok_IMPOSTOR"), recorded_by="w1", now=_NOW)


def test_sibling_inner_groups_do_not_collide(seeded_run):
    """The rev-2 key-collision hazard is structurally gone: same closer, same
    member_key, DISTINCT group_ids — two rows, no conflict."""
    db, run_id = seeded_run
    with db.engine.begin() as conn:
        assert record_group_loss(conn, run_id=run_id, spec=_spec(group_id="fg_001"), recorded_by="w1", now=_NOW)
        assert record_group_loss(conn, run_id=run_id, spec=_spec(group_id="fg_002", token_id="tok_b"), recorded_by="w1", now=_NOW)


def test_takeover_read_returns_full_table_regardless_of_adopted_epoch(seeded_run, seat_token):
    """§E.4 STATED REQUIREMENT (spec §6.2): list_group_losses reads adopted
    AND unadopted rows. A restore filtered by adopted_epoch is the enumerated
    mutant this test kills."""
    db, run_id = seeded_run
    repo = GroupLossRepository(db.engine)
    with db.engine.begin() as conn:
        record_group_loss(conn, run_id=run_id, spec=_spec(member_key="path_a"), recorded_by="w1", now=_NOW)
        record_group_loss(conn, run_id=run_id, spec=_spec(member_key="path_b", token_id="tok_b"), recorded_by="w1", now=_NOW)
    unadopted = repo.list_unadopted_group_losses(run_id=run_id)
    repo.adopt_group_losses(run_id=run_id, loss_ids=[unadopted[0].loss_id], now=_NOW, coordination_token=seat_token)
    assert len(repo.list_unadopted_group_losses(run_id=run_id)) == 1
    assert len(repo.list_group_losses(run_id=run_id)) == 2  # FULL table


def test_authenticate_adoption_loss_fork_accepts_declared_branch(seeded_run):
    db, run_id = seeded_run
    with db.engine.begin() as conn:
        authenticate_adoption_loss(
            conn,
            run_id=run_id,
            spec=_spec(member_key="path_a"),
            frame_kind=FrameKind.FORK,
            declared_roster=("path_a", "path_b"),
        )  # no raise


def test_authenticate_adoption_loss_fork_rejects_undeclared_member(seeded_run):
    db, run_id = seeded_run
    with db.engine.begin() as conn, pytest.raises(AuditIntegrityError, match="roster authority"):
        authenticate_adoption_loss(
            conn,
            run_id=run_id,
            spec=_spec(member_key="phantom"),
            frame_kind=FrameKind.FORK,
            declared_roster=("path_a", "path_b"),
        )


def test_authenticate_adoption_loss_expand_requires_frames_row(seeded_run_with_frames):
    """seeded_run_with_frames seeds one token_lineage_frames row
    (run_id, group_id='eg_001', member_key='tok_child_1')."""
    db, run_id = seeded_run_with_frames
    with db.engine.begin() as conn:
        authenticate_adoption_loss(
            conn,
            run_id=run_id,
            spec=_spec(group_id="eg_001", member_key="tok_child_1", token_id="tok_child_1"),
            frame_kind=FrameKind.EXPAND,
            declared_roster=None,
        )  # no raise
        with pytest.raises(AuditIntegrityError, match="roster authority"):
            authenticate_adoption_loss(
                conn,
                run_id=run_id,
                spec=_spec(group_id="eg_001", member_key="tok_phantom", token_id="tok_phantom"),
                frame_kind=FrameKind.EXPAND,
                declared_roster=None,
            )


def test_mark_failed_records_every_staged_group_loss_in_one_transaction(seeded_claimed_item):
    """The singular §E.5 parameter is now a per-frame collection (spec §6.2):
    every spec in group_losses commits iff the disposition commits."""
    db, repo, claimed = seeded_claimed_item
    losses = (
        _spec(member_key="path_a", token_id=claimed.token_id),
        _spec(group_id="eg_outer", member_key=claimed.token_id, token_id=claimed.token_id, closer_name="page_stitcher"),
    )
    repo.mark_failed(
        work_item_id=claimed.work_item_id,
        now=_NOW,
        expected_lease_owner=claimed.lease_owner,
        group_losses=losses,
    )
    ledger = GroupLossRepository(db.engine).list_group_losses(run_id=claimed.run_id)
    assert {(loss.closer_name, loss.group_id, loss.member_key) for loss in ledger} == {
        ("merge_paths", "fg_001", "path_a"),
        ("page_stitcher", "eg_outer", claimed.token_id),
    }
    assert all(loss.recorded_by for loss in ledger)
