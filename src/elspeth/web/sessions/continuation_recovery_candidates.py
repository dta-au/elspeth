"""Bounded, advisory discovery of unavailable continuation segment owners.

These observations confer no authority. A recovery or settlement writer must
reprove the original generation, immutable attempt and takeover eligibility in
its locked transaction; discovery may already be stale when it returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Engine, and_, or_, select
from sqlalchemy.sql.elements import ColumnElement

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import sessions_table, web_instances_table
from elspeth.web.sessions.time_normalization import restore_utc


@dataclass(frozen=True, slots=True)
class ContinuationRecoveryCandidate:
    """Original logical and physical identity, never a running-owner context."""

    session_id: UUID
    operation_id: str
    expected_generation: int
    segment_sequence: int
    actor_user_id: str
    auth_provider_type: str
    claim_attempt: int
    session_operation_id: str
    session_operation_epoch: int
    session_operation_lease_token: str
    owner_instance_id: str
    # The parent advances its head while the running segment retains the
    # checkpoint that admitted its current allowance. Both are observed.
    checkpoint_id: str
    segment_checkpoint_id: str
    cancel_requested_at: datetime | None
    session_archived_at: datetime | None


@dataclass(frozen=True, slots=True)
class ContinuationInactiveCandidate:
    """An archived non-running segment, including an actual unstarted claim."""

    session_id: UUID
    operation_id: str
    expected_generation: int
    segment_sequence: int
    actor_user_id: str
    auth_provider_type: str
    checkpoint_id: str
    claim_attempt: int
    claim_token: str | None
    claim_owner_instance_id: str | None
    claim_expires_at: datetime | None


def list_inactive_continuations(engine: Engine, *, limit: int) -> tuple[ContinuationInactiveCandidate, ...]:
    """Read archived queued/interrupted parents without stealing a live claim."""
    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    with engine.connect() as conn:
        now = database_now(conn)
        rows = conn.execute(
            select(
                jobs.c.session_id,
                jobs.c.operation_id,
                jobs.c.ownership_generation,
                segments.c.sequence,
                jobs.c.actor_user_id,
                sessions_table.c.auth_provider_type,
                jobs.c.checkpoint_id,
                jobs.c.attempt,
                segments.c.claim_token,
                segments.c.claim_owner_instance_id,
                segments.c.claim_expires_at,
            )
            .select_from(
                jobs.join(sessions_table, sessions_table.c.id == jobs.c.session_id).join(
                    segments,
                    and_(
                        segments.c.session_id == jobs.c.session_id,
                        segments.c.operation_id == jobs.c.operation_id,
                        segments.c.sequence == jobs.c.current_segment_sequence,
                        segments.c.generation == jobs.c.ownership_generation,
                        segments.c.checkpoint_id == jobs.c.checkpoint_id,
                    ),
                )
            )
            .where(
                jobs.c.lifecycle_version == 2,
                jobs.c.status.in_(("queued", "running")),
                sessions_table.c.archived_at.is_not(None),
                segments.c.state.in_(("queued", "interrupted")),
                or_(segments.c.claim_token.is_(None), segments.c.claim_expires_at <= now),
            )
            .order_by(jobs.c.created_at, jobs.c.session_id, jobs.c.operation_id, segments.c.sequence)
            .limit(limit)
        ).all()
        return tuple(
            ContinuationInactiveCandidate(
                session_id=UUID(row.session_id),
                operation_id=row.operation_id,
                expected_generation=row.ownership_generation,
                segment_sequence=row.sequence,
                actor_user_id=row.actor_user_id,
                auth_provider_type=row.auth_provider_type,
                checkpoint_id=row.checkpoint_id,
                claim_attempt=row.attempt,
                claim_token=row.claim_token,
                claim_owner_instance_id=row.claim_owner_instance_id,
                claim_expires_at=restore_utc(row.claim_expires_at) if row.claim_expires_at is not None else None,
            )
            for row in rows
        )


def list_recoverable_segments(engine: Engine, *, limit: int) -> tuple[ContinuationRecoveryCandidate, ...]:
    """Read current V2 running segments whose exact physical owner is unavailable.

    An explicitly released fence suffices on either supported backend. An
    unreleased fence must have expired; PostgreSQL additionally requires its
    owner's retained membership row to exist and have expired. Missing or
    newly registered live membership never proves death. Stop and archive
    observations remain candidates for the writer's separate disposition.
    The current head and admitted segment checkpoint are distinct once the
    physical owner commits an ordinary snapshot transition.
    """
    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    with engine.connect() as conn:
        now = database_now(conn)
        expired_owner: ColumnElement[bool] = fences.c.lease_expires_at <= now
        head = checkpoints.alias("recovery_head_checkpoint")
        entry = checkpoints.alias("recovery_entry_checkpoint")
        if conn.dialect.name == "postgresql":
            expired_owner = and_(
                expired_owner,
                select(web_instances_table.c.instance_id)
                .where(
                    web_instances_table.c.instance_id == fences.c.owner_instance_id,
                    web_instances_table.c.lease_expires_at <= now,
                )
                .correlate(fences)
                .exists(),
            )
        rows = conn.execute(
            select(
                jobs.c.session_id,
                jobs.c.operation_id,
                jobs.c.ownership_generation,
                segments.c.sequence,
                jobs.c.actor_user_id,
                sessions_table.c.auth_provider_type,
                attempts.c.claim_attempt,
                attempts.c.session_operation_id,
                attempts.c.session_operation_epoch,
                attempts.c.session_operation_lease_token,
                attempts.c.owner_instance_id,
                jobs.c.checkpoint_id,
                segments.c.checkpoint_id.label("segment_checkpoint_id"),
                jobs.c.cancel_requested_at,
                sessions_table.c.archived_at,
            )
            .select_from(
                jobs.join(sessions_table, sessions_table.c.id == jobs.c.session_id)
                .join(
                    segments,
                    and_(
                        segments.c.session_id == jobs.c.session_id,
                        segments.c.operation_id == jobs.c.operation_id,
                        segments.c.sequence == jobs.c.current_segment_sequence,
                        segments.c.generation == jobs.c.ownership_generation,
                    ),
                )
                .join(
                    head,
                    and_(
                        head.c.session_id == jobs.c.session_id,
                        head.c.operation_id == jobs.c.operation_id,
                        head.c.checkpoint_id == jobs.c.checkpoint_id,
                        head.c.cursor == jobs.c.journal_cursor,
                    ),
                )
                .join(
                    entry,
                    and_(
                        entry.c.session_id == segments.c.session_id,
                        entry.c.operation_id == segments.c.operation_id,
                        entry.c.checkpoint_id == segments.c.checkpoint_id,
                        entry.c.cursor <= head.c.cursor,
                    ),
                )
                .join(
                    attempts,
                    and_(
                        attempts.c.session_id == segments.c.session_id,
                        attempts.c.operation_id == segments.c.operation_id,
                        attempts.c.sequence == segments.c.sequence,
                        attempts.c.generation == segments.c.generation,
                        attempts.c.claim_attempt == jobs.c.attempt,
                        attempts.c.session_operation_id == segments.c.session_operation_id,
                        attempts.c.session_operation_epoch == segments.c.session_operation_epoch,
                        attempts.c.session_operation_lease_token == segments.c.session_operation_lease_token,
                        attempts.c.owner_instance_id == segments.c.claim_owner_instance_id,
                    ),
                )
                .join(
                    fences,
                    and_(
                        fences.c.session_id == attempts.c.session_id,
                        fences.c.operation_kind == "compose",
                        fences.c.operation_id == attempts.c.session_operation_id,
                        fences.c.operation_epoch == attempts.c.session_operation_epoch,
                        fences.c.lease_token == attempts.c.session_operation_lease_token,
                        fences.c.owner_instance_id == attempts.c.owner_instance_id,
                    ),
                )
            )
            .where(
                jobs.c.lifecycle_version == 2,
                jobs.c.status.in_(("queued", "running")),
                segments.c.state == "running",
                attempts.c.revoked_at.is_(None),
                attempts.c.outcome.is_(None),
                or_(fences.c.released_at.is_not(None), expired_owner),
            )
            .order_by(jobs.c.created_at, jobs.c.session_id, jobs.c.operation_id, segments.c.sequence)
            .limit(limit)
        ).all()
        return tuple(
            ContinuationRecoveryCandidate(
                session_id=UUID(row.session_id),
                operation_id=row.operation_id,
                expected_generation=row.ownership_generation,
                segment_sequence=row.sequence,
                actor_user_id=row.actor_user_id,
                auth_provider_type=row.auth_provider_type,
                claim_attempt=row.claim_attempt,
                session_operation_id=row.session_operation_id,
                session_operation_epoch=row.session_operation_epoch,
                session_operation_lease_token=row.session_operation_lease_token,
                owner_instance_id=row.owner_instance_id,
                checkpoint_id=row.checkpoint_id,
                segment_checkpoint_id=row.segment_checkpoint_id,
                cancel_requested_at=restore_utc(row.cancel_requested_at) if row.cancel_requested_at is not None else None,
                session_archived_at=restore_utc(row.archived_at) if row.archived_at is not None else None,
            )
            for row in rows
        )
