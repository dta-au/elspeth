"""Shared raw-seed helpers for group/lineage audit fixtures (WS5/WS6).

One seeding vocabulary for the surfaces that read the three canonical group
tables (`token_lineage_frames`, `group_records`, `group_losses`): the
group-satisfiability resume gate tests and the Landscape-MCP group query
tests. Column sets follow the raw-seed precedent of
`tests/unit/core/landscape/test_scheduler_repository_group_losses.py` —
extend against `core/landscape/schema.py` as landed, never by
trial-and-error.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import insert

from elspeth.contracts import NodeType, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    group_losses_table,
    group_records_table,
    node_states_table,
    nodes_table,
    rows_table,
    runs_table,
    token_lineage_frames_table,
    tokens_table,
)
from tests.fixtures.landscape import make_landscape_db

RUN_ID = "run-group-lineage-1"
NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
SOURCE_NODE_ID = "source-1"
COALESCE_NODE_ID = "coalesce::merger"
OPENER_NODE_ID = "transform::exploder"
FORK_GROUP = "fg-1"
EXPAND_GROUP = "eg-1"


def payload_json() -> str:
    return TokenSchedulerRepository.serialize_row_payload(PipelineRow({"id": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True)))


def seed_run(db: LandscapeDB, *, status: RunStatus = RunStatus.FAILED) -> None:
    """Seed one run, its three nodes (source / coalesce / opener), and row-1."""
    with db.engine.begin() as conn:
        conn.execute(
            insert(runs_table).values(
                run_id=RUN_ID,
                started_at=NOW,
                config_hash="cfg",
                settings_json="{}",
                canonical_version="v1",
                status=status.value,
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        )
        for node_id, node_type in (
            (SOURCE_NODE_ID, NodeType.SOURCE),
            (COALESCE_NODE_ID, NodeType.COALESCE),
            (OPENER_NODE_ID, NodeType.TRANSFORM),
        ):
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
                    registered_at=NOW,
                )
            )
        conn.execute(
            insert(rows_table).values(
                row_id="row-1",
                run_id=RUN_ID,
                source_node_id=SOURCE_NODE_ID,
                row_index=0,
                source_row_index=0,
                ingest_sequence=0,
                source_data_hash="hash-row-1",
                created_at=NOW,
            )
        )


def seed_fork_member(db: LandscapeDB, *, token_id: str, member_key: str, group_id: str = FORK_GROUP) -> None:
    """Mint one fork-branch token with its lineage frame (depth 0)."""
    with db.engine.begin() as conn:
        conn.execute(insert(tokens_table).values(token_id=token_id, row_id="row-1", run_id=RUN_ID, created_at=NOW))
        conn.execute(
            insert(token_lineage_frames_table).values(
                token_id=token_id,
                run_id=RUN_ID,
                depth=0,
                kind=FrameKind.FORK.value,
                group_id=group_id,
                member_key=member_key,
            )
        )


def seed_expand_frame(
    db: LandscapeDB,
    *,
    token_id: str,
    depth: int,
    group_id: str = EXPAND_GROUP,
    member_key: str | None = None,
) -> None:
    """Append one EXPAND frame to an existing token at the given depth.

    ``member_key`` defaults to the token id — the EXPAND member-key
    convention (spec §2).
    """
    with db.engine.begin() as conn:
        conn.execute(
            insert(token_lineage_frames_table).values(
                token_id=token_id,
                run_id=RUN_ID,
                depth=depth,
                kind=FrameKind.EXPAND.value,
                group_id=group_id,
                member_key=token_id if member_key is None else member_key,
            )
        )


def seed_expand_group(db: LandscapeDB, *, member_count: int, opener_node: str = OPENER_NODE_ID) -> list[str]:
    """Mint an opener token with a node_state at the opener node, the group
    record, and ``member_count`` member tokens with EXPAND frames."""
    with db.engine.begin() as conn:
        conn.execute(insert(tokens_table).values(token_id="tok-opener", row_id="row-1", run_id=RUN_ID, created_at=NOW))
        conn.execute(
            insert(node_states_table).values(
                state_id="state-opener",
                token_id="tok-opener",
                run_id=RUN_ID,
                node_id=opener_node,
                step_index=1,
                attempt=0,
                status="completed",
                input_hash="0" * 64,
                started_at=NOW,
                completed_at=NOW,
            )
        )
        conn.execute(
            insert(group_records_table).values(
                run_id=RUN_ID,
                group_id=EXPAND_GROUP,
                kind=FrameKind.EXPAND.value,
                opener_token_id="tok-opener",
                member_count=member_count,
                created_at=NOW,
            )
        )
        member_ids = [f"tok-member-{i}" for i in range(member_count)]
        for member_id in member_ids:
            conn.execute(insert(tokens_table).values(token_id=member_id, row_id="row-1", run_id=RUN_ID, created_at=NOW))
            conn.execute(
                insert(token_lineage_frames_table).values(
                    token_id=member_id,
                    run_id=RUN_ID,
                    depth=0,
                    kind=FrameKind.EXPAND.value,
                    group_id=EXPAND_GROUP,
                    member_key=member_id,
                )
            )
    return member_ids


def seed_loss(
    db: LandscapeDB,
    *,
    member_key: str,
    token_id: str,
    adopted_epoch: int | None,
    closer_name: str = "merger",
    group_id: str = FORK_GROUP,
    reason: str = "quarantined",
) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            insert(group_losses_table).values(
                loss_id=f"loss-{member_key}",
                run_id=RUN_ID,
                closer_name=closer_name,
                group_id=group_id,
                member_key=member_key,
                token_id=token_id,
                reason=reason,
                recorded_by="worker:test",
                recorded_at=NOW,
                adopted_epoch=adopted_epoch,
            )
        )


def terminalize(db: LandscapeDB, token_id: str) -> None:
    """Write a completed FAILURE/UNROUTED terminal outcome for the token."""
    RecorderFactory(db).data_flow.record_token_outcome(
        ref=TokenRef(token_id=token_id, run_id=RUN_ID),
        outcome=TerminalOutcome.FAILURE,
        path=TerminalPath.UNROUTED,
        error_hash="0" * 16,
    )


def make_seeded_db_and_factory() -> tuple[LandscapeDB, RecorderFactory]:
    """An in-memory audit DB with the base run seeded, plus its factory."""
    db = make_landscape_db()
    seed_run(db)
    return db, RecorderFactory(db)


def ensure_fork_group_record(factory: RecorderFactory, *, run_id: str, group_id: str, opener_token_id: str, member_count: int = 2) -> None:
    """Give a CRAFTED fork group (a lineage frame built by hand rather than by
    ``fork_token``) the ``group_records`` row every real fork mints — idempotent
    per ``(run_id, group_id)``.

    META-38: every merging closer reads the written release fact
    (``closes_group_id``) for the frames it walks, and a frame naming a group
    with NO row fails closed. Crafted-frame fixtures that feed a real
    coalesce/collector therefore seed the row (raw-seed precedent of this
    module) rather than loosening the predicate. ``opener_token_id`` must be
    an already-persisted token of the run (the first branch token the fixture
    persisted is fine — only the FK and the NULL ``closes_group_id`` matter to
    the readers; ``member_count`` is the fixture's declared branch count).
    """
    from sqlalchemy import select

    with factory._db.engine.begin() as conn:
        existing = conn.execute(
            select(group_records_table.c.group_id).where(group_records_table.c.run_id == run_id, group_records_table.c.group_id == group_id)
        ).one_or_none()
        if existing is not None:
            return
        conn.execute(
            insert(group_records_table).values(
                run_id=run_id,
                group_id=group_id,
                kind=FrameKind.FORK.value,
                opener_token_id=opener_token_id,
                member_count=member_count,
                created_at=NOW,
            )
        )
