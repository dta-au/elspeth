"""Group-loss ledger (spec §6.2): the unified replacement for the retired
``coalesce_branch_losses`` table.

The append-only ``group_losses`` table: in-transaction recording (riding the
caller's disposition transaction), the intake/takeover reads, the fenced
replay-cursor adoption mark, and the adoption-context frame guard. Migrated
1:1 from ``branch_losses.py`` (filigree elspeth-ef9c36d767) onto the
group-scoped natural key (run_id, closer_name, group_id, member_key).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, RowMapping

from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, CoordinationToken
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.scheduler import GroupLossSpec
from elspeth.core.ids import generate_id
from elspeth.core.landscape.database import Tier1Engine
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.core.landscape.schema import group_losses_table, token_lineage_frames_table

logger = logging.getLogger(__name__)

# The reason column is a bounded category token ("quarantined",
# "error_routed", "max_retries_exceeded", ...). Derive the bound from the
# schema so the two cannot drift.
_reason_column_type = group_losses_table.c.reason.type
if not isinstance(_reason_column_type, String) or _reason_column_type.length is None:
    raise TypeError("group_losses.reason must be a bounded String column")
_REASON_MAX_LENGTH: int = _reason_column_type.length


@dataclass(frozen=True)
class GroupLoss:
    """One ``group_losses`` row (spec §6.2 durable hand-off ledger)."""

    loss_id: str
    run_id: str
    closer_name: str
    group_id: str
    member_key: str
    token_id: str
    reason: str
    recorded_by: str
    recorded_at: datetime
    adopted_epoch: int | None


def record_group_loss(
    conn: Connection,
    *,
    run_id: str,
    spec: GroupLossSpec,
    recorded_by: str,
    now: datetime,
) -> bool:
    """Append one group-loss row in the CALLER's transaction (§E.5 carried).

    Idempotent on ``(run_id, closer_name, group_id, member_key)``. A
    natural-key hit with a DIFFERENT token_id is token lineage corruption
    (two distinct tokens claiming one member of one group) and raises
    Tier-1; a different reason is tolerated and logged — first durable
    record wins.

    Returns ``True`` if this call inserted the row, ``False`` if the row
    pre-existed.
    """
    # Fail-closed length check BEFORE the INSERT (elspeth-74b795208f): SQLite
    # does not enforce VARCHAR lengths, so an over-wide reason passes every
    # local test and dies only on real PostgreSQL (StringDataRightTruncation)
    # — the audit write killing the run it exists to explain. Deliberately do
    # NOT echo the reason content: failure-path reasons can carry secrets.
    if len(spec.reason) > _REASON_MAX_LENGTH:
        raise AuditIntegrityError(
            f"Group-loss reason is {len(spec.reason)} chars but the reason column holds a "
            f"category token of at most {_REASON_MAX_LENGTH}; producers must record a bare "
            "token (e.g. 'quarantined') and carry detail via the token outcome's error hash."
        )
    loss_id = f"loss_{generate_id()[:12]}"
    values = {
        "loss_id": loss_id,
        "run_id": run_id,
        "closer_name": spec.closer_name,
        "group_id": spec.group_id,
        "member_key": spec.member_key,
        "token_id": spec.token_id,
        "reason": spec.reason,
        "recorded_by": recorded_by,
        "recorded_at": now,
        "adopted_epoch": None,
    }
    dialect = conn.dialect.name
    stmt: Any
    if dialect == "sqlite":
        stmt = sqlite_insert(group_losses_table).values(**values)
    elif dialect == "postgresql":
        stmt = postgresql_insert(group_losses_table).values(**values)
    else:
        raise NotImplementedError(
            f"group-loss recording requires an atomic insert-or-ignore for landscape database dialect {dialect!r}; "
            "supported dialects: sqlite, postgresql"
        )
    inserted_loss_id = conn.execute(
        stmt.on_conflict_do_nothing(index_elements=["run_id", "closer_name", "group_id", "member_key"]).returning(
            group_losses_table.c.loss_id
        )
    ).scalar_one_or_none()
    if inserted_loss_id is not None:
        if inserted_loss_id != loss_id:
            raise AuditIntegrityError(f"Group-loss insert returned unexpected loss_id={inserted_loss_id!r}; expected {loss_id!r}.")
        return True
    existing = (
        conn.execute(
            select(group_losses_table)
            .where(group_losses_table.c.run_id == run_id)
            .where(group_losses_table.c.closer_name == spec.closer_name)
            .where(group_losses_table.c.group_id == spec.group_id)
            .where(group_losses_table.c.member_key == spec.member_key)
        )
        .mappings()
        .one()
    )
    if existing["token_id"] != spec.token_id:
        raise AuditIntegrityError(
            f"Group-loss record for run_id={run_id!r} closer_name={spec.closer_name!r} "
            f"group_id={spec.group_id!r} member_key={spec.member_key!r} already exists with "
            f"token_id={existing['token_id']!r}, but this call claims token_id={spec.token_id!r}; "
            "two distinct tokens cannot lose the same member of one group — token lineage corruption."
        )
    if existing["reason"] != spec.reason:
        logger.warning(
            "group-loss re-record for run %r closer %r group %r member %r tolerated a reason change "
            "(durable %r, offered %r); the first durable record wins",
            run_id,
            spec.closer_name,
            spec.group_id,
            spec.member_key,
            existing["reason"],
            spec.reason,
        )
    return False


def authenticate_adoption_loss(
    conn: Connection,
    *,
    run_id: str,
    spec: GroupLossSpec,
    frame_kind: FrameKind,
    declared_roster: tuple[str, ...] | None,
) -> None:
    """Adoption-context frame guard (spec §6.2): no claimed token exists, so the
    spec authenticates against the durable roster authority instead —
    declared branches (FORK) or a token_lineage_frames row at the group
    (EXPAND). Same self-authentication property, different witness."""
    if frame_kind is FrameKind.FORK:
        if declared_roster is None:
            raise AuditIntegrityError(
                f"FORK adoption-context loss for group {spec.group_id!r} supplied no declared roster; "
                "the fork's declared branch list is the roster authority."
            )
        if spec.member_key in declared_roster:
            return
    else:
        witnessed = conn.execute(
            select(token_lineage_frames_table.c.token_id)
            .where(token_lineage_frames_table.c.run_id == run_id)
            .where(token_lineage_frames_table.c.group_id == spec.group_id)
            .where(token_lineage_frames_table.c.member_key == spec.member_key)
            .limit(1)
        ).scalar_one_or_none()
        if witnessed is not None:
            return
    raise AuditIntegrityError(
        f"Adoption-context group loss for closer {spec.closer_name!r} group {spec.group_id!r} "
        f"member {spec.member_key!r} has no durable roster authority witness "
        f"({frame_kind.name}); losses are staged only for members the group actually minted."
    )


def _loss_from_mapping(row: RowMapping) -> GroupLoss:
    recorded_at = row["recorded_at"]
    if type(recorded_at) is datetime and recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=UTC)
    return GroupLoss(
        loss_id=row["loss_id"],
        run_id=row["run_id"],
        closer_name=row["closer_name"],
        group_id=row["group_id"],
        member_key=row["member_key"],
        token_id=row["token_id"],
        reason=row["reason"],
        recorded_by=row["recorded_by"],
        recorded_at=recorded_at,
        adopted_epoch=row["adopted_epoch"],
    )


class GroupLossRepository:
    """Read/adopt surface over the append-only group-loss ledger."""

    def __init__(self, engine: Tier1Engine) -> None:
        self._engine = engine

    def list_unadopted_group_losses(self, *, run_id: str) -> list[GroupLoss]:
        """Group-loss rows not yet replayed into leader memory (§E.5 intake read).

        The leader replays these through ``notify_branch_lost`` BEFORE each
        drain iteration's trigger evaluation, then marks them via
        :meth:`adopt_group_losses` (journal-first: mark durably FIRST, then
        replay — a crash between mark and replay loses nothing because
        takeover restore derives from the FULL table). Read-only; no event
        is recorded.
        """
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    select(group_losses_table)
                    .where(group_losses_table.c.run_id == run_id)
                    .where(group_losses_table.c.adopted_epoch.is_(None))
                    .order_by(group_losses_table.c.recorded_at, group_losses_table.c.loss_id)
                )
                .mappings()
                .all()
            )
        return [_loss_from_mapping(row) for row in rows]

    def list_group_losses(
        self,
        *,
        run_id: str,
        closer_names: frozenset[str] | None = None,
    ) -> list[GroupLoss]:
        """ALL group-loss rows, adopted or not (§E.4 takeover restore read).

        The new leader rebuilds ``lost_branches`` for still-pending closer
        groups from the full table (the D3 checkpoint scalar is retained as a
        cross-check only) and seeds executor memory directly; unadopted rows
        are then re-marked under its own epoch. Append-only ledger: rows are
        never deleted or consumed destructively. ``closer_names`` scopes a
        mixed-topology restore so unrelated row-union history is not
        materialized. Read-only; no event.
        """
        if closer_names == frozenset():
            return []
        query = select(group_losses_table).where(group_losses_table.c.run_id == run_id)
        if closer_names is not None:
            query = query.where(group_losses_table.c.closer_name.in_(closer_names))
        query = query.order_by(group_losses_table.c.recorded_at, group_losses_table.c.loss_id)
        with self._engine.connect() as conn:
            rows = conn.execute(query).mappings().all()
        return [_loss_from_mapping(row) for row in rows]

    def adopt_group_losses(
        self,
        *,
        run_id: str,
        loss_ids: Sequence[str],
        now: datetime,
        coordination_token: CoordinationToken,
    ) -> int:
        """Fenced replay-cursor mark: ``adopted_epoch NULL → epoch`` (§E.5).

        Same CAS-marker pattern as row adoption; ``coordination_token`` is
        REQUIRED (new-verb doctrine). Returns the number of rows marked —
        fewer than ``len(loss_ids)`` is FINE (already-adopted rows are the
        idempotent skip; in-memory replay dedup is structural because
        ``notify_branch_lost`` is keyed-dict assignment). A stale leader gets
        :class:`RunLeadershipLostError` before any mark.
        """
        if not loss_ids:
            return 0
        with fenced_leader_transaction(
            self._engine,
            token=coordination_token,
            now=now,
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
            verb="adopt_group_losses",
        ) as conn:
            result = conn.execute(
                update(group_losses_table)
                .where(group_losses_table.c.run_id == run_id)
                .where(group_losses_table.c.loss_id.in_(tuple(loss_ids)))
                .where(group_losses_table.c.adopted_epoch.is_(None))
                .values(adopted_epoch=coordination_token.leader_epoch)
            )
            marked = result.rowcount
        return 0 if marked is None else int(marked)

    def stage_escalation_loss(
        self,
        *,
        run_id: str,
        spec: GroupLossSpec,
        frame_kind: FrameKind,
        declared_roster: tuple[str, ...] | None,
        recorded_by: str,
        now: datetime,
        coordination_token: CoordinationToken,
    ) -> bool:
        """Escalation staging (spec §6.3, Task 8): authenticate the spec
        against the durable roster authority, then append the loss row —
        inside ONE fenced leader transaction, the same fencing
        ``adopt_group_losses`` uses. There is no claimed token at intake
        time (this runs out-of-claim, from the barrier-intake pass), so the
        adoption-context guard (``authenticate_adoption_loss``) substitutes
        for the in-claim guard ``_stage_group_loss`` uses. Returns whatever
        ``record_group_loss`` returns: ``True`` if this call inserted the
        row, ``False`` if an identical row already existed (idempotent
        re-derivation at takeover, spec §6.3 item 3).
        """
        with fenced_leader_transaction(
            self._engine,
            token=coordination_token,
            now=now,
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
            verb="stage_escalation_loss",
        ) as conn:
            authenticate_adoption_loss(conn, run_id=run_id, spec=spec, frame_kind=frame_kind, declared_roster=declared_roster)
            return record_group_loss(conn, run_id=run_id, spec=spec, recorded_by=recorded_by, now=now)
